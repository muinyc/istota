"""Timezone must not be clobbered by Ansible re-provisioning (ISSUE-102 follow-up).

The ISSUE-102 fix made the *read* paths seed-only: ``hydrate_user_configs``
(Nextcloud) and ``merge_into_user_config`` (config.toml overlay) both leave an
explicit, user-set timezone alone across restarts. But the Ansible *write* path
bypassed all of it: the "Ensure user_profiles rows" task rendered
``istota user ensure ... --tz "<inventory tz>"`` on every deploy, doing an
unconditional partial UPDATE of ``user_profiles.timezone`` and then notifying a
scheduler restart. A user who picked their timezone in the web UI had it
overwritten on the next deploy.

Option B: timezone is a user-facing preference (web UI + Nextcloud), not
deployment infra. The Ansible provisioning command must not pass ``--tz`` at
all, so a redeploy can never clobber the web-set value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

REPO = Path(__file__).resolve().parent.parent
TASKS_FILE = REPO / "deploy" / "ansible" / "tasks" / "main.yml"


def _ensure_profiles_command() -> str:
    """Return the ``command:`` template of the 'Ensure user_profiles rows' task."""
    tasks = yaml.safe_load(TASKS_FILE.read_text())
    for task in tasks:
        if isinstance(task, dict) and task.get("name") == "Ensure user_profiles rows":
            assert "command" in task, "task found but has no `command:` key"
            return task["command"]
    raise AssertionError("task 'Ensure user_profiles rows' not found in tasks/main.yml")


def _render(command: str, user_value: dict) -> str:
    """Render the command template the way Ansible would for one user.

    The task sets ``user_id`` via ``vars:`` from ``user_item.key``; the
    surrounding play supplies ``istota_home`` / ``istota_package`` /
    ``istota_repo_dir``. All Jinja in the command uses standard filters
    (``default``, ``is defined``), so a bare Jinja2 Environment renders it.
    """
    env = Environment()
    return env.from_string(command).render(
        istota_home="/srv/app/istota",
        istota_package="istota",
        istota_repo_dir="/srv/app/istota",
        user_id="alice",
        user_item={"key": "alice", "value": user_value},
    )


class TestAnsibleUserEnsureOmitsTimezone:
    def test_command_template_never_passes_tz(self):
        # An inventory timezone must not flow into the provisioning command,
        # else every deploy clobbers the web-UI-set value.
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "timezone": "Europe/Lisbon"},
        )
        assert "--tz" not in rendered, (
            "Ansible still passes --tz; a redeploy will overwrite the "
            "web-set timezone in user_profiles"
        )
        assert "Europe/Lisbon" not in rendered, (
            "inventory timezone leaked into the user-ensure command"
        )

    def test_command_template_still_provisions_other_fields(self):
        # Guard against an over-broad edit that drops the whole task body:
        # the non-timezone profile fields must still be provisioned.
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "timezone": "Europe/Lisbon"},
        )
        assert "user ensure" in rendered
        assert "--name alice" in rendered
        assert "--display-name" in rendered


class TestTimezoneSurvivesRedeploy:
    """End-to-end: web edit then redeploy preserves the user's timezone.

    Replays the lifecycle through the real CLI entrypoint with the
    post-fix invocation shape (no ``--tz``).
    """

    @pytest.fixture
    def cfg_with_db(self, tmp_path, monkeypatch):
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        return cfg, db_path

    def test_web_set_timezone_survives_redeploy(self, cfg_with_db):
        from istota import user_profiles
        from istota.cli import cmd_user_ensure

        from tests.test_cli_user_ensure import _FakeArgs

        cfg, db_path = cfg_with_db

        # First deploy: Ansible provisions the profile (no --tz under Option B).
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", display_name="Alice"))

        # User picks their timezone in the web UI.
        user_profiles.update_profile(db_path, "alice", timezone="Europe/Lisbon")

        # Redeploy: same provisioning invocation runs again.
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", display_name="Alice"))

        profile = user_profiles.get_profile(db_path, "alice")
        assert profile is not None
        assert profile.timezone == "Europe/Lisbon"


class TestAnsibleUserEnsureRestartsBothTiers:
    """Adding a user to the inventory must restart the web tier too.

    Both tiers snapshot the user set at config load: ``_apply_user_profiles``
    creates a ``config.users`` entry for a ``user_profiles`` row with no TOML
    counterpart, but ``config.users`` is only rebuilt by a full load — at
    startup and on SIGHUP. A web process that has not restarted refuses the new
    user's Nextcloud login with "Access denied: user not configured"
    (``web_app._oauth2_callback``) even though the DB row is correct.

    Nothing else in the play covers it. Per-user data no longer renders into
    ``config.toml``, so adding a user leaves the rendered file byte-identical
    and the "Deploy istota configuration" task — the one that does notify a web
    restart — stays ``ok``.
    """

    @staticmethod
    def _task() -> dict:
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        return next(
            t for t in tasks
            if isinstance(t, dict) and t.get("name") == "Ensure user_profiles rows"
        )

    def test_it_notifies_the_web_tier(self):
        assert "restart istota-web" in self._task()["notify"]

    def test_it_still_notifies_the_scheduler(self):
        assert "restart istota-scheduler" in self._task()["notify"]

    def test_changed_is_derived_from_the_cli_state_line(self):
        """`changed_when: false` silently suppresses every handler above.

        This is the half that is easy to get wrong: the task carried a
        ``notify: restart istota-scheduler`` for as long as it has existed, and
        a hardcoded ``changed_when: false`` meant it never once fired. Adding a
        second handler under that would be equally inert, so pin the condition
        rather than only the notify list.
        """
        changed_when = self._task().get("changed_when")
        assert changed_when is not False, (
            "changed_when: false suppresses the notify handlers, so a new user "
            "restarts neither tier"
        )
        assert "noop" in str(changed_when), (
            "changed should be derived from the CLI's own STATE: line"
        )

    def test_the_cli_emits_the_state_line_the_condition_matches(self, tmp_path, capsys):
        """The condition and the CLI live in different files.

        A reworded STATE line would leave the play reporting `changed` on every
        deploy, restarting both tiers each run — the failure direction is noisy
        rather than silent, but it is still wrong.
        """
        from istota import db
        from istota.cli import cmd_user_ensure

        from tests.test_cli_user_ensure import _FakeArgs

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\ntemp_dir = "{tmp_path / "tmp"}"\n'
        )

        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", display_name="Alice"))
        assert "STATE: created" in capsys.readouterr().out

        # A redeploy that changes nothing must not report `changed`, or every
        # play bounces the web tier.
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", display_name="Alice"))
        assert "STATE: noop" in capsys.readouterr().out


class TestAnsibleOutboundApprovalSurface:
    """The outbound approval gate must be operable from the inventory.

    Carried out of the Stage 4 review of the outbound-email-approval spec:
    ``[email] outbound_approval_floor`` defaults to ``untrusted`` in the
    dataclass, so the gate switches itself on for every existing deployment at
    upgrade — and with no Ansible surface there was no supported way to turn it
    back off, since the role overwrites hand edits to ``config.toml`` on the
    next play. These pin the three pieces that make it operable, each of which
    is silently inert without the other two.
    """

    @staticmethod
    def _defaults() -> dict:
        return yaml.safe_load(
            (REPO / "deploy" / "ansible" / "defaults" / "main.yml").read_text()
        )

    @staticmethod
    def _template() -> str:
        return (
            REPO / "deploy" / "ansible" / "templates" / "config.toml.j2"
        ).read_text()

    def test_the_floor_has_a_default_matching_the_dataclass(self):
        from istota.config import EmailConfig

        value = self._defaults()["istota_email_outbound_approval_floor"]
        # Running the role must not re-decide the policy on its own. A default
        # here that disagrees with the code changes behaviour for every
        # deployment that never set the variable.
        assert value == EmailConfig().outbound_approval_floor

    def test_the_template_renders_the_floor(self):
        assert "istota_email_outbound_approval_floor" in self._template(), (
            "the variable exists but nothing renders it into config.toml, so "
            "setting it in the inventory would do nothing"
        )

    def test_the_rendered_floor_survives_a_config_load(self, tmp_path):
        """End to end on the value that matters: an operator turning it off.

        An invalid floor raises at config load rather than falling back, so a
        template rendering (say) an unquoted bareword takes the daemon down on
        the next deploy instead of degrading.
        """
        from istota.config import load_config

        # The template as a whole uses Ansible-only filters (`to_json`), so a
        # bare Jinja2 Environment cannot compile it. Render the one line under
        # test, which is what this is about anyway. The role's own filters are
        # registered because that line carries `istota_toml_escape` — every
        # interpolation into a TOML basic string does since ISSUE-443, so a
        # bare Environment now fails to compile this line too.
        from tests.test_ansible_config_template import _custom_filters

        source = next(
            ln for ln in self._template().splitlines()
            if ln.startswith("outbound_approval_floor")
        )
        env = Environment()
        env.filters.update(_custom_filters())
        line = env.from_string(source).render(
            istota_email_outbound_approval_floor="off",
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(f"[email]\n{line}\n")
        assert load_config(cfg).email.outbound_approval_floor == "off"

    def test_the_floor_is_asserted_before_the_config_is_rendered(self):
        """`off` is a YAML boolean, and it is the value an operator reaches for.

        An inventory writing `istota_email_outbound_approval_floor: off`
        unquoted hands Jinja Python `False`, which renders
        `outbound_approval_floor = "False"` — and the config loader *raises* on
        an unrecognized floor rather than falling back, so that is a config file
        the daemon cannot load. `validate_config.py` would catch it, but only
        after the broken file is already on disk, where the auto-update cron's
        next restart finds it. The assert has to come before the template task
        so the failure names the variable instead.
        """
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        names = [t.get("name") for t in tasks if isinstance(t, dict)]
        assert "Validate outbound approval floor" in names
        assert names.index("Validate outbound approval floor") < names.index(
            "Deploy istota configuration"
        ), "the assert runs after the render, so a bad value is written to disk first"

        assertion = next(
            t for t in tasks
            if isinstance(t, dict) and t.get("name") == "Validate outbound approval floor"
        )
        condition = " ".join(assertion["assert"]["that"])
        for policy in ("off", "untrusted", "all"):
            assert f"'{policy}'" in condition, f"{policy} is not an accepted floor"

    def test_the_per_user_policy_is_asserted_too(self):
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        assertion = next(
            t for t in tasks
            if isinstance(t, dict)
            and t.get("name") == "Validate per-user outbound approval policies"
        )
        condition = " ".join(assertion["assert"]["that"])
        # "" is a value here (follow the floor) and must stay accepted, or the
        # documented way to clear a user's policy fails the play.
        assert "''" in condition or '""' in condition
        for policy in ("off", "untrusted", "all"):
            assert f"'{policy}'" in condition

    def test_user_ensure_threads_the_per_user_policy(self):
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "outbound_approval": "all"},
        )
        assert '--outbound-approval "all"' in rendered

    def test_an_empty_per_user_policy_is_still_passed(self):
        """`""` is a value, not an omission — it clears the user back to
        following the operator floor. A truthiness test on this key would put
        that out of reach from the inventory, which is why the template asks
        ``is defined`` here and truthiness elsewhere."""
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "outbound_approval": ""},
        )
        assert '--outbound-approval ""' in rendered

    def test_an_absent_per_user_policy_passes_nothing(self):
        # Omitting the key leaves a web- or CLI-set value alone, the same
        # non-clobber rule timezone has.
        rendered = _render(
            _ensure_profiles_command(), {"display_name": "Alice"},
        )
        assert "--outbound-approval" not in rendered

    def test_a_null_per_user_policy_is_treated_as_absent(self):
        """`outbound_approval:` with nothing after it is a dangling key, not a
        request to clear the policy — and clearing is a *loosening* action, so
        the failure direction of guessing wrong here is the unsafe one. It
        passes `is defined`, hence the explicit `is not none`."""
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "outbound_approval": None},
        )
        assert "--outbound-approval" not in rendered

    def test_user_ensure_threads_external_turn_display(self):
        rendered = _render(
            _ensure_profiles_command(),
            {"display_name": "Alice", "external_turn_display": "hidden"},
        )
        assert '--external-turn-display "hidden"' in rendered

    def test_an_absent_external_turn_display_passes_nothing(self):
        rendered = _render(
            _ensure_profiles_command(), {"display_name": "Alice"},
        )
        assert "--external-turn-display" not in rendered

    @pytest.mark.parametrize(
        "flag,value",
        [("--outbound-approval", "off"), ("--external-turn-display", "full")],
    )
    def test_the_cli_parser_accepts_the_flags_the_role_renders(
        self, flag, value, monkeypatch, tmp_path,
    ):
        """The role and the parser live in different files, and a rendered flag
        argparse does not know fails the play at deploy time, inside a looped
        task whose per-user output is easy to skim past.

        `-c` is not optional here even though the handler is stubbed: `main()`
        loads the config before dispatching, and `load_config(None)` searches
        `~/.config/istota/config.toml` — a real file on a developer machine —
        then runs the obsolete-resource migration, which writes to whatever DB
        it finds. Every other test in this file pins the config for the same
        reason.
        """
        import istota.cli as cli

        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{tmp_path / "test.db"}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
        )

        seen = {}
        monkeypatch.setattr(cli, "cmd_user_ensure", lambda args: seen.update(vars(args)))
        monkeypatch.setattr(
            "sys.argv",
            ["istota", "-c", str(cfg), "user", "ensure", "--name", "alice", flag, value],
        )
        cli.main()

        assert seen[flag.lstrip("-").replace("-", "_")] == value




class TestNothingChownsToTheUserBeforeItExists:
    """The user must exist before any task hands it a file (ISSUE-439).

    `user:` with `create_home: no` creates `istota` about 870 lines into the
    play, and two Claude-credential tasks used to sit at position eight and
    chown `{{ istota_home }}/.claude` to it. A first install with
    `claude_oauth_token` set — the documented happy path, since `wizard.sh` asks
    for it — died there on `chown failed: failed to look up user istota` before
    the play had installed a single package.

    It was invisible to every other test in this directory because all of them
    read one task at a time, and each of these two is correct in isolation. It
    was invisible in practice because a re-run gets past it (the user exists by
    then) and because a host converged once without a token never sees it.
    Nothing in the repository executed `ansible-playbook` until the `deploy`
    tier did, and this is the first thing it found.

    Held in the default suite rather than only by that tier, because the tier
    is discretionary and nothing runs it automatically.

    **Two things make the walk non-trivial, and the first version of this guard
    had neither.** It read only top-level tasks and only short module names, so
    it could see 85 of the play's 282 tasks: 46 live inside `block:` /
    `rescue:` / `always:` — six such blocks sit before the user is created —
    and 32 more spell their module `ansible.builtin.file` rather than `file`.
    A task with either shape was invisible, and the positive control could not
    reveal that, because it planted the one shape that already worked. There
    are now two controls, one per blind spot.
    """

    #: Where a file's owner comes from. `owner:`/`group:` on a file-writing
    #: module is the direct spelling; a task that only names a *path* under the
    #: home is not asserted about, since a path is not a chown.
    OWNER_KEYS = ("owner", "group")

    #: Matched after the collection prefix is stripped, so `file` and
    #: `ansible.builtin.file` are one entry.
    MODULES = ("file", "copy", "template", "lineinfile", "blockinfile", "unarchive")

    #: The collections this role draws on. `install.sh`'s `ensure_collections`
    #: installs `community.general` and `ansible.posix`; `ansible.builtin` is
    #: always available.
    COLLECTION_PREFIXES = ("ansible.builtin.", "ansible.posix.", "community.general.")

    @classmethod
    def _module_name(cls, key: str) -> str:
        for prefix in cls.COLLECTION_PREFIXES:
            if key.startswith(prefix):
                return key[len(prefix):]
        return key

    @classmethod
    def _flatten(cls, tasks: list) -> list[dict]:
        """Pre-order walk, so the index is the order Ansible would run them in.

        A `block:` executes its children in place, so a flat list of top-level
        entries is not the play — it is the play with 46 tasks missing.
        """
        out: list[dict] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            out.append(task)
            for section in ("block", "rescue", "always"):
                children = task.get(section)
                if isinstance(children, list):
                    out.extend(cls._flatten(children))
        return out

    @classmethod
    def _tasks(cls) -> list[dict]:
        return cls._flatten(yaml.safe_load(TASKS_FILE.read_text()))

    def _index_of(self, name: str, tasks: list[dict] | None = None) -> int:
        for index, task in enumerate(tasks if tasks is not None else self._tasks()):
            if task.get("name") == name:
                return index
        raise AssertionError(f"task {name!r} not found in tasks/main.yml")

    @classmethod
    def _chowners_before(cls, limit: int, tasks: list[dict]) -> list[tuple]:
        found = []
        for index, task in enumerate(tasks):
            if index >= limit:
                continue
            for key, args in task.items():
                if cls._module_name(key) not in cls.MODULES:
                    continue
                if not isinstance(args, dict):
                    continue
                for owner_key in cls.OWNER_KEYS:
                    value = args.get(owner_key)
                    if isinstance(value, str) and (
                        "istota_user" in value or "istota_group" in value
                    ):
                        found.append((index, task.get("name"), key, owner_key))
        return found

    def test_the_walk_reaches_the_whole_play(self):
        """The guard's own coverage, asserted rather than assumed.

        Everything below is a search that reports an empty list when it is
        healthy, which is also what it reports when it is looking in the wrong
        place. This is the floor: the flattened walk has to be meaningfully
        larger than the top-level list, or the recursion has silently stopped
        working and every assertion here goes quietly vacuous.
        """
        top_level = [
            t for t in yaml.safe_load(TASKS_FILE.read_text()) if isinstance(t, dict)
        ]
        flattened = self._tasks()
        assert len(flattened) > len(top_level), (
            "the flattened walk found no tasks inside blocks, so either the "
            "role stopped using them or the recursion is broken"
        )

    def test_the_user_is_created_before_anything_is_given_to_it(self):
        tasks = self._tasks()
        creation = self._index_of("Create istota system user", tasks)
        offenders = self._chowners_before(creation, tasks)
        assert not offenders, (
            "these tasks set owner/group to the istota user or group before "
            f"'Create istota system user' (index {creation}), so a first "
            "install fails with 'failed to look up user istota':\n"
            + "\n".join(
                f"  [{i}] {name!r} ({module}.{key})"
                for i, name, module, key in offenders
            )
        )

    def test_the_home_directory_exists_before_a_subdirectory_of_it_is_made(self):
        """`create_home: no` means the `user:` task does not make the home.

        Moving the Claude tasks to just after `user:` would still have been
        wrong: `{{ istota_home }}/.claude` at mode 0700 needs `{{ istota_home }}`
        to be there and owned first, and the directory loop is what does that.
        """
        assert self._index_of("Create istota directories") < self._index_of(
            "Create Claude config directory"
        )

    def test_the_guard_can_fail_on_a_plain_task(self):
        """Positive control one: the shape the original bug had.

        A short module name on a top-level task. This is the path the first
        version of the guard could see, and it is still the common one.
        """
        tasks = self._tasks()
        creation = self._index_of("Create istota system user", tasks)
        tasks.insert(
            0,
            {
                "name": "A task nobody reordered",
                "file": {
                    "path": "{{ istota_home }}/.claude",
                    "owner": "{{ istota_user }}",
                },
            },
        )
        offenders = self._chowners_before(creation + 1, tasks)
        assert [name for _, name, _, _ in offenders] == ["A task nobody reordered"]

    def test_the_guard_can_fail_on_an_fqcn_task_inside_a_block(self):
        """Positive control two: the shape the first version could not see.

        Both blind spots at once — a collection-qualified module key, on a task
        nested inside a `block:`. Six blocks sit before the user is created and
        32 tasks in this file already use an FQCN, so this is the live shape
        rather than a hypothetical one; without the recursion and the prefix
        strip it plants a defect the guard reports as clean.
        """
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        tasks.insert(
            0,
            {
                "name": "A block nobody walked into",
                "block": [
                    {
                        "name": "An FQCN task nobody reordered",
                        "ansible.builtin.copy": {
                            "dest": "{{ istota_home }}/.claude/.credentials.json",
                            "owner": "{{ istota_user }}",
                        },
                    }
                ],
            },
        )
        flattened = self._flatten(tasks)
        creation = self._index_of("Create istota system user", flattened)
        offenders = self._chowners_before(creation, flattened)
        assert [name for _, name, _, _ in offenders] == [
            "An FQCN task nobody reordered"
        ]
