"""The Docker entrypoint's config generation, driven as a script.

``docker/istota/render-config.sh`` is ``entrypoint.sh``'s ``if [ ! -f
"$CONFIG_FILE" ]`` block — roughly 460 lines, sixteen ``cat >>`` heredocs and a
dozen conditional appends — lifted out so it can be run without a Nextcloud to
provision against. Nothing in the suite could reach it before: getting there
meant seeding ``/mnt/shared/.istota-provisioned`` and then sitting in the
entrypoint's 60x2s polling loop against ``http://nextcloud``.

Its inputs are **not** the environment in the ordinary sense. They are shell
locals the provisioning phase produces earlier in the same script, and the
extraction turns each into an explicit exported variable. The script is
*executed*, never sourced: ``entrypoint.sh`` runs ``set -euo pipefail``, so a
sourced script inherits ``-u`` and any unset variable it reads would abort the
whole entrypoint rather than the render.

What these tests assert is the property the extraction has to preserve — that
the rendered file is a config ``load_config`` accepts, carrying the values the
inputs asked for. The byte-identity of the move itself was checked once, by
hand, against the pre-extraction block; see the spec. There is no golden file
here on purpose: a fixture of the whole rendered config would turn every
deliberate config change into a fixture edit, and the reviewer could not tell an
intended diff from an accident.

Scope, stated because it is easy to assume otherwise: this file covers *what*
the render produces from a set of inputs, never *whether* the entrypoint runs
it. That decision, and the drift report and the preserved values that come with
running it on every boot, is ``tests/test_entrypoint_config_stage.py``. The
three add-if-missing backfill passes this docstring used to describe are gone
with ISSUE-368 — a render on every boot writes the same keys from the same
inputs, so they had become a second writer under different rules.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tomllib
import re
import typing
from pathlib import Path

import pytest

from istota.config import Config, load_config

REPO = Path(__file__).resolve().parent.parent
RENDER_CONFIG = REPO / "docker" / "istota" / "render-config.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="render-config.sh is #!/bin/bash and shells out to python3",
)


def render(tmp_path: Path, **env: str) -> Path:
    """Run render-config.sh with a fabricated environment; return the file.

    The environment is built from scratch rather than inherited. A developer
    host with ``ISTOTA_*`` variables exported — which is the normal state of
    anyone who runs the stack locally — would otherwise leak them into the
    render and make these assertions depend on the machine.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "config.toml"
    proc = subprocess.run(
        ["bash", str(RENDER_CONFIG)],
        env={
            "PATH": os.environ.get("PATH", ""),
            "CONFIG_FILE": str(config_file),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"render-config.sh exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert config_file.exists(), f"no config written\n{proc.stdout}\n{proc.stderr}"
    # **Empty stderr is part of the contract, and it is not tidiness.** Every
    # heredoc in this script is unquoted so that ${...} expands, which means a
    # backtick pair inside one is a *command substitution*: bash runs the word,
    # says "command not found" on stderr, substitutes nothing, and exits 0. The
    # render then reports success having silently deleted text from its own
    # output — measured on the `[talk.signaling]` block, whose comment named two
    # files in backticks and rendered with both names missing.
    assert proc.stderr == "", (
        "render-config.sh wrote to stderr while exiting 0, so it produced a "
        f"config that is quietly not the one it was written to produce:\n{proc.stderr}"
    )
    return config_file


# What a caller normally hands over. BOT_USER is in here because the tests below
# assert on it, not because the script needs it — it carries a `:-istota`
# default, like every other optional input.
REQUIRED = {
    "USER_NAME": "testuser",
    "NC_URL": "http://nextcloud:80",
    "APP_PASSWORD": "app-password-value",
    "BOT_USER": "istota",
}

# The three the script refuses to run without, alongside CONFIG_FILE. Everything
# else is either guarded by a `-n` test or carries a `:-` default.
REQUIRED_INPUTS = ("USER_NAME", "NC_URL", "APP_PASSWORD")


def _resources(rendered: dict, type_: str) -> list[dict]:
    """The `[[users.testuser.resources]]` entries of one type.

    Connected services are rendered as an array of tables under the user, not as
    top-level sections — `type = "money"` under the user, not `[money]`.
    """
    entries = rendered["users"]["testuser"].get("resources", [])
    return [r for r in entries if r.get("type") == type_]


def _resource(rendered: dict, type_: str) -> dict:
    matches = _resources(rendered, type_)
    assert len(matches) == 1, f"expected exactly one {type_} resource, got {matches}"
    return matches[0]


class TestTheRenderedConfigLoads:
    """The whole point of the extraction: run it, then load the result."""

    def test_the_minimal_environment_produces_a_loadable_config(self, tmp_path):
        config = load_config(render(tmp_path, **REQUIRED))

        assert config.nextcloud.url == "http://nextcloud:80"
        assert config.nextcloud.username == "istota"
        assert "testuser" in config.users

    def test_the_output_is_valid_toml_before_anything_interprets_it(self, tmp_path):
        # load_config tolerates a certain amount; tomllib does not. A heredoc
        # that lost its terminator, or an unescaped quote in a password, shows
        # up here rather than as a mysteriously absent config section.
        path = render(tmp_path, **REQUIRED)
        tomllib.loads(path.read_text())

    def test_the_two_nextcloud_path_keys_default_to_bare_metal(self, tmp_path):
        """`dav_prefix` and `auto_share_bot_dir` exist for the Docker shape,
        where the daemon's storage root is a `files_external` mount rather than
        the bot's own file tree. An operator who sets neither must get exactly
        what every deployment got before they existed."""
        config = load_config(render(tmp_path, **REQUIRED))

        assert config.nextcloud.dav_prefix == ""
        assert config.nextcloud.auto_share_bot_dir is True

    def test_the_two_nextcloud_path_keys_are_honoured_when_given(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_NEXTCLOUD_DAV_PREFIX="Shared Files",
                ISTOTA_NEXTCLOUD_AUTO_SHARE_BOT_DIR="false",
            )
        )

        assert config.nextcloud.dav_prefix == "Shared Files"
        assert config.nextcloud.auto_share_bot_dir is False

    def test_talk_signaling_is_off_and_undiscovered_unless_asked_for(self, tmp_path):
        """The whole block has to be inert on a deployment with no HPB.

        `enabled = false` is what keeps today's poll loop running, and every
        other key has to arrive at the dataclass default rather than at
        something the shell substituted — an empty `url` in particular, since a
        non-empty one means "do not ask Talk where the server is" and would
        point a discovering daemon at nothing.
        """
        config = load_config(render(tmp_path, **REQUIRED))

        assert config.talk.signaling.enabled is False
        assert config.talk.signaling.url == ""
        assert config.talk.signaling.room_sync_interval == 300
        assert config.talk.signaling.reconnect_backoff_max == 60
        assert config.talk.signaling.payload_direct is False

    def test_talk_signaling_round_trips_every_key_the_operator_can_set(self, tmp_path):
        """Five keys, no credential, and the absence is the design.

        Read this beside `test_every_var_the_render_reads_is_passed_by_compose`
        above, which is the half that catches the family being read here and
        withheld by compose. That guard is a blanket scan over every `ISTOTA_*`
        name rather than the four prefixes it once ran over, so this family was
        covered by it the moment the block was written — but a scan proves the
        name arrives, not that it lands on the field it names, and every one of
        these went to a different field the first time round.
        """
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_TALK_SIGNALING_ENABLED="true",
                ISTOTA_TALK_SIGNALING_URL="http://signaling:8080",
                ISTOTA_TALK_SIGNALING_ROOM_SYNC_INTERVAL="30",
                ISTOTA_TALK_SIGNALING_RECONNECT_BACKOFF_MAX="15",
                ISTOTA_TALK_SIGNALING_PAYLOAD_DIRECT="true",
            )
        )

        assert config.talk.signaling.enabled is True
        assert config.talk.signaling.url == "http://signaling:8080"
        assert config.talk.signaling.room_sync_interval == 30
        assert config.talk.signaling.reconnect_backoff_max == 15
        assert config.talk.signaling.payload_direct is True

    def test_no_signaling_credential_reaches_the_rendered_config(self, tmp_path):
        """The two secrets in this family configure containers, never the daemon.

        `ISTOTA_TALK_SIGNALING_SECRET` is Talk's credential for the signaling
        server, read by `provision-nc.sh` and by the server's own container;
        `_INTERNAL_SECRET` is the server's internal-client door, which joins any
        room on the instance and which istota rejects by design. Neither has any
        reader in the daemon, so a render that wrote either into `config.toml`
        would be putting a deployment-wide credential in a task's reach for no
        purpose — the shape of ISSUE-390.
        """
        path = render(
            tmp_path,
            **REQUIRED,
            ISTOTA_TALK_SIGNALING_ENABLED="true",
            ISTOTA_TALK_SIGNALING_SECRET="fabricated-backend-secret",
            ISTOTA_TALK_SIGNALING_INTERNAL_SECRET="fabricated-internal-secret",
        )
        rendered = path.read_text()

        assert "fabricated-backend-secret" not in rendered
        assert "fabricated-internal-secret" not in rendered

    def test_user_display_name_and_timezone_default_from_the_user_name(self, tmp_path):
        config = load_config(render(tmp_path, **REQUIRED))
        profile = config.users["testuser"]

        assert profile.display_name == "testuser"
        assert profile.timezone == "UTC"

    def test_user_display_name_and_timezone_are_honoured_when_given(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                USER_DISPLAY_NAME="Test Person",
                USER_TIMEZONE="Europe/Warsaw",
            )
        )
        profile = config.users["testuser"]

        assert profile.display_name == "Test Person"
        assert profile.timezone == "Europe/Warsaw"


def _backticks_in_heredocs(script: str) -> list[str]:
    """Every line inside an unquoted heredoc that contains a backtick.

    A function rather than a method, so the control below can feed it a
    synthetic string. A scan whose only "control" asserts that the *file* still
    contains a heredoc and a backtick has never run the parser at all, and the
    parser is the part that rots: an opener with trailing content after the
    delimiter would leave the whole body read as outside a heredoc, and every
    offender in it invisible.

    A quoted opener (`<<'PY'`) is deliberately not matched — quoting is exactly
    what makes a backtick literal — and that is the one case the regex gets
    right by construction rather than by intent, so it has a control too.
    """
    heredoc = None
    offenders = []
    for number, line in enumerate(script.splitlines(), 1):
        if heredoc is None:
            match = re.search(r"<<-?\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
            if match:
                heredoc = match.group(1)
            continue
        if line.strip() == heredoc:
            heredoc = None
            continue
        if "`" in line:
            offenders.append(f"{number}: {line.strip()[:80]}")
    if heredoc is not None:
        raise AssertionError(
            f"a heredoc opened with {heredoc} never closed; the scan saw the "
            "rest of the input as heredoc body and may be reporting nonsense"
        )
    return offenders


#: A line of the render whose whole content is a TOML section header.
#:
#: Anchored at both ends on purpose. `echo "[istota] Generating config.toml..."`
#: is a log line that opens with a bracket, and a scan that merely *finds* a
#: bracket reports it as a section named `istota` — which resolves against
#: nothing and would be a permanent false positive.
#:
#: Both quote styles and `printf` are accepted because the cost of missing one
#: is silence: `_unparsed_section_writes` is what stops this list of forms
#: being a thing somebody has to remember to extend.
SECTION_RE = re.compile(
    r"""^\s*
        (?:(?:echo|printf)\s+['"])?
        (\[{1,2}[^]]+\]{1,2})
        (?:\\n)?['"]?\s*
        (?:>>?\s*"?\$\w+"?)?\s*$
    """,
    re.X,
)

#: A line that emits something section-shaped into the rendered config.
#:
#: Deliberately looser than `SECTION_RE`: it asks "is this line trying to write
#: a header" rather than "can I parse it". The gap between the two is the
#: under-scan, and `test_the_scan_can_parse_every_section_write` is what makes
#: that gap fail rather than pass quietly.
#:
#: Two discriminators keep it from being noise. A `[` followed by a space is
#: shell's `[ -n "$x" ]` test rather than a header, and this script opens with
#: four of them. And an `echo`/`printf` is only a config write when it
#: redirects into `$CONFIG_FILE` — the same script logs `[istota] ...` to
#: stderr and builds `[a, b]` array literals with `printf`, neither of which
#: goes anywhere near the rendered file.
_HEREDOC_HEADER_RE = re.compile(r"^\s*\[{1,2}[^\s\]]")
_COMMAND_HEADER_RE = re.compile(r"""^\s*(?:echo|printf)\s+['"]\[{1,2}[^\s\]]""")


def _is_section_shaped(line: str) -> bool:
    if _COMMAND_HEADER_RE.match(line):
        return "$CONFIG_FILE" in line
    return bool(_HEREDOC_HEADER_RE.match(line))


def _config_write_lines() -> list[str]:
    """Every line of the render that can put text into the config file.

    A heredoc body line writes by virtue of being in the heredoc, so the test
    is "not a comment" rather than a redirect match — the alternative is
    tracking heredoc state, which `_backticks_in_heredocs` already shows is the
    fiddly part of reading this script.
    """
    return [
        line
        for line in RENDER_CONFIG.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]


def _unparsed_section_writes() -> list[str]:
    """Section-shaped writes `SECTION_RE` cannot read.

    The rot guard that matters. Counting sections cannot detect an arm going
    dark — only two of the thirty-one names come from the `echo` arm, so
    deleting it leaves twenty-nine and any threshold worth setting still
    passes while two real sections go unscanned.
    """
    return [
        line
        for line in _config_write_lines()
        if _is_section_shaped(line) and not SECTION_RE.match(line)
    ]


def _rendered_sections() -> set[str]:
    """Every TOML section `render-config.sh` can write, gate or no gate.

    Read statically rather than by rendering, because the gate is the whole
    difficulty: a retired section sits behind `if [ -n "$ITS_OWN_VAR" ]`, so
    the one environment that cannot reach it is the one nobody thinks to
    supply. `[briefing_defaults]` rendered only for an operator who set the
    variable documenting it.
    """
    sections = set()
    for line in _config_write_lines():
        match = SECTION_RE.match(line)
        if match:
            sections.add(match.group(1).strip("[]"))
    return sections


def _config_declares(section: str) -> bool:
    """Whether a dotted section path resolves to a *table* in the `Config` tree.

    A `dict`-typed field short-circuits to True for everything below it: the
    keys under `[users.<name>]` and `[models.aliases]` are named by the
    operator, so the schema has nothing to say about them.

    A path resolving to a scalar is False rather than True. `[model]` names a
    real field (`Config.model: str`) and is still not a section, so the
    permissive reading would pass a header the loader goes on to discard.
    `_NOT_CONFIGURATION` is consulted for the same reason: `apply_section`
    reports those *because* they are real fields, so field-existence alone is
    the wrong question.
    """
    from istota.config import _NOT_CONFIGURATION

    if section in _NOT_CONFIGURATION:
        return False
    cls = Config
    for part in section.split("."):
        if not dataclasses.is_dataclass(cls):
            return False
        field = next((f for f in dataclasses.fields(cls) if f.name == part), None)
        if field is None:
            return False
        declared = field.type
        if typing.get_origin(declared) is dict:
            return True
        cls = declared
    return dataclasses.is_dataclass(cls)


def _unrecognised_keys(config_path: Path) -> list[str]:
    """The keys `load_config` walks past, by the walk's own reckoning.

    The loader's own call rather than a reimplementation of it, so a key this
    reports is exactly a key the daemon discards. `report_unknown` turns the
    same list into one WARNING line and moves on; here it is the assertion.
    """
    from istota.config import _CONFIG_HOOKS, _HANDWRITTEN, _NOT_CONFIGURATION
    from istota.config_mapper import apply_section

    unknown: list[str] = []
    apply_section(
        Config(), tomllib.loads(config_path.read_text()),
        hooks=_CONFIG_HOOKS, unknown=unknown,
        skip=_HANDWRITTEN, reject=_NOT_CONFIGURATION,
    )
    return sorted(unknown)


class TestTheRenderWritesNothingTheLoaderIgnores:
    """A rendered key no dataclass field claims is a knob that does nothing.

    The operator sets a variable `docker/.env.example` documents, the generator
    writes the section it asks for, `config_mapper` walks past it, and the
    value is discarded behind one WARNING line in the boot log that nobody has
    a reason to read. `[briefing_defaults]` shipped that way for the whole of
    its retirement (ISSUE-445): retired by the legacy-briefings work, pinned as
    ignored by `tests/test_config.py::test_briefing_defaults_section_ignored`,
    and still rendered from two documented variables the whole time.

    Nothing could see it. ISSUE-430's drift guard walks the `Config` tree
    against the rendered document, so it only ever asks whether a *field* got
    rendered; a rendered key no field claims is invisible to it by
    construction, and its docstring says as much. This is that other direction.

    Two scans, because neither reaches the other's cases. The static one reads
    section headers out of the script and is the one that catches a retired
    section behind its own gate. The dynamic one renders and asks the loader,
    which is the only way to reach an individual *key* inside a section that
    does resolve.
    """

    def test_every_section_the_render_writes_is_one_the_loader_declares(self):
        undeclared = sorted(s for s in _rendered_sections() if not _config_declares(s))
        assert not undeclared, (
            f"render-config.sh writes {undeclared}, which no field of Config "
            "declares. config_mapper walks past the whole section and the "
            "operator gets a documented knob that does nothing. Remove the "
            "section and the variables that gate it, or add the field."
        )

    def test_the_scan_can_parse_every_section_write(self):
        """The rot guard that can actually detect an arm going dark.

        Counting sections cannot. Two of the twenty-nine names come from the
        `echo "[section]" >> "$CONFIG_FILE"` arm and the rest are bare heredoc
        lines, so deleting that arm leaves twenty-seven — past any threshold
        worth setting — while two real sections silently stop being scanned.
        This asks the other question instead: is there a line trying to write a
        header that `SECTION_RE` cannot read? A new emitting form fails here
        rather than being quietly skipped.
        """
        unparsed = _unparsed_section_writes()
        assert not unparsed, (
            "these lines of render-config.sh write something section-shaped "
            "that SECTION_RE cannot parse, so the section is invisible to "
            "every check in this class:\n  " + "\n  ".join(unparsed)
        )

    def test_the_scan_still_finds_the_sections(self):
        """Names the witnesses rather than counting, for the reason above."""
        sections = _rendered_sections()
        assert len(sections) > 20, f"the scan found only {sections}; the regex has rotted"
        assert {"brain.claude_code", "brain.tmux"} <= sections, (
            "the `echo` arm of SECTION_RE found nothing; those two sections are "
            "its only yield and a count cannot tell you they went missing"
        )
        assert "istota" not in sections, (
            "the scan matched an `echo \"[istota] ...\"` log line as a section"
        )

    @pytest.mark.parametrize(
        "line",
        [
            "[newsection] trailing junk",
            '    echo "[newsection]" >> "$CONFIG_FILE" && touch /tmp/x',
        ],
    )
    def test_the_under_scan_guard_reports_a_form_it_cannot_read(self, line):
        """The control: section-shaped but unparseable has to be reported.

        Both are lines the loose test recognises as a header write and
        `SECTION_RE` cannot read, which is exactly the pair
        `_unparsed_section_writes` is built to return.
        """
        assert _is_section_shaped(line)
        assert not SECTION_RE.match(line)

    @pytest.mark.parametrize(
        "line",
        [
            '    [ -n "${FOO:-}" ] || missing="$missing FOO"',
            '    echo "[istota] Generating config.toml..." >&2',
            "        printf '[%s]' \"$out\"",
        ],
    )
    def test_the_under_scan_guard_ignores_what_is_not_a_section_write(self, line):
        """The other half of the control. A guard that fires on shell tests,
        log lines and array literals gets switched off by whoever meets it."""
        assert not _is_section_shaped(line)

    def test_the_scan_finds_an_undeclared_section_it_is_given(self):
        assert not _config_declares("briefing_defaults")
        assert not _config_declares("brain.retired_thing")
        assert _config_declares("brain.native.web_fetch")
        assert _config_declares("users.someone.resources")

    def test_a_field_that_is_not_a_table_is_not_a_section(self):
        """Field-existence is the wrong question on its own.

        `[model]` names a real `str` field and `[admin_users]` a real field the
        loader rejects on purpose, and neither is a section a config may carry
        — `apply_section` reports both. A scan asking only "does a field of
        this name exist" passes them."""
        assert not _config_declares("model")
        assert not _config_declares("admin_users")

    def test_the_minimal_render_leaves_no_key_behind(self, tmp_path):
        assert _unrecognised_keys(render(tmp_path, **REQUIRED)) == []

    def test_a_render_with_every_module_on_leaves_no_key_behind(self, tmp_path):
        config = render(
            tmp_path,
            **REQUIRED,
            ISTOTA_BROWSER_ENABLED="true",
            ISTOTA_DEVELOPER_ENABLED="true",
            ISTOTA_EMAIL_ENABLED="true",
            ISTOTA_FEEDS_ENABLED="true",
            ISTOTA_LOCATION_ENABLED="true",
            ISTOTA_MEMORY_SEARCH_ENABLED="true",
            ISTOTA_MONEY_ENABLED="true",
        )
        assert _unrecognised_keys(config) == []

    def test_the_key_check_reports_a_retired_section_it_is_given(self, tmp_path):
        """The control. Without it the two above pass on a render that wrote
        nothing at all, which is the failure every scan in this file guards."""
        config = render(tmp_path, **REQUIRED)
        config.write_text(
            config.read_text() + "\n[briefing_defaults.news]\nlookback_hours = 12\n"
        )
        assert _unrecognised_keys(config) == ["briefing_defaults"]


#: Module gates the render fails closed on and the shipped stack turns on.
#:
#: One rule rather than one map entry per module, which is what ISSUE-444 asked
#: for and what the seven entries it replaced were: a single decision written
#: out seven times. The render is invoked outside compose by the image tier,
#: the testbed's lean shape and the tests in this file, and must enable nothing
#: it was not asked for; the shipped stack ships the batteries on. The
#: divergence is only ever reachable outside compose, because compose always
#: supplies a value.
#:
#: **Directional, and that is the whole guard.** `render=false, compose=true`
#: is the shipped posture. `render=true, compose=false` is its opposite — a
#: stack that ships a module off while every standalone render turns it on —
#: and stays a failure. A set of values cannot express that difference, which
#: is why the caller tracks a value per layer.
#:
#: Bounded three ways, so it exempts a posture rather than a family of names.
#: Only a `*_ENABLED` name; only the exact boolean pair; and every layer other
#: than the render has to agree on `true`, so a gate that also disagrees with
#: `.env.example` is still reported. A non-boolean divergence is a bug rather
#: than a posture — the value the render substitutes is the one the daemon runs
#: on, and an operator commenting out the `.env` line gets it without being
#: told.
#:
#: The point of a rule over a list: an eighth module needs no edit here, and a
#: module gate that starts diverging some *other* way is not silently covered.
def _is_fail_closed_module_gate(name: str, by_layer: dict[str, str]) -> bool:
    if not name.endswith("_ENABLED"):
        return False
    if by_layer.get("render") != "false":
        return False
    others = [v for layer, v in by_layer.items() if layer != "render"]
    return bool(others) and all(v == "true" for v in others)


#: Every shipped shell script with an unquoted heredoc in it.
#:
#: `render-config.sh` is where the defect was found. `provision-nc.sh` and
#: `entrypoint.sh` have one each and were covered by nothing — and the first of
#: those is edited by the same work that found it, which is exactly when a
#: second instance gets written.
HEREDOC_SCRIPTS = (
    REPO / "docker" / "istota" / "render-config.sh",
    REPO / "docker" / "istota" / "provision-nc.sh",
    REPO / "docker" / "istota" / "entrypoint.sh",
)


class TestNoHeredocRunsACommand:
    """A structural guard for the class of defect the stderr check catches late.

    Every heredoc in these scripts is unquoted, so `${...}` expands — which is
    the point — and a backtick pair is therefore a command substitution rather
    than the prose markup a reader writing a comment intends. The consequence is
    silent in both directions: bash writes "command not found" to stderr,
    substitutes the empty string, and the script exits 0 with the words gone
    from its own output. In a *value* rather than a comment it is worse than a
    mangled string: it is an unquoted shell executing a word.

    `render()` asserting empty stderr catches this on the default path only. A
    backtick inside one of the conditional blocks — the email one, the developer
    one, either brain block — is not on that path and is caught by nothing else,
    which is why this reads the files instead of running them.

    `$(...)` is not checked, because these scripts use it deliberately outside
    heredocs and there is no such use inside one to distinguish. Backticks have
    no legitimate use in any of them: `render-config.sh`'s own header comments
    use them for markup and every one of those is outside a heredoc, which is
    what makes the heredoc-scoped rule expressible rather than a style edict.
    """

    @pytest.mark.parametrize("script", HEREDOC_SCRIPTS, ids=lambda p: p.name)
    def test_no_backtick_appears_inside_a_heredoc(self, script):
        offenders = _backticks_in_heredocs(script.read_text())

        assert not offenders, (
            f"these lines in {script.name} sit inside an unquoted heredoc and "
            "contain a backtick, so the shell runs whatever is between the pair "
            "and substitutes its output:\n" + "\n".join(offenders)
        )

    def test_the_scan_finds_an_offender_it_is_given(self):
        """The control, and it runs the parser rather than the file.

        Three inputs, because three different parser mistakes each produce an
        empty offender list and would otherwise read as a pass.
        """
        # It sees inside a heredoc at all.
        assert _backticks_in_heredocs("cat <<TOML\nx = `whoami`\nTOML\n") == [
            "2: x = `whoami`"
        ]
        # It stops at the terminator rather than running to EOF.
        assert _backticks_in_heredocs("cat <<TOML\na\nTOML\n# `markup`\n") == []
        # A quoted opener makes a backtick literal, so it is not an offender —
        # and this is the case the regex gets right by construction.
        assert _backticks_in_heredocs("cat <<'PY'\n`literal`\nPY\n") == []

    def test_the_scan_refuses_an_unbalanced_heredoc(self):
        """An opener that never closes means the parser's answer is worthless,
        and answering `[]` there would be the quiet pass this guard exists to
        prevent."""
        with pytest.raises(AssertionError, match="never closed"):
            _backticks_in_heredocs("cat <<TOML\nx\n")

    def test_every_scanned_script_actually_has_a_heredoc(self):
        """Otherwise the parametrized case above is vacuous for that file."""
        for script in HEREDOC_SCRIPTS:
            assert re.search(
                r"<<-?\s*[A-Za-z_][A-Za-z0-9_]*\s*$", script.read_text(), re.M
            ), f"{script.name} has no unquoted heredoc; drop it from the list"


class TestTheStorageBackend:
    """`NC_URL` decides which of the two shipped storage backends is rendered.

    `Config.storage_is_nextcloud` is `bool(self.nextcloud.url)`, and it routes
    `storage_backend`, the prompt's file-tool vocabulary, the `nextcloud` entry
    in `available_capabilities()` and `doctor`'s `runtime.mount_liveness`. Both
    values are shipped install shapes — the Nextcloud-free one is what
    `istota setup` produces and what every lean testbed profile runs — so the
    render has to reach both.
    """

    def test_a_url_renders_the_nextcloud_backend(self, tmp_path):
        config = load_config(render(tmp_path, **REQUIRED))

        assert config.storage_is_nextcloud is True
        assert config.storage_backend == "nextcloud"

    def test_an_empty_url_renders_the_local_backend(self, tmp_path):
        """Set-but-empty, not unset, and the difference is the whole test.

        The preflight is `[ -n "${NC_URL+x}" ]` (`render-config.sh:68`), which
        tests whether the variable is *set*. An unset `NC_URL` therefore fails
        the render outright with exit 2 — asserted one class down in
        `TestTheInputContract` — while the empty string passes it and reaches
        the `url = ""` line the local install needs.
        """
        env = {**REQUIRED, "NC_URL": "", "APP_PASSWORD": ""}
        config = load_config(render(tmp_path, **env))

        assert config.nextcloud.url == ""
        assert config.storage_is_nextcloud is False
        assert config.storage_backend == "local"
        # The mount path is a hardcoded literal in the generator, so it is
        # rendered under both backends and `use_mount` stays true — the local
        # install is a plain directory at the same place, with nothing mounted
        # on it. This is why `doctor.check_mount_liveness` gates on the backend
        # rather than on the path being configured.
        assert config.nextcloud_mount_path == Path("/mnt/shared")
        assert config.use_mount is True

    def test_the_local_backend_drops_the_nextcloud_capability(self, tmp_path):
        """The prompt-visible half, at the point the render produces it.

        A skill declaring `requires_capability: [nextcloud]` is folded into the
        effective disabled set when the capability is absent, so it leaves both
        eager selection and the on-demand menu.
        """
        env = {**REQUIRED, "NC_URL": "", "APP_PASSWORD": ""}

        assert "nextcloud" in load_config(render(tmp_path / "nc", **REQUIRED)).available_capabilities()
        assert "nextcloud" not in load_config(render(tmp_path / "local", **env)).available_capabilities()


class TestQuotingSurvivesTheRender:
    """A generated TOML file is a quoting problem wearing a config's clothes.

    Every value here is interpolated into a `"`-delimited TOML basic string by a
    shell heredoc, so the two characters that matter are `"` and `\\`. A single
    quote is inert and proves nothing — the first draft of this class tested one
    and passed while the real case was broken.

    The failure mode is the bad one: `render-config.sh` exits 0 and prints
    "Config written to", so the entrypoint's file-exists guard treats the
    corrupt file as complete on every subsequent boot and never regenerates it.
    """

    # The credentials an operator types into docker/.env by hand, and therefore
    # the ones that can carry a shell metacharacter. The rest of the values in
    # the rendered config are machine-generated hex or come from a URL.
    @pytest.mark.parametrize(
        "variable,section,key,extra",
        [
            ("APP_PASSWORD", "nextcloud", "app_password", {}),
            (
                "ISTOTA_EMAIL_IMAP_PASSWORD",
                "email",
                "imap_password",
                {
                    "ISTOTA_EMAIL_ENABLED": "true",
                    "ISTOTA_EMAIL_BOT_ADDRESS": "bot@example.com",
                    "ISTOTA_EMAIL_IMAP_HOST": "imap.example.com",
                    "ISTOTA_EMAIL_IMAP_USER": "bot",
                },
            ),
            (
                "ISTOTA_DEVELOPER_GITLAB_TOKEN",
                "developer",
                "gitlab_token",
                {
                    "ISTOTA_DEVELOPER_ENABLED": "true",
                    "ISTOTA_DEVELOPER_REPOS_DIR": "/data/repos",
                },
            ),
            (
                "ISTOTA_DEVELOPER_GITHUB_TOKEN",
                "developer",
                "github_token",
                {
                    "ISTOTA_DEVELOPER_ENABLED": "true",
                    "ISTOTA_DEVELOPER_REPOS_DIR": "/data/repos",
                },
            ),
        ],
    )
    @pytest.mark.parametrize(
        "value",
        ['pa"ss', "back\\slash", 'both"and\\', "pa'ss word"],
        ids=["double-quote", "backslash", "both", "single-quote-and-space"],
    )
    def test_a_credential_survives_the_round_trip(
        self, tmp_path, variable, section, key, extra, value
    ):
        path = render(tmp_path, **{**REQUIRED, **extra, variable: value})

        rendered = tomllib.loads(path.read_text())
        assert rendered[section][key] == value

    def test_the_monarch_python_helper_escapes_a_backslash_and_a_quote(self, tmp_path):
        # render-config.sh renders these two through a python3 heredoc rather
        # than a shell heredoc, precisely so a quote or a backslash in a
        # password cannot break the TOML. It was the only value that got that
        # treatment; the parametrized cases above are the rest catching up.
        path = render(
            tmp_path,
            **REQUIRED,
            ISTOTA_MONEY_ENABLED="true",
            MONARCH_EMAIL='we"ird@example.com',
            MONARCH_PASSWORD="back\\slash",
        )
        money = _resource(tomllib.loads(path.read_text()), "money")

        assert money["monarch_email"] == 'we"ird@example.com'
        assert money["monarch_password"] == "back\\slash"


class TestTheDeveloperBlock:
    """The three shapes the image tier's Group C fabricates, at unit level.

    Group A's forge assertions need the third one to exist and to carry a token,
    because a doctor check that SKIPs is not an assertion. If this class stops
    producing a `[developer]` block with a token in it, that tier goes quietly
    green on a broken image.
    """

    def test_developer_off_emits_no_developer_section(self, tmp_path):
        rendered = tomllib.loads(render(tmp_path, **REQUIRED).read_text())

        assert rendered.get("developer", {}).get("enabled", False) is False

    def test_developer_on_without_a_token_still_sets_the_binary_paths(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_DEVELOPER_ENABLED="true",
                ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
            )
        )

        assert config.developer.enabled is True
        assert config.developer.repos_dir == "/data/repos"
        # The paths must be present even with no token: ISSUE-263 was a config
        # that named binaries which did not exist, not a missing key.
        assert config.developer.gh_bin_path
        assert config.developer.glab_bin_path

    def test_developer_on_with_a_token_renders_it(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_DEVELOPER_ENABLED="true",
                ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
                ISTOTA_DEVELOPER_GITLAB_TOKEN="fabricated-gitlab-token",
                ISTOTA_DEVELOPER_GITLAB_URL="http://gitlab.test",
            )
        )

        assert config.developer.gitlab_token == "fabricated-gitlab-token"
        assert config.developer.gitlab_url == "http://gitlab.test"

    def test_the_reviewer_username_reaches_the_rendered_config(self, tmp_path):
        """ISSUE-289. The compose stack renders its own `[developer]` block, so
        a setting the Ansible role gained is unreachable here until this file
        gains it too — and the symptom is an MR with no reviewer, not an
        error."""
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_DEVELOPER_ENABLED="true",
                ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
                ISTOTA_DEVELOPER_GITLAB_REVIEWER="reviewer-user",
                ISTOTA_DEVELOPER_GITLAB_REVIEWER_ID="1234567",
            )
        )

        assert config.developer.gitlab_reviewer == "reviewer-user"
        assert config.developer.gitlab_reviewer_id == "1234567"

    def test_the_forge_binary_paths_can_be_overridden(self, tmp_path):
        # 30bb7c83's bug: the Ansible role installs to /usr/bin and renders that
        # path, while the dataclass default is /usr/local/bin. Both deployments
        # have to be expressible from here.
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_DEVELOPER_ENABLED="true",
                ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
                ISTOTA_DEVELOPER_GH_BIN_PATH="/usr/bin/gh",
                ISTOTA_DEVELOPER_GLAB_BIN_PATH="/usr/bin/glab",
            )
        )

        assert config.developer.gh_bin_path == "/usr/bin/gh"
        assert config.developer.glab_bin_path == "/usr/bin/glab"


class TestChannelsAndResources:
    def test_log_and_alerts_channels_come_from_the_provisioned_tokens(self, tmp_path):
        config = load_config(
            render(tmp_path, **REQUIRED, LOG_TOKEN="logtok", ALERTS_TOKEN="alerttok")
        )
        profile = config.users["testuser"]

        assert profile.log_channel == "logtok"
        assert profile.alerts_channel == "alerttok"

    def test_an_explicit_channel_overrides_the_provisioned_token(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                LOG_TOKEN="logtok",
                USER_LOG_CHANNEL="explicit",
            )
        )

        assert config.users["testuser"].log_channel == "explicit"

    def test_the_location_ingest_resource_needs_both_the_module_and_the_token(
        self, tmp_path
    ):
        without = tomllib.loads(
            render(
                tmp_path / "a",
                **REQUIRED,
                ISTOTA_LOCATION_ENABLED="true",
            ).read_text()
        )
        with_token = tomllib.loads(
            render(
                tmp_path / "b",
                **REQUIRED,
                ISTOTA_LOCATION_ENABLED="true",
                LOCATION_INGEST_TOKEN="ingest-token",
            ).read_text()
        )

        assert _resources(without, "overland") == []
        assert _resource(with_token, "overland")["ingest_token"] == "ingest-token"


class TestThePerUserModuleOptOut:
    """``USER_DISABLED_MODULES``, the parallel of ``USER_DISABLED_SKILLS``.

    Modules are default-on and the per-user opt-out is ``disabled_modules`` on
    the ``[users.X]`` block, which the Docker render had no way to write. The
    two deployment-level toggles it did have, ``ISTOTA_FEEDS_ENABLED`` and
    ``ISTOTA_MONEY_ENABLED``, are a different axis: they decide whether the
    ``[[users.X.resources]]`` block is written at all. Health and briefings had
    neither, so nothing in the Docker shape could switch either off.
    """

    def test_nothing_is_disabled_by_default(self, tmp_path):
        config = load_config(render(tmp_path, **REQUIRED))

        assert config.users["testuser"].disabled_modules == []

    def test_a_comma_separated_list_reaches_the_loaded_config(self, tmp_path):
        config = load_config(
            render(tmp_path, **REQUIRED, USER_DISABLED_MODULES="health,briefings")
        )

        assert config.users["testuser"].disabled_modules == ["health", "briefings"]

    def test_a_disabled_module_is_off_for_that_user(self, tmp_path):
        """Through the predicate the product reads, not just the field."""
        config = load_config(
            render(tmp_path, **REQUIRED, USER_DISABLED_MODULES="health")
        )

        assert not config.is_module_enabled("testuser", "health")
        assert config.is_module_enabled("testuser", "briefings")

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("health, briefings", ["health", "briefings"]),
            ("health,", ["health"]),
            (",,health,,", ["health"]),
            ("  health  ", ["health"]),
            (",", None),
            ("   ", None),
        ],
        ids=["space-after-comma", "trailing", "empty-elements", "padded", "only-separators", "blank"],
    )
    def test_the_list_survives_what_an_operator_types(self, tmp_path, raw, expected):
        """``sed 's/[^,]*/"&"/g'`` got each of these wrong.

        A space after a comma stayed part of the name, so the element matched
        no module and did nothing; a trailing comma or a doubled one produced
        an empty element. A value of only separators wrote an empty array
        rather than no key.
        """
        rendered = tomllib.loads(
            render(tmp_path, **REQUIRED, USER_DISABLED_MODULES=raw).read_text()
        )
        user = rendered["users"]["testuser"]

        if expected is None:
            assert "disabled_modules" not in user
        else:
            assert user["disabled_modules"] == expected

    def test_a_quote_renders_a_value_rather_than_breaking_the_file(self, tmp_path):
        """The one that was not merely inert.

        A ``"`` closed the TOML string early, so the render exited 0 having
        written a config nothing can parse. On a first boot there is no
        ``config.toml.prev`` to fall back to, which under
        ``restart: unless-stopped`` is a crash loop.
        """
        rendered = render(tmp_path, **REQUIRED, USER_DISABLED_MODULES='he"alth')

        # Parses at all, which is the assertion; the value is nonsense either
        # way, and a name that matches no module is inert by design.
        assert tomllib.loads(rendered.read_text())["users"]["testuser"][
            "disabled_modules"
        ] == ['he"alth']

    def test_the_skills_list_gets_the_same_treatment(self, tmp_path):
        """One helper, not two — the defect above was copied from that line."""
        rendered = tomllib.loads(
            render(
                tmp_path, **REQUIRED, USER_DISABLED_SKILLS='browse, whi"sper,'
            ).read_text()
        )

        assert rendered["users"]["testuser"]["disabled_skills"] == [
            "browse", 'whi"sper',
        ]

    def test_the_deployment_toggle_and_the_opt_out_are_separate_axes(self, tmp_path):
        """``ISTOTA_FEEDS_ENABLED`` writes the resource; this hides the module.

        Both are meaningful at once, which is why one does not stand in for
        the other: the resource block can exist while the user has the module
        switched off.
        """
        rendered = render(
            tmp_path,
            **REQUIRED,
            ISTOTA_FEEDS_ENABLED="true",
            USER_DISABLED_MODULES="feeds",
        )

        assert _resource(tomllib.loads(rendered.read_text()), "feeds")
        assert not load_config(rendered).is_module_enabled("testuser", "feeds")

    @pytest.mark.parametrize(
        "var, section, key",
        [
            ("ISTOTA_DISABLED_SKILLS", None, "disabled_skills"),
            ("ISTOTA_EXPERIMENTAL_FEATURES", "experimental", "features"),
        ],
    )
    def test_every_comma_list_the_render_writes_uses_the_one_helper(
        self, tmp_path, var, section, key
    ):
        """The deployment-wide lists, which kept the defect after the per-user
        ones were fixed.

        Four places rendered a comma-separated value with the same
        ``sed 's/[^,]*/"&"/g'`` expression and two were repaired; a helper that
        two of its four callers do not use is a fix that has not landed. Both
        of these are operator-typed in ``docker/.env`` exactly as the per-user
        pair is, and both are *global* — a quote in either writes a
        ``config.toml`` nothing can parse, which on a first boot has no
        ``config.toml.prev`` to fall back to and under
        ``restart: unless-stopped`` is a crash loop for the whole stack rather
        than for one user's setting.

        Asserted through ``tomllib`` rather than on the rendered text: parsing
        at all is the property, and the value is nonsense either way.
        """
        rendered = tomllib.loads(
            render(tmp_path, **REQUIRED, **{var: 'alpha, br"avo,'}).read_text()
        )
        holder = rendered[section] if section else rendered

        assert holder[key] == ["alpha", 'br"avo']


class TestTheWebBlock:
    def test_oauth_needs_both_halves_of_the_client_credential(self, tmp_path):
        # A client id with no secret is a half-provisioned Nextcloud, and the
        # rendered [web] block would name an oauth2 flow that cannot complete.
        rendered = tomllib.loads(
            render(tmp_path, **REQUIRED, OAUTH_CLIENT_ID="only-the-id").read_text()
        )

        assert "oauth2_client_id" not in rendered.get("web", {})

    def test_a_complete_oauth_credential_renders_the_endpoints_off_nc_url(
        self, tmp_path
    ):
        rendered = tomllib.loads(
            render(
                tmp_path,
                **REQUIRED,
                OAUTH_CLIENT_ID="client-id",
                OAUTH_CLIENT_SECRET="client-secret",
            ).read_text()
        )
        web = rendered["web"]

        assert web["oauth2_client_id"] == "client-id"
        assert web["oauth2_token_endpoint"].startswith("http://nextcloud:80/")

    def test_each_run_mints_a_fresh_session_secret(self, tmp_path):
        """With no caller supplying one — the image tier and the lean stack.

        Under the entrypoint it is an input rather than something minted here,
        resolved once per boot so a re-render keeps every logged-in session
        (``test_entrypoint_config_stage.TestWhatARerenderMustNotLose``).
        """

        def secret(where: Path) -> str:
            rendered = tomllib.loads(
                render(
                    where,
                    **REQUIRED,
                    OAUTH_CLIENT_ID="client-id",
                    OAUTH_CLIENT_SECRET="client-secret",
                ).read_text()
            )
            return rendered["web"]["session_secret_key"]

        assert secret(tmp_path / "a") != secret(tmp_path / "b")

    def test_a_supplied_session_secret_is_used_verbatim(self, tmp_path):
        rendered = tomllib.loads(
            render(
                tmp_path,
                **REQUIRED,
                OAUTH_CLIENT_ID="client-id",
                OAUTH_CLIENT_SECRET="client-secret",
                WEB_SESSION_SECRET="handed-over-by-the-entrypoint",
            ).read_text()
        )

        assert rendered["web"]["session_secret_key"] == "handed-over-by-the-entrypoint"


class TestTheInputContract:
    """What the script does when its caller gets the hand-off wrong.

    The entrypoint calls this as a subprocess precisely so a missing input
    aborts the render and not the boot. That only holds if the render actually
    aborts, rather than writing a config with an empty Nextcloud URL in it.
    """

    @pytest.mark.parametrize("missing", REQUIRED_INPUTS)
    def test_a_missing_required_input_fails_the_render(self, tmp_path, missing):
        env = {k: v for k, v in REQUIRED.items() if k != missing}
        config_file = tmp_path / "config.toml"
        proc = subprocess.run(
            ["bash", str(RENDER_CONFIG)],
            env={
                "PATH": os.environ.get("PATH", ""),
                "CONFIG_FILE": str(config_file),
                **env,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0, f"rendered anyway without {missing}"
        assert missing in proc.stderr, (
            f"the failure does not name {missing}; an operator reading this "
            f"boot log has to guess.\n{proc.stderr}"
        )
        # The assertion the preflight actually exists for, and the one the first
        # draft of this test left out. Bare `set -u` also exits non-zero and
        # also names the variable — but only *after* the first `cat >` has
        # truncated the destination, leaving 374 bytes of config that the
        # entrypoint's file-exists guard accepts as complete forever. Without
        # this line the test passes with the preflight deleted.
        assert not config_file.exists(), (
            f"a partial config was written despite the missing {missing}"
        )

    def test_config_file_itself_is_required(self, tmp_path):
        proc = subprocess.run(
            ["bash", str(RENDER_CONFIG)],
            env={"PATH": os.environ.get("PATH", ""), **REQUIRED},
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0
        assert "CONFIG_FILE" in proc.stderr

    def test_a_failure_part_way_through_leaves_no_config_behind(self, tmp_path):
        """The failure mode that turns into a silent production incident.

        Everything the preflight does not cover fails *after* the first
        ``cat >`` has truncated the destination: python3 absent for the session
        secret, ENOSPC, a future unguarded ``${VAR}``. entrypoint.sh then finds a
        file on the next boot, skips the render for good, runs the backfill
        passes over the fragment and execs the daemon on it.

        Reproduced by breaking ``python3``, which the render shells out to for
        the session secret — a real dependency of the script, failing at a point
        the preflight cannot reach, rather than a fault injected into the render
        itself. A stub in front of the real PATH rather than an empty PATH,
        which would take ``cat``, ``mv`` and ``sed`` with it and fail the render
        for a reason that has nothing to do with the property under test.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        config_file = tmp_path / "config.toml"
        stub_bin = tmp_path / "bin"
        stub_bin.mkdir()
        broken = stub_bin / "python3"
        broken.write_text("#!/bin/sh\necho 'python3 is broken' >&2\nexit 1\n")
        broken.chmod(0o755)

        proc = subprocess.run(
            ["bash", str(RENDER_CONFIG)],
            env={
                "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
                "CONFIG_FILE": str(config_file),
                **REQUIRED,
                # So the session secret — the first python3 call — is reached.
                "OAUTH_CLIENT_ID": "client-id",
                "OAUTH_CLIENT_SECRET": "client-secret",
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0, "the render reported success without python3"
        assert not config_file.exists(), (
            "a truncated config.toml was left on disk; the entrypoint's "
            "file-exists guard will treat it as complete on every later boot"
        )
        assert not list(tmp_path.glob("*.partial")), "the partial file was not cleaned up"

    def test_the_script_is_executable_and_parses_under_bash(self):
        # `sh -n` would check the wrong grammar: the file is #!/bin/bash and
        # /bin/sh is dash in the image.
        assert os.access(RENDER_CONFIG, os.X_OK), "render-config.sh is not executable"
        subprocess.run(["bash", "-n", str(RENDER_CONFIG)], check=True, timeout=30)


class TestTheEntrypointStillOwnsWhatItKept:
    """Guards on the seam, not on the script.

    A future edit that re-inlines the render breaks the property Stage 4 was
    for, and is cheap to catch by reading the entrypoint.
    """

    def test_the_entrypoint_calls_the_script_rather_than_inlining_the_render(self):
        entrypoint = (REPO / "docker" / "istota" / "entrypoint.sh").read_text()

        assert "render-config.sh" in entrypoint
        # The unmistakable first line of the old inline block.
        assert "# Istota configuration — generated by Docker entrypoint" not in entrypoint

    def test_every_provisioning_local_the_render_reads_is_exported_to_it(self):
        """The one failure mode the extraction itself creates.

        Before the split, a variable the render read was in scope by
        construction. Now it has to be named in the entrypoint's ``export``
        list, and a name that is missing renders as its ``:-`` default or empty
        — silently, in production, while every test here stays green, because
        these tests fabricate the environment directly and never exercise the
        hand-off.

        ``LOCATION_INGEST_TOKEN`` is the shape to worry about: assigned in the
        entrypoint's provisioning phase, never present in docker-compose.yml, so
        nothing else would put it in the child's environment.

        ISTOTA_* is excluded because those reach the container from compose
        rather than from the entrypoint. Names assigned inside the render are
        read from the file rather than listed here, so adding a local does not
        mean editing this test.
        """
        rendered = RENDER_CONFIG.read_text()
        entrypoint = (REPO / "docker" / "istota" / "entrypoint.sh").read_text()

        # Comments are stripped first: the header documents the contract in
        # prose and names variables inside it, including placeholders like
        # `${VAR}`, none of which the script actually reads.
        code = "\n".join(
            line for line in rendered.splitlines() if not line.lstrip().startswith("#")
        )
        referenced = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", code))
        assigned = set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)=", code, re.M))

        needed = {n for n in referenced - assigned if not n.startswith("ISTOTA_")}
        assert needed, "the scan found nothing to check; the regex has rotted"

        # The export that hands off to the render, not the unrelated one-liner
        # for ISTOTA_ADMINS_FILE near the top of the entrypoint. Identified by
        # the input every render must receive.
        blocks = [
            match.group(1)
            for match in re.finditer(
                r"^\s*export\s+((?:[^\n]*\\\n)*[^\n]*)", entrypoint, re.M
            )
            if "CONFIG_FILE" in match.group(1)
        ]
        assert len(blocks) == 1, (
            f"expected exactly one export block naming CONFIG_FILE, found {len(blocks)}"
        )
        exported = set(re.findall(r"[A-Z][A-Z0-9_]*", blocks[0]))

        missing = sorted(needed - exported)
        assert not missing, (
            f"render-config.sh reads {missing}, which entrypoint.sh does not "
            "export to it. Each would render as its default or empty on a real "
            "boot while every test in this file still passes."
        )

    # Every ``ISTOTA_*`` name the render reads has to arrive through compose,
    # so the scan needs no allowlist. This set is here for the name that one
    # day legitimately does not — a value compose is right to withhold, with
    # the reason written down. It is empty today, and the emptiness is the
    # point: nothing was excluded to make the blanket scan pass.
    COMPOSE_WITHHOLDS: dict[str, str] = {}

    def test_every_var_the_render_reads_is_passed_by_compose(self):
        """The other half of the hand-off, which nothing checked.

        The test above excludes ``ISTOTA_*`` on the grounds that compose puts
        those in the container. Nothing held compose to it, and the failure is
        the same silent one: the render substitutes its ``:-`` default and the
        setting is simply absent from the config, in production, with the suite
        green.

        This ran over four prefixes before it ran over all of them, and each of
        the four was added *after* the family it names had already broken.
        ISSUE-289 was the reviewer setting, present in the Ansible role and the
        render and absent from compose, which cost nothing until an MR opened
        with nobody on it. The email pair was ``ISTOTA_EMAIL_AUTHSERV_ID`` and
        ``ISTOTA_EMAIL_CONFIRM_SENDER_MATCH``, both documented in
        ``docker/.env.example`` and read by the render — so an operator asking
        for ``confirm_sender_match = "gate"`` on a Docker deploy silently got
        ``off``, which is the gate switched off rather than a setting ignored.
        ``ISTOTA_NEXTCLOUD_`` joined them with ``dav_prefix`` and
        ``auto_share_bot_dir``, and ``ISTOTA_WEB_MAP_`` with the basemap family
        (ISSUE-334). A list that only ever grows after an incident is a list
        that is wrong until the next incident.

        The docstring for the scoped version gave a real reason for it: names
        the entrypoint computes itself, like ``LOCATION_INGEST_TOKEN``, would
        fail a blanket scan for the wrong reason. But those carry no ``ISTOTA_``
        prefix, and the neighbouring test already checks the entrypoint half
        separately — so the blanket scan over that prefix has no such name in
        it to trip on. Measured when the scoping came off: 170 names read, and
        the only three the four prefixes were hiding.

        The three were ``ISTOTA_LOGGING_ROTATE``, ``_MAX_SIZE_MB`` and
        ``_BACKUP_COUNT``. Log rotation was not switchable on the Docker shape
        and the retention numbers were pinned at 10 MB and 5 files, because
        ``ISTOTA_LOGGING_`` was not one of the four.
        """
        code = "\n".join(
            line
            for line in RENDER_CONFIG.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        read = set(re.findall(r"\$\{?(ISTOTA_[A-Z0-9_]*)", code))
        assert len(read) > 100, "the scan found almost no reads; the regex has rotted"

        compose = (REPO / "docker" / "docker-compose.yml").read_text()
        passed = set(re.findall(r"^\s*(ISTOTA_[A-Z0-9_]*):", compose, re.M))

        stale = sorted(set(self.COMPOSE_WITHHOLDS) - read)
        assert not stale, (
            f"COMPOSE_WITHHOLDS names {stale}, which render-config.sh no longer "
            "reads. Drop the entry rather than leaving a hole open."
        )

        missing = sorted(read - passed - set(self.COMPOSE_WITHHOLDS))
        assert not missing, (
            f"render-config.sh reads {missing}, which docker-compose.yml does "
            "not pass into the container. Each renders as its default or empty "
            "on a real boot, unsettable by the operator. If compose is right to "
            "withhold one, say so in COMPOSE_WITHHOLDS with the reason."
        )

    def test_every_settable_compose_var_is_documented_in_env_example(self):
        """The third leg of the same hand-off, which nothing checked either.

        render -> compose is the test above. compose -> the operator is this
        one: a variable compose interpolates from the environment is one the
        operator is meant to set, and the only place they can learn it exists
        is ``docker/.env.example``. ``ISTOTA_NEXTCLOUD_DAV_PREFIX`` was found
        working and undocumented; it turned out to be pinned rather than
        settable, which is what this test's split makes checkable instead of a
        judgement someone re-makes by reading.

        Settable means compose passes the name through an interpolation *of
        that same name* — ``FOO: ${FOO:-x}``. That is what an operator can
        reach from ``.env``. A literal (``ISTOTA_NEXTCLOUD_AUTO_SHARE_BOT_DIR:
        "false"``), an anchor (``*shared_mount_name``) or a container-internal
        path (``ISTOTA_WEB_STATIC_DIR``) is pinned by the stack on purpose,
        each with its reason inline, and documenting it in ``.env.example``
        would invite an operator to set something that does nothing.

        Six are pinned today and the split is derived, not listed, so pinning
        a variable or opening one up moves it between the halves without
        anybody editing this test.
        """
        compose = "\n".join(
            line
            for line in (REPO / "docker" / "docker-compose.yml").read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        settable = set(
            re.findall(r"^\s*(ISTOTA_[A-Z0-9_]*):\s*\$\{\1[:\-?}]", compose, re.M)
        )
        assert len(settable) > 100, "the scan found almost no reads; the regex has rotted"

        env_example = (REPO / "docker" / ".env.example").read_text()
        documented = set(re.findall(r"^#?\s*(ISTOTA_[A-Z0-9_]*)=", env_example, re.M))

        missing = sorted(settable - documented)
        assert not missing, (
            f"docker-compose.yml lets the operator set {missing}, which "
            "docker/.env.example never mentions. It works and there is no way "
            "to learn it exists. Document it, or pin it in compose if it is "
            "not meant to be set."
        )

    #: Names whose three layers state different defaults on purpose.
    #:
    #: Empty, and meant to stay that way. The seven module gates that used to
    #: be listed here are covered by `_is_fail_closed_module_gate` instead —
    #: they were one decision written out seven times, and the eighth module's
    #: gate would have been a silent hole until somebody noticed the omission.
    #:
    #: A name still belongs here when the divergence is genuinely a one-off
    #: rather than an instance of a rule. Prefer widening a rule to adding a
    #: line: a map is a list nobody reads, and the failure mode is that it
    #: accumulates entries which stopped being true years ago.
    DEFAULT_DIVERGES: dict[str, str] = {}

    def test_the_module_gate_rule_covers_the_shipped_posture(self):
        gate = {"render": "false", "compose": "true", "env_example": "true"}
        assert _is_fail_closed_module_gate("ISTOTA_FEEDS_ENABLED", gate)
        # compose alone is enough; .env.example need not state one
        assert _is_fail_closed_module_gate(
            "ISTOTA_FEEDS_ENABLED", {"render": "false", "compose": "true"}
        )

    @pytest.mark.parametrize(
        "name,by_layer,why",
        [
            (
                "ISTOTA_FEEDS_ENABLED",
                {"render": "true", "compose": "false"},
                "the opposite posture: a stack shipping a module off while "
                "every standalone render turns it on",
            ),
            (
                "ISTOTA_SCHEDULER_WORKER_IDLE_TIMEOUT",
                {"render": "10", "compose": "30"},
                "not a gate; a non-boolean divergence is a bug rather than a "
                "posture",
            ),
            (
                "ISTOTA_FEEDS_ENABLED",
                {"render": "false", "compose": "true", "env_example": "false"},
                "one layer dissents, so the operator is still misinformed",
            ),
            (
                "ISTOTA_FEEDS_ENABLED",
                {"render": "false"},
                "nothing to diverge from",
            ),
        ],
    )
    def test_the_module_gate_rule_still_reports_everything_else(
        self, name, by_layer, why
    ):
        """The control. A rule that exempts more than the posture it names is
        worse than the seven entries it replaced, because nothing lists what it
        is covering."""
        assert not _is_fail_closed_module_gate(name, by_layer), why

    def test_the_rule_covers_every_gate_that_used_to_be_listed(self):
        """The seven names ISSUE-444 measured, pinned so emptying the map is
        checkable rather than asserted. If a future edit narrows the rule, this
        says which module stopped being covered."""
        was_listed = {
            "ISTOTA_BROWSER_ENABLED",
            "ISTOTA_DEVELOPER_ENABLED",
            "ISTOTA_EMAIL_ENABLED",
            "ISTOTA_FEEDS_ENABLED",
            "ISTOTA_LOCATION_ENABLED",
            "ISTOTA_MEMORY_SEARCH_ENABLED",
            "ISTOTA_MONEY_ENABLED",
        }
        shipped = {"render": "false", "compose": "true", "env_example": "true"}
        uncovered = sorted(
            n for n in was_listed if not _is_fail_closed_module_gate(n, shipped)
        )
        assert not uncovered, f"the rule no longer covers {uncovered}"

    def test_the_three_layers_agree_on_every_default_they_state(self):
        """The values half of the hand-off, which nothing checked.

        The two guards above are about *names*: every name the render reads is
        passed, and every name compose lets the operator set is documented.
        Both were green while ``ISTOTA_BROWSER_API_URL`` said
        ``http://istota-browser:9223`` in ``.env.example`` and
        ``http://localhost:9223`` in the other two — the istota container's own
        loopback, where nothing listens. Comment that one line out of a
        working ``.env`` and ``browse`` stops, with no error naming a cause.

        Three layers state a default and all three have to agree:

        - the render's own ``${NAME:-value}`` fallback
        - compose's ``NAME: ${NAME:-value}``, where it interpolates that name
        - the uncommented ``NAME=value`` line in ``docker/.env.example``

        **An empty value is not a default, it is a pass-through**, and that is
        a property of the mechanism rather than a convenience here: ``:-``
        substitutes on unset *or empty* at every layer, so ``NAME=`` in
        ``.env`` reaches compose's default, and compose's ``${NAME:-}`` reaches
        the render's. Only one non-empty value can ever be in force, so only
        the non-empty statements are compared. The one direction that hides is
        a render whose own fallback is empty where compose supplies one, which
        would render empty outside compose; there are none today.

        A compose **literal** is deliberately outside the scan, on the same
        split ``test_every_settable_compose_var_is_documented_in_env_example``
        draws: a literal is pinned by the stack rather than being a default an
        operator competes with, so there is no three-way to reconcile. It does
        mean the two pinned ``ISTOTA_NEXTCLOUD_*`` values disagree with the
        render's own defaults unseen by this test, which is why
        ``docker/.env.example`` says so in prose.

        A **nested** default is outside it too, and that one is a blind spot
        rather than a decision: both patterns bound the value with ``[^{}]*``,
        so ``${A:-${B:-x}}`` matches neither side and is skipped on both.
        Three compose entries are of that shape today
        (``ISTOTA_WEB_CALLBACK_URL``, ``_NC_EXTERNAL_URL``, ``_SITE_HOSTNAME``)
        and so are their render counterparts, so nothing false-greens — but
        those derive from different inputs on each side, which is a comparison
        no widening of this pattern would make meaningful.
        """
        render = "\n".join(
            line
            for line in RENDER_CONFIG.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        # Per layer, not a flat set of values: the module-gate rule below is
        # directional. `render=false, compose=true` is the shipped fail-closed
        # posture; `render=true, compose=false` is the opposite one and a bug,
        # and a set cannot tell the two apart.
        stated: dict[str, dict[str, str]] = {}

        def state(layer: str, name: str, value: str) -> None:
            if value:
                stated.setdefault(name, {})[layer] = value

        for match in re.finditer(r"\$\{(ISTOTA_[A-Z0-9_]*):-([^{}]*)\}", render):
            state("render", *match.groups())
        read = set(stated)
        assert len(read) > 100, "the scan found almost no fallbacks; the regex has rotted"

        compose = "\n".join(
            line
            for line in (REPO / "docker" / "docker-compose.yml").read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        for match in re.finditer(
            r"^\s*(ISTOTA_[A-Z0-9_]*):\s*\$\{\1:-([^{}]*)\}\s*$", compose, re.M
        ):
            state("compose", *match.groups())

        env_example = (REPO / "docker" / ".env.example").read_text()
        for match in re.finditer(r"^(ISTOTA_[A-Z0-9_]*)=(.*)$", env_example, re.M):
            state("env_example", match.group(1), match.group(2).strip())

        disagree = {n for n, v in stated.items() if len(set(v.values())) > 1}
        by_rule = {n for n in disagree if _is_fail_closed_module_gate(n, stated[n])}

        stale = sorted(set(self.DEFAULT_DIVERGES) - disagree)
        assert not stale, (
            f"DEFAULT_DIVERGES names {stale}, whose layers now agree. Drop the "
            "entry rather than leaving a hole open."
        )
        overlap = sorted(set(self.DEFAULT_DIVERGES) & by_rule)
        assert not overlap, (
            f"DEFAULT_DIVERGES names {overlap}, which the module-gate rule "
            "already covers. Drop the entry: a name in both is how a map "
            "starts restating a rule."
        )

        offenders = sorted(disagree - by_rule - set(self.DEFAULT_DIVERGES))
        # `.env.example` is documentation, so a disagreement confined to it
        # misleads the operator without changing what the daemon runs. It is
        # still a defect — it is the file people copy — but it fails for a
        # different reason and the message says which one applies.
        behavioural = [
            n for n in offenders
            if stated[n].get("render") != stated[n].get("compose")
            and {"render", "compose"} <= set(stated[n])
        ]
        assert not offenders, (
            "these variables are given different non-empty defaults by "
            "render-config.sh, docker-compose.yml and docker/.env.example:\n"
            + "\n".join(
                f"  {n}: {stated[n]}"
                + ("  <- render and compose disagree: this one changes behaviour"
                   if n in behavioural else
                   "  <- documentation only: .env.example misstates a default")
                for n in offenders
            )
            + "\nWhichever layer the operator does not edit wins, silently. "
            "Make them agree, record the divergence in DEFAULT_DIVERGES with "
            "the reason it is deliberate, or express it as a rule beside "
            "_is_fail_closed_module_gate if it is an instance of one."
        )

    # The backfill passes this class used to hold in place are gone (ISSUE-368).
    # They were the repair path for a config the render would never rewrite, and
    # the render now runs on every boot, so all three would be a second writer of
    # keys the render already produces. What replaced the assertion lives in
    # `tests/test_entrypoint_config_stage.py::TestTheRenderIsTheOnlyWriter`,
    # beside the tests for the stage that made them redundant.


ENTRYPOINT = REPO / "docker" / "istota" / "entrypoint.sh"


def _credential_block() -> str:
    """The entrypoint's credential if/elif chain, lifted out to be run.

    Nothing in the suite can execute ``entrypoint.sh`` — it provisions against
    a live Nextcloud before reaching this point — but the chain itself only
    reads environment variables and echoes, so it runs standalone. Extracted by
    text, which fails loudly (no match, no test) rather than silently drifting.
    """
    source = ENTRYPOINT.read_text()
    match = re.search(
        r'^if \[ -n "\$\{CLAUDE_CODE_OAUTH_TOKEN:-\}" \]; then\n.*?^fi$',
        source,
        re.M | re.S,
    )
    assert match, "the credential chain moved; this extraction needs updating"
    return match.group(0)


def _run_credential_block(tmp_path: Path, **env: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", _credential_block()],
        env={
            "PATH": os.environ.get("PATH", ""),
            "CLAUDE_DIR": str(tmp_path),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"chain exited {proc.returncode}: {proc.stderr}"
    return proc.stdout


class TestTheCredentialCheckKnowsWhichBrainIsRunning:
    """A native deployment has credentials; it just doesn't have Claude Code's.

    The chain only knew the two Claude Code credentials, so every native-brain
    boot — with a working ``ISTOTA_BRAIN_NATIVE_API_KEY`` and tasks completing
    fine — printed "No Claude Code credentials found". A warning that fires on
    a healthy deployment is one an operator learns to scroll past.
    """

    def test_the_native_key_is_recognised_as_a_credential(self, tmp_path):
        out = _run_credential_block(
            tmp_path,
            ISTOTA_BRAIN_KIND="native",
            ISTOTA_BRAIN_NATIVE_API_KEY="a-key",
        )

        assert "WARNING" not in out
        assert "ISTOTA_BRAIN_NATIVE_API_KEY" in out

    def test_a_native_deployment_with_no_key_is_told_which_one_it_needs(self, tmp_path):
        out = _run_credential_block(tmp_path, ISTOTA_BRAIN_KIND="native")

        assert "WARNING" in out
        assert "ISTOTA_BRAIN_NATIVE_API_KEY" in out
        # Naming Claude Code's variables here sends the operator to set a
        # credential the native brain will never read.
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in out

    def test_the_claude_code_default_still_warns_about_its_own_credentials(self, tmp_path):
        out = _run_credential_block(tmp_path)

        assert "WARNING" in out
        assert "CLAUDE_CODE_OAUTH_TOKEN" in out

    def test_a_shared_anthropic_key_satisfies_either_brain(self, tmp_path):
        for kind in ("claude_code", "native"):
            out = _run_credential_block(
                tmp_path, ISTOTA_BRAIN_KIND=kind, ANTHROPIC_API_KEY="a-key",
            )
            assert "WARNING" not in out, kind

    def test_the_oauth_branch_still_writes_a_locked_down_credentials_file(self, tmp_path):
        out = _run_credential_block(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="tok")

        assert "WARNING" not in out
        written = tmp_path / ".credentials.json"
        assert written.exists()
        assert oct(written.stat().st_mode)[-3:] == "600"

    def test_the_variables_it_branches_on_reach_the_container(self):
        """Read by the entrypoint, so compose has to pass both through."""
        compose = (REPO / "docker" / "docker-compose.yml").read_text()

        for name in ("ISTOTA_BRAIN_KIND", "ISTOTA_BRAIN_NATIVE_API_KEY"):
            assert re.search(rf"^\s*{name}:", compose, re.M), name


def _compose_service_env(service: str) -> dict:
    """The `environment:` mapping of one compose service, as YAML sees it.

    Parsed rather than regexed because the question is per-service and the
    same variable name appears under several of them. The file uses YAML
    anchors, which ``safe_load`` resolves.
    """
    import yaml

    document = yaml.safe_load((REPO / "docker" / "docker-compose.yml").read_text())
    return document["services"][service].get("environment") or {}


class TestTheWebServiceCanReachTheModel:
    """The web process executes a model on three request-scope paths.

    ``web`` used to get only ``ISTOTA_BRAIN_NATIVE_API_KEY``, on the assumption
    that the only model call it makes is the web chat surface driving the
    native brain. It is not: the health OCR extractors, the biomarker explainer
    and shared-block run-now all build a ``BrainRequest`` of their own inside a
    request handler, and every one of them takes its environment from
    ``executor.build_model_cli_env``, which copies the credential names below
    out of *the calling process's* environment. In the ``web`` container none
    of them existed, so on the default ``claude_code`` shape all three
    authenticated with nothing.

    The names come from the product rather than from a list here, so a fourth
    credential is covered by adding it to compose's ``istota`` block and to the
    set the model-call env builder reads — not by editing this test. The
    intersection with what ``istota`` is passed is what keeps it honest in the
    other direction: a name the daemon itself is not given is not a credential
    this stack has to route anywhere.

    What the intersection therefore cannot see is a name missing from *both*
    services, and there are two today: ``ANTHROPIC_AUTH_TOKEN`` and
    ``ANTHROPIC_BASE_URL`` reach neither, so a Docker operator behind a gateway
    has no way to point the CLI brain at it. That is a gap in the stack rather
    than in this test — add the pair to both services and to ``.env.example``
    and these assertions cover them with no edit here.
    """

    def _model_credential_names(self) -> set[str]:
        # Imported inside the test: this module otherwise pulls in only
        # `istota.config`, and `executor` carries a much larger graph.
        from istota import executor
        from istota.claude_runtime_env import CLAUDE_RUNTIME_ENV_VARS

        return set(executor._MODEL_CLI_ENDPOINT_VARS) | set(CLAUDE_RUNTIME_ENV_VARS)

    def test_every_model_credential_the_daemon_gets_reaches_the_web_service(self):
        names = self._model_credential_names()
        istota_env = _compose_service_env("istota")
        web_env = _compose_service_env("web")

        passed_to_daemon = names & set(istota_env)
        assert passed_to_daemon, (
            "compose passes the istota service none of the credential names "
            f"{sorted(names)} that build_model_cli_env reads; the scan has rotted"
        )

        missing = sorted(passed_to_daemon - set(web_env))
        assert not missing, (
            f"docker-compose.yml passes {missing} to the istota service and not "
            "to web. The web process calls a model on three request-scope paths "
            "(health OCR, the biomarker explainer, shared-block run-now) and "
            "build_model_cli_env reads these out of its own environment, so each "
            "of those authenticates with nothing."
        )

    def test_the_web_service_reads_them_the_same_way_the_daemon_does(self):
        """Same interpolation shape, so one ``.env`` entry serves both."""
        istota_env = _compose_service_env("istota")
        web_env = _compose_service_env("web")

        for name in sorted(self._model_credential_names() & set(istota_env)):
            assert web_env[name] == istota_env[name], (
                f"{name} reaches web through {web_env[name]!r} and istota "
                f"through {istota_env[name]!r}. An operator setting it once in "
                ".env has to reach both processes."
            )


class TestTheContainerCliFindsTheSameConfig:
    """`docker compose exec istota istota <verb>` has to reach the daemon's config.

    ``entrypoint.sh`` hands the daemon ``-c /data/config/config.toml``, which is
    on none of ``load_config``'s four search paths. Without the environment
    variable an operator following ``docs/deployment/docker.md`` got a raw
    ``sqlite3.OperationalError`` traceback from every CLI call. Worse than
    failing would be resolving a *different* config from the daemon's, so what
    is asserted is that the two name one file.
    """

    def _istota_service_env(self) -> dict:
        compose = (REPO / "docker" / "docker-compose.yml").read_text()
        # The istota service's environment block: from its `environment:` key
        # to the start of the next top-level service. Parsed by hand rather
        # than with a YAML loader because the file is full of `${VAR:-default}`
        # and anchors that a plain load would not resolve the way compose does.
        body = compose.split("\n  istota:\n", 1)[1].split("\n  web:\n", 1)[0]
        return dict(
            re.findall(r"^      ([A-Z][A-Z0-9_]*): (.*)$", body, re.M)
        )

    def test_the_service_names_a_config_path_for_the_cli(self):
        env = self._istota_service_env()

        assert env.get("ISTOTA_CONFIG_PATH") == "/data/config/config.toml"

    def test_it_is_the_file_the_entrypoint_hands_the_daemon(self):
        entrypoint = (REPO / "docker" / "istota" / "entrypoint.sh").read_text()
        declared = re.search(r'^CONFIG_FILE="([^"]+)"', entrypoint, re.M)
        assert declared, "entrypoint.sh no longer declares CONFIG_FILE"

        assert self._istota_service_env()["ISTOTA_CONFIG_PATH"] == declared.group(1)

    def test_the_variable_is_the_one_load_config_reads(self):
        """A typo here fails open — the CLI keeps its old traceback."""
        from istota import config as config_module

        source = Path(config_module.__file__).read_text()
        assert 'os.environ.get("ISTOTA_CONFIG_PATH")' in source


class TestTheRoomSelectableAllowlist:
    """The key that decides whether per-room brain selection exists here at all.

    `render-config.sh` is the single writer of `config.toml` and runs on every
    boot, so a key it does not emit cannot be set on this shape: a hand edit to
    the rendered file survives until the next restart. The feature shipped with
    no variable and no line here, which made it unreachable on Docker rather
    than merely off — and Docker is the shape the smoke stack runs.
    """

    def test_the_default_renders_no_key_at_all(self, tmp_path):
        """Unset must not render `room_selectable = []`.

        An empty list and an absent key load identically, so this is about the
        file an operator reads: a key printed at its own default in a file that
        is rewritten every boot invites an edit that cannot survive.
        """
        rendered = render(tmp_path, **REQUIRED).read_text()

        assert "room_selectable" not in rendered
        assert load_config(render(tmp_path, **REQUIRED)).brain.room_selectable == []

    def test_a_comma_separated_list_reaches_the_loaded_config(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_BRAIN_ROOM_SELECTABLE="claude_code,native",
            )
        )

        assert config.brain.room_selectable == ["claude_code", "native"]

    def test_the_rendered_list_is_offered_to_a_room(self, tmp_path):
        """The loaded value has to survive `room_selectable_kinds`.

        `load_config` accepting the key is not the property that matters — the
        picker reads the allowlist intersected with the kinds `make_brain` can
        build, so a render that survives TOML and dies there would leave the
        feature just as unreachable as no line at all.
        """
        from istota.brain import room_selectable_kinds

        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_BRAIN_ROOM_SELECTABLE="claude_code,native",
            )
        )

        assert room_selectable_kinds(config.brain) == {"claude_code", "native"}

    def test_whitespace_and_empty_entries_are_dropped(self, tmp_path):
        """`a, b,` is what an operator's `.env` actually looks like."""
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_BRAIN_ROOM_SELECTABLE=" claude_code , native ,",
            )
        )

        assert config.brain.room_selectable == ["claude_code", "native"]

    def test_a_quote_in_a_name_survives_as_a_value_rather_than_breaking_the_file(
        self, tmp_path
    ):
        """A quote must not produce an unparseable config.toml.

        On this shape that is a container that will not boot, from a typo in an
        operator's `.env`. It goes through `toml_escape` like every other
        operator-typed value, so the file parses and the bad name arrives as a
        string. Judging it is not this script's job: `_validate_room_selectable`
        warns at load about a name `make_brain` cannot build, and
        `room_selectable_kinds` never offers it — so the operator gets a reason,
        and the names either side of it keep working.
        """
        from istota.brain import room_selectable_kinds

        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_BRAIN_ROOM_SELECTABLE='claude_code,na"tive,tmux_claude',
            )
        )

        assert config.brain.room_selectable == ["claude_code", 'na"tive', "tmux_claude"]
        assert room_selectable_kinds(config.brain) == {"claude_code", "tmux_claude"}

    def test_a_value_of_only_separators_renders_no_key(self, tmp_path):
        """Not `room_selectable = []`, and not a stray `= [ ]` either."""
        rendered = render(
            tmp_path, **REQUIRED, ISTOTA_BRAIN_ROOM_SELECTABLE=" , ,"
        ).read_text()

        assert "room_selectable" not in rendered


class TestTheModelAliasTable:
    """The per-role model map has to land under the key the loader reads.

    ``config.py`` calls ``[models.roles]`` a HARD RENAME to ``[models.aliases]``
    — parsed by nothing, warned about once per process. The render kept writing
    the old spelling, so ``ISTOTA_BRAIN_NATIVE_MODEL_{FAST,GENERAL,SMART}``
    were documented in ``.env.example``, passed through by compose, and then
    silently dropped on the floor. Nothing pointed a role anywhere.
    """

    NATIVE = {
        **REQUIRED,
        "ISTOTA_BRAIN_KIND": "native",
        "ISTOTA_BRAIN_NATIVE_MODEL": "vendor/base-model",
    }

    def test_the_per_role_overrides_survive_into_the_loaded_config(self, tmp_path):
        config = load_config(render(
            tmp_path,
            **self.NATIVE,
            ISTOTA_BRAIN_NATIVE_MODEL_FAST="vendor/cheap-model",
            ISTOTA_BRAIN_NATIVE_MODEL_SMART="vendor/big-model",
        ))

        assert config.models.aliases["fast"] == "vendor/cheap-model"
        assert config.models.aliases["smart"] == "vendor/big-model"
        # Unset roles fall back to the single configured model.
        assert config.models.aliases["general"] == "vendor/base-model"

    def test_each_role_defaults_to_the_one_configured_model(self, tmp_path):
        config = load_config(render(tmp_path, **self.NATIVE))

        assert config.models.aliases == {
            "fast": "vendor/base-model",
            "general": "vendor/base-model",
            "smart": "vendor/base-model",
        }

    def test_the_retired_key_is_not_written(self, tmp_path):
        rendered = tomllib.loads(render(tmp_path, **self.NATIVE).read_text())

        assert "roles" not in rendered.get("models", {}), (
            "[models.roles] is parsed by nothing and logs a migration warning "
            "on every process start"
        )

    def test_no_model_table_without_a_configured_model(self, tmp_path):
        """The table is the native brain's; nothing to map without one."""
        rendered = tomllib.loads(render(tmp_path, **REQUIRED).read_text())

        assert "models" not in rendered


class TestThePerBrainModelDefaults:
    """`[brain.claude_code]` / `[brain.tmux]` model + effort (ISSUE-418).

    The top-level `model` was the claude_code brain's own default living at the
    root, where the executor applied it to whatever brain ran. The generator now
    writes the per-brain keys instead, migrating `ISTOTA_MODEL` onto both CLI
    brains — done here rather than by the loader's deprecation path so a Docker
    deployment that never changes its `.env` stops warning at every boot.
    """

    def test_the_legacy_variable_fills_both_cli_brains(self, tmp_path):
        rendered = tomllib.loads(
            render(tmp_path, **REQUIRED, ISTOTA_MODEL="claude-opus-5").read_text()
        )

        assert rendered["brain"]["claude_code"]["model"] == "claude-opus-5"
        assert rendered["brain"]["tmux"]["model"] == "claude-opus-5"

    def test_the_top_level_key_is_no_longer_written(self, tmp_path):
        rendered = tomllib.loads(
            render(tmp_path, **REQUIRED, ISTOTA_MODEL="claude-opus-5").read_text()
        )

        assert "model" not in rendered, (
            "the top-level key is deprecated; writing it would make the loader "
            "warn on every boot about a value this generator controls"
        )

    def test_the_legacy_variable_never_reaches_the_native_brain(self, tmp_path):
        """The one direction the migration must refuse.

        An Anthropic model id cannot carry to an openai_compat endpoint, so
        migrating it there would be the defect inside the fix.
        """
        rendered = tomllib.loads(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_BRAIN_KIND="native",
                ISTOTA_MODEL="claude-opus-5",
                ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.3-flash",
            ).read_text()
        )

        assert rendered["brain"]["native"]["model"] == "z-ai/glm-5.3-flash"

    def test_an_explicit_per_brain_value_wins_over_the_legacy_one(self, tmp_path):
        rendered = tomllib.loads(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_MODEL="claude-opus-5",
                ISTOTA_BRAIN_CLAUDE_CODE_MODEL="haiku",
            ).read_text()
        )

        assert rendered["brain"]["claude_code"]["model"] == "haiku"

    def test_neither_block_is_written_without_a_value(self, tmp_path):
        """A key printed at its own default invites editing a generated file."""
        rendered = tomllib.loads(render(tmp_path, **REQUIRED).read_text())

        assert "claude_code" not in rendered.get("brain", {})
        assert "tmux" not in rendered.get("brain", {})

    def test_the_tmux_kind_renders_one_table_not_two(self, tmp_path):
        """The regression this class exists for.

        This file already wrote a `[brain.tmux]` header for the tmux kind, so
        emitting a second one for the model keys produced a duplicate table.
        That is invalid TOML, and on this shape a config.toml that does not
        parse is a container that will not boot — `render` itself still exits 0
        and writes the file, so nothing short of parsing it catches this.
        """
        rendered = tomllib.loads(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_BRAIN_KIND="tmux_claude",
                ISTOTA_MODEL="claude-opus-5",
            ).read_text()
        )

        tmux = rendered["brain"]["tmux"]
        assert tmux["model"] == "claude-opus-5"
        # The operability knobs still land in the same table.
        assert tmux["cli_version_pin"] == "2.1.168"
        assert tmux["fallback_trip_threshold"] == 5

    def test_the_tmux_block_carries_no_knobs_off_its_own_kind(self, tmp_path):
        """A claude_code primary with a tmux model set: keys, no operability."""
        rendered = tomllib.loads(
            render(
                tmp_path, **REQUIRED, ISTOTA_BRAIN_TMUX_MODEL="claude-opus-5"
            ).read_text()
        )

        assert rendered["brain"]["tmux"] == {"model": "claude-opus-5"}


# --------------------------------------------------------------------------
# The render restates defaults config.py already owns (ISSUE-430)
# --------------------------------------------------------------------------

# Keys where the rendered value is a *deployment fact* rather than a restated
# default: a container path, a provisioned credential, a URL derived from the
# inputs, or a block this test switched on itself. Comparing one to a dataclass
# default is meaningless, so the guard only records that it diverges.
_DEPLOYMENT_FACTS = {
    "db_path": "container path, /data/db is the volume",
    "temp_dir": "container path under the data volume",
    "skills_dir": "container path, /app is the image root",
    "nextcloud_mount_path": "container path, the mount point",
    "nextcloud.url": "provisioning input (NC_URL)",
    "nextcloud.username": "provisioning input (BOT_USER)",
    "nextcloud.app_password": "provisioning input (APP_PASSWORD)",
    "site.hostname": "derived from the web port / public host",
    "web.enabled": "the block is gated on the OAuth pair this test supplies",
    "web.oauth2_provider": "the browser-facing NC URL, a provisioning output",
    "web.oauth2_client_id": "registered by provision-nc.sh at first install",
    "web.oauth2_client_secret": "registered by provision-nc.sh at first install",
    "web.oauth2_token_endpoint": "derived from NC_URL (server-to-server leg)",
    "web.oauth2_userinfo_endpoint": "derived from NC_URL (server-to-server leg)",
    "web.oauth2_redirect_uri": "derived from the web port or the callback URL",
    "web.session_secret_key": "minted per render when the entrypoint supplies none",
}

def _env_var_for(key: str) -> str:
    """The `ISTOTA_*` variable a dotted config key is rendered from.

    The render's own naming convention, which holds for every gated block this
    test switches on. It does not hold everywhere — `nextcloud.url` comes from
    `NC_URL` and `web.enabled` from the OAuth pair — which is exactly why the
    rule below only fires when the derived name is one this test actually set,
    rather than assuming the convention is total.
    """
    return "ISTOTA_" + key.replace(".", "_").upper()


def _is_set_by_this_test(key: str, env: dict[str, str]) -> bool:
    """Whether a divergence is this test's own doing rather than a deployment fact.

    Five entries in the map below were of the form "this test sets
    ISTOTA_X_ENABLED to reach the block". That is an observation about the
    fixture, not a fact about the render, and it belongs in neither map: the
    test knows which variables it supplied, so it can derive the answer instead
    of restating it once per gate. Switching on a new block to widen the walk
    then costs nothing here, where before it silently required a map entry and
    failed with "unexplained divergence" until somebody added one.

    Narrow on purpose. It fires only when the derived variable is one this test
    passed in, so it can never exempt a key whose value came from the render
    itself — which is the whole question the surrounding class exists to ask.
    """
    return _env_var_for(key) in env


# Keys where the render deliberately states a different constant from the
# dataclass. Each pins the value, so narrowing one later is a visible edit here
# rather than a silent change to a shipped deployment.
_INTENTIONAL_DEFAULTS = {
    "scheduler.max_foreground_workers": (
        3,
        "container sizing: the image is sized smaller than a bare-metal host, "
        "which gets the dataclass's 5. Deliberate since a99571d2, where every "
        "other value in the same block was copied from config.py verbatim.",
    ),
    "scheduler.max_background_workers": (
        2,
        "container sizing, as above; bare metal gets the dataclass's 3.",
    ),
    "web.token_storage": (
        "encrypted",
        "Docker is the one shape that provisions the prerequisite itself: "
        "entrypoint.sh mints /data/.web_token_key and compose hands it to the "
        "web service, so post-as-user Talk mirroring works out of the box. "
        "Ansible leaves that key to the operator, so every other shape defaults "
        "to 'ephemeral' and stores no OAuth pair at rest.",
    ),
    "memory_search.enabled": (
        False,
        "the standalone render is conservative; docker-compose.yml turns it "
        "back on for the shipped stack (ISTOTA_MEMORY_SEARCH_ENABLED:-true).",
    ),
    "security.network.enabled": (
        False,
        "the CONNECT proxy needs --unshare-net, and the shipped compose file "
        "grants neither seccomp:unconfined nor systempaths=unconfined, so no "
        "task on this shape is sandboxed to begin with. Conditional on that: "
        "docs/deployment/docker.md tells an operator how to add the pair, and "
        "the render already sets sandbox_enabled=true, so a deployment that "
        "follows it gets a sandbox with the proxy off — weaker than the "
        "dataclass. Revisit this row if the pair is ever shipped.",
    ),
    "developer.gh_bin_path": (
        "/usr/local/lib/istota_forge/gh",
        "the image installs the forge wrapper there on purpose, so the model "
        "reaches gh through the deny-policy shim rather than the real binary. "
        "ISSUE-430 names this row and the one below as deliberate and excludes "
        "them from what it settled.",
    ),
    "developer.glab_bin_path": (
        "/usr/local/lib/istota_forge/glab",
        "the forge wrapper, as above.",
    ),
}


def _diverging_scalars(rendered: dict) -> dict[str, object]:
    """Every rendered scalar whose value differs from the dataclass default.

    Walks the ``Config`` tree rather than the document, so a rendered key no
    dataclass field claims is invisible here — that is ``config_mapper``'s
    unknown-key warning's job, not this one's.

    Scalars only. List- and dict-valued fields are skipped, so
    ``scheduler.push_notification_sources``, ``experimental.features`` and the
    per-user ``users`` table are not compared; all three render empty today, so
    the blindness costs nothing yet and would not announce itself if it stopped
    being free.
    """
    found: dict[str, object] = {}

    def walk(obj, prefix: str, node: dict) -> None:
        for field in dataclasses.fields(obj):
            if field.name not in node:
                continue
            value = node[field.name]
            current = getattr(obj, field.name)
            key = f"{prefix}{field.name}"
            if dataclasses.is_dataclass(current) and isinstance(value, dict):
                walk(current, f"{key}.", value)
                continue
            if isinstance(current, (list, dict)) or not isinstance(
                value, (str, int, float, bool)
            ):
                continue
            # A Path-typed field arrives as a string; compare it as a path so a
            # value that genuinely matches the default does not read as drift.
            if isinstance(current, Path):
                if Path(str(value)) != current:
                    found[key] = value
                continue
            if value != current:
                found[key] = value

    walk(Config(), "", rendered)
    return found


class TestTheRenderDoesNotRestateADefaultWrongly:
    """`render-config.sh` states a default for keys `config.py` already owns.

    `config_mapper.py` removed the *loader's* copy of the schema, and its
    docstring names the failure class it removed: "two defaults for one field",
    where the dataclass says one thing and a second statement of the same key
    says another. The deployment generators are that second statement now, and
    two keys had drifted exactly that way by the time ISSUE-430 was filed. Each
    was correct when it was written and was left behind when `config.py` and
    the Ansible role moved together:

    - `scheduler.worker_idle_timeout` was 30 in all three artifacts until
      `e354c1e7` found the setting was *dead* config — the old single-wait
      branch lingered about one poll interval whatever the number said — made
      it a real cumulative-idle linger, and lowered the default to 10 because
      30 genuinely honoured is too long to hold a per-user slot. Docker kept 30.
    - `playbooks.retention_days` was 0 in both the Ansible defaults and the
      Docker render; `config.py` and Ansible later moved to 90 and Docker was
      left never age-pruning.

    Neither was a decision about containers. What this asserts is that every
    remaining divergence is one somebody wrote a reason for.

    Scope: this reads the script's *own* `:-` defaults, which is the right
    question for "does the render restate a default wrongly". It is not what a
    deployed container gets — `docker-compose.yml` passes most of these in as
    `${VAR:-default}`, so the variable is always set and the render's default is
    never consulted there. That is why the ISSUE-430 fix had to change
    `docker-compose.yml` and `docker/.env.example` alongside this script, and
    why `TestTheDockerDefaultsAgreeWithTheRender` below holds the three in step
    for the keys this class has an opinion about.
    """

    # Most of this script's ~110 `key = ${VAR:-default}` lines sit inside a
    # conditional block, so what the guard can see is decided here rather than
    # by the walker. Every switch below is on for that reason and for no other:
    # rendered with `REQUIRED` alone the walk reaches 128 scalars and skips
    # *109* dataclass fields whose blocks were never emitted, which is a guard
    # that looks whole and covers a little over half the file. `brain.native`
    # is the one that cost something — `prompt_caching` was rendered as a flat
    # `false` against a `bool | None` tri-state whose `None` means "derive from
    # base_url", so the default Anthropic deployment ran with caching off, and
    # the block was invisible here because `ISTOTA_BRAIN_KIND` was not `native`.
    WIDENING_ENV = {
        "OAUTH_CLIENT_ID": "client-id-value",
        "OAUTH_CLIENT_SECRET": "client-secret-value",
        "ISTOTA_PLAYBOOKS_ENABLED": "true",
        "ISTOTA_BRAIN_KIND": "native",
        "ISTOTA_DEVELOPER_ENABLED": "true",
        "ISTOTA_EMAIL_ENABLED": "true",
        "ISTOTA_FEEDS_ENABLED": "true",
        "ISTOTA_MONEY_ENABLED": "true",
        "ISTOTA_LOCATION_ENABLED": "true",
    }

    # Deliberately *not* switched on: `memory_search` and `browser`. Their own
    # `enabled` is one of the divergences recorded below — the render says
    # false and compose says true — and setting the variable here would replace
    # that record with "this test set it", which is the observation and not a
    # fact about the deployment. Nothing else in either block is a default
    # worth the trade.

    @pytest.fixture(scope="class")
    def rendered(self, tmp_path_factory):
        path = render(
            tmp_path_factory.mktemp("drift"), **REQUIRED, **self.WIDENING_ENV
        )
        return tomllib.loads(path.read_text())

    def test_the_widening_env_actually_reaches_more_of_the_script(
        self, tmp_path_factory
    ):
        """The switches above are load-bearing, so prove they switch something.

        Without this the map is only ever checked against whatever blocks
        happen to render, and a future edit that moves a key behind a new gate
        would shrink the guard silently while every assertion stayed green.
        """
        narrow = tomllib.loads(
            render(tmp_path_factory.mktemp("narrow"), **REQUIRED).read_text()
        )
        wide = tomllib.loads(
            render(
                tmp_path_factory.mktemp("wide"), **REQUIRED, **self.WIDENING_ENV
            ).read_text()
        )

        assert set(narrow) < set(wide), (
            "the widening environment renders no section the minimal one does "
            "not; it is not buying the coverage its comment claims"
        )
        assert "native" in wide.get("brain", {}), (
            "[brain.native] is the block that hid ISSUE-430's sixth divergence; "
            "it must render here"
        )

    def test_every_divergence_from_the_dataclass_is_accounted_for(self, rendered):
        found = _diverging_scalars(rendered)
        env = {**REQUIRED, **self.WIDENING_ENV}
        accounted = set(_DEPLOYMENT_FACTS) | set(_INTENTIONAL_DEFAULTS)

        unexplained = {
            k: v
            for k, v in found.items()
            if k not in accounted and not _is_set_by_this_test(k, env)
        }
        assert not unexplained, (
            "render-config.sh states a default that config.py already owns, and "
            "nothing here says why:\n"
            + "\n".join(
                f"  {k}: rendered {v!r}, dataclass default differs"
                for k, v in sorted(unexplained.items())
            )
            + "\n\nEither make the render agree with the dataclass, or add the "
            "key to _DEPLOYMENT_FACTS / _INTENTIONAL_DEFAULTS with the reason. "
            "A key this test switched on itself needs neither — "
            "_is_set_by_this_test derives that."
        )

    def test_the_test_artifact_rule_covers_the_blocks_this_class_switches_on(self):
        """The five entries the rule replaced, pinned so removing them from the
        map is checkable rather than asserted."""
        env = {**REQUIRED, **self.WIDENING_ENV}
        was_listed = [
            "playbooks.enabled",
            "brain.kind",
            "developer.enabled",
            "email.enabled",
            "location.enabled",
        ]
        uncovered = [k for k in was_listed if not _is_set_by_this_test(k, env)]
        assert not uncovered, f"the rule no longer covers {uncovered}"

    @pytest.mark.parametrize(
        "key",
        [
            "scheduler.max_foreground_workers",
            "web.token_storage",
            "db_path",
            "nextcloud.url",
        ],
    )
    def test_the_test_artifact_rule_exempts_nothing_it_did_not_set(self, key):
        """The control. The rule is only sound because it fires on the
        variables this fixture passed in and on nothing else — a broader
        reading would exempt exactly the divergences this class exists to
        report. `nextcloud.url` is the case that shows the naming convention is
        not total: it renders from `NC_URL`, so the derived name is absent and
        the key stays a listed fact."""
        env = {**REQUIRED, **self.WIDENING_ENV}
        assert not _is_set_by_this_test(key, env)

    def test_no_entry_here_has_gone_stale(self, rendered):
        """An exemption that no longer diverges has to go.

        Without this the map quietly becomes a list of things that used to be
        true, which is the same failure as the drift itself: a second statement
        of the schema that nobody is checking.
        """
        found = _diverging_scalars(rendered)
        accounted = set(_DEPLOYMENT_FACTS) | set(_INTENTIONAL_DEFAULTS)

        stale = sorted(accounted - set(found))
        assert not stale, (
            "these keys are recorded as deliberate divergences but now match "
            f"the dataclass default; remove them: {stale}"
        )

    def test_the_intentional_defaults_render_the_value_they_claim(self, rendered):
        found = _diverging_scalars(rendered)

        wrong = {
            key: (expected, found.get(key))
            for key, (expected, _reason) in _INTENTIONAL_DEFAULTS.items()
            if found.get(key) != expected
        }
        assert not wrong, (
            "a deliberate divergence no longer renders the value recorded here:\n"
            + "\n".join(
                f"  {k}: recorded {exp!r}, rendered {got!r}"
                for k, (exp, got) in sorted(wrong.items())
            )
        )

    def test_every_recorded_divergence_carries_a_reason(self):
        """The map is only worth having if each entry says why."""
        reasons = {k: v for k, v in _DEPLOYMENT_FACTS.items()}
        reasons.update({k: r for k, (_v, r) in _INTENTIONAL_DEFAULTS.items()})

        thin = sorted(k for k, r in reasons.items() if len(r.strip()) < 20)
        assert not thin, f"these entries need a real reason, not a label: {thin}"

    def test_the_keys_brought_back_to_the_dataclass_stay_there(self, rendered):
        """The regression pin, stated as values rather than as absence.

        `test_every_divergence_from_the_dataclass_is_accounted_for` would also
        catch a revert, but it would report it as "unexplained divergence",
        which reads as a new key needing a map entry rather than as these
        going backwards.

        `fallback_on_transient` is here for a second reason. Its
        `_INTENTIONAL_DEFAULTS` row is gone, so the only thing holding the flip
        is "the render equals the dataclass" — which a later edit to
        `config.py`'s own default would satisfy while silently taking the
        Docker behaviour change back out. The two ISSUE-430 keys have the same
        exposure and that is why they were written down this way.
        """
        assert rendered["scheduler"]["worker_idle_timeout"] == 10
        assert rendered["playbooks"]["retention_days"] == 90
        assert rendered["brain"]["fallback_on_transient"] is True


class TestPromptCachingKeepsItsThirdState:
    """`brain.native.prompt_caching` is `bool | None` and `None` is a value.

    `llm/__init__.py` reads `None` as "on for `api.anthropic.com`, off
    elsewhere", so the *absence* of the key is what gives the default Anthropic
    deployment its caching. The render stated a flat `false` — which is not the
    dataclass default written out, it is the operator forcing caching off — so
    every Docker `native` deployment against Anthropic paid full uncached input
    on every turn. The Ansible template already had this right and emits the
    line only for a real boolean; this is that rule on the Docker path.

    Found by the drift guard above once its fixture reached `[brain.native]`,
    which is the whole argument for widening that fixture.
    """

    NATIVE = {"ISTOTA_BRAIN_KIND": "native"}

    def _native(self, tmp_path, **env):
        rendered = tomllib.loads(
            render(tmp_path, **REQUIRED, **self.NATIVE, **env).read_text()
        )
        return rendered["brain"]["native"]

    def test_an_unset_variable_omits_the_key_entirely(self, tmp_path):
        assert "prompt_caching" not in self._native(tmp_path)

    def test_an_empty_variable_omits_the_key_entirely(self, tmp_path):
        """Empty is how compose passes "the operator said nothing"."""
        assert "prompt_caching" not in self._native(
            tmp_path, ISTOTA_BRAIN_NATIVE_PROMPT_CACHING=""
        )

    @pytest.mark.parametrize(
        "raw,expected",
        [("true", True), ("false", False), ("True", True), ("FALSE", False)],
    )
    def test_an_explicit_boolean_is_rendered(self, tmp_path, raw, expected):
        native = self._native(tmp_path, ISTOTA_BRAIN_NATIVE_PROMPT_CACHING=raw)

        assert native["prompt_caching"] is expected

    def test_the_omitted_key_leaves_the_dataclass_tri_state_intact(self, tmp_path):
        """The property that actually matters, read back through the loader."""
        config = load_config(render(tmp_path, **REQUIRED, **self.NATIVE))

        assert config.brain.native.prompt_caching is None

    def test_the_other_two_docker_artifacts_pass_the_third_state_through(self):
        """Compose is the one that actually reaches a container.

        `environment:` sets the variable whether or not the operator did, so a
        `:-false` there would force caching off no matter what this script does
        — the render's own default is never consulted on a real deployment.
        Both must therefore pass the empty value through.
        """
        compose = (REPO / "docker" / "docker-compose.yml").read_text()
        env_example = (REPO / "docker" / ".env.example").read_text()
        var = "ISTOTA_BRAIN_NATIVE_PROMPT_CACHING"

        compose_defaults = set(
            re.findall(r"^\s*" + var + r":\s*\$\{" + var + r":-([^}]*)\}", compose, re.M)
        )
        assert compose_defaults == {""}, (
            f"docker-compose.yml forces prompt_caching: {compose_defaults}"
        )

        env_values = set(re.findall(r"^" + var + r"=(.*?)\s*$", env_example, re.M))
        assert env_values == {""}, f".env.example forces prompt_caching: {env_values}"

    def test_a_junk_value_is_dropped_rather_than_written(self, tmp_path):
        """A bare word here is invalid TOML, so the container would not boot.

        `render()` asserts the config loads and that stderr is empty, and a
        warning is deliberately not silence — so this renders by hand.
        """
        config_file = tmp_path / "config.toml"
        proc = subprocess.run(
            ["bash", str(RENDER_CONFIG)],
            env={
                "PATH": os.environ.get("PATH", ""),
                "CONFIG_FILE": str(config_file),
                **REQUIRED,
                **self.NATIVE,
                "ISTOTA_BRAIN_NATIVE_PROMPT_CACHING": "yes-please",
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode == 0
        rendered = tomllib.loads(config_file.read_text())
        assert "prompt_caching" not in rendered["brain"]["native"]
        assert "ISTOTA_BRAIN_NATIVE_PROMPT_CACHING" in proc.stderr


class TestTheDockerDefaultsAgreeWithTheRender:
    """Three files on the Docker path state a default, and the render loses.

    `docker-compose.yml` passes each knob to the container as
    `ISTOTA_FOO: ${ISTOTA_FOO:-default}`, which **sets** the variable whether or
    not the operator did. So `render-config.sh`'s own `${ISTOTA_FOO:-default}`
    is dead on a real deployment: compose's value is already there. Correcting
    only the render — which is what ISSUE-430 as filed described — would have
    changed nothing on the shape the issue was about, and would have left the
    two artifacts disagreeing in the other direction.

    `docker/.env.example` is the third statement. It is documentation rather
    than a code path, which makes it the one most likely to be missed and the
    one an operator is most likely to copy.

    Deliberately narrow: only the keys ISSUE-430 settled. A general
    render/compose agreement guard runs into a shipped pattern that is not
    drift — every `*_ENABLED` knob defaults false in the render, so a
    standalone render is minimal, and true in compose, so the shipped stack has
    its features on — and separating those from real disagreements is its own
    piece of work.
    """

    KEYS = {
        "ISTOTA_SCHEDULER_WORKER_IDLE_TIMEOUT": "10",
        "ISTOTA_PLAYBOOKS_RETENTION_DAYS": "90",
        "ISTOTA_SCHEDULER_MAX_FOREGROUND_WORKERS": "3",
        "ISTOTA_SCHEDULER_MAX_BACKGROUND_WORKERS": "2",
        "ISTOTA_WEB_TOKEN_STORAGE": "encrypted",
    }

    @staticmethod
    def _render_defaults(var: str) -> set[str]:
        text = RENDER_CONFIG.read_text()
        return set(re.findall(r"\$\{" + var + r":-([^}]*)\}", text))

    @staticmethod
    def _compose_defaults(var: str) -> set[str]:
        text = (REPO / "docker" / "docker-compose.yml").read_text()
        return set(
            re.findall(
                r"^\s*" + var + r":\s*\$\{" + var + r":-([^}]*)\}", text, re.M
            )
        )

    @staticmethod
    def _env_example_values(var: str) -> set[str]:
        text = (REPO / "docker" / ".env.example").read_text()
        # `$` under re.M matches before `\n` but not before `\r\n`, so a CRLF
        # checkout would otherwise yield "10\r" and fail on the carriage return.
        return set(re.findall(r"^" + var + r"=(.*?)\s*$", text, re.M))

    @pytest.mark.parametrize("var,expected", sorted(KEYS.items()))
    def test_all_three_docker_artifacts_state_the_same_default(self, var, expected):
        render_vals = self._render_defaults(var)
        compose_vals = self._compose_defaults(var)
        env_vals = self._env_example_values(var)

        assert render_vals, f"{var} has no ${{...:-default}} in render-config.sh"
        assert compose_vals, f"{var} is not passed through docker-compose.yml"
        assert env_vals, f"{var} is not documented in docker/.env.example"

        assert render_vals == {expected}, f"render-config.sh: {var} = {render_vals}"
        assert compose_vals == {expected}, f"docker-compose.yml: {var} = {compose_vals}"
        assert env_vals == {expected}, f".env.example: {var} = {env_vals}"

    def test_the_scan_finds_a_disagreement_it_is_given(self):
        """The three readers above are regexes over shipped files.

        A regex that silently stops matching reports agreement, so each one is
        given text it must find a value in — otherwise this whole class passes
        on every deployment by matching nothing anywhere.
        """
        assert re.findall(
            r"\$\{ISTOTA_X:-([^}]*)\}", 'foo = ${ISTOTA_X:-7}'
        ) == ["7"]
        assert re.findall(
            r"^\s*ISTOTA_X:\s*\$\{ISTOTA_X:-([^}]*)\}",
            "      ISTOTA_X: ${ISTOTA_X:-7}",
            re.M,
        ) == ["7"]
        assert re.findall(r"^ISTOTA_X=(.*)$", "ISTOTA_X=7", re.M) == ["7"]
