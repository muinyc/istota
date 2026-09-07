"""Tests for the immunization API routes + parser."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.health import db as health_db
from istota.health._migrate import ensure_initialised
from istota.health.models import HealthContext
from istota.health.parser import parse_paste
from istota.health.routes import (
    get_user_context,
    require_auth,
    router,
)
from istota.health.workspace import synthesize_health_context


@pytest.fixture
def ctx(tmp_path: Path) -> HealthContext:
    c = synthesize_health_context("alice", tmp_path / "workspace")
    ensure_initialised(c)
    return c


@pytest.fixture
def client(ctx: HealthContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/istota/api/health")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_context] = lambda: ctx
    return TestClient(app)


class TestParser:
    def _refs(self, ctx):
        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_immunization_refs(conn)

    def test_mychart_shape(self, ctx):
        refs = self._refs(ctx)
        text = (
            "INFS Pres Free 6mos-Adult (Fluzone trivalent) (influenza) (Given 11/28/2025)\n"
            "Tdap (Tetanus, diphtheria, acellular pertussis) (Given 12/1/2016)\n"
            "TYDvi (Typhoid, ViCPs) (Given 10/23/2023)\n"
        )
        rows = parse_paste(text, refs)
        names = [r.name for r in rows]
        assert names == ["Influenza", "Tdap", "Typhoid"]
        assert rows[0].date_given == "2025-11-28"
        assert rows[1].date_given == "2016-12-01"
        assert rows[2].date_given == "2023-10-23"
        assert all(r.confidence == "high" for r in rows)

    def test_iso_shape(self, ctx):
        refs = self._refs(ctx)
        text = "Influenza 2025-11-28\nShingrix dose 1 2024-09-15\n"
        rows = parse_paste(text, refs)
        assert rows[0].name == "Influenza"
        assert rows[0].date_given == "2025-11-28"
        assert rows[1].name == "Shingles"
        assert rows[1].date_given == "2024-09-15"

    def test_unknown_family_keeps_source(self, ctx):
        refs = self._refs(ctx)
        text = "Some Weird Vaccine Brand (Given 1/2/2024)\n"
        rows = parse_paste(text, refs)
        assert rows[0].name == "Unknown"
        assert rows[0].date_given == "2024-01-02"
        assert rows[0].notes == "Some Weird Vaccine Brand (Given 1/2/2024)"

    def test_free_text_fallback(self, ctx):
        refs = self._refs(ctx)
        text = "Got my flu shot at the pharmacy\n"
        rows = parse_paste(text, refs)
        # 'flu' alias matches Influenza; no date → manual.
        assert rows[0].name == "Influenza"
        assert rows[0].date_given is None
        assert rows[0].confidence == "manual"

    def test_empty_input(self, ctx):
        refs = self._refs(ctx)
        assert parse_paste("", refs) == []
        assert parse_paste("\n\n   \n", refs) == []

    def test_two_digit_year_pivot(self, ctx):
        refs = self._refs(ctx)
        rows = parse_paste("Influenza (Given 11/28/68)\n", refs)
        # 68 → 2068 (matches the <70 pivot).
        assert rows[0].date_given == "2068-11-28"
        rows = parse_paste("Influenza (Given 11/28/85)\n", refs)
        # 85 → 1985.
        assert rows[0].date_given == "1985-11-28"

    def test_an_impossible_iso_date_is_rejected_at_this_seam(self, ctx):
        """The stage's one declared user-visible change, at the call site.

        ``tests/test_date_parse.py`` pins the leaf, which did not exist
        before and so cannot have gone red against the old code here. This
        is the seam: ``parse_paste`` used to hand back ``2026-02-31`` at
        ``high`` confidence, which ``POST /immunizations/bulk`` then refused
        with a 400 several screens later.
        """
        refs = self._refs(ctx)
        # A trailing ISO date is what reaches the ISO branch. `(Given …)` is
        # the MyChart shape and only ever carries M/D/YYYY.
        good = parse_paste("Influenza 2026-02-28\n", refs)
        assert good[0].date_given == "2026-02-28"
        assert good[0].confidence == "high"

        rows = parse_paste("Influenza 2026-02-31\n", refs)
        assert rows[0].date_given is None
        assert rows[0].confidence == "medium"
        assert rows[0].source_line == "Influenza 2026-02-31"
        assert rows[0].name == "Influenza"

    def test_an_impossible_us_date_was_already_rejected_here(self, ctx):
        """The pre-existing half, so the change above reads as consistency.

        The ``M/D/YYYY`` branch has always validated; the ISO branch is the
        one that did not.
        """
        refs = self._refs(ctx)
        rows = parse_paste("Influenza (Given 13/45/2026)\n", refs)
        assert rows[0].date_given is None

    def test_longest_alias_wins(self, ctx):
        refs = self._refs(ctx)
        # 'COVID-19 PF' should resolve to COVID-19, not whatever else.
        rows = parse_paste(
            "COVID-19 PF (Janssen/J&J), External Administration (Given 3/17/2021)\n",
            refs,
        )
        assert rows[0].name == "COVID-19"
        assert rows[0].date_given == "2021-03-17"


class TestCrudRoutes:
    def test_create_and_get(self, client):
        resp = client.post("/istota/api/health/immunizations", json={
            "name": "Influenza",
            "date_given": "2025-11-28",
            "product_name": "Fluzone trivalent",
            "facility": "CVS Pharmacy",
        })
        assert resp.status_code == 200, resp.text
        iid = resp.json()["id"]

        got = client.get(f"/istota/api/health/immunizations/{iid}").json()
        assert got["immunization"]["name"] == "Influenza"
        assert got["immunization"]["product_name"] == "Fluzone trivalent"

    def test_missing_name_or_date(self, client):
        resp = client.post("/istota/api/health/immunizations", json={
            "date_given": "2025-11-28",
        })
        assert resp.status_code == 400
        resp = client.post("/istota/api/health/immunizations", json={
            "name": "Influenza",
        })
        assert resp.status_code == 400

    def test_list_filters(self, client):
        for d in ("2023-10-23", "2025-11-28"):
            client.post("/istota/api/health/immunizations", json={
                "name": "Influenza", "date_given": d,
            })
        client.post("/istota/api/health/immunizations", json={
            "name": "Tdap", "date_given": "2016-12-01",
        })
        rows = client.get(
            "/istota/api/health/immunizations?name=Influenza",
        ).json()["immunizations"]
        assert all(r["name"] == "Influenza" for r in rows)
        assert len(rows) == 2

    def test_update_and_delete(self, client):
        resp = client.post("/istota/api/health/immunizations", json={
            "name": "Tdap", "date_given": "2016-12-01",
        })
        iid = resp.json()["id"]
        upd = client.put(
            f"/istota/api/health/immunizations/{iid}",
            json={"lot_number": "ABC123"},
        )
        assert upd.status_code == 200
        got = client.get(f"/istota/api/health/immunizations/{iid}").json()
        assert got["immunization"]["lot_number"] == "ABC123"

        d = client.delete(f"/istota/api/health/immunizations/{iid}")
        assert d.status_code == 200
        got = client.get(f"/istota/api/health/immunizations/{iid}")
        assert got.status_code == 404


class TestRefsAndCoverageRoutes:
    def test_refs_endpoint(self, client):
        refs = client.get("/istota/api/health/immunizations/refs").json()
        names = {r["name"] for r in refs["refs"]}
        assert "Influenza" in names
        assert "Tdap" in names

    def test_coverage_no_rows(self, client):
        resp = client.get("/istota/api/health/immunizations/coverage").json()
        # An empty account holds nothing, so nothing is part-done: every entry
        # is never_recorded, apart from the risk_based schedules that opt out
        # of flagging entirely. Nothing is series_incomplete.
        cov = resp["coverage"]
        assert cov
        assert {c["status"] for c in cov} <= {"never_recorded", "risk_based"}
        by_name = {c["name"]: c["status"] for c in cov}
        # One of each series schedule, including the risk_based-category ref
        # that is nonetheless a series (Shingles).
        assert by_name["Hepatitis B"] == "never_recorded"
        assert by_name["HPV"] == "never_recorded"
        assert by_name["Shingles"] == "never_recorded"
        assert resp["other"] == []

    def test_history_summary_no_rows_has_no_action_needed(self, client):
        # The agent-facing summary must not report action on an empty account.
        resp = client.get("/istota/api/health/history/summary").json()
        assert resp["immunizations"]["action_needed"] == []

    def test_coverage_with_rows(self, client):
        client.post("/istota/api/health/immunizations", json={
            "name": "Influenza", "date_given": "2025-09-01",
        })
        resp = client.get("/istota/api/health/immunizations/coverage").json()
        flu = next(c for c in resp["coverage"] if c["name"] == "Influenza")
        assert flu["last_given"] == "2025-09-01"
        assert flu["dose_count"] == 1

    def test_other_bucket(self, client):
        client.post("/istota/api/health/immunizations", json={
            "name": "Custom Trial Vaccine", "date_given": "2024-01-01",
        })
        resp = client.get("/istota/api/health/immunizations/coverage").json()
        other_names = {o["name"] for o in resp["other"]}
        assert "Custom Trial Vaccine" in other_names


class TestExtractRoute:
    def test_extract_returns_fallback_when_brain_unavailable(self, client):
        # No app.state.istota_config → brain unavailable → fallback empty rows
        # with a warning, but still a 200.
        files = {"file": ("vaccines.png", b"\x89PNG\r\n\x1a\nfake", "image/png")}
        resp = client.post(
            "/istota/api/health/immunizations/extract",
            files=files,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "rows" in body
        assert "warnings" in body
        assert any("unavailable" in w.lower() for w in body["warnings"])

    def test_extract_rejects_empty_upload(self, client):
        files = {"file": ("vaccines.png", b"", "image/png")}
        resp = client.post(
            "/istota/api/health/immunizations/extract",
            files=files,
        )
        assert resp.status_code == 400


class TestExtractParser:
    """LLM response parsing (the brain-call path) — tested in isolation."""

    def test_parse_object_envelope(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = (
            '{"immunizations": ['
            '{"name": "Influenza", "product_name": "Fluzone", '
            '"date_given": "2025-11-28", "confidence": "high"}'
            ']}'
        )
        rows, dropped = _parse_llm_response(raw)
        assert len(rows) == 1
        assert rows[0]["name"] == "Influenza"
        assert rows[0]["date_given"] == "2025-11-28"
        assert rows[0]["confidence"] == "high"
        assert dropped == 0

    def test_parse_bare_array(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = '[{"name": "Tdap", "date_given": "12/1/2016"}]'
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["name"] == "Tdap"
        # US date normalised to ISO.
        assert rows[0]["date_given"] == "2016-12-01"

    def test_parse_strips_code_fences(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = (
            "Sure, here you go:\n"
            "```json\n"
            '{"immunizations": [{"name": "MMR", "date_given": "1990-01-01"}]}\n'
            "```"
        )
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["name"] == "MMR"

    def test_parse_handles_two_digit_year(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = '[{"name": "Tdap", "date_given": "12/1/85"}]'
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["date_given"] == "1985-12-01"

    def test_parse_marks_no_date_as_manual(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = '[{"name": "Influenza", "date_given": null}]'
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["date_given"] is None
        assert rows[0]["confidence"] == "manual"

    def test_parse_unknown_name_defaults(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = '[{"date_given": "2024-01-01"}]'
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["name"] == "Unknown"

    def test_parse_drops_future_dated_rows(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = (
            '[{"name": "Influenza", "date_given": "2099-04-01"},'
            ' {"name": "Tdap", "date_given": "2020-01-15"}]'
        )
        rows, dropped = _parse_llm_response(raw)
        assert dropped == 1
        assert len(rows) == 1
        assert rows[0]["name"] == "Tdap"

    def test_parse_drops_malformed_iso_dates(self):
        from istota.health.immunization_ocr import _parse_llm_response

        # Month 13 — passes the regex but fromisoformat rejects it.
        raw = '[{"name": "Influenza", "date_given": "2024-13-01"}]'
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["date_given"] is None


class TestParseAndBulkRoutes:
    def test_parse_endpoint(self, client):
        text = "Tdap (Given 12/1/2016)\n"
        resp = client.post(
            "/istota/api/health/immunizations/parse",
            json={"text": text},
        )
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert rows[0]["name"] == "Tdap"
        assert rows[0]["date_given"] == "2016-12-01"

    def test_bulk_roundtrip(self, client):
        parsed = client.post(
            "/istota/api/health/immunizations/parse",
            json={"text":
                "Influenza (Given 11/28/2025)\n"
                "Tdap (Given 12/1/2016)\n"
            },
        ).json()["rows"]
        resp = client.post(
            "/istota/api/health/immunizations/bulk",
            json={"rows": parsed},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        listing = client.get(
            "/istota/api/health/immunizations",
        ).json()["immunizations"]
        assert {r["name"] for r in listing} == {"Influenza", "Tdap"}

    def test_bulk_rejects_missing_fields(self, client):
        resp = client.post(
            "/istota/api/health/immunizations/bulk",
            json={"rows": [{"name": "Influenza"}]},
        )
        assert resp.status_code == 400

    def test_bulk_rejects_malformed_date(self, client):
        resp = client.post(
            "/istota/api/health/immunizations/bulk",
            json={"rows": [{"name": "Influenza", "date_given": "yesterday"}]},
        )
        assert resp.status_code == 400
        assert "ISO" in resp.json()["error"]

    def test_bulk_rejects_future_date(self, client):
        resp = client.post(
            "/istota/api/health/immunizations/bulk",
            json={"rows": [{"name": "Influenza", "date_given": "2099-04-01"}]},
        )
        assert resp.status_code == 400
        assert "future" in resp.json()["error"]

    def test_bulk_with_import_id_dedupes_replays(self, client):
        """A client that supplies the same ``import_id`` on retry must not
        cause duplicate rows. This is the actual double-click / network-
        retry guarantee — fresh per-request UUIDs alone don't provide it.
        """
        rows = [
            {"name": "Influenza", "date_given": "2025-11-28"},
            {"name": "Tdap", "date_given": "2016-12-01"},
        ]
        payload = {"rows": rows, "import_id": "session-abc-123"}
        first = client.post(
            "/istota/api/health/immunizations/bulk", json=payload,
        )
        second = client.post(
            "/istota/api/health/immunizations/bulk", json=payload,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["ids"] == second.json()["ids"]
        listing = client.get(
            "/istota/api/health/immunizations",
        ).json()["immunizations"]
        # Two unique imports, not four.
        names = sorted(r["name"] for r in listing)
        assert names == ["Influenza", "Tdap"]

    def test_bulk_without_import_id_still_assigns_dedup_keys(
        self, client, ctx,
    ):
        """Even without a client import_id, every inserted row must carry
        a dedup_key — matches the skill CLI invariant and is what makes
        any future server-side replay mechanism safe.
        """
        client.post(
            "/istota/api/health/immunizations/bulk",
            json={"rows": [
                {"name": "MMR", "date_given": "1990-01-01"},
            ]},
        )
        with health_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT dedup_key FROM immunizations WHERE name = 'MMR'",
            ).fetchone()
        assert row is not None
        assert row["dedup_key"] is not None
        assert ":" in row["dedup_key"]  # prefix:index shape

    def test_bulk_rejects_empty_import_id(self, client):
        resp = client.post(
            "/istota/api/health/immunizations/bulk",
            json={
                "rows": [{"name": "MMR", "date_given": "1990-01-01"}],
                "import_id": "",
            },
        )
        assert resp.status_code == 400

    def test_extract_uses_temp_dir_not_uploads_dir(
        self, client, ctx, tmp_path, monkeypatch,
    ):
        """The /extract route must not leak transient uploads into the
        persistent uploads_dir on a crashed worker — verified by stubbing
        ``extract_from_file`` to capture the path the route hands it and
        asserting it's outside ``ctx.uploads_dir``.
        """
        captured: dict[str, Path] = {}

        def _fake_extract(path, mime, refs, *, config=None, user_id=""):
            captured["path"] = Path(path)
            return {"rows": [], "mode": "vision", "warnings": []}

        import istota.health.immunization_ocr as ocr_mod
        monkeypatch.setattr(ocr_mod, "extract_from_file", _fake_extract)

        # Configure a writable temp_dir on the test app state so the route
        # picks it up instead of falling back to gettempdir().
        configured_tmp = tmp_path / "scheduler-tmp"
        configured_tmp.mkdir()

        class _Cfg:
            temp_dir = str(configured_tmp)
            brain = None

        client.app.state.istota_config = _Cfg()

        files = {"file": ("vax.png", b"\x89PNG\r\n\x1a\nbytes", "image/png")}
        resp = client.post(
            "/istota/api/health/immunizations/extract", files=files,
        )
        assert resp.status_code == 200
        assert "path" in captured, "extract_from_file was not invoked"
        # Path must NOT be under uploads_dir, and SHOULD be under temp_dir.
        uploads_parent = Path(ctx.uploads_dir).resolve()
        assert uploads_parent not in captured["path"].resolve().parents
        # ISSUE-397: one level below temp_dir, not the shared root. That is the
        # directory the OCR sandbox binds read-write; the root is bound at no
        # path, so a copy written there is one the model cannot open.
        assert captured["path"].resolve().parent == (
            configured_tmp / ctx.user_id
        ).resolve()


class TestDashboardAndSummary:
    def test_dashboard_has_immunizations(self, client):
        client.post("/istota/api/health/immunizations", json={
            "name": "Influenza", "date_given": "2020-09-01",
        })
        resp = client.get("/istota/api/health/dashboard").json()
        assert "immunizations" in resp
        assert resp["immunizations"]["overdue_count"] >= 1
        assert resp["immunizations"]["last_given"]["name"] == "Influenza"

    def test_history_summary_has_immunizations(self, client):
        client.post("/istota/api/health/immunizations", json={
            "name": "Tdap", "date_given": "2010-01-01",
        })
        resp = client.get("/istota/api/health/history/summary").json()
        assert "immunizations" in resp
        # Tdap from 2010 → overdue (more than 10y).
        action = resp["immunizations"]["action_needed"]
        assert any(c["name"] == "Tdap" for c in action)


class TestExplainerRoute:
    def test_explainer_returned_for_up_to_date(self, client):
        # Status no longer gates the explainer — curated content shows
        # regardless of coverage status.
        client.post("/istota/api/health/immunizations", json={
            "name": "Influenza",
            "date_given": "2026-05-01",
        })
        resp = client.get(
            "/istota/api/health/immunizations/Influenza/explainer",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "static"
        assert body["summary"]
        assert body["why_it_matters"]

    def test_explainer_returns_static_for_actionable_status(self, client):
        client.post("/istota/api/health/immunizations", json={
            "name": "Tdap", "date_given": "2005-01-01",
        })
        resp = client.get("/istota/api/health/immunizations/Tdap/explainer")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "static"
        assert body["status"] == "overdue"
        assert body["summary"]
        assert isinstance(body["why_it_matters"], list)
        assert body["why_it_matters"]
        assert "considerations" not in body
        assert body["disclaimer"]

    def test_explainer_unknown_vaccine_404(self, client):
        resp = client.get(
            "/istota/api/health/immunizations/Notarealvaccine/explainer",
        )
        assert resp.status_code == 404


class TestStaticExplainerData:
    def test_data_loads(self):
        from istota.health.immunization_explainer import _load_explainers

        _load_explainers.cache_clear()
        data = _load_explainers()
        assert data, "bundled explainer JSON should not be empty"
        # Every entry shipped must have non-empty summary + non-empty why bullets.
        for name, entry in data.items():
            assert entry["summary"], f"{name} has empty summary"
            assert entry["why_it_matters"], f"{name} has empty why_it_matters"

    def test_covers_all_refs(self):
        """Every vaccine in the canonical refs should have a curated
        explainer entry — the UI shows the section for all of them now."""
        from istota.health._migrate import _read_bundled_immunization_refs
        from istota.health.immunization_explainer import _load_explainers

        _load_explainers.cache_clear()
        refs = _read_bundled_immunization_refs() or []
        explainers = _load_explainers()
        missing = [r["name"] for r in refs if r["name"] not in explainers]
        assert not missing, f"missing static explainer for: {missing}"


class TestImmunizationImportDocumentAttachment:
    """Proof of immunization is a document, so the upload is kept."""

    def _extract(self, client, monkeypatch, *, rows=None):
        def _fake(path, mime, refs, *, config=None, user_id=""):
            return {
                "rows": rows if rows is not None else [],
                "mode": "vision",
                "warnings": [],
            }

        import istota.health.immunization_ocr as ocr_mod
        monkeypatch.setattr(ocr_mod, "extract_from_file", _fake)
        return client.post(
            "/istota/api/health/immunizations/extract",
            files={"file": ("card.png", b"\x89PNG\r\n\x1a\ncard", "image/png")},
        )

    def test_extract_keeps_the_file_and_returns_its_id(
        self, client, ctx, monkeypatch,
    ):
        resp = self._extract(client, monkeypatch)
        assert resp.status_code == 200, resp.text
        document_id = resp.json()["document_id"]
        assert isinstance(document_id, int)
        with health_db.connect(ctx.db_path) as conn:
            doc = health_db.get_document(conn, document_id)
        assert doc is not None
        assert doc.source == "import"
        assert (ctx.uploads_dir / doc.stored_path).is_file()

    def test_storage_failure_still_returns_the_rows(
        self, client, ctx, monkeypatch,
    ):
        from istota.health import documents as health_documents

        def _boom(*a, **kw):
            raise health_documents.DocumentError("disk on fire")

        monkeypatch.setattr(health_documents, "store_document", _boom)
        resp = self._extract(
            client, monkeypatch,
            rows=[{"name": "Influenza", "date_given": "2026-01-05"}],
        )
        body = resp.json()
        assert body["document_id"] is None
        assert len(body["rows"]) == 1
        assert any("could not be kept" in w for w in body["warnings"])

    def test_bulk_links_the_document_to_every_row(
        self, client, ctx, monkeypatch,
    ):
        document_id = self._extract(client, monkeypatch).json()["document_id"]
        resp = client.post(
            "/istota/api/health/immunizations/bulk",
            json={
                "document_id": document_id,
                "rows": [
                    {"name": "Influenza", "date_given": "2026-01-05"},
                    {"name": "Tetanus", "date_given": "2025-03-11"},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["document_id"] == document_id
        with health_db.connect(ctx.db_path) as conn:
            links = set(health_db.entity_links_for_document(conn, document_id))
        assert links == {("immunization", i) for i in body["ids"]}

    def test_bulk_without_a_document_id_creates_no_links(self, client, ctx):
        client.post(
            "/istota/api/health/immunizations/bulk",
            json={"rows": [{"name": "Influenza", "date_given": "2026-01-05"}]},
        )
        with health_db.connect(ctx.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM document_links",
            ).fetchone()["n"]
        assert n == 0

    def test_unknown_document_id_is_400_and_inserts_nothing(self, client, ctx):
        resp = client.post(
            "/istota/api/health/immunizations/bulk",
            json={
                "document_id": 4242,
                "rows": [{"name": "Influenza", "date_given": "2026-01-05"}],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "document not found"
        with health_db.connect(ctx.db_path) as conn:
            assert health_db.list_immunizations(conn) == []
