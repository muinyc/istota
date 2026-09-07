"""The rendered `[developer]` block and `DeveloperConfig` must agree.

Ansible rewrites `config.toml` on every run, so the template is the only
`[developer]` block a production host ever has. Two ways that drifts from the
code, both of which have happened:

  * a key the loader stopped reading is still rendered, so a retired setting
    persists on every host until someone notices the template; and
  * a field the loader gained is never rendered, so the operator cannot set it
    and the host silently runs the code default — which is how `gh_bin_path`
    came to point at `/usr/local/bin/gh` on a host where `apt` had installed
    `/usr/bin/gh`, warning at every start and failing every forge command.
    (The role now installs to `/usr/local/bin` itself, so those two agree; the
    drift this file exists to catch does not depend on their having differed.)

Both are invisible from the Python side alone, which is why this parses the
template rather than testing the loader again.
"""

import re

import yaml
from dataclasses import fields
from pathlib import Path

import pytest

from istota.config import DeveloperConfig, ReviewConfig
from istota.skill_proxy import resolve_skill_timeout
from istota.skills.code_review import RESERVED_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "deploy" / "ansible" / "templates" / "config.toml.j2"
DEFAULTS = REPO_ROOT / "deploy" / "ansible" / "defaults" / "main.yml"
SETTINGS_TO_VARS = REPO_ROOT / "deploy" / "settings_to_vars.py"

# `key = ...` at the start of a line, ignoring Jinja control lines and the
# indented list items inside a `{% for %}`.
_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*)\s*=", re.MULTILINE)
_VAR_RE = re.compile(r"\b(istota_[a-z0-9_]+)\b")

#: A filter *application*, which is not a variable reference. The role's filter
#: plugins are namespaced `istota_*` exactly like its variables
#: (`istota_toml_escape`, `istota_briefing_blocks_toml`), so a bare
#: `_VAR_RE.findall` reads a filter as a variable and demands a
#: `defaults/main.yml` entry for a Python function.
_FILTER_RE = re.compile(r"\|\s*(istota_[a-z0-9_]+)\b")


def _referenced_vars(block: str) -> set[str]:
    """The `istota_*` variables a template block reads.

    Filter applications are cut out of the text rather than subtracted from
    the result, so a name that is somehow both a filter and a variable is
    still caught in its variable position.
    """
    return set(_VAR_RE.findall(_FILTER_RE.sub(" ", block)))


def _block(header: str) -> str:
    """The template text from `header` to whatever ends it.

    Two things end it. The `{% endif %}` closing the enclosing
    `{% if istota_developer_enabled %}` — counted rather than searched for,
    since the block nests its own `{% if %}` blocks (`author_credit`, the two
    tokens under `istota_use_environment_file`) and the first `{% endif %}`
    after `[developer]` closes one of those. Or the next TOML section header,
    which is what separates `[developer]` from the `[developer.review]`
    subsection sharing the same `{% if %}`: in TOML every key after that header
    belongs to the subsection, so a scanner that ran past it would attribute
    the subsection's keys to the parent and check them against the wrong
    dataclass.
    """
    text = TEMPLATE.read_text()
    lines = text.split("\n")
    start = lines.index(header)
    depth = 1  # already inside `{% if istota_developer_enabled %}`
    for offset, line in enumerate(lines[start:], start=start):
        stripped = line.strip()
        if offset > start and stripped.startswith("["):
            return "\n".join(lines[start:offset])
        if stripped.startswith("{% if "):
            depth += 1
        elif stripped.startswith("{% endif %}"):
            depth -= 1
            if depth == 0:
                return "\n".join(lines[start:offset])
    raise AssertionError(f"unterminated {header} block in config.toml.j2")


@pytest.fixture(scope="module")
def block() -> str:
    text = _block("[developer]")
    # The scanner counts `{% if %}` / `{% endif %}` pairs, so a whitespace-
    # control tag or an inline conditional could skew the count and return a
    # short block — which would turn every `assert key not in block` below into
    # a pass for the wrong reason. Fail loudly instead, on the key that is
    # currently *last* in the block. Move this assertion whenever a key is
    # appended after it: a canary that is no longer last stops being a canary,
    # which is what happened when ISSUE-288 added two keys below
    # `devbox_proxy_audit_log`.
    assert "worktree_retention_hours" in text, (
        "the [developer] block scanner truncated early; the retirement "
        "assertions below would pass vacuously"
    )
    return text


@pytest.fixture(scope="module")
def review_block() -> str:
    text = _block("[developer.review]")
    assert "timeout_seconds" in text, (
        "the [developer.review] block scanner returned nothing usable; the "
        "assertions below would pass vacuously"
    )
    return text


def test_every_rendered_key_is_a_developer_config_field(block):
    field_names = {f.name for f in fields(DeveloperConfig)}
    rendered = set(_KEY_RE.findall(block))
    unknown = sorted(rendered - field_names)
    assert not unknown, (
        f"config.toml.j2 renders {unknown} into [developer], but DeveloperConfig "
        "has no such field. The loader ignores unknown keys, so this is silent: "
        "the setting reaches every host and does nothing."
    )


@pytest.mark.parametrize(
    "key",
    ["gitlab_api_allowlist", "github_api_allowlist", "api_timeout_seconds"],
)
def test_retired_keys_are_no_longer_rendered(block, key):
    """These three were made inert in the loader before the template dropped
    them, so for a while every Ansible run wrote back a key nothing read."""
    assert key not in block


def test_forge_policy_and_binary_paths_are_rendered(block):
    """The wrapper's policy knobs and the path to the real binary are only
    settable through the rendered file. Unrendered, an operator's config.toml
    entry is overwritten on the next deploy."""
    rendered = set(_KEY_RE.findall(block))
    for key in (
        "forge_cli_extra_denied",
        "forge_cli_permit",
        "gh_bin_path",
        "glab_bin_path",
    ):
        assert key in rendered, f"config.toml.j2 does not render [developer] {key}"


def test_both_reviewer_keys_are_rendered(block):
    """ISSUE-289. `gitlab_reviewer` is the username the skill consumes;
    `gitlab_reviewer_id` is the numeric id, kept beside it so an operator who
    recorded one does not lose it on the next deploy. Unrendered, either is
    unsettable from the inventory."""
    rendered = set(_KEY_RE.findall(block))
    for key in ("gitlab_reviewer", "gitlab_reviewer_id"):
        assert key in rendered, f"config.toml.j2 does not render [developer] {key}"


def test_settings_to_vars_maps_both_reviewer_keys():
    text = SETTINGS_TO_VARS.read_text()
    for key in ("gitlab_reviewer", "gitlab_reviewer_id"):
        assert f'"{key}":' in text, f"settings_to_vars.py does not map {key}"


def test_the_username_reviewer_var_has_its_own_default():
    """The role has to define `istota_developer_gitlab_reviewer` in its own
    right rather than aliasing the `_id` var — a template that fell back to the
    id would put a number back in front of `glab --reviewer`."""
    defaults = DEFAULTS.read_text()
    assert re.search(r'^istota_developer_gitlab_reviewer:', defaults, re.MULTILINE)
    assert re.search(r'^istota_developer_gitlab_reviewer_id:', defaults, re.MULTILINE)


def test_every_referenced_var_has_an_ansible_default(block):
    defaults = DEFAULTS.read_text()
    referenced = _referenced_vars(block)
    missing = sorted(
        var for var in referenced if not re.search(rf"^{var}:", defaults, re.MULTILINE)
    )
    assert not missing, (
        f"config.toml.j2 references {missing}, which defaults/main.yml does not "
        "define. With Ansible's default ANSIBLE_ERROR_ON_UNDEFINED_VARS the "
        "template task fails at render time, so this is a broken play rather "
        "than a quiet misconfiguration — but it fails on the deploy, not here, "
        "which is the point of checking it here."
    )


def test_binary_paths_default_to_where_the_role_installs_them():
    """The role extracts `gh` and `glab` from the vendors' release .debs into
    /usr/local/bin, which is also the code default — so the two agree, where
    they used to differ because the archive's packages landed in /usr/bin.

    Kept as an assertion on both sides rather than dropped now that they match:
    the agreement is the thing worth holding, and a change to either one should
    be a deliberate one. `tests/test_ansible_forge_cli_install.py` holds the
    Ansible half against the install destination the task file actually uses.
    """
    defaults = DEFAULTS.read_text()
    assert 'istota_developer_gh_bin_path: "/usr/local/bin/gh"' in defaults
    assert 'istota_developer_glab_bin_path: "/usr/local/bin/glab"' in defaults
    assert DeveloperConfig().gh_bin_path == "/usr/local/bin/gh"
    assert DeveloperConfig().glab_bin_path == "/usr/local/bin/glab"


def test_settings_to_vars_maps_no_retired_developer_key():
    """`settings_to_vars.py` turns a settings dict into Ansible extra-vars. A
    mapping for a retired key resurrects it as an extra-var, which outranks the
    default and would reintroduce the key the template just dropped."""
    text = SETTINGS_TO_VARS.read_text()
    for key in ("gitlab_api_allowlist", "github_api_allowlist", "api_timeout_seconds"):
        assert f'"{key}"' not in text, f"settings_to_vars.py still maps {key}"
    for key in ("forge_cli_extra_denied", "forge_cli_permit", "gh_bin_path", "glab_bin_path"):
        assert f'"{key}"' in text, f"settings_to_vars.py does not map {key}"


def test_settings_to_vars_targets_real_ansible_vars():
    """The mapping's *values* are the names `config.toml.j2` consumes, and a
    typo in one is silent: `convert()` emits an extra-var nothing reads, and
    the template falls back to the default the operator meant to override."""
    text = SETTINGS_TO_VARS.read_text()
    defaults = DEFAULTS.read_text()
    start = text.index("_DEVELOPER_KEYS = {")
    end = text.index("}", start)
    targets = set(re.findall(r'"(istota_developer_[a-z0-9_]+)"', text[start:end]))
    assert targets, "no developer var targets found; the mapping block moved"
    missing = sorted(
        var for var in targets if not re.search(rf"^{var}:", defaults, re.MULTILINE)
    )
    assert not missing, (
        f"settings_to_vars.py maps to {missing}, which defaults/main.yml does "
        "not define — the override would silently do nothing."
    )


def test_every_rendered_review_key_is_a_review_config_field(review_block):
    """Same drift as the parent block, one level down. The loader reads
    `[developer.review]` off a plain `dev.get("review", {})` with an explicit
    name list, so a key it does not name is dropped without a word."""
    field_names = {f.name for f in fields(ReviewConfig)}
    rendered = set(_KEY_RE.findall(review_block))
    unknown = sorted(rendered - field_names)
    assert not unknown, (
        f"config.toml.j2 renders {unknown} into [developer.review], but "
        "ReviewConfig has no such field."
    )


def test_every_referenced_review_var_has_an_ansible_default(review_block):
    defaults = DEFAULTS.read_text()
    referenced = _referenced_vars(review_block)
    missing = sorted(
        var for var in referenced if not re.search(rf"^{var}:", defaults, re.MULTILINE)
    )
    assert not missing, (
        f"config.toml.j2 references {missing}, which defaults/main.yml does not "
        "define — the template task fails at render time."
    )


def _default_int(name: str) -> int:
    match = re.search(rf"^{name}:\s*(\d+)", DEFAULTS.read_text(), re.MULTILINE)
    assert match, f"defaults/main.yml defines no integer {name}"
    return int(match.group(1))


def _review_ceiling() -> int:
    """The ceiling the deploy actually gives `code_review`.

    Resolved through `skill_proxy.resolve_skill_timeout` rather than read out of
    the defaults file, because since ISSUE-448 the number that binds a review
    comes from three places in order: the role's per-skill map, the shipped
    `DEFAULT_SKILL_TIMEOUTS`, and only then the global. The role ships an empty
    map on purpose — a dict var is replaced by a group_vars override rather than
    merged — so a test reading the file alone would report the global and pass
    about an arithmetic nothing uses.
    """
    overrides = yaml.safe_load(DEFAULTS.read_text()).get(
        "istota_security_skill_proxy_timeouts"
    )
    return resolve_skill_timeout(
        _default_int("istota_security_skill_proxy_timeout"),
        overrides,
        "code_review",
    )


def test_review_timeout_default_is_not_clamped_by_the_proxy_ceiling():
    """`cmd_run` shrinks the agent budget to fit the proxy ceiling minus the
    assembly allowance *and* the join slack, because the proxy kills the whole
    command at the ceiling. A default that needs clamping is the worst of both:
    the operator sets a number, the envelope reports a smaller one, and the only
    trace is a warning in the log. Raising either var without the other
    reintroduces exactly that, which is what this catches.

    ISSUE-448 is the case where it did not catch it, because the shipped pair
    satisfied the inequality and was still not enough for the reviewer it was
    spent on. `test_review_timeout_default_covers_a_measured_bughunt_call`
    below is the half about size; this one is only about fit."""
    timeout = _default_int("istota_developer_review_timeout_seconds")
    ceiling = _review_ceiling()
    assert timeout + RESERVED_SECONDS <= ceiling, (
        f"istota_developer_review_timeout_seconds of {timeout}s plus "
        f"{RESERVED_SECONDS}s reserved for assembly and the thread join exceeds "
        f"the {ceiling}s ceiling code_review is given, so every deploy renders "
        "a budget the skill silently clamps."
    )


def test_review_timeout_default_covers_a_measured_bughunt_call():
    """Bughunt was observed dying at exactly its 240-second budget on both real
    diffs on record, and `size_review` only ever puts it on diffs over the
    threshold — so 240 was measured insufficient for the only shape it runs on.
    A deploy default at or under that number reproduces ISSUE-448."""
    assert _default_int("istota_developer_review_timeout_seconds") > 240


def test_the_deploy_never_renders_a_smaller_budget_than_a_bare_install_gets():
    """The var exists as an operator knob, and the code default is now large
    enough on its own (ISSUE-448 raised it, since a bare install reproduced the
    bug too). What the var must not do is silently make a deployed review
    *shorter* than an unconfigured one."""
    deployed = _default_int("istota_developer_review_timeout_seconds")
    assert deployed >= ReviewConfig().timeout_seconds
