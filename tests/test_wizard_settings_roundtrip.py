"""What `deploy/wizard.sh` asks about has to survive all the way to `config.toml`.

Four files have to agree for a wizard answer to reach the daemon, and each
disagreement is silent in the same way — the operator answers a question, the
install succeeds, and the setting is not there:

  * `wizard.sh` writes a key into `settings.toml`;
  * `settings_to_vars.convert` maps it to an `istota_*` extra-var;
  * `defaults/main.yml` defines that variable (an extra-var naming nothing is
    accepted by Ansible and read by no template); and
  * `config.toml.j2` renders it.

The existing coverage checks the last two against each other. Nothing checked
the first two, which is how `wizard.sh` came to ask no question at all about
`[developer]`, `[talk.signaling]`, `[brain] room_selectable`, `[brain] fallback`
or `[web.map]` while every one of them had a variable and a template line
waiting for it.

So these tests run the real chain rather than asserting name-by-name: a
settings dict through the real `convert()`, into the real template, and then
look for the value in the parsed TOML. A name that is right in three files and
wrong in the fourth fails here.
"""

import importlib.util
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.test_ansible_config_template import render

REPO = Path(__file__).resolve().parent.parent
WIZARD = REPO / "deploy" / "wizard.sh"
DEFAULTS_FILE = REPO / "deploy" / "ansible" / "defaults" / "main.yml"


def _settings_to_vars():
    """`deploy/` is not a package and not on the path; load the module by path."""
    path = REPO / "deploy" / "settings_to_vars.py"
    spec = importlib.util.spec_from_file_location("istota_settings_to_vars", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def stv():
    return _settings_to_vars()


def _render_from_settings(stv, settings: dict) -> dict:
    """The whole chain, as the installer runs it: settings -> vars -> config."""
    return tomllib.loads(render(**stv.convert(settings)))


# ---------------------------------------------------------------------------
# The chain, one case per section the stage added.
# ---------------------------------------------------------------------------


class TestASettingsAnswerReachesTheRenderedConfig:
    def test_developer_credentials_and_repos_dir(self, stv):
        """The chicken-and-egg case. `tasks/main.yml` asserts a forge token when
        the skill is on, so a wizard that could not set either left the operator
        editing vars by hand after a run that never asked."""
        config = _render_from_settings(stv, {
            "developer": {
                "enabled": True,
                "repos_dir": "/srv/app/istota/repos",
                "gitlab_url": "https://forge.example.com",
                "gitlab_username": "bot-account",
                "gitlab_token": "glpat-placeholder",
                "github_username": "bot-account",
                "github_token": "ghp-placeholder",
            },
            # The template renders the tokens into config.toml only when the
            # role is not using an environment file. Ask for the shape that
            # puts them in the file, so the assertion can see them.
            "use_environment_file": False,
        })
        assert config["developer"]["enabled"] is True
        assert config["developer"]["repos_dir"] == "/srv/app/istota/repos"
        assert config["developer"]["gitlab_url"] == "https://forge.example.com"
        assert config["developer"]["gitlab_token"] == "glpat-placeholder"
        assert config["developer"]["github_token"] == "ghp-placeholder"

    def test_the_developer_block_is_absent_when_the_wizard_leaves_it_off(self, stv):
        """The other half of the same answer, and the one the assert depends on:
        `enabled = false` has to reach the role, or a settings file written with
        no token still trips the play."""
        # The render assertion alone witnesses the Ansible default rather than
        # this answer — `istota_developer_enabled` is already false in
        # defaults/main.yml, so an empty settings dict renders identically.
        # Assert the converter carried the answer, then the render.
        assert stv.convert({"developer": {"enabled": False}})[
            "istota_developer_enabled"
        ] is False
        config = _render_from_settings(stv, {"developer": {"enabled": False}})
        assert "developer" not in config

    def test_talk_signaling(self, stv):
        config = _render_from_settings(stv, {
            "talk": {"signaling": {"enabled": True, "url": "https://signal.example.com"}},
        })
        assert config["talk"]["signaling"]["enabled"] is True
        assert config["talk"]["signaling"]["url"] == "https://signal.example.com"

    def test_talk_signaling_travels_without_its_parent_section(self, stv):
        """The wizard writes `[talk.signaling]` and no `[talk]` header, because
        writing `[talk]`'s own keys empty would override the role's defaults for
        them. A converter reaching the child only through a populated parent
        would drop the whole answer."""
        settings = {"talk": {"signaling": {"enabled": True}}}
        result = stv.convert(settings)
        assert result["istota_talk_signaling_enabled"] is True
        assert "istota_talk_enabled" not in result
        assert "istota_talk_bot_username" not in result

    def test_brain_room_selectable(self, stv):
        config = _render_from_settings(stv, {
            "brain": {"kind": "claude_code", "room_selectable": ["native", "tmux_claude"]},
        })
        assert config["brain"]["room_selectable"] == ["native", "tmux_claude"]

    def test_an_empty_allowlist_renders_no_key_at_all(self, stv):
        """Empty is the default and means no room or job may pin anything. The
        template guards the key on truthiness, so the absence is the setting."""
        config = _render_from_settings(stv, {"brain": {"room_selectable": []}})
        assert "room_selectable" not in config["brain"]

    def test_brain_fallback(self, stv):
        config = _render_from_settings(stv, {
            "brain": {"kind": "native", "fallback": "claude_code"},
        })
        assert config["brain"]["fallback"] == "claude_code"

    def test_web_map_provider_and_key(self, stv):
        config = _render_from_settings(stv, {
            "web": {"map": {"provider": "carto", "api_key": "placeholder-key"}},
        })
        assert config["web"]["map"]["provider"] == "carto"
        assert config["web"]["map"]["api_key"] == "placeholder-key"

    def test_web_map_custom_styles(self, stv):
        config = _render_from_settings(stv, {
            "web": {"map": {
                "provider": "custom",
                "dark_style": "https://tiles.example.com/dark.json",
                "light_style": "https://tiles.example.com/light.json",
                "attribution": "&copy; Example",
            }},
        })
        assert config["web"]["map"]["provider"] == "custom"
        assert config["web"]["map"]["dark_style"] == "https://tiles.example.com/dark.json"
        assert config["web"]["map"]["light_style"] == "https://tiles.example.com/light.json"
        assert config["web"]["map"]["attribution"] == "&copy; Example"


class TestTheChainWouldNoticeABrokenLink:
    """A rendering test passes for two reasons — the chain works, or the value
    was going to be there anyway. These separate them."""

    @pytest.mark.parametrize("provider", ["osm", "carto", "custom"])
    def test_the_provider_is_not_simply_the_default(self, stv, provider):
        """`openfreemap` is deliberately not in this list: it is the value in
        defaults/main.yml, so that case passes with the mapping deleted."""
        assert _render_from_settings(
            stv, {"web": {"map": {"provider": provider}}}
        )["web"]["map"]["provider"] == provider

    def test_a_key_only_reaches_the_config_when_the_settings_name_it(self, stv):
        """The control for every assertion above, as a pair rather than as a
        single negative: the same render with and without the settings key, so
        it is the key that makes the difference and not the template."""
        without = _render_from_settings(stv, {"web": {"map": {"provider": "carto"}}})
        with_key = _render_from_settings(
            stv, {"web": {"map": {"provider": "carto", "api_key": "placeholder-key"}}}
        )
        assert "api_key" not in without["web"]["map"]
        assert with_key["web"]["map"]["api_key"] == "placeholder-key"


# ---------------------------------------------------------------------------
# The derivation `fallback` has and the other four do not.
# ---------------------------------------------------------------------------


class TestTheFallbackDerivationSurvivesAnUnansweredPrompt:
    """`istota_brain_fallback`'s default is an expression, not a literal: it
    works out `claude_code` for a tmux_claude deployment and "" for the rest.
    Any extra-var replaces it, so "the operator did not answer" and "the
    operator answered none" must not produce the same settings file."""

    def test_the_default_is_still_an_expression(self):
        """If this stops being derived, the omit-when-absent rule below is
        pointless ceremony and should go with it."""
        line = next(
            text for text in DEFAULTS_FILE.read_text().splitlines()
            if text.startswith("istota_brain_fallback:")
        )
        assert "{{" in line, (
            "istota_brain_fallback is no longer derived; revisit whether the "
            "wizard still needs its 'derive' answer."
        )

    def test_settings_without_the_key_emit_no_variable(self, stv):
        assert "istota_brain_fallback" not in stv.convert({"brain": {"kind": "native"}})

    def test_a_tmux_deployment_keeps_its_derived_failover(self, stv):
        """The case the omission protects, end to end."""
        config = _render_from_settings(stv, {"brain": {"kind": "tmux_claude"}})
        assert config["brain"]["fallback"] == "claude_code"

    def test_an_explicit_empty_answer_does_override_it(self, stv):
        """And the operator can still say no — it just has to be said."""
        config = _render_from_settings(
            stv, {"brain": {"kind": "tmux_claude", "fallback": ""}}
        )
        assert "fallback" not in config["brain"]

    def test_the_wizard_writes_the_key_only_on_an_explicit_answer(self):
        """The shell half of the same rule. `derive` is the wizard's default
        answer and must reach the settings file as no key at all."""
        text = WIZARD.read_text()
        assert '_WIZ_BRAIN_FALLBACK="derive"' in text, "the 'derive' sentinel is gone"
        assert '[ "$_WIZ_BRAIN_FALLBACK" != "derive" ]' in text, (
            "wizard.sh no longer guards the fallback key on an explicit answer, "
            "so an unanswered prompt now overrides the role's derivation."
        )


# ---------------------------------------------------------------------------
# The wizard's own output, run rather than read.
# ---------------------------------------------------------------------------


# Extract `wiz_write_settings` and run it against a fixed set of answers. The
# name-level tests below cannot see the one failure that matters most here: a
# key emitted with malformed TOML on the right-hand side. `room_selectable` is
# the live example — the wizard assembles a TOML array in shell, so an
# unquoted element would be a settings file no installer can read, and every
# static check would still pass.
_HARNESS = r"""
set -euo pipefail
_BOLD=""; _BLUE=""; _GREEN=""; _YELLOW=""; _RED=""; _DIM=""; _RESET=""
eval "$(grep -E '^(info|ok|warn|error|die|section|dim)\(\)' "$WIZ")"
SETTINGS_FILE="$OUT"
ISTOTA_HOME="/srv/app/istota"
ISTOTA_NAMESPACE="istota"
REPO_URL="https://example.invalid/istota.git"
REPO_BRANCH="main"
eval "$(grep -E '^_WIZ_[A-Z0-9_]+=' "$WIZ")"
_WIZ_USER_IDS=()
eval "$(awk '/^prompt_value\(\) \{/,/^\}/' "$WIZ")"
eval "$(awk '/^prompt_bool\(\) \{/,/^\}/' "$WIZ")"
eval "$(awk '/^prompt_secret\(\) \{/,/^\}/' "$WIZ")"
eval "$(awk '/^toml_escape\(\) \{/,/^\}/' "$WIZ")"
eval "$(awk '/^toml_string_list\(\) \{/,/^\}/' "$WIZ")"
eval "$(awk '/^wiz_brain_policy\(\) \{/,/^\}/' "$WIZ")"
eval "$(awk '/^wiz_developer\(\) \{/,/^\}/' "$WIZ")"
eval "$(awk '/^wiz_write_settings\(\) \{/,/^\}/' "$WIZ")"
eval "${EXTRA:-}"
if [ -n "${DEV_ANSWERS:-}" ]; then
    wiz_developer >/dev/null <<< "$DEV_ANSWERS"
fi
if [ -n "${ANSWERS:-}" ]; then
    wiz_brain_policy >/dev/null <<< "$ANSWERS"
fi
wiz_write_settings >/dev/null
"""

# Every answer the stage added, set to something a real operator might give.
# No real host, namespace, path or account: this file is committed.
_ALL_ANSWERED = """
_WIZ_DEVELOPER_ENABLED=true
_WIZ_DEVELOPER_REPOS_DIR="/srv/app/istota/repos"
_WIZ_DEVELOPER_GITLAB_URL="https://forge.example.com"
_WIZ_DEVELOPER_GITLAB_USERNAME="bot-account"
_WIZ_DEVELOPER_GITLAB_TOKEN="glpat-placeholder"
_WIZ_DEVELOPER_GITHUB_USERNAME="bot-account"
_WIZ_DEVELOPER_GITHUB_TOKEN="ghp-placeholder"
_WIZ_TALK_SIGNALING_ENABLED=true
_WIZ_TALK_SIGNALING_URL="https://signal.example.com"
_WIZ_WEB_MAP_PROVIDER="carto"
_WIZ_WEB_MAP_API_KEY="placeholder-key"
"""

# The two `[brain]` answers are typed rather than assigned, because the wizard
# builds the allowlist into a TOML array element by element in shell and that
# assembly is the part worth testing. Setting `_WIZ_BRAIN_ROOM_SELECTABLE` to a
# pre-quoted string instead skips it: verified by mutation, an unquoted element
# left every test in this file green.
_BRAIN_ANSWERS = "native, claude_code\nclaude_code\n"


def _run_wizard_write(
    tmp_path: Path, extra: str = "", answers: str = "", dev_answers: str = ""
) -> dict:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    out = tmp_path / "settings.toml"
    proc = subprocess.run(
        ["bash", "-c", _HARNESS],
        env={
            "WIZ": str(WIZARD),
            "OUT": str(out),
            "EXTRA": extra,
            "ANSWERS": answers,
            "DEV_ANSWERS": dev_answers,
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"wiz_write_settings failed: {proc.stderr}"
    return tomllib.loads(out.read_text())


# The developer section, answered prompt by prompt. Eight reads: the enable,
# the repos dir, the GitLab URL / username / token, the GitHub username /
# token, and — only when both tokens came back empty — the retry question.
_DEV_ANSWERS_WITH_TOKEN = "\n".join([
    "y", "/srv/app/istota/repos", "https://forge.example.com",
    "bot-account", "glpat-placeholder", "bot-account", "ghp-placeholder",
]) + "\n"
_DEV_ANSWERS_NO_TOKEN = "\n".join(["y", "", "", "", "", "", "", "n"]) + "\n"


class TestTheDeveloperSkillCannotBeLeftUndeployable:
    """`tasks/main.yml:23` asserts a forge token when `istota_developer_enabled`
    is true and fails the play otherwise, so "yes" with no token has to resolve
    inside the wizard. This is the branch the whole section exists for, and
    until `wiz_developer` was split out of `wiz_features` nothing could run it.
    """

    def test_answering_yes_with_a_token_enables_the_skill(self, tmp_path):
        settings = _run_wizard_write(tmp_path, dev_answers=_DEV_ANSWERS_WITH_TOKEN)
        assert settings["developer"]["enabled"] is True
        assert settings["developer"]["gitlab_token"] == "glpat-placeholder"
        assert settings["developer"]["github_token"] == "ghp-placeholder"

    def test_answering_yes_with_no_token_leaves_the_skill_off(self, tmp_path):
        """The give-up path. Anything else here writes a settings file the
        play refuses, after the operator has answered every other question."""
        settings = _run_wizard_write(tmp_path, dev_answers=_DEV_ANSWERS_NO_TOKEN)
        assert settings["developer"]["enabled"] is False

    def test_on_the_shape_the_wizard_writes_the_token_travels_by_env_file(
        self, tmp_path, stv
    ):
        """The other developer assertions in this file force
        `use_environment_file = False`, and that is a shape `wizard.sh` never
        writes — it hardcodes `true`. Under `true`, `config.toml.j2` emits no
        `gitlab_token` at all and the credential reaches the daemon through
        `templates/secrets.env.j2`, which reads the same variable. Without this
        case the deployment the operator actually gets is asserted by nothing.
        """
        settings = _run_wizard_write(tmp_path, dev_answers=_DEV_ANSWERS_WITH_TOKEN)
        assert settings["use_environment_file"] is True

        variables = stv.convert(settings)
        # What secrets.env.j2 interpolates.
        assert variables["istota_developer_gitlab_token"] == "glpat-placeholder"

        config = tomllib.loads(render(**variables))
        assert config["developer"]["enabled"] is True
        assert config["developer"]["repos_dir"] == "/srv/app/istota/repos"
        # Deliberately absent here, and present in the sibling test that flips
        # the flag — which is what says the flag is what moves it.
        assert "gitlab_token" not in config["developer"]
        assert "github_token" not in config["developer"]

    def test_the_role_assert_would_pass_on_what_the_wizard_writes(self, tmp_path, stv):
        """Stated as the role states it, so the two cannot drift apart: the
        assert is `enabled implies at least one token`."""
        for answers in (_DEV_ANSWERS_WITH_TOKEN, _DEV_ANSWERS_NO_TOKEN):
            variables = stv.convert(_run_wizard_write(tmp_path, dev_answers=answers))
            if variables.get("istota_developer_enabled"):
                assert (
                    variables.get("istota_developer_gitlab_token")
                    or variables.get("istota_developer_github_token")
                ), "wizard produced vars that fail tasks/main.yml:23"


class TestQuotingSurvivesTheWizard:
    """A generated TOML file is a quoting problem wearing a config's clothes.

    Every operator-typed value lands between `"` in a heredoc, so the two
    characters that matter are `"` and `\\`. Both were interpolated raw until
    review caught it, and the same defect was fixed for the Docker shape in
    `render-config.sh` — `tests/test_render_config.py::TestQuotingSurvivesTheRender`
    is the sibling of this class, and its docstring records that testing a
    *single* quote proves nothing because the first draft did exactly that.

    Two failure modes, and the second is the reason a parse check alone is not
    enough: `"` mid-value is a loud TOML error, while `value" # rest` parses
    cleanly as a silently truncated value. So every case here asserts the value
    round-trips byte for byte, not merely that the file loads.
    """

    # Attribution is prompted as HTML, so a `"` is the ordinary case rather
    # than an adversarial one: every MapLibre attribution string is an <a> tag.
    _HOSTILE = [
        '<a href="https://example.com">Example</a>',
        'trailing-backslash\\',
        'inner"quote',
        'https://tiles.example.com" # rest',
        'both"and\\',
    ]

    @pytest.mark.parametrize("value", _HOSTILE)
    def test_a_custom_attribution_round_trips(self, tmp_path, value):
        settings = _run_wizard_write(tmp_path, (
            '_WIZ_WEB_MAP_PROVIDER="custom"\n'
            f"_WIZ_WEB_MAP_ATTRIBUTION={shlex.quote(value)}\n"
        ))
        assert settings["web"]["map"]["attribution"] == value

    @pytest.mark.parametrize("value", _HOSTILE)
    def test_a_signaling_url_round_trips(self, tmp_path, value):
        """The silent case has its home here: this value is a URL, and a
        truncated one leaves the daemon pointed somewhere else with no error."""
        settings = _run_wizard_write(tmp_path, (
            "_WIZ_TALK_SIGNALING_ENABLED=true\n"
            f"_WIZ_TALK_SIGNALING_URL={shlex.quote(value)}\n"
        ))
        assert settings["talk"]["signaling"]["url"] == value

    @pytest.mark.parametrize("value", _HOSTILE)
    def test_a_forge_token_round_trips(self, tmp_path, value):
        settings = _run_wizard_write(tmp_path, (
            "_WIZ_DEVELOPER_ENABLED=true\n"
            f"_WIZ_DEVELOPER_GITLAB_TOKEN={shlex.quote(value)}\n"
        ))
        assert settings["developer"]["gitlab_token"] == value

    @pytest.mark.parametrize("value", _HOSTILE)
    def test_a_pre_existing_value_round_trips_too(self, tmp_path, value):
        """The Nextcloud app password predates this change and is interpolated
        the same way. It is covered because a helper only some of its callers
        use is the shape `7e7baf47` called a fix that has not landed."""
        settings = _run_wizard_write(
            tmp_path, f"_WIZ_NC_APP_PASSWORD={shlex.quote(value)}\n"
        )
        assert settings["nextcloud_app_password"] == value

    def test_a_user_id_cannot_forge_a_table_header(self, tmp_path):
        """`[users.$uid]` puts an operator string in key position, where a `"`
        or a `.` changes which table the following keys belong to."""
        settings = _run_wizard_write(tmp_path, (
            '_WIZ_USERS_BLOCK="$(printf \'\\n[users.\\"%s\\"]\\n'
            'display_name = \\"x\\"\\n\' "$(toml_escape \'a"b\')")"\n'
        ))
        assert 'a"b' in settings["users"]


class TestTheWizardWritesAFileTheInstallerCanRead:
    def test_the_default_answers_produce_valid_toml(self, tmp_path):
        settings = _run_wizard_write(tmp_path)
        assert settings["developer"]["enabled"] is False
        assert settings["talk"]["signaling"]["enabled"] is False
        assert settings["web"]["map"]["provider"] == "openfreemap"
        assert settings["brain"]["room_selectable"] == []

    def test_the_default_run_writes_no_fallback_key(self, tmp_path):
        """The 'derive' answer, as it reaches disk. A `fallback` of any value
        here would replace the role's derivation for every wizard install."""
        assert "fallback" not in _run_wizard_write(tmp_path)["brain"]

    def test_every_answer_survives_to_the_rendered_config(self, tmp_path, stv):
        """The whole chain in one assertion, starting from the wizard rather
        than from a settings dict written by hand to match it."""
        settings = _run_wizard_write(tmp_path, _ALL_ANSWERED, _BRAIN_ANSWERS)
        settings["use_environment_file"] = False
        config = tomllib.loads(render(**stv.convert(settings)))

        assert config["developer"]["repos_dir"] == "/srv/app/istota/repos"
        assert config["developer"]["gitlab_url"] == "https://forge.example.com"
        assert config["developer"]["gitlab_token"] == "glpat-placeholder"
        assert config["developer"]["github_token"] == "ghp-placeholder"
        assert config["talk"]["signaling"]["enabled"] is True
        assert config["talk"]["signaling"]["url"] == "https://signal.example.com"
        assert config["brain"]["room_selectable"] == ["native", "claude_code"]
        assert config["brain"]["fallback"] == "claude_code"
        assert config["web"]["map"]["provider"] == "carto"
        assert config["web"]["map"]["api_key"] == "placeholder-key"

    def test_the_allowlist_is_written_as_a_toml_array(self, tmp_path):
        """Assembled element by element in shell, which is the one value here
        that can be emitted as something TOML rejects."""
        settings = _run_wizard_write(tmp_path, _ALL_ANSWERED, _BRAIN_ANSWERS)
        assert settings["brain"]["room_selectable"] == ["native", "claude_code"]


# ---------------------------------------------------------------------------
# The wizard and the converter, held together by name.
# ---------------------------------------------------------------------------


def _wizard_keys(section: str) -> set[str]:
    """The `key = ` names wizard.sh writes under a `[section]` header.

    Reads both places the wizard emits TOML — the heredoc and the shell
    variables it builds blocks in — since a section can be written either way
    and a scanner that knew only one would report an empty set, which every
    test below would pass on. Each caller asserts the set is non-empty for that
    reason.

    The header has to be alone on its line: `wizard.sh` names several of these
    sections in prose too, and matching a comment mentioning `[brain]` returns
    the keys of whatever block follows it.

    Two things this deliberately does not do, both because it found nothing
    when it did. It does not stop at the `"` closing the shell string, since
    the keys a section writes conditionally are appended *after* that quote —
    stopping there saw one key of `[developer]`'s seven and missed `[brain]`'s
    `fallback` entirely, while every test still passed. And it strips a
    `varname+="` prefix, since that is how those appended lines start. Only a
    TOML header or the heredoc terminator ends a block. The cost is that a
    stray `key = value` in the shell between two headers is picked up as
    written; that direction fails loudly, asking for a mapping that is not
    needed, rather than quietly checking nothing.
    """
    lines = WIZARD.read_text().splitlines()
    try:
        start = lines.index(f"[{section}]")
    except ValueError as exc:
        raise AssertionError(f"no `[{section}]` header on a line of its own") from exc

    keys = set()
    for line in lines[start + 1:]:
        stripped = line.strip()
        # A TOML header, the heredoc terminator, or the heredoc opener — that
        # last one because the block written just above it is a shell string,
        # and without it the scan runs on into the heredoc's own top-level keys.
        if stripped.startswith("[") or stripped == "TOML" or stripped.startswith("cat >"):
            break
        stripped = re.sub(r'^[a-z_]+\+?="', "", stripped)
        match = re.match(r'^([a-z_][a-z0-9_]*) = ', stripped)
        if match:
            keys.add(match.group(1))
    return keys


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        # The two sections whose keys the scanner cannot see without its
        # shell-append handling. Both passed while finding almost nothing: the
        # `[developer]` scan saw `enabled` alone, so the six credential keys
        # this stage added were checked against the converter by nothing, and
        # the `[brain]` scan missed `fallback`. `written <= mapped` is true of
        # the empty set, so the subset test cannot catch its own blindness —
        # this names what has to be in there.
        ("developer", {"enabled", "repos_dir", "gitlab_token", "github_token"}),
        ("brain", {"kind", "room_selectable", "fallback"}),
        # These two are written contiguously today, so the scan reaches them
        # without the append handling — but `assert written` alone is satisfied
        # by one key, so a refactor that truncated the scan would leave the
        # mapping test passing on a blind spot.
        ("talk.signaling", {"enabled", "url"}),
        ("web.map", {"provider", "api_key", "attribution"}),
    ],
)
def test_the_scanner_reaches_the_conditionally_written_keys(section, expected):
    found = _wizard_keys(section)
    assert expected <= found, (
        f"the [{section}] scan found {sorted(found)}, missing "
        f"{sorted(expected - found)}. Either wizard.sh stopped writing them or "
        "the scanner stopped seeing them; the second reads as a pass."
    )


@pytest.mark.parametrize(
    ("section", "mapping_name"),
    [
        ("developer", "_DEVELOPER_KEYS"),
        ("talk.signaling", "_TALK_SIGNALING_KEYS"),
        ("web.map", "_WEB_MAP_KEYS"),
    ],
)
def test_every_key_the_wizard_writes_is_mapped_by_the_converter(stv, section, mapping_name):
    """A key `wizard.sh` writes and `settings_to_vars.py` does not map is an
    answer the operator gives and the deployment never sees."""
    written = _wizard_keys(section)
    assert written, f"found no keys under [{section}] in wizard.sh; the scanner broke"
    mapped = set(getattr(stv, mapping_name))
    assert written <= mapped, (
        f"wizard.sh writes {sorted(written - mapped)} under [{section}], which "
        f"{mapping_name} does not map — the answer would be silently dropped."
    )


def test_the_brain_keys_the_wizard_writes_are_mapped(stv):
    """`[brain]` is checked apart from the three above because its block also
    carries `kind`, which `convert` handles on its own rather than through a
    map, and the nested `[brain.native]` keys, which have their own."""
    written = _wizard_keys("brain")
    assert written, "found no keys under [brain] in wizard.sh; the scanner broke"
    mapped = set(stv._BRAIN_FLAT_KEYS) | {"kind"}
    assert written <= mapped, (
        f"wizard.sh writes {sorted(written - mapped)} under [brain], which "
        "convert() does not map."
    )


@pytest.mark.parametrize(
    "mapping_name", ["_TALK_SIGNALING_KEYS", "_WEB_MAP_KEYS", "_BRAIN_FLAT_KEYS"]
)
def test_the_new_mappings_target_real_ansible_vars(stv, mapping_name):
    """Same check `test_ansible_developer_config` makes of the developer map: a
    typo in a target is silent, because Ansible accepts an extra-var nothing
    reads and the template falls back to the default."""
    defaults = DEFAULTS_FILE.read_text()
    missing = sorted(
        var for var in getattr(stv, mapping_name).values()
        if not re.search(rf"^{var}:", defaults, re.MULTILINE)
    )
    assert not missing, (
        f"{mapping_name} maps to {missing}, which defaults/main.yml does not "
        "define — the override would silently do nothing."
    )


# ---------------------------------------------------------------------------
# The escape list, held honest.
# ---------------------------------------------------------------------------
#
# `wiz_write_settings` escapes its values by iterating a hand-written list of
# ~40 variable names. That list is the whole protection, and it is the shape
# this repo already guards elsewhere for the same reason `tests/test_lint_scope.py`
# guards `[tool.ruff] extend-include`: a name added to the wizard later and not
# added to the list is silently unescaped, and every other test still passes —
# including the parametrized quoting tests, which name the variables they cover.
#
# The failure the list prevents is not cosmetic. A `"` or a trailing `\` in an
# operator's answer produces a `settings.toml` the installer cannot parse while
# the wizard exits 0, and `https://host" # rest` parses as a *truncated* value,
# which is the silent half.


def _escaped_variables() -> set[str]:
    """The names `wiz_write_settings`'s escape loop actually covers."""
    text = WIZARD.read_text()
    match = re.search(r"for _tv in \\\n(.*?)\n\s*do\n", text, re.S)
    assert match, (
        "could not find the `for _tv in` escape loop in wizard.sh; the scanner "
        "has rotted and every assertion below would pass on an empty set"
    )
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group(1)))


def _interpolated_into_a_toml_string() -> set[str]:
    """Every variable substituted inside a TOML basic string in wizard.sh.

    Both spellings, because the wizard emits TOML two ways: a heredoc, where a
    quote is a bare `"`, and shell strings it appends blocks to, where the same
    quote is written `\\"`. A scan that knew only one would miss thirteen
    names — every one this stage added — while reporting a healthy set.
    """
    found = set()
    for line in WIZARD.read_text().splitlines():
        # An appended block line starts `varname+="` mid-string; the existing
        # `_wizard_keys` scanner strips the same prefix for the same reason,
        # and without it `[brain] fallback` and `[developer] repos_dir` —
        # both written that way — are invisible while the set still looks
        # healthy.
        stripped = re.sub(r'^\s*[a-z_]+\+?="', "", line)
        match = re.match(
            r'^[a-z_][a-z0-9_]* = \\?"\$\{?([A-Za-z_][A-Za-z0-9_]*)', stripped
        )
        if match:
            found.add(match.group(1))
    # The space-delimited `=` is what separates a TOML line from a shell
    # assignment (`value="$rest"`), which has none. Without that the scan
    # returns every local in the file and the guard fails on noise.
    assert len(found) > 30, (
        f"the scan found only {sorted(found)}; the regex has rotted"
    )
    return found


#: Interpolated but deliberately not in the escape loop, each with its reason.
#: Never add a name here to make the test pass — the question is whether the
#: value can carry a `"` or a `\`, and an operator-typed one always can.
_NOT_ESCAPED_DELIBERATELY = {
    # Locals assigned from `_WIZ_BRAIN_ROLE_*`, which the loop escapes; escaping
    # again would double every backslash.
    "_role_fast": "derived from the already-escaped _WIZ_BRAIN_ROLE_FAST",
    "_role_general": "derived from the already-escaped _WIZ_BRAIN_ROLE_GENERAL",
    "_role_smart": "derived from the already-escaped _WIZ_BRAIN_ROLE_SMART",
}


def test_every_value_interpolated_into_toml_is_escaped_first():
    """The drift guard on the escape list.

    Without this, the list is a comment: the next `_WIZ_*` variable someone
    adds is unescaped by default, and nothing anywhere goes red.
    """
    interpolated = _interpolated_into_a_toml_string()
    escaped = _escaped_variables()
    unguarded = sorted(interpolated - escaped - set(_NOT_ESCAPED_DELIBERATELY))
    assert not unguarded, (
        f"wizard.sh interpolates {unguarded} into a TOML string without passing "
        "them through toml_escape first. A `\"` or a trailing `\\` in any of "
        "them writes a settings.toml the installer cannot parse, with the "
        "wizard exiting 0. Add each to the `for _tv in` loop, or to "
        "_NOT_ESCAPED_DELIBERATELY with the reason it cannot need escaping."
    )


def test_the_exemptions_are_still_real():
    """A stale exemption is a hole that looks like a decision."""
    interpolated = _interpolated_into_a_toml_string()
    stale = sorted(set(_NOT_ESCAPED_DELIBERATELY) - interpolated)
    assert not stale, (
        f"_NOT_ESCAPED_DELIBERATELY names {stale}, which wizard.sh no longer "
        "interpolates into TOML. Drop the entry rather than leaving it open."
    )


def test_the_escape_loop_names_nothing_that_has_gone():
    """The other direction: a name escaped but no longer used is dead weight,
    and `printf -v` on an unset variable quietly writes an empty string."""
    text = WIZARD.read_text()
    gone = sorted(
        name for name in _escaped_variables()
        if len(re.findall(rf"\b{re.escape(name)}\b", text)) < 2
    )
    assert not gone, (
        f"the escape loop names {gone}, which appear nowhere else in "
        "wizard.sh. Drop them; the loop is the list of what needs escaping."
    )


def test_the_guard_can_fail():
    """Positive control. The scan is a regex over a shell script, so its
    healthy answer and its broken answer look alike from here — a rotted
    pattern returns a small set that is trivially a subset of the escape list,
    which is a pass. This plants the exact shape the guard exists to catch and
    requires it to be caught."""
    interpolated = _interpolated_into_a_toml_string()
    escaped = _escaped_variables()
    planted = interpolated | {"_WIZ_A_NEW_SETTING_NOBODY_ESCAPED"}
    assert planted - escaped - set(_NOT_ESCAPED_DELIBERATELY) == {
        "_WIZ_A_NEW_SETTING_NOBODY_ESCAPED"
    }
