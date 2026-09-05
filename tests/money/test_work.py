"""Tests for money.work module."""

import time
from datetime import date

import pytest

from istota.money.work import (
    WorkFileQuarantined,
    WorkStoreLocked,
    _work_dir,
    _work_lock,
    add_work_entry,
    assign_invoice_number,
    assign_invoice_number_by_uids,
    backfill_work_ids,
    clear_invoice_payment,
    entry_etag,
    get_entries_for_invoice,
    get_invoice_numbers,
    get_uninvoiced_entries,
    invoice_issue_date,
    list_work_entries,
    load_work_entries,
    record_invoice_payment,
    remove_work_entry,
    remove_work_entry_by_uid,
    update_work_entry,
    update_work_entry_by_uid,
    void_invoice,
)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def _write_raw_year(data_dir, year: int, lines: list[str]) -> None:
    """Write a year file by hand, bypassing the serializer.

    Used to simulate a legacy (pre-uid) store or a hand-edited file.
    """
    work_dir = data_dir / "invoices" / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    body = "[[entries]]\n" + "\n".join(lines) + "\n"
    (work_dir / f"{year}.toml").write_text(body)


class TestLoadAndAdd:
    def test_empty(self, data_dir):
        entries = load_work_entries(data_dir)
        assert entries == []

    def test_add_and_load(self, data_dir):
        idx = add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8, description="Coding")
        assert idx == 1
        entries = load_work_entries(data_dir)
        assert len(entries) == 1
        e = entries[0]
        assert e.id == 1
        assert e.date == date(2026, 3, 1)
        assert e.client == "acme"
        assert e.service == "dev"
        assert e.qty == 8
        assert e.description == "Coding"
        assert e.invoice == ""
        assert e.paid_date is None

    def test_add_multiple_sorted_by_date(self, data_dir):
        add_work_entry(data_dir, "2026-03-15", "acme", "dev", qty=4)
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        entries = load_work_entries(data_dir)
        assert len(entries) == 2
        assert entries[0].date == date(2026, 3, 1)
        assert entries[1].date == date(2026, 3, 15)
        assert entries[0].id == 1
        assert entries[1].id == 2

    def test_year_partitioning(self, data_dir):
        add_work_entry(data_dir, "2025-12-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-01-15", "acme", "dev", qty=4)
        work_dir = data_dir / "invoices" / "work"
        assert (work_dir / "2025.toml").exists()
        assert (work_dir / "2026.toml").exists()

    def test_add_with_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8, invoice="INV-000001")
        entries = load_work_entries(data_dir)
        assert entries[0].invoice == "INV-000001"

    def test_add_all_fields(self, data_dir):
        add_work_entry(
            data_dir, "2026-03-01", "acme", "dev",
            qty=2.5, amount=375.0, discount=50, description="Work",
            entity="llc", invoice="INV-001",
        )
        e = load_work_entries(data_dir)[0]
        assert e.qty == 2.5
        assert e.amount == 375.0
        assert e.discount == 50
        assert e.description == "Work"
        assert e.entity == "llc"
        assert e.invoice == "INV-001"


class TestListFilters:
    def test_list_all(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "beta", "dev", qty=4)
        assert len(list_work_entries(data_dir)) == 2

    def test_list_by_client(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "beta", "dev", qty=4)
        entries = list_work_entries(data_dir, client="acme")
        assert len(entries) == 1
        assert entries[0].client == "acme"

    def test_list_by_period(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-04-01", "acme", "dev", qty=4)
        entries = list_work_entries(data_dir, period="2026-03")
        assert len(entries) == 1

    def test_list_invoiced_filter(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert len(list_work_entries(data_dir, invoiced=False)) == 1
        assert len(list_work_entries(data_dir, invoiced=True)) == 1


class TestUpdate:
    def test_update(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(data_dir, 1, qty=10, description="Updated") is True
        e = load_work_entries(data_dir)[0]
        assert e.qty == 10
        assert e.description == "Updated"

    def test_update_nonexistent(self, data_dir):
        assert update_work_entry(data_dir, 99, qty=10) is False

    def test_update_no_fields(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(data_dir, 1) is False

    def test_update_invoiced_blocked(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert update_work_entry(data_dir, 1, qty=10) is False

    def test_update_date_string(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        update_work_entry(data_dir, 1, date="2026-04-01")
        e = load_work_entries(data_dir)[0]
        assert e.date == date(2026, 4, 1)


class TestRemove:
    def test_remove_uninvoiced(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert remove_work_entry(data_dir, 1) is True
        assert load_work_entries(data_dir) == []

    def test_remove_invoiced_blocked(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert remove_work_entry(data_dir, 1) is False

    def test_remove_nonexistent(self, data_dir):
        assert remove_work_entry(data_dir, 99) is False

    def test_remove_cleans_empty_year_file(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        year_file = data_dir / "invoices" / "work" / "2026.toml"
        assert year_file.exists()
        remove_work_entry(data_dir, 1)
        assert not year_file.exists()


class TestUninvoiced:
    def test_get_uninvoiced(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-15", "acme", "dev", qty=4)
        add_work_entry(data_dir, "2026-04-01", "beta", "dev", qty=6)
        assign_invoice_number(data_dir, [1], "INV-000001")
        entries = get_uninvoiced_entries(data_dir)
        assert len(entries) == 2

    def test_get_uninvoiced_with_period(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-04-01", "acme", "dev", qty=4)
        entries = get_uninvoiced_entries(data_dir, period="2026-03")
        assert len(entries) == 1
        assert entries[0].date == date(2026, 3, 1)

    def test_get_uninvoiced_with_client(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "beta", "dev", qty=4)
        entries = get_uninvoiced_entries(data_dir, client="beta")
        assert len(entries) == 1
        assert entries[0].client == "beta"


class TestInvoiceAssignment:
    def test_assign_and_list(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        count = assign_invoice_number(data_dir, [1, 2], "INV-000001")
        assert count == 2
        entries = get_entries_for_invoice(data_dir, "INV-000001")
        assert len(entries) == 2
        assert all(e.invoice == "INV-000001" for e in entries)

    def test_assign_skips_already_invoiced(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        count = assign_invoice_number(data_dir, [1], "INV-000002")
        assert count == 0

    def test_get_invoice_numbers(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assign_invoice_number(data_dir, [2], "INV-000002")
        numbers = get_invoice_numbers(data_dir)
        assert numbers == ["INV-000001", "INV-000002"]

    def test_record_payment(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1, 2], "INV-000001")
        count = record_invoice_payment(data_dir, "INV-000001", "2026-04-15")
        assert count == 2
        entries = get_entries_for_invoice(data_dir, "INV-000001")
        assert all(e.paid_date == date(2026, 4, 15) for e in entries)

    def test_payment_idempotent(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")
        count = record_invoice_payment(data_dir, "INV-000001", "2026-05-01")
        assert count == 0


class TestClearInvoicePayment:
    def test_clear_payment_keeps_invoice_number(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1, 2], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")

        count = clear_invoice_payment(data_dir, "INV-000001")
        assert count == 2

        # paid_date cleared, but the invoice number stays put
        entries = get_entries_for_invoice(data_dir, "INV-000001")
        assert len(entries) == 2
        assert all(e.paid_date is None for e in entries)
        assert all(e.invoice == "INV-000001" for e in entries)

    def test_clear_payment_when_already_unpaid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        count = clear_invoice_payment(data_dir, "INV-000001")
        assert count == 0
        # Still invoiced, just never paid.
        assert get_entries_for_invoice(data_dir, "INV-000001")[0].invoice == "INV-000001"

    def test_clear_payment_nonexistent_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        count = clear_invoice_payment(data_dir, "INV-999999")
        assert count == 0

    def test_clear_payment_does_not_affect_other_invoices(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assign_invoice_number(data_dir, [2], "INV-000002")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")
        record_invoice_payment(data_dir, "INV-000002", "2026-04-16")

        clear_invoice_payment(data_dir, "INV-000001")

        entries = load_work_entries(data_dir)
        assert entries[0].paid_date is None
        assert entries[1].paid_date == date(2026, 4, 16)


class TestVoidInvoice:
    def test_void_clears_invoice_and_paid_date(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1, 2], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")

        count = void_invoice(data_dir, "INV-000001")
        assert count == 2

        # Entries should now be uninvoiced and unpaid
        entries = load_work_entries(data_dir)
        assert all(e.invoice == "" for e in entries)
        assert all(e.paid_date is None for e in entries)

    def test_void_unpaid_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")

        count = void_invoice(data_dir, "INV-000001")
        assert count == 1
        entries = load_work_entries(data_dir)
        assert entries[0].invoice == ""

    def test_void_nonexistent_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        count = void_invoice(data_dir, "INV-999999")
        assert count == 0

    def test_void_does_not_affect_other_invoices(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assign_invoice_number(data_dir, [2], "INV-000002")

        void_invoice(data_dir, "INV-000001")

        entries = load_work_entries(data_dir)
        assert entries[0].invoice == ""
        assert entries[1].invoice == "INV-000002"

    def test_void_entries_become_reinvoiceable(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")

        void_invoice(data_dir, "INV-000001")

        # Should now appear as uninvoiced
        uninvoiced = get_uninvoiced_entries(data_dir)
        assert len(uninvoiced) == 1

        # Should be assignable to a new invoice
        count = assign_invoice_number(data_dir, [1], "INV-000002")
        assert count == 1
        entries = get_entries_for_invoice(data_dir, "INV-000002")
        assert len(entries) == 1

    def test_void_removes_from_invoice_numbers_list(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")

        assert "INV-000001" in get_invoice_numbers(data_dir)
        void_invoice(data_dir, "INV-000001")
        assert "INV-000001" not in get_invoice_numbers(data_dir)


class TestConcurrencySafety:
    def test_work_lock_is_exclusive(self, data_dir):
        # flock is per-open-file-description and mutually exclusive across
        # fds even within one process, so a nested non-blocking acquire times
        # out — proving two writers can't interleave.
        with _work_lock(data_dir):
            with pytest.raises(WorkStoreLocked):
                with _work_lock(data_dir, timeout_seconds=0.2):
                    pass

    def test_lock_released_after_context(self, data_dir):
        with _work_lock(data_dir):
            pass
        # Re-acquire immediately; should not raise.
        with _work_lock(data_dir, timeout_seconds=0.2):
            pass

    def test_save_leaves_no_temp_files(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")
        wd = _work_dir(data_dir)
        # Every entry against an allowlist, not a `.tmp` filter: the staging
        # name is minted by `atomic_write` and carries no fixed suffix, so a
        # suffix filter passes whether or not one was left behind.
        assert sorted(p.name for p in wd.iterdir()) == [".work.lock", "2026.toml"]

    def test_lock_file_not_parsed_as_year(self, data_dir):
        # The .work.lock anchor lives in the work dir; it must never be
        # mistaken for a {year}.toml data file.
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        with _work_lock(data_dir):
            pass
        entries = load_work_entries(data_dir)
        assert len(entries) == 1


class TestClientCaseNormalization:
    def test_add_normalizes_client_to_lowercase(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "Globex", "dev", qty=8)
        entries = load_work_entries(data_dir)
        assert entries[0].client == "globex"

    def test_add_mixed_case_normalized(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "AcMe", "dev", qty=8)
        entries = load_work_entries(data_dir)
        assert entries[0].client == "acme"

    def test_list_filter_case_insensitive(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        entries = list_work_entries(data_dir, client="ACME")
        assert len(entries) == 1
        assert entries[0].client == "acme"

    def test_uninvoiced_filter_case_insensitive(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        entries = get_uninvoiced_entries(data_dir, client="ACME")
        assert len(entries) == 1

    def test_update_normalizes_client(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        update_work_entry(data_dir, 1, client="BETA")
        entries = load_work_entries(data_dir)
        assert entries[0].client == "beta"


class TestFileFormat:
    def test_optional_fields_omitted(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        content = (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert "discount" not in content
        assert "description" not in content
        assert "entity" not in content
        assert "invoice" not in content
        assert "paid_date" not in content
        assert "amount" not in content

    def test_roundtrip_all_fields(self, data_dir):
        add_work_entry(
            data_dir, "2026-03-01", "acme", "dev",
            qty=2.5, discount=50, description="Test work",
            entity="llc", invoice="INV-001",
        )
        entries = load_work_entries(data_dir)
        assert len(entries) == 1
        e = entries[0]
        assert e.qty == 2.5
        assert e.discount == 50
        assert e.description == "Test work"
        assert e.entity == "llc"
        assert e.invoice == "INV-001"

    def test_whole_numbers_no_decimal(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        content = (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert "qty = 8\n" in content
        assert "8.0" not in content


class TestStableUids:
    def test_add_stamps_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        assert e.uid
        assert len(e.uid) == 32

    def test_uid_persisted_to_toml(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        text = (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert "uid = " in text

    def test_uids_are_distinct(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        uids = {e.uid for e in load_work_entries(data_dir)}
        assert len(uids) == 2

    def test_uid_survives_unrelated_mutation(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-05", "acme", "dev", qty=4)
        before = [e.uid for e in load_work_entries(data_dir)]
        update_work_entry(data_dir, 2, qty=6)
        after = [e.uid for e in load_work_entries(data_dir)]
        assert before == after

    def test_uid_survives_invoice_assignment(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert load_work_entries(data_dir)[0].uid == uid

    def test_load_does_not_stamp_uids(self, data_dir):
        """Reading is never a write — an un-backfilled file stays un-backfilled."""
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        entries = load_work_entries(data_dir)
        assert entries[0].uid == ""
        assert "uid" not in (data_dir / "invoices" / "work" / "2026.toml").read_text()


class TestBackfillWorkIds:
    def test_backfill_stamps_missing_uids(self, data_dir):
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        assert backfill_work_ids(data_dir) == 1
        e = load_work_entries(data_dir)[0]
        assert e.uid

    def test_backfill_is_idempotent(self, data_dir):
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        backfill_work_ids(data_dir)
        uid = load_work_entries(data_dir)[0].uid
        assert backfill_work_ids(data_dir) == 0
        assert load_work_entries(data_dir)[0].uid == uid

    def test_backfill_empty_store(self, data_dir):
        assert backfill_work_ids(data_dir) == 0

    def test_backfill_stamps_invoiced_entries_too(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'invoice = "INV-000001"',
        ])
        assert backfill_work_ids(data_dir) == 1
        assert load_work_entries(data_dir)[0].uid


class TestUidAddressedMutations:
    def test_update_by_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        result = update_work_entry_by_uid(data_dir, uid, qty=10)
        assert result.ok
        assert result.status == "ok"
        assert load_work_entries(data_dir)[0].qty == 10

    def test_update_by_uid_hits_right_entry_after_index_shift(self, data_dir):
        """The failure the uid exists to prevent: an insert before the target."""
        add_work_entry(data_dir, "2026-03-10", "acme", "dev", qty=8, description="target")
        uid = load_work_entries(data_dir)[0].uid
        # Something else inserts a backdated entry — the target is now #2.
        add_work_entry(data_dir, "2026-03-01", "beta", "dev", qty=1, description="intruder")

        result = update_work_entry_by_uid(data_dir, uid, qty=99)
        assert result.ok

        entries = load_work_entries(data_dir)
        by_desc = {e.description: e for e in entries}
        assert by_desc["target"].qty == 99
        assert by_desc["intruder"].qty == 1

    def test_update_by_uid_not_found(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        result = update_work_entry_by_uid(data_dir, "deadbeef", qty=10)
        assert not result.ok
        assert result.status == "not_found"

    def test_update_by_uid_invoiced_refused(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assign_invoice_number(data_dir, [1], "INV-000001")
        result = update_work_entry_by_uid(data_dir, uid, qty=10)
        assert result.status == "invoiced"
        assert load_work_entries(data_dir)[0].qty == 8

    def test_update_by_uid_no_fields(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assert update_work_entry_by_uid(data_dir, uid).status == "no_fields"

    def test_update_by_uid_ignores_empty_uid(self, data_dir):
        """An un-backfilled entry (uid == '') must not be addressable by ''."""
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        assert update_work_entry_by_uid(data_dir, "", qty=10).status == "not_found"

    def test_update_by_uid_cannot_rewrite_identity_fields(self, data_dir):
        """``uid`` is structurally unsettable (it's the positional arg);
        ``id`` and ``extra`` are filtered out."""
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        update_work_entry_by_uid(data_dir, uid, id=99, extra={"x": 1}, qty=9)
        e = load_work_entries(data_dir)[0]
        assert e.uid == uid
        assert e.id == 1
        assert e.extra == {}
        assert e.qty == 9

    def test_update_by_uid_coerces_date_and_client(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        update_work_entry_by_uid(data_dir, uid, date="2026-04-02", client="BETA")
        e = load_work_entries(data_dir)[0]
        assert e.date == date(2026, 4, 2)
        assert e.client == "beta"

    def test_update_by_uid_returns_fresh_entry(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        result = update_work_entry_by_uid(data_dir, uid, qty=3)
        assert result.entry is not None
        assert result.entry.qty == 3
        assert result.entry.id == 1

    def test_remove_by_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assert remove_work_entry_by_uid(data_dir, uid).ok
        assert load_work_entries(data_dir) == []

    def test_remove_by_uid_hits_right_entry_after_index_shift(self, data_dir):
        add_work_entry(data_dir, "2026-03-10", "acme", "dev", qty=8, description="target")
        uid = load_work_entries(data_dir)[0].uid
        add_work_entry(data_dir, "2026-03-01", "beta", "dev", qty=1, description="intruder")

        assert remove_work_entry_by_uid(data_dir, uid).ok

        remaining = load_work_entries(data_dir)
        assert len(remaining) == 1
        assert remaining[0].description == "intruder"

    def test_remove_by_uid_not_found(self, data_dir):
        assert remove_work_entry_by_uid(data_dir, "nope").status == "not_found"

    def test_remove_by_uid_invoiced_refused(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert remove_work_entry_by_uid(data_dir, uid).status == "invoiced"
        assert len(load_work_entries(data_dir)) == 1


class TestEntryEtag:
    def test_etag_is_stable_for_unchanged_entry(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        first = entry_etag(load_work_entries(data_dir)[0])
        second = entry_etag(load_work_entries(data_dir)[0])
        assert first == second
        assert first

    def test_etag_changes_when_entry_changes(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        before = entry_etag(load_work_entries(data_dir)[0])
        update_work_entry_by_uid(data_dir, uid, qty=9)
        assert entry_etag(load_work_entries(data_dir)[0]) != before

    def test_etag_ignores_display_index(self, data_dir):
        """Two entries differing only in position must not share an etag,
        but the same entry at a different index keeps its etag."""
        add_work_entry(data_dir, "2026-03-10", "acme", "dev", qty=8)
        before = entry_etag(load_work_entries(data_dir)[0])
        add_work_entry(data_dir, "2026-03-01", "beta", "dev", qty=1)
        moved = [e for e in load_work_entries(data_dir) if e.client == "acme"][0]
        assert moved.id == 2
        assert entry_etag(moved) == before

    def test_update_with_matching_etag_succeeds(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        result = update_work_entry_by_uid(data_dir, e.uid, expect_etag=entry_etag(e), qty=10)
        assert result.ok

    def test_update_with_stale_etag_conflicts(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        stale = entry_etag(e)
        # Someone else edits the same entry first.
        update_work_entry_by_uid(data_dir, e.uid, qty=99)

        result = update_work_entry_by_uid(data_dir, e.uid, expect_etag=stale, qty=10)
        assert result.status == "conflict"
        assert result.entry is not None
        assert result.entry.qty == 99
        assert load_work_entries(data_dir)[0].qty == 99

    def test_remove_with_stale_etag_conflicts(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        stale = entry_etag(e)
        update_work_entry_by_uid(data_dir, e.uid, qty=99)

        result = remove_work_entry_by_uid(data_dir, e.uid, expect_etag=stale)
        assert result.status == "conflict"
        assert len(load_work_entries(data_dir)) == 1

    def test_remove_with_matching_etag_succeeds(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        assert remove_work_entry_by_uid(data_dir, e.uid, expect_etag=entry_etag(e)).ok

    def test_no_etag_skips_the_check(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assert update_work_entry_by_uid(data_dir, uid, qty=10).ok


class TestUnknownKeyRoundTrip:
    def test_unknown_key_survives_load_save(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'qty = 8', 'billing_note = "holiday rate"',
        ])
        # Mutating a *different* entry rewrites the whole year file.
        add_work_entry(data_dir, "2026-03-05", "beta", "dev", qty=1)

        text = (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert 'billing_note = "holiday rate"' in text

    def test_unknown_key_exposed_on_the_entry(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"', 'reviewed = true',
        ])
        assert load_work_entries(data_dir)[0].extra == {"reviewed": True}

    def test_unknown_key_types_round_trip(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'reviewed = true', 'ticket = 42', 'weight = 1.5',
            'tags = ["a", "b"]', 'approved_on = 2026-04-01',
        ])
        backfill_work_ids(data_dir)
        e = load_work_entries(data_dir)[0]
        assert e.extra == {
            "reviewed": True,
            "ticket": 42,
            "weight": 1.5,
            "tags": ["a", "b"],
            "approved_on": date(2026, 4, 1),
        }

    def test_unserializable_extra_is_dropped_not_fatal(self, data_dir):
        """A nested table can't be written back by the hand-rolled serializer.

        It's dropped rather than crashing the write — the alternative is
        a save path that can be poisoned by an arbitrary hand edit.
        """
        work_dir = data_dir / "invoices" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "2026.toml").write_text(
            "[[entries]]\n"
            "date = 2026-03-01\n"
            'client = "acme"\n'
            'service = "dev"\n'
            "\n[entries.meta]\n"
            'source = "import"\n'
        )
        assert backfill_work_ids(data_dir) == 1
        e = load_work_entries(data_dir)[0]
        assert e.uid
        assert "meta" not in (work_dir / "2026.toml").read_text()

    def test_etag_covers_extra_keys(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
        ])
        plain = entry_etag(load_work_entries(data_dir)[0])
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'note = "x"',
        ])
        assert entry_etag(load_work_entries(data_dir)[0]) != plain


class TestSerializerControlCharacters:
    """A control character in a string field must never poison the year file.

    The web routes put arbitrary JSON strings on the write path, and a bare
    CR/tab/NUL inside a TOML basic string makes the whole year unreadable —
    every subsequent load, including invoicing's, raises TOMLDecodeError.
    """

    def test_carriage_return_round_trips(self, data_dir):
        add_work_entry(
            data_dir, "2026-03-01", "acme", "dev", qty=8,
            description="line one\r\nline two",
        )
        entries = load_work_entries(data_dir)
        assert entries[0].description == "line one\r\nline two"

    @pytest.mark.parametrize("raw", ["a\rb", "a\tb", "a\x00b", "a\x1fb", "a\x7fb", "a\bb"])
    def test_control_characters_round_trip(self, data_dir, raw):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1, description=raw)
        assert load_work_entries(data_dir)[0].description == raw

    def test_control_character_in_extra_round_trips(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        entries = load_work_entries(data_dir)
        entries[0].extra["note"] = "a\rb"
        from istota.money.work import _save_entries
        _save_entries(data_dir, entries)
        assert load_work_entries(data_dir)[0].extra["note"] == "a\rb"

    def test_unwritable_year_file_is_never_persisted(self, data_dir, monkeypatch):
        """A serializer bug must fail the write, not leave an unparseable file."""
        import istota.money.work as work_mod

        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        good = (data_dir / "invoices" / "work" / "2026.toml").read_text()

        monkeypatch.setattr(work_mod, "_serialize_entry", lambda e: "[[entries]]\nnot toml =")
        with pytest.raises(ValueError):
            add_work_entry(data_dir, "2026-03-02", "beta", "dev", qty=1)

        assert (data_dir / "invoices" / "work" / "2026.toml").read_text() == good


class TestExtraKeyQuoting:
    """A hand-edited quoted key must survive the next write.

    ``extra`` keys were re-emitted bare, so ``"my key" = 1`` came back as
    ``my key = 1`` and the file failed to parse on the following read.
    """

    def test_quoted_extra_key_round_trips(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            '"my key" = "hello"',
        ])
        add_work_entry(data_dir, "2026-03-05", "beta", "dev", qty=1)

        entries = load_work_entries(data_dir)
        acme = [e for e in entries if e.client == "acme"][0]
        assert acme.extra["my key"] == "hello"

    def test_dotted_extra_key_does_not_nest_on_write(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            '"a.b" = 1',
        ])
        add_work_entry(data_dir, "2026-03-05", "beta", "dev", qty=1)
        acme = [e for e in load_work_entries(data_dir) if e.client == "acme"][0]
        assert acme.extra["a.b"] == 1


class TestAssignInvoiceNumberByUids:
    def test_stamps_by_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assert assign_invoice_number_by_uids(data_dir, [uid], "INV-000001") == 1
        assert load_work_entries(data_dir)[0].invoice == "INV-000001"

    def test_hits_right_entry_after_index_shift(self, data_dir):
        add_work_entry(data_dir, "2026-03-10", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        # Something inserts an earlier-dated entry: every display index shifts.
        add_work_entry(data_dir, "2026-03-01", "hooli", "dev", qty=1)

        assert assign_invoice_number_by_uids(data_dir, [uid], "INV-000001") == 1
        by_client = {e.client: e for e in load_work_entries(data_dir)}
        assert by_client["acme"].invoice == "INV-000001"
        assert by_client["hooli"].invoice == ""

    def test_skips_already_invoiced(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001")
        assert assign_invoice_number_by_uids(data_dir, [uid], "INV-000002") == 0
        assert load_work_entries(data_dir)[0].invoice == "INV-000001"

    def test_unknown_uid_ignored(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert assign_invoice_number_by_uids(data_dir, ["nope"], "INV-000001") == 0
        assert load_work_entries(data_dir)[0].invoice == ""

    def test_empty_uid_is_not_addressable(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"', 'qty = 8',
        ])
        assert assign_invoice_number_by_uids(data_dir, [""], "INV-000001") == 0

    def test_empty_list(self, data_dir):
        assert assign_invoice_number_by_uids(data_dir, [], "INV-000001") == 0


class TestInvoiceDate:
    """ISSUE-256: an invoice records when it was issued.

    Before this, the only dates on a work entry were the work date and the
    payment date, so a reader asking "when did this invoice go out?" had to
    guess from the work billed on it.
    """

    def _invoiced(self, data_dir, when="2026-03-01"):
        add_work_entry(data_dir, when, "acme", "dev", qty=8)
        return load_work_entries(data_dir)[0].uid

    def test_stamped_by_uid_addressed_assignment(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(
            data_dir, [uid], "INV-000001", date(2026, 5, 1),
        )
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)

    def test_stamped_by_index_addressed_assignment(self, data_dir):
        self._invoiced(data_dir)
        assign_invoice_number(data_dir, [1], "INV-000001", date(2026, 5, 1))
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)

    def test_defaults_to_today_when_the_caller_names_no_date(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001")
        assert load_work_entries(data_dir)[0].invoice_date == date.today()

    def test_accepts_an_iso_string(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001", "2026-05-01")
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)

    def test_survives_a_write_by_an_unrelated_caller(self, data_dir):
        """The field has to round-trip the serializer, not just live in memory."""
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(
            data_dir, [uid], "INV-000001", date(2026, 5, 1),
        )
        # Any other write rewrites the whole year file from the serializer.
        add_work_entry(data_dir, "2026-04-01", "hooli", "dev", qty=1)
        by_client = {e.client: e for e in load_work_entries(data_dir)}
        assert by_client["acme"].invoice_date == date(2026, 5, 1)

    def test_void_clears_it_with_the_number(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(
            data_dir, [uid], "INV-000001", date(2026, 5, 1),
        )
        void_invoice(data_dir, "INV-000001")
        entry = load_work_entries(data_dir)[0]
        assert entry.invoice == ""
        assert entry.invoice_date is None

    def test_payment_does_not_touch_it(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(
            data_dir, [uid], "INV-000001", date(2026, 5, 1),
        )
        record_invoice_payment(data_dir, "INV-000001", "2026-06-01")
        entry = load_work_entries(data_dir)[0]
        assert entry.invoice_date == date(2026, 5, 1)
        assert entry.paid_date == date(2026, 6, 1)

    def test_an_uninvoiced_entry_has_none(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert load_work_entries(data_dir)[0].invoice_date is None

    def test_a_date_without_a_number_is_ignored(self, data_dir):
        """It is a property of the invoice, so it cannot exist without one."""
        add_work_entry(
            data_dir, "2026-03-01", "acme", "dev", qty=8,
            invoice_date=date(2026, 5, 1),
        )
        assert load_work_entries(data_dir)[0].invoice_date is None

    def test_pre_assigned_number_carries_the_date(self, data_dir):
        """The `invoice create` path, which never goes through an assign call."""
        add_work_entry(
            data_dir, "2026-05-01", "acme", "dev", qty=8,
            invoice="INV-000001", invoice_date=date(2026, 5, 1),
        )
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)

    def test_a_legacy_file_loads_with_no_date(self, data_dir):
        """Nothing can reconstruct one, so it stays absent rather than guessed."""
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'qty = 8', 'invoice = "INV-000001"',
        ])
        assert load_work_entries(data_dir)[0].invoice_date is None

    def test_an_unreadable_stored_date_degrades_to_absent(self, data_dir):
        """A hand-edited file must not take down every reader of the store."""
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'qty = 8', 'invoice = "INV-000001"', 'invoice_date = "not a date"',
        ])
        assert load_work_entries(data_dir)[0].invoice_date is None

    def test_it_is_a_known_key_not_an_extra(self, data_dir):
        """Left in `extra` it would round-trip but no reader would see it."""
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'qty = 8', 'invoice = "INV-000001"', 'invoice_date = 2026-05-01',
        ])
        entry = load_work_entries(data_dir)[0]
        assert entry.invoice_date == date(2026, 5, 1)
        assert "invoice_date" not in entry.extra


class TestInvoiceDateOnHandAssignment:
    """`work update --invoice` stamps a number without going through an assign.

    A number written there without a date would read as a pre-field invoice
    and silently fall back to the loose latest-work bound.
    """

    def test_hand_assigned_number_gets_a_date(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(data_dir, 1, invoice="INV-000001")
        assert load_work_entries(data_dir)[0].invoice_date == date.today()

    def test_by_uid_too(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        result = update_work_entry_by_uid(data_dir, uid, invoice="INV-000001")
        assert result.ok
        assert load_work_entries(data_dir)[0].invoice_date == date.today()

    def test_an_explicit_date_wins_over_today(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(
            data_dir, 1, invoice="INV-000001", invoice_date="2026-05-01",
        )
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)

    def test_an_empty_number_leaves_the_entry_uninvoiced_and_undated(self, data_dir):
        """The two fields move together, so neither can be set on its own."""
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(data_dir, 1, invoice="")
        entry = load_work_entries(data_dir)[0]
        assert entry.invoice == ""
        assert entry.invoice_date is None

    def test_an_already_invoiced_entry_is_refused_outright(self, data_dir):
        """Which is why the clearing branch in `_sync_invoice_date` is unreachable."""
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        update_work_entry(data_dir, 1, invoice="INV-000001")
        assert not update_work_entry(data_dir, 1, invoice="")
        assert load_work_entries(data_dir)[0].invoice == "INV-000001"


class TestInvoiceIssueDate:
    """The one rule every reader of an invoice's date goes through."""

    def _entry(self, work_day, issue_day=None):
        from istota.money.core.models import WorkEntry

        return WorkEntry(
            date=date(2026, 1, work_day), client="acme", service="dev",
            invoice="INV-000001",
            invoice_date=date(2026, 5, issue_day) if issue_day else None,
        )

    def test_uses_the_stored_date_when_there_is_one(self):
        entries = [self._entry(5, 1), self._entry(20, 1)]
        assert invoice_issue_date(entries) == date(2026, 5, 1)

    def test_falls_back_to_the_latest_work_not_the_earliest(self):
        entries = [self._entry(5), self._entry(20)]
        assert invoice_issue_date(entries) == date(2026, 1, 20)

    def test_a_partially_stamped_invoice_uses_the_stored_date(self):
        """A hand-edited file can leave one entry without a date."""
        entries = [self._entry(5), self._entry(20, 1)]
        assert invoice_issue_date(entries) == date(2026, 5, 1)

    def test_disagreeing_stored_dates_take_the_earliest(self):
        """A hand-edited file can disagree with itself.

        This is a "cannot have existed before this" bound, so the earliest is
        the reading that cannot reject a payment the invoice really caused.
        """
        entries = [self._entry(5, 1), self._entry(20, 9)]
        assert invoice_issue_date(entries) == date(2026, 5, 1)

    def test_no_entries(self):
        assert invoice_issue_date([]) is None


class TestLoaderCoercion:
    """A hand-edited year file must degrade one row, not the whole store.

    These files are deliberately hand-editable and the web UI turns any load
    failure into a 500 with no diagnostic, so the loader takes the plausible
    mistakes (a quoted date, a quoted number) rather than handing a string to
    code that will call ``.isoformat()`` on it three layers later.
    """

    def test_quoted_date_is_coerced(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = "2026-01-05"', 'client = "acme"', 'service = "dev"',
        ])
        assert load_work_entries(data_dir)[0].date == date(2026, 1, 5)

    def test_quoted_paid_date_is_coerced(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-01-05', 'client = "acme"', 'service = "dev"',
            'paid_date = "2026-02-01"',
        ])
        assert load_work_entries(data_dir)[0].paid_date == date(2026, 2, 1)

    def test_datetime_date_is_narrowed(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-01-05T09:30:00', 'client = "acme"', 'service = "dev"',
        ])
        loaded = load_work_entries(data_dir)[0].date
        assert loaded == date(2026, 1, 5)
        assert loaded.isoformat() == "2026-01-05"

    def test_quoted_numbers_are_coerced(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-01-05', 'client = "acme"', 'service = "dev"',
            'qty = "8"', 'amount = "500.50"', 'discount = "10"',
        ])
        e = load_work_entries(data_dir)[0]
        assert (e.qty, e.amount, e.discount) == (8.0, 500.5, 10.0)

    def test_unusable_number_is_dropped_but_the_entry_survives(self, data_dir):
        # A vanished billable entry is worse than a visible $0 one — the
        # zero is something a human notices on the Work tab.
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-01-05', 'client = "acme"', 'service = "dev"',
            'qty = "eight"',
        ])
        entries = load_work_entries(data_dir)
        assert len(entries) == 1
        assert entries[0].qty is None

    def test_unusable_date_skips_only_that_entry(self, data_dir):
        work_dir = data_dir / "invoices" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "2026.toml").write_text(
            "[[entries]]\n"
            'uid = "bad"\ndate = "not-a-date"\nclient = "acme"\nservice = "dev"\n'
            "\n[[entries]]\n"
            'uid = "good"\ndate = 2026-01-05\nclient = "acme"\nservice = "dev"\nqty = 2\n'
        )
        entries = load_work_entries(data_dir)
        assert [e.uid for e in entries] == ["good"]

    def test_missing_required_key_skips_only_that_entry(self, data_dir):
        work_dir = data_dir / "invoices" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "2026.toml").write_text(
            "[[entries]]\n"
            'uid = "bad"\ndate = 2026-01-04\nservice = "dev"\n'
            "\n[[entries]]\n"
            'uid = "good"\ndate = 2026-01-05\nclient = "acme"\nservice = "dev"\n'
        )
        assert [e.uid for e in load_work_entries(data_dir)] == ["good"]

    def _quarantined_year(self, data_dir):
        work_dir = data_dir / "invoices" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "2026.toml").write_text(
            "[[entries]]\n"
            'uid = "bad"\ndate = "not-a-date"\nclient = "acme"\nservice = "dev"\n'
            "\n[[entries]]\n"
            'uid = "good"\ndate = 2026-01-05\nclient = "acme"\nservice = "dev"\nqty = 2\n'
        )
        return work_dir / "2026.toml"

    def test_a_write_to_a_year_with_a_skipped_entry_is_refused(self, data_dir):
        """Dropping a row on read must never delete it on the next write.

        The whole year is rewritten from the loaded list, so a row we couldn't
        model would vanish. Reads degrade; writes to that year fail loudly
        until a human fixes the row.
        """
        path = self._quarantined_year(data_dir)
        before = path.read_text()

        with pytest.raises(WorkFileQuarantined):
            add_work_entry(data_dir, "2026-01-06", "beta", "dev", qty=1)

        assert path.read_text() == before

    def test_a_year_with_a_skipped_entry_is_still_readable(self, data_dir):
        self._quarantined_year(data_dir)
        assert [e.uid for e in load_work_entries(data_dir)] == ["good"]

    def test_another_year_is_still_writable(self, data_dir):
        path = self._quarantined_year(data_dir)
        before = path.read_text()
        add_work_entry(data_dir, "2027-01-06", "beta", "dev", qty=1)
        assert path.read_text() == before
        assert any(e.client == "beta" for e in load_work_entries(data_dir))

    def test_a_year_with_only_a_skipped_entry_is_not_deleted(self, data_dir):
        work_dir = data_dir / "invoices" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        path = work_dir / "2026.toml"
        path.write_text(
            "[[entries]]\n"
            'uid = "bad"\ndate = "not-a-date"\nclient = "acme"\nservice = "dev"\n'
        )
        # The year loads as empty, and an empty year file is normally unlinked.
        with pytest.raises(WorkFileQuarantined):
            add_work_entry(data_dir, "2026-01-06", "beta", "dev", qty=1)
        assert path.exists()


class TestBackfillDoesNotLockWhenIdle:
    """A no-op backfill must not take the exclusive write lock.

    ``ensure_initialised`` runs it on every money web request and every skill
    invocation, so locking first meant a plain ``GET /work`` contended with
    ``invoice generate`` — and waited out a 10s timeout when it lost.
    """

    def test_no_op_backfill_does_not_wait_for_the_lock(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        with _work_lock(data_dir):
            started = time.monotonic()
            assert backfill_work_ids(data_dir) == 0
            assert time.monotonic() - started < 1.0

    def test_backfill_still_stamps_when_something_is_missing(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
        ])
        assert backfill_work_ids(data_dir) == 1
        assert load_work_entries(data_dir)[0].uid

    def test_backfill_reports_contention_when_it_has_work_to_do(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
        ])
        with _work_lock(data_dir):
            with pytest.raises(WorkStoreLocked):
                backfill_work_ids(data_dir, timeout_seconds=0.1)


class TestReadingDoesNotCreateTheStore:
    def test_load_does_not_create_the_work_dir(self, data_dir):
        assert load_work_entries(data_dir) == []
        assert not (data_dir / "invoices" / "work").exists()

    def test_no_op_backfill_does_not_create_the_work_dir(self, data_dir):
        assert backfill_work_ids(data_dir) == 0
        assert not (data_dir / "invoices" / "work").exists()

    def test_writing_still_creates_it(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        assert (data_dir / "invoices" / "work" / "2026.toml").exists()
