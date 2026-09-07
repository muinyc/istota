"""Tests for the biomarker out-of-range explainer."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.health import explainer as health_explainer
from istota.health._migrate import ensure_initialised
from istota.health.models import HealthContext
from istota.health.routes import (
    get_user_context,
    require_auth,
    router,
)
from istota.health.workspace import synthesize_health_context


_GOOD_JSON = (
    '{"summary": "Elevated CO2 (bicarbonate) can reflect a shift in '
    'acid-base balance; trends matter more than a single value.", '
    '"causes": ['
    '"Mild dehydration or chronic diuretic use may raise bicarbonate.",'
    '"Compensatory response to respiratory disorders is common.",'
    '"Some antacids and supplements can elevate CO2 readings."'
    '], '
    '"mitigations": ['
    '"Consider a repeat test in a few weeks to confirm the trend.",'
    '"Review hydration and medications with your prescriber.",'
    '"Discuss the result with your healthcare provider for context."'
    ']}'
)


@pytest.fixture
def ctx(tmp_path):
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


class TestParseResponse:
    def test_good_object(self):
        out = health_explainer._parse_response(_GOOD_JSON)
        assert out is not None
        assert out["summary"].startswith("Elevated CO2")
        assert len(out["causes"]) == 3
        assert len(out["mitigations"]) == 3

    def test_strips_code_fences(self):
        raw = "```json\n" + _GOOD_JSON + "\n```"
        out = health_explainer._parse_response(raw)
        assert out is not None

    def test_only_the_fence_strip_can_parse_this_one(self):
        """The discriminating case ``test_strips_code_fences`` is missing.

        That one passes with the fence stripping removed entirely, because
        ``_parse_response``'s own widest-``{...}`` fallback rescues it — so
        it cannot fail for the reason it was written. Found by making
        ``llm_json.strip_fences`` a no-op as a control while F38 was
        extracted.

        Here the trailing prose carries a ``{`` of its own, so the fallback
        spans past the fence and produces invalid JSON.
        """
        raw = "```json\n" + _GOOD_JSON + "\n```\n\nHope that {helps}.\n"
        out = health_explainer._parse_response(raw)
        assert out is not None
        assert out["summary"].startswith("Elevated CO2")

    def test_rejects_missing_summary(self):
        bad = '{"causes": ["a", "b", "c"], "mitigations": ["x", "y", "z"]}'
        assert health_explainer._parse_response(bad) is None

    def test_rejects_empty_lists(self):
        bad = '{"summary": "abc", "causes": [], "mitigations": ["x"]}'
        assert health_explainer._parse_response(bad) is None

    def test_garbage_returns_none(self):
        assert health_explainer._parse_response("not json") is None


class TestGetOrGenerate:
    def test_generates_and_caches(self, ctx):
        with patch.object(health_explainer, "_call_brain", return_value=_GOOD_JSON):
            first = health_explainer.get_or_generate(
                ctx,
                name="CO2", display_name="CO2 (Bicarbonate)",
                direction="high", unit="mmol/L", ref_low=22, ref_high=29,
                category="CMP", config=object(),
            )
        assert first["source"] == "generated"
        assert first["summary"].startswith("Elevated CO2")
        assert first["disclaimer"]

        # Second call must come from cache without invoking the brain.
        with patch.object(
            health_explainer, "_call_brain",
            side_effect=AssertionError("should not be called"),
        ):
            second = health_explainer.get_or_generate(
                ctx,
                name="CO2", display_name="CO2 (Bicarbonate)",
                direction="high", unit="mmol/L", ref_low=22, ref_high=29,
                category="CMP", config=object(),
            )
        assert second["source"] == "cache"
        assert second["summary"] == first["summary"]

    def test_fallback_when_brain_missing(self, ctx):
        with patch.object(health_explainer, "_call_brain", return_value=None):
            out = health_explainer.get_or_generate(
                ctx,
                name="CO2", display_name="CO2 (Bicarbonate)",
                direction="high", unit="mmol/L", ref_low=22, ref_high=29,
                config=object(),
            )
        assert out["source"] == "fallback"
        assert out["summary"]
        assert len(out["causes"]) >= 1
        assert len(out["mitigations"]) >= 1
        # Fallback is NOT persisted — next call still falls back.
        with patch.object(health_explainer, "_call_brain", return_value=None):
            again = health_explainer.get_or_generate(
                ctx,
                name="CO2", display_name="CO2 (Bicarbonate)",
                direction="high", unit="mmol/L", ref_low=22, ref_high=29,
                config=object(),
            )
        assert again["source"] == "fallback"

    def test_fallback_on_unusable_brain_output(self, ctx):
        with patch.object(
            health_explainer, "_call_brain",
            return_value='{"summary": "ok"}',  # missing causes + mitigations
        ):
            out = health_explainer.get_or_generate(
                ctx,
                name="LDL", display_name="LDL Cholesterol",
                direction="high", unit="mg/dL", ref_low=None, ref_high=100,
                config=object(),
            )
        assert out["source"] == "fallback"

    def test_invalid_direction_raises(self, ctx):
        with pytest.raises(ValueError):
            health_explainer.get_or_generate(
                ctx, name="CO2", display_name="CO2",
                direction="weird", unit="mmol/L", ref_low=22, ref_high=29,
            )


class TestRoute:
    def test_route_returns_payload(self, client):
        with patch.object(health_explainer, "_call_brain", return_value=_GOOD_JSON):
            resp = client.get(
                "/istota/api/health/biomarkers/CO2/explainer?direction=high",
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "CO2"
        assert body["direction"] == "high"
        assert "discuss" in body["mitigations"][2].lower() or body["mitigations"]
        assert body["disclaimer"]

    def test_route_validates_direction(self, client):
        resp = client.get(
            "/istota/api/health/biomarkers/CO2/explainer?direction=weird",
        )
        assert resp.status_code == 400

    def test_route_uses_alias_resolution(self, client):
        # "Hgb" → Hemoglobin via alias map; the route should canonicalize.
        with patch.object(health_explainer, "_call_brain", return_value=_GOOD_JSON):
            resp = client.get(
                "/istota/api/health/biomarkers/Hgb/explainer?direction=low",
            )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Hemoglobin"
