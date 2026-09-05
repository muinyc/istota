"""Tests for the per-user health SQLite layer."""

import sqlite3
from datetime import datetime, timezone

import pytest

from istota.health import db as health_db
from istota.health.workspace import synthesize_health_context
from istota.health._migrate import ensure_initialised


def _ctx(tmp_path):
    return synthesize_health_context("alice", tmp_path / "workspace")


class TestInitDb:
    def test_creates_tables(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {
            "stats", "panels", "biomarkers", "biomarker_refs",
            "health_settings", "schema_meta",
            "encounters", "diagnoses",
        } <= tables

    def test_idempotent(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        health_db.init_db(ctx.db_path)

    def test_records_schema_version(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
        assert row["value"] == str(health_db.SCHEMA_VERSION)

    def test_migrates_pre_content_hash_db(self, tmp_path):
        """A DB created before the content_hash column must migrate cleanly.

        Regression for prod 500s where executescript hit
        ``CREATE INDEX … ON panels(content_hash)`` before the migration's
        ALTER on the existing panels table.
        """
        import sqlite3

        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        # Materialise an older panels table without the content_hash column.
        conn = sqlite3.connect(ctx.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE panels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drawn_at TEXT NOT NULL,
                    lab_name TEXT,
                    panel_type TEXT,
                    source_file TEXT,
                    source_mime TEXT,
                    ocr_text TEXT,
                    draft INTEGER NOT NULL DEFAULT 0,
                    notes TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO panels (drawn_at) VALUES ('2026-05-01T00:00:00+00:00');
                """,
            )
            conn.commit()
        finally:
            conn.close()
        # This used to raise sqlite3.OperationalError: no such column.
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(panels)")}
            assert "content_hash" in cols
            indices = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            assert "idx_panels_content_hash" in indices
            # Pre-existing rows survive with a NULL hash.
            row = conn.execute("SELECT content_hash FROM panels").fetchone()
            assert row["content_hash"] is None


class TestStats:
    def test_insert_and_list(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            id_a = health_db.insert_stat(
                conn, metric="weight", value=82.5, unit="kg",
                measured_at="2026-05-01T10:00:00+00:00",
            )
            id_b = health_db.insert_stat(
                conn, metric="weight", value=82.0, unit="kg",
                measured_at="2026-05-08T10:00:00+00:00",
            )
            health_db.insert_stat(
                conn, metric="resting_hr", value=62, unit="bpm",
                measured_at="2026-05-08T10:00:00+00:00",
            )
            conn.commit()
            rows = health_db.list_stats(conn, metric="weight")
        assert [r.id for r in rows] == [id_b, id_a]
        assert rows[0].value == pytest.approx(82.0)

    def test_latest_per_metric(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            health_db.insert_stat(
                conn, metric="weight", value=83.0, unit="kg",
                measured_at="2026-05-01T10:00:00+00:00",
            )
            health_db.insert_stat(
                conn, metric="weight", value=82.0, unit="kg",
                measured_at="2026-05-08T10:00:00+00:00",
            )
            health_db.insert_stat(
                conn, metric="resting_hr", value=60, unit="bpm",
                measured_at="2026-05-08T10:00:00+00:00",
            )
            conn.commit()
            latest = health_db.latest_stats(conn)
        assert set(latest.keys()) == {"weight", "resting_hr"}
        assert latest["weight"].value == pytest.approx(82.0)
        assert latest["resting_hr"].value == pytest.approx(60.0)

    def test_delete_stat(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            sid = health_db.insert_stat(
                conn, metric="weight", value=82.0, unit="kg",
            )
            conn.commit()
            n = health_db.delete_stat(conn, sid)
            conn.commit()
        assert n == 1


class TestPanelsAndBiomarkers:
    def test_panel_lifecycle(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-08", lab_name="Quest",
                panel_type="CBC",
            )
            health_db.insert_biomarker(
                conn, panel_id=pid, name="Hemoglobin",
                value=15.0, unit="g/dL",
                ref_range_low=13.5, ref_range_high=17.5,
            )
            health_db.insert_biomarker(
                conn, panel_id=pid, name="WBC",
                value=12.5, unit="10^3/uL",
                ref_range_low=4.0, ref_range_high=11.0,
                flag="H",
            )
            conn.commit()

            panel = health_db.get_panel(conn, pid)
            assert panel is not None
            assert panel.lab_name == "Quest"

            biomarkers = health_db.list_biomarkers_for_panel(conn, pid)
            assert len(biomarkers) == 2

            total, flagged = health_db.panel_counts(conn, pid)
            assert (total, flagged) == (2, 1)

            # CASCADE
            health_db.delete_panel(conn, pid)
            conn.commit()
            assert health_db.list_biomarkers_for_panel(conn, pid) == []

    def test_panel_collision(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            health_db.insert_panel(
                conn, drawn_at="2026-05-08", lab_name="Quest",
            )
            conn.commit()
            hit = health_db.find_panel_collision(
                conn, drawn_at="2026-05-08", lab_name="Quest",
            )
            miss = health_db.find_panel_collision(
                conn, drawn_at="2026-05-08", lab_name="Kaiser",
            )
        assert hit is not None
        assert miss is None

    def test_biomarker_trend_excludes_drafts(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            p1 = health_db.insert_panel(
                conn, drawn_at="2026-01-01", lab_name="Quest", draft=False,
            )
            p2 = health_db.insert_panel(
                conn, drawn_at="2026-05-01", lab_name="Quest", draft=True,
            )
            health_db.insert_biomarker(
                conn, panel_id=p1, name="LDL", value=110, unit="mg/dL",
            )
            health_db.insert_biomarker(
                conn, panel_id=p2, name="LDL", value=88, unit="mg/dL",
            )
            conn.commit()
            trend = health_db.biomarker_trend(conn, name="LDL")
        assert [d for _, d in trend] == ["2026-01-01"]
        assert trend[0][0].value == pytest.approx(110)

    def test_replace_biomarkers(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(conn, drawn_at="2026-05-08")
            health_db.insert_biomarker(
                conn, panel_id=pid, name="WBC", value=8, unit="10^3/uL",
            )
            n = health_db.replace_biomarkers(conn, pid, [
                {"name": "WBC", "value": 9.0, "unit": "10^3/uL"},
                {"name": "RBC", "value": 5.0, "unit": "10^6/uL"},
            ])
            conn.commit()
        assert n == 2
        with health_db.connect(ctx.db_path) as conn:
            rows = health_db.list_biomarkers_for_panel(conn, pid)
        assert {r.name for r in rows} == {"WBC", "RBC"}


class TestBiomarkerRefs:
    def test_seed_idempotent(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            refs1 = health_db.list_biomarker_refs(conn)
        assert any(r.name == "Hemoglobin" for r in refs1)
        # Second call must not duplicate.
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            refs2 = health_db.list_biomarker_refs(conn)
        assert len(refs1) == len(refs2)

    def test_find_by_alias(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            ref = health_db.find_biomarker_ref_by_alias(conn, "Hgb")
        assert ref is not None
        assert ref.name == "Hemoglobin"

    def test_sex_specific_ranges_present(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            hgb = health_db.get_biomarker_ref(conn, "Hemoglobin")
        assert hgb is not None
        assert hgb.ref_range_low_m is not None
        assert hgb.ref_range_low_f is not None
        assert hgb.ref_range_low_m != hgb.ref_range_low_f


class TestRecanonicalize:
    def test_rewrites_alias_to_canonical(self, tmp_path):
        # ensure_initialised must rewrite biomarker rows that match an
        # alias of a canonical ref. Regression for the CSV-import path
        # that stored raw column names before the alias table caught up.
        from istota.health._migrate import recanonicalize_biomarker_names

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-08", lab_name="Quest", draft=False,
            )
            # Insert a row under a raw alias name (NOT canonical).
            bid = health_db.insert_biomarker(
                conn, panel_id=pid, name="Cholesterol",
                value=180, unit="mg/dL",
            )
            conn.commit()
            # Force a re-run: clear the recanon sentinel.
            conn.execute(
                "DELETE FROM schema_meta WHERE key = 'biomarker_recanonicalize_hash'",
            )
            conn.commit()

        fixed = recanonicalize_biomarker_names(ctx)
        assert fixed == 1
        with health_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT name FROM biomarkers WHERE id = ?", (bid,),
            ).fetchone()
        assert row["name"] == "Cholesterol_Total"

        # Idempotent — second call is a no-op.
        assert recanonicalize_biomarker_names(ctx) == 0


class TestSettings:
    def test_roundtrip(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            health_db.set_setting(conn, "dob", "1985-03-12")
            health_db.set_setting(conn, "height_cm", 178)
            health_db.set_setting(
                conn, "display_units",
                {"weight": "lb", "height": "cm", "temp": "F"},
            )
            conn.commit()
            settings = health_db.get_settings(conn)
        assert settings["dob"] == "1985-03-12"
        assert settings["height_cm"] == 178
        assert settings["display_units"]["weight"] == "lb"


class TestEncounters:
    def test_insert_and_get(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn,
                encounter_date="2026-05-13",
                encounter_type="procedure",
                provider="Dr. Smith",
                facility="Riverside Clinic",
                specialty="gastroenterology",
                reason="Screening colonoscopy",
                notes="Grade I-II hemorrhoids found.",
            )
            conn.commit()
            enc = health_db.get_encounter(conn, eid)
        assert enc is not None
        assert enc.encounter_type == "procedure"
        assert enc.provider == "Dr. Smith"
        assert enc.facility == "Riverside Clinic"

    def test_list_filters(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = health_db.insert_encounter(
                conn, encounter_date="2026-01-15", encounter_type="visit",
            )
            b = health_db.insert_encounter(
                conn, encounter_date="2026-05-13", encounter_type="procedure",
            )
            health_db.insert_encounter(
                conn, encounter_date="2025-09-01", encounter_type="screening",
            )
            conn.commit()
            recent = health_db.list_encounters(conn, since="2026-01-01")
            procs = health_db.list_encounters(conn, encounter_type="procedure")
        assert [e.id for e in recent] == [b, a]
        assert [e.id for e in procs] == [b]

    def test_update_encounter(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure", notes="initial",
            )
            n = health_db.update_encounter(
                conn, eid, notes="Follow-up in 3 years",
                facility="Kaiser",
            )
            conn.commit()
        assert n == 1
        with health_db.connect(ctx.db_path) as conn:
            enc = health_db.get_encounter(conn, eid)
        assert enc.notes == "Follow-up in 3 years"
        assert enc.facility == "Kaiser"

    def test_update_encounter_clears_nullable_fields(self, tmp_path):
        # Explicit None on nullable fields must actually clear them.
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
                provider="Dr Smith", facility="Kaiser",
                specialty="GI", reason="screening", notes="initial",
            )
            n = health_db.update_encounter(
                conn, eid, provider=None, facility=None,
                specialty=None, reason=None, notes=None,
            )
            conn.commit()
        assert n == 1
        with health_db.connect(ctx.db_path) as conn:
            enc = health_db.get_encounter(conn, eid)
        assert enc.provider is None
        assert enc.facility is None
        assert enc.specialty is None
        assert enc.reason is None
        assert enc.notes is None

    def test_update_encounter_rejects_none_required(self, tmp_path):
        # encounter_date / encounter_type are NOT NULL; explicit None is a no-op.
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            # Only None for required fields → 0 rows changed.
            n = health_db.update_encounter(
                conn, eid, encounter_date=None, encounter_type=None,
            )
            conn.commit()
        assert n == 0
        with health_db.connect(ctx.db_path) as conn:
            enc = health_db.get_encounter(conn, eid)
        assert enc.encounter_date == "2026-05-13"
        assert enc.encounter_type == "procedure"

    def test_delete_clears_panel_fk(self, tmp_path):
        # SET NULL on encounter delete must propagate to panels.encounter_id.
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-13", lab_name="Quest",
                encounter_id=eid,
            )
            conn.commit()
            health_db.delete_encounter(conn, eid)
            conn.commit()
            panel = health_db.get_panel(conn, pid)
        assert panel is not None
        assert panel.encounter_id is None

    def test_panels_for_encounter(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="visit",
            )
            p1 = health_db.insert_panel(
                conn, drawn_at="2026-05-13",
                lab_name="Quest", encounter_id=eid,
            )
            health_db.insert_panel(
                conn, drawn_at="2026-05-13", lab_name="Kaiser",
            )
            conn.commit()
            linked = health_db.panels_for_encounter(conn, eid)
        assert [p.id for p in linked] == [p1]


class TestDiagnoses:
    def test_insert_and_status_filter(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            active = health_db.insert_diagnosis(
                conn, name="Internal hemorrhoids",
                date_diagnosed="2026-05-13", severity="mild",
                icd10="K64.0",
            )
            chronic = health_db.insert_diagnosis(
                conn, name="Hypertension", status="chronic",
                date_diagnosed="2020-01-15",
            )
            resolved = health_db.insert_diagnosis(
                conn, name="Strep throat", status="resolved",
                date_diagnosed="2024-12-01",
                date_resolved="2024-12-15",
            )
            conn.commit()
            actives = health_db.list_diagnoses(conn, status="active")
            chronics = health_db.list_diagnoses(conn, status="chronic")
            all_d = health_db.list_diagnoses(conn, status="all")
        assert [d.id for d in actives] == [active]
        assert [d.id for d in chronics] == [chronic]
        # default ordering: active → chronic → resolved
        assert [d.id for d in all_d] == [active, chronic, resolved]

    def test_unknown_status_rejected(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(ValueError):
                health_db.insert_diagnosis(conn, name="X", status="bogus")

    def test_update_marks_resolved(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = health_db.insert_diagnosis(
                conn, name="Hemorrhoids", date_diagnosed="2026-05-13",
            )
            health_db.update_diagnosis(
                conn, did, status="resolved", date_resolved="2026-06-15",
            )
            conn.commit()
            d = health_db.get_diagnosis(conn, did)
        assert d.status == "resolved"
        assert d.date_resolved == "2026-06-15"

    def test_diagnoses_for_encounter(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            d_linked = health_db.insert_diagnosis(
                conn, name="Hemorrhoids", encounter_id=eid,
                date_diagnosed="2026-05-13",
            )
            health_db.insert_diagnosis(
                conn, name="Unrelated",
                date_diagnosed="2024-01-01",
            )
            conn.commit()
            linked = health_db.diagnoses_for_encounter(conn, eid)
            up = health_db.encounters_for_diagnosis(conn, d_linked)
        assert [d.id for d in linked] == [d_linked]
        assert [e.id for e in up] == [eid]

    def test_delete_encounter_sets_diagnosis_fk_null(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            did = health_db.insert_diagnosis(
                conn, name="Hemorrhoids", encounter_id=eid,
            )
            conn.commit()
            health_db.delete_encounter(conn, eid)
            conn.commit()
            d = health_db.get_diagnosis(conn, did)
        assert d.encounter_id is None


class TestDiagnosisEncounterLinks:
    """Many-to-many between diagnoses and encounters.

    A condition is routinely seen by several people — GP, then a specialist,
    then a follow-up — so the link is a set, not a scalar. ``diagnoses.
    encounter_id`` survives as an unread legacy column; ``diagnosis_encounters``
    is the source of truth.
    """

    def _encounter(self, conn, date, kind="visit"):
        return health_db.insert_encounter(
            conn, encounter_date=date, encounter_type=kind,
        )

    def test_links_several_encounters_to_one_diagnosis(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            gp = self._encounter(conn, "2026-06-02")
            spec = self._encounter(conn, "2026-07-14")
            followup = self._encounter(conn, "2026-09-01")
            did = health_db.insert_diagnosis(conn, name="Iron deficiency anemia")
            for eid in (gp, spec, followup):
                health_db.link_diagnosis_encounter(conn, did, eid)
            conn.commit()
            linked = health_db.encounters_for_diagnosis(conn, did)
        # Newest encounter first, matching how encounters list elsewhere.
        assert [e.id for e in linked] == [followup, spec, gp]

    def test_link_is_idempotent(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = self._encounter(conn, "2026-06-02")
            did = health_db.insert_diagnosis(conn, name="Asthma")
            assert health_db.link_diagnosis_encounter(conn, did, eid) is True
            assert health_db.link_diagnosis_encounter(conn, did, eid) is False
            conn.commit()
            assert len(health_db.encounters_for_diagnosis(conn, did)) == 1

    def test_unlink_leaves_the_other_links(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            gp = self._encounter(conn, "2026-06-02")
            spec = self._encounter(conn, "2026-07-14")
            did = health_db.insert_diagnosis(conn, name="Asthma")
            health_db.link_diagnosis_encounter(conn, did, gp)
            health_db.link_diagnosis_encounter(conn, did, spec)
            conn.commit()
            assert health_db.unlink_diagnosis_encounter(conn, did, gp) == 1
            conn.commit()
            assert [e.id for e in health_db.encounters_for_diagnosis(conn, did)] == [spec]
            # The condition itself is untouched by an unlink.
            assert health_db.get_diagnosis(conn, did) is not None

    def test_one_encounter_carries_several_diagnoses(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = self._encounter(conn, "2026-06-02")
            a = health_db.insert_diagnosis(conn, name="Anemia")
            b = health_db.insert_diagnosis(conn, name="Asthma")
            health_db.insert_diagnosis(conn, name="Unrelated")
            health_db.link_diagnosis_encounter(conn, a, eid)
            health_db.link_diagnosis_encounter(conn, b, eid)
            conn.commit()
            linked = health_db.diagnoses_for_encounter(conn, eid)
        assert {d.id for d in linked} == {a, b}

    def test_insert_with_encounter_id_creates_a_link(self, tmp_path):
        """The legacy scalar argument is shorthand for one link."""
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = self._encounter(conn, "2026-06-02")
            did = health_db.insert_diagnosis(
                conn, name="Anemia", encounter_id=eid,
            )
            conn.commit()
            assert [e.id for e in health_db.encounters_for_diagnosis(conn, did)] == [eid]
            assert [d.id for d in health_db.diagnoses_for_encounter(conn, eid)] == [did]

    def test_set_replaces_the_whole_set(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = self._encounter(conn, "2026-06-02")
            b = self._encounter(conn, "2026-07-14")
            c = self._encounter(conn, "2026-09-01")
            did = health_db.insert_diagnosis(conn, name="Anemia", encounter_id=a)
            health_db.set_diagnosis_encounters(conn, did, [b, c])
            conn.commit()
            assert {e.id for e in health_db.encounters_for_diagnosis(conn, did)} == {b, c}

    def test_set_to_empty_clears_every_link(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = self._encounter(conn, "2026-06-02")
            did = health_db.insert_diagnosis(conn, name="Anemia", encounter_id=eid)
            health_db.set_diagnosis_encounters(conn, did, [])
            conn.commit()
            assert health_db.encounters_for_diagnosis(conn, did) == []

    def test_deleting_an_encounter_drops_only_its_own_links(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            gp = self._encounter(conn, "2026-06-02")
            spec = self._encounter(conn, "2026-07-14")
            did = health_db.insert_diagnosis(conn, name="Anemia")
            health_db.link_diagnosis_encounter(conn, did, gp)
            health_db.link_diagnosis_encounter(conn, did, spec)
            conn.commit()
            health_db.delete_encounter(conn, gp)
            conn.commit()
            assert [e.id for e in health_db.encounters_for_diagnosis(conn, did)] == [spec]

    def test_deleting_a_diagnosis_drops_its_links(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = self._encounter(conn, "2026-06-02")
            did = health_db.insert_diagnosis(conn, name="Anemia", encounter_id=eid)
            conn.commit()
            health_db.delete_diagnosis(conn, did)
            conn.commit()
            assert health_db.diagnoses_for_encounter(conn, eid) == []
            rows = conn.execute(
                "SELECT COUNT(*) FROM diagnosis_encounters WHERE diagnosis_id = ?",
                (did,),
            ).fetchone()[0]
        assert rows == 0

    def test_encounter_ids_for_diagnoses_batches(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = self._encounter(conn, "2026-06-02")
            b = self._encounter(conn, "2026-07-14")
            one = health_db.insert_diagnosis(conn, name="Anemia")
            two = health_db.insert_diagnosis(conn, name="Asthma")
            unlinked = health_db.insert_diagnosis(conn, name="Eczema")
            health_db.link_diagnosis_encounter(conn, one, a)
            health_db.link_diagnosis_encounter(conn, one, b)
            health_db.link_diagnosis_encounter(conn, two, b)
            conn.commit()
            got = health_db.encounter_ids_for_diagnoses(conn, [one, two, unlinked])
        assert got[one] == [b, a]   # newest encounter first
        assert got[two] == [b]
        assert unlinked not in got  # absent rather than an empty list

    def test_unknown_encounter_is_rejected(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = health_db.insert_diagnosis(conn, name="Anemia")
            with pytest.raises(sqlite3.IntegrityError):
                health_db.link_diagnosis_encounter(conn, did, 9999)


class TestDiagnosisEncounterBackfill:
    def test_backfills_the_legacy_scalar_column(self, tmp_path):
        """A v3 DB's single links become join rows exactly once."""
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-02", encounter_type="visit",
            )
            did = health_db.insert_diagnosis(
                conn, name="Anemia", encounter_id=eid,
            )
            # Rewind to the pre-migration state: join table empty, version 3.
            conn.execute("DELETE FROM diagnosis_encounters")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', '3')",
            )
            conn.commit()

        health_db.init_db(ctx.db_path)

        with health_db.connect(ctx.db_path) as conn:
            assert [e.id for e in health_db.encounters_for_diagnosis(conn, did)] == [eid]

    def test_backfill_does_not_resurrect_a_deliberate_unlink(self, tmp_path):
        """Re-running init_db at the current version must not re-add links.

        The legacy column is left populated on migrated rows, so a backfill
        that ran on every open would undo the user's next unlink.
        """
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-02", encounter_type="visit",
            )
            did = health_db.insert_diagnosis(
                conn, name="Anemia", encounter_id=eid,
            )
            health_db.unlink_diagnosis_encounter(conn, did, eid)
            conn.commit()

        health_db.init_db(ctx.db_path)

        with health_db.connect(ctx.db_path) as conn:
            assert health_db.encounters_for_diagnosis(conn, did) == []


class TestDiagnosisReconcile:
    """Content-based reconciliation for import/agent diagnosis writes.

    ``reconcile=True`` merges a clinically-equivalent condition into the
    existing row instead of inserting a duplicate. Identity is ICD10-first
    (authoritative when both rows carry a code), normalized-name fallback.
    """

    def test_same_name_from_two_sources_merges(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            first = health_db.insert_diagnosis(
                conn, name="Hypertension", status="chronic",
                date_diagnosed="2020-01-15", reconcile=True,
            )
            # A second source names it slightly differently (case/space/punct).
            second = health_db.insert_diagnosis(
                conn, name="  hypertension. ", status="active",
                date_diagnosed="2022-06-01", reconcile=True,
            )
            conn.commit()
            all_d = health_db.list_diagnoses(conn, status="all")
        assert first == second
        assert len(all_d) == 1

    def test_matching_icd10_merges_despite_name_difference(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = health_db.insert_diagnosis(
                conn, name="Essential hypertension", icd10="I10",
                reconcile=True,
            )
            b = health_db.insert_diagnosis(
                conn, name="HTN", icd10="i10", reconcile=True,
            )
            conn.commit()
            all_d = health_db.list_diagnoses(conn, status="all")
        assert a == b
        assert len(all_d) == 1

    def test_different_icd10_stays_distinct_even_if_name_matches(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = health_db.insert_diagnosis(
                conn, name="Diabetes", icd10="E11.9", reconcile=True,
            )
            b = health_db.insert_diagnosis(
                conn, name="Diabetes", icd10="E10.9", reconcile=True,
            )
            conn.commit()
            all_d = health_db.list_diagnoses(conn, status="all")
        assert a != b
        assert len(all_d) == 2

    def test_merge_backfills_null_fields_only(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            first = health_db.insert_diagnosis(
                conn, name="Hypertension", status="chronic",
                severity=None, reconcile=True,
            )
            # Second source supplies icd10 + severity the first lacked, but a
            # different (non-null) status must not overwrite the existing one.
            health_db.insert_diagnosis(
                conn, name="Hypertension", status="active",
                icd10="I10", severity="moderate", reconcile=True,
            )
            conn.commit()
            d = health_db.get_diagnosis(conn, first)
        assert d.icd10 == "I10"          # backfilled (was null)
        assert d.severity == "moderate"  # backfilled (was null)
        assert d.status == "chronic"     # preserved (was already set)

    def test_reconcile_off_by_default_still_duplicates(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            health_db.insert_diagnosis(conn, name="Hypertension")
            health_db.insert_diagnosis(conn, name="Hypertension")
            conn.commit()
            all_d = health_db.list_diagnoses(conn, status="all")
        assert len(all_d) == 2

    def test_dedup_key_replay_still_returns_same_row(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = health_db.insert_diagnosis(
                conn, name="Asthma", dedup_key="imp:0:dx:0", reconcile=True,
            )
            b = health_db.insert_diagnosis(
                conn, name="Asthma", dedup_key="imp:0:dx:0", reconcile=True,
            )
            conn.commit()
            all_d = health_db.list_diagnoses(conn, status="all")
        assert a == b
        assert len(all_d) == 1


class TestImmunizationReconcile:
    """Content-based reconciliation for immunization import writes.

    Keyed on normalized name + date_given: a re-import of the same shot on
    the same day merges, but a genuine booster on another date stays a
    distinct row.
    """

    def test_same_vaccine_same_date_merges(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-10-01",
                source="import", reconcile=True,
            )
            b = health_db.insert_immunization(
                conn, name="  influenza ", date_given="2025-10-01",
                source="import", reconcile=True,
            )
            conn.commit()
            rows = health_db.list_immunizations(conn)
        assert a == b
        assert len(rows) == 1

    def test_same_vaccine_different_date_stays_distinct(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = health_db.insert_immunization(
                conn, name="Influenza", date_given="2024-10-01",
                source="import", reconcile=True,
            )
            b = health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-10-01",
                source="import", reconcile=True,
            )
            conn.commit()
            rows = health_db.list_immunizations(conn)
        assert a != b
        assert len(rows) == 2

    def test_merge_backfills_null_fields_only(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            first = health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-10-01",
                lot_number=None, manufacturer="Sanofi",
                source="import", reconcile=True,
            )
            health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-10-01",
                lot_number="ABC123", manufacturer="Pfizer",
                source="import", reconcile=True,
            )
            conn.commit()
            imm = health_db.get_immunization(conn, first)
        assert imm.lot_number == "ABC123"      # backfilled (was null)
        assert imm.manufacturer == "Sanofi"    # preserved (was already set)


class TestDeferredEncounterReplay:
    def test_replay_inserts(self, tmp_path):
        """The scheduler replays deferred encounter/diagnosis ops."""
        import json
        from unittest.mock import MagicMock

        from istota import health as health_pkg
        from istota import scheduler_deferred

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        user_temp = tmp_path / "user_temp"
        user_temp.mkdir()
        ops_file = user_temp / "task_55_health_ops.json"
        ops_file.write_text(json.dumps([
            {
                "op": "insert_encounter",
                "encounter_date": "2026-05-13",
                "encounter_type": "procedure",
                "provider": "Dr. Smith",
            },
            {
                "op": "insert_diagnosis",
                "name": "Hemorrhoids",
                "encounter_id": 1,
                "date_diagnosed": "2026-05-13",
            },
        ]))
        fake_resolve = MagicMock(return_value=ctx)
        original = health_pkg.resolve_for_user
        health_pkg.resolve_for_user = fake_resolve
        try:
            count = scheduler_deferred._process_deferred_health_ops(
                MagicMock(), MagicMock(id=55, user_id="alice"), user_temp,
            )
        finally:
            health_pkg.resolve_for_user = original
        assert count == 2
        with health_db.connect(ctx.db_path) as conn:
            encs = health_db.list_encounters(conn)
            diags = health_db.list_diagnoses(conn)
        assert [e.provider for e in encs] == ["Dr. Smith"]
        assert [d.name for d in diags] == ["Hemorrhoids"]
        assert diags[0].encounter_id == encs[0].id

    def test_replay_update_and_delete(self, tmp_path):
        import json
        from unittest.mock import MagicMock

        from istota import health as health_pkg
        from istota import scheduler_deferred

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            did = health_db.insert_diagnosis(
                conn, name="Hemorrhoids",
                date_diagnosed="2026-05-13",
            )
            conn.commit()
        user_temp = tmp_path / "user_temp"
        user_temp.mkdir()
        ops_file = user_temp / "task_99_health_ops.json"
        ops_file.write_text(json.dumps([
            {
                "op": "update_diagnosis",
                "diagnosis_id": did,
                "status": "resolved",
                "date_resolved": "2026-06-15",
            },
            {
                "op": "update_encounter",
                "encounter_id": eid,
                "notes": "Follow-up in 3 years",
            },
            {"op": "delete_encounter", "encounter_id": eid},
        ]))
        fake_resolve = MagicMock(return_value=ctx)
        original = health_pkg.resolve_for_user
        health_pkg.resolve_for_user = fake_resolve
        try:
            count = scheduler_deferred._process_deferred_health_ops(
                MagicMock(), MagicMock(id=99, user_id="alice"), user_temp,
            )
        finally:
            health_pkg.resolve_for_user = original
        assert count == 3
        with health_db.connect(ctx.db_path) as conn:
            assert health_db.get_encounter(conn, eid) is None
            d = health_db.get_diagnosis(conn, did)
        assert d.status == "resolved"
        assert d.date_resolved == "2026-06-15"
        # encounter_id should have been NULLed by ON DELETE SET NULL
        assert d.encounter_id is None

    def test_replay_failure_writes_sidecar(self, tmp_path):
        """A bad op must surface as an ERROR + a failure sidecar file."""
        import json
        from unittest.mock import MagicMock

        from istota import health as health_pkg
        from istota import scheduler_deferred

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        user_temp = tmp_path / "user_temp"
        user_temp.mkdir()
        ops_file = user_temp / "task_88_health_ops.json"
        # Bad op: missing required encounter_date.
        ops_file.write_text(json.dumps([
            {"op": "insert_encounter", "encounter_type": "procedure"},
        ]))
        fake_resolve = MagicMock(return_value=ctx)
        original = health_pkg.resolve_for_user
        health_pkg.resolve_for_user = fake_resolve
        try:
            scheduler_deferred._process_deferred_health_ops(
                MagicMock(), MagicMock(id=88, user_id="alice"), user_temp,
            )
        finally:
            health_pkg.resolve_for_user = original
        sidecar = user_temp / "task_88_health_op_failures.json"
        assert sidecar.exists()
        payload = json.loads(sidecar.read_text())
        assert len(payload) == 1
        assert payload[0]["op"]["op"] == "insert_encounter"
        assert "encounter_date" in payload[0]["error"] or "KeyError" in payload[0]["error"]

    def test_replay_is_idempotent_on_dedup_key(self, tmp_path):
        """Replaying the same insert op twice must not duplicate the row."""
        import json
        from unittest.mock import MagicMock

        from istota import health as health_pkg
        from istota import scheduler_deferred

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        user_temp = tmp_path / "user_temp"
        user_temp.mkdir()
        ops_payload = [
            {
                "op": "insert_encounter",
                "dedup_key": "deadbeef",
                "encounter_date": "2026-05-13",
                "encounter_type": "procedure",
                "provider": "Dr. Smith",
            },
            {
                "op": "insert_diagnosis",
                "dedup_key": "cafef00d",
                "name": "Hemorrhoids",
                "date_diagnosed": "2026-05-13",
            },
        ]
        fake_resolve = MagicMock(return_value=ctx)
        original = health_pkg.resolve_for_user
        health_pkg.resolve_for_user = fake_resolve
        try:
            for _ in range(2):
                ops_file = user_temp / "task_77_health_ops.json"
                ops_file.write_text(json.dumps(ops_payload))
                scheduler_deferred._process_deferred_health_ops(
                    MagicMock(), MagicMock(id=77, user_id="alice"), user_temp,
                )
        finally:
            health_pkg.resolve_for_user = original
        with health_db.connect(ctx.db_path) as conn:
            encs = health_db.list_encounters(conn)
            diags = health_db.list_diagnoses(conn)
        assert len(encs) == 1
        assert len(diags) == 1


class TestPanelEncounterMigration:
    def test_migrates_pre_encounter_db(self, tmp_path):
        """A panels table created before encounter_id must migrate cleanly."""
        import sqlite3

        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        conn = sqlite3.connect(ctx.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE panels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drawn_at TEXT NOT NULL,
                    lab_name TEXT,
                    panel_type TEXT,
                    source_file TEXT,
                    source_mime TEXT,
                    ocr_text TEXT,
                    draft INTEGER NOT NULL DEFAULT 0,
                    notes TEXT,
                    content_hash TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO panels (drawn_at) VALUES ('2026-05-01T00:00:00+00:00');
                """,
            )
            conn.commit()
        finally:
            conn.close()
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(panels)")}
            assert "encounter_id" in cols
            row = conn.execute("SELECT encounter_id FROM panels").fetchone()
            assert row["encounter_id"] is None

    def test_panel_insert_with_encounter_id(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="visit",
            )
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-13", lab_name="Quest",
                encounter_id=eid,
            )
            conn.commit()
            panel = health_db.get_panel(conn, pid)
        assert panel.encounter_id == eid

    def test_update_panel_clears_encounter_id(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="visit",
            )
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-13", encounter_id=eid,
            )
            conn.commit()
            health_db.update_panel(conn, pid, encounter_id=None)
            conn.commit()
            panel = health_db.get_panel(conn, pid)
        assert panel.encounter_id is None


class TestDocuments:
    def _doc(self, conn, *, content_hash="h1", filename="a.pdf"):
        return health_db.insert_document(
            conn, filename=filename, mime="application/pdf", byte_size=10,
            content_hash=content_hash,
            stored_path=f"documents/x/{filename}",
        )

    def test_init_creates_document_tables(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"documents", "document_links"} <= tables

    def test_existing_db_picks_up_the_tables_without_a_migration(self, tmp_path):
        """A pre-documents DB gains both tables on the next init_db.

        Both are brand new, so `CREATE TABLE IF NOT EXISTS` inside
        executescript covers it — no migration function needed.
        """

        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-01-01", encounter_type="visit",
            )
            conn.execute("DROP TABLE document_links")
            conn.execute("DROP TABLE documents")
            conn.commit()

        health_db.init_db(ctx.db_path)

        with health_db.connect(ctx.db_path) as conn:
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name='documents'"
            ).fetchone() is not None
            # Pre-existing rows untouched.
            assert health_db.get_encounter(conn, eid) is not None

    def test_link_and_unlink_are_idempotent(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = self._doc(conn)
            assert health_db.link_document(conn, did, "encounter", 1) is True
            assert health_db.link_document(conn, did, "encounter", 1) is False
            assert health_db.unlink_document(conn, did, "encounter", 1) == 1
            assert health_db.unlink_document(conn, did, "encounter", 1) == 0

    def test_documents_for_entity_is_type_scoped(self, tmp_path):
        """encounter 1 and diagnosis 1 share a numeric id and must not bleed."""
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            enc_doc = self._doc(conn, content_hash="h-enc", filename="e.pdf")
            dx_doc = self._doc(conn, content_hash="h-dx", filename="d.pdf")
            health_db.link_document(conn, enc_doc, "encounter", 1)
            health_db.link_document(conn, dx_doc, "diagnosis", 1)
            enc = health_db.documents_for_entity(conn, "encounter", 1)
            dx = health_db.documents_for_entity(conn, "diagnosis", 1)
        assert [d.id for d in enc] == [enc_doc]
        assert [d.id for d in dx] == [dx_doc]

    def test_documents_for_entity_is_newest_first(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            first = self._doc(conn, content_hash="h1", filename="a.pdf")
            second = self._doc(conn, content_hash="h2", filename="b.pdf")
            conn.execute(
                "UPDATE documents SET created_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00+00:00", first),
            )
            conn.execute(
                "UPDATE documents SET created_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00+00:00", second),
            )
            health_db.link_document(conn, first, "encounter", 1)
            health_db.link_document(conn, second, "encounter", 1)
            docs = health_db.documents_for_entity(conn, "encounter", 1)
        assert [d.id for d in docs] == [second, first]

    def test_counts_for_entities(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = self._doc(conn, content_hash="ha", filename="a.pdf")
            b = self._doc(conn, content_hash="hb", filename="b.pdf")
            health_db.link_document(conn, a, "encounter", 1)
            health_db.link_document(conn, b, "encounter", 1)
            health_db.link_document(conn, a, "encounter", 2)
            counts = health_db.document_counts_for_entities(
                conn, "encounter", [1, 2, 3],
            )
        assert counts == {1: 2, 2: 1}

    def test_counts_for_entities_empty_list(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            assert health_db.document_counts_for_entities(
                conn, "encounter", [],
            ) == {}

    def test_counts_chunk_past_the_sqlite_variable_limit(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = self._doc(conn)
            health_db.link_document(conn, did, "encounter", 600)
            counts = health_db.document_counts_for_entities(
                conn, "encounter", range(1, 601),
            )
        assert counts == {600: 1}

    @pytest.mark.parametrize("fn,args", [
        ("documents_for_entity", (1,)),
        ("document_counts_for_entities", ([1],)),
        ("link_document", ("panel", 1)),
        ("unlink_document", ("panel", 1)),
        ("unlink_entity_documents", (1,)),
    ])
    def test_entity_type_is_validated(self, tmp_path, fn, args):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            func = getattr(health_db, fn)
            with pytest.raises(ValueError, match="unknown entity type"):
                if fn in ("link_document", "unlink_document"):
                    func(conn, 1, *args)
                else:
                    func(conn, "panel", *args)

    def test_delete_document_clears_links(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = self._doc(conn)
            health_db.link_document(conn, did, "encounter", 1)
            assert health_db.delete_document(conn, did) == 1
            assert health_db.entity_links_for_document(conn, did) == []

    def test_entity_deletes_clear_links(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = self._doc(conn)
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-01-01", encounter_type="visit",
            )
            dxid = health_db.insert_diagnosis(conn, name="Asthma")
            iid = health_db.insert_immunization(
                conn, name="Influenza", date_given="2026-01-05",
            )
            health_db.link_document(conn, did, "encounter", eid)
            health_db.link_document(conn, did, "diagnosis", dxid)
            health_db.link_document(conn, did, "immunization", iid)

            health_db.delete_encounter(conn, eid)
            health_db.delete_diagnosis(conn, dxid)
            health_db.delete_immunization(conn, iid)

            assert health_db.entity_links_for_document(conn, did) == []
            # The document itself survives — it may be linked elsewhere later.
            assert health_db.get_document(conn, did) is not None

    def test_orphan_ids_respect_the_window_and_links(self, tmp_path):
        from datetime import timedelta

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            old = self._doc(conn, content_hash="ho", filename="o.pdf")
            fresh = self._doc(conn, content_hash="hf", filename="f.pdf")
            linked = self._doc(conn, content_hash="hl", filename="l.pdf")
            health_db.link_document(conn, linked, "encounter", 1)
            stale = (
                datetime.now(timezone.utc) - timedelta(hours=48)
            ).isoformat()
            for d in (old, linked):
                conn.execute(
                    "UPDATE documents SET created_at = ?, last_touched_at = ? "
                    "WHERE id = ?",
                    (stale, stale, d),
                )
            ids = health_db.orphan_document_ids(conn, older_than_hours=24)
        assert ids == [old]
        assert fresh not in ids


class TestBulkDocumentLinks:
    """The two bulk readers behind the Documents table (ISSUE-423).

    A table showing every document's associations is N+1 against the per-row
    readers — `entity_links_for_document` plus one `get_*` per link — so both
    of these exist for the reason `document_counts_for_entities` does.
    """

    def _doc(self, conn, *, content_hash="h1", filename="a.pdf"):
        return health_db.insert_document(
            conn, filename=filename, mime="application/pdf", byte_size=10,
            content_hash=content_hash,
            stored_path=f"documents/x/{filename}",
        )

    def test_links_for_documents_groups_by_document(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = self._doc(conn, content_hash="ha", filename="a.pdf")
            b = self._doc(conn, content_hash="hb", filename="b.pdf")
            unlinked = self._doc(conn, content_hash="hu", filename="u.pdf")
            health_db.link_document(conn, a, "encounter", 1)
            health_db.link_document(conn, a, "diagnosis", 7)
            health_db.link_document(conn, b, "immunization", 3)
            out = health_db.entity_links_for_documents(conn, [a, b, unlinked])
        assert out[a] == [("diagnosis", 7), ("encounter", 1)]
        assert out[b] == [("immunization", 3)]
        # A linkless document is absent, as in document_counts_for_entities.
        assert unlinked not in out

    def test_links_for_documents_matches_the_single_reader(self, tmp_path):
        """The bulk and per-row readers must not drift on ordering."""
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = self._doc(conn)
            health_db.link_document(conn, did, "immunization", 2)
            health_db.link_document(conn, did, "encounter", 9)
            health_db.link_document(conn, did, "encounter", 4)
            bulk = health_db.entity_links_for_documents(conn, [did])
            single = health_db.entity_links_for_document(conn, did)
        assert bulk[did] == single

    def test_links_for_documents_empty_list(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            assert health_db.entity_links_for_documents(conn, []) == {}

    def test_links_for_documents_chunks_past_the_variable_limit(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = self._doc(conn)
            health_db.link_document(conn, did, "encounter", 1)
            # More ids than SQLITE_MAX_VARIABLE_NUMBER allows in one statement,
            # with the real document last so a single-chunk read misses it.
            out = health_db.entity_links_for_documents(
                conn, [*range(did + 1, did + 900), did],
            )
        assert out == {did: [("encounter", 1)]}

    def test_links_for_documents_tolerates_a_repeated_id(self, tmp_path):
        """The accumulator appends, so a duplicate must not double the list.

        Reachable only across a chunk boundary today, which is why the ids are
        placed one on each side of it rather than adjacent.
        """
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = self._doc(conn)
            health_db.link_document(conn, did, "encounter", 1)
            padding = list(range(did + 1, did + 501))
            out = health_db.entity_links_for_documents(
                conn, [did, *padding, did],
            )
        assert out == {did: [("encounter", 1)]}

    def test_entities_by_id_returns_typed_records(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            dxid = health_db.insert_diagnosis(conn, name="Anaemia")
            iid = health_db.insert_immunization(
                conn, name="Tetanus", date_given="2026-02-02",
            )
            encounters = health_db.entities_by_id(conn, "encounter", [eid, 999])
            diagnoses = health_db.entities_by_id(conn, "diagnosis", [dxid])
            immunizations = health_db.entities_by_id(
                conn, "immunization", [iid],
            )
        assert encounters[eid].encounter_type == "visit"
        # An id with no row is absent rather than mapped to None: the caller
        # falls back to a generic label instead of unpacking a null.
        assert 999 not in encounters
        assert diagnoses[dxid].name == "Anaemia"
        assert immunizations[iid].name == "Tetanus"

    def test_entities_by_id_empty_and_unknown_type(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            assert health_db.entities_by_id(conn, "encounter", []) == {}
            with pytest.raises(ValueError):
                health_db.entities_by_id(conn, "panel", [1])

    def test_the_entity_maps_cover_every_declared_type(self):
        """A type added to the tuple and not to both maps is a request-time
        KeyError, which is why the module refuses to import in that state."""
        assert set(health_db._ENTITY_TABLES) == set(
            health_db.DOCUMENT_ENTITY_TYPES,
        )
        assert set(health_db._ENTITY_ROW_CONVERTERS) == set(
            health_db.DOCUMENT_ENTITY_TYPES,
        )

    def test_entities_by_id_chunks_past_the_variable_limit(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            out = health_db.entities_by_id(
                conn, "encounter", [*range(eid + 1, eid + 900), eid],
            )
        assert list(out) == [eid]
