"""Stage 4: the daemon's task-less model calls, driven through the real seam.

`tests/test_usage_persistence.py` covers `persist_brain_usage` itself. These
tests go through the *callers*, with a stub brain, because a site that dropped
its persist call or typo'd its `origin` would be invisible to a test that calls
the writer directly — every assertion there would still pass against the
pre-Stage-4 tree.

Two representative sites: one with a real user id (the health explainer) and one
that is ownerless (the channel sleep-cycle pass). The other five follow the same
shape and share the same writer.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from istota import db
from istota.usage import SYSTEM_USER_ID, BrainUsage, ModelUsage


def _usage():
    u = BrainUsage(
        billed_input_tokens=550,
        output_tokens=161,
        cache_read_tokens=14425,
        cache_write_tokens=14565,
        cost_usd=0.0319275,
        cost_basis="api",
        totals_source="model_usage",
        has_totals=True,
        turns=1,
        model="model-a",
    )
    u.models = [ModelUsage(model="model-a", billed_input_tokens=550)]
    return u


@dataclass
class _StubResult:
    """Mirrors BrainResult's shape for the fields these callers read."""

    success: bool = True
    result_text: str = "{}"
    stop_reason: str = "completed"
    usage: object | None = field(default_factory=_usage)
    model_used: str = "model-a"
    brain_kind: str = "claude_code"
    actions_taken: object | None = None
    execution_trace: object | None = None


@dataclass
class _StubBrain:
    result: _StubResult = field(default_factory=_StubResult)

    def resolve_model_name(self, name):
        return f"resolved/{name}"

    def execute(self, req):
        return self.result


@pytest.fixture
def cfg(tmp_path):
    from istota.config import Config

    dbp = tmp_path / "istota.db"
    db.init_db(dbp)
    config = Config()
    config.db_path = dbp
    config.temp_dir = tmp_path
    return config


def _rows(config):
    with db.get_db(config.db_path) as conn:
        return list(conn.execute("SELECT * FROM task_usage ORDER BY id").fetchall())


class TestHealthExplainer:
    """A site with a real user id behind it."""

    def _call(self, config, result=None):
        from istota.health import explainer

        brain = _StubBrain(result or _StubResult(result_text="{}"))
        # Only at the definition site. There used to be a second patch here on
        # `explainer.make_brain` with `create=True`, which fabricated an
        # attribute the module has never had: the import is inside the function
        # and resolves through `istota.brain`, so the module global was read by
        # nothing. A patch that cannot miss is a patch that proves nothing, and
        # it would let a future refactor look covered when it is not.
        with patch("istota.brain.make_brain", return_value=brain):
            return explainer._call_brain("prompt", config, user_id="alice")

    def test_a_call_writes_a_row_with_its_origin_and_user(self, cfg):
        self._call(cfg)

        rows = _rows(cfg)
        assert len(rows) == 1
        assert rows[0]["origin"] == "health_explainer"
        assert rows[0]["user_id"] == "alice"
        assert rows[0]["task_id"] is None
        assert rows[0]["billed_input_tokens"] == 550

    def test_a_failed_call_is_recorded_too(self, cfg):
        """Tokens are spent whether or not the run succeeded, so the persist
        must sit above the success branch rather than inside it."""
        self._call(cfg, _StubResult(success=False, stop_reason="timeout"))

        rows = _rows(cfg)
        assert len(rows) == 1
        assert rows[0]["success"] == 0
        assert rows[0]["stop_reason"] == "timeout"

    def test_a_brain_reporting_no_usage_writes_nothing(self, cfg):
        self._call(cfg, _StubResult(usage=None))

        assert _rows(cfg) == []


class TestChannelSleepCycle:
    """An ownerless site. A channel pass has no single user, so it must land on
    the same no-owner sentinel shared briefing blocks use — otherwise a per-user
    grouping grows two buckets for one idea."""

    def _call(self, config, user_id):
        from istota.memory import sleep_cycle

        brain = _StubBrain(_StubResult(result_text="ok"))
        with patch.object(sleep_cycle, "make_brain", return_value=brain):
            return sleep_cycle._run_sleep_cycle_brain(
                config, "prompt", model="general", label="test", user_id=user_id,
            )

    def test_the_ownerless_pass_uses_the_shared_sentinel(self, cfg):
        self._call(cfg, SYSTEM_USER_ID)

        rows = _rows(cfg)
        assert len(rows) == 1
        assert rows[0]["origin"] == "sleep_cycle"
        assert rows[0]["user_id"] == SYSTEM_USER_ID

    def test_a_per_user_pass_carries_the_real_user(self, cfg):
        self._call(cfg, "alice")

        assert _rows(cfg)[0]["user_id"] == "alice"

    def test_the_row_has_no_task(self, cfg):
        self._call(cfg, "alice")

        assert _rows(cfg)[0]["task_id"] is None


def test_every_named_origin_is_reachable_from_its_module():
    """The eight task-less origins, checked as source-level presence.

    Cheap and shallow on purpose — it catches a site losing its call or drifting
    to a different `origin` string, which is the failure the direct-writer tests
    cannot see. The two classes above prove the mechanism actually runs.

    `context_triage` is keyed on `executor.py` rather than on `context.py`,
    unlike the other seven: the inference happens in `context.py`, but the sink
    that names the origin is built in the executor, which is what knows the
    task, user and source_type the row belongs to.

    The two halves are keyed separately because the four health callers no
    longer make the call themselves: F10 folded their `_call_brain` copies into
    `health/_brain_call.py`, which is where the writer lives now, while each
    caller still names its own `origin` — which is the half a site can drift on
    and the half a cost breakdown groups by. Collapsing both onto one path
    would have meant either dropping the origin check for those four or
    asserting the writer is in a file it is deliberately not in.
    """
    import pathlib

    health_writer = "src/istota/health/_brain_call.py"
    #: ``{origin site: (origin, file holding the persist_brain_usage call)}``
    expected = {
        "src/istota/memory/sleep_cycle.py": ("sleep_cycle", None),
        "src/istota/briefings/shared_blocks.py": ("shared_blocks", None),
        "src/istota/health/explainer.py": ("health_explainer", health_writer),
        "src/istota/health/ocr.py": ("health_ocr", health_writer),
        "src/istota/health/encounter_ocr.py": (
            "health_encounter_ocr", health_writer,
        ),
        "src/istota/health/immunization_ocr.py": (
            "health_immunization_ocr", health_writer,
        ),
        "src/istota/skills/code_review/__init__.py": ("code_review", None),
        "src/istota/executor.py": ("context_triage", None),
    }
    root = pathlib.Path(__file__).resolve().parent.parent

    for path, (origin, writer) in expected.items():
        source = (root / path).read_text()
        assert f'origin="{origin}"' in source, f"{path} lost origin={origin}"
        writer_source = (root / (writer or path)).read_text()
        assert "persist_brain_usage(" in writer_source, (
            f"{writer or path} lost its persist call"
        )
