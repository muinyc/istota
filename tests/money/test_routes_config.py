"""Tests for the new /money/config/* CRUD + import/export routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.money import config_store
from istota.money.cli import UserContext
from istota.money.routes import get_user_config, require_auth, router


@pytest.fixture
def ctx(tmp_path: Path) -> UserContext:
    data_dir = tmp_path / "money"
    db_path = data_dir / "data" / "money.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(db_path)
    return UserContext(data_dir=data_dir, ledgers=[], db_path=db_path)


@pytest.fixture
def client(ctx: UserContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/istota/api/money")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_config] = lambda: ctx
    return TestClient(app)


# =============================================================================
# Invoicing — settings + collections
# =============================================================================


class TestInvoicingScalars:
    def test_get_defaults(self, client):
        resp = client.get("/istota/api/money/config/invoicing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["settings"]["currency"] == "USD"

    def test_put_updates(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/invoicing",
            json={"next_invoice_number": 42, "currency": "EUR"},
        )
        assert resp.status_code == 200
        cfg = config_store.load_invoicing(ctx.db_path)
        assert cfg.next_invoice_number == 42
        assert cfg.currency == "EUR"

    def test_put_rejects_unknown(self, client):
        resp = client.put(
            "/istota/api/money/config/invoicing",
            json={"unknown_key": 1},
        )
        assert resp.status_code == 400


class TestCompanies:
    def test_create_list_delete(self, ctx, client):
        resp = client.post(
            "/istota/api/money/config/companies",
            json={"key": "ochotona", "name": "Ochotona LLC"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "created"

        resp = client.get("/istota/api/money/config/companies")
        assert resp.json()["companies"][0]["key"] == "ochotona"

        resp = client.put(
            "/istota/api/money/config/companies/ochotona",
            json={"address": "1 St"},
        )
        assert resp.json()["state"] == "updated"

        resp = client.delete("/istota/api/money/config/companies/ochotona")
        assert resp.json()["removed"] is True


class TestClients:
    def test_create_update(self, client):
        resp = client.post(
            "/istota/api/money/config/clients",
            json={"key": "acme", "name": "Acme Corp"},
        )
        assert resp.json()["state"] == "created"
        resp = client.put(
            "/istota/api/money/config/clients/acme",
            json={"terms": "NET 15"},
        )
        body = resp.json()
        assert body["state"] == "updated"
        assert body["client"]["terms"] == "NET 15"


class TestServices:
    def test_create(self, client):
        resp = client.post(
            "/istota/api/money/config/services",
            json={"key": "consulting", "display_name": "Consulting", "rate": 150},
        )
        assert resp.json()["state"] == "created"


# =============================================================================
# Tax
# =============================================================================


class TestTaxScalars:
    def test_put_get(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/tax",
            json={"tax_year": 2026, "w2_income": 90000},
        )
        assert resp.status_code == 200
        body = client.get("/istota/api/money/config/tax").json()
        assert body["tax"]["w2_income"] == 90000


class TestTaxYears:
    """The year table carries the payroll scalars only.

    Brackets and standard deductions moved to `tax_schedules`, which has the
    filing-status dimension they actually need.
    """

    def test_upsert(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/tax/years/2026",
            json={"ss_wage_base": 184500, "ss_rate": 0.124},
        )
        assert resp.json()["state"] == "created"
        years = client.get("/istota/api/money/config/tax/years").json()["years"]
        assert years[0]["tax_year"] == 2026
        assert years[0]["ss_wage_base"] == 184500

    def test_unknown_field_rejected(self, client):
        resp = client.put(
            "/istota/api/money/config/tax/years/2026",
            json={"unknown": 1},
        )
        assert resp.status_code == 400

    def test_moved_bracket_field_rejected_with_a_pointer(self, client):
        resp = client.put(
            "/istota/api/money/config/tax/years/2026",
            json={"federal_standard_deduction": 30000},
        )
        assert resp.status_code == 400
        assert "schedules" in resp.json()["error"]


class TestTaxPatterns:
    def test_replace_all(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/tax/patterns",
            json={"se_income": ["Income:Side"], "se_expense": ["Expenses:Biz"]},
        )
        assert resp.status_code == 200
        body = client.get("/istota/api/money/config/tax/patterns").json()
        assert body["patterns"]["se_income"] == ["Income:Side"]
        assert body["patterns"]["se_expense"] == ["Expenses:Biz"]


# =============================================================================
# Monarch
# =============================================================================


class TestMonarchProfiles:
    def test_create_then_account_map(self, ctx, client):
        resp = client.post(
            "/istota/api/money/config/monarch/profiles",
            json={"name": "acme", "ledger": "acme"},
        )
        assert resp.json()["state"] == "created"

        resp = client.put(
            "/istota/api/money/config/monarch/account-map?profile=acme",
            json={"Acme Visa": "Liabilities:Visa"},
        )
        assert resp.status_code == 200
        body = client.get(
            "/istota/api/money/config/monarch/account-map?profile=acme",
        ).json()
        assert body["mapping"] == {"Acme Visa": "Liabilities:Visa"}

    def test_create_without_ledger_400(self, client):
        resp = client.post(
            "/istota/api/money/config/monarch/profiles",
            json={"name": "x"},
        )
        assert resp.status_code == 400

    def test_global_scope(self, client):
        resp = client.put(
            "/istota/api/money/config/monarch/account-map?profile=global",
            json={"Bank": "Assets:Bank"},
        )
        assert resp.status_code == 200
        body = client.get(
            "/istota/api/money/config/monarch/account-map?profile=global",
        ).json()
        assert body["mapping"] == {"Bank": "Assets:Bank"}

    def test_rejects_unparseable_account(self, client):
        resp = client.put(
            "/istota/api/money/config/monarch/category-map?profile=global",
            json={
                "Internet Services (Reimbursed)":
                    "Expenses:Uncategorized:InternetServices(Reimbursed)",
            },
        )
        assert resp.status_code == 400
        body = client.get(
            "/istota/api/money/config/monarch/category-map?profile=global",
        ).json()
        assert body["mapping"] == {}


class TestMonarchTagFilters:
    def test_replace(self, ctx, client):
        client.post(
            "/istota/api/money/config/monarch/profiles",
            json={"name": "acme", "ledger": "acme"},
        )
        resp = client.put(
            "/istota/api/money/config/monarch/tag-filters?profile=acme",
            json={"include": ["Biz"], "exclude": ["Hide"]},
        )
        assert resp.status_code == 200
        body = client.get(
            "/istota/api/money/config/monarch/tag-filters?profile=acme",
        ).json()
        assert body["tags"]["include"] == ["Biz"]
        assert body["tags"]["exclude"] == ["Hide"]


# =============================================================================
# Import / export
# =============================================================================


class TestExport:
    def test_section_invoicing(self, client):
        client.post(
            "/istota/api/money/config/clients",
            json={"key": "acme", "name": "Acme"},
        )
        resp = client.get("/istota/api/money/config/export?section=invoicing")
        assert resp.status_code == 200
        assert "[clients.acme]" in resp.text
        assert "Acme" in resp.text

    def test_combined(self, client):
        client.post(
            "/istota/api/money/config/clients",
            json={"key": "acme", "name": "Acme"},
        )
        client.put(
            "/istota/api/money/config/tax", json={"tax_year": 2026},
        )
        resp = client.get("/istota/api/money/config/export")
        assert resp.status_code == 200
        # The combined dump nests both [invoicing.*] and [tax].
        assert "tax_year" in resp.text


class TestImport:
    def test_dry_run(self, ctx, client):
        toml_text = '[clients.foo]\nname = "Foo"\n'
        resp = client.post(
            "/istota/api/money/config/import?section=invoicing&dry_run=1",
            json={"text": toml_text},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["sections"][0]["section"] == "invoicing"
        # Database not touched.
        cfg = config_store.load_invoicing(ctx.db_path)
        assert "foo" not in cfg.clients

    def test_apply(self, ctx, client):
        resp = client.post(
            "/istota/api/money/config/import?section=invoicing",
            json={"text": '[clients.foo]\nname = "Foo"\n'},
        )
        assert resp.status_code == 200
        cfg = config_store.load_invoicing(ctx.db_path)
        assert "foo" in cfg.clients

    def test_unparseable(self, client):
        resp = client.post(
            "/istota/api/money/config/import?section=invoicing",
            json={"text": "this is not toml = "},
        )
        assert resp.status_code == 400


# =============================================================================
# Collection CRUD hardening (money-config-editing spec, Stage 2)
#
# The /config/* routes were written as a thin passthrough for a trusted CLI
# caller. These cover the four sharp edges that only matter once a browser
# form is driving them.
# =============================================================================


API = "/istota/api/money"


def _seed_business(client) -> None:
    client.post(f"{API}/config/companies", json={"key": "main", "name": "Main LLC"})
    client.post(f"{API}/config/clients", json={"key": "acme", "name": "Acme Corp"})
    client.post(
        f"{API}/config/services",
        json={"key": "dev", "display_name": "Development", "rate": 150},
    )


class TestCreateMeansCreate:
    def test_duplicate_client_conflicts(self, ctx, client):
        client.post(f"{API}/config/clients", json={"key": "acme", "name": "Acme Corp"})
        resp = client.post(
            f"{API}/config/clients", json={"key": "acme", "name": "Acme Holdings"},
        )
        assert resp.status_code == 409
        assert "acme" in resp.json()["error"]
        # The existing record is untouched — the whole point of the guard.
        assert config_store.load_invoicing(ctx.db_path).clients["acme"].name == "Acme Corp"

    def test_duplicate_company_conflicts(self, client):
        client.post(f"{API}/config/companies", json={"key": "main", "name": "Main"})
        resp = client.post(f"{API}/config/companies", json={"key": "main", "name": "Other"})
        assert resp.status_code == 409

    def test_duplicate_service_conflicts(self, client):
        client.post(f"{API}/config/services", json={"key": "dev", "rate": 150})
        resp = client.post(f"{API}/config/services", json={"key": "dev", "rate": 200})
        assert resp.status_code == 409

    def test_put_still_upserts(self, client):
        """`ensure`-style idempotence stays available to CLI/agent callers."""
        resp = client.put(f"{API}/config/clients/fresh", json={"name": "Fresh"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "created"

    def test_missing_key_rejected(self, client):
        assert client.post(f"{API}/config/clients", json={"name": "No key"}).status_code == 400

    def test_bad_key_rejected(self, client):
        resp = client.post(f"{API}/config/clients", json={"key": "has space", "name": "X"})
        assert resp.status_code == 400
        assert "key" in resp.json()["error"]


class TestFieldValidation:
    def test_unknown_key_named_in_error(self, client):
        resp = client.post(
            f"{API}/config/clients", json={"key": "acme", "nmae": "Acme"},
        )
        assert resp.status_code == 400
        assert "nmae" in resp.json()["error"]

    def test_unknown_key_on_put(self, client):
        client.post(f"{API}/config/clients", json={"key": "acme", "name": "Acme"})
        resp = client.put(f"{API}/config/clients/acme", json={"bogus": 1})
        assert resp.status_code == 400

    def test_store_rule_surfaces_as_400(self, client):
        # "hourly" is the plausible typo; the store rejects it and the route
        # passes the message through rather than 500ing.
        resp = client.post(
            f"{API}/config/services", json={"key": "dev", "type": "hourly"},
        )
        assert resp.status_code == 400
        assert "type" in resp.json()["error"]

    def test_non_numeric_rate_is_a_400_not_a_500(self, client):
        resp = client.post(f"{API}/config/services", json={"key": "dev", "rate": "abc"})
        assert resp.status_code == 400

    def test_wrong_json_type_rejected(self, client):
        resp = client.post(f"{API}/config/clients", json={"key": "acme", "name": 42})
        assert resp.status_code == 400
        resp = client.post(
            f"{API}/config/clients", json={"key": "acme", "schedule_day": True},
        )
        assert resp.status_code == 400

    def test_control_characters_rejected(self, client):
        resp = client.post(
            f"{API}/config/clients", json={"key": "acme", "name": "Acme\rCorp"},
        )
        assert resp.status_code == 400

    def test_newlines_allowed_in_prose_fields(self, client):
        """Address and payment instructions are genuinely multi-line."""
        resp = client.post(
            f"{API}/config/companies",
            json={"key": "main", "name": "Main", "address": "1 St\nCity",
                  "payment_instructions": "Wire to\nIBAN DE00"},
        )
        assert resp.status_code == 200
        assert resp.json()["company"]["address"] == "1 St\nCity"

    def test_empty_string_clears_an_optional_field(self, client):
        client.post(
            f"{API}/config/clients",
            json={"key": "acme", "name": "Acme", "entity": "main"},
        )
        resp = client.put(f"{API}/config/clients/acme", json={"entity": ""})
        assert resp.status_code == 200
        assert resp.json()["client"]["entity"] == ""

    def test_bundles_and_separate_round_trip(self, client):
        resp = client.post(
            f"{API}/config/clients",
            json={"key": "acme", "name": "Acme", "separate": ["dev"],
                  "bundles": [{"name": "Bundle", "services": ["dev"]}]},
        )
        assert resp.status_code == 200
        assert resp.json()["client"]["separate"] == ["dev"]
        # Omitting them preserves what's stored — the reason the forms can
        # leave them out without a nested-list editor.
        client.put(f"{API}/config/clients/acme", json={"name": "Acme Corp"})
        body = client.get(f"{API}/config/clients").json()["clients"][0]
        assert body["separate"] == ["dev"]
        assert body["bundles"] == [{"name": "Bundle", "services": ["dev"]}]


class TestDeleteMissing:
    def test_client_404(self, client):
        assert client.delete(f"{API}/config/clients/nope").status_code == 404

    def test_company_404(self, client):
        assert client.delete(f"{API}/config/companies/nope").status_code == 404

    def test_service_404(self, client):
        assert client.delete(f"{API}/config/services/nope").status_code == 404


class TestDeleteGuards:
    """Each collection's guard matches how badly its absence corrupts things."""

    def test_service_referenced_by_work_is_refused(self, ctx, client):
        from istota.money.work import add_work_entry, assign_invoice_number

        _seed_business(client)
        add_work_entry(ctx.data_dir, "2026-03-01", "acme", "dev", qty=3)
        add_work_entry(ctx.data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(ctx.data_dir, [1], "INV-000001")

        resp = client.delete(f"{API}/config/services/dev")
        assert resp.status_code == 409
        body = resp.json()
        assert body["references"]["work_entries"] == 2
        assert body["references"]["invoices"] == 1
        assert "dev" in config_store.load_invoicing(ctx.db_path).services

    def test_unreferenced_service_deletes(self, ctx, client):
        _seed_business(client)
        resp = client.delete(f"{API}/config/services/dev")
        assert resp.status_code == 200
        assert "dev" not in config_store.load_invoicing(ctx.db_path).services

    def test_entity_referenced_by_client_is_refused(self, ctx, client):
        _seed_business(client)
        client.put(f"{API}/config/clients/acme", json={"entity": "main"})
        resp = client.delete(f"{API}/config/companies/main")
        assert resp.status_code == 409
        assert resp.json()["references"]["clients"] == ["acme"]

    def test_default_entity_is_refused(self, ctx, client):
        _seed_business(client)
        client.post(f"{API}/config/companies", json={"key": "spare", "name": "Spare"})
        client.put(f"{API}/config/invoicing", json={"default_entity": "spare"})
        resp = client.delete(f"{API}/config/companies/spare")
        assert resp.status_code == 409
        assert resp.json()["references"]["default_entity"] is True

    def test_entity_clients_fall_back_to_is_refused(self, ctx, client):
        """A blank `entity` on a client means "bill under the default".

        Reading only the *stored* scalar isn't enough: with no default pinned,
        `load_invoicing` derives one from the first company, so deleting it
        would silently repoint every such client at whichever company happens
        to be next — the exact wrong-legal-entity-on-the-PDF failure.
        """
        _seed_business(client)
        client.post(f"{API}/config/companies", json={"key": "spare", "name": "Spare"})
        # acme has no explicit entity, so it bills under whatever the default is.
        resp = client.delete(f"{API}/config/companies/main")
        assert resp.status_code == 409
        assert resp.json()["references"]["default_for_clients"] == 1

    def test_unreferenced_entity_deletes(self, ctx, client):
        """A sole entity nobody points at is deletable — the bootstrap path.

        `load_invoicing` *derives* a default_entity when none is stored, so
        the guard has to read the stored scalar (and count who actually falls
        back to it) or a fresh user could never undo their first entity.
        """
        client.post(f"{API}/config/companies", json={"key": "main", "name": "Main LLC"})
        resp = client.delete(f"{API}/config/companies/main")
        assert resp.status_code == 200
        assert "main" not in config_store.load_invoicing(ctx.db_path).companies

    def test_client_delete_is_soft(self, ctx, client):
        from istota.money.work import add_work_entry

        _seed_business(client)
        add_work_entry(ctx.data_dir, "2026-03-01", "acme", "dev", qty=3)
        resp = client.delete(f"{API}/config/clients/acme")
        assert resp.status_code == 200
        assert resp.json()["references"]["work_entries"] == 1
        assert "acme" not in config_store.load_invoicing(ctx.db_path).clients

    def test_empty_data_dir_counts_as_zero_references(self, ctx, client):
        _seed_business(client)
        assert not (ctx.data_dir / "invoices").exists()
        assert client.delete(f"{API}/config/services/dev").status_code == 200

    def test_unreadable_work_store_refuses_rather_than_deleting_blind(
        self, ctx, client, monkeypatch,
    ):
        """If we can't count references, we can't know the delete is safe."""
        _seed_business(client)

        def boom(*a, **kw):
            raise OSError("work store unreadable")

        monkeypatch.setattr("istota.money.work.load_work_entries", boom)
        resp = client.delete(f"{API}/config/services/dev")
        assert resp.status_code == 500
        assert resp.json()["status"] == "error"
        assert "dev" in config_store.load_invoicing(ctx.db_path).services


class TestEmptyConfigBootstrap:
    def test_business_settings_defaults_is_null_when_unconfigured(self, client):
        body = client.get(f"{API}/business-settings").json()
        assert body["defaults"] is None
        assert body["entities"] == []
        assert body["services"] == []

    def test_defaults_present_once_configured(self, client):
        _seed_business(client)
        body = client.get(f"{API}/business-settings").json()
        assert body["defaults"]["currency"] == "USD"


# =============================================================================
# Delete guards — the fail-open cases
# =============================================================================


def _seed_work(ctx, **kwargs):
    from istota.money.work import add_work_entry

    defaults = dict(entry_date="2026-03-01", client="acme", service="dev", qty=3)
    defaults.update(kwargs)
    return add_work_entry(ctx.data_dir, **defaults)


def _quarantine_year(ctx, year: int = 2026) -> None:
    """Write a year file whose second row the loader can't model.

    `_load_year` skips such a row and records the year as quarantined — it
    does not raise — so the row is invisible to a reference count and a guard
    built on that count fails open.
    """
    work_dir = ctx.data_dir / "invoices" / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / f"{year}.toml").write_text(
        "[[entries]]\n"
        'date = 2026-03-01\nclient = "acme"\nservice = "dev"\nqty = 1\n\n'
        "[[entries]]\n"
        'date = 2026-03-02\nclient = "acme"\n'  # no service — unreadable
    )


class TestEntityGuardCountsWorkEntries:
    """`resolve_entity` checks `entry.entity` *before* the client's, so an
    entry pinned to an entity re-bills under a different one if it vanishes."""

    def test_pinned_entry_blocks_delete(self, ctx, client):
        config_store.upsert_company(ctx.db_path, "oldco", name="Old Co")
        config_store.upsert_company(ctx.db_path, "newco", name="New Co")
        _seed_work(ctx, entity="oldco")

        resp = client.delete("/istota/api/money/config/companies/oldco")
        assert resp.status_code == 409
        assert "pinned" in resp.json()["error"]
        assert resp.json()["references"]["work_entries"] == 1
        assert "oldco" in config_store.load_invoicing(ctx.db_path).companies

    def test_unpinned_entity_still_deletable(self, ctx, client):
        config_store.upsert_company(ctx.db_path, "first", name="First")
        config_store.upsert_company(ctx.db_path, "spare", name="Spare")
        _seed_work(ctx, entity="first")

        resp = client.delete("/istota/api/money/config/companies/spare")
        assert resp.status_code == 200


class TestEntityGuardUsesTheRealDefault:
    """A stored `default_entity` naming no company is an ordinary outcome of
    migrating a TOML with clients but no `[companies]` block. `load_invoicing`
    then falls back to the *first* company, so that is the entity blank-entity
    clients really bill under — trusting the stale scalar let it be deleted."""

    def test_dangling_stored_default_still_protects_the_real_fallback(self, ctx, client):
        config_store.upsert_company(ctx.db_path, "acme", name="Acme LLC")
        config_store.upsert_client(ctx.db_path, "globex", name="Globex", entity="")
        cfg = config_store.load_invoicing(ctx.db_path)
        cfg.default_entity = "nonexistent"
        config_store.save_invoicing(ctx.db_path, cfg, replace_collections=False)

        # Precondition: the config still resolves invoices to `acme`.
        assert config_store.load_invoicing(ctx.db_path).company.key == "acme"

        resp = client.delete("/istota/api/money/config/companies/acme")
        assert resp.status_code == 409
        assert resp.json()["references"]["default_for_clients"] == 1
        assert "acme" in config_store.load_invoicing(ctx.db_path).companies

    def test_a_fresh_users_only_entity_is_still_deletable(self, ctx, client):
        """No clients means nothing falls back to it — bootstrapping works."""
        config_store.upsert_company(ctx.db_path, "main", name="Main")
        resp = client.delete("/istota/api/money/config/companies/main")
        assert resp.status_code == 200


class TestGuardsFailClosedOnQuarantine:
    """A row the loader skipped is invisible to the count, so a guard built on
    it reads zero. The strict deletes refuse; the soft client delete doesn't."""

    def test_service_delete_refused(self, ctx, client):
        config_store.upsert_service(ctx.db_path, "consulting", display_name="Consulting")
        _quarantine_year(ctx)

        resp = client.delete("/istota/api/money/config/services/consulting")
        assert resp.status_code == 409
        assert "can't read" in resp.json()["error"]
        assert resp.json()["references"]["quarantined"] == ["2026.toml"]
        assert "consulting" in config_store.load_invoicing(ctx.db_path).services

    def test_entity_delete_refused(self, ctx, client):
        config_store.upsert_company(ctx.db_path, "spare", name="Spare")
        config_store.upsert_company(ctx.db_path, "main", name="Main")
        _quarantine_year(ctx)

        resp = client.delete("/istota/api/money/config/companies/spare")
        assert resp.status_code == 409
        assert "can't read" in resp.json()["error"]

    def test_client_delete_still_allowed(self, ctx, client):
        """It destroys nothing, so refusing would strand the user."""
        config_store.upsert_client(ctx.db_path, "acme", name="Acme")
        _quarantine_year(ctx)

        resp = client.delete("/istota/api/money/config/clients/acme")
        assert resp.status_code == 200
        assert "acme" not in config_store.load_invoicing(ctx.db_path).clients

    def test_a_clean_store_reports_no_quarantine(self, ctx, client):
        config_store.upsert_service(ctx.db_path, "design", display_name="Design")
        _seed_work(ctx, service="dev")

        resp = client.delete("/istota/api/money/config/services/design")
        assert resp.status_code == 200
        assert resp.json()["references"]["quarantined"] == []


class TestClientReferenceCountIsCaseInsensitive:
    def test_legacy_mixed_case_key_counts_its_entries(self, ctx, client):
        config_store.init_db(ctx.db_path)
        with config_store._connect(ctx.db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_clients(key, name) VALUES (?, ?)", ("Acme", "Acme"),
            )
        _seed_work(ctx, client="acme")

        resp = client.delete("/istota/api/money/config/clients/Acme")
        assert resp.status_code == 200
        assert resp.json()["references"]["work_entries"] == 1


class TestClientKeyCaseOnCreate:
    def test_mixed_case_key_is_a_400(self, client):
        resp = client.post(
            "/istota/api/money/config/clients", json={"key": "Acme", "name": "Acme"},
        )
        assert resp.status_code == 400
        assert "lowercase" in resp.json()["error"]

    def test_lowercase_key_accepted(self, client):
        resp = client.post(
            "/istota/api/money/config/clients", json={"key": "acme", "name": "Acme"},
        )
        assert resp.status_code == 200


# =============================================================================
# Body handling + PUT semantics
# =============================================================================


class TestMalformedBody:
    """Reading an unparseable body as `{}` turned a broken request into a
    silent write that created a defaults-only record and answered 200."""

    def test_put_rejects_non_json(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/clients/ghost",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "ghost" not in config_store.load_invoicing(ctx.db_path).clients

    def test_put_rejects_a_json_array(self, client):
        resp = client.put("/istota/api/money/config/clients/ghost", json=[1, 2])
        assert resp.status_code == 400

    def test_post_rejects_non_json(self, client):
        resp = client.post(
            "/istota/api/money/config/services",
            content="{oops",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_invoicing_put_rejects_non_json(self, client):
        resp = client.put(
            "/istota/api/money/config/invoicing",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_an_empty_body_is_a_noop_update(self, ctx, client):
        config_store.upsert_client(ctx.db_path, "acme", name="Acme")
        resp = client.put("/istota/api/money/config/clients/acme", content="")
        assert resp.status_code == 200
        assert config_store.load_invoicing(ctx.db_path).clients["acme"].name == "Acme"


class TestPutCreateFalse:
    """The forms only ever PUT a record they loaded; a key another tab deleted
    should 404 rather than resurrect as a partial record."""

    def test_missing_key_404s(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/clients/ghost?create=false", json={"name": "Ghost"},
        )
        assert resp.status_code == 404
        assert "ghost" not in config_store.load_invoicing(ctx.db_path).clients

    def test_entity_and_service_honour_it_too(self, client):
        assert client.put(
            "/istota/api/money/config/companies/ghost?create=false", json={"name": "G"},
        ).status_code == 404
        assert client.put(
            "/istota/api/money/config/services/ghost?create=false", json={"display_name": "G"},
        ).status_code == 404

    def test_default_still_upserts_for_ensure_callers(self, ctx, client):
        resp = client.put("/istota/api/money/config/clients/fresh", json={"name": "Fresh"})
        assert resp.status_code == 200
        assert "fresh" in config_store.load_invoicing(ctx.db_path).clients


class TestPutSurfacesStoreErrors:
    """The `except ValueError` blocks on the PUT handlers were untested."""

    def test_service_type(self, ctx, client):
        config_store.upsert_service(ctx.db_path, "dev", display_name="Dev")
        resp = client.put("/istota/api/money/config/services/dev", json={"type": "hourly"})
        assert resp.status_code == 400
        assert "hourly" in resp.json()["error"]

    def test_client_schedule(self, ctx, client):
        config_store.upsert_client(ctx.db_path, "acme", name="Acme")
        resp = client.put("/istota/api/money/config/clients/acme", json={"schedule": "weekly"})
        assert resp.status_code == 400

    def test_company_account(self, ctx, client):
        config_store.upsert_company(ctx.db_path, "main", name="Main")
        resp = client.put(
            "/istota/api/money/config/companies/main", json={"ar_account": "assets ar"},
        )
        assert resp.status_code == 400

    def test_company_logo_escape(self, ctx, client):
        config_store.upsert_company(ctx.db_path, "main", name="Main")
        resp = client.put(
            "/istota/api/money/config/companies/main", json={"logo": "/etc/passwd"},
        )
        assert resp.status_code == 400
        assert config_store.load_invoicing(ctx.db_path).companies["main"].logo == ""


class TestUnknownKeysNamedPerCollection:
    def test_entity(self, client):
        resp = client.post(
            "/istota/api/money/config/companies", json={"key": "main", "nmae": "Main"},
        )
        assert resp.status_code == 400
        assert "nmae" in resp.json()["error"]

    def test_service(self, client):
        resp = client.post(
            "/istota/api/money/config/services", json={"key": "dev", "raet": 1},
        )
        assert resp.status_code == 400
        assert "raet" in resp.json()["error"]


class TestInvoicingScalarHardening:
    def test_non_integer_invoice_number_refused(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/invoicing", json={"next_invoice_number": "lots"},
        )
        assert resp.status_code == 400
        assert config_store.load_invoicing(ctx.db_path).next_invoice_number == 1

    def test_unknown_default_entity_refused(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/invoicing", json={"default_entity": "nope"},
        )
        assert resp.status_code == 400

    def test_known_default_entity_accepted(self, ctx, client):
        config_store.upsert_company(ctx.db_path, "main", name="Main")
        resp = client.put(
            "/istota/api/money/config/invoicing", json={"default_entity": "main"},
        )
        assert resp.status_code == 200
        assert config_store.load_invoicing(ctx.db_path).default_entity == "main"

    def test_malformed_default_account_refused(self, client):
        resp = client.put(
            "/istota/api/money/config/invoicing", json={"default_ar_account": "assets ar"},
        )
        assert resp.status_code == 400

    def test_zero_invoice_number_refused(self, client):
        resp = client.put(
            "/istota/api/money/config/invoicing", json={"next_invoice_number": 0},
        )
        assert resp.status_code == 400


class TestMonarchSyncSettings:
    def test_rejects_unparseable_default_account(self, client):
        resp = client.put(
            "/istota/api/money/config/monarch",
            json={"default_account": "Assets:Bank (Checking)"},
        )
        assert resp.status_code == 400
        body = client.get("/istota/api/money/config/monarch").json()
        assert body["sync"]["default_account"] == "Assets:Bank:Checking"

    def test_accepts_a_valid_default_account(self, client):
        resp = client.put(
            "/istota/api/money/config/monarch",
            json={"default_account": "Assets:Bank:Savings"},
        )
        assert resp.status_code == 200
        body = client.get("/istota/api/money/config/monarch").json()
        assert body["sync"]["default_account"] == "Assets:Bank:Savings"

    def test_an_unrelated_bad_row_does_not_block_the_edit(self, ctx, client):
        import sqlite3

        with sqlite3.connect(ctx.db_path) as conn:
            conn.execute(
                "INSERT INTO monarch_category_map(profile_id, monarch_category, "
                "beancount_account) VALUES (?, ?, ?)",
                (config_store.GLOBAL_PROFILE_ID, "Fees", "Expenses:Fees (Bank)"),
            )
        resp = client.put(
            "/istota/api/money/config/monarch", json={"lookback_days": 30},
        )
        assert resp.status_code == 200
        assert client.get("/istota/api/money/config/monarch").json()[
            "sync"
        ]["lookback_days"] == 30

    def test_rejects_an_empty_default_account(self, client):
        resp = client.put(
            "/istota/api/money/config/monarch", json={"default_account": ""},
        )
        assert resp.status_code == 400


# =============================================================================
# Transaction rules
# =============================================================================

RULES = "/istota/api/money/config/transaction-rules"


def _rule(client, **fields):
    """Create one rule through the API and hand back the stored row."""
    body = {"ledger": "personal", "source": "monarch-api", **fields}
    resp = client.post(RULES, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["rule"]


class TestTransactionRulesList:
    def test_an_exact_scope_excludes_the_seed_tier(self, client):
        """``list_transaction_rules`` matches scope exactly, not by wildcard.

        The seeded shipped map lives at ``ledger=''``, and an editor showing
        one ledger's rules must not fold those in as though the user wrote
        them there.
        """
        _rule(client, field="category", match_value="Software",
              action="posting_account", target="Expenses:Biz:Software")
        body = client.get(f"{RULES}?ledger=personal&source=monarch-api").json()
        assert body["status"] == "ok"
        assert [r["match_value"] for r in body["rules"]] == ["Software"]

    def test_the_global_scope_is_reachable_as_an_empty_ledger(self, client):
        body = client.get(f"{RULES}?ledger=&source=").json()
        values = {r["match_value"] for r in body["rules"]}
        assert "Groceries" in values
        assert all(r["origin"] == "seed" for r in body["rules"])

    def test_rules_come_back_in_evaluation_order(self, client):
        _rule(client, field="category", match_value="B",
              action="posting_account", target="Expenses:B", priority=200)
        _rule(client, field="category", match_value="A",
              action="posting_account", target="Expenses:A", priority=50)
        body = client.get(f"{RULES}?ledger=personal&source=monarch-api").json()
        assert [r["match_value"] for r in body["rules"]] == ["A", "B"]

    def test_disabled_rules_are_listed_by_default(self, client):
        _rule(client, field="category", match_value="Off",
              action="posting_account", target="Expenses:Off", enabled=False)
        body = client.get(f"{RULES}?ledger=personal&source=monarch-api").json()
        assert body["rules"][0]["enabled"] is False

    def test_include_disabled_false_drops_them(self, client):
        _rule(client, field="category", match_value="On",
              action="posting_account", target="Expenses:On")
        _rule(client, field="category", match_value="Off",
              action="posting_account", target="Expenses:Off", enabled=False)
        body = client.get(
            f"{RULES}?ledger=personal&source=monarch-api&include_disabled=false",
        ).json()
        assert [r["match_value"] for r in body["rules"]] == ["On"]


class TestTransactionRuleCreate:
    def test_creates_and_returns_the_stored_row(self, ctx, client):
        rule = _rule(client, field="category", match_value="Software",
                     action="posting_account", target="Expenses:Biz:Software")
        assert rule["id"] > 0
        assert rule["priority"] == 100
        assert rule["match_kind"] == "iexact"
        stored = config_store.get_transaction_rule(ctx.db_path, rule["id"])
        assert stored["target"] == "Expenses:Biz:Software"

    def test_ledger_must_be_sent_explicitly(self, client):
        """Both scope columns default to ``''``, which the engine reads as
        "any". A create that omits one silently writes a rule applying to
        every ledger, so the widest scope has to be chosen, not defaulted."""
        resp = client.post(RULES, json={
            "source": "monarch-api", "field": "category",
            "match_value": "Software", "action": "posting_account",
            "target": "Expenses:Biz:Software",
        })
        assert resp.status_code == 400
        assert "ledger" in resp.json()["error"]

    def test_source_must_be_sent_explicitly(self, client):
        resp = client.post(RULES, json={
            "ledger": "personal", "field": "category",
            "match_value": "Software", "action": "posting_account",
            "target": "Expenses:Biz:Software",
        })
        assert resp.status_code == 400
        assert "source" in resp.json()["error"]

    def test_an_empty_scope_is_legal_when_it_is_sent(self, client):
        rule = _rule(client, ledger="", source="", field="category",
                     match_value="Widgets", action="posting_account",
                     target="Expenses:Widgets")
        assert rule["ledger"] == "" and rule["source"] == ""

    def test_nothing_is_written_when_the_scope_is_missing(self, ctx, client):
        """The refusal has to be a refusal, not a 400 over a stored row."""
        client.post(RULES, json={
            "field": "category", "match_value": "Software",
            "action": "posting_account", "target": "Expenses:Biz:Software",
        })
        assert not [
            r for r in config_store.list_transaction_rules(ctx.db_path)
            if r["match_value"] == "Software"
        ]

    def test_a_bad_account_is_a_400(self, client):
        resp = client.post(RULES, json={
            "ledger": "personal", "source": "monarch-api", "field": "category",
            "match_value": "Software", "action": "posting_account",
            "target": "Expenses:Biz (Software)",
        })
        assert resp.status_code == 400

    def test_a_bad_field_is_a_400(self, client):
        resp = client.post(RULES, json={
            "ledger": "personal", "source": "monarch-api", "field": "amount",
            "match_value": "5", "action": "posting_account",
            "target": "Expenses:X",
        })
        assert resp.status_code == 400

    def test_a_duplicate_names_the_existing_id_and_not_the_value(self, client):
        first = _rule(client, field="category", match_value="Software",
                      action="posting_account", target="Expenses:A")
        resp = client.post(RULES, json={
            "ledger": "personal", "source": "monarch-api", "field": "category",
            "match_kind": "iexact", "match_value": "Software",
            "action": "posting_account", "target": "Expenses:B",
        })
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert f"id {first['id']}" in error
        assert "Software" not in error

    def test_a_body_that_is_not_an_object_is_a_400(self, client):
        assert client.post(RULES, json=["nope"]).status_code == 400

    def test_an_unknown_field_is_a_400(self, client):
        resp = client.post(RULES, json={
            "ledger": "personal", "source": "monarch-api", "field": "category",
            "match_value": "Software", "action": "posting_account",
            "target": "Expenses:X", "colour": "red",
        })
        assert resp.status_code == 400

    def test_the_seed_origin_is_reserved(self, client):
        resp = client.post(RULES, json={
            "ledger": "personal", "source": "monarch-api", "field": "category",
            "match_value": "Software", "action": "posting_account",
            "target": "Expenses:X", "origin": "seed",
        })
        assert resp.status_code == 400


class TestTransactionRuleUpdate:
    def test_updates_and_returns_the_row(self, client):
        rule = _rule(client, field="category", match_value="Software",
                     action="posting_account", target="Expenses:A")
        resp = client.put(f"{RULES}/{rule['id']}", json={"target": "Expenses:B"})
        assert resp.status_code == 200
        assert resp.json()["rule"]["target"] == "Expenses:B"

    def test_a_partial_change_is_validated_against_the_whole_row(self, client):
        """A ``skip`` action and a target arriving in separate requests are
        still checked against each other."""
        rule = _rule(client, field="category", match_value="Software",
                     action="posting_account", target="Expenses:A")
        resp = client.put(f"{RULES}/{rule['id']}", json={"action": "skip"})
        assert resp.status_code == 400

    def test_a_missing_rule_is_a_404(self, client):
        assert client.put(f"{RULES}/99999", json={"target": "Expenses:B"}).status_code == 404

    def test_a_body_that_is_not_an_object_is_a_400(self, client):
        rule = _rule(client, field="category", match_value="Software",
                     action="posting_account", target="Expenses:A")
        assert client.put(f"{RULES}/{rule['id']}", json="nope").status_code == 400

    def test_an_update_onto_another_rules_identity_is_a_400(self, client):
        _rule(client, field="category", match_value="A",
              action="posting_account", target="Expenses:A")
        second = _rule(client, field="category", match_value="B",
                       action="posting_account", target="Expenses:B")
        resp = client.put(f"{RULES}/{second['id']}", json={"match_value": "A"})
        assert resp.status_code == 400


class TestTransactionRuleDelete:
    def test_removes_the_rule(self, ctx, client):
        rule = _rule(client, field="category", match_value="Software",
                     action="posting_account", target="Expenses:A")
        body = client.delete(f"{RULES}/{rule['id']}").json()
        assert body == {"status": "ok", "removed": True}
        assert config_store.get_transaction_rule(ctx.db_path, rule["id"]) is None

    def test_a_missing_rule_is_ok_and_not_removed(self, client):
        """Matching ``delete_monarch_profile``'s existing shape."""
        resp = client.delete(f"{RULES}/99999")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "removed": False}


class TestTransactionRuleTest:
    def _preview(self, client, **body):
        return client.post(f"{RULES}/test", json={
            "ledger": "personal", "source": "monarch-api", **body,
        })

    def test_resolves_against_the_stored_rules(self, client):
        posting = _rule(client, field="category", match_value="Software",
                        action="posting_account", target="Expenses:Biz:Software")
        contra = _rule(client, field="account", match_value="Chase Business",
                       action="contra_account", target="Assets:Bank:Chase")
        body = self._preview(
            client, category="Software", account="Chase Business",
        ).json()
        assert body["status"] == "ok"
        assert body["resolution"]["skip"] is False
        assert body["resolution"]["posting_account"] == "Expenses:Biz:Software"
        assert body["resolution"]["contra_account"] == "Assets:Bank:Chase"
        assert [h["rule_id"] for h in body["resolution"]["hits"]] == [
            posting["id"], contra["id"],
        ]

    def test_the_trace_names_the_rule_that_shadowed_a_match(self, client):
        winner = _rule(client, field="category", match_value="Software",
                       action="posting_account", target="Expenses:First",
                       priority=10)
        loser = _rule(client, field="category", match_kind="contains",
                      match_value="Soft", action="posting_account",
                      target="Expenses:Second", priority=20)
        body = self._preview(client, category="Software").json()
        by_id = {t["rule_id"]: t for t in body["trace"]}
        assert by_id[winner["id"]]["outcome"] == "applied"
        assert by_id[loser["id"]]["outcome"] == "shadowed"
        assert by_id[loser["id"]]["shadowed_by"] == winner["id"]

    def test_a_skip_ends_the_pass_and_the_rest_are_unevaluated(self, client):
        skip = _rule(client, field="tag", match_value="Personal",
                     action="skip", priority=50)
        later = _rule(client, field="category", match_value="Software",
                      action="posting_account", target="Expenses:Biz",
                      priority=100)
        body = self._preview(
            client, category="Software", tags=["Personal"],
        ).json()
        assert body["resolution"]["skip"] is True
        assert body["resolution"]["posting_account"] is None
        assert [h["rule_id"] for h in body["resolution"]["hits"]] == [skip["id"]]
        by_id = {t["rule_id"]: t for t in body["trace"]}
        assert by_id[later["id"]]["outcome"] == "not_evaluated"

    def test_a_rule_that_did_not_match_says_so(self, client):
        rule = _rule(client, field="category", match_value="Software",
                     action="posting_account", target="Expenses:Biz")
        body = self._preview(client, category="Groceries").json()
        by_id = {t["rule_id"]: t for t in body["trace"]}
        assert by_id[rule["id"]]["outcome"] == "no_match"

    def test_the_seed_tier_is_in_scope_for_every_ledger(self, client):
        """A seeded rule is at ``ledger=''``, which the engine reads as any."""
        body = self._preview(client, category="Groceries").json()
        assert body["resolution"]["posting_account"] == "Expenses:Food:Groceries"

    def test_disabled_rules_do_not_fire(self, client):
        _rule(client, field="category", match_value="Software",
              action="posting_account", target="Expenses:Biz", enabled=False)
        body = self._preview(client, category="Software").json()
        assert body["resolution"]["posting_account"] is None

    def test_ledger_and_source_must_be_sent(self, client):
        """An omitted ledger silently previews the global scope alone, which
        is a wrong answer rather than a missing one."""
        resp = client.post(f"{RULES}/test", json={"category": "Software"})
        assert resp.status_code == 400

    def test_a_non_string_subject_is_a_400(self, client):
        assert self._preview(client, category=7).status_code == 400

    def test_tags_must_be_a_list_of_strings(self, client):
        assert self._preview(client, tags="Personal").status_code == 400
        assert self._preview(client, tags=[1, 2]).status_code == 400

    def test_a_body_that_is_not_an_object_is_a_400(self, client):
        assert client.post(f"{RULES}/test", json=[]).status_code == 400

    def test_an_unmigrated_deployment_refuses_rather_than_previewing(
        self, client, monkeypatch,
    ):
        """``load_rules_for_run`` answering ``None`` means an import would
        take the dict path, so a preview drawn from the table would describe
        behaviour the deployment does not have.

        The missing sentinel is patched rather than deleted: every accessor
        calls ``init_db``, so a deleted row is rewritten by the very request
        under test. ``_rules_migrated`` is the predicate the absent sentinel
        produces, and ``load_rules_for_run`` still computes its ``None`` from
        it for real.
        """
        monkeypatch.setattr(config_store, "_rules_migrated", lambda conn: False)
        resp = self._preview(client, category="Groceries")
        assert resp.status_code == 409
        assert resp.json()["status"] == "error"

    def test_a_healthy_deployment_does_not_take_that_branch(self, client):
        """The control for the case above: same request, sentinel present."""
        assert self._preview(client, category="Groceries").status_code == 200


class TestTransactionRuleCoverage:
    def _seed(self, ctx):
        from istota.money.db import get_db, init_db, track_monarch_transactions_batch

        init_db(ctx.db_path)
        with get_db(ctx.db_path) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "a", "amount": -1, "merchant": "A", "txn_date": "2026-07-01",
                 "src_category": "Software", "src_account": "Chase",
                 "posted_account": "Expenses:Biz:Software"},
                {"id": "b", "amount": -2, "merchant": "B", "txn_date": "2026-07-02",
                 "src_category": "Software", "src_account": "Chase",
                 "posted_account": "Expenses:Biz:Software"},
                {"id": "c", "amount": -3, "merchant": "C", "txn_date": "2026-07-03",
                 "src_category": "Widgets",
                 "posted_account": "Expenses:Uncategorized:Widgets"},
                {"id": "d", "amount": -4, "merchant": "D", "txn_date": "2026-06-01",
                 "posted_account": "Expenses:Old"},
            ], profile="biz")

    def test_returns_values_counts_and_the_posted_account(self, ctx, client):
        self._seed(ctx)
        body = client.get(f"{RULES}/coverage?field=category").json()
        assert body["status"] == "ok"
        by_value = {v["value"]: v for v in body["values"]}
        assert by_value["Software"]["count"] == 2
        assert by_value["Software"]["last_seen"] == "2026-07-02"
        assert by_value["Widgets"]["posted_account"] == "Expenses:Uncategorized:Widgets"

    def test_untraced_rows_are_a_separate_count(self, ctx, client):
        self._seed(ctx)
        body = client.get(f"{RULES}/coverage?field=category").json()
        assert body["untraced"] == 1

    def test_the_untraced_count_is_absent_for_the_account_field(self, ctx, client):
        """It counts rows with no ``src_category``, which says nothing about
        the account column — a row can carry a category and no account."""
        self._seed(ctx)
        resp = client.get(f"{RULES}/coverage?field=account")
        assert resp.status_code == 200
        body = resp.json()
        assert "untraced" not in body
        assert [v["value"] for v in body["values"]] == ["Chase"]

    def test_the_profile_scopes_the_read(self, ctx, client):
        self._seed(ctx)
        assert client.get(
            f"{RULES}/coverage?field=category&profile=biz",
        ).json()["values"]
        assert client.get(
            f"{RULES}/coverage?field=category&profile=other",
        ).json()["values"] == []

    def test_an_unknown_field_is_a_400(self, ctx, client):
        self._seed(ctx)
        assert client.get(f"{RULES}/coverage?field=payee").status_code == 400

    def test_an_empty_table_is_an_empty_list(self, ctx, client):
        from istota.money.db import init_db

        init_db(ctx.db_path)
        body = client.get(f"{RULES}/coverage?field=category").json()
        assert body["values"] == []
        assert body["untraced"] == 0


class TestTransactionRuleMutationsRequireCsrf:
    """Every mutation carries ``verify_origin``; the reads do not."""

    def _client(self, ctx) -> TestClient:
        from fastapi import HTTPException

        from istota.money.routes import verify_origin

        app = FastAPI()
        app.include_router(router, prefix="/istota/api/money")
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_config] = lambda: ctx

        def _reject():
            raise HTTPException(403, "bad origin")

        app.dependency_overrides[verify_origin] = _reject
        return TestClient(app, raise_server_exceptions=False)

    def test_create_blocked(self, ctx, client):
        blocked = self._client(ctx)
        assert blocked.post(RULES, json={
            "ledger": "personal", "source": "monarch-api", "field": "category",
            "match_value": "Software", "action": "posting_account",
            "target": "Expenses:X",
        }).status_code == 403
        assert not [
            r for r in config_store.list_transaction_rules(ctx.db_path)
            if r["match_value"] == "Software"
        ]

    def test_update_blocked(self, ctx, client):
        rule = _rule(client, field="category", match_value="Software",
                     action="posting_account", target="Expenses:A")
        assert self._client(ctx).put(
            f"{RULES}/{rule['id']}", json={"target": "Expenses:B"},
        ).status_code == 403
        assert config_store.get_transaction_rule(
            ctx.db_path, rule["id"],
        )["target"] == "Expenses:A"

    def test_delete_blocked(self, ctx, client):
        rule = _rule(client, field="category", match_value="Software",
                     action="posting_account", target="Expenses:A")
        assert self._client(ctx).delete(
            f"{RULES}/{rule['id']}",
        ).status_code == 403
        assert config_store.get_transaction_rule(ctx.db_path, rule["id"]) is not None

    def test_the_preview_is_a_mutation_shaped_read_and_is_guarded(self, ctx):
        """A POST from another origin must not become a rule oracle."""
        assert self._client(ctx).post(f"{RULES}/test", json={
            "ledger": "personal", "source": "monarch-api", "category": "Groceries",
        }).status_code == 403

    def test_the_reads_are_not_blocked(self, ctx):
        blocked = self._client(ctx)
        assert blocked.get(f"{RULES}?ledger=&source=").status_code == 200
