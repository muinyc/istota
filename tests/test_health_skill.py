"""Tests for ``istota-skill health`` CLI."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context
from tests.support.monotonic_spy import monotonic_spy
from tests.support.sleep_spy import sleep_spy


def _run(args, env, expect_success=True) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "istota.skills.health", *args],
        capture_output=True, text=True, env=env,
    )
    if expect_success:
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


@pytest.fixture
def ready(tmp_path) -> tuple[Path, dict]:
    ctx = synthesize_health_context("alice", tmp_path / "workspace")
    ensure_initialised(ctx)
    env = {
        **os.environ,
        "HEALTH_DB_PATH": str(ctx.db_path),
        # Direct mode — no deferred dir.
        "ISTOTA_DEFERRED_DIR": "",
        "ISTOTA_TASK_ID": "",
    }
    return ctx.db_path, env


class TestStatsCli:
    def test_log_and_latest(self, ready):
        db_path, env = ready
        _run(["log", "weight", "82.5"], env)
        _run(["log", "weight", "182", "--unit", "lb"], env)
        out = _run(["latest"], env)
        # Latest in kg, lb input converted.
        assert "weight" in out["stats"]
        # 182 lb ≈ 82.554 kg
        assert abs(out["stats"]["weight"]["value"] - 82.554) < 0.1
        assert out["stats"]["weight"]["unit"] == "kg"

    def test_stats_list_filter(self, ready):
        db_path, env = ready
        _run(["log", "weight", "82.5"], env)
        _run(["log", "resting_hr", "60"], env)
        out = _run(["stats", "--metric", "weight"], env)
        assert all(s["metric"] == "weight" for s in out["stats"])


class TestPanelsCli:
    def test_add_panel_and_biomarker(self, ready):
        db_path, env = ready
        out = _run(
            ["add-panel", "--drawn-at", "2026-05-08", "--lab", "Quest", "--type", "CBC"],
            env,
        )
        pid = out["id"]
        _run([
            "add-biomarker", str(pid),
            "Hemoglobin", "14.8", "g/dL",
            "--ref-low", "13.5", "--ref-high", "17.5",
        ], env)
        out = _run(["panel", str(pid)], env)
        assert out["panel"]["lab_name"] == "Quest"
        assert out["biomarkers"][0]["name"] == "Hemoglobin"

    def test_deferred_panel_ref_emission(self, ready, tmp_path):
        """ISSUE-092: in deferred mode, add-panel --ref NAME emits `ref` and
        add-biomarker @NAME emits `panel_ref` (no real panel_id needed)."""
        db_path, env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {**env, "ISTOTA_DEFERRED_DIR": str(deferred), "ISTOTA_TASK_ID": "55"}
        out = _run([
            "add-panel", "--drawn-at", "2026-05-08", "--lab", "Quest",
            "--type", "CBC", "--ref", "cbc",
        ], env)
        assert out["deferred"] is True
        out = _run(["add-biomarker", "@cbc", "Hemoglobin", "14.8", "g/dL"], env)
        assert out["deferred"] is True
        ops = json.loads((deferred / "task_55_health_ops.json").read_text())
        assert ops[0]["op"] == "insert_panel" and ops[0]["ref"] == "cbc"
        assert ops[1]["op"] == "insert_biomarker"
        assert ops[1]["panel_ref"] == "cbc"
        assert "panel_id" not in ops[1]

    def test_panel_ref_direct_mode_errors(self, ready):
        """A @ref is meaningless outside a deferred batch — direct mode rejects
        it with an error envelope instead of crashing or misfiling."""
        db_path, env = ready  # ready = direct mode (no deferred dir)
        out = _run(
            ["add-biomarker", "@cbc", "Hemoglobin", "14.8", "g/dL"],
            env, expect_success=False,
        )
        assert out["status"] == "error"
        assert "deferred" in out["error"]


class TestSettingsCli:
    def test_set_height_parses_imperial(self, ready):
        db_path, env = ready
        _run(["set", "height", "5ft10in"], env)
        out = _run(["settings"], env)
        # 70 in × 2.54 = 177.8 cm
        assert abs(out["settings"]["height_cm"] - 177.8) < 0.1

    def test_display_units_merge(self, ready):
        db_path, env = ready
        _run(["set", "display.weight", "lb"], env)
        _run(["set", "display.temp", "F"], env)
        out = _run(["settings"], env)
        du = out["settings"]["display_units"]
        assert du == {"weight": "lb", "temp": "F"}


class TestCsvCli:
    SAMPLE = (
        ",,MORPHOLOGY,,LIPID PANEL,\n"
        "Date,Lab,Hgb (g/dL),WBC (th/mm3),LDL-C (mg/dL),HDL (mg/dL)\n"
        ",,12.7-16.7,4.8-10.5,20-100,40-60\n"
        "2024-07-27,Kaiser,14.5,6.4,148,51\n"
        "2025-11-28,Kaiser,14.6,6.1,148,55\n"
    )

    def test_import_then_export_roundtrip(self, ready, tmp_path):
        db_path, env = ready
        src = tmp_path / "bw.csv"
        src.write_text(self.SAMPLE)
        out = _run(["import-csv", str(src)], env)
        assert out["status"] == "ok"
        assert out["panels_created"] == 2
        assert out["biomarkers_created"] == 8

        # `--output` is a host path and this CLI runs host-side, so it is
        # scoped to the caller's own workspace. The mount below is what the
        # executor would have put in the environment.
        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        env = {
            **env,
            "NEXTCLOUD_MOUNT_PATH": str(mount),
            "ISTOTA_USER_ID": "alice",
        }
        export_path = mount / "Users" / "alice" / "out.csv"
        result = _run(["export-csv", "-o", str(export_path)], env)
        assert result["status"] == "ok"
        assert export_path.exists()
        # 3 header rows + 2 data rows
        assert len(export_path.read_text().strip().splitlines()) == 5

    def test_import_deferred(self, ready, tmp_path):
        db_path, env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {
            **env,
            "ISTOTA_DEFERRED_DIR": str(deferred),
            "ISTOTA_TASK_ID": "7",
        }
        src = tmp_path / "bw.csv"
        src.write_text(self.SAMPLE)
        out = _run(["import-csv", str(src)], env)
        assert out["deferred"] is True
        ops_file = deferred / "task_7_health_ops.json"
        ops = json.loads(ops_file.read_text())
        assert ops[0]["op"] == "import_csv"
        assert ops[0]["source_path"] == str(src)


class TestDeferredMode:
    def test_writes_to_deferred_file(self, ready, tmp_path):
        db_path, env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {
            **env,
            "ISTOTA_DEFERRED_DIR": str(deferred),
            "ISTOTA_TASK_ID": "42",
        }
        out = _run(["log", "weight", "82.5"], env)
        assert out["deferred"] is True
        ops_file = deferred / "task_42_health_ops.json"
        assert ops_file.exists()
        ops = json.loads(ops_file.read_text())
        assert ops[0]["op"] == "insert_stat"
        assert ops[0]["metric"] == "weight"
        assert ops[0]["value"] == 82.5


class TestEncountersCli:
    def test_add_list_and_show(self, ready):
        _db, env = ready
        out = _run(
            [
                "add-encounter", "--date", "2026-05-13", "--type", "procedure",
                "--provider", "Dr. Smith", "--specialty", "gastroenterology",
                "--notes", "Clean colonoscopy",
            ],
            env,
        )
        eid = out["id"]
        listing = _run(["encounters"], env)
        assert listing["encounters"][0]["provider"] == "Dr. Smith"
        detail = _run(["encounter", str(eid)], env)
        assert detail["encounter"]["specialty"] == "gastroenterology"
        assert detail["diagnoses"] == []

    def test_filter_by_type(self, ready):
        _db, env = ready
        _run(["add-encounter", "--date", "2026-01-01", "--type", "visit"], env)
        _run(
            ["add-encounter", "--date", "2026-05-13", "--type", "procedure"],
            env,
        )
        listing = _run(["encounters", "--type", "procedure"], env)
        assert [e["encounter_type"] for e in listing["encounters"]] == [
            "procedure",
        ]

    def test_update_encounter(self, ready):
        _db, env = ready
        eid = _run(
            ["add-encounter", "--date", "2026-05-13", "--type", "visit"], env,
        )["id"]
        _run(
            ["update-encounter", str(eid), "--notes", "Follow-up in 3 years"],
            env,
        )
        detail = _run(["encounter", str(eid)], env)
        assert detail["encounter"]["notes"] == "Follow-up in 3 years"

    def test_delete_encounter(self, ready):
        _db, env = ready
        eid = _run(
            ["add-encounter", "--date", "2026-05-13", "--type", "visit"], env,
        )["id"]
        _run(["delete-encounter", str(eid)], env)
        listing = _run(["encounters"], env)
        assert listing["encounters"] == []

    def test_deferred_writes(self, ready, tmp_path):
        _db, env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {
            **env,
            "ISTOTA_DEFERRED_DIR": str(deferred),
            "ISTOTA_TASK_ID": "11",
        }
        out = _run(
            ["add-encounter", "--date", "2026-05-13", "--type", "procedure",
             "--provider", "Dr. Smith"],
            env,
        )
        assert out["deferred"] is True
        ops = json.loads(
            (deferred / "task_11_health_ops.json").read_text(),
        )
        assert ops[0]["op"] == "insert_encounter"
        assert ops[0]["provider"] == "Dr. Smith"


class TestDiagnosesCli:
    def test_add_list_filter(self, ready):
        _db, env = ready
        _run(
            [
                "add-diagnosis", "Internal hemorrhoids",
                "--date-diagnosed", "2026-05-13", "--icd10", "K64.0",
                "--severity", "mild",
            ],
            env,
        )
        _run(
            ["add-diagnosis", "Hypertension", "--status", "chronic"],
            env,
        )
        actives = _run(["diagnoses", "--status", "active"], env)
        assert [d["name"] for d in actives["diagnoses"]] == [
            "Internal hemorrhoids",
        ]

    def test_resolve_shorthand(self, ready):
        _db, env = ready
        did = _run(
            ["add-diagnosis", "Hemorrhoids", "--date-diagnosed", "2026-05-13"],
            env,
        )["id"]
        out = _run(
            ["resolve-diagnosis", str(did), "--date", "2026-06-15"], env,
        )
        assert out["date_resolved"] == "2026-06-15"
        detail = _run(["diagnosis", str(did)], env)
        assert detail["diagnosis"]["status"] == "resolved"
        assert detail["diagnosis"]["date_resolved"] == "2026-06-15"

    def test_linked_encounter(self, ready):
        _db, env = ready
        eid = _run(
            ["add-encounter", "--date", "2026-05-13", "--type", "procedure"],
            env,
        )["id"]
        did = _run(
            ["add-diagnosis", "Hemorrhoids", "--encounter-id", str(eid)],
            env,
        )["id"]
        detail = _run(["diagnosis", str(did)], env)
        assert [e["id"] for e in detail["encounters"]] == [eid]
        assert detail["diagnosis"]["encounter_ids"] == [eid]

    def test_link_and_unlink_several_encounters(self, ready):
        """A condition seen by a GP and a specialist keeps both visits."""
        _db, env = ready
        gp = _run(
            ["add-encounter", "--date", "2026-06-02", "--type", "visit"], env,
        )["id"]
        spec = _run(
            ["add-encounter", "--date", "2026-07-14", "--type", "visit"], env,
        )["id"]
        did = _run(
            ["add-diagnosis", "Anemia", "--encounter-id", str(gp)], env,
        )["id"]

        _run(["link-encounter", str(did), "--encounter", str(spec)], env)
        detail = _run(["diagnosis", str(did)], env)
        assert [e["id"] for e in detail["encounters"]] == [spec, gp]

        _run(["unlink-encounter", str(did), "--encounter", str(gp)], env)
        detail = _run(["diagnosis", str(did)], env)
        assert [e["id"] for e in detail["encounters"]] == [spec]

    def test_link_rejects_unknown_encounter(self, ready):
        _db, env = ready
        did = _run(["add-diagnosis", "Anemia"], env)["id"]
        out = _run(
            ["link-encounter", str(did), "--encounter", "9999"],
            env, expect_success=False,
        )
        assert out["status"] == "error"

    def test_listing_carries_encounter_ids(self, ready):
        _db, env = ready
        eid = _run(
            ["add-encounter", "--date", "2026-06-02", "--type", "visit"], env,
        )["id"]
        _run(["add-diagnosis", "Anemia", "--encounter-id", str(eid)], env)
        _run(["add-diagnosis", "Eczema"], env)
        rows = _run(["diagnoses", "--status", "all"], env)["diagnoses"]
        by_name = {d["name"]: d for d in rows}
        assert by_name["Anemia"]["encounter_ids"] == [eid]
        assert by_name["Eczema"]["encounter_ids"] == []


class TestHistorySummaryCli:
    def test_summary(self, ready):
        _db, env = ready
        eid = _run(
            ["add-encounter", "--date", "2026-05-13", "--type", "procedure"],
            env,
        )["id"]
        _run(["add-diagnosis", "Hypertension", "--status", "chronic"], env)
        _run(
            ["add-diagnosis", "Hemorrhoids", "--encounter-id", str(eid)],
            env,
        )
        summary = _run(["history-summary"], env)
        assert [d["name"] for d in summary["active_diagnoses"]] == [
            "Hemorrhoids",
        ]
        assert [d["name"] for d in summary["chronic_diagnoses"]] == [
            "Hypertension",
        ]
        assert [e["encounter_type"] for e in summary["recent_procedures"]] == [
            "procedure",
        ]


# ---------------------------------------------------------------------------
# Garmin sync routing: direct vs delegated
# ---------------------------------------------------------------------------


def _garmin_args(days_back: int = 7) -> argparse.Namespace:
    return argparse.Namespace(days_back=days_back)


class TestGarminSyncRouting:
    """``cmd_garmin_sync`` picks direct mode (engine runs inline) when
    ``ISTOTA_SECRET_KEY`` is available, and delegated mode (enqueue
    skill-task + poll) otherwise. The delegated path exists because the
    Fernet master key is intentionally not propagated to subprocesses —
    instead, the scheduler's in-daemon short-circuit
    (``_run_garmin_sync_inprocess``) does the work. See
    ``Skill proxy execution model and the master-key boundary`` in
    project notes."""

    def test_direct_mode_when_key_available(self, monkeypatch):
        from istota.skills.health import cmd_garmin_sync

        with patch(
            "istota.secrets_store.secret_key_available", return_value=True,
        ), patch(
            "istota.skills.health._cmd_garmin_sync_direct",
        ) as direct, patch(
            "istota.skills.health._cmd_garmin_sync_delegated",
        ) as delegated:
            cmd_garmin_sync(_garmin_args())
        direct.assert_called_once()
        delegated.assert_not_called()

    def test_delegated_mode_when_key_missing(self, monkeypatch):
        from istota.skills.health import cmd_garmin_sync

        with patch(
            "istota.secrets_store.secret_key_available", return_value=False,
        ), patch(
            "istota.skills.health._cmd_garmin_sync_direct",
        ) as direct, patch(
            "istota.skills.health._cmd_garmin_sync_delegated",
        ) as delegated:
            cmd_garmin_sync(_garmin_args())
        delegated.assert_called_once()
        direct.assert_not_called()


class TestGarminSyncDelegated:
    """The delegated path enqueues a ``skill="health"`` task, sets
    ``max_attempts=1``, polls until the scheduler transitions it to a
    terminal state, then surfaces the engine's JSON payload."""

    def _env(self, monkeypatch, db_path: Path, **overrides):
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        for k, v in overrides.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)

    def _patch_poll(self, monkeypatch):
        """Skip ``time.sleep`` and advance ``time.monotonic`` in fixed steps so
        the polling loop terminates quickly.

        Both go through the thread-bounded helpers (ISSUE-411): the skill
        imports ``time`` inside the function, so the stdlib module is the only
        handle, and an unbounded patch would hand a scheduler thread a stepped
        clock and a no-op wait for the length of the test.
        """
        monotonic_state = {"t": 0.0}

        def fake_monotonic():
            monotonic_state["t"] += 0.1
            return monotonic_state["t"]

        sleep_spy(monkeypatch, time, record=False)
        monotonic_spy(monkeypatch, time, fake_monotonic)

    def test_success_path_emits_payload(self, db_path, monkeypatch, capsys):
        from istota.skills.health import _cmd_garmin_sync_delegated
        self._env(monkeypatch, db_path)

        def fake_get_task(conn, task_id):
            return db.Task(
                id=task_id, status="completed",
                source_type="cli", user_id="alice", prompt="",
                result='{"status":"ok","inserted":5,"days_processed":3,"auth_error":false}',
            )

        self._patch_poll(monkeypatch)
        with patch("istota.db.get_task", side_effect=fake_get_task):
            _cmd_garmin_sync_delegated(_garmin_args())

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["status"] == "ok"
        assert payload["inserted"] == 5
        assert payload["days_processed"] == 3

        # Confirm the task was actually inserted with the right args + max_attempts=1.
        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT skill, skill_args, max_attempts, source_type, user_id "
                "FROM tasks ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row["skill"] == "health"
        assert json.loads(row["skill_args"]) == ["garmin-sync", "--days-back", "7"]
        assert row["max_attempts"] == 1
        assert row["source_type"] == "cli"
        assert row["user_id"] == "alice"

    def test_failed_task_surfaces_error(self, db_path, monkeypatch, capsys):
        from istota.skills.health import _cmd_garmin_sync_delegated
        self._env(monkeypatch, db_path)

        def fake_get_task(conn, task_id):
            return db.Task(
                id=task_id, status="failed",
                source_type="cli", user_id="alice", prompt="",
                error="garmin sync: token_expired",
            )

        self._patch_poll(monkeypatch)
        with patch("istota.db.get_task", side_effect=fake_get_task):
            with pytest.raises(SystemExit) as exc_info:
                _cmd_garmin_sync_delegated(_garmin_args())
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["status"] == "error"
        assert "token_expired" in payload["error"]

    def test_cancelled_task_surfaces_error(self, db_path, monkeypatch, capsys):
        from istota.skills.health import _cmd_garmin_sync_delegated
        self._env(monkeypatch, db_path)

        def fake_get_task(conn, task_id):
            return db.Task(
                id=task_id, status="cancelled",
                source_type="cli", user_id="alice", prompt="",
            )

        self._patch_poll(monkeypatch)
        with patch("istota.db.get_task", side_effect=fake_get_task):
            with pytest.raises(SystemExit):
                _cmd_garmin_sync_delegated(_garmin_args())
        out = capsys.readouterr().out
        assert "cancelled" in json.loads(out)["error"]

    def test_timeout_when_task_stuck_pending(self, db_path, monkeypatch, capsys):
        """If the scheduler isn't running (or is overloaded), the CLI
        gives up after ``_GARMIN_SYNC_DELEGATED_POLL_TIMEOUT_S`` and
        surfaces a clear scheduler-may-not-be-running message rather
        than hanging forever."""
        from istota.skills.health import _cmd_garmin_sync_delegated
        self._env(monkeypatch, db_path)

        def fake_get_task(conn, task_id):
            return db.Task(
                id=task_id, status="pending",
                source_type="cli", user_id="alice", prompt="",
            )

        # Force monotonic to jump past the timeout on the very first call
        # after start, so the loop exits without sleeping in real time.
        monotonic_state = {"calls": 0}

        def fake_monotonic():
            monotonic_state["calls"] += 1
            return 1000.0 if monotonic_state["calls"] >= 2 else 0.0

        sleep_spy(monkeypatch, time, record=False)
        monotonic_spy(monkeypatch, time, fake_monotonic)
        with patch("istota.db.get_task", side_effect=fake_get_task):
            with pytest.raises(SystemExit):
                _cmd_garmin_sync_delegated(_garmin_args())
        out = capsys.readouterr().out
        assert "scheduler may not be running" in json.loads(out)["error"]

    def test_missing_user_id_fails_loud(self, db_path, monkeypatch, capsys):
        from istota.skills.health import _cmd_garmin_sync_delegated
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))

        with pytest.raises(SystemExit):
            _cmd_garmin_sync_delegated(_garmin_args())
        out = capsys.readouterr().out
        assert "ISTOTA_USER_ID" in json.loads(out)["error"]

    def test_missing_db_path_fails_with_web_ui_hint(
        self, tmp_path, monkeypatch, capsys,
    ):
        """No master key AND no DB to enqueue against — there's nothing
        the CLI can do. Tell the operator about /garmin/sync explicitly
        so the failure mode is recoverable."""
        from istota.skills.health import _cmd_garmin_sync_delegated
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)

        with pytest.raises(SystemExit):
            _cmd_garmin_sync_delegated(_garmin_args())
        out = capsys.readouterr().out
        err = json.loads(out)["error"]
        assert "ISTOTA_SECRET_KEY" in err
        assert "/garmin/sync" in err

    def test_readonly_db_falls_through_with_web_ui_hint(
        self, db_path, monkeypatch, capsys,
    ):
        """Sandboxed callers (LLM Bash through bwrap) see EROFS when they
        try to enqueue — the DB is mounted read-only inside the sandbox.
        The CLI must surface this as a "use the web UI" message, not
        crash with a stack trace."""
        from istota.skills.health import _cmd_garmin_sync_delegated
        self._env(monkeypatch, db_path)

        with patch(
            "istota.db.get_db",
            side_effect=sqlite3.OperationalError("attempt to write a readonly database"),
        ):
            with pytest.raises(SystemExit):
                _cmd_garmin_sync_delegated(_garmin_args())
        out = capsys.readouterr().out
        err = json.loads(out)["error"]
        assert "readonly" in err.lower() or "Sandboxed" in err
        assert "/garmin/sync" in err


class TestDocumentsCli:
    def _paperwork(self, tmp_path, name="discharge.pdf") -> Path:
        p = tmp_path / name
        p.write_bytes(b"%PDF-1.4 discharge summary")
        return p

    def test_attach_and_list_direct(self, ready, tmp_path):
        db_path, env = ready
        from istota.health import db as health_db

        with health_db.connect(db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()

        src = self._paperwork(tmp_path)
        out = _run(
            ["attach-document", "--path", str(src), "--to", f"encounter:{eid}"],
            env,
        )
        assert out["status"] == "ok"
        assert out["created"] is True
        assert out["filename"] == "discharge.pdf"

        with health_db.connect(db_path) as conn:
            docs = health_db.documents_for_entity(conn, "encounter", eid)
        assert [d.id for d in docs] == [out["id"]]
        assert docs[0].source == "agent"

        listed = _run(["documents", "--entity", f"encounter:{eid}"], env)
        assert [d["id"] for d in listed["documents"]] == [out["id"]]

        detail = _run(["document", str(out["id"])], env)
        assert detail["links"] == [
            {"entity_type": "encounter", "entity_id": eid},
        ]

    def test_detach_direct(self, ready, tmp_path):
        db_path, env = ready
        from istota.health import db as health_db

        with health_db.connect(db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()
        src = self._paperwork(tmp_path)
        did = _run(
            ["attach-document", "--path", str(src), "--to", f"encounter:{eid}"],
            env,
        )["id"]
        out = _run(
            ["detach-document", str(did), "--from", f"encounter:{eid}"], env,
        )
        assert out == {"status": "ok", "removed": True}
        with health_db.connect(db_path) as conn:
            assert health_db.documents_for_entity(conn, "encounter", eid) == []

    def test_attach_defers_under_sandbox(self, ready, tmp_path):
        """Sandboxed, the health DB is read-only — the write must defer."""
        db_path, env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {**env, "ISTOTA_DEFERRED_DIR": str(deferred), "ISTOTA_TASK_ID": "77"}
        src = self._paperwork(tmp_path)

        out = _run(
            ["attach-document", "--path", str(src), "--to", "immunization:5",
             "--notes", "Pharmacy record"],
            env,
        )
        assert out["deferred"] is True
        ops = json.loads((deferred / "task_77_health_ops.json").read_text())
        assert ops == [{
            "op": "attach_document",
            "source_path": str(src),
            "filename": "discharge.pdf",
            "entity_type": "immunization",
            "notes": "Pharmacy record",
            "entity_id": 5,
        }]

    def test_encounter_ref_defers_as_a_ref(self, ready, tmp_path):
        db_path, env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {**env, "ISTOTA_DEFERRED_DIR": str(deferred), "ISTOTA_TASK_ID": "78"}
        src = self._paperwork(tmp_path)

        _run(
            ["add-encounter", "--date", "2026-06-29", "--type", "visit",
             "--ref", "visit"],
            env,
        )
        _run(
            ["attach-document", "--path", str(src), "--to", "encounter:@visit"],
            env,
        )
        ops = json.loads((deferred / "task_78_health_ops.json").read_text())
        assert ops[0]["ref"] == "visit"
        assert ops[1]["encounter_ref"] == "visit"
        assert "entity_id" not in ops[1]

    def test_detach_defers_under_sandbox(self, ready, tmp_path):
        db_path, env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {**env, "ISTOTA_DEFERRED_DIR": str(deferred), "ISTOTA_TASK_ID": "79"}
        out = _run(["detach-document", "3", "--from", "diagnosis:8"], env)
        assert out["deferred"] is True
        ops = json.loads((deferred / "task_79_health_ops.json").read_text())
        assert ops == [{
            "op": "detach_document",
            "document_id": 3,
            "entity_type": "diagnosis",
            "entity_id": 8,
        }]

    def test_missing_file_is_an_error(self, ready, tmp_path):
        db_path, env = ready
        out = _run(
            ["attach-document", "--path", str(tmp_path / "nope.pdf"),
             "--to", "encounter:1"],
            env, expect_success=False,
        )
        assert out["status"] == "error"
        assert "file not found" in out["error"]

    @pytest.mark.parametrize("ref,fragment", [
        ("panel:1", "unknown entity type"),
        ("encounter:abc", "must be an integer"),
        ("encounter", "expected TYPE:ID"),
        ("", "expected TYPE:ID"),
        ("diagnosis:@x", "only supported for encounter"),
    ])
    def test_bad_entity_ref_is_an_error(self, ready, tmp_path, ref, fragment):
        db_path, env = ready
        src = self._paperwork(tmp_path)
        out = _run(
            ["attach-document", "--path", str(src), "--to", ref],
            env, expect_success=False,
        )
        assert out["status"] == "error"
        assert fragment in out["error"]
