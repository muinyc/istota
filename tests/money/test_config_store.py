"""Tests for money.config_store — DB-backed config storage."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import tomli

import pytest

from istota.money import config_store as cs
from istota.money.core import rules as rule_engine
from istota.money.core.importers.base import NormalizedTransaction
from istota.money.core.transactions import (
    MONARCH_CATEGORY_MAP,
    account_component,
    map_monarch_category_with_config,
)
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
)


# Real-world TOML fixtures (sanitized) lifted from the production configs.

INVOICING_TOML = """\
accounting_path = "."
invoice_output = "invoices/generated"
next_invoice_number = 236

default_entity = "ochotona"
default_ar_account = "Assets:Accounts-Receivable"
default_bank_account = "Assets:SK-Income-Fidelity"
currency = "USD"

[companies.ochotona]
name = "Ochotona LLC"
address = "1 Sample St\\nCity, State 12345"
email = "billing@example.com"
payment_instructions = "Pay via ACH"
ar_account = "Assets:Accounts-Receivable"
bank_account = "Assets:SK-Income-Fidelity"
currency = "USD"

[companies.personal]
name = "Personal"
address = "1 Sample St"
email = "me@example.com"

[clients.acme]
name = "Acme Corp"
address = "100 Acme Way"
email = "ap@acme.example"
terms = "On receipt"
ar_account = "Assets:Accounts-Receivable"
entity = "ochotona"

[clients.acme.invoicing]
schedule = "monthly"
day = 1
ledger_posting = true
reminder_days = 5
notifications = "billing@example.com"
days_until_overdue = 30

[clients.globex]
name = "Globex"
terms = 30
entity = "personal"

[clients.globex.invoicing]
schedule = "monthly"
day = 15
separate = ["consulting", "training"]

[services.consulting]
display_name = "Consulting"
rate = 150.0
type = "hours"
income_account = "Income:Consulting"

[services.flat]
display_name = "Flat Project"
rate = 5000.0
type = "flat"
"""


TAX_TOML = """\
[tax]
filing_status = "mfj"
tax_year = 2026
state = "CA"

[tax.w2]
income = 80000
federal_withholding = 12000
state_withholding = 4000

[tax.estimated_payments]
federal = 5000
state = 1500

[tax.options]
enable_qbi_deduction = true

[tax.accounts]
se_income = ["Income:ScheduleC", "Income:Side"]
se_expenses = ["Expenses:Business"]

[tax.safe_harbor]
prior_year_federal_tax = 25000
prior_year_state_tax = 8000

[tax.rates]
ss_wage_base = 176100
ss_rate = 0.124
medicare_rate = 0.029
se_taxable_fraction = 0.9235
federal_standard_deduction = 30000
state_standard_deduction = 10726
federal_brackets = [[0, 0.1], [23850, 0.12], [96950, 0.22]]
state_brackets = [[0, 0.01], [21428, 0.02]]
"""


MONARCH_TOML = """\
[monarch.sync]
lookback_days = 30

[monarch.profiles.acme]
ledger = "acme"
default_account = "Assets:Acme:Bank"

[monarch.profiles.acme.tags]
include = ["Alice Business"]

[monarch.profiles.acme.accounts]
"Acme Visa" = "Liabilities:Acme:Visa"
"Acme Bank" = "Assets:Acme:Bank"

[monarch.profiles.acme.categories]
"Software" = "Expenses:Acme:Software"

[monarch.profiles.personal]
ledger = "personal"
lookback_days = 60
recategorize_account = "Expenses:Personal:Misc"

[monarch.profiles.personal.tags]
exclude = ["Hide"]

[monarch.profiles.personal.accounts]
"Fidelity VISA" = "Liabilities:Visa-Fidelity"
"""


class TestInitDb:
    def test_creates_all_tables(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for expected in (
            "schema_meta",
            "invoicing_settings", "invoicing_companies", "invoicing_clients",
            "invoicing_services",
            "tax_settings", "tax_account_patterns", "tax_year_rates",
            "monarch_settings", "monarch_profiles", "monarch_account_map",
            "monarch_category_map", "monarch_tag_filters",
        ):
            assert expected in tables, f"missing table: {expected}"

    def test_idempotent(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cs.init_db(db_path)  # no error
        assert cs.get_meta(db_path, "schema_version") == "1"

    def test_global_profile_row_present(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT name FROM monarch_profiles WHERE id = 0"
            ).fetchone()
        assert row[0] == "__global__"


class TestInvoicingRoundTrip:
    def test_round_trip_dict_save_load(self, tmp_path):
        data = tomli.loads(INVOICING_TOML)
        cfg = cs.invoicing_config_from_toml_dict(data)
        db_path = tmp_path / "money.db"
        cs.save_invoicing(db_path, cfg)
        loaded = cs.load_invoicing(db_path)

        assert loaded.accounting_path == "."
        assert loaded.next_invoice_number == 236
        assert loaded.default_entity == "ochotona"
        assert loaded.default_bank_account == "Assets:SK-Income-Fidelity"
        assert loaded.currency == "USD"

        assert set(loaded.companies) == {"ochotona", "personal"}
        assert loaded.companies["ochotona"].bank_account == "Assets:SK-Income-Fidelity"
        assert loaded.company.key == "ochotona"

        assert set(loaded.clients) == {"acme", "globex"}
        acme = loaded.clients["acme"]
        assert acme.terms == "On receipt"
        assert acme.schedule == "monthly"
        assert acme.schedule_day == 1
        assert acme.ledger_posting is True
        assert acme.reminder_days == 5
        assert acme.notifications == "billing@example.com"
        assert acme.days_until_overdue == 30

        globex = loaded.clients["globex"]
        assert globex.terms == 30
        assert globex.schedule_day == 15
        assert globex.separate == ["consulting", "training"]

        assert set(loaded.services) == {"consulting", "flat"}
        assert loaded.services["consulting"].rate == 150.0
        assert loaded.services["flat"].type == "flat"

    def test_to_toml_dict_round_trip(self, tmp_path):
        data = tomli.loads(INVOICING_TOML)
        cfg = cs.invoicing_config_from_toml_dict(data)
        out = cs.invoicing_to_toml_dict(cfg)
        # Re-hydrate, save+load, render again — should match the first render.
        cfg2 = cs.invoicing_config_from_toml_dict(out)
        db_path = tmp_path / "money.db"
        cs.save_invoicing(db_path, cfg2)
        roundtripped = cs.load_invoicing(db_path)
        out2 = cs.invoicing_to_toml_dict(roundtripped)
        assert out == out2

    def test_legacy_company_block(self, tmp_path):
        toml = (
            'accounting_path = "."\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "Default Co"\n\n'
            '[clients.foo]\nname = "Foo"\n\n'
            '[services.bar]\ndisplay_name = "Bar"\nrate = 100\n'
        )
        cfg = cs.invoicing_config_from_toml_dict(tomli.loads(toml))
        assert "default" in cfg.companies
        assert cfg.companies["default"].name == "Default Co"


class TestInvoicingGranular:
    def test_upsert_company_create_then_update_then_noop(self, tmp_path):
        db_path = tmp_path / "money.db"
        comp, state = cs.upsert_company(db_path, "acme", name="Acme")
        assert state == "created"
        comp, state = cs.upsert_company(db_path, "acme", address="123 St")
        assert state == "updated"
        assert comp.address == "123 St"
        comp, state = cs.upsert_company(db_path, "acme", address="123 St")
        assert state == "noop"

    def test_delete_company(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_company(db_path, "acme", name="Acme")
        assert cs.delete_company(db_path, "acme") is True
        assert cs.delete_company(db_path, "acme") is False

    def test_upsert_client(self, tmp_path):
        db_path = tmp_path / "money.db"
        client, state = cs.upsert_client(db_path, "acme", name="Acme")
        assert state == "created"
        client, state = cs.upsert_client(db_path, "acme", terms="NET 15")
        assert state == "updated"
        assert client.terms == "NET 15"

    def test_upsert_service(self, tmp_path):
        db_path = tmp_path / "money.db"
        svc, state = cs.upsert_service(db_path, "consulting",
                                       display_name="Consulting", rate=150.0)
        assert state == "created"
        assert svc.rate == 150.0


class TestTaxRoundTrip:
    def test_round_trip(self, tmp_path):
        data = tomli.loads(TAX_TOML)
        cfg = cs.tax_config_from_toml_dict(data)
        db_path = tmp_path / "money.db"
        # The TOML importer's path: these rates really are the user's own, for
        # this config's own year and filing status.
        cs.save_tax(db_path, cfg, write_schedules=True)
        loaded = cs.load_tax(db_path)

        assert loaded.filing_status == "mfj"
        assert loaded.tax_year == 2026
        assert loaded.w2_income == 80000
        assert loaded.federal_estimated_paid == 5000
        assert loaded.enable_qbi_deduction is True
        assert sorted(loaded.se_income_accounts) == [
            "Income:ScheduleC", "Income:Side",
        ]
        assert loaded.prior_year_federal_tax == 25000
        assert loaded.federal_standard_deduction == 30000.0
        assert loaded.state == "CA"
        assert loaded.state_standard_deduction == 10726.0
        assert loaded.federal_brackets == [[0, 0.1], [23850, 0.12], [96950, 0.22]]
        assert loaded.ss_wage_base == 176100

    def test_to_toml_dict_round_trip(self, tmp_path):
        data = tomli.loads(TAX_TOML)
        cfg = cs.tax_config_from_toml_dict(data)
        out = cs.tax_to_toml_dict(cfg)
        cfg2 = cs.tax_config_from_toml_dict(out)
        db_path = tmp_path / "money.db"
        cs.save_tax(db_path, cfg2, write_schedules=True)
        roundtripped = cs.load_tax(db_path)
        out2 = cs.tax_to_toml_dict(roundtripped)
        assert out == out2

    def test_patterns_add_remove(self, tmp_path):
        db_path = tmp_path / "money.db"
        assert cs.add_tax_pattern(db_path, "se_income", "Income:Side") == "created"
        assert cs.add_tax_pattern(db_path, "se_income", "Income:Side") == "noop"
        patterns = cs.list_tax_patterns(db_path)
        assert "Income:Side" in patterns["se_income"]
        assert cs.remove_tax_pattern(db_path, "se_income", "Income:Side") is True

    def test_year_rates_upsert(self, tmp_path):
        db_path = tmp_path / "money.db"
        state = cs.upsert_tax_year_rates(
            db_path, 2026,
            ss_wage_base=176100, ss_rate=0.124, federal_standard_deduction=30000,
        )
        assert state == "created"
        state = cs.upsert_tax_year_rates(
            db_path, 2026, federal_standard_deduction=30000,
        )
        assert state == "noop"
        state = cs.upsert_tax_year_rates(db_path, 2026, ca_standard_deduction=10726)
        assert state == "updated"


class TestMonarchRoundTrip:
    def test_round_trip(self, tmp_path):
        data = tomli.loads(MONARCH_TOML)
        cfg = cs.monarch_config_from_toml_dict(data)
        db_path = tmp_path / "money.db"
        cs.save_monarch(db_path, cfg)
        loaded = cs.load_monarch(db_path)

        assert loaded.sync.lookback_days == 30
        # Profiles preserved
        names = sorted(p.name for p in loaded.profiles)
        assert names == ["acme", "personal"]

        acme = next(p for p in loaded.profiles if p.name == "acme")
        assert acme.ledger == "acme"
        assert acme.sync.default_account == "Assets:Acme:Bank"
        assert acme.tags.include == ["Alice Business"]
        assert acme.accounts == {
            "Acme Visa": "Liabilities:Acme:Visa",
            "Acme Bank": "Assets:Acme:Bank",
        }
        assert acme.categories == {"Software": "Expenses:Acme:Software"}

        personal = next(p for p in loaded.profiles if p.name == "personal")
        assert personal.sync.lookback_days == 60
        assert personal.sync.recategorize_account == "Expenses:Personal:Misc"
        assert personal.tags.exclude == ["Hide"]

    def test_to_toml_dict_round_trip(self, tmp_path):
        data = tomli.loads(MONARCH_TOML)
        cfg = cs.monarch_config_from_toml_dict(data)
        out = cs.monarch_to_toml_dict(cfg)
        cfg2 = cs.monarch_config_from_toml_dict(out)
        db_path = tmp_path / "money.db"
        cs.save_monarch(db_path, cfg2)
        roundtripped = cs.load_monarch(db_path)
        out2 = cs.monarch_to_toml_dict(roundtripped)
        assert out == out2

    def test_credentials_omitted_from_export(self, tmp_path):
        cfg = MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[],
        )
        out = cs.monarch_to_toml_dict(cfg)
        assert "session_id" not in out["monarch"]
        assert "csrftoken" not in out["monarch"]

    def test_credentials_loaded_from_secrets(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[],
        )
        cs.save_monarch(db_path, cfg)
        loaded = cs.load_monarch(
            db_path,
            secrets={"monarch": {"session_id": "SID-x", "csrftoken": "CSRF-y"}},
        )
        assert loaded.credentials.session_id == "SID-x"
        assert loaded.credentials.csrftoken == "CSRF-y"


class TestMonarchGranular:
    def test_profile_lifecycle(self, tmp_path):
        db_path = tmp_path / "money.db"
        prof, state = cs.upsert_monarch_profile(
            db_path, "acme", ledger="acme",
        )
        assert state == "created"
        assert prof["ledger"] == "acme"
        prof, state = cs.upsert_monarch_profile(
            db_path, "acme", lookback_days=60,
        )
        assert state == "updated"
        assert cs.delete_monarch_profile(db_path, "acme") is True
        assert cs.delete_monarch_profile(db_path, "acme") is False

    def test_global_profile_cannot_be_deleted(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        assert cs.delete_monarch_profile(db_path, "__global__") is False

    def test_account_map_set_unset(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_monarch_profile(db_path, "acme", ledger="acme")
        assert cs.set_account_map_entry(
            db_path, "acme", "Visa", "Liabilities:Visa",
        ) == "created"
        assert cs.set_account_map_entry(
            db_path, "acme", "Visa", "Liabilities:Visa",
        ) == "noop"
        assert cs.set_account_map_entry(
            db_path, "acme", "Visa", "Liabilities:NewVisa",
        ) == "updated"
        assert cs.get_account_map(db_path, "acme") == {
            "Visa": "Liabilities:NewVisa",
        }
        assert cs.unset_account_map_entry(db_path, "acme", "Visa") is True

    def test_account_map_global_scope(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.set_account_map_entry(db_path, None, "Bank", "Assets:Bank")
        assert cs.get_account_map(db_path, None) == {"Bank": "Assets:Bank"}

    def test_unknown_profile_raises(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with pytest.raises(ValueError, match="nonexistent"):
            cs.set_account_map_entry(db_path, "nonexistent", "X", "Assets:Bank")

    def test_map_rejects_unparseable_account(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with pytest.raises(ValueError, match="category-map"):
            cs.set_category_map_entry(
                db_path, None, "Internet Services (Reimbursed)",
                "Expenses:Uncategorized:InternetServices(Reimbursed)",
            )
        with pytest.raises(ValueError, match="account-map"):
            cs.set_account_map_entry(db_path, None, "Visa", "Liabilities Visa")
        with pytest.raises(ValueError, match="category-map"):
            cs.replace_category_map(db_path, None, {"Fees": "Expenses:Fees (Bank)"})
        assert cs.get_category_map(db_path, None) == {}

    def test_map_accepts_valid_account(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.set_category_map_entry(
            db_path, None, "Internet Services (Reimbursed)",
            "Expenses:Internet-Services",
        )
        assert cs.get_category_map(db_path, None) == {
            "Internet Services (Reimbursed)": "Expenses:Internet-Services",
        }

    def test_tag_filters(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_monarch_profile(db_path, "acme", ledger="acme")
        assert cs.add_tag_filter(db_path, "acme", "include", "Biz") == "created"
        assert cs.add_tag_filter(db_path, "acme", "include", "Biz") == "noop"
        assert cs.get_tag_filters(db_path, "acme") == {
            "include": ["Biz"], "exclude": [],
        }
        assert cs.remove_tag_filter(db_path, "acme", "include", "Biz") is True


class TestSchemaMeta:
    def test_has_data_helpers(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        assert cs.has_invoicing_data(db_path) is False
        assert cs.has_tax_data(db_path) is False
        assert cs.has_monarch_data(db_path) is False
        cs.upsert_client(db_path, "acme", name="Acme")
        assert cs.has_invoicing_data(db_path) is True

    def test_meta_set_get(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cs.set_meta(db_path, "test_key", "test_value")
        assert cs.get_meta(db_path, "test_key") == "test_value"


class TestReplaceVsMerge:
    def test_save_replace_truncates(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "old", name="Old Client")
        new_cfg = InvoicingConfig(
            accounting_path=".",
            invoice_output="invoices/generated",
            next_invoice_number=1,
            company=CompanyConfig(name="X", key="x"),
            clients={"new": ClientConfig(key="new", name="New")},
            services={},
            companies={"x": CompanyConfig(name="X", key="x")},
            default_entity="x",
        )
        cs.save_invoicing(db_path, new_cfg, replace_collections=True)
        loaded = cs.load_invoicing(db_path)
        assert set(loaded.clients) == {"new"}

    def test_save_merge_preserves_existing(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "old", name="Old Client")
        new_cfg = InvoicingConfig(
            accounting_path=".",
            invoice_output="invoices/generated",
            next_invoice_number=1,
            company=CompanyConfig(name="X", key="x"),
            clients={"new": ClientConfig(key="new", name="New")},
            services={},
            companies={"x": CompanyConfig(name="X", key="x")},
            default_entity="x",
        )
        cs.save_invoicing(db_path, new_cfg, replace_collections=False)
        loaded = cs.load_invoicing(db_path)
        assert {"new", "old"} <= set(loaded.clients)


# =============================================================================
# Regression tests from mulder/scully review
# =============================================================================


class TestHasDataExcludesScalarRoundTrip:
    """Mulder P0: save→load→save of empty cfg must not flag DB-populated."""

    def test_tax_save_load_save_does_not_block_migration(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = cs.load_tax(db_path)  # empty defaults
        cs.save_tax(db_path, cfg)
        # tax_settings now has filing_status + tax_year, but the section
        # should NOT be considered "populated" — collection tables are empty.
        assert cs.has_tax_data(db_path) is False
        # Once we add a real pattern, it does count as populated.
        cs.add_tax_pattern(db_path, "se_income", "Income:Real")
        assert cs.has_tax_data(db_path) is True

    def test_monarch_save_load_save_does_not_block_migration(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = cs.load_monarch(db_path)  # empty defaults
        cs.save_monarch(db_path, cfg)
        # monarch_settings has the three sync defaults; not "populated".
        assert cs.has_monarch_data(db_path) is False
        cs.upsert_monarch_profile(db_path, "real", ledger="real")
        assert cs.has_monarch_data(db_path) is True


class TestReplaceTaxPatterns:
    """Mulder P1 #4: replace_tax_patterns helper used by routes."""

    def test_replace_per_kind(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.add_tax_pattern(db_path, "se_income", "Income:Old")
        cs.add_tax_pattern(db_path, "se_expense", "Expenses:Old")
        cs.replace_tax_patterns(db_path, {"se_income": ["Income:New"]})
        patterns = cs.list_tax_patterns(db_path)
        assert patterns["se_income"] == ["Income:New"]
        # se_expense untouched (not in the dict)
        assert patterns["se_expense"] == ["Expenses:Old"]

    def test_unknown_kind_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            cs.replace_tax_patterns(tmp_path / "money.db", {"bogus": ["x"]})


# =============================================================================
# Invoicing collection validation (money-config-editing spec, Stage 1)
#
# The invariants whose violation changes behaviour *silently* live in the
# store, not the route, so the CLI and the agent are held to them too.
# =============================================================================


class TestKeyValidation:
    def test_new_key_must_be_slug_shaped(self, tmp_path):
        db_path = tmp_path / "money.db"
        for bad in ("has space", "has.dot", "-leading", "", "a" * 65):
            with pytest.raises(ValueError, match="key"):
                cs.upsert_client(db_path, bad, name="X")

    def test_valid_keys_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        for good in ("acme", "acme-corp", "acme_corp", "9to5", "a" * 64):
            cs.upsert_client(db_path, good, name="X")

    def test_existing_nonconforming_key_still_updatable(self, tmp_path):
        """A legacy key with a dot in it must stay editable.

        The rule exists so *new* keys stay TOML- and CLI-friendly; enforcing
        it on every write would lock a user out of their own data.
        """
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_clients(key, name) VALUES (?, ?)",
                ("legacy.key", "Legacy"),
            )
        client, state = cs.upsert_client(db_path, "legacy.key", email="x@example.com")
        assert state == "updated"
        assert client.email == "x@example.com"

    def test_applies_to_companies_and_services(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="key"):
            cs.upsert_company(db_path, "bad key", name="X")
        with pytest.raises(ValueError, match="key"):
            cs.upsert_service(db_path, "bad key", display_name="X", rate=1)


class TestServiceValidation:
    def test_type_is_a_closed_set(self, tmp_path):
        db_path = tmp_path / "money.db"
        # "hourly" is the plausible typo: entry_line_item has no branch for it
        # and silently bills as hours.
        with pytest.raises(ValueError, match="type"):
            cs.upsert_service(db_path, "consulting", type="hourly")
        for good in ("hours", "days", "flat", "other"):
            cs.upsert_service(db_path, "consulting", type=good)

    def test_rate_must_be_a_finite_non_negative_number(self, tmp_path):
        db_path = tmp_path / "money.db"
        for bad in ("abc", -1, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="rate"):
                cs.upsert_service(db_path, "consulting", rate=bad)
        cs.upsert_service(db_path, "consulting", rate="150.5")
        assert cs.load_invoicing(db_path).services["consulting"].rate == 150.5

    def test_income_account_shape(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="income_account"):
            cs.upsert_service(db_path, "consulting", income_account="income consulting")
        cs.upsert_service(db_path, "consulting", income_account="Income:Consulting")
        # Empty clears the field rather than failing the shape check.
        cs.upsert_service(db_path, "consulting", income_account="")

    def test_unknown_field_rejected(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="unknown"):
            cs.upsert_service(db_path, "consulting", rat=150)


class TestClientValidation:
    def test_schedule_is_a_closed_set(self, tmp_path):
        db_path = tmp_path / "money.db"
        # check_scheduled_invoices only acts on "monthly" — anything else is
        # accepted and then never fires.
        with pytest.raises(ValueError, match="schedule"):
            cs.upsert_client(db_path, "acme", schedule="weekly")
        for good in ("on-demand", "monthly"):
            cs.upsert_client(db_path, "acme", schedule=good)

    def test_schedule_day_range(self, tmp_path):
        db_path = tmp_path / "money.db"
        for bad in (0, 40, "x", 1.5):
            with pytest.raises(ValueError, match="schedule_day"):
                cs.upsert_client(db_path, "acme", schedule_day=bad)
        cs.upsert_client(db_path, "acme", schedule_day=15)

    def test_non_negative_day_counts(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="reminder_days"):
            cs.upsert_client(db_path, "acme", reminder_days=-1)
        with pytest.raises(ValueError, match="days_until_overdue"):
            cs.upsert_client(db_path, "acme", days_until_overdue=-1)
        cs.upsert_client(db_path, "acme", reminder_days=0, days_until_overdue=45)

    def test_terms_int_or_nonempty_string(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "acme", terms=30)
        cs.upsert_client(db_path, "acme", terms="NET 15")
        with pytest.raises(ValueError, match="terms"):
            cs.upsert_client(db_path, "acme", terms=-5)
        with pytest.raises(ValueError, match="terms"):
            cs.upsert_client(db_path, "acme", terms="")

    def test_ar_account_shape(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="ar_account"):
            cs.upsert_client(db_path, "acme", ar_account="assets receivable")
        cs.upsert_client(db_path, "acme", ar_account="Assets:Accounts-Receivable")
        cs.upsert_client(db_path, "acme", ar_account="")

    def test_booleans_are_not_numbers(self, tmp_path):
        """JSON `true` is an int to Python and would sail into a day field."""
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="schedule_day"):
            cs.upsert_client(db_path, "acme", schedule_day=True)

    def test_unknown_field_rejected(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="unknown"):
            cs.upsert_client(db_path, "acme", nmae="Acme")


class TestCompanyValidation:
    def test_account_and_currency_shape(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="bank_account"):
            cs.upsert_company(db_path, "ochotona", bank_account="checking")
        with pytest.raises(ValueError, match="currency"):
            cs.upsert_company(db_path, "ochotona", currency="us dollars")
        cs.upsert_company(
            db_path, "ochotona",
            bank_account="Assets:Bank:Checking", currency="USD",
            ar_account="Assets:Accounts-Receivable",
        )

    def test_multiline_text_fields_allowed(self, tmp_path):
        """Address and payment instructions are genuinely multi-line."""
        db_path = tmp_path / "money.db"
        comp, _ = cs.upsert_company(
            db_path, "ochotona", address="1 St\nCity", payment_instructions="Wire\nIBAN",
        )
        assert comp.address == "1 St\nCity"

    def test_unknown_field_rejected(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="unknown"):
            cs.upsert_company(db_path, "ochotona", nmae="X")


class TestValidationRejectsBeforeWriting:
    def test_failed_upsert_leaves_no_row(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError):
            cs.upsert_service(db_path, "consulting", type="hourly")
        assert "consulting" not in cs.load_invoicing(db_path).services


class TestClientKeyIsLowercase:
    """A mixed-case client key matches no work entry, so its work never bills.

    `add_work_entry` stores `client.lower()` and `build_line_items` looks the
    client up by the entry's (lowercased) key, so an `Acme` config key silently
    produces empty invoices. Only clients are constrained — `service` and
    `entity` are stored verbatim on the entry.
    """

    def test_mixed_case_client_key_refused_on_create(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="lowercase"):
            cs.upsert_client(db_path, "Acme", name="Acme Corp")
        assert "Acme" not in cs.load_invoicing(db_path).clients

    def test_lowercase_client_key_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        client, state = cs.upsert_client(db_path, "acme", name="Acme Corp")
        assert (client.key, state) == ("acme", "created")

    def test_existing_mixed_case_client_stays_editable(self, tmp_path):
        """The rule fires on create only, so a legacy row can still be fixed."""
        db_path = tmp_path / "money.db"
        cs.upsert_company(db_path, "main")  # unrelated collection, still mixed-case ok
        cs.upsert_company(db_path, "Main")
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_clients(key, name) VALUES (?, ?)", ("Legacy", "Legacy"),
            )
        client, _ = cs.upsert_client(db_path, "Legacy", name="Renamed")
        assert client.name == "Renamed"

    def test_entities_and_services_may_be_mixed_case(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_company(db_path, "MainCo", name="Main Co")
        cs.upsert_service(db_path, "DesignWork", display_name="Design")
        cfg = cs.load_invoicing(db_path)
        assert "MainCo" in cfg.companies
        assert "DesignWork" in cfg.services


class TestUnchangedFieldsAreGrandfathered:
    """A legacy row with one bad value has to stay editable.

    A form seeds every input from the stored value and sends the lot back, so
    validating a field the caller didn't change makes such a row permanently
    unsaveable — and the error names a field the user never touched.
    """

    def _legacy_service(self, db_path):
        cs.init_db(db_path)
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_services(key, display_name, rate, type) "
                "VALUES (?, ?, ?, ?)", ("consulting", "Consulting", 150.0, "hourly"),
            )

    def test_resending_an_unchanged_bad_type_is_allowed(self, tmp_path):
        db_path = tmp_path / "money.db"
        self._legacy_service(db_path)
        svc, _ = cs.upsert_service(
            db_path, "consulting", display_name="Renamed", type="hourly", rate=150.0,
        )
        assert svc.display_name == "Renamed"
        assert svc.type == "hourly"

    def test_changing_a_bad_value_to_another_bad_one_is_refused(self, tmp_path):
        db_path = tmp_path / "money.db"
        self._legacy_service(db_path)
        with pytest.raises(ValueError, match="type"):
            cs.upsert_service(db_path, "consulting", type="weekly")

    def test_legacy_client_schedule_survives_a_rename(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_clients(key, name, schedule) VALUES (?, ?, ?)",
                ("acme", "Acme", "weekly"),
            )
        client, _ = cs.upsert_client(db_path, "acme", name="Acme Corp", schedule="weekly")
        assert client.name == "Acme Corp"

    def test_legacy_account_survives_a_rename(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_companies(key, name, ar_account) VALUES (?, ?, ?)",
                ("main", "Main", "assets:ar"),
            )
        comp, _ = cs.upsert_company(db_path, "main", name="Main Co", ar_account="assets:ar")
        assert comp.name == "Main Co"
        with pytest.raises(ValueError, match="ar_account"):
            cs.upsert_company(db_path, "main", ar_account="still:not valid")

    def test_a_new_record_gets_no_exemption(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="type"):
            cs.upsert_service(db_path, "new", type="hourly")


class TestTermsAsNumericString:
    """The column is TEXT and the loader coerces it back, so "-5" *is* -5."""

    def test_negative_numeric_string_refused(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="terms"):
            cs.upsert_client(db_path, "acme", terms="-5")

    def test_a_label_is_still_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        client, _ = cs.upsert_client(db_path, "acme", terms="NET 15")
        assert client.terms == "NET 15"

    def test_a_non_negative_numeric_string_round_trips_as_int(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "acme", terms="45")
        assert cs.load_invoicing(db_path).clients["acme"].terms == 45


class TestAccountShapeIsUnicodeAware:
    """Beancount's own account regex is Unicode; an ASCII-only check locks a
    non-English ledger out of the account it has been posting to."""

    @pytest.mark.parametrize("account", [
        "Assets:Forderungen:Müller",
        "Assets:Accounts-Receivable",
        "Income:Consulting",
        "Aktiva:Bank:Girokonto",
        "Assets:Bank:2024",
    ])
    def test_valid_accounts_accepted(self, tmp_path, account):
        db_path = tmp_path / f"{abs(hash(account))}.db"
        cs.upsert_company(db_path, "main", ar_account=account)

    @pytest.mark.parametrize("account", [
        "assets:ar",            # lowercase root
        "Assets",               # single component
        "Assets:Bank_Checking",  # underscore
        "Assets: Bank",         # space
    ])
    def test_invalid_accounts_refused(self, tmp_path, account):
        db_path = tmp_path / f"{abs(hash(account))}.db"
        with pytest.raises(ValueError, match="ar_account"):
            cs.upsert_company(db_path, "main", ar_account=account)

    def test_single_letter_commodity_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        comp, _ = cs.upsert_company(db_path, "main", currency="X")
        assert comp.currency == "X"


class TestLogoStaysInsideTheWorkspace:
    """The logo is base64-embedded into the PDF, resolved as
    `accounting_path / logo` — pathlib lets an absolute operand escape."""

    @pytest.mark.parametrize("logo", ["/etc/passwd", "../../secrets.png", "~/private.png"])
    def test_escaping_paths_refused(self, tmp_path, logo):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="logo"):
            cs.upsert_company(db_path, "main", logo=logo)

    def test_relative_path_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        comp, _ = cs.upsert_company(db_path, "main", logo="invoices/logo.png")
        assert comp.logo == "invoices/logo.png"


class TestCreateOnly:
    """The 409 is decided inside the write transaction, so two concurrent
    creates can't both pass a pre-check and have the second overwrite."""

    def test_create_only_refuses_an_existing_key(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "acme", name="Acme")
        with pytest.raises(cs.KeyExistsError):
            cs.upsert_client(db_path, "acme", create_only=True, name="Other")
        assert cs.load_invoicing(db_path).clients["acme"].name == "Acme"

    def test_create_only_allows_a_fresh_key(self, tmp_path):
        db_path = tmp_path / "money.db"
        _, state = cs.upsert_service(db_path, "design", create_only=True, display_name="Design")
        assert state == "created"

    def test_key_exists_error_is_a_value_error(self, tmp_path):
        """So an `except ValueError` caller keeps behaving as before."""
        assert issubclass(cs.KeyExistsError, ValueError)


class TestSaveInvoicingSanitizes:
    """`save_invoicing` is the bulk path the migration and `config import` use.

    It bypassed the per-field validation entirely, so the exact values the
    granular ops exist to keep out could still land in the store.
    """

    def test_out_of_set_service_type_is_coerced(self, tmp_path, caplog):
        db_path = tmp_path / "money.db"
        cfg = InvoicingConfig(
            accounting_path="", invoice_output="", next_invoice_number=1,
            company=CompanyConfig(name="Main", key="main"),
            clients={}, services={
                "consulting": ServiceConfig(
                    key="consulting", display_name="Consulting", rate=150.0, type="hourly",
                ),
            },
        )
        with caplog.at_level("WARNING"):
            cs.save_invoicing(db_path, cfg)
        assert cs.load_invoicing(db_path).services["consulting"].type == "hours"
        assert "money_config_sanitized" in caplog.text

    def test_out_of_set_client_schedule_is_coerced(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = InvoicingConfig(
            accounting_path="", invoice_output="", next_invoice_number=1,
            company=CompanyConfig(name="Main", key="main"),
            clients={
                "acme": ClientConfig(key="acme", name="Acme", schedule="weekly"),
            },
            services={},
        )
        cs.save_invoicing(db_path, cfg)
        assert cs.load_invoicing(db_path).clients["acme"].schedule == "on-demand"

    def test_a_conforming_config_is_untouched(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = InvoicingConfig(
            accounting_path="", invoice_output="", next_invoice_number=1,
            company=CompanyConfig(name="Main", key="main"),
            clients={"acme": ClientConfig(key="acme", name="Acme", schedule="monthly")},
            services={
                "design": ServiceConfig(key="design", display_name="Design", rate=90.0,
                                        type="flat"),
            },
        )
        cs.save_invoicing(db_path, cfg)
        loaded = cs.load_invoicing(db_path)
        assert loaded.clients["acme"].schedule == "monthly"
        assert loaded.services["design"].type == "flat"


class TestInvoicingScalarShapes:
    def test_default_accounts_are_shape_checked(self):
        with pytest.raises(ValueError, match="default_ar_account"):
            cs.check_invoicing_scalars({"default_ar_account": "assets ar"})
        cs.check_invoicing_scalars({"default_ar_account": "Assets:Accounts-Receivable"})

    def test_currency_is_shape_checked(self):
        with pytest.raises(ValueError, match="currency"):
            cs.check_invoicing_scalars({"currency": "us dollars"})
        cs.check_invoicing_scalars({"currency": "EUR"})

    def test_blank_values_are_a_noop(self):
        cs.check_invoicing_scalars({"default_ar_account": "", "currency": ""})


class TestMonarchAccountsReachTheLedger:
    """Every config value naming an account the sync posts to is shape-checked.

    The map writers were guarded first, but they are not the only route: a
    profile's `default_account` is what `map_monarch_account` returns verbatim
    for an unmapped Monarch account, and `save_monarch` writes both the maps
    and the profile rows without going through the guarded wrappers.
    """

    def test_profile_create_rejects_unparseable_default_account(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with pytest.raises(ValueError, match="default_account"):
            cs.upsert_monarch_profile(
                db_path, "acme", ledger="acme",
                default_account="Assets:Bank (Checking)",
            )
        assert cs.list_monarch_profiles(db_path) == []

    def test_profile_create_rejects_unparseable_recategorize_account(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with pytest.raises(ValueError, match="recategorize_account"):
            cs.upsert_monarch_profile(
                db_path, "acme", ledger="acme",
                recategorize_account="Expenses:Personal Expense",
            )

    def test_profile_update_rejects_unparseable_default_account(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cs.upsert_monarch_profile(
            db_path, "acme", ledger="acme",
            default_account="Assets:Bank:Checking",
        )
        with pytest.raises(ValueError, match="default_account"):
            cs.upsert_monarch_profile(
                db_path, "acme", default_account="Assets:Bank (Checking)",
            )
        row = cs.list_monarch_profiles(db_path)[0]
        assert row["default_account"] == "Assets:Bank:Checking"

    def test_profile_accepts_a_valid_account(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        _, state = cs.upsert_monarch_profile(
            db_path, "acme", ledger="acme",
            default_account="Assets:Bank:Checking",
            recategorize_account="Expenses:Personal-Expense",
        )
        assert state == "created"

    def _config(self, **kw) -> MonarchConfig:
        return MonarchConfig(
            credentials=MonarchCredentials(),
            sync=kw.pop("sync", MonarchSyncSettings()),
            accounts=kw.pop("accounts", {}),
            categories=kw.pop("categories", {}),
            tags=MonarchTagFilters(),
            profiles=kw.pop("profiles", []),
        )

    def test_save_rejects_unparseable_category_map_target(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cfg = self._config(categories={
            "Internet Services (Reimbursed)":
                "Expenses:Uncategorized:InternetServices(Reimbursed)",
        })
        with pytest.raises(ValueError, match="category-map"):
            cs.save_monarch(db_path, cfg, replace_collections=True)
        assert cs.get_category_map(db_path, None) == {}

    def test_save_rejects_unparseable_account_map_target(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cfg = self._config(accounts={"Visa": "Liabilities Visa"})
        with pytest.raises(ValueError, match="account-map"):
            cs.save_monarch(db_path, cfg, replace_collections=True)

    def test_save_rejects_unparseable_profile_default_account(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cfg = self._config(profiles=[MonarchProfile(
            name="acme", ledger="acme",
            sync=MonarchSyncSettings(default_account="Assets:Bank (Checking)"),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )])
        with pytest.raises(ValueError, match="default_account"):
            cs.save_monarch(db_path, cfg, replace_collections=True)

    def test_save_accepts_a_valid_config(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cfg = self._config(
            categories={"Internet Services (Reimbursed)": "Expenses:Internet-Services"},
            accounts={"Visa": "Liabilities:Visa-Fidelity"},
        )
        cs.save_monarch(db_path, cfg, replace_collections=True)
        assert cs.get_category_map(db_path, None) == {
            "Internet Services (Reimbursed)": "Expenses:Internet-Services",
        }

    def test_a_rejected_save_leaves_the_stored_config_alone(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cs.save_monarch(
            db_path,
            self._config(categories={"Internet Services": "Expenses:Internet-Services"}),
            replace_collections=True,
        )
        bad = self._config(categories={"Fees": "Expenses:Fees (Bank)"})
        with pytest.raises(ValueError, match="category-map"):
            cs.save_monarch(db_path, bad, replace_collections=True)
        assert cs.get_category_map(db_path, None) == {
            "Internet Services": "Expenses:Internet-Services",
        }

    def test_the_global_sync_accounts_may_not_be_empty(self, tmp_path):
        """`""` means "inherit" on a profile row and means nothing on the global
        one, where `map_monarch_account` hands it to the ledger verbatim."""
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        for field in ("default_account", "recategorize_account"):
            cfg = self._config(sync=MonarchSyncSettings(**{field: ""}))
            with pytest.raises(ValueError, match=field):
                cs.save_monarch(db_path, cfg, replace_collections=True)
        assert cs.load_monarch(db_path).sync.default_account == "Assets:Bank:Checking"

    def test_an_empty_profile_account_still_inherits(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cs.upsert_monarch_profile(db_path, "acme", ledger="acme", default_account="")
        profile = [
            p for p in cs.load_monarch(db_path).profiles if p.name == "acme"
        ][0]
        assert profile.sync.default_account == "Assets:Bank:Checking"

    def test_a_bad_stored_row_does_not_block_a_sync_settings_edit(self, tmp_path):
        """config_store.py's stated rule: only the fields a caller passes are
        checked, so an existing non-conforming row never becomes uneditable."""
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO monarch_category_map(profile_id, monarch_category, "
                "beancount_account) VALUES (?, ?, ?)",
                (cs.GLOBAL_PROFILE_ID, "Fees", "Expenses:Fees (Bank)"),
            )
        cs.set_monarch_sync(db_path, lookback_days=30)
        assert cs.load_monarch(db_path).sync.lookback_days == 30

    def test_the_sync_writer_checks_what_it_is_given(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with pytest.raises(ValueError, match="default_account"):
            cs.set_monarch_sync(db_path, default_account="Assets:Bank (Checking)")
        with pytest.raises(ValueError, match="default_account"):
            cs.set_monarch_sync(db_path, default_account="")
        cs.set_monarch_sync(db_path, default_account="Assets:Bank:Savings")
        assert cs.load_monarch(db_path).sync.default_account == "Assets:Bank:Savings"

    def test_a_global_map_row_counts_as_stored_monarch_config(self, tmp_path):
        """`has_monarch_data` stays profile-only: it also answers sync-monarch's
        "is there anything to sync". This is the migration's question."""
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        assert cs.has_monarch_config_rows(db_path) is False
        cs.set_category_map_entry(
            db_path, None, "Internet Services", "Expenses:Internet-Services",
        )
        assert cs.has_monarch_config_rows(db_path) is True
        assert cs.has_monarch_data(db_path) is False

    def test_an_invalid_account_raises_the_dedicated_type(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with pytest.raises(cs.InvalidAccountError):
            cs.set_category_map_entry(db_path, None, "Fees", "Expenses:Fees (Bank)")
        assert issubclass(cs.InvalidAccountError, ValueError)


# =============================================================================
# Transaction rules (spec: transaction-rules, Stage 2)
# =============================================================================


def legacy_db(
    db_path,
    *,
    accounts=None,
    categories=None,
    tags=None,
    profiles=(),
    keep_seed=False,
):
    """A money.db in the pre-rules shape: old tables populated, no sentinel.

    `init_db` runs the migration, so the only way to build the shape it
    migrates *from* is to create the schema, take the sentinel back off and
    write the old tables directly. `profiles` is a list of dicts with `name`,
    `ledger` and optional `accounts` / `categories` / `tags`.
    """
    cs.init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM schema_meta WHERE key IN (?, ?)",
            (cs._RULES_MIGRATION_SENTINEL, cs._RULES_MIGRATION_NOTES),
        )
        if not keep_seed:
            conn.execute("DELETE FROM transaction_rules")

        def write(pid, acc, cat, tg):
            for name, account in (acc or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO monarch_account_map("
                    "profile_id, monarch_name, beancount_account) VALUES (?, ?, ?)",
                    (pid, name, account),
                )
            for category, account in (cat or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO monarch_category_map("
                    "profile_id, monarch_category, beancount_account) "
                    "VALUES (?, ?, ?)",
                    (pid, category, account),
                )
            for kind, values in (tg or {}).items():
                for tag in values:
                    conn.execute(
                        "INSERT OR IGNORE INTO monarch_tag_filters("
                        "profile_id, kind, tag) VALUES (?, ?, ?)",
                        (pid, kind, tag),
                    )

        write(cs.GLOBAL_PROFILE_ID, accounts, categories, tags)
        for profile in profiles:
            cur = conn.execute(
                "INSERT INTO monarch_profiles(name, ledger) VALUES (?, ?)",
                (profile["name"], profile["ledger"]),
            )
            write(
                cur.lastrowid,
                profile.get("accounts"),
                profile.get("categories"),
                profile.get("tags"),
            )
        conn.commit()
    return db_path


def rule_rows(db_path, **filters):
    """Every rule, as plain dicts, with the seeded tier dropped."""
    out = [r for r in cs.list_transaction_rules(db_path) if r["origin"] != "seed"]
    for key, value in filters.items():
        out = [r for r in out if r[key] == value]
    return out


def rule_tuples(db_path, **filters):
    return [
        (r["ledger"], r["source"], r["field"], r["match_kind"], r["match_value"],
         r["action"], r["target"], r["priority"], r["origin"])
        for r in rule_rows(db_path, **filters)
    ]


def as_profile_config(cfg, profile):
    """One profile's effective view, as the config object the lookup takes."""
    return MonarchConfig(
        credentials=cfg.credentials,
        sync=profile.sync,
        accounts=profile.accounts,
        categories=profile.categories,
        tags=profile.tags,
        profiles=[],
    )


VALID_RULE = {
    "ledger": "acme",
    "source": "monarch-api",
    "field": "category",
    "match_kind": "iexact",
    "match_value": "Software",
    "action": "posting_account",
    "target": "Expenses:Business:Software",
    "priority": 100,
    "enabled": True,
    "origin": "user",
    "note": "",
}


class TestTransactionRuleCrud:
    def test_create_read_update_delete(self, tmp_path):
        db_path = tmp_path / "money.db"
        created = cs.create_transaction_rule(db_path, **VALID_RULE)
        assert created["id"] > 0
        assert created["target"] == "Expenses:Business:Software"
        assert created["enabled"] is True
        assert created["created_at"] and created["updated_at"]

        assert cs.get_transaction_rule(db_path, created["id"]) == created

        updated = cs.update_transaction_rule(
            db_path, created["id"], target="Expenses:Software", priority=42,
        )
        assert updated["target"] == "Expenses:Software"
        assert updated["priority"] == 42
        # The merge is over the whole record: untouched fields survive.
        assert updated["match_value"] == "Software"
        assert updated["ledger"] == "acme"

        assert cs.delete_transaction_rule(db_path, created["id"]) is True
        assert cs.delete_transaction_rule(db_path, created["id"]) is False
        assert cs.get_transaction_rule(db_path, created["id"]) is None

    def test_an_unknown_id_reads_as_absent_rather_than_raising(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        assert cs.get_transaction_rule(db_path, 9999) is None
        assert cs.update_transaction_rule(db_path, 9999, priority=5) is None
        assert cs.delete_transaction_rule(db_path, 9999) is False

    def test_a_duplicate_is_refused_naming_the_existing_id(self, tmp_path):
        db_path = tmp_path / "money.db"
        first = cs.create_transaction_rule(db_path, **VALID_RULE)
        with pytest.raises(ValueError) as exc:
            cs.create_transaction_rule(
                db_path, **{**VALID_RULE, "target": "Expenses:Other"},
            )
        assert f"(id {first['id']})" in str(exc.value)
        # The message reaches an HTTP response and a Talk room; the match value
        # is the user's own financial data and stays out of it.
        assert "Software" not in str(exc.value)
        assert len(rule_rows(db_path)) == 1

    def test_a_rule_may_keep_its_own_identity_on_update(self, tmp_path):
        """The dedup check must not read the row being edited as its own clash."""
        db_path = tmp_path / "money.db"
        rule = cs.create_transaction_rule(db_path, **VALID_RULE)
        assert cs.update_transaction_rule(
            db_path, rule["id"], target="Expenses:Other",
        )["target"] == "Expenses:Other"

    def test_an_update_onto_another_rules_identity_is_refused(self, tmp_path):
        db_path = tmp_path / "money.db"
        first = cs.create_transaction_rule(db_path, **VALID_RULE)
        second = cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "match_value": "Consulting"},
        )
        with pytest.raises(ValueError, match=f"id {first['id']}"):
            cs.update_transaction_rule(db_path, second["id"], match_value="Software")

    def test_unknown_fields_are_refused_on_both_write_paths(self, tmp_path):
        db_path = tmp_path / "money.db"
        rule = cs.create_transaction_rule(db_path, **VALID_RULE)
        with pytest.raises(ValueError, match="unknown rule field"):
            cs.create_transaction_rule(db_path, **{**VALID_RULE, "colour": "red"})
        with pytest.raises(ValueError, match="unknown rule field"):
            cs.update_transaction_rule(db_path, rule["id"], colour="red")


class TestTransactionRuleValidation:
    @pytest.mark.parametrize("key,value", [
        ("field", "amount"),
        ("match_kind", "regex"),
        ("action", "rewrite_payee"),
        ("origin", "imported"),
    ])
    def test_every_enum_is_closed(self, tmp_path, key, value):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match=key):
            cs.create_transaction_rule(db_path, **{**VALID_RULE, key: value})

    def test_the_target_goes_through_the_account_check(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(cs.InvalidAccountError):
            cs.create_transaction_rule(
                db_path, **{**VALID_RULE, "target": "Expenses:Fees (Bank)"},
            )

    def test_a_skip_rule_takes_no_target(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="skip rule takes no target"):
            cs.create_transaction_rule(
                db_path, **{**VALID_RULE, "field": "tag", "action": "skip"},
            )
        skip = cs.create_transaction_rule(
            db_path,
            **{**VALID_RULE, "field": "tag", "action": "skip",
               "match_value": "Personal", "target": ""},
        )
        assert skip["action"] == "skip"

    @pytest.mark.parametrize("priority", [-1, 10_000])
    def test_priority_is_bounded(self, tmp_path, priority):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="priority"):
            cs.create_transaction_rule(db_path, **{**VALID_RULE, "priority": priority})

    def test_match_value_is_required_and_capped(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="match_value"):
            cs.create_transaction_rule(db_path, **{**VALID_RULE, "match_value": "   "})
        with pytest.raises(ValueError, match="match_value"):
            cs.create_transaction_rule(
                db_path,
                **{**VALID_RULE,
                   "match_value": "x" * (rule_engine.MAX_MATCH_VALUE_CHARS + 1)},
            )

    def test_enabled_is_never_coerced_from_a_string(self, tmp_path):
        """`bool("false")` is True, and a rule the user switched off staying
        live is the one direction this must not fail in."""
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="enabled"):
            cs.create_transaction_rule(db_path, **{**VALID_RULE, "enabled": "false"})
        assert rule_rows(db_path) == []

    def test_nothing_is_written_when_validation_fails(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError):
            cs.create_transaction_rule(db_path, **{**VALID_RULE, "field": "amount"})
        assert rule_rows(db_path) == []


class TestTransactionRuleListing:
    def test_listing_is_in_evaluation_order_and_scoped_exactly(self, tmp_path):
        db_path = tmp_path / "money.db"
        low = cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "match_value": "Late", "priority": 200},
        )
        high = cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "match_value": "Early", "priority": 50},
        )
        other = cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "ledger": "", "match_value": "Wide"},
        )
        assert [r["id"] for r in rule_rows(db_path)] == [
            high["id"], other["id"], low["id"],
        ]
        # Exact scope, not the engine's wildcard: an editor showing one
        # ledger's rules must not fold in every ''-scoped one as if it belonged.
        scoped = [
            r for r in cs.list_transaction_rules(db_path, ledger="acme")
            if r["origin"] != "seed"
        ]
        assert [r["id"] for r in scoped] == [high["id"], low["id"]]

    def test_include_disabled_is_the_editors_flag_and_defaults_on(self, tmp_path):
        db_path = tmp_path / "money.db"
        off = cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "enabled": False},
        )
        assert off["id"] in [r["id"] for r in cs.list_transaction_rules(db_path)]
        visible = cs.list_transaction_rules(db_path, include_disabled=False)
        assert off["id"] not in [r["id"] for r in visible]

    def test_load_rules_for_run_wildcards_the_scope_and_drops_disabled(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "ledger": "", "source": "",
                        "match_value": "Anywhere"},
        )
        cs.create_transaction_rule(db_path, **{**VALID_RULE, "match_value": "Here"})
        cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "ledger": "other", "match_value": "Elsewhere"},
        )
        cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "match_value": "Off", "enabled": False},
        )
        cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "source": "monarch-csv",
                        "match_value": "OtherSource"},
        )
        loaded = cs.load_rules_for_run(db_path, "acme", "monarch-api")
        assert [r.match_value for r in loaded if r.origin != "seed"] == [
            "Anywhere", "Here",
        ]
        assert all(isinstance(r, rule_engine.Rule) for r in loaded)
        assert all(r.enabled for r in loaded)

    def test_load_rules_for_run_returns_them_in_evaluation_order(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "match_value": "Second", "priority": 100},
        )
        cs.create_transaction_rule(
            db_path, **{**VALID_RULE, "match_value": "First", "priority": 10},
        )
        loaded = [
            r for r in cs.load_rules_for_run(db_path, "acme", "monarch-api")
            if r.origin != "seed"
        ]
        assert [r.match_value for r in loaded] == ["First", "Second"]
        assert loaded == sorted(loaded, key=rule_engine.sort_key)


class TestTransactionRuleSeed:
    def test_the_shipped_map_is_seeded_behind_everything(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        seeded = {
            r["match_value"]: r
            for r in cs.list_transaction_rules(db_path)
            if r["origin"] == "seed"
        }
        assert set(seeded) == set(MONARCH_CATEGORY_MAP)
        for category, account in MONARCH_CATEGORY_MAP.items():
            row = seeded[category]
            assert row["target"] == account
            assert row["priority"] == 900
            assert row["ledger"] == ""
            assert row["source"] == ""
            assert row["match_kind"] == "iexact"
            assert row["action"] == "posting_account"

    def test_a_deleted_seed_row_stays_deleted(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        victim = next(
            r for r in cs.list_transaction_rules(db_path) if r["origin"] == "seed"
        )
        assert cs.delete_transaction_rule(db_path, victim["id"]) is True
        cs.init_db(db_path)
        remaining = [
            r["match_value"] for r in cs.list_transaction_rules(db_path)
            if r["origin"] == "seed"
        ]
        assert victim["match_value"] not in remaining

    def test_the_seed_is_invisible_to_the_dict_views(self, tmp_path):
        """The shipped map was a module constant and was never in any of these
        dicts. `map_monarch_category` still carries it as the fallback tier, so
        surfacing it here would double it into every export."""
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        assert cs.get_category_map(db_path, None) == {}
        assert cs.load_monarch(db_path).categories == {}
        assert cs.has_monarch_config_rows(db_path) is False

    def test_a_migrated_rule_for_a_seeded_category_outranks_it(self, tmp_path):
        """The two tiers coexist rather than colliding: the migration writes
        `source='monarch-api'` and the seed writes `''`, so they are different
        rows under the unique index. Priority is what separates them, and it is
        the separation `map_monarch_category_with_config` already had."""
        db_path = tmp_path / "money.db"
        category = next(iter(MONARCH_CATEGORY_MAP))
        legacy_db(
            db_path, categories={category: "Expenses:Override"}, keep_seed=True,
        )
        cs.init_db(db_path)
        by_origin = {
            r["origin"]: r
            for r in cs.list_transaction_rules(db_path)
            if r["match_value"] == category
        }
        assert by_origin["migrated"]["priority"] < by_origin["seed"]["priority"]
        assert by_origin["migrated"]["target"] == "Expenses:Override"


class TestTransactionRuleMigration:
    def test_a_global_only_map_lands_at_the_wildcard_ledger(self, tmp_path):
        db_path = tmp_path / "money.db"
        legacy_db(
            db_path,
            accounts={"Chase": "Assets:Bank:Chase"},
            categories={"Software": "Expenses:Software"},
        )
        cs.init_db(db_path)
        assert set(rule_tuples(db_path)) == {
            ("", "monarch-api", "account", "iexact", "Chase",
             "contra_account", "Assets:Bank:Chase", 100, "migrated"),
            ("", "monarch-api", "category", "iexact", "Software",
             "posting_account", "Expenses:Software", 100, "migrated"),
        }

    def test_a_profile_with_its_own_maps_lands_at_its_ledger(self, tmp_path):
        db_path = tmp_path / "money.db"
        legacy_db(
            db_path,
            categories={"Software": "Expenses:Global"},
            profiles=[{
                "name": "acme", "ledger": "acme",
                "categories": {"Software": "Expenses:Acme"},
            }],
        )
        cs.init_db(db_path)
        tuples = rule_tuples(db_path)
        assert ("acme", "monarch-api", "category", "iexact", "Software",
                "posting_account", "Expenses:Acme", 100, "migrated") in tuples
        assert ("", "monarch-api", "category", "iexact", "Software",
                "posting_account", "Expenses:Global", 100, "migrated") in tuples

    def test_a_profile_inheriting_global_gets_its_own_copy(self, tmp_path):
        """Old inheritance is replacement, not layering — a profile with one
        own rule ignored the whole global map — so the effective map is written
        out per profile rather than left to be layered."""
        db_path = tmp_path / "money.db"
        legacy_db(
            db_path,
            accounts={"Chase": "Assets:Bank:Chase"},
            categories={"Software": "Expenses:Software"},
            profiles=[{"name": "acme", "ledger": "acme"}],
        )
        cs.init_db(db_path)
        acme = rule_tuples(db_path, ledger="acme")
        assert ("acme", "monarch-api", "category", "iexact", "Software",
                "posting_account", "Expenses:Software", 100, "migrated") in acme
        assert ("acme", "monarch-api", "account", "iexact", "Chase",
                "contra_account", "Assets:Bank:Chase", 100, "migrated") in acme

    def test_exclude_tags_become_skip_rules_ahead_of_the_mapping_tier(self, tmp_path):
        db_path = tmp_path / "money.db"
        legacy_db(
            db_path,
            tags={"exclude": ["Hide"], "include": ["Biz"]},
            profiles=[{
                "name": "acme", "ledger": "acme",
                "tags": {"exclude": ["Personal"]},
            }],
        )
        cs.init_db(db_path)
        skips = rule_tuples(db_path, field="tag")
        assert ("", "", "tag", "iexact", "Hide", "skip", "", 50, "migrated") in skips
        assert ("acme", "", "tag", "iexact", "Personal", "skip", "", 50,
                "migrated") in skips
        assert all(r["priority"] < 100 for r in rule_rows(db_path, field="tag"))

    def test_include_tags_are_not_migrated(self, tmp_path):
        """An include list is a gate over the whole set, so a rule expressing it
        would mean something different depending on whether its siblings
        existed."""
        db_path = tmp_path / "money.db"
        legacy_db(db_path, tags={"include": ["Biz"], "exclude": ["Hide"]})
        cs.init_db(db_path)
        assert [r["match_value"] for r in rule_rows(db_path, field="tag")] == ["Hide"]
        assert cs.get_tag_filters(db_path, None) == {
            "include": ["Biz"], "exclude": ["Hide"],
        }
        with sqlite3.connect(db_path) as conn:
            rows = [
                tuple(r) for r in conn.execute(
                    "SELECT kind, tag FROM monarch_tag_filters"
                )
            ]
        assert ("include", "Biz") in rows

    def test_a_case_colliding_group_emits_an_exact_tier_and_one_representative(
        self, tmp_path,
    ):
        db_path = tmp_path / "money.db"
        legacy_db(db_path, categories={
            "Software": "Expenses:Upper",
            "software": "Expenses:Lower",
            "Rent": "Expenses:Rent",
        })
        cs.init_db(db_path)
        assert {
            (r["match_kind"], r["match_value"], r["target"], r["priority"])
            for r in rule_rows(db_path, field="category")
        } == {
            ("exact", "Software", "Expenses:Upper", 90),
            ("exact", "software", "Expenses:Lower", 90),
            ("iexact", "Software", "Expenses:Upper", 100),
            ("iexact", "Rent", "Expenses:Rent", 100),
        }

    def test_the_representative_is_the_groups_first_key_in_map_order(self, tmp_path):
        """The old scan returns the first case-insensitive match in map order.
        `_legacy_category_map` reads `ORDER BY monarch_category`, so map order
        is that order, and taking the group's sort-order-*last* key would answer
        with the other account."""
        db_path = tmp_path / "money.db"
        # 'SOFTWARE' sorts before 'Software' under SQLite's BINARY collation,
        # so it is the group's first key however the rows went in.
        legacy_db(db_path, categories={
            "Software": "Expenses:Mixed",
            "SOFTWARE": "Expenses:Upper",
        })
        cs.init_db(db_path)
        representative = [
            r for r in rule_rows(db_path, field="category")
            if r["match_kind"] == "iexact"
        ]
        assert len(representative) == 1
        assert representative[0]["match_value"] == "SOFTWARE"
        assert representative[0]["target"] == "Expenses:Upper"

    def test_two_profiles_on_one_ledger_keep_the_first_and_record_the_clash(
        self, tmp_path,
    ):
        db_path = tmp_path / "money.db"
        legacy_db(db_path, profiles=[
            {"name": "alpha", "ledger": "shared",
             "categories": {"Software": "Expenses:Alpha"}},
            {"name": "beta", "ledger": "shared",
             "categories": {"Software": "Expenses:Beta"}},
        ])
        cs.init_db(db_path)
        rows = rule_rows(db_path, field="category")
        assert len(rows) == 1
        # Profiles are migrated in name order, so alpha's is the one written.
        assert rows[0]["target"] == "Expenses:Alpha"
        clash = [
            n for n in cs.get_transaction_rules_migration_notes(db_path)
            if n["reason"] == "duplicate"
        ]
        assert len(clash) == 1
        assert clash[0]["kept_rule_id"] == rows[0]["id"]
        assert clash[0]["kept_target"] == "Expenses:Alpha"
        assert clash[0]["dropped_target"] == "Expenses:Beta"

    def test_the_two_unrepresentable_map_keys_are_reported_not_dropped_silently(
        self, tmp_path,
    ):
        """Both are storable today, because `_check_map_account` validates the
        value and never the key. The empty-key case is a genuine behaviour
        change on a deployment that has one, which is why it is recorded."""
        db_path = tmp_path / "money.db"
        long_key = "L" * (rule_engine.MAX_SUBJECT_CHARS + 1)
        legacy_db(db_path, categories={
            "": "Expenses:Empty",
            long_key: "Expenses:Long",
            "Software": "Expenses:Software",
        })
        cs.init_db(db_path)
        assert [r["match_value"] for r in rule_rows(db_path)] == ["Software"]
        notes = cs.get_transaction_rules_migration_notes(db_path)
        assert {n["reason"] for n in notes} == {"empty-key", "over-long-key"}
        over_long = next(n for n in notes if n["reason"] == "over-long-key")
        assert over_long["key_length"] == len(long_key)
        assert over_long["limit"] == rule_engine.MAX_SUBJECT_CHARS

    def test_it_runs_once_and_a_re_init_changes_nothing(self, tmp_path):
        db_path = tmp_path / "money.db"
        legacy_db(
            db_path,
            categories={"Software": "Expenses:Software", "software": "Expenses:Lower"},
            tags={"exclude": ["Hide"]},
            profiles=[{"name": "acme", "ledger": "acme"}],
        )
        cs.init_db(db_path)
        first = cs.list_transaction_rules(db_path)
        for _ in range(2):
            cs.init_db(db_path)
        assert cs.list_transaction_rules(db_path) == first

    def test_a_failed_migration_leaves_nothing_behind_and_the_views_fall_back(
        self, tmp_path, monkeypatch,
    ):
        """The one thing that makes a failed migration safe. The sentinel stays
        unwritten, the savepoint takes the partial write back, and every view
        goes on reading — and writing — the old tables."""
        db_path = tmp_path / "money.db"
        legacy_db(db_path, categories={"Software": "Expenses:Software"})

        def boom(mapping):
            raise RuntimeError("migration exploded")

        monkeypatch.setattr(cs, "_emit_map_entries", boom)
        cs.init_db(db_path)
        assert cs.get_meta(db_path, cs._RULES_MIGRATION_SENTINEL) is None
        assert rule_rows(db_path) == []
        assert cs.get_category_map(db_path, None) == {"Software": "Expenses:Software"}

        # And writes keep landing where the reads are looking.
        assert cs.set_category_map_entry(
            db_path, None, "Rent", "Expenses:Rent",
        ) == "created"
        assert cs.get_category_map(db_path, None) == {
            "Rent": "Expenses:Rent", "Software": "Expenses:Software",
        }
        assert rule_rows(db_path) == []

        monkeypatch.undo()
        cs.init_db(db_path)
        assert cs.get_category_map(db_path, None) == {
            "Rent": "Expenses:Rent", "Software": "Expenses:Software",
        }


MIGRATION_SHAPES = {
    "empty": {},
    "global only": {
        "accounts": {"Chase": "Assets:Bank:Chase"},
        "categories": {"Software": "Expenses:Software"},
        "tags": {"include": ["Biz"], "exclude": ["Hide"]},
    },
    "profile with its own maps": {
        "categories": {"Software": "Expenses:Global"},
        "profiles": [{
            "name": "acme", "ledger": "acme",
            "accounts": {"Visa": "Liabilities:Visa"},
            "categories": {"Software": "Expenses:Acme"},
            "tags": {"exclude": ["Personal"]},
        }],
    },
    "profile inheriting global": {
        "accounts": {"Chase": "Assets:Bank:Chase"},
        "categories": {"Software": "Expenses:Software"},
        "profiles": [{"name": "acme", "ledger": "acme"}],
    },
    "case collisions": {
        "categories": {
            "Software": "Expenses:Upper",
            "software": "Expenses:Lower",
            "Rent": "Expenses:Rent",
        },
    },
    "two profiles, two ledgers": {
        "categories": {"Software": "Expenses:Global"},
        "profiles": [
            {"name": "acme", "ledger": "acme",
             "categories": {"Software": "Expenses:Acme"}},
            {"name": "personal", "ledger": "personal"},
        ],
    },
}


def load_monarch_shape(db_path, shape, *, migrate, monkeypatch):
    """`load_monarch` over one legacy shape, with or without the migration.

    Suppressing it is not a mock of the answer: it leaves the sentinel absent,
    which is the same fallback a failed migration lands on, so the "before"
    reading comes out of the old tables through the previous release's code.
    """
    legacy_db(db_path, **shape)
    if not migrate:
        monkeypatch.setattr(cs, "_migrate_transaction_rules", lambda conn: 0)
    loaded = cs.load_monarch(db_path)
    monkeypatch.undo()
    return loaded


class TestTransactionRuleMigrationIsInert:
    """The stage's whole claim: `load_monarch` answers what it answered before.

    Each shape is built twice from identical legacy state — once with the
    migration suppressed, so the views read the old tables exactly as the
    previous release did, and once with it applied.
    """

    @pytest.mark.parametrize("name", sorted(MIGRATION_SHAPES))
    def test_load_monarch_is_unchanged(self, tmp_path, monkeypatch, name):
        shape = MIGRATION_SHAPES[name]
        before = load_monarch_shape(
            tmp_path / "before.db", shape, migrate=False, monkeypatch=monkeypatch,
        )
        after = load_monarch_shape(
            tmp_path / "after.db", shape, migrate=True, monkeypatch=monkeypatch,
        )
        assert after == before

    @pytest.mark.parametrize("name", sorted(MIGRATION_SHAPES))
    def test_map_iteration_order_is_unchanged(self, tmp_path, monkeypatch, name):
        """Dict equality ignores order, and order is part of the answer here:
        `map_monarch_category_with_config` scans the map in iteration order, so
        that is what a case-colliding group resolves through."""
        shape = MIGRATION_SHAPES[name]
        before = load_monarch_shape(
            tmp_path / "before.db", shape, migrate=False, monkeypatch=monkeypatch,
        )
        after = load_monarch_shape(
            tmp_path / "after.db", shape, migrate=True, monkeypatch=monkeypatch,
        )
        assert list(after.categories.items()) == list(before.categories.items())
        assert list(after.accounts.items()) == list(before.accounts.items())
        for old, new in zip(before.profiles, after.profiles):
            assert list(new.categories.items()) == list(old.categories.items())
            assert list(new.accounts.items()) == list(old.accounts.items())

    @pytest.mark.parametrize("name", sorted(MIGRATION_SHAPES))
    @pytest.mark.parametrize("category", [
        "Software", "software", "SOFTWARE", "SoFtWaRe", "Rent", "rent",
        "Not In Any Map", "",
    ])
    def test_the_resolved_account_is_unchanged(
        self, tmp_path, monkeypatch, name, category,
    ):
        """The property the migration exists to preserve, asserted through the
        lookup itself rather than through the dict it reads."""
        shape = MIGRATION_SHAPES[name]
        before = load_monarch_shape(
            tmp_path / "before.db", shape, migrate=False, monkeypatch=monkeypatch,
        )
        after = load_monarch_shape(
            tmp_path / "after.db", shape, migrate=True, monkeypatch=monkeypatch,
        )
        pairs = [(before, after)] + [
            (as_profile_config(before, old), as_profile_config(after, new))
            for old, new in zip(before.profiles, after.profiles)
        ]
        for old_cfg, new_cfg in pairs:
            assert (
                map_monarch_category_with_config(category, new_cfg)
                == map_monarch_category_with_config(category, old_cfg)
            )


class TestTheMigratedRulesResolveAsTheOldLookupDid:
    """The migration's real claim, asserted through the engine rather than the
    dict view.

    The dict views cannot see this. A colliding group emits an `exact` rule for
    every member *and* one `iexact` rule for the representative, and flattening
    to `{match_value: target}` makes the representative redundant — so the views
    answer identically whichever member is chosen, while the engine answers
    differently for a casing no member spells. Only a real `resolve` pass over
    the rules the migration actually wrote can tell them apart.

    Scoped to the global rule set on purpose. A profile ledger also pulls in
    every `ledger=''` rule, which is a precedence question `load_rules_for_run`
    raises and nothing answers until Stage 3 puts the engine on the import path.
    """

    CASES = [
        "Software", "software", "SOFTWARE", "SoFtWaRe",
        "Rent", "rent", "RENT",
        "Groceries", "groceries", "Not In Any Map", "", "  ",
    ]

    @staticmethod
    def _engine_account(db_path, category):
        loaded = cs.load_rules_for_run(db_path, "", "monarch-api")
        scoped = rule_engine.rules_in_scope(
            rule_engine.compile_rules(loaded), "", "monarch-api",
        )
        resolution = rule_engine.resolve(
            NormalizedTransaction(
                date=date(2026, 1, 1), payee="Acme", amount=Decimal("1"),
                category=category,
            ),
            scoped,
        )
        if resolution.posting_account is not None:
            return resolution.posting_account
        return f"Expenses:Uncategorized:{account_component(category)}"

    @pytest.mark.parametrize("name", sorted(MIGRATION_SHAPES))
    @pytest.mark.parametrize("category", CASES)
    def test_the_engine_answers_what_the_old_lookup_answered(
        self, tmp_path, monkeypatch, name, category,
    ):
        shape = MIGRATION_SHAPES[name]
        legacy = load_monarch_shape(
            tmp_path / "legacy.db", shape, migrate=False, monkeypatch=monkeypatch,
        )
        db_path = legacy_db(tmp_path / "migrated.db", keep_seed=True, **shape)
        cs.init_db(db_path)
        assert self._engine_account(db_path, category) == (
            map_monarch_category_with_config(category, legacy)
        )

    def test_an_unseen_casing_resolves_through_the_groups_representative(
        self, tmp_path, monkeypatch,
    ):
        """The case the whole `exact` tier exists for, spelled out.

        `SOFTWARE` matches neither stored key exactly, so the old scan falls to
        the first case-insensitive match in map order — and the migration has to
        emit that key as the group's `iexact` rule or the engine answers with
        the other account.
        """
        shape = {"categories": {
            "Software": "Expenses:Upper", "software": "Expenses:Lower",
        }}
        legacy = load_monarch_shape(
            tmp_path / "legacy.db", shape, migrate=False, monkeypatch=monkeypatch,
        )
        assert map_monarch_category_with_config("SOFTWARE", legacy) == "Expenses:Upper"
        db_path = legacy_db(tmp_path / "migrated.db", keep_seed=True, **shape)
        cs.init_db(db_path)
        assert self._engine_account(db_path, "SOFTWARE") == "Expenses:Upper"
        assert self._engine_account(db_path, "software") == "Expenses:Lower"


class TestCompatibilityViews:
    def test_the_category_map_round_trips_through_the_rules(self, tmp_path):
        db_path = tmp_path / "money.db"
        mapping = {"Software": "Expenses:Software", "Rent": "Expenses:Rent"}
        cs.replace_category_map(db_path, None, mapping)
        assert cs.get_category_map(db_path, None) == mapping
        assert {r["match_value"] for r in rule_rows(db_path, field="category")} == {
            "Software", "Rent",
        }
        cs.replace_category_map(db_path, None, {"Rent": "Expenses:Rent"})
        assert cs.get_category_map(db_path, None) == {"Rent": "Expenses:Rent"}

    def test_the_account_map_round_trips_through_the_rules(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_monarch_profile(db_path, "acme", ledger="acme")
        cs.replace_account_map(db_path, "acme", {"Visa": "Liabilities:Visa"})
        assert cs.get_account_map(db_path, "acme") == {"Visa": "Liabilities:Visa"}
        assert rule_tuples(db_path, field="account") == [
            ("acme", "monarch-api", "account", "iexact", "Visa",
             "contra_account", "Liabilities:Visa", 100, "user"),
        ]

    def test_a_contains_rule_survives_a_full_dict_put(self, tmp_path):
        """The precise contract: a rule the dict view cannot represent is
        omitted from GET and preserved by PUT."""
        db_path = tmp_path / "money.db"
        cs.set_category_map_entry(db_path, None, "Software", "Expenses:Software")
        survivor = cs.create_transaction_rule(
            db_path,
            **{**VALID_RULE, "ledger": "", "match_kind": "contains",
               "match_value": "coffee", "target": "Expenses:Coffee"},
        )
        assert cs.get_category_map(db_path, None) == {"Software": "Expenses:Software"}
        cs.replace_category_map(db_path, None, {"Rent": "Expenses:Rent"})
        assert cs.get_category_map(db_path, None) == {"Rent": "Expenses:Rent"}
        assert cs.get_transaction_rule(db_path, survivor["id"]) is not None

    def test_a_disabled_rule_is_omitted_and_preserved_the_same_way(self, tmp_path):
        db_path = tmp_path / "money.db"
        off = cs.create_transaction_rule(
            db_path,
            **{**VALID_RULE, "ledger": "", "match_value": "Dormant",
               "enabled": False},
        )
        assert cs.get_category_map(db_path, None) == {}
        cs.replace_category_map(db_path, None, {"Rent": "Expenses:Rent"})
        assert cs.get_transaction_rule(db_path, off["id"])["enabled"] is False

    def test_writing_a_key_a_disabled_rule_holds_switches_it_back_on(self, tmp_path):
        """The unique index covers a disabled row too, so the alternative is a
        second row it would refuse."""
        db_path = tmp_path / "money.db"
        off = cs.create_transaction_rule(
            db_path,
            **{**VALID_RULE, "ledger": "", "match_value": "Dormant",
               "enabled": False},
        )
        assert cs.set_category_map_entry(
            db_path, None, "Dormant", "Expenses:Awake",
        ) == "created"
        again = cs.get_transaction_rule(db_path, off["id"])
        assert again["enabled"] is True
        assert again["target"] == "Expenses:Awake"

    def test_setting_a_key_that_collides_grows_the_exact_tier(self, tmp_path):
        """The collision encoding is a property of the map, not of a key, so a
        per-key write has to re-derive the scope's whole emission."""
        db_path = tmp_path / "money.db"
        cs.set_category_map_entry(db_path, None, "Software", "Expenses:Upper")
        assert cs.set_category_map_entry(
            db_path, None, "software", "Expenses:Lower",
        ) == "created"
        assert {
            (r["match_kind"], r["match_value"], r["target"])
            for r in rule_rows(db_path, field="category")
        } == {
            ("exact", "Software", "Expenses:Upper"),
            ("exact", "software", "Expenses:Lower"),
            ("iexact", "Software", "Expenses:Upper"),
        }
        assert cs.get_category_map(db_path, None) == {
            "Software": "Expenses:Upper", "software": "Expenses:Lower",
        }

    def test_unsetting_a_groups_representative_promotes_the_next_member(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.replace_category_map(db_path, None, {
            "Software": "Expenses:Upper", "software": "Expenses:Lower",
        })
        assert cs.unset_category_map_entry(db_path, None, "Software") is True
        assert rule_tuples(db_path, field="category") == [
            ("", "monarch-api", "category", "iexact", "software",
             "posting_account", "Expenses:Lower", 100, "user"),
        ]
        assert cs.get_category_map(db_path, None) == {"software": "Expenses:Lower"}

    def test_an_untouched_rule_keeps_its_id_priority_and_note(self, tmp_path):
        db_path = tmp_path / "money.db"
        kept = cs.create_transaction_rule(
            db_path,
            **{**VALID_RULE, "ledger": "", "match_value": "Rent",
               "target": "Expenses:Rent", "priority": 7, "note": "hand-tuned"},
        )
        cs.set_category_map_entry(db_path, None, "Software", "Expenses:Software")
        assert cs.get_transaction_rule(db_path, kept["id"]) == kept

    def test_the_set_entry_states_are_unchanged(self, tmp_path):
        db_path = tmp_path / "money.db"
        assert cs.set_category_map_entry(
            db_path, None, "Software", "Expenses:A",
        ) == "created"
        assert cs.set_category_map_entry(
            db_path, None, "Software", "Expenses:A",
        ) == "noop"
        assert cs.set_category_map_entry(
            db_path, None, "Software", "Expenses:B",
        ) == "updated"
        assert cs.unset_category_map_entry(db_path, None, "Software") is True
        assert cs.unset_category_map_entry(db_path, None, "Software") is False

    def test_tag_filters_read_include_from_the_old_table_and_exclude_from_rules(
        self, tmp_path,
    ):
        db_path = tmp_path / "money.db"
        cs.upsert_monarch_profile(db_path, "acme", ledger="acme")
        assert cs.add_tag_filter(db_path, "acme", "include", "Biz") == "created"
        assert cs.add_tag_filter(db_path, "acme", "exclude", "Hide") == "created"
        assert cs.add_tag_filter(db_path, "acme", "exclude", "Hide") == "noop"
        assert cs.get_tag_filters(db_path, "acme") == {
            "include": ["Biz"], "exclude": ["Hide"],
        }
        assert rule_tuples(db_path, field="tag") == [
            ("acme", "", "tag", "iexact", "Hide", "skip", "", 50, "user"),
        ]
        with sqlite3.connect(db_path) as conn:
            kinds = {
                r[0] for r in conn.execute("SELECT kind FROM monarch_tag_filters")
            }
        assert kinds == {"include"}
        assert cs.remove_tag_filter(db_path, "acme", "exclude", "Hide") is True
        assert cs.remove_tag_filter(db_path, "acme", "exclude", "Hide") is False
        assert cs.get_tag_filters(db_path, "acme") == {"include": ["Biz"], "exclude": []}

    def test_replace_tag_filters_rewrites_both_halves(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.replace_tag_filters(db_path, None, ["Biz"], ["Hide"])
        assert cs.get_tag_filters(db_path, None) == {
            "include": ["Biz"], "exclude": ["Hide"],
        }
        cs.replace_tag_filters(db_path, None, [], ["Personal"])
        assert cs.get_tag_filters(db_path, None) == {
            "include": [], "exclude": ["Personal"],
        }

    def test_a_wholesale_save_clears_a_dropped_profiles_scope(self, tmp_path):
        """Rules carry a ledger, not a profile id, so the FK cascade that used
        to take a dropped profile's maps cannot reach them."""
        db_path = tmp_path / "money.db"
        cfg = cs.monarch_config_from_toml_dict(tomli.loads(MONARCH_TOML))
        cs.save_monarch(db_path, cfg)
        assert rule_rows(db_path, ledger="acme")
        cs.save_monarch(db_path, MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={}, categories={}, tags=MonarchTagFilters(), profiles=[],
        ), replace_collections=True)
        assert rule_rows(db_path) == []
        assert cs.load_monarch(db_path).profiles == []

    def test_deleting_a_profile_clears_the_maps_only_it_expressed(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_monarch_profile(db_path, "acme", ledger="acme")
        cs.set_category_map_entry(db_path, "acme", "Software", "Expenses:Acme")
        assert cs.delete_monarch_profile(db_path, "acme") is True
        assert rule_rows(db_path, ledger="acme") == []

    def test_deleting_one_of_two_profiles_on_a_ledger_keeps_the_shared_scope(
        self, tmp_path,
    ):
        db_path = tmp_path / "money.db"
        cs.upsert_monarch_profile(db_path, "alpha", ledger="shared")
        cs.upsert_monarch_profile(db_path, "beta", ledger="shared")
        cs.set_category_map_entry(db_path, "alpha", "Software", "Expenses:Shared")
        assert cs.delete_monarch_profile(db_path, "alpha") is True
        assert cs.get_category_map(db_path, "beta") == {"Software": "Expenses:Shared"}


class TestMapKeysAreCheckedOnWrite:
    """Both shapes are storable in the old tables today, because
    `_check_map_account` validates the value and never the key. A rule cannot
    carry either, so a write is refused rather than silently dropped."""

    @pytest.mark.parametrize("key", ["", "   "])
    def test_an_empty_key_is_refused(self, tmp_path, key):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="non-empty"):
            cs.set_category_map_entry(db_path, None, key, "Expenses:X")
        with pytest.raises(ValueError, match="non-empty"):
            cs.replace_account_map(db_path, None, {key: "Assets:Bank"})
        assert rule_rows(db_path) == []

    def test_an_over_long_key_is_refused(self, tmp_path):
        db_path = tmp_path / "money.db"
        key = "L" * (rule_engine.MAX_SUBJECT_CHARS + 1)
        with pytest.raises(ValueError, match="longer than"):
            cs.set_category_map_entry(db_path, None, key, "Expenses:X")
        assert rule_rows(db_path) == []
