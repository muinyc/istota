"""The five module loaders answer the same four refusals — with their own class.

`istota.module_loader` is the shared body; what these assert is the property the
extraction had to preserve rather than the extraction itself. Each module keeps
its own ``UserNotFoundError``, so every ``except`` clause in the tree catches
exactly what it caught before: a single shared class re-exported five ways would
have widened each of them to the other four modules' failures, which is cheap to
write and invisible until a caller wraps two modules in one ``try``.

The message assertions are verbatim rather than substring-matched. Two of them
already appear in ``pytest.raises(match=...)`` elsewhere in the suite, and the
refusal text is what an operator reads out of a log when a module will not
resolve.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from istota import module_loader
from istota.config import Config, UserConfig

MODULES = ["briefings", "feeds", "health", "location", "money"]


def _loader(module: str):
    return importlib.import_module(f"istota.{module}._loader")


def _config(tmp_path: Path, *, users: dict[str, UserConfig], mount=True) -> Config:
    root = tmp_path / "mount"
    root.mkdir(exist_ok=True)
    return Config(
        nextcloud_mount_path=root if mount else None,
        users=users,
        bot_name="Istota",
        db_path=tmp_path / "no.db",  # not exercised — keeps best-effort paths quiet
    )


class TestEachModuleKeepsItsOwnError:
    @pytest.mark.parametrize("module", MODULES)
    def test_the_class_is_the_module_s_own(self, module):
        cls = _loader(module).UserNotFoundError
        assert cls is not module_loader.UserNotFoundError
        assert issubclass(cls, module_loader.UserNotFoundError)

    @pytest.mark.parametrize("module", MODULES)
    def test_no_config_raises_it(self, module):
        loader = _loader(module)
        with pytest.raises(loader.UserNotFoundError) as exc:
            loader.resolve_for_user("alice", None)
        assert str(exc.value) == "istota config not loaded"

    @pytest.mark.parametrize("module", MODULES)
    def test_a_disabled_module_raises_it(self, module, tmp_path):
        loader = _loader(module)
        cfg = _config(
            tmp_path,
            users={"alice": UserConfig(disabled_modules=[module])},
        )
        with pytest.raises(loader.UserNotFoundError) as exc:
            loader.resolve_for_user("alice", cfg)
        assert str(exc.value) == f"{module} module disabled for 'alice'"

    @pytest.mark.parametrize("module", MODULES)
    def test_an_unknown_user_raises_it(self, module, tmp_path):
        loader = _loader(module)
        cfg = _config(tmp_path, users={"alice": UserConfig()})
        with pytest.raises(loader.UserNotFoundError) as exc:
            loader.resolve_for_user("ghost", cfg)
        assert str(exc.value) == "user 'ghost' not in istota config"

    @pytest.mark.parametrize("module", MODULES)
    def test_no_mount_raises_it(self, module, tmp_path):
        loader = _loader(module)
        cfg = _config(tmp_path, users={"alice": UserConfig()}, mount=False)
        with pytest.raises(loader.UserNotFoundError) as exc:
            loader.resolve_for_user("alice", cfg)
        assert str(exc.value) == (
            f"{module} module for 'alice' has no nextcloud mount configured"
        )

    def test_one_module_s_class_does_not_catch_another_s(self, tmp_path):
        """The reason the exception type is a parameter rather than shared.

        `scheduler.py` imports `health._loader.UserNotFoundError` by name and
        wraps a health call in it. Under one shared class that clause would also
        swallow a money or a feeds failure raised in the same block.
        """
        feeds = _loader("feeds")
        money = _loader("money")
        cfg = _config(tmp_path, users={"alice": UserConfig(disabled_modules=["money"])})

        with pytest.raises(money.UserNotFoundError):
            try:
                money.resolve_for_user("alice", cfg)
            except feeds.UserNotFoundError:  # pragma: no cover - the assertion
                pytest.fail("feeds' error class caught money's failure")

    def test_the_family_can_be_caught_at_once(self, tmp_path):
        """The one thing the subclassing buys, and it widens nothing existing."""
        cfg = _config(tmp_path, users={"alice": UserConfig(disabled_modules=["feeds"])})
        with pytest.raises(module_loader.UserNotFoundError):
            _loader("feeds").resolve_for_user("alice", cfg)


class TestListUsers:
    @pytest.mark.parametrize("module", MODULES)
    def test_it_filters_the_opted_out(self, module, tmp_path):
        cfg = _config(tmp_path, users={
            "alice": UserConfig(),
            "bob": UserConfig(disabled_modules=[module]),
        })
        assert _loader(module).list_users(cfg) == ["alice"]

    @pytest.mark.parametrize("module", MODULES)
    def test_no_config_is_an_empty_list_not_a_raise(self, module):
        assert _loader(module).list_users(None) == []


class TestTheGuard:
    """No ``_loader.py`` may derive the workspace or the gate for itself again.

    A grep-shaped guard, following ``tests/test_lint_scope.py``: the audit found
    ten prose "this is a copy of X" comments that did not stop the copies
    drifting, so the pin is a test rather than a note.
    """

    def _imported_names(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
        return names

    def _code_strings(self, path: Path) -> list[str]:
        """Every string literal that is not a docstring.

        Prose is the wrong thing to grep here: every one of these files
        legitimately *names* ``get_user_bot_path`` and ``module_db_path`` in its
        docstring, and a guard that tripped on that would have to be softened
        until it stopped catching the thing it is for.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                body = getattr(node, "body", None)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        return [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]

    def _loader_paths(self) -> dict[str, Path]:
        root = Path(__file__).resolve().parents[1] / "src" / "istota"
        return {module: root / module / "_loader.py" for module in MODULES}

    def test_none_of_them_imports_the_workspace_helper(self):
        offenders = [
            module for module, path in self._loader_paths().items()
            if "get_user_bot_path" in self._imported_names(path)
        ]
        assert offenders == []

    def test_none_of_them_reaches_for_module_db_path(self):
        offenders = [
            module for module, path in self._loader_paths().items()
            if "module_db_path" in self._code_strings(path)
        ]
        assert offenders == []

    def test_none_of_them_spells_the_refusals(self):
        offenders = [
            module for module, path in self._loader_paths().items()
            if any(
                "not in istota config" in text or "module disabled for" in text
                for text in self._code_strings(path)
            )
        ]
        assert offenders == []

    def test_the_shared_module_is_the_one_that_does(self):
        """Otherwise the three above pass on a tree where nothing does it."""
        path = (
            Path(__file__).resolve().parents[1]
            / "src" / "istota" / "module_loader.py"
        )
        assert "get_user_bot_path" in self._imported_names(path)
        strings = self._code_strings(path)
        assert "module_db_path" in strings
        assert any("not in istota config" in text for text in strings)
        assert any("module disabled for" in text for text in strings)
