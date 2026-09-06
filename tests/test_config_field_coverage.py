"""Every *field* of the ``Config`` tree is rendered, documented, or exempted.

``tests/test_config_section_coverage.py`` is this guard one layer up: it holds
every TOML *section* the dataclass tree declares to the Ansible template or to
``config/config.example.toml``. This one asks the same question of the leaf
fields inside those sections, which is where a setting an operator cannot learn
about actually hides — a documented ``[scheduler]`` block says nothing about
whether ``skill_overlay_reindex_interval`` is in it.

**The counting rule was the hard part, and it is three decisions.** ISSUE-438
measured the tree with a deliberately naive rule — a field counts as present if
its bare name appears anywhere in the artifact as an assignment — and got
answers that disagreed with the section guard's own docstring by a factor of
two on the middle number. Two plausible rules disagreeing that much is a sign
the rule is the product, not the guard. What is written down here:

**A dict-typed field is a section, not a field.** ``models.aliases``,
``brain.native.extra_headers``, ``brain.native.model_overrides`` and
``brain.source_type_overrides`` are all written as ``[models.aliases]``, so a
rule looking for ``aliases =`` reports them missing from a file that documents
them under their own header. They are excluded here and covered there, which is
also how ``_carries_keys`` in the section guard already reasons about them. The
same goes for a list or dict *of dataclasses* (``[[default_briefings]]``,
``[users.alice]``) for the same reason.

**A field that is not a setting is excluded structurally, not by name.** The
loader already answers this: ``config._NOT_CONFIGURATION`` is the set of
declared fields that must not be writable from TOML at all — ``config_path``
(the loader records where it read from), ``bundled_skills_dir`` (a test seam),
``admin_users`` (overwritten by ``load_admin_users()``, so it reaches a
deployment through ``/etc/istota/admins`` and nothing else). Reusing it rather
than restating it is what keeps the exclusion bounded: an entry there is
already a reviewed change, and ``tests/test_config_mapper.py`` already refuses
one that stops naming a live field. It also settles ISSUE-438's third question
— whether ``admin_users`` counts as documented by documenting its mechanism —
by making it moot. The field is not a config key, so the question does not
arise.

**Coverage is scoped to the field's own section.** This is the decision that
does the most work and the one the naive rule got wrong. ``enabled``,
``model``, ``interval`` and their like appear in dozens of blocks, so a bare
name match makes one documented ``enabled =`` anywhere in the file count as
documentation of every ``enabled`` in the tree. The scoped rule reads the
artifact into blocks and asks whether ``[a.b]`` assigns ``c``, which is what
``test_config.py::TestTheExampleDocumentsEveryLiveSection`` already does for
six hand-picked sections; this is that rule over the whole tree.

**Named by the template counts, exactly as it does one layer up.** 23 leaf
fields are written by ``config.toml.j2`` and documented in the example nowhere.
That is a gap worth closing one day and it is not this guard's claim: the
section-level rule is "an operator has *some* way to learn this exists", and a
key in the file every bare-metal deployment reads is such a way. Requiring both
would make this guard a 23-entry todo list on the day it landed, which is how a
guard becomes something people route around.

"Named by the template" is the honest phrasing and is weaker than "rendered":
30 of the template's 38 section headers sit inside a ``{% if %}``, and
``brain.native.api_key`` is gated on two conditions of its own, so a field can
satisfy this guard while appearing in no particular deployment's rendered
``config.toml``. The claim is that an operator has somewhere to read the key,
which the template source is; it is not that every deployment writes it.

Measured with the rule above, on the tree this landed with: 353 leaf fields,
350 after the ``_NOT_CONFIGURATION`` filter, 23 of those undocumented in the
example, and zero in neither artifact. Nothing pins those three numbers, so
treat them as the reading that motivated the shape rather than as a contract —
the one the guard actually holds is the zero. :data:`EXEMPT` is empty, and the
emptiness is the point.
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

#: Leaf fields that belong in neither artifact, each with the reason.
#:
#: Empty, and it stayed empty through the change that introduced this guard:
#: the nine fields ISSUE-438 found in neither artifact were documented rather
#: than exempted. That order matters. Writing the guard first and exempting
#: what it caught would have shipped a nine-entry map on day one, and a guard
#: whose exemption list is its findings is a rubber stamp.
#:
#: An entry here is a claim that a *settable* field should be learnable from
#: no operator-facing artifact. That is a much stronger claim than the section
#: guard's, because a field the loader will read is a field somebody can set.
#: If the real answer is "this is not a setting", the entry belongs in
#: ``config._NOT_CONFIGURATION`` instead, where the loader will also refuse it
#: from the file. Write the reason, not just the name.
EXEMPT: dict[str, str] = {}


def walk_fields(instance: object, prefix: str = "") -> list[str]:
    """Every leaf field of the ``Config`` tree, as a dotted path.

    A leaf is a field that is written in TOML as ``name = value`` under some
    header. Everything that is written as a *header* instead — a nested
    dataclass, a dict, a collection of dataclasses — is a section, and belongs
    to ``test_config_section_coverage.py``.
    """
    found: list[str] = []
    for field in dataclasses.fields(instance):
        path = f"{prefix}.{field.name}" if prefix else field.name

        nested = _nested_dataclass(instance, field)
        if nested is not None:
            found.extend(walk_fields(nested, path))
            continue

        if _collection_of_dataclasses(type(instance), field):
            continue
        if isinstance(getattr(instance, field.name, None), dict):
            continue

        found.append(path)

    return found


def _collection_of_dataclasses(owner: type, field: dataclasses.Field) -> bool:
    """Whether the field is declared as a dict or list of dataclasses.

    Read off the *declared* type rather than the value, because the default is
    an empty container that says nothing about what goes in it — the same
    reasoning, and the same ``get_type_hints`` resolution, as the section
    guard's copy.
    """
    declared = typing.get_type_hints(owner).get(field.name)
    if typing.get_origin(declared) not in (dict, list):
        return False
    return any(dataclasses.is_dataclass(arg) for arg in typing.get_args(declared))


#: A section header, live or commented, alone on its line but for a trailing
#: comment. Same shape as the section guard's, and for the same reason: the
#: example names `[models.aliases]` mid-prose several times, and a pattern with
#: no end anchor reads each of those as a header — which here would misfile
#: every assignment after it.
_HEADER = re.compile(r"^[ \t]*(?:#[ \t]*)?\[{1,2}([a-z_][a-z0-9_.]*)\]{1,2}[ \t]*(?:#.*)?$")

#: A header whose name is built by Jinja — ``[models.aliases."{{ name }}"]`` in
#: the template. It is a real section boundary that :data:`_HEADER` cannot name,
#: so without this the walk keeps the *previous* header current and files every
#: assignment after it into the wrong section. Harmless where it stands today,
#: and silently wrong the moment one of those keys collides with a real field of
#: the section above it.
_DYNAMIC_HEADER = re.compile(r"^[ \t]*(?:#[ \t]*)?\[{1,2}[^\]\n]*\{[{%]")

#: A section no field path can equal, so a dynamic header ends the block above
#: it rather than extending it.
_UNNAMEABLE = "\x00dynamic"

#: An assignment, live or commented out. A commented one counts: the example
#: documents most of the tree that way, and a line an operator uncomments is a
#: line they learned about.
#:
#: The value shape is required rather than just ``name =``, and it is doing real
#: work: ``config.example.toml`` line 854 reads
#: ``# lower = more intermediate text is shown live.`` mid-prose, which a bare
#: ``name =`` pattern records as ``[scheduler]`` documenting a key called
#: ``lower``. Costs nothing — measured, the two patterns cover exactly the same
#: fields — and removes one of the two live prose collisions in the file.
#: ``{{``/``{%`` are values in the Jinja template.
_ASSIGN = re.compile(
    r"""^[ \t]*(?:\#[ \t]*)?([a-z_][a-z0-9_]*)[ \t]*=[ \t]*
        (?:["'\[{]|-?\d|true|false|\{\{|\{%)""",
    re.VERBOSE,
)

#: The same assignment with the value *terminated* — end of line, or a trailing
#: comment. Not used for coverage, because a real multi-line array and a Jinja
#: expression make it too sharp an instrument to decide what an operator can
#: learn from. It is the audit in
#: :meth:`TestTheCoveringLinesAreRealAssignments`, which is what makes the
#: tolerance above a tested boundary instead of an accident: the residual prose
#: collision (line 290, ``# db_path = "/data/db/istota.db", so the directory``)
#: has a valid value and then a sentence, so only the terminator sees it.
_AUDIT_ASSIGN = re.compile(
    r"""^[ \t]*(?:\#[ \t]*)?([a-z_][a-z0-9_]*)[ \t]*=[ \t]*
        (?:"[^"]*"|'[^']*'|\[[^\]]*\]|\[|\{[^}]*\}|-?[\d.]+|true|false|\{\{.*?\}\})
        [ \t]*(?:\#.*)?$""",
    re.VERBOSE,
)


def assignments(path: Path) -> dict[str, set[str]]:
    """:func:`assignments_of` over an artifact on disk."""
    return assignments_of(path.read_text())


def assignments_of(text: str) -> dict[str, set[str]]:
    """``{section path: {names assigned under it}}``, with ``""`` the preamble.

    Line-oriented rather than a TOML parse, because both artifacts are mostly
    *commented out* — the example documents by commented block, and the Ansible
    template is Jinja and not valid TOML until it is rendered. A real parser
    sees almost nothing in either.

    Takes text rather than a path so the scanning rules can be driven by a
    control. A parser that can only be pointed at the two real files is one
    whose edge cases are asserted by nothing.
    """
    blocks: dict[str, set[str]] = {"": set()}
    current = ""
    for line in text.splitlines():
        header = _HEADER.match(line)
        if header:
            current = header.group(1)
            blocks.setdefault(current, set())
            continue
        if _DYNAMIC_HEADER.match(line):
            current = _UNNAMEABLE
            continue
        assign = _ASSIGN.match(line)
        if assign:
            blocks.setdefault(current, set()).add(assign.group(1))
    return blocks


def covering_lines(path: Path, field: str) -> list[str]:
    """Every line in ``field``'s own section that :data:`_ASSIGN` reads as it.

    The same walk as :func:`assignments`, kept beside it rather than folded in:
    the audit needs the *line*, and coverage needs only the name.
    """
    section, _, name = field.rpartition(".")
    found: list[str] = []
    current = ""
    for line in path.read_text().splitlines():
        header = _HEADER.match(line)
        if header:
            current = header.group(1)
            continue
        if _DYNAMIC_HEADER.match(line):
            current = _UNNAMEABLE
            continue
        assign = _ASSIGN.match(line)
        if assign and current == section and assign.group(1) == name:
            found.append(line)
    return found


def covered_by(field: str, blocks: dict[str, set[str]]) -> bool:
    """Whether the artifact assigns this field under its own section header."""
    section, _, name = field.rpartition(".")
    return name in blocks.get(section, set())


# The three exemption rules, as functions so they can be driven by a control.
# `EXEMPT` is empty, so a rule written inline is a rule nothing ever executes.


def stale_exemptions(exempt: dict[str, str], fields: list[str]) -> list[str]:
    return sorted(set(exempt) - set(fields))


def bare_exemptions(exempt: dict[str, str]) -> list[str]:
    return sorted(name for name, reason in exempt.items() if not reason.strip())


def redundant_exemptions(
    exempt: dict[str, str],
    fields: list[str],
    rendered: dict[str, set[str]],
    documented: dict[str, set[str]],
) -> list[str]:
    live = set(fields)
    return sorted(
        name
        for name in exempt
        if name in live and (covered_by(name, rendered) or covered_by(name, documented))
    )


@pytest.fixture(scope="module")
def fields() -> list[str]:
    return [f for f in walk_fields(Config()) if f not in config_module._NOT_CONFIGURATION]


class TestTheWalkItself:
    """A guard whose scan has rotted passes silently. These are the cheap
    assertions that say the inputs are still being read, and that the counting
    rule above is the one actually running."""

    def test_the_walk_finds_the_whole_tree(self, fields):
        assert len(fields) > 300, f"the walk found only {len(fields)} leaf fields"
        # One per shape: top level, one deep, two deep, three deep.
        for expected in (
            "bot_name",
            "scheduler.poll_interval",
            "brain.native.model",
            "brain.native.web_fetch.enabled",
        ):
            assert expected in fields, f"the walk no longer finds {expected}"

    def test_the_walk_excludes_what_is_written_as_a_header(self):
        """A dict, and a collection of dataclasses, are sections rather than
        fields. Counting them here is exactly the false positive that made
        ISSUE-438's naive measurement disagree with the section guard."""
        found = walk_fields(Config())
        for section in (
            "models.aliases",
            "brain.native.extra_headers",
            "brain.native.model_overrides",
            "brain.source_type_overrides",
            "users",
            "default_briefings",
            "briefing_shared_blocks",
        ):
            assert section not in found, (
                f"{section} is written as a TOML header, not as an assignment. "
                "test_config_section_coverage.py owns it."
            )

    def test_the_walk_excludes_what_the_loader_refuses_from_the_file(self):
        """`_NOT_CONFIGURATION` is the structural answer to "is this a
        setting". Reusing it rather than restating it is what keeps the
        exclusion bounded — and `tests/test_config_mapper.py` already refuses
        an entry that stops naming a live field, so it cannot go stale here
        either.

        Asserted against the **unfiltered** walk on purpose. The obvious
        spelling — take the `fields` fixture and assert each name is absent —
        restates the list comprehension that built it and holds for any return
        value of `walk_fields`, an empty list included. The property worth
        having is the other one: each entry is a leaf field the walk *does*
        find, and the fixture is what removes it. That goes red when an entry
        becomes a section or stops being a field.
        """
        assert config_module._NOT_CONFIGURATION, "the loader's reject set is empty"
        found = walk_fields(Config())
        for name in config_module._NOT_CONFIGURATION:
            assert name in found, (
                f"{name} is in _NOT_CONFIGURATION but is not a leaf field of the "
                "tree. Either it became a section, or it is gone — and the "
                "filter this guard applies is now excluding nothing."
            )

    def test_a_dynamic_header_ends_the_block_above_it(self):
        """`[models.aliases."{{ name }}"]` in the template is a real section
        boundary `_HEADER` cannot name. Left unhandled the walk keeps the
        previous header current and files the keys under it into that section,
        which is a false pass waiting for a name collision."""
        blocks = assignments_of(
            '[scheduler]\npoll_interval = 5\n'
            '[models.aliases."{{ n }}"]\nportable = true\n'
        )
        assert blocks["scheduler"] == {"poll_interval"}
        assert "portable" not in blocks["scheduler"]

    def test_prose_that_looks_like_an_assignment_is_bounded(self):
        """Both live collisions in `config.example.toml`, named as literals.

        The first is rejected by requiring a value shape. The second is not,
        and cannot be by any regex — it is a valid assignment followed by a
        sentence — so it is `_AUDIT_ASSIGN` and the audit below that bound it.
        Recorded here so the tolerance is a known boundary rather than an
        accident nobody measured.
        """
        prose_value = "# lower = more intermediate text is shown live. Keep equal to the"
        assert not _ASSIGN.match(prose_value)

        looks_real = '# db_path = "/data/db/istota.db", so the directory resolves to'
        assert _ASSIGN.match(looks_real), "if this stops matching, drop the audit's rationale"
        assert not _AUDIT_ASSIGN.match(looks_real)

        assert _ASSIGN.match("# poll_interval = 5")
        assert _AUDIT_ASSIGN.match("# poll_interval = 5")
        assert _AUDIT_ASSIGN.match("# shim_commands = [")
        assert _AUDIT_ASSIGN.match('# cron = "0 2 * * *"   # 2am')

    def test_both_artifacts_still_parse_as_blocks(self):
        for artifact in (RENDERED, DOCUMENTED):
            blocks = assignments(artifact)
            assert len(blocks) > 30, f"{artifact.name} yielded almost no sections"
            assert sum(len(v) for v in blocks.values()) > 100, (
                f"{artifact.name} yielded almost no assignments"
            )

    def test_prose_naming_a_section_is_not_a_header(self):
        """The section guard's own negative control, repeated because here a
        false header does more damage: it misfiles every assignment after it
        into a section that does not exist, and the fields of the real section
        then read as undocumented."""
        assert not _HEADER.match("#                      [models.aliases] below")
        assert not _HEADER.match("# `[models.aliases]` (further down) is the registry")
        assert not _HEADER.match("# [web] avatar_import_from_nextcloud and the backend.")
        assert _HEADER.match("# [playbooks]        # Learned playbooks — off by default")
        assert _HEADER.match("[[users.alice.briefings]]")

    def test_coverage_is_scoped_to_the_field_s_own_section(self):
        """The decision that does the most work, given a control.

        `enabled` is a field of dozens of sections. Under a bare-name rule one
        documented `enabled =` anywhere in the file covers all of them, which
        is a rubber stamp on the most common field names in the tree.
        """
        blocks = {"scheduler": {"enabled"}, "": set()}
        assert covered_by("scheduler.enabled", blocks)
        assert not covered_by("browser.enabled", blocks)
        assert not covered_by("enabled", blocks)

    def test_a_top_level_field_is_read_from_the_preamble(self):
        """Top-level keys sit above the first header in both artifacts, so
        they are covered by the `""` block or by nothing at all."""
        assert covered_by("bot_name", assignments(DOCUMENTED))


class TestEveryFieldIsRenderedDocumentedOrExempted:
    """The guard itself.

    Read the failure message rather than this docstring — it says which of the
    three places to add the field to.
    """

    def test_no_field_is_reachable_from_neither_artifact(self, fields):
        rendered = assignments(RENDERED)
        documented = assignments(DOCUMENTED)

        missing = sorted(
            f
            for f in fields
            if not covered_by(f, rendered)
            and not covered_by(f, documented)
            and f not in EXEMPT
        )
        assert not missing, (
            f"{missing} are live settings of the Config dataclass tree that no "
            "operator-facing artifact assigns. The loader reads them and there "
            "is no way to learn they exist. Do one of three things, in this "
            "order of preference:\n"
            f"  1. document the key in {DOCUMENTED.relative_to(REPO)} under its "
            "own section header — a commented `# key = value` counts, and that "
            "is how most of the tree is documented;\n"
            f"  2. render it from {RENDERED.relative_to(REPO)}, which is what "
            "every deployed bare-metal install reads;\n"
            "  3. if it is not a setting at all — the loader owns it, or it is "
            "a test seam — add it to `_NOT_CONFIGURATION` in config.py, which "
            "also stops an operator writing it into the file and being ignored. "
            f"Only if it is a real setting that belongs in neither artifact does "
            f"it go in EXEMPT in {Path(__file__).name}, with the reason. A name "
            "with no reason is not an exemption.\n"
            "Note the section scoping: the key has to appear under its own "
            "header. A `model =` in some other block does not document this one."
        )

    def test_a_field_that_reaches_neither_artifact_is_reported(self, fields):
        """The guard's own failure path, exercised.

        `test_no_field_is_reachable_from_neither_artifact` computes `missing`
        over the real files and finds nothing, which is the answer we want and
        is indistinguishable from a walk that produced no fields, a `covered_by`
        that returns True unconditionally, or an artifact that parsed as one
        enormous section. Feed the same computation a field path that cannot be
        in either file and require it to come back.
        """
        rendered = assignments(RENDERED)
        documented = assignments(DOCUMENTED)
        planted = "scheduler.__not_a_real_field__"
        assert not covered_by(planted, rendered)
        assert not covered_by(planted, documented)

        missing = [
            f
            for f in [*fields, planted]
            if not covered_by(f, rendered) and not covered_by(f, documented)
        ]
        assert missing == [planted]

    def test_the_exemption_map_names_only_live_fields(self, fields):
        stale = stale_exemptions(EXEMPT, fields)
        assert not stale, (
            f"EXEMPT names {stale}, which the Config tree no longer declares as "
            "a leaf field. Drop the entry rather than leaving a hole open for a "
            "future field that reuses the name. A field that became a section, "
            "or moved into `_NOT_CONFIGURATION`, is this case too."
        )

    def test_every_exemption_carries_a_reason(self):
        bare = bare_exemptions(EXEMPT)
        assert not bare, (
            f"{bare} are exempted with no reason. The reason is the artifact — "
            "it is what the next person reads instead of re-deciding."
        )

    def test_an_exemption_is_not_a_second_home_for_a_documented_field(self, fields):
        """An exemption that is also rendered or documented is a hole nobody
        opened on purpose: the field reached an artifact and the entry stayed,
        so the next field to take that path is exempt by accident."""
        redundant = redundant_exemptions(
            EXEMPT, fields, assignments(RENDERED), assignments(DOCUMENTED)
        )
        assert not redundant, (
            f"{redundant} are exempted and also covered by an artifact. Drop "
            "the exemption; the field is documented."
        )


class TestTheExemptionRulesCanFail:
    """`EXEMPT` is empty, so the three tests above pass over an empty mapping
    and observe nothing about their own logic.

    That is defensible — the machinery is for a future entry — but it means
    three of this file's tests are no-ops in every green run, which is worth
    knowing rather than discovering. Each rule is driven here over a map built
    for the purpose, so the logic is covered now and the first real entry
    inherits tests that have already been shown to work.
    """

    def test_a_stale_entry_is_caught(self):
        assert stale_exemptions({"scheduler.gone": "r"}, ["scheduler.poll_interval"]) == [
            "scheduler.gone"
        ]
        assert stale_exemptions({"scheduler.poll_interval": "r"}, ["scheduler.poll_interval"]) == []

    def test_a_reasonless_entry_is_caught(self):
        assert bare_exemptions({"a.b": "", "c.d": "   ", "e.f": "a reason"}) == ["a.b", "c.d"]

    def test_an_entry_an_artifact_covers_is_caught(self):
        live = ["scheduler.poll_interval"]
        documented = {"scheduler": {"poll_interval"}}
        empty: dict[str, set[str]] = {}
        assert redundant_exemptions(
            {"scheduler.poll_interval": "r"}, live, empty, documented
        ) == ["scheduler.poll_interval"]
        # Covered under some *other* section is not covered, same as the guard.
        assert redundant_exemptions(
            {"scheduler.poll_interval": "r"}, live, empty, {"browser": {"poll_interval"}}
        ) == []


class TestTheCoveringLinesAreRealAssignments:
    """Every field this guard counts as covered is covered by a line that is
    actually an assignment, not by a sentence that reads like one.

    This is what bounds `_ASSIGN`'s tolerance. Both artifacts are prose-heavy
    and a commented assignment is the documenting form, so the matcher has to
    accept `# key = value` — which also accepts a comment that happens to open
    that way. `config.example.toml:290` is exactly that:
    `# db_path = "/data/db/istota.db", so the directory resolves to ...`, filed
    under `[brain.native.web_fetch]`, which covers nothing only because that
    section has no `db_path`. One prose sentence beginning `enabled = true` in
    a section that *does* have an `enabled` would document a field nobody
    documented, silently.

    A stricter matcher cannot close that, because the collision is a valid
    assignment followed by a sentence. So coverage stays permissive and this
    audit — value terminated by end of line or a trailing comment — asserts
    that no field currently depends on the permissiveness. It goes red when one
    starts to.
    """

    @pytest.mark.parametrize("artifact", [RENDERED, DOCUMENTED], ids=["rendered", "documented"])
    def test_every_covered_field_has_a_terminated_assignment(self, fields, artifact):
        blocks = assignments(artifact)
        prose_covered = []
        checked = 0
        for field in fields:
            if not covered_by(field, blocks):
                continue
            checked += 1
            lines = covering_lines(artifact, field)
            if not any(_AUDIT_ASSIGN.match(line) for line in lines):
                prose_covered.append((field, lines[0] if lines else "<no line>"))

        assert checked > 100, (
            f"the audit only found {checked} covered fields in {artifact.name}; "
            "covering_lines has drifted from assignments"
        )
        assert not prose_covered, (
            f"in {artifact.name}, these fields are counted as documented only by "
            f"a line that is not a terminated assignment: {prose_covered}. That "
            "is prose being read as documentation. Document the key properly, or "
            "reword the sentence so it does not open with `name = value`."
        )


class TestTheNineISSUE438Found:
    """The gaps that motivated the guard, named individually.

    The guard above would catch any of these coming back, but it reports a list
    rather than a reason, and these nine are the ones with a reason worth
    keeping: each was a live setting the loader read and no artifact mentioned.
    `web.auth` is the one to keep an eye on — ISSUE-438's own table filed it as
    a dict written as a sub-table, and it is a plain `str` gating whether the
    web UI authenticates at all.
    """

    NINE = [
        "max_memory_chars",
        "max_knowledge_facts",
        "scheduler.skill_overlay_reindex_interval",
        "brain.native.compaction_reserve_tokens",
        "brain.native.compaction_keep_recent_tokens",
        "sleep_cycle.extraction_model",
        "sleep_cycle.curation_model",
        "channel_sleep_cycle.extraction_model",
        "web.auth",
    ]

    @pytest.mark.parametrize("field", NINE)
    def test_the_example_documents_it(self, field):
        assert covered_by(field, assignments(DOCUMENTED)), (
            f"{field} was documented by ISSUE-438 and is not in the example any "
            "more. It is a live setting with no other operator-facing home."
        )

    def test_they_are_all_still_live_leaf_fields(self, fields):
        gone = sorted(set(self.NINE) - set(fields))
        assert not gone, (
            f"{gone} are no longer leaf settings. If a field was removed or "
            "became a section, drop it from this list — but check the example "
            "does not still document a key nothing reads."
        )
