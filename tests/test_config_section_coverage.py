"""Every section of the ``Config`` tree is rendered, documented, or exempted.

The drift this closes is measured rather than supposed. Every artifact in this
repo that *renders* config stayed current across seven weeks of new sections,
and every artifact that *tells an operator a section exists* went stale — by up
to seven weeks in the standalone wizard's case, three months in
``docker/init.sh``'s. The difference is not care: the renderers are edited in
the same commit as the key they render because a test fails otherwise, and
nothing at all failed when a section reached the dataclass tree and no
operator-facing artifact.

``tests/test_render_config.py`` is the working model — three guards over the
render → compose → ``.env.example`` chain, each with an exemption map carrying a
reason and a staleness check that refuses an entry the thing no longer has. This
is the same shape one layer up, over the ``Config`` dataclass tree itself.

**What it requires, and deliberately not more.** A section must be rendered by
the Ansible template or documented in ``config/config.example.toml``, or be
named in :data:`EXEMPT` with the reason it is in neither. It does *not* require
a wizard prompt per key — that would be wrong, since most sections should not be
asked about at install time. It forces a decision to be recorded when a section
is added, which is the thing that was missing.

**Sections, not fields, and that is a measurement rather than a preference.**
Field level over the whole tree does not reach today: of 338 leaf fields, 80 are
undocumented in the example and 14 appear in neither artifact, and several of
those 14 are dict-typed fields written as sub-tables rather than assignments, so
even the count needs a rule this guard would have to invent. That is a product
change with its own argument, not a guard. Field level *is* enforced where it
was affordable —
``tests/test_config.py::TestTheExampleDocumentsEveryLiveSection`` walks the
fields of the six sections Stage 5 added to the example — and the two guards are
deliberately separate, one per question.

**The walk is the loader's own.** ``config_mapper._nested_dataclass`` is the
predicate ``apply_section`` uses to decide it is looking at a sub-table, so a
section here is a section there by construction rather than by a second copy of
the rule. Collections of dataclasses (``[users.alice]``,
``[[default_briefings]]``) are sections in TOML but not recursion targets in the
loader, so they are found by their declared item type and cross-checked against
the loader's own hand-parsed list below.
"""

from __future__ import annotations

import dataclasses
import re
import typing
from pathlib import Path

import pytest

from istota import config as config_module
from istota.config import Config
from istota.config_mapper import _nested_dataclass

REPO = Path(__file__).resolve().parents[1]
RENDERED = REPO / "deploy" / "ansible" / "templates" / "config.toml.j2"
DOCUMENTED = REPO / "config" / "config.example.toml"

#: Sections that belong in neither artifact, each with the reason.
#:
#: Empty today, and the emptiness is the point: nothing was excluded to make the
#: walk pass. Two candidates were checked against the tree while this was
#: written and neither turned out to need an entry, which is worth recording so
#: they are not re-added on the strength of the claim alone:
#:
#: - ``users`` is deliberately absent from the Ansible template — profiles reach
#:   a deployment through ``istota user ensure`` and land in ``user_profiles`` —
#:   but ``config.example.toml`` carries worked ``[users.alice]`` and
#:   ``[users.bob]`` blocks, so an operator can learn the shape. Documented, not
#:   exempt.
#: - ``experimental`` is rendered by the template (``[experimental] features``)
#:   *and* live in the example. It was never undocumented.
#:
#: An entry here is a claim that an operator has no way to learn the section
#: exists and that this is correct. Write the reason, not just the name.
EXEMPT: dict[str, str] = {}


@dataclasses.dataclass(frozen=True)
class Section:
    """One TOML section the ``Config`` tree declares."""

    path: str
    #: Whether a header of this section's own name could carry a key at all.
    #: ``[models]`` cannot — ``ModelsConfig`` declares only ``aliases``, a dict,
    #: which is written as ``[models.aliases]``. Nor can ``[users]``, whose
    #: content is one table per user. Such a section is covered by a header
    #: beneath it; every other one has to appear under its own name.
    carries_keys: bool


def walk_sections(instance: object, prefix: str = "") -> list[Section]:
    """Every section the ``Config`` dataclass tree declares, depth first."""
    found: list[Section] = []
    for field in dataclasses.fields(instance):
        path = f"{prefix}.{field.name}" if prefix else field.name

        nested = _nested_dataclass(instance, field)
        if nested is not None:
            found.append(Section(path, carries_keys=_carries_keys(nested)))
            found.extend(walk_sections(nested, path))
            continue

        if _collection_of_dataclasses(type(instance), field):
            # A dict or list of dataclasses is `[users.alice]` /
            # `[[default_briefings]]`: the section is the item, so the bare
            # name carries nothing.
            found.append(Section(path, carries_keys=False))

    return found


def _carries_keys(instance: object) -> bool:
    """Whether ``[section]`` could hold an assignment of its own.

    A nested dataclass is its own header and a dict field is written as one, so
    a section made only of those is reached exclusively through its children.
    """
    for field in dataclasses.fields(instance):
        if _nested_dataclass(instance, field) is not None:
            continue
        if isinstance(getattr(instance, field.name, None), dict):
            continue
        return True
    return False


def _collection_of_dataclasses(owner: type, field: dataclasses.Field) -> bool:
    """Whether the field is declared as a dict or list of dataclasses.

    Read off the *declared* type rather than the value, because the default is
    an empty container that says nothing about what goes in it. Resolved
    through ``get_type_hints`` so a module that switches on postponed
    annotations does not silently stop matching — the annotation is a string
    there, and every collection section would vanish from the walk.
    """
    declared = typing.get_type_hints(owner).get(field.name)
    if typing.get_origin(declared) not in (dict, list):
        return False
    return any(dataclasses.is_dataclass(arg) for arg in typing.get_args(declared))


#: A section header, live or commented, alone on its line but for a trailing
#: comment. The trailing bound is what keeps prose out: `config.example.toml`
#: names `[models.aliases]` mid-sentence in several places, and a header pattern
#: with no end anchor reads each of those as documentation of the section.
_HEADER = re.compile(
    r"^[ \t]*(?:#[ \t]*)?\[{1,2}([a-z_][a-z0-9_.]*)\]{1,2}[ \t]*(?:#.*)?$",
    re.MULTILINE,
)


def headers(path: Path) -> set[str]:
    """Every section header the artifact states, commented blocks included.

    A commented block counts: ``config.example.toml`` documents most of the tree
    that way, and a block an operator uncomments is a block they learned about.
    """
    return set(_HEADER.findall(path.read_text()))


def covered_by(section: Section, present: set[str]) -> bool:
    if section.path in present:
        return True
    if section.carries_keys:
        return False
    return any(name.startswith(f"{section.path}.") for name in present)


@pytest.fixture(scope="module")
def sections() -> list[Section]:
    return walk_sections(Config())


class TestTheWalkItself:
    """A guard whose scan has rotted passes silently, which is the whole
    failure mode `.claude/rules/testbed.md` catalogues. These are the cheap
    assertions that say the inputs are still being read."""

    def test_the_walk_finds_the_whole_tree(self, sections):
        paths = {s.path for s in sections}
        assert len(paths) > 30, f"the walk found only {len(paths)} sections"
        # One per shape: a top-level block, a nested one, one three deep, and a
        # collection reached by its declared item type rather than by recursion.
        for expected in ("scheduler", "talk.signaling", "brain.native.web_fetch", "users"):
            assert expected in paths, f"the walk no longer finds {expected}"

    def test_both_artifacts_still_parse_as_headers(self):
        assert len(headers(RENDERED)) > 30, "config.toml.j2 yielded almost no headers"
        assert len(headers(DOCUMENTED)) > 30, "config.example.toml yielded almost no headers"

    def test_prose_naming_a_section_is_not_documentation(self):
        """The one way this guard would go quietly green.

        `config.example.toml` names `[models.aliases]` inside running prose at
        least four times. A header pattern anchored only at the start of the
        line reads every one of those as the section being documented, and the
        guard then passes for a file that documents nothing.
        """
        assert not _HEADER.findall("#                      [models.aliases] below\n")
        assert not _HEADER.findall("# `[models.aliases]` (further down) is the registry\n")
        assert _HEADER.findall("# [playbooks]        # Learned playbooks — off by default\n")
        assert _HEADER.findall("[[users.alice.briefings]]\n")

    def test_the_collections_are_the_ones_the_loader_parses_by_hand(self, sections):
        """A collection section the loader neither maps nor hand-parses is a
        section an operator can write and nothing will read."""
        collections = {s.path for s in sections if not s.carries_keys and "." not in s.path}
        unparsed = sorted(collections - config_module._PARSED_BY_HAND)
        assert not unparsed, (
            f"{unparsed} are collections of dataclasses that config_mapper "
            "cannot walk and config.py does not name in _PARSED_BY_HAND. "
            "Nothing reads them."
        )


class TestEverySectionIsRenderedDocumentedOrExempted:
    """The guard itself.

    Read the failure message rather than this docstring — it says which of the
    three places to add the section to.
    """

    def test_no_section_is_reachable_from_neither_artifact(self, sections):
        rendered = headers(RENDERED)
        documented = headers(DOCUMENTED)

        missing = sorted(
            s.path
            for s in sections
            if not covered_by(s, rendered)
            and not covered_by(s, documented)
            and s.path not in EXEMPT
        )
        assert not missing, (
            f"{missing} are live sections of the Config dataclass tree that no "
            "operator-facing artifact mentions. The settings work and there is "
            "no way to learn they exist. Do one of three things, in this order "
            "of preference:\n"
            f"  1. render the section from {RENDERED.relative_to(REPO)}, which "
            "is what every deployed bare-metal install reads;\n"
            f"  2. document it in {DOCUMENTED.relative_to(REPO)} — a commented "
            "block counts, and that is how most of the tree is documented;\n"
            "  3. if it belongs in neither, add it to EXEMPT in "
            f"{Path(__file__).name} with the reason. A name with no reason is "
            "not an exemption.\n"
            "Renaming or merging a section counts as adding it: the new name "
            "needs one of the three, and the old name has to leave EXEMPT."
        )

    def test_the_exemption_map_names_only_live_sections(self, sections):
        paths = {s.path for s in sections}
        stale = sorted(set(EXEMPT) - paths)
        assert not stale, (
            f"EXEMPT names {stale}, which the Config tree no longer declares. "
            "Drop the entry rather than leaving a hole open for a future "
            "section that reuses the name."
        )

    def test_every_exemption_carries_a_reason(self):
        bare = sorted(name for name, reason in EXEMPT.items() if not reason.strip())
        assert not bare, (
            f"{bare} are exempted with no reason. The reason is the artifact — "
            "it is what the next person reads instead of re-deciding."
        )

    def test_an_exemption_is_not_a_second_home_for_a_documented_section(self, sections):
        """An exemption that is also rendered or documented is a hole nobody
        opened on purpose: the section moved into an artifact and the entry
        stayed, so the next section to take that name is exempt by accident."""
        present = headers(RENDERED) | headers(DOCUMENTED)
        by_path = {s.path: s for s in sections}
        redundant = sorted(
            name for name in EXEMPT
            if name in by_path and covered_by(by_path[name], present)
        )
        assert not redundant, (
            f"{redundant} are exempted and also covered by an artifact. Drop "
            "the exemption; the section is documented."
        )
