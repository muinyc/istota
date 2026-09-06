"""One health brain call, four callers (F10).

``health/ocr.py``, ``health/encounter_ocr.py``, ``health/immunization_ocr.py``
and ``health/explainer.py`` each carried a ``_call_brain``. Three were
byte-identical apart from a log prefix and an ``origin`` string; the fourth is
the text-only explainer, which builds no namespace and takes a shorter timeout.
``health/_brain_call.call_health_brain`` is the one implementation and the four
keep their names as thin callers.

What this file pins, and why each is here rather than left to
``tests/test_brain_request_confinement.py``:

- **One builder.** That file's AST guards hold properties of *every*
  ``BrainRequest`` site in the tree; they do not say how many there are under
  ``health/``. A fifth copy would satisfy every one of them.
- **The fail-closed refusal, per caller.** The refusal — a namespace was wanted
  and could not be built, so do not grant ``Read`` — is now stated once, so a
  test naming one module no longer says anything about the other two. Both
  directions are asserted here: refused means no call, not refused means the
  grant.
- **The log discriminator.** ``health_enc_ocr_sandbox_refused`` and
  ``health_imm_ocr_sandbox_refused`` have to stay distinguishable in the log
  after the bodies merged, and the prefix is a caller-supplied value now rather
  than a literal in each file.
- **The explainer's shape.** Nothing asserted it before: the only test naming
  that function checked its ``env=`` expression by AST. So folding it in behind
  the OCR keywords — a namespace it never had, a 180s timeout it never had, a
  per-user cwd it never had — would have gone unnoticed.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from istota.brain import BrainRequest, BrainResult

HEALTH = Path(__file__).resolve().parents[1] / "src" / "istota" / "health"


class _CapturingBrain:
    def __init__(self, captured: list[BrainRequest]) -> None:
        self._captured = captured

    def resolve_model_name(self, _role: str) -> str:
        return "test-model"

    def execute(self, req: BrainRequest) -> BrainResult:
        self._captured.append(req)
        return BrainResult(success=True, result_text="[]")


@pytest.fixture
def capture(monkeypatch):
    """Capture the request each caller builds, and the usage row it persists.

    ``call_health_brain`` imports ``make_brain`` from ``istota.brain`` and
    ``persist_brain_usage`` from ``istota.executor`` at call time, so both are
    patched at their definition sites.
    """
    import istota.brain as brain_mod
    import istota.executor as executor_mod

    requests: list[BrainRequest] = []
    persisted: list[dict] = []

    monkeypatch.setattr(
        brain_mod, "make_brain", lambda _cfg: _CapturingBrain(requests)
    )
    monkeypatch.setattr(
        executor_mod,
        "persist_brain_usage",
        lambda *a, **k: persisted.append(dict(k)),
    )
    return requests, persisted


def _document(tmp_path: Path) -> Path:
    doc = tmp_path / "uploads" / "7" / "original.png"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_bytes(b"\x89PNG\r\n\x1a\n")
    return doc.resolve()


def _ocr_callers():
    """``(module, log_prefix, origin)`` for the three document extractors."""
    from istota.health import encounter_ocr, immunization_ocr, ocr

    return [
        (ocr, "health_ocr", "health_ocr"),
        (encounter_ocr, "health_enc_ocr", "health_encounter_ocr"),
        (immunization_ocr, "health_imm_ocr", "health_immunization_ocr"),
    ]


def _ocr_ids():
    return ["panel", "encounter", "immunization"]


def _calls_daemon_sandbox(path: Path) -> bool:
    """True when ``path`` calls ``build_daemon_sandbox`` under any binding.

    Resolves the local name from the import rather than matching the text, so
    ``import build_daemon_sandbox as _bds`` is still found and a docstring
    naming the function is not.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "build_daemon_sandbox":
                    names.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = getattr(func, "attr", None) or getattr(func, "id", None)
        if called in names or called == "build_daemon_sandbox":
            return True
    return False


class TestOnlyOneBuilderUnderHealth:
    """The pin: a second copy under ``health/`` fails here."""

    def test_only_the_shared_module_builds_a_brain_request(self):
        builders = set()
        for path in sorted(HEALTH.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == "BrainRequest":
                    builders.add(path.relative_to(HEALTH).as_posix())
        assert builders == {"_brain_call.py"}, (
            "health/ has more than one BrainRequest builder. The confinement, "
            "env and fail-closed rules are stated once in "
            "health/_brain_call.py; a second copy satisfies the tree-wide AST "
            "guards while drifting from it: " + ", ".join(sorted(builders))
        )

    def test_no_caller_keeps_its_own_sandbox_build(self):
        """The refusal branch, not just the request, moved.

        A caller that still called ``build_daemon_sandbox`` itself would build
        a namespace and then hand it to the shared helper, which builds another
        — and the refusal would then be decided twice.

        Parsed rather than grepped, for the reason the test above it is: a
        substring scan passes on ``import build_daemon_sandbox as _bds`` and
        fails on a docstring that merely names the function, so it can be
        green and red for reasons unrelated to what it claims.
        """
        offenders = sorted(
            path.relative_to(HEALTH).as_posix()
            for path in HEALTH.rglob("*.py")
            if path.name != "_brain_call.py"
            and _calls_daemon_sandbox(path)
        )
        assert offenders == [], (
            "these health modules still build their own daemon sandbox: "
            + ", ".join(offenders)
        )


class TestEachCallerKeepsItsRequestShape:
    """Equivalence with the four bodies this replaced."""

    @pytest.mark.parametrize(
        "module, _prefix, _origin", _ocr_callers(), ids=_ocr_ids()
    )
    def test_the_document_extractors_run_confined_for_180_seconds(
        self, module, _prefix, _origin, capture, make_config, tmp_path
    ):
        requests, _ = capture
        config = make_config()
        document = _document(tmp_path)

        module._call_brain(
            "extract this", config, read_path=document, user_id="alice"
        )

        req = requests[0]
        assert req.timeout_seconds == 180
        assert req.allowed_tools == ["Read"]
        assert req.fs_read_roots == [document]
        assert req.sandbox_wrap is not None
        assert Path(req.cwd) == (config.temp_dir / "alice").resolve()
        assert req.streaming is False

    def test_the_explainer_is_text_only_and_unconfined(
        self, capture, make_config
    ):
        """The shape nothing asserted before the four merged.

        No namespace, the shared temp root as its cwd, and a 120s timeout. All
        three are things the OCR keywords would have silently supplied.
        """
        from istota.health import explainer

        requests, _ = capture
        config = make_config()

        explainer._call_brain("explain this", config, user_id="alice")

        req = requests[0]
        assert req.timeout_seconds == 120
        assert req.allowed_tools == []
        assert req.fs_read_roots is None
        assert req.sandbox_wrap is None, (
            "the explainer grants no tool and never built a namespace; adding "
            "one here is a runtime change, not a tightening"
        )
        assert Path(req.cwd) == config.temp_dir

    @pytest.mark.parametrize(
        "module, _prefix, origin", _ocr_callers(), ids=_ocr_ids()
    )
    def test_each_extractor_keeps_its_own_usage_origin(
        self, module, _prefix, origin, capture, make_config
    ):
        _, persisted = capture

        module._call_brain("extract this", make_config(), user_id="alice")

        assert persisted[0]["origin"] == origin

    def test_the_explainer_keeps_its_own_usage_origin(
        self, capture, make_config
    ):
        from istota.health import explainer

        _, persisted = capture

        explainer._call_brain("explain this", make_config(), user_id="alice")

        assert persisted[0]["origin"] == "health_explainer"


class TestTheFailClosedRefusalIsStatedOnce:
    """Both directions, because only the pair distinguishes the boundary.

    Asserting the refusal alone passes against a helper that refuses always;
    asserting the grant alone passes against one that never refuses. The
    negative control for this stage forces each in turn.
    """

    @pytest.mark.parametrize(
        "module, prefix, _origin", _ocr_callers(), ids=_ocr_ids()
    )
    def test_a_grant_that_cannot_be_confined_is_refused_and_named(
        self, module, prefix, _origin, capture, make_config, tmp_path, caplog
    ):
        """An empty user id joins to the shared temp root, so no wrap is safe.

        The log line carries the caller's own prefix: the three bodies are one
        body now, and an operator reading ``health_..._sandbox_refused`` still
        has to be able to tell which upload path refused.
        """
        requests, _ = capture

        with caplog.at_level(logging.WARNING):
            result = module._call_brain(
                "extract this",
                make_config(),
                read_path=_document(tmp_path),
                user_id="",
            )

        assert result is None
        assert requests == [], "the brain must not have been invoked"
        assert f"{prefix}_sandbox_refused" in caplog.text

    @pytest.mark.parametrize(
        "module, _prefix, _origin", _ocr_callers(), ids=_ocr_ids()
    )
    def test_a_confinable_grant_is_granted(
        self, module, _prefix, _origin, capture, make_config, tmp_path
    ):
        requests, _ = capture

        module._call_brain(
            "extract this",
            make_config(),
            read_path=_document(tmp_path),
            user_id="alice",
        )

        assert len(requests) == 1
        assert requests[0].allowed_tools == ["Read"]

    @pytest.mark.parametrize(
        "module, _prefix, _origin", _ocr_callers(), ids=_ocr_ids()
    )
    def test_the_text_branch_is_not_refused(
        self, module, _prefix, _origin, capture, make_config
    ):
        """The refusal is about the tool grant, not about the call."""
        requests, _ = capture

        module._call_brain("extract this", make_config(), user_id="")

        assert len(requests) == 1
        assert requests[0].allowed_tools == []

    def test_the_explainer_has_nothing_to_refuse(self, capture, make_config):
        """It grants no tool on any path, so an unusable user id is not fatal."""
        from istota.health import explainer

        requests, _ = capture

        explainer._call_brain("explain this", make_config(), user_id="")

        assert len(requests) == 1


class TestTheHelperCannotBeAskedForAnUnconfinedGrant:
    """The degree of freedom the consolidation added, and its guard.

    Both reviewers found this and it is the one thing here that is not a
    restatement of the four bodies. Separately, those bodies could not express
    it: the explainer had no ``read_path`` parameter and the three extractors
    had no way to skip the namespace. One function with two keywords can, and
    the tree-wide AST guard in ``test_brain_request_confinement.py`` cannot see
    it — it requires ``sandbox_wrap=`` to be present and not a literal ``None``,
    and what it now reads is a bare name that is ``None`` on a live branch.

    So these go against ``call_health_brain`` directly rather than through a
    wrapper: no wrapper can reach this state, which is exactly why nothing else
    in the file covers it.
    """

    def test_a_read_grant_with_no_namespace_is_refused(
        self, capture, make_config, tmp_path, caplog
    ):
        from istota.health._brain_call import call_health_brain

        requests, persisted = capture

        with caplog.at_level(logging.ERROR):
            result = call_health_brain(
                "extract this",
                make_config(),
                origin="health_ocr",
                user_id="alice",
                read_path=_document(tmp_path),
                sandboxed=False,
            )

        assert result is None
        assert requests == [], (
            "a Read grant with sandbox_wrap=None is the ISSUE-397 exposure "
            "verbatim; the request must not be built at all"
        )
        assert persisted == []
        assert "health_ocr_unconfined_grant_refused" in caplog.text

    def test_the_refusal_does_not_catch_the_two_shapes_that_ship(
        self, capture, make_config, tmp_path
    ):
        """It is the pairing that is refused, not either half of it.

        A guard that also refused ``sandboxed=False`` alone would break the
        explainer, and one that refused ``read_path`` alone would break all
        three extractors — so both live shapes are asserted here beside it.
        """
        from istota.health._brain_call import call_health_brain

        requests, _ = capture
        config = make_config()

        call_health_brain(
            "explain this", config, origin="health_explainer",
            user_id="alice", sandboxed=False,
        )
        call_health_brain(
            "extract this", config, origin="health_ocr", user_id="alice",
            read_path=_document(tmp_path),
        )

        assert len(requests) == 2
        assert requests[0].allowed_tools == []
        assert requests[0].sandbox_wrap is None
        assert requests[1].allowed_tools == ["Read"]
        assert requests[1].sandbox_wrap is not None


class TestEachCallerKeepsItsLoggerName:
    """``logging_setup`` renders ``[%(name)-18s]`` on every console and file
    line, so the record name is part of what an operator reads. Four names
    collapsing onto ``istota.health._brain_call`` would have moved every health
    warning line while the message prefix — the thing the ``log_prefix``
    parameter exists to hold still — stayed put.
    """

    @pytest.mark.parametrize(
        "module, _prefix, _origin", _ocr_callers(), ids=_ocr_ids()
    )
    def test_a_document_extractor_logs_under_its_own_module(
        self, module, _prefix, _origin, monkeypatch, make_config, caplog
    ):
        import istota.brain as brain_mod

        class _Exploding:
            def resolve_model_name(self, _role):
                return "test-model"

            def execute(self, _req):
                raise RuntimeError("provider down")

        monkeypatch.setattr(brain_mod, "make_brain", lambda _cfg: _Exploding())

        with caplog.at_level(logging.WARNING):
            module._call_brain("extract this", make_config(), user_id="alice")

        assert [r.name for r in caplog.records] == [module.__name__]

    def test_the_explainer_logs_under_its_own_module(
        self, monkeypatch, make_config, caplog
    ):
        import istota.brain as brain_mod
        from istota.health import explainer

        class _Exploding:
            def resolve_model_name(self, _role):
                return "test-model"

            def execute(self, _req):
                raise RuntimeError("provider down")

        monkeypatch.setattr(brain_mod, "make_brain", lambda _cfg: _Exploding())

        with caplog.at_level(logging.WARNING):
            explainer._call_brain("explain this", make_config(), user_id="alice")

        assert [r.name for r in caplog.records] == ["istota.health.explainer"]


class TestTheHelperStillFailsSoft:
    """Every caller's contract is ``str | None``; nothing here raises."""

    @pytest.mark.parametrize(
        "module, prefix, _origin", _ocr_callers(), ids=_ocr_ids()
    )
    def test_a_brain_that_raises_returns_none(
        self, module, prefix, _origin, monkeypatch, make_config, caplog
    ):
        import istota.brain as brain_mod

        class _Exploding:
            def resolve_model_name(self, _role):
                return "test-model"

            def execute(self, _req):
                raise RuntimeError("provider down")

        monkeypatch.setattr(brain_mod, "make_brain", lambda _cfg: _Exploding())
        monkeypatch.setattr(
            __import__("istota.executor", fromlist=["x"]),
            "persist_brain_usage",
            lambda *a, **k: None,
        )

        with caplog.at_level(logging.WARNING):
            assert module._call_brain(
                "extract this", make_config(), user_id="alice"
            ) is None
        assert f"{prefix}_brain_failed" in caplog.text

    @pytest.mark.parametrize(
        "module, _prefix, _origin", _ocr_callers(), ids=_ocr_ids()
    )
    def test_no_config_returns_none(
        self, module, _prefix, _origin, capture
    ):
        requests, _ = capture

        assert module._call_brain("extract this", None, user_id="alice") is None
        assert requests == []
