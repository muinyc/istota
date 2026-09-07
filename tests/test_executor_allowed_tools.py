"""`WebFetch` is available to every user, and an operator can take it back.

The native brain's `WebFetch` runs in the daemon's own network namespace,
outside the CONNECT allowlist a `claude_code` or `tmux_claude` task is held to.
That asymmetry used to be answered with an identity gate: `build_allowed_tools`
withheld the tool from a non-admin whatever `[brain.native.web_fetch] enabled`
said, and `NativeBrain._build_tools` filters its in-process tool surface by
exactly the list this function returns.

The gate is gone (ISSUE-449). The egress it was standing in for has its own
policy on the same config block — `allow_hosts`, `block_hosts`,
`extra_blocked_cidrs`, `allowed_ports`, `allow_http`, the built-in SSRF
blocklist and `require_url_provenance` — and that policy binds every caller
identically, which an identity gate does not. `admin_only` keeps the old
posture for a deployment that wants it, and is off by default.

The CLI brains are unaffected either way — they run with
`--dangerously-skip-permissions` and never receive this list as an allowlist.
"""

from pathlib import Path

from istota.config import Config, WebFetchConfig
from istota.executor import build_allowed_tools, build_prompt


class TestWebFetchIsNotIdentityScopedByDefault:
    def test_non_admin_has_webfetch(self):
        assert "WebFetch" in build_allowed_tools(is_admin=False, skill_names=[])

    def test_admin_keeps_webfetch(self):
        assert "WebFetch" in build_allowed_tools(is_admin=True, skill_names=[])

    def test_the_default_answer_matches_the_config_default(self):
        """The keyword default and `WebFetchConfig.admin_only` are one answer.

        Two defaults for one decision is the `config_mapper` defect class: the
        dataclass says one thing, a caller's `.get(key, default)` says another,
        and which one a deployment gets depends on whether the block was
        written out. Asserted rather than assumed because the call site passes
        the config value and the keyword default only covers callers that do
        not.
        """
        assert WebFetchConfig().admin_only is False
        assert build_allowed_tools(
            is_admin=False, skill_names=[],
        ) == build_allowed_tools(
            is_admin=False, skill_names=[], web_fetch_admin_only=False,
        )

    def test_the_two_lists_are_identical(self):
        """Nothing at all is scoped by identity here now."""
        assert build_allowed_tools(is_admin=True, skill_names=[]) == (
            build_allowed_tools(is_admin=False, skill_names=[])
        )

    def test_websearch_is_not_scoped_either(self):
        """The two web tools are not the same boundary.

        `WebSearch` runs server-side at the provider and returns titles and
        URLs rather than page bodies, so it grants no egress from this host.
        Asserted so a later reader does not conclude the two moved together for
        the same reason.
        """
        for is_admin in (True, False):
            assert "WebSearch" in build_allowed_tools(
                is_admin=is_admin, skill_names=[],
            )

    def test_skill_names_still_do_not_change_the_list(self):
        base = build_allowed_tools(is_admin=False, skill_names=[])
        assert base == build_allowed_tools(
            is_admin=False, skill_names=["developer", "browse"],
        )


class TestAdminOnlyRestoresTheOldPosture:
    """`admin_only = true` is the operator's way back to the ISSUE-389 gate.

    Read unconditionally rather than only where the task routes to native,
    matching what the identity gate did: the asymmetry it addresses exists on a
    native-default deployment as well as in a room somebody pinned, and a rule
    that applied only to pinned rooms would leave a non-admin with more egress
    on the deployment default than in a pinned room.
    """

    def test_non_admin_loses_webfetch(self):
        assert "WebFetch" not in build_allowed_tools(
            is_admin=False, skill_names=[], web_fetch_admin_only=True,
        )

    def test_admin_keeps_it(self):
        assert "WebFetch" in build_allowed_tools(
            is_admin=True, skill_names=[], web_fetch_admin_only=True,
        )

    def test_webfetch_is_the_only_difference(self):
        admin = build_allowed_tools(
            is_admin=True, skill_names=[], web_fetch_admin_only=True,
        )
        non_admin = build_allowed_tools(
            is_admin=False, skill_names=[], web_fetch_admin_only=True,
        )
        assert set(admin) - set(non_admin) == {"WebFetch"}
        assert set(non_admin) - set(admin) == set()


class TestTheNativeToolSurfaceFollows:
    """The scoping is only worth anything because the native brain reads it.

    `build_allowed_tools` returning a shorter list is a fact about a list; what
    makes it a boundary is `_build_tools` filtering on it. Both directions, so
    the assertion cannot pass against a brain that never builds the tool.
    """

    def _brain_and_request(self, is_admin: bool, admin_only: bool = False):
        from istota.brain._types import BrainRequest
        from istota.brain.native import NativeBrain
        from istota.config import NativeBrainConfig

        brain = NativeBrain(NativeBrainConfig(model="m"), provider=object())
        req = BrainRequest(
            prompt="hi",
            allowed_tools=build_allowed_tools(
                is_admin=is_admin,
                skill_names=[],
                web_fetch_admin_only=admin_only,
            ),
            cwd=Path("/tmp"),
            env={},
            timeout_seconds=30,
        )
        return brain, req

    def test_non_admin_native_request_has_the_webfetch_tool(self):
        brain, req = self._brain_and_request(is_admin=False)
        assert "WebFetch" in [t.schema.name for t in brain._build_tools(req)]

    def test_admin_native_request_has_the_webfetch_tool(self):
        brain, req = self._brain_and_request(is_admin=True)
        assert "WebFetch" in [t.schema.name for t in brain._build_tools(req)]

    def test_admin_only_still_withholds_it_from_a_non_admin(self):
        brain, req = self._brain_and_request(is_admin=False, admin_only=True)
        assert "WebFetch" not in [t.schema.name for t in brain._build_tools(req)]


class TestTheWithheldCaseIsStated:
    """A withheld tool is said out loud rather than left as an absence.

    Under the identity gate a non-admin on a deployment with no browser service
    got no page-reading line at all: the tool was unregistered and the prompt
    said nothing, so the model had no way to tell the user why reading a page
    did not happen. That silence is the third thing ISSUE-449 asked about, and
    it survives `admin_only`, which is the setting that reintroduces it.

    The sentence is only true where the task would have carried the daemon-side
    tool, which is why the predicate asks the routing question that
    `build_allowed_tools` deliberately does not. `admin_only` only ever removes
    the native brain's own tool; a CLI-brain task keeps the `claude` CLI's
    `WebFetch` whatever the list says, so telling that user they have no fetch
    tool would be an absence asserted where there is none.
    """

    def _config(self, tmp_path, *, kind: str = "native", browser: bool = False,
                **web_fetch) -> Config:
        config = Config(
            db_path=tmp_path / "t.db",
            skills_dir=tmp_path / "skills",
            bundled_skills_dir=tmp_path / "_empty",
            temp_dir=tmp_path / "temp",
        )
        config.skills_dir.mkdir(parents=True, exist_ok=True)
        config.brain.kind = kind
        config.browser.enabled = browser
        for k, v in web_fetch.items():
            setattr(config.brain.native.web_fetch, k, v)
        return config

    def _task(self):
        from istota import db

        return db.Task(
            id=1, status="running", source_type="talk", user_id="alice",
            prompt="read https://example.com", conversation_token="room1",
        )

    def test_non_admin_is_told_about_webfetch_by_default(self, tmp_path):
        system = build_prompt(
            self._task(), [], self._config(tmp_path), is_admin=False,
        ).system
        assert "Reading web pages: WebFetch fetches a URL" in system

    def test_non_admin_is_told_why_it_is_missing_under_admin_only(self, tmp_path):
        system = build_prompt(
            self._task(), [], self._config(tmp_path, admin_only=True),
            is_admin=False,
        ).system
        assert "restricted to administrators" in system
        assert "Reading web pages: WebFetch fetches a URL" not in system

    def test_an_admin_under_admin_only_is_told_nothing_new(self, tmp_path):
        system = build_prompt(
            self._task(), [], self._config(tmp_path, admin_only=True),
            is_admin=True,
        ).system
        assert "Reading web pages: WebFetch fetches a URL" in system
        assert "restricted to administrators" not in system

    def test_a_cli_brain_task_is_not_told_it_has_no_fetch_tool(self, tmp_path):
        """The one the setting must not be allowed to lie about.

        `build_allowed_tools` drops `WebFetch` from the list here too, and that
        list is not an allowlist for either CLI brain — they run with the full
        default toolset — so the task does have a fetch tool and the prompt has
        to keep naming it.
        """
        system = build_prompt(
            self._task(), [],
            self._config(tmp_path, kind="claude_code", admin_only=True),
            is_admin=False,
        ).system
        assert "Reading web pages: WebFetch fetches a URL" in system
        assert "restricted to administrators" not in system

    def test_a_disabled_tool_is_not_blamed_on_administrators(self, tmp_path):
        """`enabled = false` is an operator withdrawing the tool from everyone.

        The wording names administrators, so it must not be reached by a
        deployment where the reason is something else. What a non-admin is told
        there is the pre-ISSUE-449 line, which overstates the tool on a native
        deployment with it switched off — that inconsistency predates this
        change, is on the `enabled` axis rather than the identity one, and is
        left alone deliberately rather than papered over here.
        """
        system = build_prompt(
            self._task(), [],
            self._config(tmp_path, enabled=False, admin_only=True),
            is_admin=False,
        ).system
        assert "restricted to administrators" not in system

    def test_the_browser_branch_keeps_its_remedy(self, tmp_path):
        """A deployment with the browse skill up has a working route.

        So the withheld case there drops the WebFetch fallback sentence and
        says nothing further — an absence with a remedy beside it is not the
        silence ISSUE-449 was about.
        """
        system = build_prompt(
            self._task(), [],
            self._config(tmp_path, browser=True, admin_only=True),
            is_admin=False,
        ).system
        assert "prefer the browse skill" in system
        assert "lightweight fallback" not in system
        assert "restricted to administrators" not in system


class TestTheUntrustedInputGuardrailsFollowTheTool:
    """`untrusted_input` is folded in wherever the tool is actually built.

    The predicate used to short-circuit on `is_admin`, which was correct only
    because the tool was withheld on that same axis. It now asks the question
    the fold is about — will this task have the tool — so a non-admin's native
    task gets the inbound-handling guidance it is now able to need.
    """

    def _task(self, **kw):
        from istota import db

        defaults = dict(
            id=1, status="running", source_type="talk", user_id="alice",
            prompt="hi", conversation_token="room1", brain="native",
        )
        defaults.update(kw)
        return db.Task(**defaults)

    def _config(self, tmp_path, **web_fetch) -> Config:
        config = Config(
            db_path=tmp_path / "t.db",
            skills_dir=tmp_path / "skills",
            bundled_skills_dir=tmp_path / "_empty",
            temp_dir=tmp_path / "temp",
        )
        config.skills_dir.mkdir(parents=True, exist_ok=True)
        config.brain.kind = "native"
        for k, v in web_fetch.items():
            setattr(config.brain.native.web_fetch, k, v)
        return config

    def test_non_admin_native_task_folds_it_in(self, tmp_path):
        from istota.executor import _native_web_fetch_enabled

        assert _native_web_fetch_enabled(
            self._task(), self._config(tmp_path), False,
        )

    def test_admin_only_takes_it_back_out_for_a_non_admin(self, tmp_path):
        from istota.executor import _native_web_fetch_enabled

        config = self._config(tmp_path, admin_only=True)
        assert not _native_web_fetch_enabled(self._task(), config, False)
        assert _native_web_fetch_enabled(self._task(), config, True)

    def test_a_disabled_tool_folds_nothing_in_for_anyone(self, tmp_path):
        from istota.executor import _native_web_fetch_enabled

        config = self._config(tmp_path, enabled=False)
        assert not _native_web_fetch_enabled(self._task(), config, True)
        assert not _native_web_fetch_enabled(self._task(), config, False)
