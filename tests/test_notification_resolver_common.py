"""What the six notification sources hand to the store, asserted verbatim.

`notification_resolvers/_common.py` collapsed four mechanical bodies that were
identical in every source bar a noun. The stage that did it declares no
behaviour change, so the pin is a behaviour-equivalence test rather than a
structural one: each source's ``write`` is driven with the store stubbed out and
the captured keyword arguments compared against the exact set it passed before
the extraction. A helper that quietly dropped ``purpose`` or turned an absent
``params`` into ``{}`` would pass every structural check and change what lands
in the table.

Two properties get their own cases because they are the ones a future edit is
most likely to lose:

- ``task_alert`` passes **no** ``object_type``, ``object_id`` or ``link`` on any
  path. Its module docstring commits to there being no branch that could emit a
  URL, and ``row_kwargs`` deliberately offers no ``link`` parameter at all.
- a malformed ``object_id`` is still logged under the *source's* own logger, so
  a warning about a confirmation row is attributed to the confirmation source
  rather than to the shared file.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from istota import notification_store as store
from istota.notification_resolvers import (
    _common,
    confirmation,
    connected_service,
    cron_job,
    health_panel,
    outbound_draft,
    task_alert,
)
from istota.notification_sources import NotificationRow

SOURCE_MODULES = [
    confirmation,
    connected_service,
    cron_job,
    health_panel,
    outbound_draft,
    task_alert,
]


def _row(**kwargs) -> NotificationRow:
    base = {
        "id": 7,
        "user_id": "alice",
        "source": "confirmation",
        "dedup_key": "task:1",
        "object_type": "task",
        "object_id": "1",
        "severity": "warning",
        "actionable": True,
        "title": "t",
        "body": "b",
    }
    base.update(kwargs)
    return NotificationRow(**base)


@pytest.fixture
def captured(monkeypatch):
    """Stub the store and hand back what each ``write`` asked it for."""
    calls: list[dict] = []

    def _write_notification(conn, user_id, **kwargs):
        calls.append({"conn": conn, "user_id": user_id, **kwargs})
        return None

    monkeypatch.setattr(store, "write_notification", _write_notification)
    return calls


class TestTheWriteArgumentsAreUnchanged:
    """One case per source, spelling out the whole argument set."""

    def test_confirmation(self, captured):
        confirmation.write(
            "CONN", "alice", task_id=12, title="Held", body="why", room_token="tok",
        )
        assert captured == [{
            "conn": "CONN", "user_id": "alice",
            "source": "confirmation",
            "dedup_key": "task:12",
            "title": "Held",
            "body": "why",
            "severity": "warning",
            "actionable": True,
            "object_type": "task",
            "object_id": "12",
            "params": None,
            "room_token": "tok",
            "purpose": "alert",
        }]

    def test_connected_service(self, captured):
        connected_service.write("CONN", "alice", service="garmin", reason="401")
        assert captured == [{
            "conn": "CONN", "user_id": "alice",
            "source": "connected_service",
            "dedup_key": "service:garmin",
            "title": connected_service.title_for("garmin"),
            "body": connected_service.body_for("garmin", "401"),
            "severity": "warning",
            "actionable": True,
            "object_type": "secret",
            "object_id": "garmin",
            "params": {"service": "garmin", "reason": "401"},
            "room_token": None,
            "purpose": "alert",
        }]

    def test_cron_job(self, captured):
        cron_job.write(
            "CONN", "alice",
            job_id=3, job_name="nightly", fail_count=5,
            cron_expression="0 3 * * *", last_error="boom", room_token="tok",
        )
        assert captured == [{
            "conn": "CONN", "user_id": "alice",
            "source": "cron_job",
            "dedup_key": "job:3",
            "title": cron_job.title_for("nightly", 5),
            "body": cron_job.body_for("nightly", "0 3 * * *", "boom"),
            "severity": "warning",
            "actionable": True,
            "object_type": "scheduled_job",
            "object_id": "3",
            "params": {"job_name": "nightly", "failures": 5},
            "room_token": "tok",
            "purpose": "alert",
        }]

    def test_health_panel(self, captured):
        health_panel.write(
            "CONN", "alice", panel_id=4, drawn_at="2026-01-02", lab_name="Acme",
        )
        assert captured == [{
            "conn": "CONN", "user_id": "alice",
            "source": "health_panel",
            "dedup_key": "panel:4",
            "title": health_panel.title_for("2026-01-02", "Acme"),
            "body": health_panel.body_for("2026-01-02", "Acme"),
            "severity": "info",
            "actionable": True,
            "object_type": "health_panel",
            "object_id": "4",
            "params": {"drawn_at": "2026-01-02", "lab_name": "Acme"},
            "room_token": None,
            "purpose": "alert",
        }]

    def test_outbound_draft(self, captured):
        outbound_draft.write(
            "CONN", "alice", draft_id=9, title="Reply", body="subject",
            room_token="tok",
        )
        assert captured == [{
            "conn": "CONN", "user_id": "alice",
            "source": "outbound_draft",
            "dedup_key": "draft:9",
            "title": "Reply",
            "body": "subject",
            "severity": "warning",
            "actionable": True,
            "object_type": "draft",
            "object_id": "9",
            "params": None,
            "room_token": "tok",
            "purpose": "alert",
        }]

    def test_task_alert(self, captured):
        task_alert.write(
            "CONN", "alice",
            dedup_key="throttle:mail", title="Held mail", body="one line",
            severity="info", actionable=False, params={"kind": "mail"},
        )
        assert captured == [{
            "conn": "CONN", "user_id": "alice",
            "source": "task_alert",
            "dedup_key": "throttle:mail",
            "title": "Held mail",
            "body": "one line",
            "severity": "info",
            "actionable": False,
            "object_type": None,
            "object_id": None,
            "params": {"kind": "mail"},
            "room_token": None,
            "purpose": "alert",
        }]

    def test_task_alert_points_at_no_object_and_carries_no_link(self, captured):
        """The one source that must never emit a URL, on every path.

        Checked as a property rather than as part of the case above, since the
        case above would still pass if the fields arrived carrying something.
        """
        task_alert.write("CONN", "alice", dedup_key="dmarc:fail", title="Canary")
        sent = captured[0]
        assert sent["object_type"] is None
        assert sent["object_id"] is None
        assert "link" not in sent

    def test_row_kwargs_offers_no_link_parameter(self):
        with pytest.raises(TypeError):
            _common.row_kwargs(
                source="s", dedup_key="k", title="t", severity="info",
                link="https://example.invalid/",
            )


class TestTheClose:
    """``resolve_for`` answers exactly what each source's own call did."""

    @pytest.fixture
    def closed(self, monkeypatch):
        calls: list[tuple] = []
        monkeypatch.setattr(
            store, "resolve_by_object",
            lambda *args, **kwargs: calls.append((args, kwargs)) or 1,
        )
        return calls

    def test_confirmation(self, closed):
        assert confirmation.resolve_for_task("CONN", "alice", 12, by="user") == 1
        assert closed == [(("CONN", "alice", "confirmation", "task", "12"),
                           {"by": "user"})]

    def test_connected_service(self, closed):
        connected_service.resolve_for_service("CONN", "alice", "garmin", by="sync")
        assert closed == [(("CONN", "alice", "connected_service", "secret", "garmin"),
                           {"by": "sync"})]

    def test_cron_job(self, closed):
        cron_job.resolve_for_job("CONN", "alice", 3, by="run")
        assert closed == [(("CONN", "alice", "cron_job", "scheduled_job", "3"),
                           {"by": "run"})]

    def test_health_panel(self, closed):
        health_panel.resolve_for_panel("CONN", "alice", 4, by="confirm")
        assert closed == [(("CONN", "alice", "health_panel", "health_panel", "4"),
                           {"by": "confirm"})]

    def test_outbound_draft(self, closed):
        outbound_draft.resolve_for_draft("CONN", "alice", 9, by="send")
        assert closed == [(("CONN", "alice", "outbound_draft", "draft", "9"),
                           {"by": "send"})]


class TestTheCoercion:
    @pytest.mark.parametrize("raw,expected", [
        ("12", 12),
        (" 12 ", 12),
        (12, 12),
        ("0", 0),
        ("-3", -3),
        ("1/../../admin/x", None),
        ("", None),
        (None, None),
        ("abc", None),
    ])
    def test_the_shared_coercion(self, raw, expected):
        got = _common.coerce_object_id(
            _row(object_id=raw), noun="task", logger=logging.getLogger("x"),
        )
        assert got == expected

    @pytest.mark.parametrize("raw,expected", [("4", 4), ("0", None), ("-1", None)])
    def test_positive_refuses_zero_and_below(self, raw, expected):
        got = _common.coerce_object_id(
            _row(object_id=raw), noun="panel",
            logger=logging.getLogger("x"), positive=True,
        )
        assert got == expected

    @pytest.mark.parametrize("module,fn,noun", [
        (confirmation, "_task_id", "task"),
        (cron_job, "_job_id", "job"),
        (outbound_draft, "_draft_id", "draft"),
        (health_panel, "_panel_id", "panel"),
    ])
    def test_each_source_logs_under_its_own_name(self, module, fn, noun, caplog):
        with caplog.at_level(logging.WARNING):
            assert getattr(module, fn)(_row(object_id="nope")) is None
        record = caplog.records[-1]
        assert record.name == module.__name__
        assert record.getMessage() == (
            f"notification 7 names a non-numeric {noun} id 'nope'"
        )

    def test_the_impossible_panel_message_is_unchanged(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert health_panel._panel_id(_row(object_id="0")) is None
        record = caplog.records[-1]
        assert record.name == health_panel.__name__
        assert record.getMessage() == (
            "notification 7 names an impossible panel id '0'"
        )


class TestTheGuard:
    """No source may grow its own copy of what ``_common`` now owns."""

    def _package(self) -> Path:
        return (
            Path(__file__).resolve().parents[1]
            / "src" / "istota" / "notification_resolvers"
        )

    def _called_names(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def _sources(self) -> dict[str, Path]:
        return {
            module.__name__.rsplit(".", 1)[-1]:
                self._package() / f"{module.__name__.rsplit('.', 1)[-1]}.py"
            for module in SOURCE_MODULES
        }

    def test_none_of_them_calls_resolve_by_object(self):
        offenders = [
            name for name, path in self._sources().items()
            if "resolve_by_object" in self._called_names(path)
        ]
        assert offenders == []

    def test_none_of_them_coerces_object_id_for_itself(self):
        offenders = [
            name for name, path in self._sources().items()
            if "object_id" in path.read_text(encoding="utf-8")
            and "int(str(row.object_id" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_none_of_them_still_points_at_the_confirmation_source(self):
        """The prose the extraction replaced. Ten such comments across the tree
        did not stop the copies drifting, which is why the audit exists."""
        offenders = [
            name for name, path in self._sources().items()
            if "see the confirmation source" in path.read_text(encoding="utf-8").lower()
        ]
        assert offenders == []

    def test_common_is_the_one_that_does(self):
        """Otherwise the three above pass on a tree where nothing does it."""
        path = self._package() / "_common.py"
        assert "resolve_by_object" in self._called_names(path)
        assert "int(str(row.object_id" in path.read_text(encoding="utf-8")

    def test_common_imports_nothing_from_the_package_at_module_scope(self):
        """The property every module in this package holds: a producer on a
        daemon hot path imports its source module directly."""
        tree = ast.parse((self._package() / "_common.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0:
                pytest.fail(f"relative import at module scope: {node.module}")
