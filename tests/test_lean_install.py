"""The suite runs on an install without the heavy ML extras, and stays that way.

`memory-search` (torch, sentence-transformers) and `whisper` (faster-whisper,
av, onnxruntime) are together about 750 MB of wheels. Nothing needs them to
collect: every heavy import in `src/` is inside a function, deliberately, so the
whole suite bar one test runs on an install that omits them. That matters
because the venv is per-worktree and per-container, so the difference is paid
again on every checkout.

Two failure modes keep that property from holding on its own, and both are
silent on a developer host where the extras happen to be installed:

  * a test importing torch or faster-whisper at module scope. Collection fails
    before any marker applies, so the `ml` marker cannot rescue it, and the
    result is an error in an unrelated file rather than a missing package;
  * a test dependency reaching the suite only as somebody else's transitive.
    `jinja2` used to arrive via mkdocs and torch, `psutil` via the `whisper`
    extra — so a lean install reported eight collection errors and two failures
    that read as a code regression. `pyyaml` was the same thing a third time,
    on `caldav`'s coattails, and went unnoticed because the check was a
    hand-written pair of tests per package. All four now sit in the `dev`
    group, and the check is a sweep over every package the suite imports —
    see `TestTheTestOnlyDependenciesAreDeclared`.

The same shape as `tests/test_image_tier.py` and `tests/test_linux_runner.py`,
which guard the tiers either side of this one.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"

# Top-level module names the two heavy extras bring in. A test importing any of
# these at module scope breaks collection on a lean install.
HEAVY_MODULES = frozenset(
    {
        "torch",
        "sentence_transformers",
        "transformers",
        "sqlite_vec",
        "faster_whisper",
        "onnxruntime",
        "av",
    }
)

# Every test module, enumerated at import so the checks below cover files added
# after they were written. Guarded by `test_the_enumeration_found_the_suite`,
# because an empty list would make the sweep collapse into a green no-op.
_TEST_MODULES = sorted(
    p for p in TESTS_ROOT.rglob("*.py") if "__pycache__" not in p.parts
)


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


def _ini() -> dict:
    return _pyproject()["tool"]["pytest"]["ini_options"]


def _dev_group() -> list[str]:
    return _pyproject()["dependency-groups"]["dev"]


def _requirement_names(requirements: list[str]) -> set[str]:
    """Distribution names from a requirements list, normalised to import form."""
    names = set()
    for req in requirements:
        name = re.split(r"[<>=!~;\[ ]", req, maxsplit=1)[0].strip()
        if name:
            names.add(name.replace("-", "_").lower())
    return names


def _names_from(nodes) -> set[str]:
    imported = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    return imported


@functools.lru_cache(maxsize=None)
def _imports(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """`path`'s imports, module scope and every scope, from one parse.

    The AST is deliberately **not** what gets cached. Holding one per test
    module retained about 490 MB in a single process, and `addopts` runs the
    suite under `-n auto`, so each xdist worker that lands one of the three
    sweeps below builds its own copy of that. The two frozensets are what the
    callers want and cost nothing to keep.

    `ast` rather than a regex so a name in a docstring or a comment cannot
    register as an import.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    return frozenset(_names_from(tree.body)), frozenset(_names_from(ast.walk(tree)))


def _module_scope_imports(path: Path) -> frozenset[str]:
    """Top-level package names imported at module scope by `path`.

    Module scope only: an import inside a function or a `try` body that the
    module tolerates failing is not what breaks collection.
    """
    return _imports(path)[0]


def _all_imports(path: Path) -> frozenset[str]:
    """Every top-level package name `path` imports, at any scope.

    Deliberately wider than `_module_scope_imports`, and the two are not
    interchangeable. The heavy-extra sweep wants module scope alone, because the
    property it guards is that *collection* survives an install without torch —
    an import inside a test body is exactly the shape that check asks for. The
    declaration sweep wants every scope, because an undeclared package fails
    whenever it is reached: at collection from module scope, and as a test
    failure from a function body. Restricting it to module scope would have let
    `testmon` and `xdist` through, both of which are imported inside functions.
    """
    return _imports(path)[1]


# Distribution names for the imports whose module name is not the name
# `pyproject.toml` declares.
#
# `importlib.metadata.packages_distributions()` answers this too, and was tried:
# it resolved nothing this map does not, cost 0.1s a call uncached, and made the
# verdict depend on what happens to be installed — green on a full venv, red on
# the lean one this file exists to protect, which is the wrong way round for a
# check about the manifest. A missing entry here is a package reported as
# undeclared, which is the safe direction and arrives with the name in the
# message. `test_the_import_map_names_only_declared_distributions` keeps the
# entries honest.
IMPORT_TO_DISTRIBUTION = {
    "yaml": "pyyaml",
    "PIL": "pillow",
    "dotenv": "python-dotenv",
    "testmon": "pytest-testmon",
    "xdist": "pytest-xdist",
}

# The roots that hold this project's own importable code. `docker` is in the
# list because `tests/test_browser_*.py` put `docker/browser/` on `sys.path` and
# import `browse_api` and `chrome` from it — names no manifest will ever
# declare, because they are files in this tree. Enumerated from disk rather than
# listed, so a module added beside them needs no entry here; bounded to these
# five roots rather than walking the repo, which would descend into `.venv`,
# `node_modules` and any worktree under `.claude/`.
_REPO_SOURCE_ROOTS = ("src", "tests", "testbed", "docker", "scripts")


@functools.lru_cache(maxsize=None)
def _repo_local_modules() -> frozenset[str]:
    """Top-level names importable from this tree rather than from a wheel.

    A root's own name is added only where the root is itself a package, which
    `tests` and `testbed` are and `docker`, `scripts` and `src` are not. That
    matters for exactly one of them: `docker` is a real distribution on PyPI
    (the Docker SDK), and this repo has four Docker-driven test tiers, so
    admitting the bare root name would have let an `import docker` pass as
    repo-local for ever — the failure class this file exists to end, on the
    likeliest candidate name in the tree.
    """
    names = set()
    for root in _REPO_SOURCE_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        if (base / "__init__.py").exists():
            names.add(root)
        for path in base.rglob("*"):
            if "__pycache__" in path.parts:
                continue
            if path.suffix == ".py":
                names.add(path.stem)
            elif path.is_dir() and (path / "__init__.py").exists():
                names.add(path.name)
    return frozenset(names)


def _distribution_candidates(module: str) -> set[str]:
    """The declared names that would satisfy an import of `module`."""
    candidates = {module, IMPORT_TO_DISTRIBUTION.get(module, module)}
    return {c.replace("-", "_").lower() for c in candidates}


@functools.lru_cache(maxsize=None)
def _lean_install_closure() -> frozenset[str]:
    """What `uv sync --extra test` installs, by declared name.

    **The sweep resolves against this and not against every declaration in the
    file, and that is the whole check.** A test-only package sitting in the
    `docs` extra is declared, is present in a full install, and is missing from
    every lean one — which is byte for byte the state `jinja2` and `psutil` were
    in before they were moved, and the state `pyyaml` was in by way of `caldav`.
    Accepting any declaration anywhere would have left that green.

    The six names this deliberately excludes are the two heavy extras' and
    `docs`'. Heavy imports are handled a rung above, by `HEAVY_MODULES` and the
    `ml` marker, and are exempted from the sweep by name for that reason.
    """
    pyproject = _pyproject()
    extras = pyproject["project"]["optional-dependencies"]
    expanded: set[str] = set()

    def expand(extra: str) -> set[str]:
        if extra in expanded or extra not in extras:
            return set()
        expanded.add(extra)
        names: set[str] = set()
        for requirement in extras[extra]:
            composed = re.findall(r"istota\[([\w-]+)\]", requirement)
            if composed:
                for inner in composed:
                    names |= expand(inner)
            else:
                names |= _requirement_names([requirement])
        return names

    closure = _requirement_names(pyproject["project"]["dependencies"])
    closure |= expand("test")
    closure |= _requirement_names(_dev_group())
    closure.discard("istota")
    return frozenset(closure)


class TestTheEnumerationIsNotEmpty:
    def test_the_enumeration_found_the_suite(self):
        # Every sweep below iterates this list. A bad glob would make them all
        # pass while checking nothing.
        assert len(_TEST_MODULES) > 100, (
            f"only found {len(_TEST_MODULES)} test modules under {TESTS_ROOT}"
        )


class TestTheMarkerIsRegisteredAndOffByDefault:
    def test_ml_is_a_registered_marker(self):
        # An unregistered marker is a warning, not an error, so a typo would
        # deselect nothing and make the heavy extras required again.
        assert any(m.startswith("ml:") for m in _ini()["markers"])

    def test_addopts_deselects_ml(self):
        match = re.search(r"-m '([^']+)'", _ini()["addopts"])
        assert match, f"could not find a -m expression in addopts: {_ini()['addopts']!r}"
        assert re.search(r"\bnot ml\b", match.group(1))

    def test_the_marker_names_what_it_needs(self):
        # The point of the description is that a red `-m ml` run tells you which
        # extra to install, rather than leaving you to read the traceback.
        description = next(m for m in _ini()["markers"] if m.startswith("ml:"))
        assert "memory-search" in description
        assert "whisper" in description


class TestNoTestImportsAHeavyPackageAtModuleScope:
    """The property the `ml` marker cannot enforce.

    A marker is applied during collection; a module-scope import fails *at*
    collection. So the marker deselects the test and the import error is
    reported anyway.
    """

    def test_no_module_scope_heavy_imports(self):
        offenders = {}
        for path in _TEST_MODULES:
            heavy = _module_scope_imports(path) & HEAVY_MODULES
            if heavy:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = sorted(heavy)

        assert not offenders, (
            "these test modules import a heavy-extra package at module scope, "
            "which breaks collection on an install without it. Move the import "
            "into the test body and mark the test `ml`:\n"
            + "\n".join(f"  {p}: {', '.join(mods)}" for p, mods in sorted(offenders.items()))
        )


class TestTheTestOnlyDependenciesAreDeclared:
    """Every package the *suite* imports is reachable on a lean install, and
    every dev-group declaration is still used.

    Checked in both directions on purpose. A one-way check that a package
    appears in the dev group would keep passing after the last user was deleted;
    a one-way check that something imports it would keep passing while it
    arrived as somebody else's transitive, which is the state this file exists
    to end.

    Both directions used to be a hand-written pair per package — `jinja2` and
    `psutil` by name — which covered those two and saw nothing else. `pyyaml`
    was the third instance and went unnoticed for as long as the pair existed:
    eighteen test files imported it while `pyproject.toml` declared it nowhere,
    and it reached the venv only because `caldav` happens to depend on it and
    `calendar` happens to be in the `test` extra (ISSUE-437). Both directions
    are now a sweep, so the fourth instance fails here rather than as eighteen
    collection errors that read as a code regression.

    The scope is `tests/`, which is what `_TEST_MODULES` enumerates. An
    undeclared dependency imported only by `src/` is caught by nothing here —
    `starlette` was found by this sweep only because a test helper imports it
    too, not because the product does.
    """

    def _importers(self, module: str) -> list[str]:
        return sorted(
            p.relative_to(REPO_ROOT).as_posix()
            for p in _TEST_MODULES
            if module in _all_imports(p)
        )

    def test_every_package_the_suite_imports_is_reachable_on_a_lean_install(self):
        local = _repo_local_modules()
        reachable = _lean_install_closure()

        unreachable = {}
        for path in _TEST_MODULES:
            for module in _all_imports(path):
                if module in sys.stdlib_module_names or module in local:
                    continue
                # The heavy extras sit outside the lean install on purpose, and
                # are guarded a rung up by `HEAVY_MODULES` and the `ml` marker.
                if module in HEAVY_MODULES:
                    continue
                if _distribution_candidates(module) & reachable:
                    continue
                unreachable.setdefault(module, []).append(
                    path.relative_to(REPO_ROOT).as_posix()
                )

        assert not unreachable, (
            "these packages are imported by the suite and are not reachable "
            "from `uv sync --extra test`, so they arrive only as somebody "
            "else's transitive. Add each to the `dev` group — not to an extra, "
            "which is a shipping artifact, and not to `docs` or a heavy extra, "
            "which a lean install does not have:\n"
            + "\n".join(
                f"  {mod}: {len(files)} file(s), e.g. {', '.join(sorted(files)[:3])}"
                for mod, files in sorted(unreachable.items())
            )
        )

    def test_a_declaration_outside_the_lean_install_does_not_count(self):
        # The control for the check above, and the reason it resolves against
        # the lean closure rather than against every declaration in the file. A
        # test-only package parked in `docs` reads as declared while being
        # absent from every lean install — which is where `jinja2` and `pyyaml`
        # each were, so accepting any declaration anywhere leaves the bug green.
        closure = _lean_install_closure()
        assert "mkdocs" not in closure
        assert "torch" not in closure
        assert "faster_whisper" not in closure
        # ...while all three routes that do reach a lean install count.
        assert "pillow" in closure  # project.dependencies
        assert "caldav" in closure  # the `test` extra, through `calendar`
        assert "pyyaml" in closure  # the dev group

    def test_the_import_map_names_only_declared_distributions(self):
        # An entry pointing at a name nothing declares would silently excuse the
        # import it maps, which is the shape of the bug this file exists for.
        # Same guard as `test_the_not_imported_exemptions_are_all_declared`.
        closure = _lean_install_closure()
        unknown = sorted(
            f"{module} -> {dist}"
            for module, dist in IMPORT_TO_DISTRIBUTION.items()
            if dist.replace("-", "_").lower() not in closure
        )
        assert not unknown, (
            "these import-name mappings point at nothing the lean install "
            "declares: " + ", ".join(unknown)
        )

    # A pytest plugin is registered through an entry point and a linter is run
    # as a binary, so neither is ever named in an `import` statement. Both have
    # their own coverage: `ruff` in the class below, `pytest-asyncio` in the
    # asyncio_mode setting every async test depends on.
    NOT_IMPORTED = frozenset({"pytest_asyncio", "ruff"})

    def test_every_dev_group_declaration_is_still_used(self):
        unused = []
        for name in sorted(_requirement_names(_dev_group())):
            if name in self.NOT_IMPORTED:
                continue
            modules = {name} | {
                module
                for module, dist in IMPORT_TO_DISTRIBUTION.items()
                if dist.replace("-", "_").lower() == name
            }
            if not any(self._importers(module) for module in modules):
                unused.append(name)

        assert not unused, (
            "nothing imports these any more — drop them from the dev group: "
            + ", ".join(unused)
        )

    def test_the_not_imported_exemptions_are_all_declared(self):
        # An exemption for a package that has since been removed would sit here
        # silently excusing a name nothing declares.
        assert self.NOT_IMPORTED <= _requirement_names(_dev_group())


class TestRuffIsInstalledByTheDocumentedSetup:
    """ISSUE-301: `AGENTS.md` mandates a linter the setup command did not install.

    `ruff check --output-format concise src tests testbed` is the Python half of
    the documented verification, and `ruff` was in neither the `test` extra, the
    `dev` group nor the venv — `uvx` is not installed either, so on a fresh
    clone the step simply could not be run. The one place it existed was
    `docker/test/Dockerfile`, as a `uv tool install` in the Linux runner image,
    which is the one environment a developer is least likely to be sitting in.

    It goes in the `dev` group and not in an extra for the same reason `jinja2`
    and `psutil` do: an extra is a shipping artifact, and nothing that installs
    istota to run it wants a linter. `uv sync` installs the default groups, so
    the `uv sync --extra test` in `scripts/setup.sh` now gets it with no new
    flag.
    """

    def test_ruff_is_in_the_dev_group(self):
        assert "ruff" in _requirement_names(_dev_group())

    def test_it_is_pinned_exactly(self):
        # The rule set in [tool.ruff.lint] is ruff's defaults for E4/E7/E9/F,
        # and a new release can add a rule to any of them. Unpinned, that
        # arrives as "the tree is suddenly dirty" on whoever syncs next.
        requirement = next(r for r in _dev_group() if r.startswith("ruff"))
        assert re.fullmatch(r"ruff==\d+\.\d+\.\d+", requirement), requirement

    def test_the_runner_image_pins_the_same_version(self):
        # Two installs of ruff in the Linux runner: the dev group's, and the
        # `uv tool install` that predates it and still owns the image's PATH.
        # Same version or the lint gate answers differently depending on which
        # one a shell resolves.
        body = (REPO_ROOT / "docker" / "test" / "Dockerfile").read_text()
        match = re.search(r"uv tool install (ruff==[\d.]+)", body)
        assert match, "docker/test/Dockerfile no longer installs ruff as a tool"
        assert match.group(1) in _dev_group()

    def test_the_verification_docs_still_name_it(self):
        # The other half of the two-way check the file's other pairs make: a
        # dependency nothing asks for should come back out.
        agents = (REPO_ROOT / "AGENTS.md").read_text()
        assert "ruff check" in agents


class TestTheTestExtraIsAllMinusTheHeavyOnes:
    """`test` is a hand-written copy of `all`, so it drifts.

    There is no way to subtract an extra, so the two lists are maintained
    separately and a new module extra added to `all` will not appear in `test`.
    The symptom is a test importing a package the lean install does not have,
    which surfaces on whichever machine syncs `test` rather than on the one that
    made the change.
    """

    HEAVY_EXTRAS = frozenset({"memory-search", "whisper"})

    def _extras(self) -> dict[str, list[str]]:
        return _pyproject()["project"]["optional-dependencies"]

    def _composed(self, extra: str) -> set[str]:
        """The `istota[x]` self-references in a composite extra."""
        return set(re.findall(r"istota\[([\w-]+)\]", " ".join(self._extras()[extra])))

    def test_test_is_exactly_all_minus_the_heavy_extras(self):
        assert self._composed("test") == self._composed("all") - self.HEAVY_EXTRAS

    def test_all_still_contains_the_heavy_extras(self):
        # The subtraction above is vacuous if `all` stops carrying them, and a
        # deployment install would silently lose memory search and transcription.
        assert self.HEAVY_EXTRAS <= self._composed("all")

    def test_test_composes_rather_than_listing_packages(self):
        # A raw package in here is a package that has to be kept in step with
        # the extra it was copied from.
        assert all(req.startswith("istota[") for req in self._extras()["test"])


class TestTheLinuxRunnerInstallsTheDevGroupAndTheTestExtra:
    """The runner image is where a lean install is actually exercised.

    It used to carry `--extra docs` purely for jinja2 and `--extra whisper`
    purely for psutil; both are now in the dev group, and re-adding either extra
    to fix an import error would hide the declaration bug again.
    """

    def _dockerfile_sync(self) -> str:
        body = (REPO_ROOT / "docker" / "test" / "Dockerfile").read_text()
        match = re.search(r"^RUN uv sync .*?(?=\n\n)", body, re.MULTILINE | re.DOTALL)
        assert match, "could not find the `RUN uv sync` block in docker/test/Dockerfile"
        return match.group(0)

    def test_it_installs_the_dev_group(self):
        # Without this the runner has no jinja2 and no psutil, and reports the
        # collection errors this change exists to remove.
        assert "--group dev" in self._dockerfile_sync()

    def test_it_installs_the_test_extra(self):
        assert "--extra test" in self._dockerfile_sync()

    def test_it_reaches_for_no_extra_beyond_test(self):
        # `--extra docs` (jinja2), `--extra whisper` (psutil) and
        # `--extra memory-search` (a heavy import someone moved to module scope)
        # are the three that would plausibly get added back.
        extras = set(re.findall(r"--extra ([\w-]+)", self._dockerfile_sync()))
        assert extras == {"test"}

    def test_the_setup_script_installs_the_same_thing(self):
        # A fresh clone gets its venv here, and a bare `uv sync` is what
        # produced the several-hundred-error state in the first place.
        body = (REPO_ROOT / "scripts" / "setup.sh").read_text()
        assert re.search(r"^uv sync --extra test\b", body, re.MULTILINE), (
            "scripts/setup.sh no longer installs the test extra"
        )
