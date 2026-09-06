"""Tests for money.core.transactions module."""

import re
from datetime import date
from pathlib import Path
from unittest.mock import patch

from istota.money import config_store
from istota.money.core import rules as rule_engine
from istota.money.core.importers import import_transactions
from istota.money.core.models import (
    MonarchConfig,
    MonarchCredentials,
    MonarchProfile,
    MonarchSyncSettings,
    MonarchTagFilters,
)
from istota.money.core.transactions import (
    MONARCH_CATEGORY_MAP,
    _ledger_has_posting,
    lookup_mapping,
    uncategorized_account,
    _normalize_monarch_txn,
    annotate_rule_drops,
    filter_by_tags,
    load_import_rules,
    load_import_rules_for_ledgers,
    format_beancount_transaction,
    format_category_change_entry,
    format_recategorization_entry,
    import_csv,
    map_monarch_category,
    map_monarch_category_with_config,
    map_monarch_account,
    parse_monarch_config,
    parse_monarch_csv,
    parse_tags,
    sync_all_profiles,
    sync_monarch,
    add_transaction,
    backup_ledger,
    append_to_ledger,
)
from istota.money.db import MonarchSyncedTransaction


class TestLookupMapping:
    """The one exact-then-case-insensitive lookup the four call sites share.

    ISSUE-426: this body was written four times, once per call site, and the
    two copies over `MONARCH_CATEGORY_MAP` were byte-identical.
    """

    def test_exact_match_wins(self):
        assert lookup_mapping("Groceries", {"Groceries": "A"}, "F") == "A"

    def test_case_insensitive_match(self):
        assert lookup_mapping("groceries", {"Groceries": "A"}, "F") == "A"
        assert lookup_mapping("GROCERIES", {"Groceries": "A"}, "F") == "A"

    def test_exact_match_beats_an_earlier_case_variant(self):
        """The exact key wins even where a case variant is listed first."""
        mapping = {"groceries": "lower", "Groceries": "exact"}
        assert lookup_mapping("Groceries", mapping, "F") == "exact"

    def test_first_insertion_order_wins_among_case_variants(self):
        """Dict order decides a case-insensitive tie, as it always did."""
        mapping = {"GROCERIES": "first", "groceries": "second"}
        assert lookup_mapping("Groceries", mapping, "F") == "first"

    def test_fallback_value(self):
        assert lookup_mapping("Nothing", {}, "Assets:Bank:Default") == "Assets:Bank:Default"

    def test_fallback_callable_gets_the_key_with_its_original_case(self):
        seen = []

        def fallback(key):
            seen.append(key)
            return "computed:" + key

        assert lookup_mapping("Fees & Charges", {}, fallback) == "computed:Fees & Charges"
        assert seen == ["Fees & Charges"]

    def test_fallback_is_not_consulted_on_a_hit(self):
        def fallback(key):  # pragma: no cover - must not run
            raise AssertionError("fallback called on a hit")

        assert lookup_mapping("groceries", {"Groceries": "A"}, fallback) == "A"

    def test_a_mapped_empty_string_is_returned_rather_than_the_fallback(self):
        """An empty *value* is a hit. Only a miss reaches the fallback."""
        assert lookup_mapping("Groceries", {"Groceries": ""}, "F") == ""

    def test_an_empty_mapping_never_folds_the_key(self):
        """A non-string key against an empty mapping reaches the fallback.

        The four bodies this replaced computed `key.lower()` in the loop, so an
        empty mapping never touched it. Hoisting it above the loop turned a
        `None` account name — which `sync_monarch` could produce from a JSON
        `"displayName": null` — into an `AttributeError` that aborted the run.
        """
        assert lookup_mapping(None, {}, "Assets:Bank:Default") == "Assets:Bank:Default"


class TestUncategorizedAccount:
    def test_slugs_the_category(self):
        assert uncategorized_account("Fees & Charges") == "Expenses:Uncategorized:FeesCharges"

    def test_is_what_the_category_mappers_fall_back_to(self):
        assert map_monarch_category("No Such Category") == uncategorized_account(
            "No Such Category",
        )


class TestCategoryMapping:
    def test_exact_match(self):
        assert map_monarch_category("Groceries") == "Expenses:Food:Groceries"
        assert map_monarch_category("Income") == "Income:Salary"

    def test_case_insensitive_match(self):
        assert map_monarch_category("groceries") == "Expenses:Food:Groceries"
        assert map_monarch_category("GROCERIES") == "Expenses:Food:Groceries"

    def test_unknown_category(self):
        result = map_monarch_category("Unknown Category")
        assert result == "Expenses:Uncategorized:UnknownCategory"

    def test_unknown_category_with_punctuation(self):
        assert (
            map_monarch_category("Internet Services (Reimbursed)")
            == "Expenses:Uncategorized:InternetServicesReimbursed"
        )
        assert map_monarch_category("Fees & Charges") == "Expenses:Uncategorized:FeesCharges"
        assert map_monarch_category("Utilities - Water") == "Expenses:Uncategorized:Utilities-Water"
        assert map_monarch_category("401k match") == "Expenses:Uncategorized:401kmatch"
        assert map_monarch_category("~~~") == "Expenses:Uncategorized:Unknown"

    def test_unknown_category_accounts_parse(self, tmp_path):
        from beancount import loader

        for category in ("Internet Services (Reimbursed)", "Fees & Charges", "e-bike / repair"):
            ledger = tmp_path / "t.beancount"
            ledger.write_text(
                'plugin "beancount.plugins.auto_accounts"\n'
                "2026-08-30 * \"Joker.com\" \"note\"\n"
                f"  {map_monarch_category(category)}  574.00 USD\n"
                "  Liabilities:Visa-Fidelity\n"
            )
            _, errors, _ = loader.load_file(str(ledger))
            assert errors == [], f"{category}: {errors}"

    def test_csv_importer_map_slugs_unknown_category(self):
        """The importer's category path ends on the same slug fallback.

        It used to be a private `_map_category` in `importers/__init__` — the
        same body over the same map as `map_monarch_category`, in a file that
        already imported that function (ISSUE-426).
        """
        assert (
            lookup_mapping(
                "Internet Services (Reimbursed)", {}, uncategorized_account,
            )
            == "Expenses:Uncategorized:InternetServicesReimbursed"
        )

    def test_all_mapped_categories_valid(self):
        for category, account in MONARCH_CATEGORY_MAP.items():
            assert ":" in account
            assert account.startswith(("Income:", "Expenses:", "Assets:", "Liabilities:", "Equity:"))

    def test_with_config_overrides(self):
        config = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={},
            categories={"Groceries": "Expenses:Business:Food"},
            tags=MonarchTagFilters(),
        )
        assert map_monarch_category_with_config("Groceries", config) == "Expenses:Business:Food"

    def test_config_fallback_to_builtin(self):
        config = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={},
            categories={},
            tags=MonarchTagFilters(),
        )
        assert map_monarch_category_with_config("Groceries", config) == "Expenses:Food:Groceries"


class TestAccountMapping:
    def test_exact_match(self):
        config = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={"Chase Checking": "Assets:Bank:Chase"},
            categories={},
            tags=MonarchTagFilters(),
        )
        assert map_monarch_account("Chase Checking", config) == "Assets:Bank:Chase"

    def test_fallback_to_default(self):
        config = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Default"),
            accounts={},
            categories={},
            tags=MonarchTagFilters(),
        )
        assert map_monarch_account("Unknown Account", config) == "Assets:Bank:Default"


class TestTagParsing:
    def test_parse_tags(self):
        assert parse_tags("Business, Travel") == ["Business", "Travel"]
        assert parse_tags("") == []
        assert parse_tags("  ") == []
        assert parse_tags("Single") == ["Single"]

    def test_filter_by_tags_include(self):
        assert filter_by_tags(["Business"], ["Business"], None) is True
        assert filter_by_tags(["Personal"], ["Business"], None) is False

    def test_filter_by_tags_exclude(self):
        assert filter_by_tags(["Personal"], None, ["Personal"]) is False
        assert filter_by_tags(["Business"], None, ["Personal"]) is True

    def test_filter_by_tags_both(self):
        assert filter_by_tags(["Business", "Tax"], ["Business"], ["Tax"]) is False
        assert filter_by_tags(["Business"], ["Business"], ["Tax"]) is True

    def test_filter_no_filters(self):
        assert filter_by_tags(["anything"], None, None) is True


class TestCSVParsing:
    def test_parse_monarch_csv(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Whole Foods,Groceries,Chase Checking,WHOLE FOODS #123,Weekly groceries,-85.50,Personal,Alice\n"
            "2026-01-16,Employer,Income,Chase Checking,PAYROLL DEPOSIT,Paycheck,5000.00,Business,Alice\n"
            "01/17/2026,Amazon,Shopping,Chase Checking,AMAZON.COM,,-42.99,,Alice\n"
        )
        transactions = parse_monarch_csv(csv_file)
        assert len(transactions) == 3
        assert transactions[0]["date"] == date(2026, 1, 15)
        assert transactions[0]["merchant"] == "Whole Foods"
        assert transactions[0]["amount"] == -85.50
        assert transactions[0]["tags"] == ["Personal"]
        assert transactions[2]["date"] == date(2026, 1, 17)
        assert transactions[2]["tags"] == []

    def test_skips_invalid_dates(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "invalid-date,Store,Shopping,Account,STORE,-10.00,,,\n"
            "2026-01-15,Valid Store,Shopping,Account,VALID STORE,,-20.00,,Alice\n"
        )
        transactions = parse_monarch_csv(csv_file)
        assert len(transactions) == 1

    def test_include_filter(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Store A,Shopping,Account,STORE A,,-10.00,Business,Alice\n"
            "2026-01-16,Store B,Shopping,Account,STORE B,,-20.00,Personal,Alice\n"
        )
        transactions = parse_monarch_csv(csv_file, include_tags=["Business"])
        assert len(transactions) == 1
        assert transactions[0]["merchant"] == "Store A"

    def test_exclude_filter(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Store A,Shopping,Account,STORE A,,-10.00,Business,Alice\n"
            "2026-01-16,Store B,Shopping,Account,STORE B,,-20.00,Personal,Alice\n"
        )
        transactions = parse_monarch_csv(csv_file, exclude_tags=["Personal"])
        assert len(transactions) == 1
        assert transactions[0]["merchant"] == "Store A"


class TestBeancountFormatting:
    def test_expense_transaction(self):
        result = format_beancount_transaction(
            txn_date=date(2026, 1, 15), payee="Whole Foods",
            narration="Weekly groceries", posting_account="Expenses:Food:Groceries",
            contra_account="Assets:Bank:Checking", amount=-85.50,
        )
        assert '2026-01-15 * "Whole Foods" "Weekly groceries"' in result
        assert "Expenses:Food:Groceries  85.50 USD" in result
        assert "Assets:Bank:Checking" in result

    def test_income_transaction(self):
        result = format_beancount_transaction(
            txn_date=date(2026, 1, 16), payee="Employer",
            narration="Paycheck", posting_account="Income:Salary",
            contra_account="Assets:Bank:Checking", amount=5000.00,
        )
        assert '2026-01-16 * "Employer" "Paycheck"' in result
        assert "Assets:Bank:Checking  5000.00 USD" in result

    def test_escapes_quotes(self):
        result = format_beancount_transaction(
            txn_date=date(2026, 1, 15), payee='Store "Best"',
            narration='Item "Special"', posting_account="Expenses:Shopping",
            contra_account="Assets:Bank:Checking", amount=-10.00,
        )
        assert '\\"Best\\"' in result
        assert '\\"Special\\"' in result

    def test_recategorization_entry_expense(self):
        """Expense recat: category swap from original bucket to recategorize_account."""
        result = format_recategorization_entry(
            txn_date=date(2026, 2, 7), merchant="Starbucks",
            posted_account="Expenses:Food:Coffee",
            contra_account="Liabilities:CreditCard",
            amount=-5.50,
            recategorize_account="Expenses:Personal-Expense",
        )
        assert "Recategorized: business tag removed" in result
        assert "Expenses:Personal-Expense  5.50 USD" in result
        assert "Expenses:Food:Coffee  -5.50 USD" in result
        # Cash leg untouched — only the expense bucket changed.
        assert "Liabilities:CreditCard" not in result

    def test_recategorization_entry_income_reverses_original(self):
        """Income recat: full reversal of the original entry.

        Original eBay income (amount=+40.89):
            DR Equity:Owner-Drawings +40.89 / CR Income:Sales -40.89
        Reversal must be the exact mirror.
        """
        result = format_recategorization_entry(
            txn_date=date(2026, 4, 25), merchant="eBay",
            posted_account="Income:Sales",
            contra_account="Equity:Owner-InvestmentDrawings",
            amount=40.89,
        )
        assert result is not None
        assert "Reversal: business tag removed" in result
        assert "Income:Sales  40.89 USD" in result
        # contra side gets the implicit balancing -40.89 (no explicit amount line)
        assert "Equity:Owner-InvestmentDrawings" in result
        # Personal-Expense must NOT appear — that was the bug.
        assert "Personal-Expense" not in result

    def test_recategorization_entry_income_negative_amount(self):
        """Income reversal still works when Monarch stored a negative-signed amount."""
        result = format_recategorization_entry(
            txn_date=date(2026, 4, 25), merchant="eBay",
            posted_account="Income:Sales",
            contra_account="Assets:Bank:Checking",
            amount=-40.89,
        )
        assert result is not None
        assert "Reversal:" in result
        # Reversal must still balance regardless of original sign.
        assert "Income:Sales" in result
        assert "Assets:Bank:Checking" in result

    def test_recategorization_entry_income_missing_contra_returns_none(self):
        """Legacy rows synced before contra_account was tracked can't be reversed."""
        result = format_recategorization_entry(
            txn_date=date(2026, 4, 25), merchant="eBay",
            posted_account="Income:Sales",
            contra_account=None,
            amount=40.89,
        )
        assert result is None

    def test_recategorization_entry_expense_works_without_contra(self):
        """Expense recat doesn't need contra_account — it only swaps the expense bucket."""
        result = format_recategorization_entry(
            txn_date=date(2026, 2, 7), merchant="Starbucks",
            posted_account="Expenses:Food:Coffee",
            contra_account=None,
            amount=-5.50,
            recategorize_account="Expenses:Personal-Expense",
        )
        assert result is not None
        assert "Expenses:Personal-Expense  5.50 USD" in result
        assert "Expenses:Food:Coffee  -5.50 USD" in result

    def test_category_change_entry_expense(self):
        """Expense → expense category change: DR new / CR old."""
        result = format_category_change_entry(
            txn_date=date(2026, 2, 14), merchant="PayPal",
            old_account="Expenses:Office-Supplies",
            new_account="Expenses:Entertainment:Recreation", amount=25.00,
        )
        assert "Recategorized in Monarch" in result
        assert "Expenses:Entertainment:Recreation  25.00 USD" in result
        assert "Expenses:Office-Supplies  -25.00 USD" in result

    def test_category_change_entry_income(self):
        """Income → income category change: signs flip — DR old / CR new — to
        cancel the original credit and re-credit the new income account."""
        result = format_category_change_entry(
            txn_date=date(2026, 4, 25), merchant="Client Co",
            old_account="Income:Sales",
            new_account="Income:Consulting", amount=500.00,
        )
        assert "Recategorized in Monarch" in result
        assert "Income:Sales  500.00 USD" in result
        assert "Income:Consulting  -500.00 USD" in result


class TestConfigParsing:
    def test_parse_monarch_config(self, tmp_path):
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nsession_id = "inline-sid"\ncsrftoken = "inline-csrf"\n\n'
            '[monarch.sync]\nlookback_days = 60\n\n'
            '[monarch.accounts]\n"Chase" = "Assets:Bank:Chase"\n\n'
            '[monarch.categories]\n"Custom" = "Expenses:Custom"\n\n'
            '[monarch.tags]\ninclude = ["Business"]\n'
        )
        config = parse_monarch_config(config_file)
        assert config.credentials.session_id == "inline-sid"
        assert config.credentials.csrftoken == "inline-csrf"
        assert config.sync.lookback_days == 60
        assert config.accounts["Chase"] == "Assets:Bank:Chase"
        assert config.categories["Custom"] == "Expenses:Custom"
        assert config.tags.include == ["Business"]

    def test_parse_monarch_config_with_secrets_overlay(self, tmp_path):
        """Secrets dict overlays cookie pair onto monarch config credentials."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\n\n[monarch.sync]\nlookback_days = 60\n'
        )
        secrets = {"monarch": {"session_id": "SID-secret", "csrftoken": "CSRF-secret"}}
        config = parse_monarch_config(config_file, secrets=secrets)
        assert config.credentials.session_id == "SID-secret"
        assert config.credentials.csrftoken == "CSRF-secret"

    def test_parse_monarch_config_secrets_override_config_credentials(self, tmp_path):
        """Secrets dict takes precedence over config file for the same field."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nsession_id = "old-sid"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        secrets = {"monarch": {"session_id": "new-sid"}}
        config = parse_monarch_config(config_file, secrets=secrets)
        assert config.credentials.session_id == "new-sid"

    def test_parse_monarch_config_no_secrets(self, tmp_path):
        """Without secrets, behavior is unchanged."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nsession_id = "inline-sid"\ncsrftoken = "inline-csrf"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        config = parse_monarch_config(config_file, secrets=None)
        assert config.credentials.session_id == "inline-sid"
        assert config.credentials.csrftoken == "inline-csrf"

    def test_parse_monarch_config_empty_secrets(self, tmp_path):
        """Empty secrets dict doesn't break anything."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text('[monarch]\n')
        config = parse_monarch_config(config_file, secrets={})
        assert config.credentials.session_id is None
        assert config.credentials.csrftoken is None


class TestProfileConfigParsing:
    def test_parse_profiles(self, tmp_path):
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
            '[monarch.sync]\nlookback_days = 60\ndefault_account = "Expenses:Default"\n\n'
            '[monarch.accounts]\n"Chase" = "Assets:Bank:Chase"\n\n'
            '[monarch.profiles.business]\n'
            'ledger = "business"\n'
            'default_account = "Expenses:Business:Uncategorized"\n'
            'recategorize_account = "Expenses:Business:Personal"\n\n'
            '[monarch.profiles.business.tags]\n'
            'include = ["business"]\n\n'
            '[monarch.profiles.business.accounts]\n'
            '"Chase" = "Assets:Business:Chase"\n\n'
            '[monarch.profiles.business.categories]\n'
            '"Food & Drink" = "Expenses:Business:Meals"\n\n'
            '[monarch.profiles.personal]\n'
            'ledger = "personal"\n'
            'default_account = "Expenses:Personal:Uncategorized"\n\n'
            '[monarch.profiles.personal.tags]\n'
            'exclude = ["business"]\n'
        )
        config = parse_monarch_config(config_file)
        assert len(config.profiles) == 2

        biz = next(p for p in config.profiles if p.name == "business")
        assert biz.ledger == "business"
        assert biz.sync.default_account == "Expenses:Business:Uncategorized"
        assert biz.sync.recategorize_account == "Expenses:Business:Personal"
        assert biz.tags.include == ["business"]
        assert biz.accounts["Chase"] == "Assets:Business:Chase"
        assert biz.categories["Food & Drink"] == "Expenses:Business:Meals"
        # Inherits lookback_days from top level
        assert biz.sync.lookback_days == 60

        personal = next(p for p in config.profiles if p.name == "personal")
        assert personal.ledger == "personal"
        assert personal.sync.default_account == "Expenses:Personal:Uncategorized"
        assert personal.tags.exclude == ["business"]
        # No profile-level accounts, inherits top-level
        assert personal.accounts == {"Chase": "Assets:Bank:Chase"}

    def test_parse_no_profiles_backward_compat(self, tmp_path):
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nemail = "test@example.com"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        config = parse_monarch_config(config_file)
        assert config.profiles == []

    def test_profile_inherits_top_level_sync(self, tmp_path):
        """Profile without explicit sync settings inherits from top-level."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
            '[monarch.sync]\n'
            'lookback_days = 45\n'
            'default_account = "Expenses:TopLevel"\n'
            'recategorize_account = "Expenses:TopRecat"\n\n'
            '[monarch.profiles.minimal]\n'
            'ledger = "main"\n'
        )
        config = parse_monarch_config(config_file)
        assert len(config.profiles) == 1
        p = config.profiles[0]
        assert p.sync.lookback_days == 45
        assert p.sync.default_account == "Expenses:TopLevel"
        assert p.sync.recategorize_account == "Expenses:TopRecat"


class TestSyncMonarchPendingGuard:
    """ISSUE-160: don't book a Monarch transaction until it settles.

    Monarch drops a pending row and re-creates it with a new id and the
    final (post-tip) amount when it settles. sync-monarch dedups on
    monarch-id and a content hash of date+amount+merchant, so the pending
    and settled twins both look new and the pending copy is left stale
    when the settled one lands in a later sync. Skipping pending rows at
    the source means the ghost is never booked in the first place.
    """

    def _config(self):
        return MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )

    def _ledger(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            "2024-01-01 open Assets:Bank:Checking\n"
            "2024-01-01 open Liabilities:Visa\n"
            "2024-01-01 open Expenses:Meals\n"
        )
        return ledger

    def test_pending_row_is_skipped(self, tmp_path):
        ledger = self._ledger(tmp_path)
        txns = [{
            "id": "248903185098537619", "date": "2026-07-11",
            "merchant": {"name": "Hi Tops"},
            "category": {"name": "Meals"},
            "account": {"displayName": "Visa"},
            "amount": -29.68, "notes": "", "tags": [],
            "pending": True,
        }]
        result = sync_monarch(ledger, self._config(), transactions=txns)
        assert result["status"] == "ok"
        assert result["transaction_count"] == 0
        assert result["pending_skipped_count"] == 1
        # Nothing booked
        assert "Hi Tops" not in ledger.read_text()

    def test_settled_row_imports_normally(self, tmp_path):
        ledger = self._ledger(tmp_path)
        txns = [{
            "id": "249176335512173524", "date": "2026-07-13",
            "merchant": {"name": "Hi Tops"},
            "category": {"name": "Meals"},
            "account": {"displayName": "Visa"},
            "amount": -35.00, "notes": "", "tags": [],
            "pending": False,
        }]
        result = sync_monarch(ledger, self._config(), transactions=txns)
        assert result["status"] == "ok"
        assert result["transaction_count"] == 1
        assert result["pending_skipped_count"] == 0
        assert "Hi Tops" in ledger.read_text()

    def test_missing_pending_field_imports(self, tmp_path):
        """Back-compat: a row with no ``pending`` key is treated as settled."""
        ledger = self._ledger(tmp_path)
        txns = [{
            "id": "mon-1", "date": "2026-07-13",
            "merchant": {"name": "Corner Store"},
            "category": {"name": "Meals"},
            "account": {"displayName": "Visa"},
            "amount": -12.00, "notes": "", "tags": [],
        }]
        result = sync_monarch(ledger, self._config(), transactions=txns)
        assert result["transaction_count"] == 1
        assert result["pending_skipped_count"] == 0

    def test_pending_then_settled_sequence_yields_one_entry(self, tmp_path):
        """The two-sync pending→settled lifecycle books exactly one entry."""
        ledger = self._ledger(tmp_path)
        config = self._config()

        # First sync: only the pending pre-tip charge is available.
        pending = {
            "id": "248903185098537619", "date": "2026-07-11",
            "merchant": {"name": "Hi Tops"},
            "category": {"name": "Meals"},
            "account": {"displayName": "Visa"},
            "amount": -29.68, "notes": "", "tags": [],
            "pending": True,
        }
        r1 = sync_monarch(ledger, config, transactions=[pending])
        assert r1["transaction_count"] == 0

        # Second sync: Monarch has dropped the pending row and created a
        # fresh settled row with a new id and the post-tip amount.
        settled = {
            "id": "249176335512173524", "date": "2026-07-13",
            "merchant": {"name": "Hi Tops"},
            "category": {"name": "Meals"},
            "account": {"displayName": "Visa"},
            "amount": -35.00, "notes": "", "tags": [],
            "pending": False,
        }
        r2 = sync_monarch(ledger, config, transactions=[settled])
        assert r2["transaction_count"] == 1

        # Exactly one Hi Tops entry, and it's the settled $35.00 — no ghost.
        text = ledger.read_text()
        assert text.count('"Hi Tops"') == 1
        assert "35.00" in text
        assert "29.68" not in text


class TestSyncAllProfiles:
    def test_no_profiles_uses_default_ledger(self, tmp_path):
        """Without profiles, syncs to the default (first) ledger."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = MonarchConfig(
            credentials=MonarchCredentials(session_id="sid", csrftoken="csrf"),
            sync=MonarchSyncSettings(lookback_days=30),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )
        ledgers = [{"name": "main", "path": ledger}]

        with patch("istota.money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = []
            result = sync_all_profiles(config, ledgers, dry_run=True)

        assert result["status"] == "ok"
        assert "profiles" not in result  # no profiles = single sync result

    def test_multiple_profiles(self, tmp_path):
        """Syncs each profile to its target ledger."""
        biz_ledger = tmp_path / "business.beancount"
        biz_ledger.write_text("")
        personal_ledger = tmp_path / "personal.beancount"
        personal_ledger.write_text("")

        config = MonarchConfig(
            credentials=MonarchCredentials(session_id="sid", csrftoken="csrf"),
            sync=MonarchSyncSettings(lookback_days=30),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[
                MonarchProfile(
                    name="business", ledger="business",
                    sync=MonarchSyncSettings(default_account="Expenses:Biz"),
                    accounts={"Chase": "Assets:Biz:Chase"},
                    categories={}, tags=MonarchTagFilters(include=["business"]),
                ),
                MonarchProfile(
                    name="personal", ledger="personal",
                    sync=MonarchSyncSettings(default_account="Expenses:Personal"),
                    accounts={}, categories={},
                    tags=MonarchTagFilters(exclude=["business"]),
                ),
            ],
        )
        ledgers = [
            {"name": "business", "path": biz_ledger},
            {"name": "personal", "path": personal_ledger},
        ]

        with patch("istota.money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = []
            result = sync_all_profiles(config, ledgers, dry_run=True)

        assert result["status"] == "ok"
        assert len(result["profiles"]) == 2
        names = [p["name"] for p in result["profiles"]]
        assert "business" in names
        assert "personal" in names
        # API fetched only once
        mock_fetch.assert_called_once()

    def test_profile_routes_transactions_by_tags(self, tmp_path):
        """Transactions are routed to profiles based on tag filters."""
        biz_ledger = tmp_path / "business.beancount"
        biz_ledger.write_text("")
        personal_ledger = tmp_path / "personal.beancount"
        personal_ledger.write_text("")

        config = MonarchConfig(
            credentials=MonarchCredentials(session_id="sid", csrftoken="csrf"),
            sync=MonarchSyncSettings(lookback_days=30),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[
                MonarchProfile(
                    name="business", ledger="business",
                    sync=MonarchSyncSettings(default_account="Expenses:Biz"),
                    accounts={}, categories={},
                    tags=MonarchTagFilters(include=["business"]),
                ),
                MonarchProfile(
                    name="personal", ledger="personal",
                    sync=MonarchSyncSettings(default_account="Expenses:Personal"),
                    accounts={}, categories={},
                    tags=MonarchTagFilters(exclude=["business"]),
                ),
            ],
        )
        ledgers = [
            {"name": "business", "path": biz_ledger},
            {"name": "personal", "path": personal_ledger},
        ]

        mock_txns = [
            {
                "id": "txn-biz",
                "date": "2026-01-15",
                "merchant": {"name": "Office Store"},
                "category": {"name": "Shopping"},
                "account": {"displayName": "Chase"},
                "amount": -50.0,
                "notes": "",
                "tags": [{"name": "business"}],
            },
            {
                "id": "txn-personal",
                "date": "2026-01-16",
                "merchant": {"name": "Grocery"},
                "category": {"name": "Groceries"},
                "account": {"displayName": "Chase"},
                "amount": -30.0,
                "notes": "",
                "tags": [],
            },
        ]

        with patch("istota.money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = mock_txns
            result = sync_all_profiles(config, ledgers, dry_run=True)

        biz_result = next(p for p in result["profiles"] if p["name"] == "business")
        personal_result = next(p for p in result["profiles"] if p["name"] == "personal")
        assert biz_result["transaction_count"] == 1
        assert personal_result["transaction_count"] == 1

    def test_profile_ledger_not_found(self, tmp_path):
        """Error when a profile references a non-existent ledger."""
        config = MonarchConfig(
            credentials=MonarchCredentials(session_id="sid", csrftoken="csrf"),
            sync=MonarchSyncSettings(), accounts={}, categories={},
            tags=MonarchTagFilters(),
            profiles=[
                MonarchProfile(
                    name="missing", ledger="nonexistent",
                    sync=MonarchSyncSettings(), accounts={}, categories={},
                    tags=MonarchTagFilters(),
                ),
            ],
        )
        ledgers = [{"name": "main", "path": tmp_path / "main.beancount"}]

        with patch("istota.money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = []
            result = sync_all_profiles(config, ledgers)

        # Should report error for the missing ledger profile
        assert result["profiles"][0]["status"] == "error"


class TestImportCSV:
    def test_import_creates_staging_and_appends(self, tmp_path):
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text("2026-01-01 open Assets:Bank:Checking USD\n")

        csv_file = tmp_path / "export.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Whole Foods,Groceries,Chase,WHOLE FOODS,,-85.50,,Alice\n"
        )

        result = import_csv(ledger_file, csv_file, "Assets:Bank:Checking")

        assert result["status"] == "ok"
        assert result["transaction_count"] == 1
        assert "staging_file" in result

        staging = Path(result["staging_file"])
        assert staging.exists()
        assert "Whole Foods" in staging.read_text()

        assert "Whole Foods" in ledger_file.read_text()

        backups_dir = ledger_dir / "backups"
        assert backups_dir.exists()

    def test_import_file_not_found(self, tmp_path):
        ledger_file = tmp_path / "main.beancount"
        ledger_file.write_text("")
        result = import_csv(ledger_file, tmp_path / "missing.csv", "Assets:Bank")
        assert result["status"] == "error"


class TestAddTransaction:
    def test_success(self, tmp_path):
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text("; main ledger\n")

        with patch("istota.money.core.ledger.run_bean_check") as mock_check:
            mock_check.return_value = (True, [])
            result = add_transaction(
                ledger_file, date(2026, 2, 4), "Test Store", "Test purchase",
                "Expenses:Food:Groceries", "Assets:Bank:Checking", 25.00,
            )

        assert result["status"] == "ok"
        assert result["payee"] == "Test Store"
        assert result["amount"] == 25.00

        # The transaction must land in the main ledger — the same file every
        # other write path appends to and the same file bean-check validates.
        assert "Test Store" in ledger_file.read_text()
        # It must NOT be misfiled into an orphan transactions/ subdir that no
        # ledger includes (ISSUE-158).
        assert not (ledger_dir / "transactions").exists()

    def test_lands_in_validated_ledger(self, tmp_path):
        """An added transaction must be visible to bean-check on the main ledger.

        Regression test for ISSUE-158: add_transaction previously wrote to an
        un-included transactions/ subdir, so the post-write bean-check passed
        vacuously against a ledger that never saw the entry.
        """
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text("; main ledger\n")

        checked_files = []

        def fake_check(path):
            checked_files.append(Path(path))
            assert "Groceries" in Path(path).read_text()
            return (True, [])

        with patch("istota.money.core.ledger.run_bean_check", side_effect=fake_check):
            result = add_transaction(
                ledger_file, date(2026, 2, 4), "Groceries", "Weekly shop",
                "Expenses:Food:Groceries", "Assets:Bank:Checking", 25.00,
            )

        assert result["status"] == "ok"
        assert checked_files == [ledger_file]

    def test_negative_amount(self, tmp_path):
        ledger_file = tmp_path / "main.beancount"
        ledger_file.write_text("")
        result = add_transaction(
            ledger_file, date(2026, 2, 4), "Store", "Purchase",
            "Expenses:Food", "Assets:Bank", -10.00,
        )
        assert result["status"] == "error"
        assert "Amount must be positive" in result["error"]

    def test_validation_failure(self, tmp_path):
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text('include "transactions/*.beancount"\n')

        with patch("istota.money.core.ledger.run_bean_check") as mock_check:
            mock_check.return_value = (False, ["Invalid account"])
            result = add_transaction(
                ledger_file, date(2026, 2, 4), "Store", "Purchase",
                "Expenses:Bad", "Assets:Bank", 10.00,
            )

        assert result["status"] == "error"
        assert "validation failed" in result["error"]


class TestLedgerFileOps:
    def test_backup_ledger(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("content")
        result = backup_ledger(ledger)
        assert result is not None
        assert result.exists()
        backups = list((tmp_path / "backups").glob("main.beancount.*"))
        assert len(backups) == 1

    def test_backup_nonexistent(self, tmp_path):
        result = backup_ledger(tmp_path / "missing.beancount")
        assert result is None

    def test_append_to_ledger(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("initial content\n")
        append_to_ledger(ledger, ["entry1", "entry2"])
        content = ledger.read_text()
        assert "entry1" in content
        assert "entry2" in content

    def test_append_empty_list(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("initial content\n")
        append_to_ledger(ledger, [])
        assert ledger.read_text() == "initial content\n"


class TestLedgerHasPosting:
    """ISSUE-071: detect when DB tracking is stale relative to ledger so
    we don't generate phantom category-change entries."""

    def _txn(self, txn_date="2026-05-01", merchant="Apple", account="Income:Consulting"):
        return MonarchSyncedTransaction(
            id=1,
            monarch_transaction_id="m1",
            tags_json=None,
            amount=13.99,
            merchant=merchant,
            posted_account=account,
            txn_date=txn_date,
            contra_account=None,
        )

    def test_ledger_contains_old_account_returns_true(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-05-01 * "Apple" "App Store"\n'
            '  Assets:Bank:Checking          13.99 USD\n'
            '  Income:Consulting            -13.99 USD\n'
        )
        synced = self._txn()
        assert _ledger_has_posting(ledger, synced, "Income:Consulting") is True

    def test_ledger_already_changed_returns_false(self, tmp_path):
        """The actual ISSUE-071 case: user manually edited the posting
        from Income:Consulting to Income:Sales. The next sync must NOT
        emit another category-change entry."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-05-01 * "Apple" "App Store"\n'
            '  Assets:Bank:Checking          13.99 USD\n'
            '  Income:Sales                 -13.99 USD\n'
        )
        synced = self._txn()
        assert _ledger_has_posting(ledger, synced, "Income:Consulting") is False

    def test_missing_ledger_falls_back_to_true(self, tmp_path):
        """Conservative: emit the change rather than silently swallow it."""
        synced = self._txn()
        assert _ledger_has_posting(tmp_path / "missing.beancount", synced, "Income:Consulting") is True

    def test_missing_date_or_merchant_falls_back_to_true(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        no_date = self._txn(txn_date=None)
        no_merchant = self._txn(merchant=None)
        assert _ledger_has_posting(ledger, no_date, "Income:Consulting") is True
        assert _ledger_has_posting(ledger, no_merchant, "Income:Consulting") is True

    def test_different_date_does_not_match(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-04-01 * "Apple" "App Store"\n'
            '  Assets:Bank:Checking          13.99 USD\n'
            '  Income:Consulting            -13.99 USD\n'
        )
        synced = self._txn(txn_date="2026-05-01")
        assert _ledger_has_posting(ledger, synced, "Income:Consulting") is False

    def test_different_merchant_does_not_match(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-05-01 * "Microsoft" "Office"\n'
            '  Assets:Bank:Checking          13.99 USD\n'
            '  Income:Consulting            -13.99 USD\n'
        )
        synced = self._txn(merchant="Apple")
        assert _ledger_has_posting(ledger, synced, "Income:Consulting") is False

    def test_pending_marker_also_recognized(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-05-01 ! "Apple" "App Store"\n'
            '  Assets:Bank:Checking          13.99 USD\n'
            '  Income:Consulting            -13.99 USD\n'
        )
        synced = self._txn()
        assert _ledger_has_posting(ledger, synced, "Income:Consulting") is True

    def test_account_substring_does_not_false_match(self, tmp_path):
        """Income:ConsultingExtra must not match Income:Consulting."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-05-01 * "Apple" "App Store"\n'
            '  Assets:Bank:Checking          13.99 USD\n'
            '  Income:ConsultingExtra       -13.99 USD\n'
        )
        synced = self._txn()
        assert _ledger_has_posting(ledger, synced, "Income:Consulting") is False

    def test_multiple_same_day_returns_true_if_any_match(self, tmp_path):
        """Multiple txns same day, same merchant — if any still posts to
        the old account, prefer to emit the change (false positive over
        false negative)."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-05-01 * "Apple" "App Store 1"\n'
            '  Assets:Bank:Checking          13.99 USD\n'
            '  Income:Sales                 -13.99 USD\n'
            '\n'
            '2026-05-01 * "Apple" "App Store 2"\n'
            '  Assets:Bank:Checking          13.99 USD\n'
            '  Income:Consulting            -13.99 USD\n'
        )
        synced = self._txn()
        assert _ledger_has_posting(ledger, synced, "Income:Consulting") is True


class TestSyncMonarchNullAccountName:
    """A Monarch row whose account carries an explicit JSON null.

    `displayName` was the one of the four extracted fields with no `or ""`
    guard, so a null arrived as `None` and went to `map_monarch_account`. With
    an empty `accounts` map — the ordinary shape, since `default_account`
    exists for the user who maps none — nothing folded it and the default came
    back. Found while unifying the lookup (ISSUE-426): the moment anything in
    that lookup touches the key before the mapping is walked, the same row
    aborts the whole sync.
    """

    def _config(self):
        return MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )

    def _txn(self):
        return {
            "id": "100000000000000001", "date": "2026-07-13",
            "merchant": {"name": "Corner Cafe"},
            "category": {"name": "Meals"},
            "account": {"displayName": None},
            "amount": -35.00, "notes": "", "tags": [],
            "pending": False,
        }

    def _ledger(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        return ledger

    def test_it_books_against_the_default_account(self, tmp_path):
        ledger = self._ledger(tmp_path)
        result = sync_monarch(ledger, self._config(), transactions=[self._txn()])
        assert result["status"] == "ok"
        assert result["transaction_count"] == 1
        assert "Assets:Bank:Checking" in ledger.read_text()

    def test_it_books_against_a_mapped_account_too(self, tmp_path):
        """A non-empty map is the case the missing guard crashed on all along."""
        config = self._config()
        config.accounts = {"Visa": "Liabilities:Visa"}
        ledger = self._ledger(tmp_path)
        result = sync_monarch(ledger, config, transactions=[self._txn()])
        assert result["status"] == "ok"
        assert "Assets:Bank:Checking" in ledger.read_text()


class TestSyncMonarchImportedPayments:
    """ISSUE-083: the sync reports what it booked so invoices can be matched.

    ``result["imported"]`` is the seam the invoice matcher reads. Without it
    the CLI would have to re-parse the staging file to learn which credits
    were new this run.
    """

    def _config(self):
        return MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )

    def _ledger(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("2024-01-01 open Assets:Bank:Checking\n")
        return ledger

    def test_imported_lists_booked_transactions(self, tmp_path):
        txns = [{
            "id": "mon-1", "date": "2026-05-05",
            "merchant": {"name": "Northwind Ltd"},
            "category": {"name": "Consulting"},
            "account": {"displayName": "Checking"},
            "amount": 6400.00, "notes": "", "tags": [],
        }]
        result = sync_monarch(self._ledger(tmp_path), self._config(), transactions=txns)
        assert result["imported"] == [{
            "date": "2026-05-05", "amount": 6400.00, "payee": "Northwind Ltd",
        }]

    def test_debits_are_reported_too(self, tmp_path):
        """``imported`` mirrors what was booked; filtering is the matcher's job."""
        txns = [{
            "id": "mon-2", "date": "2026-05-06",
            "merchant": {"name": "Corner Store"},
            "category": {"name": "Meals"},
            "account": {"displayName": "Checking"},
            "amount": -12.00, "notes": "", "tags": [],
        }]
        result = sync_monarch(self._ledger(tmp_path), self._config(), transactions=txns)
        assert [r["amount"] for r in result["imported"]] == [-12.00]

    def test_skipped_transactions_are_not_imported(self, tmp_path):
        """A pending row is never booked, so it never reaches the matcher."""
        txns = [{
            "id": "mon-3", "date": "2026-05-07",
            "merchant": {"name": "Northwind Ltd"},
            "category": {"name": "Consulting"},
            "account": {"displayName": "Checking"},
            "amount": 6400.00, "notes": "", "tags": [], "pending": True,
        }]
        result = sync_monarch(self._ledger(tmp_path), self._config(), transactions=txns)
        assert result["imported"] == []

    def test_dry_run_reports_what_it_would_book(self, tmp_path):
        txns = [{
            "id": "mon-4", "date": "2026-05-08",
            "merchant": {"name": "Northwind Ltd"},
            "category": {"name": "Consulting"},
            "account": {"displayName": "Checking"},
            "amount": 500.00, "notes": "", "tags": [],
        }]
        result = sync_monarch(
            self._ledger(tmp_path), self._config(), transactions=txns, dry_run=True,
        )
        assert [r["amount"] for r in result["imported"]] == [500.00]


class TestAccountComponentUnicode:
    """The slug keeps the characters beancount keeps.

    `config_store._is_account` is deliberately Unicode-aware — beancount's own
    ACCOUNT_RE is — so an ASCII-only slug here would turn `Café` into `Caf` and
    collapse every category with no Latin letters onto one account, silently
    merging categories that have nothing to do with each other.
    """

    def test_accented_letters_survive(self):
        from istota.money.core.transactions import account_component

        assert account_component("Café") == "Café"
        assert account_component("Bücher & Zeitschriften") == "BücherZeitschriften"
        assert account_component("Forderungen Müller") == "ForderungenMüller"

    def test_underscore_is_not_a_beancount_character(self):
        from istota.money.core.transactions import account_component

        assert account_component("internet_services") == "Internetservices"

    def test_uncased_scripts_do_not_collapse_onto_one_account(self):
        from istota.money.core.transactions import account_component

        first = account_component("日用品")
        second = account_component("交通費")
        assert first != second
        assert first != "Unknown" and second != "Unknown"

    def test_every_slug_is_an_account_beancount_accepts(self):
        from istota.money.config_store import _is_account
        from istota.money.core.transactions import map_monarch_category

        for category in (
            "Internet Services (Reimbursed)", "Café", "Bücher & Zeitschriften",
            "日用品", "e-bike / repair", "401k match", "~~~", "Forderungen Müller",
        ):
            account = map_monarch_category(category)
            assert _is_account(account), f"{category!r} -> {account!r}"

    def test_unicode_slugs_load_in_beancount(self, tmp_path):
        from beancount import loader

        from istota.money.core.transactions import map_monarch_category

        for category in (
            "Café", "Bücher & Zeitschriften", "日用品", "Ⅷ", "²x", "٣abc",
            "Internet Services (Reimbursed)", "Sub-", "misc", "~~~",
        ):
            ledger = tmp_path / "t.beancount"
            ledger.write_text(
                'plugin "beancount.plugins.auto_accounts"\n'
                '2026-08-30 * "Payee" "note"\n'
                f"  {map_monarch_category(category)}  1.00 USD\n"
                "  Liabilities:Visa-Fidelity\n"
            )
            _, errors, _ = loader.load_file(str(ledger))
            assert errors == [], f"{category}: {errors}"


class TestDefaultAccountReachesThePosting:
    """Why an unparseable `default_account` is the same defect as a bad map.

    `map_monarch_account` returns the configured default verbatim for every
    Monarch account with no mapping, so whatever is stored there is written
    into the ledger unexamined.
    """

    def test_unmapped_account_falls_back_to_the_configured_default(self):
        from istota.money.core.models import (
            MonarchConfig,
            MonarchCredentials,
            MonarchSyncSettings,
            MonarchTagFilters,
        )
        from istota.money.core.transactions import map_monarch_account

        cfg = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )
        assert map_monarch_account("Some Card", cfg) == "Assets:Bank:Checking"


class TestAccountComponentShape:
    def test_a_trailing_dash_is_kept(self):
        """Beancount's component class allows one, so stripping it would rename
        an account that already worked."""
        from istota.money.core.transactions import account_component

        assert account_component("Sub-") == "Sub-"
        assert account_component("--Water") == "Water"
        assert account_component("---") == "Unknown"

    def test_a_lowercase_initial_is_raised(self):
        """`Expenses:Uncategorized:misc` is not a valid account and never was —
        beancount rejects a lowercase component initial."""
        from istota.money.core.transactions import account_component

        assert account_component("misc") == "Misc"

    def test_punctuation_only_differences_collide(self):
        """Documented rather than fixed: replacing a separator with a dash would
        rename every multi-word category's account."""
        from istota.money.core.transactions import account_component

        assert account_component("Food & Drink") == account_component("Food Drink")


# =============================================================================
# Stage 3: the rules engine on the import path
# =============================================================================


def _rule(rule_id, **kw):
    """One stored rule, with the columns a test does not care about defaulted."""
    fields = {
        "ledger": "", "source": "", "field": "category", "match_kind": "iexact",
        "match_value": "", "action": "posting_account", "target": "",
        "priority": 100, "enabled": True, "origin": "user", "note": "",
    }
    fields.update(kw)
    return rule_engine.Rule(id=rule_id, **fields)


def _compiled(*rules):
    return rule_engine.compile_rules(rules)


def _strip_ids(text: str) -> str:
    """Ledger text with the per-entry random ``id:`` metadata removed."""
    return re.sub(r'id: "[^"]*"', 'id: "X"', text)


class TestNormalizeMonarchTxn:
    """The seam the rules engine needs, and the one a Fidelity source mirrors."""

    def test_full_payload(self):
        txn = _normalize_monarch_txn({
            "id": "mon-1", "date": "2026-07-13T00:00:00",
            "merchant": {"name": "Hi Tops"},
            "category": {"name": "Meals"},
            "account": {"displayName": "Visa"},
            "amount": -35.0, "notes": "team lunch",
            "tags": [{"name": "Business"}, {"name": "Reimbursable"}],
        })
        assert txn.date == date(2026, 7, 13)
        assert txn.payee == "Hi Tops"
        assert txn.category == "Meals"
        assert txn.account_name == "Visa"
        assert txn.amount == -35.0
        assert txn.notes == "team lunch"
        assert txn.tags == ["Business", "Reimbursable"]
        assert txn.source_id == "mon-1"

    def test_merchant_falls_back_to_name(self):
        txn = _normalize_monarch_txn({
            "date": "2026-07-13", "name": "Raw Statement Text",
            "merchant": {}, "amount": -1.0,
        })
        assert txn.payee == "Raw Statement Text"

    def test_merchant_falls_back_to_unknown(self):
        txn = _normalize_monarch_txn({"date": "2026-07-13", "amount": -1.0})
        assert txn.payee == "Unknown"

    def test_empty_category_defaults_to_uncategorized(self):
        """The sync's own default, not NormalizedTransaction's empty string."""
        txn = _normalize_monarch_txn({
            "date": "2026-07-13", "merchant": {"name": "X"},
            "category": {"name": ""}, "amount": -1.0,
        })
        assert txn.category == "Uncategorized"

    def test_null_notes_becomes_empty_string(self):
        txn = _normalize_monarch_txn({
            "date": "2026-07-13", "merchant": {"name": "X"},
            "amount": -1.0, "notes": None,
        })
        assert txn.notes == ""

    def test_unparseable_date_returns_none(self):
        assert _normalize_monarch_txn({"date": "", "amount": -1.0}) is None
        assert _normalize_monarch_txn({"date": "not-a-date", "amount": -1.0}) is None

    def test_raw_payload_is_carried(self):
        payload = {"date": "2026-07-13", "merchant": {"name": "X"}, "amount": -1.0}
        assert _normalize_monarch_txn(payload).raw is payload


class TestSyncMonarchRulesInertness:
    """``rules=None`` must preserve today's dict path exactly."""

    def _config(self):
        return MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={"Visa": "Liabilities:Visa"},
            categories={"Meals": "Expenses:Meals"},
            tags=MonarchTagFilters(),
        )

    def _txns(self):
        return [
            {
                "id": "mon-1", "date": "2026-07-13",
                "merchant": {"name": "Hi Tops"}, "category": {"name": "Meals"},
                "account": {"displayName": "Visa"},
                "amount": -35.0, "notes": "", "tags": [],
            },
            {
                # Unmapped category and unmapped account: both fallbacks.
                "id": "mon-2", "date": "2026-07-14",
                "merchant": {"name": "Odd Shop"},
                "category": {"name": "Weird & Rare"},
                "account": {"displayName": "Unknown Bank"},
                "amount": -12.5, "notes": "n", "tags": [],
            },
            {
                # A category the shipped constant knows and the config does not.
                "id": "mon-3", "date": "2026-07-15",
                "merchant": {"name": "Market"}, "category": {"name": "Groceries"},
                "account": {"displayName": "Visa"},
                "amount": -80.0, "notes": "", "tags": [],
            },
        ]

    def test_rules_none_produces_byte_identical_entries(self, tmp_path):
        """The compatibility contract every existing caller depends on."""
        entries = []
        for name in ("without", "with_none"):
            ledger = tmp_path / f"{name}.beancount"
            ledger.write_text("")
            kwargs = {} if name == "without" else {"rules": None}
            result = sync_monarch(
                ledger, self._config(), dry_run=True,
                transactions=self._txns(), **kwargs,
            )
            entries.append([_strip_ids(e) for e in result["sample_entries"]])
        assert entries[0] == entries[1]
        # And the accounts are the ones the dict path picks.
        booked = "\n".join(entries[0])
        assert "Expenses:Meals" in booked
        assert "Liabilities:Visa" in booked
        assert "Expenses:Uncategorized:WeirdRare" in booked
        assert "Assets:Bank:Checking" in booked
        assert "Expenses:Food:Groceries" in booked

    def test_rules_none_reports_no_rule_keys(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        result = sync_monarch(
            ledger, self._config(), dry_run=True, transactions=self._txns(),
        )
        assert "rule_skipped_count" not in result
        assert "rule_drop_count" not in result

    def test_empty_rule_list_falls_back_to_uncategorized(self, tmp_path):
        """An empty list is *not* ``None``: the dict tiers no longer apply.

        ``[]`` means the store answered and had nothing in scope, which on a
        migrated deployment cannot happen — the seed alone fills it. It is
        asserted because it is the difference between the two sentinels.
        """
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        result = sync_monarch(
            ledger, self._config(), dry_run=True,
            transactions=self._txns(), rules=[],
        )
        booked = "\n".join(result["sample_entries"])
        assert "Expenses:Uncategorized:Meals" in booked
        assert "Expenses:Uncategorized:Groceries" in booked
        # The contra slot falls back to the profile's default account.
        assert "Liabilities:Visa" not in booked
        assert "Assets:Bank:Checking" in booked


class TestSyncMonarchWithRules:
    def _config(self):
        return MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )

    def _txns(self):
        return [
            {
                "id": "mon-1", "date": "2026-07-13",
                "merchant": {"name": "Hi Tops"}, "category": {"name": "Meals"},
                "account": {"displayName": "Visa"},
                "amount": -35.0, "notes": "", "tags": [],
            },
            {
                "id": "mon-2", "date": "2026-07-14",
                "merchant": {"name": "Market"}, "category": {"name": "Groceries"},
                "account": {"displayName": "Checking"},
                "amount": -80.0, "notes": "", "tags": [],
            },
        ]

    def test_per_transaction_contra_accounts(self, tmp_path):
        """The half ``map_monarch_account`` could do and ``import_csv`` could not."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        rules = _compiled(
            _rule(1, field="account", match_value="Visa",
                  action="contra_account", target="Liabilities:Visa"),
            _rule(2, field="account", match_value="Checking",
                  action="contra_account", target="Assets:Bank:Checking"),
            _rule(3, field="category", match_value="Meals",
                  action="posting_account", target="Expenses:Business:Meals"),
        )
        result = sync_monarch(
            ledger, self._config(), dry_run=True,
            transactions=self._txns(), rules=rules,
        )
        first, second = result["sample_entries"]
        assert "Liabilities:Visa" in first
        assert "Expenses:Business:Meals" in first
        assert "Assets:Bank:Checking" in second
        # No posting rule for Groceries: the Uncategorized fallback.
        assert "Expenses:Uncategorized:Groceries" in second

    def test_skip_rule_removes_a_transaction_and_is_counted(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        rules = _compiled(
            _rule(1, field="category", match_value="Groceries",
                  action="skip", target="", priority=50),
        )
        result = sync_monarch(
            ledger, self._config(), dry_run=True,
            transactions=self._txns(), rules=rules,
        )
        assert result["transaction_count"] == 1
        assert result["rule_skipped_count"] == 1
        assert "Market" not in "\n".join(result["sample_entries"])

    def test_a_skipped_transaction_is_not_dedup_tracked(self, tmp_path):
        import sqlite3

        from istota.money.db import init_db as money_init_db

        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        db_path = tmp_path / "money.db"
        money_init_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rules = _compiled(
            _rule(1, field="payee", match_value="Market",
                  action="skip", target="", priority=50),
        )
        try:
            sync_monarch(
                ledger, self._config(), db_conn=conn,
                transactions=self._txns(), rules=rules,
            )
            conn.commit()
            ids = {
                r["monarch_transaction_id"]
                for r in conn.execute(
                    "SELECT monarch_transaction_id FROM monarch_synced_transactions"
                )
            }
        finally:
            conn.close()
        assert ids == {"mon-1"}

    def test_source_provenance_and_rule_ids_are_recorded(self, tmp_path):
        import sqlite3

        from istota.money.db import init_db as money_init_db

        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        db_path = tmp_path / "money.db"
        money_init_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rules = _compiled(
            _rule(7, field="category", match_value="Meals",
                  action="posting_account", target="Expenses:Business:Meals"),
            _rule(9, field="account", match_value="Visa",
                  action="contra_account", target="Liabilities:Visa"),
        )
        try:
            sync_monarch(
                ledger, self._config(), db_conn=conn,
                transactions=self._txns()[:1], rules=rules,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM monarch_synced_transactions "
                "WHERE monarch_transaction_id = 'mon-1'"
            ).fetchone()
        finally:
            conn.close()
        assert row["src_category"] == "Meals"
        assert row["src_account"] == "Visa"
        assert row["src_source"] == "monarch-api"
        assert row["rule_ids"] == "[7, 9]"

    def test_rule_ids_is_null_when_no_rules_ran(self, tmp_path):
        """``None`` and ``[]`` are different facts: no engine, versus no hit."""
        import sqlite3

        from istota.money.db import init_db as money_init_db

        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        db_path = tmp_path / "money.db"
        money_init_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            sync_monarch(
                ledger, self._config(), db_conn=conn,
                transactions=self._txns()[:1],
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM monarch_synced_transactions"
            ).fetchone()
        finally:
            conn.close()
        assert row["rule_ids"] is None
        assert row["src_category"] == "Meals"

    def test_exclude_tags_are_carried_by_a_skip_rule(self, tmp_path):
        """The exclusion moved into the pass, so a skip rule carries it."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = self._config()
        config.tags = MonarchTagFilters(exclude=["Personal"])
        txns = self._txns()
        txns[1]["tags"] = [{"name": "Personal"}]
        rules = _compiled(
            _rule(1, field="tag", match_value="Personal",
                  action="skip", target="", priority=50),
        )
        result = sync_monarch(
            ledger, config, dry_run=True, transactions=txns, rules=rules,
        )
        assert result["transaction_count"] == 1
        assert result["rule_skipped_count"] == 1

    def test_include_tags_still_gate_ahead_of_the_rules(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = self._config()
        config.tags = MonarchTagFilters(include=["Business"])
        txns = self._txns()
        txns[0]["tags"] = [{"name": "Business"}]
        result = sync_monarch(
            ledger, config, dry_run=True, transactions=txns, rules=[],
        )
        assert result["transaction_count"] == 1
        # Gated out before the pass, so it is not a rule skip.
        assert result.get("rule_skipped_count", 0) == 0

    def _synced_conn(self, tmp_path):
        import sqlite3

        from istota.money.db import init_db as money_init_db

        db_path = tmp_path / "money.db"
        money_init_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def test_reconciliation_agrees_with_the_ingest_pass(self, tmp_path):
        """Otherwise a category-change entry is booked on every sync, for ever.

        The reconciliation used to recompute through ``config.categories``,
        which is the *compatibility view* of the rules table and cannot express
        a ``payee`` match. A transaction the ingest posted by rule would then
        look changed on the next run, and the correcting entry double-counts.
        """
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        conn = self._synced_conn(tmp_path)
        rules = _compiled(
            _rule(1, field="payee", match_value="Hi Tops",
                  action="posting_account", target="Expenses:Business:Meals"),
        )
        txns = self._txns()[:1]
        try:
            first = sync_monarch(
                ledger, self._config(), db_conn=conn,
                transactions=txns, rules=rules,
            )
            conn.commit()
            second = sync_monarch(
                ledger, self._config(), db_conn=conn,
                transactions=txns, rules=rules,
            )
            conn.commit()
        finally:
            conn.close()
        assert first["transaction_count"] == 1
        assert second["transaction_count"] == 0
        assert second["category_changed_count"] == 0
        assert ledger.read_text().count("Expenses:Business:Meals") == 1

    def test_a_skip_rule_does_not_reverse_an_already_booked_transaction(self, tmp_path):
        """Reversal is what an exclude tag does; a new rule is not retroactive."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        conn = self._synced_conn(tmp_path)
        txns = self._txns()[:1]
        try:
            sync_monarch(
                ledger, self._config(), db_conn=conn,
                transactions=txns, rules=[],
            )
            conn.commit()
            after = sync_monarch(
                ledger, self._config(), db_conn=conn, transactions=txns,
                rules=_compiled(
                    _rule(1, field="payee", match_value="Hi Tops",
                          action="skip", target="", priority=50),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        assert after["recategorized_count"] == 0
        assert after["category_changed_count"] == 0

    def test_a_tag_skip_rule_does_reverse_one(self, tmp_path):
        """The counterpart, and the asymmetry is the inertness contract.

        `config_store._load_tag_filters` builds `MonarchTagFilters.exclude`
        out of the `field='tag'` skip rules, so such a rule still reaches
        `still_has_business_tag` and still reverses a booked transaction —
        exactly as the config value it replaced did. A `payee` skip has no
        such predecessor and stays non-retroactive.
        """
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        conn = self._synced_conn(tmp_path)
        txns = self._txns()[:1]
        txns[0]["tags"] = [{"name": "Personal"}]
        config = self._config()
        try:
            first = sync_monarch(
                ledger, config, db_conn=conn, transactions=txns, rules=[],
            )
            conn.commit()
            # The tag is now excluded, which is what a migrated tag skip rule
            # renders as through the compatibility view.
            config.tags = MonarchTagFilters(exclude=["Personal"])
            after = sync_monarch(
                ledger, config, db_conn=conn, transactions=txns,
                rules=_compiled(
                    _rule(1, field="tag", match_value="Personal",
                          action="skip", target="", priority=50),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        assert first["transaction_count"] == 1
        assert after["recategorized_count"] == 1

    def test_an_unmigrated_store_leaves_the_dict_path_standing(self, tmp_path):
        """The one thing that makes a failed migration safe.

        `init_db` runs the migration and the seed as two independent guarded
        savepoints, so a migration that fails leaves the seed rows behind with
        its own sentinel unwritten. Handing those on would run the import
        against a rule set carrying none of the user's own map while the
        `MonarchConfig` beside it was still served from the legacy tables.
        """
        import sqlite3

        db_path = tmp_path / "money.db"

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError("migration failed")

        # Deleting the sentinel by hand is not this shape: every entry point
        # calls `init_db`, which would simply migrate again. The migration has
        # to fail from *inside* its own guarded savepoint, so the savepoint
        # rolls back and leaves the sentinel unwritten while the seed's
        # separate savepoint still commits.
        with patch.object(
            config_store, "_migrate_one_map", side_effect=_boom,
        ):
            config_store.init_db(db_path)
            rules, dropped = load_import_rules(
                db_path, "personal", "monarch-api",
            )

        conn = sqlite3.connect(str(db_path))
        seeded = conn.execute(
            "SELECT COUNT(*) FROM transaction_rules"
        ).fetchone()[0]
        sentinel = conn.execute(
            "SELECT 1 FROM schema_meta WHERE key = 'transaction_rules_migrated_at'"
        ).fetchone()
        conn.close()
        assert seeded > 0, "the seed must have run for this to be the real shape"
        assert sentinel is None

        assert rules is None
        assert dropped == []

        # And the sync then posts the config's own accounts, which on this
        # deployment are still being served out of the legacy tables.
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = self._config()
        config.categories = {"Meals": "Expenses:Config:Meals"}
        config.accounts = {"Visa": "Liabilities:Config:Visa"}
        result = sync_monarch(
            ledger, config, dry_run=True,
            transactions=self._txns()[:1], rules=rules,
        )
        booked = "\n".join(result["sample_entries"])
        assert "Expenses:Config:Meals" in booked
        assert "Liabilities:Config:Visa" in booked

    def test_a_profile_with_no_ledger_does_not_get_the_global_scope(self, tmp_path):
        """`_rule_scope`'s refusal, reached by a second route.

        An empty ledger resolves to `''`, which the engine reads as "any", so
        such a profile would be scored against the global map while its own
        config was still served from the legacy tables.
        """
        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        loaded = load_import_rules_for_ledgers(
            db_path, ["", "business"], "monarch-api",
        )
        assert loaded[""] == (None, [])
        assert loaded["business"][0] is not None

    def test_a_dropped_rule_reaches_the_import_result(self, tmp_path):
        """A dropped ``skip`` widens the import, so a log line is not enough."""
        import sqlite3

        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        created = config_store.create_transaction_rule(
            db_path, ledger="", source="", field="category",
            match_kind="iexact", match_value="Meals", action="skip",
            target="", priority=50, enabled=True, origin="user", note="",
        )
        # Reach past validation the way a hand edit or a rollback would.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE transaction_rules SET match_kind = 'regex' WHERE id = ?",
            (created["id"],),
        )
        conn.commit()
        conn.close()

        rules, dropped = load_import_rules(db_path, "", "monarch-api")
        assert dropped == [created["id"]]
        assert all(c.id != created["id"] for c in rules)

        result = sync_monarch(
            ledger, self._config(), dry_run=True,
            transactions=self._txns(), rules=rules,
        )
        annotate_rule_drops(result, dropped)
        assert result["rule_drop_count"] == 1
        assert result["dropped_rule_ids"] == [created["id"]]
        # And the skip did not happen, which is the direction that matters.
        assert result["transaction_count"] == 2


class TestSyncAllProfilesLoadsRules:
    def _ledgers(self, tmp_path):
        biz = tmp_path / "business.beancount"
        biz.write_text("")
        return [{"name": "business", "path": biz}]

    def _txns(self):
        return [{
            "id": "mon-1", "date": "2026-07-13",
            "merchant": {"name": "Hi Tops"}, "category": {"name": "Meals"},
            "account": {"displayName": "Visa"},
            "amount": -35.0, "notes": "", "tags": [],
        }]

    def test_no_profiles_branch_loads_rules_for_the_first_ledger(self, tmp_path):
        ledgers = self._ledgers(tmp_path)
        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        config_store.create_transaction_rule(
            db_path, ledger="business", source="monarch-api", field="category",
            match_kind="iexact", match_value="Meals", action="posting_account",
            target="Expenses:Business:Meals", priority=10, enabled=True,
            origin="user", note="",
        )
        config = MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )
        with patch(
            "istota.money.core.transactions.fetch_monarch_transactions"
        ) as mock_fetch:
            mock_fetch.return_value = self._txns()
            result = sync_all_profiles(
                config, ledgers, dry_run=True, db_path=db_path,
            )
        assert "Expenses:Business:Meals" in "\n".join(result["sample_entries"])

    def test_a_profile_gets_its_own_ledger_scope(self, tmp_path):
        biz = tmp_path / "business.beancount"
        biz.write_text("")
        personal = tmp_path / "personal.beancount"
        personal.write_text("")
        ledgers = [
            {"name": "business", "path": biz},
            {"name": "personal", "path": personal},
        ]
        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        config_store.create_transaction_rule(
            db_path, ledger="business", source="monarch-api", field="category",
            match_kind="iexact", match_value="Meals", action="posting_account",
            target="Expenses:Business:Meals", priority=10, enabled=True,
            origin="user", note="",
        )
        config_store.create_transaction_rule(
            db_path, ledger="personal", source="monarch-api", field="category",
            match_kind="iexact", match_value="Meals", action="posting_account",
            target="Expenses:Personal:Meals", priority=10, enabled=True,
            origin="user", note="",
        )
        config = MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[
                MonarchProfile(
                    name="biz", ledger="business",
                    sync=MonarchSyncSettings(default_account="Assets:Biz"),
                    accounts={}, categories={}, tags=MonarchTagFilters(),
                ),
                MonarchProfile(
                    name="me", ledger="personal",
                    sync=MonarchSyncSettings(default_account="Assets:Me"),
                    accounts={}, categories={}, tags=MonarchTagFilters(),
                ),
            ],
        )
        with patch(
            "istota.money.core.transactions.fetch_monarch_transactions"
        ) as mock_fetch:
            mock_fetch.return_value = self._txns()
            result = sync_all_profiles(
                config, ledgers, dry_run=True, db_path=db_path,
            )
        by_name = {p["name"]: p for p in result["profiles"]}
        assert "Expenses:Business:Meals" in "\n".join(
            by_name["biz"]["sample_entries"]
        )
        assert "Expenses:Personal:Meals" in "\n".join(
            by_name["me"]["sample_entries"]
        )

    def test_rules_are_loaded_before_the_first_profile_writes(self, tmp_path):
        """A load from inside the loop is an immediate ``database is locked``.

        The sync writes through ``db_conn`` and holds that write transaction
        until it commits, and ``load_rules_for_run`` calls ``init_db`` on a
        second connection that sets no busy timeout — so the second profile's
        load does not wait, it fails. Two profiles on one connection is the
        smallest shape that reaches it.
        """
        import sqlite3

        from istota.money.db import init_db as money_init_db

        biz = tmp_path / "business.beancount"
        biz.write_text("")
        personal = tmp_path / "personal.beancount"
        personal.write_text("")
        ledgers = [
            {"name": "business", "path": biz},
            {"name": "personal", "path": personal},
        ]
        db_path = tmp_path / "money.db"
        money_init_db(db_path)
        config_store.init_db(db_path)
        config = MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[
                MonarchProfile(
                    name="biz", ledger="business",
                    sync=MonarchSyncSettings(default_account="Assets:Biz"),
                    accounts={}, categories={}, tags=MonarchTagFilters(),
                ),
                MonarchProfile(
                    name="me", ledger="personal",
                    sync=MonarchSyncSettings(default_account="Assets:Me"),
                    accounts={}, categories={}, tags=MonarchTagFilters(),
                ),
            ],
        )
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            with patch(
                "istota.money.core.transactions.fetch_monarch_transactions"
            ) as mock_fetch:
                mock_fetch.return_value = self._txns()
                result = sync_all_profiles(
                    config, ledgers, db_conn=conn, db_path=db_path,
                )
        finally:
            conn.close()
        assert [p["status"] for p in result["profiles"]] == ["ok", "ok"]

    def test_without_a_db_path_the_dict_path_is_unchanged(self, tmp_path):
        ledgers = self._ledgers(tmp_path)
        config = MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={}, categories={"Meals": "Expenses:Meals"},
            tags=MonarchTagFilters(),
        )
        with patch(
            "istota.money.core.transactions.fetch_monarch_transactions"
        ) as mock_fetch:
            mock_fetch.return_value = self._txns()
            result = sync_all_profiles(config, ledgers, dry_run=True)
        assert "Expenses:Meals" in "\n".join(result["sample_entries"])


class TestImportTransactionsWithRules:
    def _txns(self):
        from istota.money.core.importers.base import NormalizedTransaction

        return [
            NormalizedTransaction(
                date=date(2026, 7, 13), amount=-35.0, payee="Hi Tops",
                category="Meals", account_name="Visa", notes="", tags=[],
            ),
            NormalizedTransaction(
                date=date(2026, 7, 14), amount=-80.0, payee="Market",
                category="Groceries", account_name="Checking", notes="", tags=[],
            ),
        ]

    def test_a_rule_overrides_the_file_wide_contra_account(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        rules = _compiled(
            _rule(1, field="account", match_value="Visa",
                  action="contra_account", target="Liabilities:Visa"),
        )
        import_transactions(
            ledger_path=ledger, transactions=self._txns(),
            source_name="monarch-csv", contra_account="Assets:Fallback",
            rules=rules,
        )
        text = ledger.read_text()
        assert "Liabilities:Visa" in text
        # The second transaction has no account rule, so the file's --account.
        assert "Assets:Fallback" in text

    def test_category_map_is_ignored_when_rules_are_given(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        rules = _compiled(
            _rule(1, field="category", match_value="Meals",
                  action="posting_account", target="Expenses:Ruled"),
        )
        import_transactions(
            ledger_path=ledger, transactions=self._txns(),
            source_name="monarch-csv", contra_account="Assets:Fallback",
            category_map={"Meals": "Expenses:FromMap",
                          "Groceries": "Expenses:FromMap2"},
            rules=rules,
        )
        text = ledger.read_text()
        assert "Expenses:Ruled" in text
        assert "Expenses:FromMap" not in text
        assert "Expenses:Uncategorized:Groceries" in text

    def test_a_skip_rule_drops_a_row_and_is_counted(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        rules = _compiled(
            _rule(1, field="payee", match_value="Market",
                  action="skip", target="", priority=50),
        )
        result = import_transactions(
            ledger_path=ledger, transactions=self._txns(),
            source_name="monarch-csv", contra_account="Assets:Fallback",
            rules=rules,
        )
        assert result["transaction_count"] == 1
        assert result["rule_skipped_count"] == 1
        assert "Market" not in ledger.read_text()

    def test_every_row_skipped_is_still_an_ok_result(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        rules = _compiled(
            _rule(1, field="category", match_kind="contains", match_value="e",
                  action="skip", target="", priority=50),
        )
        result = import_transactions(
            ledger_path=ledger, transactions=self._txns(),
            source_name="monarch-csv", contra_account="Assets:Fallback",
            rules=rules,
        )
        assert result["status"] == "ok"
        assert result["transaction_count"] == 0
        assert result["rule_skipped_count"] == 2
        assert ledger.read_text() == ""

    def test_an_empty_category_keeps_the_bare_uncategorized_fallback(self, tmp_path):
        from istota.money.core.importers.base import NormalizedTransaction

        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        import_transactions(
            ledger_path=ledger,
            transactions=[NormalizedTransaction(
                date=date(2026, 7, 13), amount=-1.0, payee="X",
                category="", account_name="", notes="", tags=[],
            )],
            source_name="monarch-csv", contra_account="Assets:Fallback",
            rules=[],
        )
        text = ledger.read_text()
        assert "Expenses:Uncategorized " in text
        assert "Expenses:Uncategorized:" not in text

    def test_the_builtin_map_resolves_the_same_whether_passed_or_defaulted(
        self, tmp_path,
    ):
        """ISSUE-426: passing `MONARCH_CATEGORY_MAP` and passing nothing agreed.

        `import_transactions` had two arms for this — one calling a private
        copy of `map_monarch_category` over the map it was handed, one calling
        `map_monarch_category` itself — and the only map any caller ever passed
        was that same builtin, so both returned the same string.
        """
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        ledger_a = tmp_path / "a" / "main.beancount"
        ledger_a.write_text("")
        ledger_b = tmp_path / "b" / "main.beancount"
        ledger_b.write_text("")
        for ledger, kwargs in (
            (ledger_a, {"category_map": MONARCH_CATEGORY_MAP}),
            (ledger_b, {}),
        ):
            import_transactions(
                ledger_path=ledger, transactions=self._txns(),
                source_name="monarch-csv", contra_account="Assets:Fallback",
                **kwargs,
            )
        a = _strip_ids(ledger_a.read_text())
        b = _strip_ids(ledger_b.read_text())
        assert a == b
        # "Groceries" is in the builtin map, so this is a hit rather than two
        # matching fallbacks.
        assert "Expenses:Food:Groceries" in a

    def test_an_empty_category_map_falls_back_to_the_builtin(self, tmp_path):
        """`{}` took the old `elif` arm and takes the new `or`. Same answer."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        import_transactions(
            ledger_path=ledger, transactions=self._txns(),
            source_name="monarch-csv", contra_account="Assets:Fallback",
            category_map={},
        )
        assert "Expenses:Food:Groceries" in ledger.read_text()

    def test_a_category_map_that_differs_from_the_builtin_still_wins(self, tmp_path):
        """The arm `_map_category` used to serve, now that it is gone.

        `Groceries` is in `MONARCH_CATEGORY_MAP`, so a passed map that spells it
        differently is the only thing separating "the map was consulted" from
        "the builtin answered".
        """
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        import_transactions(
            ledger_path=ledger, transactions=self._txns(),
            source_name="monarch-csv", contra_account="Assets:Fallback",
            category_map={"Groceries": "Expenses:Custom"},
        )
        text = ledger.read_text()
        assert "Expenses:Custom" in text
        assert "Expenses:Food:Groceries" not in text

    def test_rules_none_leaves_the_category_map_path_alone(self, tmp_path):
        # Separate directories: ``parse_ledger_transactions`` also scans the
        # sibling ``imports/`` staging files, so two ledgers under one parent
        # would dedup against each other.
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        ledger_a = tmp_path / "a" / "main.beancount"
        ledger_a.write_text("")
        ledger_b = tmp_path / "b" / "main.beancount"
        ledger_b.write_text("")
        for ledger, kwargs in ((ledger_a, {}), (ledger_b, {"rules": None})):
            import_transactions(
                ledger_path=ledger, transactions=self._txns(),
                source_name="monarch-csv", contra_account="Assets:Fallback",
                category_map={"Meals": "Expenses:FromMap"}, **kwargs,
            )
        a = _strip_ids(ledger_a.read_text())
        b = _strip_ids(ledger_b.read_text())
        assert a == b
        assert "Expenses:FromMap" in a


class TestImportCSVResolvesRules:
    def _csv(self, tmp_path):
        path = tmp_path / "monarch.csv"
        path.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags\n"
            "2026-07-13,Hi Tops,Meals,Visa,HI TOPS SF,,-35.00,\n"
        )
        return path

    def test_a_csv_import_now_reads_the_users_rules(self, tmp_path):
        """The defect in Context 2: a CSV import ignored the user's own maps."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        config_store.create_transaction_rule(
            db_path, ledger="main", source="", field="category",
            match_kind="iexact", match_value="Meals", action="posting_account",
            target="Expenses:Business:Meals", priority=10, enabled=True,
            origin="user", note="",
        )
        result = import_csv(
            ledger_path=ledger, file_path=self._csv(tmp_path),
            account="Assets:Fallback", db_path=db_path, ledger_name="main",
        )
        assert result["status"] == "ok"
        assert "Expenses:Business:Meals" in ledger.read_text()

    def test_without_a_db_path_the_shipped_constant_still_applies(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        result = import_csv(
            ledger_path=ledger, file_path=self._csv(tmp_path),
            account="Assets:Fallback",
        )
        assert result["status"] == "ok"
        assert "Expenses:Uncategorized:Meals" in ledger.read_text()
