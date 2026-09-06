"""The skill CLI facade — `istota.skills._cli` — and the pins that keep it one copy.

Three questions, in the order they matter:

1. Does the facade do what the convention says? A returned error envelope exits
   1, a raised exception exits 1, a success envelope exits 0.
2. Does every skill route through it? A second copy of the epilogue is how the
   convention came to be stated six ways across eight files in the first place.
3. Is every `__main__.py` one shape?

The second and third are grep-shaped guards in the manner of
`tests/test_lint_scope.py`: they walk the tree for the pattern that was just
removed and require the hit set to be exactly the exemptions, each of which is
named here with its reason.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota.skills._cli import (
    emit,
    error_envelope,
    fail,
    is_error,
    run_skill_cli,
    status_exit_code,
)

SKILLS_DIR = Path(__file__).resolve().parents[1] / "src" / "istota" / "skills"
SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "istota"


def _args(command="go", **extra):
    return SimpleNamespace(command=command, **extra)


class TestTheEnvelopeHelpers:
    def test_error_envelope_carries_the_extras(self):
        assert error_envelope("nope", code=7) == {
            "status": "error", "error": "nope", "code": 7,
        }

    def test_extras_cannot_overwrite_the_discriminator(self):
        # `emit` exits on `is_error`, so an envelope whose status was splatted
        # over would print and not exit — turning `_output_error` from an
        # unconditional refusal into a conditional one.
        assert error_envelope("real", status="ok", error="fake", code=3) == {
            "status": "error", "error": "real", "code": 3,
        }

    def test_is_error_only_on_a_dict_saying_error(self):
        assert is_error({"status": "error"})
        assert not is_error({"status": "ok"})
        assert not is_error({"status": "not_found"})
        # A handler may return a list; `.get` on one is an AttributeError.
        assert not is_error([{"status": "error"}])
        assert not is_error(None)

    def test_status_exit_code_asks_for_ok_not_for_not_error(self):
        # Deliberately not the mirror of `is_error`: `memory` and `ntfy` both
        # had this rule already, and a third status is non-zero here.
        assert status_exit_code({"status": "ok"}) == 0
        assert status_exit_code({"status": "error"}) == 1
        assert status_exit_code({"status": "skipped"}) == 1
        assert status_exit_code(None) == 1


class TestEmitAndFail:
    def test_a_success_envelope_prints_and_does_not_exit(self, capsys):
        emit({"status": "ok", "n": 1})
        assert json.loads(capsys.readouterr().out) == {"status": "ok", "n": 1}

    def test_an_error_envelope_exits_one(self, capsys):
        with pytest.raises(SystemExit) as exc:
            emit({"status": "error", "error": "bad"})
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["error"] == "bad"

    def test_exit_on_error_false_makes_it_a_printer(self, capsys):
        emit({"status": "error", "error": "bad"}, exit_on_error=False)
        assert json.loads(capsys.readouterr().out)["error"] == "bad"

    def test_the_serialization_knobs_reach_json_dumps(self, capsys):
        emit({"status": "ok", "s": "é"}, indent=None, ensure_ascii=True)
        assert capsys.readouterr().out.strip() == '{"status": "ok", "s": "\\u00e9"}'

    def test_default_carries_an_unserializable_value(self, capsys):
        emit({"status": "ok", "p": Path("/tmp/x")}, default=str)
        assert json.loads(capsys.readouterr().out)["p"] == "/tmp/x"

    def test_fail_prints_and_exits_one(self, capsys):
        with pytest.raises(SystemExit) as exc:
            fail("no good", namespace="_reserved")
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out) == {
            "status": "error", "error": "no good", "namespace": "_reserved",
        }


class TestRunSkillCli:
    """The three cases the stage names, plus the shapes the call sites need."""

    def test_a_success_envelope_prints_and_exits_zero(self, capsys):
        run_skill_cli({"go": lambda a: {"status": "ok", "n": 2}}, _args())
        assert json.loads(capsys.readouterr().out) == {"status": "ok", "n": 2}

    def test_a_returned_error_envelope_exits_one(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run_skill_cli({"go": lambda a: {"status": "error", "error": "x"}}, _args())
        assert exc.value.code == 1
        # Printed before the exit, so the model still sees the reason.
        assert json.loads(capsys.readouterr().out)["error"] == "x"

    def test_a_raised_exception_exits_one(self, capsys):
        def boom(args):
            raise ValueError("kaboom")

        with pytest.raises(SystemExit) as exc:
            run_skill_cli({"go": boom}, _args())
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out) == {
            "status": "error", "error": "kaboom",
        }

    def test_a_status_that_is_not_error_exits_zero(self, capsys):
        # `browse` depends on this: "closed" and "not_found" are answers.
        run_skill_cli({"go": lambda a: {"status": "not_found"}}, _args())
        assert json.loads(capsys.readouterr().out)["status"] == "not_found"

    def test_a_handler_that_exits_itself_is_not_rewritten(self, capsys):
        def emits(args):
            emit({"status": "error", "error": "mine"}, indent=None)

        with pytest.raises(SystemExit) as exc:
            run_skill_cli({"go": emits}, _args())
        assert exc.value.code == 1
        # One envelope, not two: SystemExit is not an Exception.
        assert capsys.readouterr().out.strip() == '{"status": "error", "error": "mine"}'

    def test_handlers_print_ignores_the_return_value(self, capsys):
        run_skill_cli(
            {"go": lambda a: {"status": "error", "error": "ignored"}},
            _args(), handlers_print=True,
        )
        assert capsys.readouterr().out == ""

    def test_handlers_print_still_catches_an_exception(self, capsys):
        def boom(args):
            raise RuntimeError("still mine")

        with pytest.raises(SystemExit) as exc:
            run_skill_cli({"go": boom}, _args(), handlers_print=True)
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["error"] == "still mine"

    def test_on_exception_builds_the_envelope(self, capsys):
        def boom(args):
            raise TimeoutError("")

        def describe(exc):
            return error_envelope(f"{type(exc).__name__} against nowhere")

        with pytest.raises(SystemExit):
            run_skill_cli({"go": boom}, _args(), on_exception=describe)
        assert json.loads(capsys.readouterr().out)["error"] == (
            "TimeoutError against nowhere"
        )

    def test_an_unserializable_result_comes_back_as_an_envelope(self, capsys):
        """The serialization is inside the try, so this is not a traceback.

        Seven skills had `print(json.dumps(result, ...))` on the line after the
        dispatch, inside the `try`, and none of them passes a `default`. A
        result carrying a `datetime` or a `Decimal` has to come back as a
        well-formed envelope on stdout, not as an empty stdout and a traceback
        the caller cannot classify.
        """
        class Unserializable:
            pass

        with pytest.raises(SystemExit) as exc:
            run_skill_cli({"go": lambda a: {"status": "ok", "x": Unserializable()}},
                          _args())
        assert exc.value.code == 1
        assert "not JSON serializable" in json.loads(capsys.readouterr().out)["error"]

    def test_error_indent_is_separate_from_the_result_indent(self, capsys):
        # `skills/nextcloud` printed both through one `indent=2` helper.
        def boom(args):
            raise ValueError("x")

        with pytest.raises(SystemExit):
            run_skill_cli({"go": boom}, _args(), error_indent=2)
        assert capsys.readouterr().out.startswith("{\n  ")

    def test_an_unknown_command_exits_one(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run_skill_cli({"go": lambda a: None}, _args(command="nope"))
        assert exc.value.code == 1
        assert "unknown command" in json.loads(capsys.readouterr().out)["error"]

    def test_a_command_override_reaches_a_tuple_keyed_table(self, capsys):
        # `skills/nextcloud` keys `_COMMANDS` by (group, command).
        run_skill_cli(
            {("files", "ls"): lambda a: {"status": "ok"}},
            _args(command="ls", group="files"), command=("files", "ls"),
        )
        assert json.loads(capsys.readouterr().out)["status"] == "ok"

    def test_the_error_path_keeps_the_result_paths_default(self, capsys):
        def boom(args):
            raise ValueError(Path("/tmp/x"))

        with pytest.raises(SystemExit):
            run_skill_cli(
                {"go": boom}, _args(),
                on_exception=lambda e: {"status": "error", "p": Path("/tmp/x")},
                default=str,
            )
        assert json.loads(capsys.readouterr().out)["p"] == "/tmp/x"


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------

#: `main` bodies that do not dispatch through `run_skill_cli`. Each entry is a
#: reason that skill *cannot* be converted, not a note that it has not been —
#: "handlers print their own envelope" is not one of them, since that is what
#: `handlers_print=True` exists for and is how `kv`, `location`, `feeds`,
#: `health` and `tasks` are converted.
EPILOGUE_EXEMPT = {
    # A sibling session holds `skills/money/`; converting it would edit their
    # tree. Left for the round that follows theirs. Its `main` is also a nested
    # if/elif over four sub-tables, so it is not the one-line conversion the
    # others are.
    "money": "held by a parallel session, and a nested sub-table dispatcher",
    # Not a dispatcher at all: `os.execvp` into the `gws` binary, which replaces
    # this process and so has no envelope and no exit code of its own.
    "google_workspace": "execvp passthrough, no envelope",
    # No dispatch table to hand over: an if/elif chain over three and four
    # branches respectively. Converting means inventing the table first, which
    # is a change to what the file says rather than to where the epilogue lives.
    "markets": "if/elif chain, no dispatch table",
    "skills": "if/elif chain, no dispatch table",
    # One call, not a dispatch: `main` forwards argv straight to `_run`.
    "briefings": "single `_run` call, no per-command handler",
    # `main` *returns* an exit code for `__main__` to pass to `sys.exit`, where
    # `run_skill_cli` raises `SystemExit` itself. Converting would change the
    # contract `_emit`/`status_exit_code` are built around.
    "memory": "main returns an exit code rather than raising SystemExit",
    "ntfy": "main returns an exit code rather than raising SystemExit",
}


def _skill_main_sources() -> dict[str, str]:
    """The source of each skill's `main`, keyed by skill.

    Keyed off `__main__.py` rather than off every `__init__.py`, because most
    skills under `skills/` are library-only and have no CLI at all. `whisper`
    is the one whose `main` is not in its package `__init__`.
    """
    out = {}
    for path in sorted(SKILLS_DIR.glob("*/__main__.py")):
        name = path.parent.name
        src = path.parent / ("cli.py" if name == "whisper" else "__init__.py")
        out[name] = src.read_text()
    return out


def _all_skill_sources() -> dict[str, str]:
    """Every skill module, CLI or not — what the epilogue guard walks.

    Keyed by path rather than by skill: `whisper` holds its `main` in `cli.py`
    and its package `__init__.py` beside it, and a skill-name key would let
    whichever came second overwrite the first and go unwalked.
    """
    out = {}
    for pattern in ("*/__init__.py", "*/*.py"):
        for path in sorted(SKILLS_DIR.glob(pattern)):
            out[str(path.relative_to(SKILLS_DIR))] = path.read_text()
    return out


class TestEverySkillRoutesThroughTheFacade:
    def test_no_skill_carries_its_own_epilogue(self):
        """Nothing under `skills/` may hand-roll the epilogue. No exemptions.

        Deliberately not scoped to `EPILOGUE_EXEMPT`: that list says which
        `main` bodies cannot *dispatch* through the facade, which is a
        different question from whether a file may build its own error envelope
        in an `except`. Eleven names were exempt from both, so the eleven most
        likely to regrow the copy were the eleven this could not see.
        """
        # The shape the audit found five verbatim copies of: catch, print an
        # error envelope built by hand, exit 1.
        pattern = re.compile(
            r"except\s+Exception[^\n]*:\s*\n\s*print\(json\.dumps\(\{\s*\"status\":\s*\"error\"",
        )
        offenders = sorted(
            name for name, src in _all_skill_sources().items()
            if pattern.search(src)
        )
        assert offenders == [], (
            f"a hand-rolled skill CLI epilogue is back in: {offenders}"
        )

    def test_every_main_dispatches_through_run_skill_cli_or_is_exempt(self):
        missing = []
        for name, src in _skill_main_sources().items():
            if name in EPILOGUE_EXEMPT:
                continue
            tree = ast.parse(src)
            main = next(
                (n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "main"),
                None,
            )
            assert main is not None, f"{name} has no main()"
            calls = {
                n.func.id for n in ast.walk(main)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if "run_skill_cli" not in calls:
                missing.append(name)
        assert missing == [], (
            f"these skills dispatch without the shared epilogue and are not "
            f"listed in EPILOGUE_EXEMPT: {missing}"
        )

    def test_every_exemption_names_a_real_skill(self):
        known = set(_skill_main_sources())
        assert set(EPILOGUE_EXEMPT) <= known, (
            f"EPILOGUE_EXEMPT names skills that do not exist: "
            f"{sorted(set(EPILOGUE_EXEMPT) - known)}"
        )

    def test_no_exemption_claims_only_that_its_handlers_print(self):
        """`handlers_print=True` is the conversion, not a reason to skip one.

        Five skills are written that way and all five are converted; an
        exemption resting on it would be the copy staying behind under a
        reason that reads like one.
        """
        bad = sorted(
            name for name, reason in EPILOGUE_EXEMPT.items()
            if "handlers call emit" in reason or "handlers print" in reason
        )
        assert bad == [], (
            f"these exemptions describe what `handlers_print=True` handles: {bad}"
        )


class TestOcrLeafKeepsItsOwnCopy:
    """The one deliberate copy of the epilogue, and the reason it stays.

    `istota/ocr_leaf.py` is spawned once per attachment image and its contract
    is that it imports the standard library, Pillow and pytesseract and nothing
    from `istota`. Importing `skills._cli` would run `istota/skills/__init__.py`
    and star-import every skill, which is the 0.22s-per-spawn regression that
    module exists to prevent.
    """

    def test_the_leaf_imports_nothing_from_the_package(self):
        src = (SRC_DIR / "ocr_leaf.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("istota"):
                pytest.fail(f"ocr_leaf.py imports {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("istota"), alias.name

    def test_it_still_carries_the_epilogue_it_may_not_share(self):
        src = (SRC_DIR / "ocr_leaf.py").read_text()
        assert 'print(json.dumps({"status": "error", "error": str(e)}))' in src


MAIN_MODULE_TEMPLATE = '''"""Allow running as `python -m istota.skills.{name}`.

`sys.exit(main())` rather than a bare call, so a `main` that returns an exit
code (`ntfy`) has it passed on; identical for every `main` that exits itself.
One shape for all of them — `tests/test_skill_cli_facade.py` is the pin.
"""

import sys

from {module} import main

sys.exit(main())
'''

#: `skills/money/` is held by a parallel session; its `__main__.py` is left as
#: it was and converted in the round that follows theirs.
MAIN_MODULE_EXEMPT = {"money"}


class TestEveryMainModuleIsOneShape:
    def test_the_entry_points_are_byte_identical_modulo_the_name(self):
        wrong = []
        for path in sorted(SKILLS_DIR.glob("*/__main__.py")):
            name = path.parent.name
            if name in MAIN_MODULE_EXEMPT:
                continue
            module = ".cli" if name == "whisper" else "."
            expected = MAIN_MODULE_TEMPLATE.format(name=name, module=module)
            if path.read_text() != expected:
                wrong.append(name)
        assert wrong == [], f"__main__.py drifted from the one shape in: {wrong}"

    def test_there_are_still_as_many_as_there_were(self):
        # A skill CLI that loses its `__main__.py` stops being reachable at all:
        # both `skill_client._run_direct` and `skill_proxy` spawn
        # `python -m istota.skills.<name>`.
        assert len(list(SKILLS_DIR.glob("*/__main__.py"))) == 22
