"""``docker/istota/config-diff.py`` — the boot's drift report, by key.

The entrypoint calls it twice: after a re-render, to say what this boot changed,
and under ``ISTOTA_CONFIG_RENDER=preserve``, to say what this boot is ignoring.
ISSUE-368's own conclusion was that the class of failure is the silence, so this
is the half of the fix that survives whichever mode a deployment runs in.

Two properties carry real weight. **It never prints a value that must not be
logged**, because the destination is the container log and ``config.toml`` holds
the bot's app password, the OAuth2 client secret, the forge tokens and the Talk
room tokens. And **it never fails the boot**, because it runs on the start-up
path of a deployment that is otherwise fine.

The first property is where this file earned its keep, in the wrong direction:
its first draft asserted that ``users.a.log_channel`` was *not* a credential,
which is where the room token lives — so the leak was covered by a passing test
saying it was correct, and the only redaction case with coverage was
``imap_password``, whose leaf the substring rule cannot get wrong. That is the
"probe whose success is indistinguishable from a no-op" failure
``.claude/rules/testbed.md`` describes, and the cases below are picked so that
the rule has to do real work to satisfy them.

Loaded by path rather than imported: the file has no ``.py``-importable name in
a package, and it deliberately ships as a standalone script beside
``entrypoint.sh`` so a container can run it with the system ``python3``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "docker" / "istota" / "config-diff.py"


def _load():
    spec = importlib.util.spec_from_file_location("istota_config_diff", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config_diff = _load()


def describe(old: dict, new: dict) -> list[str]:
    return config_diff.describe(old, new, label_old="before", label_new="after")


class TestFlattening:
    def test_nested_tables_become_dotted_keys(self):
        flat = config_diff.flatten({"brain": {"native": {"model": "glm-5.2"}}})
        assert flat == {"brain.native.model": "glm-5.2"}

    def test_an_array_of_tables_is_indexed(self):
        flat = config_diff.flatten(
            {"users": {"a": {"resources": [{"type": "feeds"}, {"type": "money"}]}}}
        )
        assert flat == {
            "users.a.resources[0].type": "feeds",
            "users.a.resources[1].type": "money",
        }

    def test_a_scalar_array_stays_whole(self):
        """`email_addresses = ["a@x", "b@x"]` reads better as one value."""
        flat = config_diff.flatten({"users": {"a": {"email_addresses": ["a@x", "b@x"]}}})
        assert flat == {"users.a.email_addresses": ["a@x", "b@x"]}


class TestWhatMustNotBePrinted:
    @pytest.mark.parametrize(
        "key",
        [
            "nextcloud.app_password",
            "web.oauth2_client_secret",
            "web.session_secret_key",
            "users.a.resources[0].ingest_token",
            "developer.gitlab_token",
            "web.map.api_key",
            "users.a.monarch_password",
        ],
    )
    def test_credential_shaped_keys_are_recognised(self, key):
        assert config_diff.is_sensitive(key)

    @pytest.mark.parametrize("key", ["users.a.log_channel", "users.a.alerts_channel"])
    def test_the_talk_room_tokens_are_recognised(self, key):
        """These hold a bearer token and match no credential word at all.

        `render-config.sh` writes the token `create_group_room` returned
        straight into `log_channel` / `alerts_channel`, and whoever holds a Talk
        room token can read and post in that room. The first draft of this
        module printed both in full while its docstring said it did not, and
        this file asserted that was correct — so the leak had a test holding it
        in place.
        """
        assert config_diff.is_sensitive(key)

    def test_the_email_address_is_withheld(self):
        assert config_diff.is_sensitive("users.a.email_addresses")

    def test_a_boolean_that_merely_contains_channel_is_still_printed(self):
        """`scheduler.log_channel_show_skills` is why the rule is exact-leaf.

        Adding `channel` to the substring markers would have withheld this too,
        and it is an ordinary setting whose flips are worth seeing.
        """
        assert not config_diff.is_sensitive("scheduler.log_channel_show_skills")

    @pytest.mark.parametrize(
        "key",
        [
            "brain.native.model",
            "nextcloud.url",
            "logging.rotate",
        ],
    )
    def test_ordinary_keys_are_not(self, key):
        assert not config_diff.is_sensitive(key)

    @pytest.mark.parametrize(
        "key",
        [
            "brain.native.extra_headers.x-api-key",
            "brain.native.extra_headers.api-key",
            "brain.native.extra_headers.Authorization",
        ],
    )
    def test_the_three_header_shapes_the_audit_found_printed(self, key):
        """F31, reproduced by executing this module rather than by reading it.

        `flatten` turns `[brain.native.extra_headers]` into one dotted key per
        header, so the *header name* is the leaf the rule sees. All three of
        these are how a non-Anthropic deployment spells its provider key, none
        of them matched a marker before this stage, and every one of them was
        printed in full into `docker logs`.

        Two independent rules now answer for these keys, and this test cannot
        tell them apart — it stayed green with the hyphen normalization
        reverted, because the wholesale rule catches the prefix before the leaf
        is ever computed. Measured, not assumed. The sibling below is what
        isolates the leaf half; keep both.
        """
        assert config_diff.is_sensitive(key)

    @pytest.mark.parametrize(
        "header", ["x-api-key", "api-key", "Authorization"]
    )
    def test_the_same_header_names_match_outside_the_wholesale_subtree(
        self, header
    ):
        """The leaf half of the same fix, with the wholesale rule out of reach.

        `admin_config_view` folds hyphens in `is_secret_name` and applies it to
        every key of every dict-valued field, so a credential header under some
        *other* config table is redacted there. The rule here has to hold on its
        own, and asserting it only under `brain.native.extra_headers` would
        leave the normalization pinned by nothing that names the finding.
        """
        assert config_diff.is_sensitive(f"some.section.{header}")

    def test_a_header_matching_no_marker_is_withheld_anyway(self):
        """The wholesale rule, and the reason it is not redundant.

        The three spellings above now match on their own. This one does not,
        and `brain.native.extra_headers` is an auth channel by construction —
        `llm/openai_compat.py` merges it over the `Authorization` header — so a
        header nobody has thought of must not be the thing standing between a
        container log and a live provider key. `admin_config_view` redacts the
        whole field for the same reason.
        """
        assert config_diff.is_sensitive("brain.native.extra_headers.x-tenant")
        assert config_diff.is_sensitive("brain.native.extra_headers")

    def test_the_wholesale_rule_does_not_leak_onto_a_sibling(self):
        """Prefix, not `startswith`. `extra_headers_enabled` is not a subtree."""
        assert not config_diff.is_sensitive("brain.native.extra_headers_note")


class TestParityWithTheAdminConfigView:
    """`is_sensitive` restates `admin_config_view.is_secret_field`, and cannot
    import it: this file runs under the system `python3` from `entrypoint.sh`,
    before and independently of the venv, and its docstring commits to stdlib
    only. So the restatement is pinned here instead — the pattern the repo
    already uses for `usageFormat.parity.test.ts` and the vendored devbox lib.

    **The corpus is walked out of the `Config` dataclass tree, not written
    down.** A hand-written key list is exactly the guard round 1 shipped twice
    and found blind both times: it can only ever assert about the fields whoever
    wrote it happened to think of, so a field added later is outside it while
    the test reports green. The walk follows dataclass-typed fields and the
    dataclass element types of `dict` and `list` fields, so `users.x.log_channel`
    and `users.x.resources[0].*` are in it.

    What that buys is **parity across every shipped field**, and not more than
    that. This is a parity test: a credential field both modules miss is green
    here, because neither side flags it. The rule that a new credential must be
    matched at all lives in `tests/test_admin_config_view.py`, which asserts it
    against the reference implementation; this file only holds the restatement
    to it.

    Its one boundary, stated rather than implied: the rendered `config.toml`
    carries a few keys no dataclass field declares (per-resource `ingest_token`,
    `monarch_password`), so those stay as the hand-written cases in
    `TestWhatMustNotBePrinted` above.
    """

    @staticmethod
    def _corpus() -> list[tuple[str, str]]:
        import dataclasses
        import typing

        from istota import config as config_module

        def walk(cls, prefix: str, seen: frozenset):
            if cls in seen:
                return
            seen = seen | {cls}
            hints = typing.get_type_hints(cls)
            for field in dataclasses.fields(cls):
                annotation = hints[field.name]
                key = f"{prefix}.{field.name}" if prefix else field.name
                if dataclasses.is_dataclass(annotation) and isinstance(
                    annotation, type
                ):
                    yield from walk(annotation, key, seen)
                    continue
                nested = [
                    arg
                    for arg in typing.get_args(annotation)
                    if dataclasses.is_dataclass(arg) and isinstance(arg, type)
                ]
                if nested:
                    suffix = (
                        ".x" if typing.get_origin(annotation) is dict else "[0]"
                    )
                    for child in nested:
                        yield from walk(child, key + suffix, seen)
                    continue
                yield key, field.name

        return sorted(set(walk(config_module.Config, "", frozenset())))

    def test_the_corpus_is_big_enough_to_mean_something(self):
        """A walk that silently stopped returning fields would satisfy every
        assertion below vacuously. This is the floor that says it did not."""
        corpus = self._corpus()
        assert len(corpus) > 300, len(corpus)
        keys = {key for key, _ in corpus}
        assert "users.x.log_channel" in keys
        assert "brain.native.api_key" in keys

    def test_every_field_the_admin_view_redacts_is_withheld_here(self):
        """The one direction that is a safety property.

        The other direction is deliberately not asserted: `admin_config_view`
        carries a `NON_SECRET_KEYS` allowlist (`web.token_storage`,
        `security.passthrough_env_vars`, the two tmux markers) and this module
        does not, so it withholds a handful of operational values it need not.
        That is the safe direction on a log line, and an allowlist on the boot
        path is a fail-open surface for the sake of legibility.
        """
        from istota import admin_config_view as admin

        leaked = [
            key
            for key, name in self._corpus()
            if admin.is_secret_field(key, name) and not config_diff.is_sensitive(key)
        ]
        assert leaked == [], (
            "config-diff.py would print these into the container log while "
            f"the admin config view redacts them: {leaked}"
        )

    def test_the_marker_list_covers_the_admin_view_s(self):
        """A marker whose effect no shipped field happens to exercise is
        invisible to the corpus test above; this is what catches it."""
        from istota import admin_config_view as admin

        missing = {p.lower() for p in admin.SECRET_NAME_PATTERNS} - set(
            config_diff._CREDENTIAL_MARKERS
        )
        assert missing == set(), (
            f"admin_config_view.SECRET_NAME_PATTERNS gained {sorted(missing)}; "
            "config-diff.py._CREDENTIAL_MARKERS restates that list and cannot "
            "import it"
        )

    def test_the_wholesale_list_covers_the_admin_view_s(self):
        from istota import admin_config_view as admin

        missing = set(admin._ALWAYS_SECRET_KEYS) - set(
            config_diff._ALWAYS_WITHHELD_PREFIXES
        )
        assert missing == set(), (
            f"admin_config_view._ALWAYS_SECRET_KEYS gained {sorted(missing)}; "
            "config-diff.py._ALWAYS_WITHHELD_PREFIXES restates that list"
        )

    def test_hyphenated_and_underscored_spellings_agree(self):
        """The normalization, asserted as the equivalence it is rather than
        against three literals — `is_secret_name` folds hyphens and this did
        not, which is the whole of why the three header shapes printed."""
        from istota import admin_config_view as admin

        for name in ("x-api-key", "api-key", "authorization", "x-auth-token"):
            assert admin.is_secret_name(name)
            assert config_diff.is_sensitive(f"some.section.{name}")
            assert config_diff.is_sensitive(
                f"some.section.{name.replace('-', '_')}"
            )


class TestTheReport:
    def test_agreement_reports_nothing(self):
        assert describe({"a.b": 1}, {"a.b": 1}) == []

    def test_a_changed_value_is_named_with_both_sides(self):
        (line,) = describe({"brain.native.model": "old"}, {"brain.native.model": "new"})
        assert "brain.native.model" in line
        assert "old" in line and "new" in line

    def test_a_changed_credential_is_named_without_either_side(self):
        (line,) = describe(
            {"nextcloud.app_password": "hunter2"},
            {"nextcloud.app_password": "correct-horse"},
        )
        assert "nextcloud.app_password" in line
        assert "hunter2" not in line and "correct-horse" not in line
        assert config_diff.REDACTED in line

    def test_an_added_credential_is_named_without_its_value(self):
        (line,) = describe({}, {"developer.gitlab_token": "glpat-secret"})
        assert "developer.gitlab_token" in line
        assert "glpat-secret" not in line

    def test_a_removed_credential_is_named_without_its_value(self):
        (line,) = describe({"developer.gitlab_token": "glpat-secret"}, {})
        assert "developer.gitlab_token" in line
        assert "glpat-secret" not in line

    def test_a_long_value_is_truncated(self):
        (line,) = describe({"a.b": "x"}, {"a.b": "y" * 500})
        assert len(line) < 300
        assert "..." in line

    def test_a_truncated_string_is_still_a_balanced_literal(self):
        """Truncating the `repr` cut inside the quotes and printed `'aaa...`.

        Which reads as a malformed value rather than an elided one, on a log
        line whose whole job is to be read by a person.
        """
        (line,) = describe({"a.b": "x"}, {"a.b": "y" * 500})
        rendered = line.split("-> ")[1].split(" (after)")[0]
        assert rendered.startswith("'") and rendered.endswith("'"), rendered

    def test_keys_are_reported_in_a_stable_order(self):
        lines = describe({}, {"z.a": 1, "a.z": 2, "m.m": 3})
        assert [line.split()[1] for line in lines] == ["a.z", "m.m", "z.a"]


class TestItNeverFailsTheBoot:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_a_missing_file_exits_zero_and_says_why(self, tmp_path):
        present = tmp_path / "a.toml"
        present.write_text('x = 1\n')

        result = self._run(str(present), str(tmp_path / "absent.toml"))

        assert result.returncode == 0
        assert "does not exist" in result.stderr

    def test_unparseable_toml_exits_zero_and_says_why(self, tmp_path):
        good = tmp_path / "a.toml"
        good.write_text('x = 1\n')
        bad = tmp_path / "b.toml"
        bad.write_text('x = = =\n')

        result = self._run(str(good), str(bad))

        assert result.returncode == 0
        assert "not valid TOML" in result.stderr

    def test_a_real_difference_is_printed_and_still_exits_zero(self, tmp_path):
        old = tmp_path / "a.toml"
        old.write_text('[brain.native]\nmodel = "old"\n')
        new = tmp_path / "b.toml"
        new.write_text('[brain.native]\nmodel = "new"\n')

        result = self._run(str(old), str(new), "--heading", "config.toml changed")

        assert result.returncode == 0
        assert "config.toml changed (1 key(s))" in result.stdout
        assert "brain.native.model" in result.stdout

    def test_identical_files_print_nothing(self, tmp_path):
        for name in ("a.toml", "b.toml"):
            (tmp_path / name).write_text('[brain.native]\nmodel = "same"\n')

        result = self._run(str(tmp_path / "a.toml"), str(tmp_path / "b.toml"))

        assert result.returncode == 0
        assert result.stdout == ""


class TestItShipsInTheImage:
    def test_the_dockerfile_copies_it_beside_the_entrypoint(self):
        """The entrypoint resolves it relative to its own directory.

        A guard there makes a missing reporter cost the report rather than the
        boot, which is right — and is exactly why nothing else would notice the
        Dockerfile forgetting the file.
        """
        dockerfile = (REPO / "docker" / "istota" / "Dockerfile").read_text()
        assert "COPY docker/istota/config-diff.py /config-diff.py" in dockerfile
