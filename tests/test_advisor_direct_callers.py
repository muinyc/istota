"""Advisor-model spec — direct-caller coverage (Stage 1).

Eight of the nine ``BrainRequest`` construction sites (sleep cycle,
shared-block synthesis, the four health callers — since consolidated into one
builder — the code_review CLI, and conversation-context triage) run
unsandboxed — seven of
them building their env from ``dict(os.environ)`` — so they'd otherwise inherit
the host's ``~/.claude/settings.json`` ``advisorModel`` the same way the
sandboxed executor path did before Stage 1.
None of them ever sets ``BrainRequest.advisor``
— the fix at the brain layer (Stage 1) covers them "for free" because the
suppression predicate lives in ``ClaudeCodeBrain.execute`` / ``TmuxClaudeBrain``,
not at any of these call sites.

This file proves that structurally, not by importing and running each module
(which would drag in health/memory/briefings fixtures unrelated to the advisor):
it grep-counts the call sites (so a new caller is a loud test failure, not a
silent gap — the spec's own requirement), then exercises the two request
*shapes* those sites actually construct through the real brain.
"""

import re
from pathlib import Path
from unittest.mock import patch

import typing

from istota.brain._types import BrainRequest
from istota.brain.claude_code import ClaudeCodeBrain

SRC = Path(__file__).resolve().parent.parent / "src" / "istota"

# The sites named in the spec (executor.py is the one that sets `advisor`; the
# rest keep the `""` default). Path is relative to `SRC`.
_KNOWN_SITES = {
    "executor.py",
    "memory/sleep_cycle.py",
    "briefings/shared_blocks.py",
    # One site for all four health callers (the explainer and the three
    # document extractors) since F10 folded their `_call_brain` copies into
    # `call_health_brain`. It builds both request shapes: text-only for the
    # explainer and the text-native OCR branch, Read-only for the vision one.
    "health/_brain_call.py",
    # The code_review CLI's reviewers. `advisor=""` is not an oversight here
    # but the point: the reviewers are text-only by construction
    # (`allowed_tools=[]`), and the empty default is also what makes
    # ClaudeCodeBrain suppress the settings-file advisor channel — so a host's
    # ~/.claude/settings.json cannot hand a tool to a reviewer that is
    # specified not to have one.
    "skills/code_review/__init__.py",
    # Conversation-context triage. Text-only (`allowed_tools=[]`) and the
    # `advisor=""` default is load-bearing here for a second reason: this is
    # the daemon's most frequent model call, once per conversational task with
    # older history, so a host settings-file advisor left open would be paid
    # for on every turn. Before ISSUE-272 this site spawned `claude` directly
    # and never set the disable var at all — the channel really was open.
    "context.py",
}


def _grep_brain_request_sites() -> list[str]:
    # Scans every .py under src/istota (not just _KNOWN_SITES) so a *new*
    # site is caught rather than silently skipped.
    hits = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if re.search(r"\bBrainRequest\(", text):
            hits.append(str(path.relative_to(SRC)))
    return sorted(hits)


class TestKnownSites:
    def test_exactly_the_known_sites_construct_a_brain_request(self):
        # A new caller must fail this test until someone has thought about
        # whether it needs an explicit `advisor=` (advisor-model spec,
        # Tests section — "Direct-caller coverage").
        found = set(_grep_brain_request_sites())
        assert found == _KNOWN_SITES, (
            f"BrainRequest(...) construction sites changed: "
            f"missing={_KNOWN_SITES - found} new={found - _KNOWN_SITES}"
        )


class TestDirectCallerShapesStayAdvisorFree:
    """The two request shapes the direct callers actually build: text-only
    (sleep cycle, shared blocks, explainer, code_review) and Read-only (the
    three OCR paths). Both leave `advisor` at its `""` default; run each
    through the real ClaudeCodeBrain and confirm neither emits `--advisor`
    nor leaves the settings-file channel open."""

    def _execute_capturing(self, tmp_path, *, allowed_tools):
        req = BrainRequest(
            prompt="hi",
            allowed_tools=allowed_tools,
            cwd=tmp_path,
            env={},
            timeout_seconds=60,
            streaming=False,
            # advisor left at the dataclass default "" — exactly what every
            # direct caller does today.
        )
        captured_env = {}
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            captured_cmd.extend(cmd)
            return typing.cast(
                typing.Any,
                type("R", (), {"stdout": "ok", "stderr": "", "returncode": 0})(),
            )

        with patch("istota.brain.claude_code.subprocess.run", side_effect=fake_run):
            ClaudeCodeBrain().execute(req)
        return captured_cmd, captured_env

    def test_text_only_shape(self, tmp_path):
        # sleep_cycle / shared_blocks / explainer
        cmd, env = self._execute_capturing(tmp_path, allowed_tools=[])
        assert "--advisor" not in cmd
        assert env.get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL") == "1"

    def test_read_only_shape(self, tmp_path):
        # ocr / encounter_ocr / immunization_ocr (read_path=<document>)
        cmd, env = self._execute_capturing(tmp_path, allowed_tools=["Read"])
        assert "--advisor" not in cmd
        assert env.get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL") == "1"
