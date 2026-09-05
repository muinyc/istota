"""Configuration loading for istota.config module."""

import logging
from pathlib import Path

import pytest

from istota.config import (
    devbox_container_backend,
    BriefingConfig,
    ChannelSleepCycleConfig,
    Config,
    DeveloperConfig,
    EmailConfig,
    MemorySearchConfig,
    NextcloudConfig,
    ResourceConfig,
    SiteConfig,
    SleepCycleConfig,
    UserConfig,
    load_config,
)


class TestConfigDefaults:
    def test_default_db_path(self):
        cfg = Config()
        assert cfg.db_path == Path("data/istota.db")

    def test_default_rclone_remote(self):
        cfg = Config()
        assert cfg.rclone_remote == "nextcloud"

    def test_default_nextcloud_config(self):
        cfg = Config()
        assert cfg.nextcloud.url == ""
        assert cfg.nextcloud.username == ""
        assert cfg.nextcloud.app_password == ""

    def test_default_talk_config(self):
        cfg = Config()
        assert cfg.talk.enabled is True
        assert cfg.talk.bot_username == "istota"

    def test_default_email_config(self):
        cfg = Config()
        assert cfg.email.enabled is False
        assert cfg.email.imap_host == ""
        assert cfg.email.imap_port == 993
        assert cfg.email.smtp_port == 587
        assert cfg.email.poll_folder == "INBOX"

    def test_default_conversation_config(self):
        cfg = Config()
        assert cfg.conversation.enabled is True
        assert cfg.conversation.lookback_count == 25
        assert cfg.conversation.selection_timeout == 30.0
        assert cfg.conversation.skip_selection_threshold == 3

    def test_default_scheduler_config(self):
        cfg = Config()
        # 5, not 2. The dataclass said 2 and the loader's own `.get()` said 5,
        # so the value depended on whether a `[scheduler]` header was present.
        # Every generator, the example config and `istota setup` write 5, so
        # that is what deployments actually run and what the single default is
        # now. See tests/test_config_mapper.py::TestOneDefaultPerField.
        assert cfg.scheduler.poll_interval == 5
        assert cfg.scheduler.dispatch_interval == 0.5
        assert cfg.scheduler.email_poll_interval == 60
        assert cfg.scheduler.talk_poll_interval == 10
        assert cfg.scheduler.talk_poll_timeout == 30
        assert cfg.scheduler.progress_updates is True
        assert cfg.scheduler.task_timeout_minutes == 30
        assert cfg.scheduler.task_retention_days == 7
        assert cfg.scheduler.worker_idle_timeout == 10
        assert cfg.scheduler.worker_idle_poll_interval == 0.5

    def test_default_logging_config(self):
        cfg = Config()
        assert cfg.logging.level == "INFO"
        assert cfg.logging.output == "console"
        assert cfg.logging.file == ""
        assert cfg.logging.rotate is True
        assert cfg.logging.max_size_mb == 10
        assert cfg.logging.backup_count == 5

    def test_default_no_users(self):
        cfg = Config()
        assert cfg.users == {}

    def test_use_mount_false_by_default(self):
        cfg = Config()
        assert cfg.nextcloud_mount_path is None
        assert cfg.use_mount is False

    def test_default_bot_name(self):
        cfg = Config()
        assert cfg.bot_name == "Istota"
        assert cfg.bot_dir_name == "istota"

    def test_bot_dir_name_with_spaces(self):
        cfg = Config(bot_name="Mister Jones")
        assert cfg.bot_dir_name == "mister_jones"

    def test_bot_dir_name_with_special_chars(self):
        cfg = Config(bot_name="My Bot!")
        assert cfg.bot_dir_name == "my_bot"

    def test_bot_dir_name_fallback(self):
        cfg = Config(bot_name="!!!")
        assert cfg.bot_dir_name == "istota"

    def test_bot_dir_name_strips_unicode(self):
        cfg = Config(bot_name="Café Bot")
        assert cfg.bot_dir_name == "caf_bot"

    def test_bot_dir_name_preserves_hyphens(self):
        cfg = Config(bot_name="My-Bot 2")
        assert cfg.bot_dir_name == "my-bot_2"

    def test_default_custom_system_prompt(self):
        cfg = Config()
        assert cfg.custom_system_prompt is False


class TestConfigLoading:
    def test_load_missing_file_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert cfg.db_path == Path("data/istota.db")
        assert cfg.users == {}

    def test_load_minimal_config(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('db_path = "mydb.sqlite"\n')
        cfg = load_config(p)
        assert cfg.db_path == Path("mydb.sqlite")

    def test_load_module_data_dir_and_backup_knobs(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            'module_data_dir = "/srv/local/modules"\n\n'
            "[scheduler]\n"
            "main_loop_read_timeout_ms = 1500\n"
            "db_backup_enabled = false\n"
            "db_backup_interval = 43200\n"
            'db_backup_dir = "/srv/backups"\n'
        )
        cfg = load_config(p)
        assert cfg.module_data_dir == Path("/srv/local/modules")
        assert cfg.scheduler.main_loop_read_timeout_ms == 1500
        assert cfg.scheduler.db_backup_enabled is False
        assert cfg.scheduler.db_backup_interval == 43200
        assert cfg.scheduler.db_backup_dir == "/srv/backups"

    def test_module_data_dir_defaults_none(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('db_path = "d.sqlite"\n')
        cfg = load_config(p)
        assert cfg.module_data_dir is None
        # derives alongside the framework DB
        assert cfg.module_db_path("alice", "feeds") == (
            Path("d.sqlite").resolve().parent / "modules" / "alice" / "feeds.db"
        )

    def test_stale_skills_block_warns_not_fails(self, tmp_path, caplog):
        # The [skills] section is obsolete (single-axis selection has no knobs).
        # A stale block must keep loading, with a warning — never raise.
        import logging

        p = tmp_path / "config.toml"
        p.write_text(
            'db_path = "test.db"\n\n'
            "[skills]\n"
            "progressive_disclosure = false\n"
            "auto_lazy_threshold_chars = 4000\n"
        )
        with caplog.at_level(logging.WARNING):
            cfg = load_config(p)
        assert cfg.db_path == Path("test.db")
        assert not hasattr(cfg, "skills")
        assert any("[skills] block" in r.message for r in caplog.records)

    def test_load_custom_system_prompt_true(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('custom_system_prompt = true\n')
        cfg = load_config(p)
        assert cfg.custom_system_prompt is True

    def test_load_custom_system_prompt_default(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('db_path = "test.db"\n')
        cfg = load_config(p)
        assert cfg.custom_system_prompt is False

    def test_load_nextcloud_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[nextcloud]\n'
            'url = "https://cloud.example.com"\n'
            'username = "bot"\n'
            'app_password = "secret123"\n'
        )
        cfg = load_config(p)
        assert cfg.nextcloud.url == "https://cloud.example.com"
        assert cfg.nextcloud.username == "bot"
        assert cfg.nextcloud.app_password == "secret123"

    def test_load_talk_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[talk]\n'
            'enabled = false\n'
            'bot_username = "mybot"\n'
        )
        cfg = load_config(p)
        assert cfg.talk.enabled is False
        assert cfg.talk.bot_username == "mybot"

    def test_load_email_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[email]\n'
            'enabled = true\n'
            'imap_host = "imap.example.com"\n'
            'imap_port = 993\n'
            'imap_user = "user@example.com"\n'
            'imap_password = "pass"\n'
            'smtp_host = "smtp.example.com"\n'
            'smtp_port = 465\n'
            'smtp_user = "smtpuser"\n'
            'smtp_password = "smtppass"\n'
            'poll_folder = "INBOX"\n'
            'bot_email = "bot@example.com"\n'
        )
        cfg = load_config(p)
        assert cfg.email.enabled is True
        assert cfg.email.imap_host == "imap.example.com"
        assert cfg.email.smtp_host == "smtp.example.com"
        assert cfg.email.smtp_port == 465
        assert cfg.email.smtp_user == "smtpuser"
        assert cfg.email.smtp_password == "smtppass"
        assert cfg.email.bot_email == "bot@example.com"

    def test_load_conversation_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[conversation]\n'
            'enabled = false\n'
            'lookback_count = 20\n'
            'selection_timeout = 15.0\n'
            'skip_selection_threshold = 5\n'
        )
        cfg = load_config(p)
        assert cfg.conversation.enabled is False
        assert cfg.conversation.lookback_count == 20
        assert cfg.conversation.selection_timeout == 15.0
        assert cfg.conversation.skip_selection_threshold == 5

    def test_load_scheduler_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'poll_interval = 10\n'
            'dispatch_interval = 0.25\n'
            'email_poll_interval = 120\n'
            'talk_poll_interval = 5\n'
            'progress_updates = false\n'
            'event_log_enabled = false\n'
            'task_timeout_minutes = 60\n'
            'confirmation_timeout_minutes = 60\n'
            'task_retention_days = 14\n'
            'email_retention_days = 30\n'
            'worker_idle_timeout = 20\n'
            'worker_idle_poll_interval = 1.5\n'
            'host_pressure_enabled = false\n'
            'host_pressure_breadcrumb_interval_seconds = 60\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.poll_interval == 10
        assert cfg.scheduler.dispatch_interval == 0.25
        assert cfg.scheduler.email_poll_interval == 120
        assert cfg.scheduler.talk_poll_interval == 5
        assert cfg.scheduler.progress_updates is False
        assert cfg.scheduler.event_log_enabled is False
        assert cfg.scheduler.task_timeout_minutes == 60
        assert cfg.scheduler.confirmation_timeout_minutes == 60
        assert cfg.scheduler.task_retention_days == 14
        assert cfg.scheduler.email_retention_days == 30
        assert cfg.scheduler.worker_idle_timeout == 20
        assert cfg.scheduler.worker_idle_poll_interval == 1.5
        assert cfg.scheduler.host_pressure_enabled is False
        assert cfg.scheduler.host_pressure_breadcrumb_interval_seconds == 60

    def test_load_logging_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[logging]\n'
            'level = "DEBUG"\n'
            'output = "both"\n'
            'file = "/var/log/istota.log"\n'
            'rotate = false\n'
            'max_size_mb = 50\n'
            'backup_count = 10\n'
        )
        cfg = load_config(p)
        assert cfg.logging.level == "DEBUG"
        assert cfg.logging.output == "both"
        assert cfg.logging.file == "/var/log/istota.log"
        assert cfg.logging.rotate is False
        assert cfg.logging.max_size_mb == 50
        assert cfg.logging.backup_count == 10

    def test_briefing_defaults_section_ignored(self, tmp_path):
        # [briefing_defaults] is retired (retire-legacy-briefing-components).
        # A stale section in TOML no longer parses into any config field.
        from istota import config as config_mod

        p = tmp_path / "config.toml"
        p.write_text(
            '[briefing_defaults.markets]\n'
            'futures = ["ES=F", "NQ=F"]\n'
        )
        cfg = load_config(p)
        assert not hasattr(cfg, "briefing_defaults")
        assert not hasattr(config_mod, "BriefingDefaultsConfig")

    def test_moneyman_section_ignored(self, tmp_path):
        from istota import config as config_mod

        p = tmp_path / "config.toml"
        p.write_text(
            '[moneyman]\n'
            'api_url = "http://localhost:8090"\n'
        )
        cfg = load_config(p)
        assert not hasattr(cfg, "moneyman")
        assert not hasattr(config_mod, "MoneymanConfig")

    def test_parse_default_briefings_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[[default_briefings]]\n'
            'name = "Daily"\n'
            'cron = "0 7 * * *"\n'
            'output = "talk"\n'
            '\n'
            '[[default_briefings.blocks]]\n'
            'title = "News"\n'
            '\n'
            '[[default_briefings.blocks.sources]]\n'
            'kind = "rss"\n'
        )
        cfg = load_config(p)
        assert len(cfg.default_briefings) == 1
        d = cfg.default_briefings[0]
        assert d.name == "Daily"
        assert d.cron == "0 7 * * *"
        assert d.output == "talk"
        assert d.blocks[0]["title"] == "News"

    def test_default_briefings_seeded_into_user(self, tmp_path):
        db_file = tmp_path / "istota.db"
        from istota import db as _db
        _db.init_db(db_file)
        p = tmp_path / "config.toml"
        p.write_text(
            f'db_path = "{db_file}"\n'
            '\n'
            '[[default_briefings]]\n'
            'name = "Daily"\n'
            'cron = "0 7 * * *"\n'
            '\n'
            '[users.alice]\n'
            'display_name = "Alice"\n'
        )
        cfg = load_config(p)
        names = [b.name for b in cfg.users["alice"].briefings]
        assert "Daily" in names

    def test_toml_components_authoring_ignored(self, tmp_path):
        # Legacy `[[briefings]] [briefings.components]` authoring is dropped;
        # a stray components key is ignored (blocks-only content model).
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.alice]\n'
            'display_name = "Alice"\n'
            '\n'
            '[[users.alice.briefings]]\n'
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'output = "talk"\n'
            '\n'
            '[users.alice.briefings.components]\n'
            'calendar = true\n'
        )
        cfg = load_config(p)
        briefings = cfg.users["alice"].briefings
        assert len(briefings) == 1
        assert briefings[0].name == "morning"
        assert briefings[0].components == {}

    def test_load_users_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.alice]\n'
            'display_name = "Alice Smith"\n'
            'email_addresses = ["alice@example.com", "alice@work.com"]\n'
            'timezone = "America/New_York"\n'
        )
        cfg = load_config(p)
        assert "alice" in cfg.users
        alice = cfg.users["alice"]
        assert alice.display_name == "Alice Smith"
        assert alice.email_addresses == ["alice@example.com", "alice@work.com"]
        assert alice.timezone == "America/New_York"
        assert alice.briefings == []

    def test_load_users_reminders_file_backward_compat(self, tmp_path):
        """Legacy reminders_file string is auto-migrated to a resource."""
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.alice]\n'
            'display_name = "Alice"\n'
            'reminders_file = "/alice/REMINDERS.md"\n'
        )
        cfg = load_config(p)
        alice = cfg.users["alice"]
        reminder_resources = [r for r in alice.resources if r.type == "reminders_file"]
        assert len(reminder_resources) == 1
        assert reminder_resources[0].path == "/alice/REMINDERS.md"
        assert reminder_resources[0].name == "Reminders"

    def test_load_users_with_briefings(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.bob]\n'
            'display_name = "Bob"\n'
            'timezone = "Europe/Berlin"\n'
            '\n'
            '[[users.bob.briefings]]\n'
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "room1"\n'
            'output = "both"\n'
            '\n'
            '[users.bob.briefings.components]\n'
            'calendar = true\n'
            'todos = true\n'
            '\n'
            '[[users.bob.briefings]]\n'
            'name = "evening"\n'
            'cron = "0 18 * * *"\n'
        )
        cfg = load_config(p)
        bob = cfg.users["bob"]
        assert len(bob.briefings) == 2
        morning = bob.briefings[0]
        assert morning.name == "morning"
        assert morning.cron == "0 7 * * *"
        assert morning.conversation_token == "room1"
        assert morning.output == "both"
        # TOML component authoring is retired — the stray section is ignored.
        assert morning.components == {}
        evening = bob.briefings[1]
        assert evening.name == "evening"
        assert evening.cron == "0 18 * * *"
        assert evening.conversation_token == ""
        assert evening.output == "talk"
        assert evening.components == {}

    def test_load_mount_path(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('nextcloud_mount_path = "/srv/mount/nextcloud/content"\n')
        cfg = load_config(p)
        assert cfg.nextcloud_mount_path == Path("/srv/mount/nextcloud/content")

    def test_load_skills_dir(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('skills_dir = "/opt/istota/skills"\n')
        cfg = load_config(p)
        assert cfg.skills_dir == Path("/opt/istota/skills")

    def test_load_security_skill_proxy(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[security]\n'
            'skill_proxy_enabled = true\n'
            'skill_proxy_timeout = 120\n'
        )
        cfg = load_config(p)
        assert cfg.security.skill_proxy_enabled is True
        assert cfg.security.skill_proxy_timeout == 120

    def test_load_security_skill_proxy_defaults(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[security]\nsandbox_enabled = true\n')
        cfg = load_config(p)
        assert cfg.security.skill_proxy_enabled is True
        assert cfg.security.skill_proxy_timeout == 300


class TestTheSandboxWithoutTheProxyWarning:
    """`sandbox_enabled` with `skill_proxy_enabled = false` is the one pairing
    where the sandbox is a real boundary and every configured credential rides
    past it in the environment: `_split_credential_env` only removes them under
    the proxy branch. The warning has to say that, because it is the silent half
    — the masked databases fail loudly on their own."""

    def test_the_pairing_names_the_credential_consequence(self, tmp_path, caplog):
        p = tmp_path / "config.toml"
        p.write_text(
            "[security]\n"
            "sandbox_enabled = true\n"
            "skill_proxy_enabled = false\n"
        )
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(p)
        pairing = [
            r.getMessage() for r in caplog.records
            if "sandbox_enabled with skill_proxy_enabled = false" in r.getMessage()
        ]
        assert pairing, [r.getMessage() for r in caplog.records]
        assert any("credential" in m for m in pairing), pairing

    def test_the_pairing_still_names_the_masked_databases(self, tmp_path, caplog):
        """Widening must not drop what was already there: an operator whose
        skill CLIs have stopped working needs the functional half too."""
        p = tmp_path / "config.toml"
        p.write_text(
            "[security]\n"
            "sandbox_enabled = true\n"
            "skill_proxy_enabled = false\n"
        )
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(p)
        # Scoped to the pairing message rather than to every record: `caplog`
        # collects whatever else `load_config` warned about, so an unscoped
        # substring search would pass on somebody else's warning.
        pairing = [
            r.getMessage() for r in caplog.records
            if "sandbox_enabled with skill_proxy_enabled = false" in r.getMessage()
        ]
        assert pairing, [r.getMessage() for r in caplog.records]
        assert any("masked out" in m for m in pairing), pairing

    def test_no_warning_when_the_proxy_is_on(self, tmp_path, caplog):
        """Keyed on the pairing prefix rather than on `skill_proxy_enabled =
        false`, which `doctor.check_skill_proxy` also emits and
        `_validate_forge_clis` routes into this same logger. Paired with a
        control: a bare negative assertion passes just as well when nothing was
        captured at all."""
        p = tmp_path / "config.toml"
        p.write_text("[security]\nsandbox_enabled = true\n")
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(p)
        assert not any(
            "sandbox_enabled with skill_proxy_enabled = false" in r.getMessage()
            for r in caplog.records
        )
        # Control: the same capture does see the pairing warning when it fires.
        caplog.clear()
        q = tmp_path / "warns.toml"
        q.write_text(
            "[security]\n"
            "sandbox_enabled = true\n"
            "skill_proxy_enabled = false\n"
        )
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(q)
        assert any(
            "sandbox_enabled with skill_proxy_enabled = false" in r.getMessage()
            for r in caplog.records
        )

    def test_no_warning_when_both_are_off(self, tmp_path, caplog):
        """The single-user install writes both switches off together. There is
        no boundary there for a credential to cross, so this shape is a trust
        decision rather than a defect and must stay silent."""
        p = tmp_path / "config.toml"
        p.write_text(
            "[security]\n"
            "sandbox_enabled = false\n"
            "skill_proxy_enabled = false\n"
        )
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(p)
        assert not any(
            "sandbox_enabled with skill_proxy_enabled" in r.getMessage()
            for r in caplog.records
        )


class TestTheReadOnlyPathThatWouldExposeTheControlTree:
    """`sandbox_ro_paths` is bound verbatim, and one broad entry has already
    cost this project every database once.

    `sandbox_ro_paths = ["/srv/app"]` was the shipped default, `db_path` and
    `module_data_dir` lived under it, and one read-only bind that mentioned no
    database exposed the framework DB, every module DB and the local backups.
    The masks were the fix for that. The task control tree has no mask behind
    it and cannot have one — the model has to be able to *read* its own
    directory — so the only thing standing between a broad entry and every
    task of every user's assembled prompt is that nobody writes one.

    An entry at or above `{temp_dir}/.control` binds the whole tree; an entry
    inside it binds part of one. Both are warned about, and only where a
    sandbox is asked for, since with no sandbox nothing is bound at all.
    """

    _MARKER = "would bind the task control tree"

    @pytest.fixture(autouse=True)
    def _clear_the_latch(self):
        """The warning is said once per process per entry, so a test that did
        not clear it would depend on whether an earlier one had already used
        the same path — and under xdist, on which worker it landed."""
        from istota.config import _RO_PATH_CONTROL_TREE_WARNED

        _RO_PATH_CONTROL_TREE_WARNED.clear()
        yield
        _RO_PATH_CONTROL_TREE_WARNED.clear()

    def _warnings(self, path, caplog):
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(path)
        return [
            r.getMessage() for r in caplog.records if self._MARKER in r.getMessage()
        ]

    def test_an_entry_above_the_control_root_is_named(self, tmp_path, caplog):
        p = tmp_path / "config.toml"
        p.write_text(
            f'temp_dir = "{tmp_path}/tmp"\n'
            "[security]\n"
            "sandbox_enabled = true\n"
            f'sandbox_ro_paths = ["{tmp_path}/tmp"]\n'
            "\n"
        )
        hits = self._warnings(p, caplog)
        assert hits, [r.getMessage() for r in caplog.records]
        # The entry as written, so an operator can find the line to delete.
        assert any(f"{tmp_path}/tmp" in m for m in hits), hits

    def test_an_entry_inside_the_control_root_is_named(self, tmp_path, caplog):
        """One user's tree rather than every user's, and still every task of
        theirs. Warned about for the same reason, and it is the shape a
        well-meaning "let the model read its own control dir" edit takes."""
        p = tmp_path / "config.toml"
        p.write_text(
            f'temp_dir = "{tmp_path}/tmp"\n'
            "[security]\n"
            "sandbox_enabled = true\n"
            f'sandbox_ro_paths = ["{tmp_path}/tmp/.control/alice"]\n'
            "\n"
        )
        assert self._warnings(p, caplog), [r.getMessage() for r in caplog.records]

    def test_a_narrow_entry_is_silent(self, tmp_path, caplog):
        """The control: a path beside the temp root is what the setting is
        for, and warning on it would train an operator to ignore the message.
        """
        p = tmp_path / "config.toml"
        p.write_text(
            f'temp_dir = "{tmp_path}/tmp"\n'
            "[security]\n"
            "sandbox_enabled = true\n"
            f'sandbox_ro_paths = ["{tmp_path}/srv/app"]\n'
            "\n"
        )
        assert not self._warnings(p, caplog)
        # Control: the same capture does see it when the entry is broad.
        caplog.clear()
        q = tmp_path / "broad.toml"
        q.write_text(
            f'temp_dir = "{tmp_path}/tmp"\n'
            "[security]\n"
            "sandbox_enabled = true\n"
            f'sandbox_ro_paths = ["{tmp_path}/tmp"]\n'
            "\n"
        )
        assert self._warnings(q, caplog)

    def test_no_sandbox_no_warning(self, tmp_path, caplog):
        """`build_bwrap_cmd` hands the command back unwrapped with the sandbox
        off, so the entry binds nothing and there is no boundary for it to
        widen. Same reasoning the credential pairing above uses, and the same
        *requested* flag: the effective one costs a subprocess and this path
        runs on every CLI invocation."""
        p = tmp_path / "config.toml"
        p.write_text(
            f'temp_dir = "{tmp_path}/tmp"\n'
            "[security]\n"
            "sandbox_enabled = false\n"
            f'sandbox_ro_paths = ["{tmp_path}/tmp"]\n'
            "\n"
        )
        assert not self._warnings(p, caplog)


    def test_it_is_said_once_per_process_not_once_per_load(self, tmp_path, caplog):
        """`load_config` runs on every host-side skill CLI the proxy spawns,
        which is once per model tool call. A multi-line warning on each of
        those is noise on a path the model reads, so the notice latches the way
        `_validate_brain_fallback`'s does."""
        p = tmp_path / "config.toml"
        p.write_text(
            f'temp_dir = "{tmp_path}/tmp"\n'
            "[security]\n"
            "sandbox_enabled = true\n"
            f'sandbox_ro_paths = ["{tmp_path}/tmp"]\n'
        )
        assert self._warnings(p, caplog)
        caplog.clear()
        assert not self._warnings(p, caplog)

    def test_a_relative_temp_dir_is_skipped_rather_than_guessed_at(
        self, tmp_path, caplog,
    ):
        """`Path.resolve()` on a relative value answers against the calling
        process's cwd, which differs between the daemon, the web app and a
        skill CLI — so the same config would warn in one and not another."""
        p = tmp_path / "config.toml"
        p.write_text(
            'temp_dir = "relative/tmp"\n'
            "[security]\n"
            "sandbox_enabled = true\n"
            'sandbox_ro_paths = ["relative"]\n'
        )
        assert not self._warnings(p, caplog)


class TestConfigMethods:
    def test_find_user_by_email_found(self):
        cfg = Config(users={
            "alice": UserConfig(email_addresses=["alice@example.com"]),
        })
        assert cfg.find_user_by_email("alice@example.com") == "alice"

    def test_find_user_by_email_case_insensitive(self):
        cfg = Config(users={
            "alice": UserConfig(email_addresses=["Alice@Example.COM"]),
        })
        assert cfg.find_user_by_email("alice@example.com") == "alice"

    def test_find_user_by_email_not_found(self):
        cfg = Config(users={
            "alice": UserConfig(email_addresses=["alice@example.com"]),
        })
        assert cfg.find_user_by_email("bob@example.com") is None

    def test_caldav_url(self):
        cfg = Config(nextcloud=NextcloudConfig(url="https://cloud.example.com"))
        assert cfg.caldav_url == "https://cloud.example.com/remote.php/dav"

    def test_caldav_url_empty(self):
        cfg = Config()
        assert cfg.caldav_url == ""

    def test_get_user_found(self):
        user = UserConfig(display_name="Alice")
        cfg = Config(users={"alice": user})
        assert cfg.get_user("alice") is user

    def test_get_user_not_found(self):
        cfg = Config()
        assert cfg.get_user("nobody") is None

    def test_use_mount_true(self):
        cfg = Config(nextcloud_mount_path=Path("/mnt/nc"))
        assert cfg.use_mount is True


class TestResolveUserTimezone:
    """`Config.resolve_user_timezone` is the single source of truth for a
    user's timezone, preferring the live ``user_profiles`` DB row over the
    in-memory ``UserConfig`` so web-UI edits take effect without a scheduler
    restart (ISSUE-099). Every timezone reader (prompt header, briefings,
    scheduled jobs, Garmin sync, subprocess env) routes through it.
    """

    def _make_config(self, tmp_path, *, user_tz="America/Los_Angeles"):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        return Config(
            db_path=db_path,
            users={"alice": UserConfig(timezone=user_tz)},
        )

    def test_db_row_wins_over_stale_in_memory_config(self, tmp_path):
        from istota import user_profiles
        cfg = self._make_config(tmp_path, user_tz="America/Los_Angeles")
        user_profiles.ensure_profile(cfg.db_path, "alice", timezone="Europe/Lisbon")
        assert cfg.resolve_user_timezone("alice") == "Europe/Lisbon"

    def test_falls_back_to_in_memory_when_no_db_row(self, tmp_path):
        cfg = self._make_config(tmp_path, user_tz="America/New_York")
        assert cfg.resolve_user_timezone("alice") == "America/New_York"

    def test_unknown_user_returns_utc(self, tmp_path):
        cfg = self._make_config(tmp_path)
        assert cfg.resolve_user_timezone("nobody") == "UTC"

    def test_no_db_path_uses_in_memory(self, tmp_path):
        cfg = Config(db_path=None, users={"alice": UserConfig(timezone="Asia/Tokyo")})
        assert cfg.resolve_user_timezone("alice") == "Asia/Tokyo"

    def test_does_not_validate_zone_name(self, tmp_path):
        # The Config helper returns the raw string; ZoneInfo validation is
        # the caller's job (so callers control the invalid-zone fallback).
        from istota import user_profiles
        cfg = self._make_config(tmp_path)
        user_profiles.ensure_profile(cfg.db_path, "alice", timezone="Not/AZone")
        assert cfg.resolve_user_timezone("alice") == "Not/AZone"

    def test_accepts_reused_connection(self, tmp_path):
        from istota import db, user_profiles
        cfg = self._make_config(tmp_path, user_tz="America/Los_Angeles")
        user_profiles.ensure_profile(cfg.db_path, "alice", timezone="Europe/Lisbon")
        with db.get_db(cfg.db_path) as conn:
            assert cfg.resolve_user_timezone("alice", conn=conn) == "Europe/Lisbon"


class TestEmailReplyRouting:
    def test_default_when_unset(self):
        cfg = Config(users={"carol": UserConfig()})
        assert cfg.email_reply_routing_for("carol") == "origin+thread"

    def test_default_for_unknown_user(self):
        cfg = Config()
        assert cfg.email_reply_routing_for("nobody") == "origin+thread"

    def test_valid_values_pass_through(self):
        for val in ("origin+thread", "origin", "thread"):
            cfg = Config(users={"carol": UserConfig(email_reply_routing=val)})
            assert cfg.email_reply_routing_for("carol") == val

    def test_invalid_value_falls_back(self):
        cfg = Config(users={"carol": UserConfig(email_reply_routing="bogus")})
        assert cfg.email_reply_routing_for("carol") == "origin+thread"


class TestTrustedEmailSenders:
    def test_own_email_always_trusted(self):
        cfg = Config(users={
            "carol": UserConfig(email_addresses=["alice@example.com"]),
        })
        assert cfg.is_trusted_email_sender("carol", "alice@example.com") is True

    def test_own_email_case_insensitive(self):
        cfg = Config(users={
            "carol": UserConfig(email_addresses=["Alice@Example.COM"]),
        })
        assert cfg.is_trusted_email_sender("carol", "alice@example.com") is True

    def test_exact_match(self):
        cfg = Config(users={
            "carol": UserConfig(trusted_email_senders=["alice@example.com"]),
        })
        assert cfg.is_trusted_email_sender("carol", "Alice@Example.com") is True

    def test_domain_wildcard(self):
        cfg = Config(users={
            "carol": UserConfig(trusted_email_senders=["*@corp.com"]),
        })
        assert cfg.is_trusted_email_sender("carol", "anyone@corp.com") is True
        assert cfg.is_trusted_email_sender("carol", "anyone@sub.corp.com") is False

    def test_subdomain_wildcard(self):
        cfg = Config(users={
            "carol": UserConfig(trusted_email_senders=["*@*.corp.com"]),
        })
        assert cfg.is_trusted_email_sender("carol", "x@sub.corp.com") is True
        assert cfg.is_trusted_email_sender("carol", "x@corp.com") is False

    def test_no_match_returns_false(self):
        cfg = Config(users={
            "carol": UserConfig(trusted_email_senders=[]),
        })
        assert cfg.is_trusted_email_sender("carol", "stranger@evil.com") is False

    def test_unknown_user_returns_false(self):
        cfg = Config(users={})
        assert cfg.is_trusted_email_sender("nobody", "a@b.com") is False

    def test_multiple_patterns(self):
        cfg = Config(users={
            "carol": UserConfig(trusted_email_senders=[
                "alice@example.com",
                "*@example.org",
            ]),
        })
        assert cfg.is_trusted_email_sender("carol", "alice@example.com") is True
        assert cfg.is_trusted_email_sender("carol", "bob@example.org") is True
        assert cfg.is_trusted_email_sender("carol", "bob@evil.com") is False

    def test_alerts_channel_default_empty(self):
        uc = UserConfig()
        assert uc.alerts_channel == ""

    def test_trusted_email_senders_default_empty(self):
        uc = UserConfig()
        assert uc.trusted_email_senders == []

    def test_db_trusted_sender_checked_with_conn(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        cfg = Config(users={
            "carol": UserConfig(trusted_email_senders=[]),
        })

        with db.get_db(db_path) as conn:
            # Not trusted without DB entry
            assert cfg.is_trusted_email_sender("carol", "joe@example.com", conn) is False

            # Add to DB
            db.add_trusted_sender(conn, "carol", "joe@example.com")
            assert cfg.is_trusted_email_sender("carol", "joe@example.com", conn) is True

    def test_db_trusted_sender_not_checked_without_conn(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        cfg = Config(users={
            "carol": UserConfig(trusted_email_senders=[]),
        })

        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "carol", "joe@example.com")

        # Without conn, DB is not checked (backward compat)
        assert cfg.is_trusted_email_sender("carol", "joe@example.com") is False


class TestTrustedEmailSendersExcludingOwnAddresses:
    """``include_own_addresses=False`` — the caller wants trust that is evidence
    of something beyond the (unauthenticated) ``From:`` claim itself. Used by the
    sender-match confirmation gate, which would otherwise be circular."""

    def test_own_email_not_trusted_when_excluded(self):
        cfg = Config(users={
            "carol": UserConfig(email_addresses=["alice@example.com"]),
        })
        assert cfg.is_trusted_email_sender(
            "carol", "alice@example.com", include_own_addresses=False,
        ) is False

    def test_own_email_case_insensitive_when_excluded(self):
        cfg = Config(users={
            "carol": UserConfig(email_addresses=["Alice@Example.COM"]),
        })
        assert cfg.is_trusted_email_sender(
            "carol", "alice@example.com", include_own_addresses=False,
        ) is False

    def test_config_pattern_still_trusted_when_own_excluded(self):
        cfg = Config(users={
            "carol": UserConfig(
                email_addresses=["alice@example.com"],
                trusted_email_senders=["alice@example.com"],
            ),
        })
        assert cfg.is_trusted_email_sender(
            "carol", "alice@example.com", include_own_addresses=False,
        ) is True

    def test_db_trust_still_honored_when_own_excluded(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        cfg = Config(users={
            "carol": UserConfig(email_addresses=["alice@example.com"]),
        })

        with db.get_db(db_path) as conn:
            assert cfg.is_trusted_email_sender(
                "carol", "alice@example.com", conn, include_own_addresses=False,
            ) is False

            db.add_trusted_sender(conn, "carol", "alice@example.com")
            assert cfg.is_trusted_email_sender(
                "carol", "alice@example.com", conn, include_own_addresses=False,
            ) is True

    def test_unknown_user_returns_false_when_own_excluded(self):
        cfg = Config(users={})
        assert cfg.is_trusted_email_sender(
            "nobody", "a@b.com", include_own_addresses=False,
        ) is False

    def test_default_still_includes_own_addresses(self):
        cfg = Config(users={
            "carol": UserConfig(email_addresses=["alice@example.com"]),
        })
        assert cfg.is_trusted_email_sender("carol", "alice@example.com") is True


class TestEmailConfig:
    def test_effective_smtp_user_fallback(self):
        ec = EmailConfig(imap_user="imap@example.com", smtp_user="")
        assert ec.effective_smtp_user == "imap@example.com"

    def test_effective_smtp_password_fallback(self):
        ec = EmailConfig(imap_password="imappass", smtp_password="")
        assert ec.effective_smtp_password == "imappass"

    def test_effective_smtp_user_explicit(self):
        ec = EmailConfig(imap_user="imap@example.com", smtp_user="smtp@example.com")
        assert ec.effective_smtp_user == "smtp@example.com"

    def test_confirm_sender_match_defaults_off(self):
        """Opt-in (ISSUE-227): the gate was dead code until now, so `off` is the
        behaviour every existing deployment already has. Defaulting it on would
        start holding every self-sent email as a side effect of a bug fix."""
        assert EmailConfig().confirm_sender_match == "off"

    def test_confirm_sender_match_loads_the_legacy_true_as_gate(self, tmp_path):
        """ISSUE-249 turned this key from a bool into a three-state policy. Both
        deploy paths still render a boolean, so `true` has to keep meaning exactly
        what it meant — hold every self-addressed message."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
confirm_sender_match = true
""")
        cfg = load_config(config_file)
        assert cfg.email.confirm_sender_match == "gate"

    def test_confirm_sender_match_loads_the_legacy_false_as_off(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
confirm_sender_match = false
""")
        cfg = load_config(config_file)
        assert cfg.email.confirm_sender_match == "off"

    def test_confirm_sender_match_accepts_a_boolean_rendered_as_a_string(self, tmp_path):
        """Ansible renders a YAML boolean into a quoted template slot as "False",
        so the string forms have to load as the booleans they came from or a
        deployment breaks on a value it did not choose."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
confirm_sender_match = "False"
""")
        cfg = load_config(config_file)
        assert cfg.email.confirm_sender_match == "off"

    def test_confirm_sender_match_omitted_from_toml_defaults_off(self, tmp_path):
        """Every deployment gets its value through load_config, not EmailConfig(),
        so the loader's own fallback is what the default actually means."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
imap_host = "imap.example.com"
""")
        cfg = load_config(config_file)
        assert cfg.email.confirm_sender_match == "off"

    def test_confirm_sender_match_verify_requires_an_authserv_id(self, tmp_path):
        """ISSUE-249 Gap 3. Unscoped, the verdict comes off whichever header
        arrived on top, which in the case the gate exists for is the sender's own
        — so `verify` without `authserv_id` gates on a value the sender writes.
        Refusing to load is how "requires" differs from "prefers"."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
confirm_sender_match = "verify"
""")
        with pytest.raises(ValueError, match="authserv_id"):
            load_config(config_file)

    def test_confirm_sender_match_verify_loads_with_an_authserv_id(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
confirm_sender_match = "verify"
authserv_id = "mx.example.com"
""")
        cfg = load_config(config_file)
        assert cfg.email.confirm_sender_match == "verify"

    def test_confirm_sender_match_rejects_an_unknown_policy(self, tmp_path):
        """A security control with a typo in it stops the process rather than
        picking a policy on the operator's behalf — same rule as
        outbound_approval_floor, and for the same reason: there is no neutral
        fallback, since `off` disables a gate that was asked for and `gate` holds
        every message on an instance that deliberately wrote `off`."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
confirm_sender_match = "verifty"
""")
        with pytest.raises(ValueError, match="confirm_sender_match"):
            load_config(config_file)

    def test_dmarc_canary_defaults_on(self):
        """ISSUE-228 — on by default: a deployment that never thinks to enable a
        drift detector is exactly the one that needs it."""
        assert EmailConfig().dmarc_canary is True

    def test_dmarc_canary_warn_on_missing_defaults_off(self):
        """Absence of a verdict is silent by default. A mail path that stamps no
        Authentication-Results at all would otherwise warn on every message."""
        assert EmailConfig().dmarc_canary_warn_on_missing is False

    def test_dmarc_canary_omitted_from_toml_keeps_both_defaults(self, tmp_path):
        """The loader fallback is what the default actually means in production."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
imap_host = "imap.example.com"
""")
        cfg = load_config(config_file)
        assert cfg.email.dmarc_canary is True
        assert cfg.email.dmarc_canary_warn_on_missing is False

    def test_dmarc_canary_loads_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
dmarc_canary = false
dmarc_canary_warn_on_missing = true
""")
        cfg = load_config(config_file)
        assert cfg.email.dmarc_canary is False
        assert cfg.email.dmarc_canary_warn_on_missing is True

    def test_authserv_id_defaults_blank(self):
        """ISSUE-249 — blank keeps the pre-existing topmost-only read, so an
        existing deployment sees no change until the operator names their MTA."""
        assert EmailConfig().authserv_id == ""

    def test_authserv_id_omitted_from_toml_keeps_the_default(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
imap_host = "imap.example.com"
""")
        cfg = load_config(config_file)
        assert cfg.email.authserv_id == ""

    def test_authserv_id_loads_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
authserv_id = "mx.example.com"
""")
        cfg = load_config(config_file)
        assert cfg.email.authserv_id == "mx.example.com"

    def test_authserv_id_with_a_version_number_warns(self, tmp_path, caplog):
        """RFC 8601 puts a version number directly after the authserv-id, and the
        operator is told to copy the value off a real header — so pasting
        `mx.example.com 1` is the plausible mistake. Nothing matches it and every
        message reads as unstamped, which is loud but says nothing about why."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
authserv_id = "mx.example.com 1"
""")
        with caplog.at_level("WARNING"):
            cfg = load_config(config_file)

        assert "authserv_id" in caplog.text
        # Warned, not corrected: an operator with a genuinely unusual id keeps it.
        assert cfg.email.authserv_id == "mx.example.com 1"

    def test_authserv_id_is_trimmed_without_warning(self, tmp_path, caplog):
        """Surrounding whitespace is a paste artefact, not a malformed id."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[email]
enabled = true
authserv_id = "  mx.example.com  "
""")
        with caplog.at_level("WARNING"):
            cfg = load_config(config_file)

        assert cfg.email.authserv_id == "mx.example.com"
        assert "authserv_id" not in caplog.text


class TestSleepCycleConfig:
    def test_defaults(self):
        sc = SleepCycleConfig()
        assert sc.enabled is True
        assert sc.cron == "0 2 * * *"
        assert sc.memory_retention_days == 0
        assert sc.lookback_hours == 24

    def test_config_default(self):
        cfg = Config()
        assert cfg.sleep_cycle.enabled is True

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[sleep_cycle]
enabled = true
cron = "0 3 * * *"
memory_retention_days = 60
lookback_hours = 36
""")
        cfg = load_config(config_file)
        assert cfg.sleep_cycle.enabled is True
        assert cfg.sleep_cycle.cron == "0 3 * * *"
        assert cfg.sleep_cycle.memory_retention_days == 60
        assert cfg.sleep_cycle.lookback_hours == 36

    def test_load_native_brain_overrides_and_compaction_knobs(self, tmp_path):
        # NB-4 / NB-14: [brain.native] model overrides + compaction sizing knobs.
        from istota.llm.catalog import get_model_info, set_model_overrides

        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[brain]
kind = "native"

[brain.native]
model = "qwen/qwen3-thinking"
compaction_reserve_tokens = 1000
compaction_keep_recent_tokens = 3000

[brain.native.model_overrides."qwen/qwen3-thinking"]
supports_thinking = true
context_window = 32000
""")
        try:
            cfg = load_config(config_file)
            assert cfg.brain.native.compaction_reserve_tokens == 1000
            assert cfg.brain.native.compaction_keep_recent_tokens == 3000
            assert cfg.brain.native.model_overrides["qwen/qwen3-thinking"][
                "supports_thinking"
            ] is True
            # Applied globally to the catalog at load time.
            info = get_model_info("qwen/qwen3-thinking")
            assert info.supports_thinking is True
            assert info.context_window == 32000
        finally:
            set_model_overrides({})

    def test_native_model_catalog_fetch_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[brain]
kind = "native"

[brain.native]
model = "anthropic/claude-opus-4.8"
base_url = "https://openrouter.ai/api/v1"
""")
        cfg = load_config(config_file)
        assert cfg.brain.native.model_catalog_fetch is True
        assert cfg.brain.native.model_catalog_cache_ttl_hours == 24.0

    def test_native_model_catalog_fetch_overrides(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[brain]
kind = "native"

[brain.native]
model = "local/model"
model_catalog_fetch = false
model_catalog_cache_ttl_hours = 6
""")
        cfg = load_config(config_file)
        assert cfg.brain.native.model_catalog_fetch is False
        assert cfg.brain.native.model_catalog_cache_ttl_hours == 6.0

    def test_load_without_sleep_cycle(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[users.bob]
display_name = "Bob"
""")
        cfg = load_config(config_file)
        assert cfg.sleep_cycle.enabled is True

    def test_load_sleep_cycle_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[sleep_cycle]
enabled = true
""")
        cfg = load_config(config_file)
        sc = cfg.sleep_cycle
        assert sc.cron == "0 2 * * *"
        assert sc.memory_retention_days == 0
        assert sc.lookback_hours == 24


class TestChannelSleepCycleConfig:
    def test_defaults(self):
        csc = ChannelSleepCycleConfig()
        assert csc.enabled is True
        assert csc.cron == "0 3 * * *"
        assert csc.lookback_hours == 24
        assert csc.memory_retention_days == 0

    def test_config_default(self):
        cfg = Config()
        assert cfg.channel_sleep_cycle.enabled is True

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[channel_sleep_cycle]
enabled = true
cron = "0 4 * * *"
lookback_hours = 48
memory_retention_days = 60
""")
        cfg = load_config(config_file)
        assert cfg.channel_sleep_cycle.enabled is True
        assert cfg.channel_sleep_cycle.cron == "0 4 * * *"
        assert cfg.channel_sleep_cycle.lookback_hours == 48
        assert cfg.channel_sleep_cycle.memory_retention_days == 60

    def test_load_defaults_when_not_set(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.channel_sleep_cycle.enabled is True
        assert cfg.channel_sleep_cycle.cron == "0 3 * * *"


class TestResourceConfig:
    def test_defaults(self):
        rc = ResourceConfig(type="folder", path="/test")
        assert rc.type == "folder"
        assert rc.path == "/test"
        assert rc.name == ""
        assert rc.permissions == "read"

    def test_with_all_fields(self):
        rc = ResourceConfig(type="todo_file", path="/todo.md", name="Tasks", permissions="write")
        assert rc.type == "todo_file"
        assert rc.name == "Tasks"
        assert rc.permissions == "write"

    def test_obsolete_type_raises(self):
        """Direct construction with retired types raises — protects fixtures
        from drifting after the modules / connected services refactor and the
        Resources sunset (the c1423ba class of bugs)."""
        with pytest.raises(ValueError, match="retired"):
            ResourceConfig(type="karakeep", extra={"base_url": "https://x"})
        with pytest.raises(ValueError, match="retired"):
            ResourceConfig(type="feeds")
        with pytest.raises(ValueError, match="retired"):
            ResourceConfig(type="money")

    def test_obsolete_type_allowed_via_flag(self):
        """The TOML/DB loaders set ``_allow_obsolete=True`` so the migration
        step can absorb credentials before dropping the rows."""
        rc = ResourceConfig(
            type="karakeep", extra={"base_url": "https://x"}, _allow_obsolete=True,
        )
        assert rc.type == "karakeep"

    def test_user_config_default_empty_resources(self):
        uc = UserConfig()
        assert uc.resources == []

    def test_user_config_with_resources(self):
        uc = UserConfig(resources=[
            ResourceConfig(type="folder", path="/projects"),
            ResourceConfig(type="todo_file", path="/todo.md", permissions="write"),
        ])
        assert len(uc.resources) == 2
        assert uc.resources[0].type == "folder"
        assert uc.resources[1].permissions == "write"


class TestDeveloperConfig:
    def test_defaults(self):
        dev = DeveloperConfig()
        assert dev.enabled is False
        assert dev.repos_dir == ""
        assert dev.gitlab_url == "https://gitlab.com"
        assert dev.gitlab_token == ""
        assert dev.gitlab_username == ""
        assert dev.github_url == "https://github.com"
        assert dev.github_token == ""
        assert dev.github_username == ""
        assert dev.github_default_owner == ""
        assert dev.github_reviewer == ""
        # The two REST endpoint allowlists are gone with the devbox proxy's
        # API actions — the real gh/glab run behind forge_cli.py instead.
        assert not hasattr(dev, "github_api_allowlist")
        assert not hasattr(dev, "gitlab_api_allowlist")
        # Forge CLI wrapper defaults.
        assert dev.forge_cli_extra_denied == []
        assert dev.forge_cli_permit == []
        assert dev.gh_bin_path == "/usr/local/bin/gh"
        assert dev.glab_bin_path == "/usr/local/bin/glab"
        # `api_timeout_seconds` went with the devbox proxy's httpx client:
        # nothing makes a REST call of its own any more.
        assert not hasattr(dev, "api_timeout_seconds")
        # Devbox proxy defaults.
        assert dev.devbox_proxy_enabled is True
        assert dev.devbox_proxy_socket_dir == "/var/run/istota"
        assert dev.devbox_proxy_audit_log == ""
        # Worktree reaping (ISSUE-288). On by default: the whole point was that
        # a retention rule nobody applied is the state that produced the leak.
        assert dev.worktree_reap_enabled is True
        assert dev.worktree_retention_hours == 24.0

    def test_load_worktree_reaping_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
worktree_reap_enabled = false
worktree_retention_hours = 72
""")
        cfg = load_config(config_file)
        assert cfg.developer.worktree_reap_enabled is False
        # TOML gives an int for a bare `72`; the field is a float and the
        # reaper multiplies it by 3600, so either arrives at the same window.
        assert cfg.developer.worktree_retention_hours == 72

    def test_load_devbox_proxy_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
devbox_proxy_enabled = false
devbox_proxy_socket_dir = "/run/istota"
devbox_proxy_audit_log = "/var/log/istota/devbox-proxy-audit.log"
""")
        cfg = load_config(config_file)
        assert cfg.developer.devbox_proxy_enabled is False
        assert cfg.developer.devbox_proxy_socket_dir == "/run/istota"
        assert cfg.developer.devbox_proxy_audit_log == "/var/log/istota/devbox-proxy-audit.log"

    def test_config_default(self):
        cfg = Config()
        assert cfg.developer.enabled is False

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
gitlab_url = "https://gitlab.example.com"
gitlab_token = "glpat-test"
gitlab_username = "istota"
""")
        cfg = load_config(config_file)
        assert cfg.developer.enabled is True
        assert cfg.developer.repos_dir == "/srv/repos"
        assert cfg.developer.gitlab_url == "https://gitlab.example.com"
        assert cfg.developer.gitlab_token == "glpat-test"
        assert cfg.developer.gitlab_username == "istota"

    def test_load_github_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
github_url = "https://github.example.com"
github_token = "ghp_test123"
github_username = "botuser"
github_default_owner = "myorg"
github_reviewer = "reviewer-user"
""")
        cfg = load_config(config_file)
        assert cfg.developer.github_url == "https://github.example.com"
        assert cfg.developer.github_token == "ghp_test123"
        assert cfg.developer.github_username == "botuser"
        assert cfg.developer.github_default_owner == "myorg"
        assert cfg.developer.github_reviewer == "reviewer-user"

    def test_load_gitlab_reviewer_from_toml(self, tmp_path):
        """ISSUE-289. `gitlab_reviewer` is the username `glab mr create
        --reviewer` resolves against; `gitlab_reviewer_id` is the numeric user
        id, kept because operators have it and it is what the API paths want,
        but read by nothing."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
gitlab_reviewer = "reviewer-user"
gitlab_reviewer_id = "1234567"
""")
        cfg = load_config(config_file)
        assert cfg.developer.gitlab_reviewer == "reviewer-user"
        assert cfg.developer.gitlab_reviewer_id == "1234567"

    def test_gitlab_reviewer_defaults_empty(self):
        dev = DeveloperConfig()
        assert dev.gitlab_reviewer == ""
        assert dev.gitlab_reviewer_id == ""

    def test_retired_keys_load_clean_and_inert(self, tmp_path):
        """Every deployed host has these three keys in its config.toml.
        `config.toml.j2` no longer renders them, but a host keeps its
        last-rendered file until Ansible runs again, and a hand-written
        config.toml may carry them for years. The loader ignores unknown keys
        by design, so they must load without raising and without reaching the
        DeveloperConfig constructor — a TypeError here would take the whole
        daemon down on upgrade, on every host at once."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
github_api_allowlist = ["GET /repos/*"]
gitlab_api_allowlist = ["GET /projects/*"]
api_timeout_seconds = 60
""")
        cfg = load_config(config_file)
        assert cfg.developer.enabled is True
        assert cfg.developer.repos_dir == "/srv/repos"
        assert not hasattr(cfg.developer, "github_api_allowlist")
        assert not hasattr(cfg.developer, "gitlab_api_allowlist")
        assert not hasattr(cfg.developer, "api_timeout_seconds")

    def test_load_forge_cli_knobs(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
forge_cli_extra_denied = ["gh pr merge", "repo view"]
forge_cli_permit = ["gh repo delete"]
gh_bin_path = "/opt/gh"
glab_bin_path = "/opt/glab"
""")
        cfg = load_config(config_file)
        assert cfg.developer.forge_cli_extra_denied == ["gh pr merge", "repo view"]
        assert cfg.developer.forge_cli_permit == ["gh repo delete"]
        assert cfg.developer.gh_bin_path == "/opt/gh"
        assert cfg.developer.glab_bin_path == "/opt/glab"

    def test_forge_cli_knob_given_as_a_bare_string(self, tmp_path):
        """`list("gh pr merge")` is eighteen one-character deny rules that
        match nothing, warn about nothing, and read in the config file exactly
        like a rule that is in force. A bare string is the plausible hand-edit,
        so take it as the single entry it was meant to be."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
forge_cli_extra_denied = "gh pr merge"
forge_cli_permit = ""
""")
        cfg = load_config(config_file)
        assert cfg.developer.forge_cli_extra_denied == ["gh pr merge"]
        assert cfg.developer.forge_cli_permit == []

    def test_dead_forge_cli_permit_is_warned_about(self, tmp_path, caplog):
        """A permit matching no rule is turning nothing off. Silence there
        reads exactly like a hatch that is still open."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
gitlab_token = "glpat-x"
forge_cli_permit = ["gh repo delete-repo"]
""")
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(config_file)
        assert any("forge_cli_permit" in r.getMessage() for r in caplog.records)

    def test_live_forge_cli_permit_is_not_warned_about(self, tmp_path, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
gitlab_token = "glpat-x"
forge_cli_permit = ["gh repo delete"]
""")
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(config_file)
        assert not any("forge_cli_permit" in r.getMessage() for r in caplog.records)

    def test_permit_cancelling_an_operator_addition_is_not_warned_about(
        self, tmp_path, caplog,
    ):
        """Cancelling your own extra_denied entry is a legitimate thing to
        write; warning about it is how a real warning gets ignored."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
gitlab_token = "glpat-x"
forge_cli_extra_denied = ["gh pr merge"]
forge_cli_permit = ["gh pr merge"]
""")
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(config_file)
        assert not any("forge_cli_permit" in r.getMessage() for r in caplog.records)

    def test_missing_forge_binary_is_warned_about(self, tmp_path, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text(f"""
[developer]
enabled = true
repos_dir = "/srv/repos"
github_token = "ghp-x"
gh_bin_path = "{tmp_path / 'no-such-gh'}"
""")
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(config_file)
        # Asserted as the condition rather than the phrasing: the wording now
        # comes from the doctor check, and a test that pins a sentence breaks on
        # every rewording while saying nothing about behaviour.
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "forge_binaries.gh" in messages
        assert str(tmp_path / "no-such-gh") in messages

    def test_no_binary_warning_without_a_token(self, tmp_path, caplog):
        """No token means no forge calls, so a missing binary is not yet a
        problem worth a line at every startup."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(f"""
[developer]
enabled = true
repos_dir = "/srv/repos"
gh_bin_path = "{tmp_path / 'no-such-gh'}"
""")
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(config_file)
        assert not any("not found at" in r.getMessage() for r in caplog.records)

    def test_forge_tokens_without_the_skill_proxy_are_warned_about(
        self, tmp_path, caplog,
    ):
        """With the proxy off, `setup_env` grants `direct_token` in the policy
        file and the wrapper reads the ambient token, so forge commands work
        (pinned by `test_policy_grants_direct_tokens_only_with_the_proxy_off`).
        What the warning is about is where the token then sits: in the
        environment the model's own shell inherits, rather than injected per
        call. This assertion used to demand the opposite message — that every
        command would fail — which had stopped being true when the direct-token
        branch landed."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
github_token = "ghp-x"

[security]
skill_proxy_enabled = false
""")
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(config_file)
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "readable by anything else the task runs" in m for m in messages
        ), messages
        # The warning must not claim breakage: an operator told their forge
        # commands are dead turns the skill off rather than turning the proxy on.
        assert not any("every forge command will fail" in m for m in messages)

    def test_no_proxy_warning_when_the_proxy_is_on(self, tmp_path, caplog):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
github_token = "ghp-x"
""")
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(config_file)
        assert not any(
            "no credential proxy to ask" in r.getMessage() for r in caplog.records
        )

    def test_github_env_var_override(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        monkeypatch.setenv("ISTOTA_DEVELOPER_GITHUB_TOKEN", "ghp_env_override")
        cfg = load_config(config_file)
        assert cfg.developer.github_token == "ghp_env_override"

    def test_load_defaults_when_not_set(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.developer.enabled is False
        assert cfg.developer.gitlab_url == "https://gitlab.com"
        assert cfg.developer.github_url == "https://github.com"

    def test_partial_config(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
""")
        cfg = load_config(config_file)
        assert cfg.developer.enabled is True
        assert cfg.developer.repos_dir == "/srv/repos"
        assert cfg.developer.gitlab_url == "https://gitlab.com"
        assert cfg.developer.gitlab_token == ""
        assert cfg.developer.github_url == "https://github.com"
        assert cfg.developer.github_token == ""


class TestMoneyModuleConfig:
    def test_lookup_defaults_on(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[nextcloud]\nurl = 'https://nc.example.com'\n")
        assert load_config(config_file).money.autoclass_lookup is True

    def test_operator_can_switch_the_lookup_off(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[money]\nautoclass_lookup = false\n")
        assert load_config(config_file).money.autoclass_lookup is False


class TestSiteConfig:
    def test_defaults(self):
        sc = SiteConfig()
        assert sc.hostname == ""

    def test_no_static_web_root_fields(self):
        """The agent-writable static web root was removed (ISSUE-194)."""
        sc = SiteConfig()
        assert not hasattr(sc, "enabled")
        assert not hasattr(sc, "base_path")

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[site]
hostname = "istota.example.com"
""")
        cfg = load_config(config_file)
        assert cfg.site.hostname == "istota.example.com"

    def test_retired_keys_are_ignored_with_warning(self, tmp_path, caplog):
        """A stale [site] block from a pre-removal deploy must not fail load."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[site]
enabled = true
hostname = "istota.example.com"
base_path = "/srv/app/istota/html"
""")
        with caplog.at_level(logging.WARNING):
            cfg = load_config(config_file)
        assert cfg.site.hostname == "istota.example.com"
        assert "base_path" in caplog.text

    def test_load_defaults_when_not_set(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.site.hostname == ""


        assert cfg.config_path == config_file

    def test_load_config_default_has_no_path(self):
        cfg = Config()
        assert cfg.config_path is None

    def test_load_config_honors_env_var(self, tmp_path, monkeypatch):
        """ISTOTA_CONFIG_PATH lets a subprocess find the parent's config."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        config_file = tmp_path / "from_env.toml"
        config_file.write_text('bot_name = "FromEnv"\n')
        # cwd is somewhere without a config/config.toml on the search list.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config_file))
        cfg = load_config()
        assert cfg.config_path == config_file
        assert cfg.bot_name == "FromEnv"

    def test_load_config_explicit_path_overrides_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        env_cfg = tmp_path / "env.toml"
        env_cfg.write_text('bot_name = "FromEnv"\n')
        explicit_cfg = tmp_path / "explicit.toml"
        explicit_cfg.write_text('bot_name = "Explicit"\n')
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(env_cfg))
        cfg = load_config(explicit_cfg)
        assert cfg.config_path == explicit_cfg
        assert cfg.bot_name == "Explicit"

    def test_load_config_env_var_missing_file_falls_through(self, tmp_path, monkeypatch):
        """If ISTOTA_CONFIG_PATH points at a missing file, search continues."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(tmp_path / "does_not_exist.toml"))
        # Isolate the search order from the developer/CI home: two candidates are
        # ~/src/config/config.toml and ~/.config/istota/config.toml (via
        # Path.home() → $HOME). Point HOME at the empty tmp dir so a real local
        # config (e.g. from a standalone `istota setup`) doesn't get picked up and
        # break the "nothing found" assertion. chdir handles the relative
        # config/config.toml candidate.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        cfg = load_config()
        # Default Config — config_path stays None because nothing was loaded.
        assert cfg.config_path is None


class TestAdminUsersLoadConfig:
    def test_load_config_loads_admin_users(self, tmp_path, monkeypatch):
        admins_file = tmp_path / "admins"
        admins_file.write_text("alice\n")
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins_file))
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.admin_users == {"alice"}

    def test_load_config_no_admins_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "nonexistent"))
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.admin_users == set()


class TestWorkerConcurrencyConfig:
    def test_scheduler_new_worker_fields_from_toml(self, tmp_path, monkeypatch):
        """Explicit max_foreground_workers/max_background_workers parsed from TOML."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'max_foreground_workers = 8\n'
            'max_background_workers = 4\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.max_foreground_workers == 8
        assert cfg.scheduler.max_background_workers == 4

    def test_scheduler_defaults(self):
        """Default values for new fields."""
        cfg = Config()
        assert cfg.scheduler.max_foreground_workers == 5
        assert cfg.scheduler.max_background_workers == 3

    def test_user_config_worker_limits(self, tmp_path, monkeypatch):
        """Per-user worker limits parsed from TOML."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.alice]\n'
            'display_name = "Alice"\n'
            'max_foreground_workers = 2\n'
            'max_background_workers = 3\n'
        )
        cfg = load_config(p)
        assert cfg.users["alice"].max_foreground_workers == 2
        assert cfg.users["alice"].max_background_workers == 3

    def test_user_config_worker_limits_defaults(self):
        """UserConfig defaults to 0/0 (use global default)."""
        from istota.config import UserConfig
        uc = UserConfig()
        assert uc.max_foreground_workers == 0
        assert uc.max_background_workers == 0

    def test_global_user_worker_defaults_from_toml(self, tmp_path, monkeypatch):
        """Global per-user worker defaults parsed from scheduler section."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'user_max_foreground_workers = 3\n'
            'user_max_background_workers = 2\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.user_max_foreground_workers == 3
        assert cfg.scheduler.user_max_background_workers == 2

    def test_global_user_worker_defaults(self):
        """Default global per-user limits are 2/1."""
        cfg = Config()
        assert cfg.scheduler.user_max_foreground_workers == 2
        assert cfg.scheduler.user_max_background_workers == 1

    def test_long_task_slot_defaults(self):
        cfg = Config()
        assert cfg.scheduler.long_task_threshold_minutes == 10
        assert cfg.scheduler.user_max_long_workers == 1
        assert cfg.scheduler.max_long_workers == 2

    def test_long_task_slot_keys_from_toml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'long_task_threshold_minutes = 25\n'
            'user_max_long_workers = 2\n'
            'max_long_workers = 3\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.long_task_threshold_minutes == 25
        assert cfg.scheduler.user_max_long_workers == 2
        assert cfg.scheduler.max_long_workers == 3

    def test_long_task_slot_keys_are_coerced_to_int(self, tmp_path, monkeypatch):
        """These reach arithmetic that decides how many worker threads exist.
        A TOML string would compare against an int rather than raise anywhere
        a reader would look — the same reason the admission gate's keys are
        coerced rather than passed through."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'long_task_threshold_minutes = "25"\n'
            'user_max_long_workers = "2"\n'
            'max_long_workers = "3"\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.long_task_threshold_minutes == 25
        assert cfg.scheduler.user_max_long_workers == 2
        assert cfg.scheduler.max_long_workers == 3

    def test_task_cgroup_defaults(self):
        cfg = Config()
        assert cfg.scheduler.task_cgroup_enabled is True
        assert cfg.scheduler.task_memory_max_mb == 2048
        assert cfg.scheduler.task_pids_max == 512
        assert cfg.scheduler.task_cpu_max_percent == 200

    def test_task_cgroup_keys_from_toml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'task_cgroup_enabled = false\n'
            'task_memory_max_mb = 4096\n'
            'task_pids_max = 1024\n'
            'task_cpu_max_percent = 0\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.task_cgroup_enabled is False
        assert cfg.scheduler.task_memory_max_mb == 4096
        assert cfg.scheduler.task_pids_max == 1024
        assert cfg.scheduler.task_cpu_max_percent == 0

    def test_task_cgroup_keys_are_coerced_to_int(self, tmp_path, monkeypatch):
        """These reach arithmetic whose result is written into a kernel file.
        A TOML string would raise `can't multiply sequence` from inside the
        spawn path — a task failure attributed to the brain, not the config."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'task_memory_max_mb = "4096"\n'
            'task_pids_max = "1024"\n'
            'task_cpu_max_percent = "150"\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.task_memory_max_mb == 4096
        assert cfg.scheduler.task_pids_max == 1024
        assert cfg.scheduler.task_cpu_max_percent == 150

    def test_load_config_user_worker_defaults_match_dataclass(self, tmp_path, monkeypatch):
        """load_config() without explicit settings should match Config() defaults."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text('[scheduler]\n')
        cfg = load_config(p)
        defaults = Config()
        assert cfg.scheduler.user_max_foreground_workers == defaults.scheduler.user_max_foreground_workers
        assert cfg.scheduler.user_max_background_workers == defaults.scheduler.user_max_background_workers

    def test_effective_user_workers_uses_global_default(self):
        """When user has 0 (not set), effective value comes from global default."""
        from istota.config import UserConfig
        cfg = Config()
        cfg.scheduler.user_max_foreground_workers = 3
        cfg.scheduler.user_max_background_workers = 2
        cfg.users["alice"] = UserConfig()  # 0/0 = use global
        assert cfg.effective_user_max_fg_workers("alice") == 3
        assert cfg.effective_user_max_bg_workers("alice") == 2

    def test_effective_user_workers_per_user_override(self):
        """Per-user setting overrides global default."""
        from istota.config import UserConfig
        cfg = Config()
        cfg.scheduler.user_max_foreground_workers = 1
        cfg.scheduler.user_max_background_workers = 1
        cfg.users["alice"] = UserConfig(max_foreground_workers=4, max_background_workers=2)
        assert cfg.effective_user_max_fg_workers("alice") == 4
        assert cfg.effective_user_max_bg_workers("alice") == 2

    def test_effective_user_workers_unknown_user(self):
        """Unknown user gets global default."""
        cfg = Config()
        cfg.scheduler.user_max_foreground_workers = 2
        cfg.scheduler.user_max_background_workers = 3
        assert cfg.effective_user_max_fg_workers("unknown") == 2
        assert cfg.effective_user_max_bg_workers("unknown") == 3

    def test_parsed_user_defaults_to_global_workers(self, tmp_path, monkeypatch):
        """User loaded without explicit worker limits uses global default."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'user_max_foreground_workers = 3\n'
            '\n'
            '[users.alice]\n'
            'display_name = "Alice"\n'
        )
        cfg = load_config(p)
        # alice doesn't set worker limits, so effective should use global
        assert cfg.effective_user_max_fg_workers("alice") == 3
        assert cfg.users["alice"].max_foreground_workers == 0  # sentinel, not 1


# ---------------------------------------------------------------------------
# TestMemorySystemConfigDefaults
# ---------------------------------------------------------------------------


class TestMemorySystemConfigDefaults:
    def test_sleep_cycle_auto_load_dated_days_default(self):
        cfg = SleepCycleConfig()
        assert cfg.auto_load_dated_days == 3

    def test_sleep_cycle_curate_user_memory_default(self):
        cfg = SleepCycleConfig()
        assert cfg.curate_user_memory is False

    def test_memory_search_auto_recall_default(self):
        cfg = MemorySearchConfig()
        assert cfg.auto_recall is False

    def test_memory_search_auto_recall_limit_default(self):
        cfg = MemorySearchConfig()
        assert cfg.auto_recall_limit == 5

    def test_memory_search_enabled_by_default(self):
        cfg = MemorySearchConfig()
        assert cfg.enabled is True

    def test_config_max_memory_chars_default(self):
        cfg = Config()
        assert cfg.max_memory_chars == 0


class TestMemorySystemConfigLoading:
    def test_load_sleep_cycle_auto_load_dated_days(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[sleep_cycle]\n'
            'enabled = true\n'
            'auto_load_dated_days = 7\n'
            'curate_user_memory = true\n'
        )
        cfg = load_config(p)
        assert cfg.sleep_cycle.auto_load_dated_days == 7
        assert cfg.sleep_cycle.curate_user_memory is True

    def test_load_memory_search_auto_recall(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[memory_search]\n'
            'enabled = true\n'
            'auto_recall = true\n'
            'auto_recall_limit = 10\n'
        )
        cfg = load_config(p)
        assert cfg.memory_search.auto_recall is True
        assert cfg.memory_search.auto_recall_limit == 10

    def test_load_max_memory_chars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text('max_memory_chars = 5000\n')
        cfg = load_config(p)
        assert cfg.max_memory_chars == 5000

    def test_load_defaults_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text('db_path = "test.db"\n')
        cfg = load_config(p)
        assert cfg.sleep_cycle.auto_load_dated_days == 3
        assert cfg.sleep_cycle.curate_user_memory is False
        assert cfg.memory_search.auto_recall is False
        assert cfg.memory_search.auto_recall_limit == 5
        assert cfg.max_memory_chars == 0


class TestTheExampleDocumentsEveryLiveSection:
    """Six live `Config` sections were in `config.example.toml` nowhere at all.

    A field an operator cannot learn about is a field nobody sets, and the
    example is the only place several of these are described — `[caldav]` and
    `[location]` are rendered by the Ansible template with no prose, `[devbox]`
    and `[browser]` gate whole containers, and `[brain.native.web_fetch]` is
    the one tool that leaves the sandbox's network namespace.

    Shaped after `test_config_native_session_log.py`'s own guard, which is
    where this idea already worked: walk the dataclass, require a commented
    assignment per field inside the block. Field-level and per section — the
    section-level "rendered, documented or exempted" walk is a separate guard,
    and it lives in `tests/test_config_section_coverage.py`. Keeping the two
    apart is deliberate: this one holds six hand-picked blocks to the field, and
    that one holds the whole tree to the section, because field level over the
    whole tree does not reach yet (14 leaf fields appear in neither artifact).
    """

    EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"

    SECTIONS = [
        ("caldav", "CaldavConfig"),
        ("location", "LocationReceiverConfig"),
        ("memory_search", "MemorySearchConfig"),
        ("devbox", "DevboxConfig"),
        ("browser", "BrowserConfig"),
        ("brain.native.web_fetch", "WebFetchConfig"),
    ]

    @pytest.mark.parametrize("header,dataclass_name", SECTIONS)
    def test_every_field_appears_in_the_commented_block(self, header, dataclass_name):
        import dataclasses
        import re

        from istota import config as config_module

        target = getattr(config_module, dataclass_name)

        text = self.EXAMPLE.read_text()
        marker = f"# [{header}]"
        assert marker in text, f"[{header}] is documented nowhere in the example"

        block = text.split(marker, 1)[1]
        # Up to the next section header, commented or live.
        block = re.split(r"^(?:# )?\[", block, maxsplit=1, flags=re.M)[0]

        missing = [
            f.name for f in dataclasses.fields(target)
            if not re.search(rf"^#\s*{f.name}\s*=", block, re.M)
        ]
        assert not missing, (
            f"[{header}] documents no {missing}. They are settable and there is "
            "no way to learn they exist."
        )

    def test_the_example_still_loads(self, tmp_path, monkeypatch):
        """The blocks above are commented, so this is the claim they cannot
        break — and it was asserted by nothing: the one existing test parses
        the file with `tomli` and never hands it to the loader."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))

        config = load_config(self.EXAMPLE)

        assert isinstance(config, Config)
        assert config.bot_name


class TestConfigToplevelKeyOrdering:
    """Regression: a table header in the rendered config or example file must
    not appear above top-level keys, or those keys get parsed as members of
    the table (TOML rule). This bit us once — `[brain]` placed above
    `db_path` in the Ansible template silently nested `db_path` under
    `brain`, causing the scheduler to fall back to the default DB path and
    log "unable to open database file" forever.
    """

    def test_example_config_db_path_at_root(self, tmp_path, monkeypatch):
        """Loading config/config.example.toml must yield root-level db_path,
        temp_dir, skills_dir — not nested under any table."""
        import tomli
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        example = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"
        with open(example, "rb") as f:
            data = tomli.load(f)
        # These must be at the root, not under [brain] or any other table.
        for key in ("db_path", "temp_dir", "skills_dir", "rclone_remote"):
            assert key in data, f"{key} not at root in config.example.toml"
        # And they must NOT be inside the brain table.
        if "brain" in data:
            for key in ("db_path", "temp_dir", "skills_dir", "rclone_remote"):
                assert key not in data["brain"], (
                    f"{key} ended up under [brain] — table header is positioned wrong"
                )

    def test_ansible_template_brain_below_toplevel_keys(self):
        """Verify the [brain] header in deploy/ansible/templates/config.toml.j2
        appears AFTER all top-level key assignments (db_path, temp_dir, etc.).
        TOML places every key after a table header into that table.
        """
        template = Path(__file__).resolve().parent.parent / "deploy" / "ansible" / "templates" / "config.toml.j2"
        text = template.read_text()
        brain_idx = text.find("\n[brain]\n")
        assert brain_idx >= 0, "[brain] section missing from Ansible template"
        # Top-level keys that must be defined before any table header.
        for key in ("db_path", "temp_dir", "skills_dir", "rclone_remote"):
            key_idx = text.find(f"\n{key} = ")
            assert key_idx >= 0, f"{key} assignment missing from template"
            assert key_idx < brain_idx, (
                f"{key} is defined AFTER [brain] in the Ansible template — "
                "it will be parsed as brain.{key}, breaking config loading"
            )


class TestAnsibleValidateConfigScript:
    """ISSUE-058: deploy/ansible/files/validate_config.py is the post-render
    structural check the role runs before restarting the scheduler.
    """

    @staticmethod
    def _run(tmp_path, cfg_text, expected_db, expected_tmp):
        import subprocess
        import sys

        cfg = tmp_path / "config.toml"
        cfg.write_text(cfg_text)
        script = (
            Path(__file__).resolve().parent.parent
            / "deploy" / "ansible" / "files" / "validate_config.py"
        )
        proc = subprocess.run(
            [sys.executable, str(script), str(cfg), "istota", expected_db, expected_tmp],
            capture_output=True, text=True,
        )
        return proc

    def test_passes_on_well_formed_config(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/srv/app/istota/data/istota.db"\n'
            'temp_dir = "/srv/app/istota/tmp"\n'
            "\n[brain]\nkind = \"claude_code\"\n"
        )
        proc = self._run(tmp_path, cfg, "/srv/app/istota/data/istota.db", "/srv/app/istota/tmp")
        assert proc.returncode == 0, proc.stderr
        assert "ok" in proc.stdout

    def test_fails_when_root_keys_leak_under_brain(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            "\n[brain]\n"
            'kind = "claude_code"\n'
            'db_path = "/srv/app/istota/data/istota.db"\n'
            'temp_dir = "/srv/app/istota/tmp"\n'
        )
        proc = self._run(tmp_path, cfg, "/srv/app/istota/data/istota.db", "/srv/app/istota/tmp")
        assert proc.returncode == 1
        assert "leaked under [brain]" in proc.stderr
        assert "db_path" in proc.stderr and "temp_dir" in proc.stderr

    def test_fails_when_db_path_does_not_match_expected(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/wrong/path.db"\n'
            'temp_dir = "/srv/app/istota/tmp"\n'
            "\n[brain]\nkind = \"claude_code\"\n"
        )
        proc = self._run(tmp_path, cfg, "/srv/app/istota/data/istota.db", "/srv/app/istota/tmp")
        assert proc.returncode == 1
        assert "db_path" in proc.stderr and "/wrong/path.db" in proc.stderr

    def test_fails_on_unparseable_toml(self, tmp_path):
        proc = self._run(tmp_path, "this is not [valid TOML\n", "x", "y")
        assert proc.returncode == 1
        assert "TOML parse error" in proc.stderr

    def test_brain_kind_alone_does_not_trip_leak_check(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/srv/app/istota/data/istota.db"\n'
            'temp_dir = "/srv/app/istota/tmp"\n'
            "\n[brain]\n"
            'kind = "claude_code"\n'
        )
        proc = self._run(tmp_path, cfg, "/srv/app/istota/data/istota.db", "/srv/app/istota/tmp")
        assert proc.returncode == 0, proc.stderr

    def test_advisor_model_non_string_fails(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/srv/app/istota/data/istota.db"\n'
            'temp_dir = "/srv/app/istota/tmp"\n'
            "advisor_model = 5\n"
            "\n[brain]\nkind = \"claude_code\"\n"
        )
        proc = self._run(tmp_path, cfg, "/srv/app/istota/data/istota.db", "/srv/app/istota/tmp")
        assert proc.returncode == 1
        assert "advisor_model must be a string" in proc.stderr

    def test_advisor_model_under_native_warns_not_fails(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/srv/app/istota/data/istota.db"\n'
            'temp_dir = "/srv/app/istota/tmp"\n'
            'advisor_model = "opus"\n'
            "\n[brain]\nkind = \"native\"\n"
            "\n[brain.native]\nmodel = \"anthropic/claude-opus-4.8\"\n"
        )
        proc = self._run(tmp_path, cfg, "/srv/app/istota/data/istota.db", "/srv/app/istota/tmp")
        assert proc.returncode == 0, proc.stderr
        assert "not an anthropic-namespace brain" in proc.stderr

    def test_advisor_model_under_native_still_warns_with_anthropic_fallback(self, tmp_path):
        # A fallback doesn't rescue the setting: the executor only ever
        # resolves `advisor` for the primary brain when its namespace is
        # anthropic, and a native->anthropic fallback never picks one up.
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/srv/app/istota/data/istota.db"\n'
            'temp_dir = "/srv/app/istota/tmp"\n'
            'advisor_model = "opus"\n'
            "\n[brain]\nkind = \"native\"\nfallback = \"claude_code\"\n"
            "\n[brain.native]\nmodel = \"anthropic/claude-opus-4.8\"\n"
        )
        proc = self._run(tmp_path, cfg, "/srv/app/istota/data/istota.db", "/srv/app/istota/tmp")
        assert proc.returncode == 0, proc.stderr
        assert "not an anthropic-namespace brain" in proc.stderr

    def test_advisor_model_under_claude_code_is_silent(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/srv/app/istota/data/istota.db"\n'
            'temp_dir = "/srv/app/istota/tmp"\n'
            'advisor_model = "opus"\n'
            "\n[brain]\nkind = \"claude_code\"\n"
        )
        proc = self._run(tmp_path, cfg, "/srv/app/istota/data/istota.db", "/srv/app/istota/tmp")
        assert proc.returncode == 0, proc.stderr
        assert "advisor_model" not in proc.stderr

    def test_advisor_model_with_no_brain_kind_is_silent(self, tmp_path):
        # BrainConfig.kind defaults to "claude_code" — an omitted [brain]/kind
        # key must not read as brain.get("kind") is None and trip the
        # not-anthropic warning for a perfectly working default deployment.
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/srv/app/istota/data/istota.db"\n'
            'temp_dir = "/srv/app/istota/tmp"\n'
            'advisor_model = "opus"\n'
        )
        proc = self._run(tmp_path, cfg, "/srv/app/istota/data/istota.db", "/srv/app/istota/tmp")
        assert proc.returncode == 0, proc.stderr
        assert "advisor_model" not in proc.stderr


class TestApplyUserResources:
    """`_apply_user_resources` overlays DB resource rows onto loaded UserConfig.

    The runtime invariant is: every resource the operator has provisioned —
    whether via TOML or via `istota resource ensure` / web UI — appears in
    ``config.users[uid].resources`` so existing call sites (executor merge,
    webhook_receiver, money/feeds loaders, secrets_store import) work
    uniformly. DB rows win when the (type, path) pair matches a TOML row.
    """

    def _write_minimal_config(self, tmp_path: Path, db_path: Path) -> Path:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        return cfg

    def test_db_resource_appears_in_user_config(self, tmp_path, monkeypatch):
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Docs", display_name="Docs",
                extras={"meta_key": "meta-val", "meta_count": 75},
            )
        cfg_path = self._write_minimal_config(tmp_path, db_path)
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg_path)
        resources = config.users["alice"].resources
        folders = [r for r in resources if r.type == "folder"]
        assert len(folders) == 1
        assert folders[0].extra == {"meta_key": "meta-val", "meta_count": 75}
        assert folders[0].name == "Docs"

    def test_db_row_dedupes_against_matching_toml_row(self, tmp_path, monkeypatch):
        # Same (type, path): DB row replaces TOML row. Without dedupe the
        # executor would see two ResourceConfig entries for one logical
        # resource and double-count.
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Docs", display_name="Docs (DB)",
                extras={"meta_key": "from-db"},
            )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.resources]]\n"
            'type = "folder"\n'
            'path = "/Docs"\n'
            'name = "Docs (TOML)"\n'
            'meta_key = "from-toml"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        resources = config.users["alice"].resources
        folders = [r for r in resources if r.type == "folder"]
        assert len(folders) == 1
        # DB wins because once the row exists, operators expect it to be
        # authoritative — same precedence as user_profiles.
        assert folders[0].extra["meta_key"] == "from-db"
        assert folders[0].name == "Docs (DB)"

    def test_distinct_paths_keep_both_resources(self, tmp_path, monkeypatch):
        # Two folders with different paths must coexist — dedupe is keyed on
        # (type, path), not type alone.
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Documents", display_name="Docs",
            )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.resources]]\n"
            'type = "folder"\n'
            'path = "/Pictures"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        folders = sorted(
            (r.path for r in config.users["alice"].resources if r.type == "folder")
        )
        assert folders == ["/Documents", "/Pictures"]

    def test_synthesises_user_when_only_db_row_exists(self, tmp_path, monkeypatch):
        # A user with no TOML stanza but a DB resource row must still be
        # reachable through config.users[uid].resources. Mirrors the
        # _apply_user_profiles pattern for synthesised UserConfigs.
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="bob", resource_type="folder",
                resource_path="/Bob", display_name="Bob's folder",
            )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        assert "bob" in config.users
        assert any(r.type == "folder" and r.path == "/Bob"
                   for r in config.users["bob"].resources)

    def test_missing_db_does_not_fail_load(self, tmp_path, monkeypatch):
        # Same best-effort contract as _apply_user_profiles: callers like
        # `istota init` run before the DB exists.
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{tmp_path / "does-not-exist.db"}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.resources]]\n"
            'type = "folder"\n'
            'path = "/x"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        assert any(r.type == "folder" for r in config.users["alice"].resources)


class TestApplyUserBriefings:
    """`_apply_user_briefings` overlays DB briefing rows onto loaded UserConfig.

    DB row replaces the matching TOML briefing by ``name``; new DB rows are
    added on top. Disabled rows drop the matching TOML name without
    scheduling a replacement, so the web UI can mute a TOML-templated
    briefing without re-templating.
    """

    def _write_minimal_config(self, tmp_path: Path, db_path: Path) -> Path:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        return cfg

    def test_db_briefing_appears_in_user_config(self, tmp_path, monkeypatch):
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 7 * * 1-5", conversation_token="tok123",
            output="talk", components={"calendar": True},
        )
        cfg_path = self._write_minimal_config(tmp_path, db_path)
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg_path)
        briefings = config.users["alice"].briefings
        assert len(briefings) == 1
        assert briefings[0].name == "morning"
        assert briefings[0].cron == "0 7 * * 1-5"
        assert briefings[0].components == {"calendar": True}

    def test_db_row_replaces_matching_toml_row(self, tmp_path, monkeypatch):
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 8 * * *", conversation_token="db-room",
            output="talk", components={"calendar": True},
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.briefings]]\n"
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "toml-room"\n'
            'output = "talk"\n'
            "[users.alice.briefings.components]\n"
            "todos = true\n"
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        briefings = config.users["alice"].briefings
        assert len(briefings) == 1
        # DB wins
        assert briefings[0].cron == "0 8 * * *"
        assert briefings[0].conversation_token == "db-room"

    def test_distinct_names_coexist(self, tmp_path, monkeypatch):
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="evening",
            cron="0 19 * * *", conversation_token="t", output="talk",
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.briefings]]\n"
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "t"\n'
            'output = "talk"\n'
            "[users.alice.briefings.components]\n"
            "calendar = true\n"
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        names = {b.name for b in config.users["alice"].briefings}
        assert names == {"morning", "evening"}

    def test_disabled_db_row_drops_toml_briefing(self, tmp_path, monkeypatch):
        # Operator can mute a TOML-templated briefing via the web UI by
        # toggling the row off. Without this the TOML would resurrect it.
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 7 * * *", conversation_token="t", output="talk",
            enabled=False,
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.briefings]]\n"
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "t"\n'
            'output = "talk"\n'
            "[users.alice.briefings.components]\n"
            "calendar = true\n"
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        assert config.users["alice"].briefings == []

    def test_missing_db_does_not_fail_load(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{tmp_path / "does-not-exist.db"}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        assert "alice" in config.users


class TestConfigAuthoredBriefingBlocks:
    """Config-authored rich ``[[briefings.blocks]]`` thread through config load.

    ``blocks`` is an in-memory-only field: parsed from TOML, re-attached to the
    surviving (DB-shadowed) entry in ``_apply_user_briefings``, and never
    persisted to ``briefing_configs``.
    """

    _BLOCKS_TOML = (
        "\n[[users.alice.briefings]]\n"
        'name = "morning"\n'
        'cron = "0 7 * * *"\n'
        'output = "email"\n'
        "\n[[users.alice.briefings.blocks]]\n"
        'title = "World News"\n'
        'directive = "neutral"\n'
        'render_mode = "synthesis"\n'
        "options = { story_count = 5 }\n"
        "\n[[users.alice.briefings.blocks.sources]]\n"
        'kind = "rss"\n'
        "config = { lookback_hours = 24 }\n"
        "\n[[users.alice.briefings.blocks.sources]]\n"
        'kind = "browse"\n'
        'config = { preset = "ap" }\n'
    )

    def test_parse_user_data_populates_blocks(self, tmp_path, monkeypatch):
        # No DB row → the TOML briefing survives natively, blocks intact.
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{tmp_path / "does-not-exist.db"}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            + self._BLOCKS_TOML
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        briefings = config.users["alice"].briefings
        assert len(briefings) == 1
        blocks = briefings[0].blocks
        assert len(blocks) == 1
        assert blocks[0]["title"] == "World News"
        assert blocks[0]["options"] == {"story_count": 5}
        kinds = [s["kind"] for s in blocks[0]["sources"]]
        assert kinds == ["rss", "browse"]

    def test_blocks_reattached_to_db_shadowed_entry(self, tmp_path, monkeypatch):
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        # A briefing_configs row (as import_from_user_configs would seed) claims
        # the name — the TOML briefing is dropped, but its blocks must ride on
        # the surviving DB-sourced entry.
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 7 * * *", conversation_token="", output="email",
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            + self._BLOCKS_TOML
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        briefings = config.users["alice"].briefings
        assert len(briefings) == 1
        assert briefings[0].from_db is True
        assert [b["title"] for b in briefings[0].blocks] == ["World News"]

    def test_blocks_not_persisted_to_framework_db(self, tmp_path, monkeypatch):
        # The framework briefing_configs row is byte-unchanged by blocks —
        # ensure_briefing equality is a no-op regardless of blocks.
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 7 * * *", conversation_token="", output="email",
        )
        # Re-run with the same schedule/delivery → noop; blocks live only in
        # config, never reach the row.
        _, state2 = user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 7 * * *", conversation_token="", output="email",
        )
        assert state2 == "noop"
        row = user_briefings.list_briefings(db_path)[0]
        assert "__blocks__" not in row.components
        assert "blocks" not in row.components

    def test_blocks_survive_get_briefings_for_user(self, tmp_path):
        # get_briefings_for_user returns briefings verbatim (no component
        # expansion); config-authored blocks pass through untouched.
        from istota.config import (
            Config,
            UserConfig,
        )
        from istota.skills.briefing import get_briefings_for_user

        briefing = BriefingConfig(
            name="morning", cron="0 7 * * *",
            blocks=[{"title": "News", "sources": [{"kind": "rss", "config": {}}]}],
        )
        config = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"alice": UserConfig(briefings=[briefing])},
        )
        result = get_briefings_for_user(config, "alice")
        assert len(result) == 1
        assert [b["title"] for b in result[0].blocks] == ["News"]


class TestDisabledModules:
    """Phase 1 of the modules / connected services refactor."""

    def test_user_config_default_empty(self):
        assert UserConfig().disabled_modules == []

    def test_parsed_from_toml(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.alice]\n'
            'display_name = "Alice"\n'
            'disabled_modules = ["feeds", "money"]\n'
        )
        cfg = load_config(p)
        assert cfg.users["alice"].disabled_modules == ["feeds", "money"]

    def test_is_module_enabled_default_on(self):
        cfg = Config()
        cfg.users["alice"] = UserConfig()
        assert cfg.is_module_enabled("alice", "feeds") is True
        assert cfg.is_module_enabled("alice", "money") is True
        assert cfg.is_module_enabled("alice", "location") is True

    def test_is_module_enabled_unknown_user_default_on(self):
        cfg = Config()
        # No users configured at all — default-on still applies (docker
        # auto-seeding flow can hit this path before profiles are written).
        assert cfg.is_module_enabled("ghost", "feeds") is True

    def test_is_module_enabled_disabled_for_user(self):
        cfg = Config()
        cfg.users["alice"] = UserConfig(disabled_modules=["feeds"])
        assert cfg.is_module_enabled("alice", "feeds") is False
        assert cfg.is_module_enabled("alice", "money") is True

    def test_is_module_enabled_false_when_dependency_missing(self, monkeypatch):
        """A module whose optional install extra is absent is unavailable —
        hidden from the web UI and skipped by the scheduler — so a lean install
        (e.g. `local` without the money extra) shows no broken Money tab."""
        from istota import modules

        monkeypatch.setitem(
            modules.MODULE_DEPENDENCIES, "money", ("a_pkg_not_installed_zzz",),
        )
        monkeypatch.setattr(modules, "_AVAILABILITY_CACHE", {})
        cfg = Config()
        cfg.users["alice"] = UserConfig()
        assert cfg.is_module_enabled("alice", "money") is False
        # A module with its deps present is unaffected.
        assert cfg.is_module_enabled("alice", "feeds") is True

    def test_module_available_true_when_no_deps_declared(self, monkeypatch):
        from istota import modules

        monkeypatch.setattr(modules, "_AVAILABILITY_CACHE", {})
        assert modules.module_available("feeds") is True

    def test_is_module_enabled_unknown_module(self):
        # Unknown module names are never "enabled" — guard against typos
        # leaking into user-supplied data.
        cfg = Config()
        cfg.users["alice"] = UserConfig()
        assert cfg.is_module_enabled("alice", "ghost") is False

    def test_is_module_enabled_reads_live_db_row(self, tmp_path):
        # Cross-process scenario: web_app writes user_profiles.disabled_modules
        # via the settings UI; the scheduler (its own Config instance) must
        # pick up the new value on the next is_module_enabled call without
        # any reload. The in-memory UserConfig deliberately disagrees with
        # the DB row to prove the DB is consulted, not the in-memory copy.
        from istota import db, user_profiles

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_profiles.ensure_profile(db_path, "alice", display_name="Alice")

        cfg = Config()
        cfg.db_path = db_path
        cfg.users["alice"] = UserConfig(disabled_modules=["feeds"])  # stale in-memory

        # DB row has feeds enabled (default empty list) — DB wins.
        assert cfg.is_module_enabled("alice", "feeds") is True

        # External writer disables money in the DB; next call reflects it.
        user_profiles.update_profile(db_path, "alice", disabled_modules=["money"])
        assert cfg.is_module_enabled("alice", "money") is False
        assert cfg.is_module_enabled("alice", "feeds") is True

    def test_is_module_enabled_falls_back_when_no_db_row(self, tmp_path):
        # A user with no user_profiles row (e.g. mid-init, before auto-seed)
        # falls back to the in-memory UserConfig.disabled_modules list.
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        cfg = Config()
        cfg.db_path = db_path
        cfg.users["alice"] = UserConfig(disabled_modules=["feeds"])
        assert cfg.is_module_enabled("alice", "feeds") is False


class TestCleanupObsoleteResources:
    """db.cleanup_obsolete_resources removes retired resource types."""

    def test_drops_retired_types(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            for rtype in ("feeds", "money", "monarch", "moneyman", "karakeep", "overland"):
                db.add_user_resource(
                    conn, user_id="alice", resource_type=rtype,
                    resource_path=rtype, display_name=f"{rtype} display",
                )
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Docs", display_name="Docs",
            )
        removed = db.cleanup_obsolete_resources(db_path)
        assert removed == 6
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert [r.resource_type for r in rows] == ["folder"]

    def test_idempotent(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Docs", display_name="Docs",
            )
        # Second run is a no-op — operators can leave the call wired into
        # startup without worrying about duplicate work.
        assert db.cleanup_obsolete_resources(db_path) == 0
        assert db.cleanup_obsolete_resources(db_path) == 0

    def test_missing_db_is_noop(self, tmp_path):
        from istota import db
        # Mirrors the best-effort contract used by _apply_user_profiles —
        # callers like `istota init` may run before the DB exists.
        assert db.cleanup_obsolete_resources(tmp_path / "no.db") == 0

    def test_load_config_runs_cleanup(self, tmp_path, monkeypatch):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={"ingest_token": "tok-xyz"},
            )

        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
        config = load_config(cfg)

        # The retired row no longer surfaces in the in-memory resources
        # list (the load_config-time filter caught it).
        assert all(r.type != "overland" for r in config.users["alice"].resources)

        # And the row is gone from the DB.
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert all(r.resource_type != "overland" for r in rows)

        # The credential was migrated into the secrets table during the
        # same load — webhook_receiver.reload_config picks it up from there.
        from istota import secrets_store
        assert secrets_store.get_secret(
            db_path, "alice", "overland", "ingest_token",
        ) == "tok-xyz"


class TestCleanupSunsetResources:
    """The Resources sunset retires calendar/email_folder/notes_folder
    (cleaned unconditionally). todo_file/reminders_file survive as deprecated
    overrides (read by the legacy briefing fetchers) and are never auto-cleaned."""

    def test_drops_sunset_obsolete_types(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            for rtype in ("calendar", "email_folder", "notes_folder"):
                db.add_user_resource(
                    conn, user_id="alice", resource_type=rtype,
                    resource_path=f"/{rtype}", display_name=rtype,
                )
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Docs", display_name="Docs",
            )
        removed = db.cleanup_obsolete_resources(db_path)
        assert removed == 3
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert [r.resource_type for r in rows] == ["folder"]

    def test_todo_file_reminders_file_not_auto_cleaned(self, tmp_path):
        """todo_file/reminders_file are deprecated overrides, not obsolete —
        they survive cleanup so a user on the legacy path keeps their file."""
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="todo_file",
                resource_path="Users/alice/shared/TASKS.md", display_name="Tasks",
            )
            db.add_user_resource(
                conn, user_id="alice", resource_type="reminders_file",
                resource_path="Users/alice/shared/REMINDERS.md",
            )
        removed = db.cleanup_obsolete_resources(db_path)
        assert removed == 0
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert sorted(r.resource_type for r in rows) == ["reminders_file", "todo_file"]

    def test_missing_db_is_noop(self, tmp_path):
        from istota import db
        assert db.cleanup_obsolete_resources(tmp_path / "no.db") == 0


class TestResourceConfigGrepGuard:
    """After the Resources sunset, ResourceConfig has no base_url/api_key
    fields (credentials live in extra/secrets). Guards against drift."""

    def test_no_base_url_or_api_key_attributes(self):
        from istota.config import ResourceConfig
        rc = ResourceConfig(type="folder", path="/x")
        assert not hasattr(rc, "base_url")
        assert not hasattr(rc, "api_key")
        # Credentials flow through extra, not flat fields.
        rc2 = ResourceConfig(
            type="folder", path="/x",
            extra={"base_url": "https://k.example", "api_key": "secret"},
        )
        assert rc2.extra["base_url"] == "https://k.example"
        assert rc2.extra["api_key"] == "secret"


class TestBriefingEmailHtmlFor:
    """Config.briefing_email_html_for — per-user HTML briefing email preference."""

    def test_defaults_on(self):
        cfg = Config(users={"carol": UserConfig()})
        assert cfg.briefing_email_html_for("carol") is True

    def test_unknown_user_defaults_on(self):
        cfg = Config(users={})
        assert cfg.briefing_email_html_for("nobody") is True

    def test_opt_out(self):
        cfg = Config(users={"carol": UserConfig(briefing_email_html=False)})
        assert cfg.briefing_email_html_for("carol") is False


class TestValidateForgeClis:
    """`_validate_forge_clis` runs inside every `load_config`, which means the
    daemon, the web app, the webhook receiver, every CLI invocation, and every
    host-side skill CLI the skill proxy spawns *per call*.

    These tests pin the two properties that must survive its reduction onto the
    doctor registry: which deployment shapes warn at all, and that none of them
    spawns a subprocess to find out. They are written to pass against both the
    original implementation and the reduced one — an equivalence test that only
    passes after the change is just a test of the new code.
    """

    @staticmethod
    def _warnings(caplog, config):
        from istota.config import _validate_forge_clis

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            _validate_forge_clis(config)
        return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]

    @staticmethod
    def _dev(tmp_path, **overrides):
        repos = tmp_path / "repos"
        repos.mkdir(exist_ok=True)
        fields = {
            "enabled": True,
            "repos_dir": str(repos),
            "gitlab_token": "NOT-A-REAL-TOKEN-" + "x" * 12,
            "gh_bin_path": str(tmp_path / "nowhere" / "gh"),
            "glab_bin_path": str(tmp_path / "nowhere" / "glab"),
        }
        fields.update(overrides)
        return DeveloperConfig(**fields)

    def test_skill_disabled_is_silent(self, caplog, tmp_path):
        config = Config(developer=DeveloperConfig(enabled=False))
        assert self._warnings(caplog, config) == []

    def test_enabled_without_repos_dir_is_silent(self, caplog, tmp_path):
        config = Config(developer=self._dev(tmp_path, repos_dir=""))
        assert self._warnings(caplog, config) == []

    def test_enabled_with_repos_dir_but_no_token_is_silent(self, caplog, tmp_path):
        """A skill that is not wired is not a failure. Without this gate a
        tokenless developer deployment goes from silent to alerting."""
        config = Config(developer=self._dev(tmp_path, gitlab_token="", github_token=""))
        assert self._warnings(caplog, config) == []

    def test_token_with_a_missing_binary_warns_naming_both(self, caplog, tmp_path):
        config = Config(developer=self._dev(tmp_path))
        messages = " ".join(self._warnings(caplog, config))
        assert str(tmp_path / "nowhere" / "gh") in messages
        assert str(tmp_path / "nowhere" / "glab") in messages

    def test_token_with_present_binaries_does_not_warn_about_them(self, caplog, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for name in ("gh", "glab"):
            path = bindir / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        config = Config(
            developer=self._dev(
                tmp_path,
                gh_bin_path=str(bindir / "gh"),
                glab_bin_path=str(bindir / "glab"),
            )
        )
        messages = " ".join(self._warnings(caplog, config))
        assert "does not exist" not in messages
        assert "not found" not in messages

    def test_delivers_only_the_three_facts_it_owned(self, caplog, tmp_path):
        """The registry has grown three other checks. This path runs once per
        *call* in a skill CLI, so a warning from here repeats for as long as the
        condition holds; the boot run and the hourly sweep say each one once."""
        config = Config(developer=self._dev(tmp_path))
        messages = " ".join(self._warnings(caplog, config))
        assert "forge_config_drift" not in messages
        assert "forge_wrapper_shadowing" not in messages
        assert "security.skill_proxy:" not in messages

    def test_proxy_off_with_tokens_warns_about_the_posture(self, caplog, tmp_path):
        from istota.config import SecurityConfig

        config = Config(
            developer=self._dev(tmp_path),
            security=SecurityConfig(skill_proxy_enabled=False),
        )
        messages = " ".join(self._warnings(caplog, config))
        assert "readable by anything else the task runs" in messages

    def test_proxy_off_without_tokens_is_silent_about_the_posture(self, caplog, tmp_path):
        from istota.config import SecurityConfig

        config = Config(
            developer=self._dev(tmp_path, gitlab_token="", github_token=""),
            security=SecurityConfig(skill_proxy_enabled=False),
        )
        messages = " ".join(self._warnings(caplog, config))
        assert "readable by anything else" not in messages

    def test_unmatched_permit_warns_naming_the_entry(self, caplog, tmp_path):
        config = Config(developer=self._dev(tmp_path, forge_cli_permit=["gh not-a-real-verb"]))
        messages = " ".join(self._warnings(caplog, config))
        assert "not-a-real-verb" in messages

    def test_spawns_no_subprocess(self, caplog, tmp_path, monkeypatch):
        """The whole reason `run_checks` takes `probe`. Five `--version` spawns
        per skill-CLI invocation is not a refactor.

        Counted with a spy, not asserted by raising: `_validate_forge_clis`
        wraps the call in `except Exception` so a raising stub is swallowed into
        a "forge CLI validation failed" warning and the test passes regardless.
        That warning is asserted absent for the same reason.
        """
        import subprocess

        from istota.config import _validate_forge_clis

        spawns = []

        def _spy(*args, **kwargs):
            spawns.append(args[0] if args else kwargs.get("args"))
            raise OSError("no subprocesses in this test")

        monkeypatch.setattr(subprocess, "run", _spy)
        monkeypatch.setattr(subprocess, "Popen", _spy)
        monkeypatch.setattr(subprocess, "check_output", _spy)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            _validate_forge_clis(Config(developer=self._dev(tmp_path)))
        assert spawns == [], f"config load spawned: {spawns}"
        assert not any(
            "forge CLI validation failed" in r.getMessage() for r in caplog.records
        ), "the run raised and was swallowed, so the spawn assertion proved nothing"

    def test_a_resolvable_fallback_no_longer_warns_here(self, caplog, tmp_path):
        """The one deliberate narrowing against the old behaviour.

        The old code checked the *configured* path directly, so a `config.toml`
        naming a stale path warned even when resolution fell back successfully —
        the `30bb7c83` shape. That condition is now reported by
        `developer.forge_config_drift`, which the boot run and the hourly sweep
        deliver; this per-call path stays quiet about it.
        """
        bindir = tmp_path / "bin"
        bindir.mkdir()
        real = bindir / "gh"
        real.write_text("#!/bin/sh\nexit 0\n")
        real.chmod(0o755)
        monkeypatch_path = str(bindir)

        import os

        from istota.config import _validate_forge_clis

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = monkeypatch_path + os.pathsep + old_path
        try:
            # gh_bin_path left at the dataclass default, which does not exist;
            # resolution falls through to the `gh` now on PATH.
            config = Config(
                developer=self._dev(tmp_path, gh_bin_path="/usr/local/bin/gh")
            )
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                _validate_forge_clis(config)
            messages = " ".join(r.getMessage() for r in caplog.records)
        finally:
            os.environ["PATH"] = old_path
        assert "forge_binaries.gh" not in messages
        assert "forge_config_drift" not in messages

    def test_never_raises(self, tmp_path):
        """It is on the config-load path; an exception there is an outage."""
        from istota.config import _validate_forge_clis

        broken = self._dev(tmp_path)
        broken.forge_cli_permit = None  # a shape nothing should produce
        _validate_forge_clis(Config(developer=broken))


class TestTheDeveloperContainerBlock:
    """`[developer.container]` — where project code builds and runs.

    Corrects rather than refuses, one WARNING per correction, because
    `load_config` runs in the scheduler, the web app, the webhook receiver and
    every host-side skill CLI the proxy spawns per call. A typo on a knob must
    not stop any of them from starting.
    """

    def _parse(self, raw):
        from istota.config import _parse_container_block

        return _parse_container_block(raw)

    def test_the_default_is_the_behaviour_every_deployment_already_had(self):
        """No devbox, no container: development work runs on the host."""
        from istota.config import Config, container_backend

        assert container_backend(Config()) == "none"

    def test_the_retired_backend_key_is_not_a_field(self):
        from istota.config import ContainerConfig

        assert not hasattr(ContainerConfig(), "backend")

    def test_the_retired_key_is_ignored_and_says_so(self, caplog):
        """Silence would be the worst option available.

        An operator who wrote `backend = "none"` to keep builds on the host had
        that honoured until this release; on the next deploy their devbox
        starts taking the work. Dropping the key without a word is how that
        becomes a surprise at 2am rather than a line in the boot log.
        """
        with caplog.at_level(logging.WARNING):
            parsed = self._parse({"backend": "none"})

        assert "backend" not in parsed
        assert "retired" in caplog.text
        assert "[devbox] enabled" in caplog.text

    def test_a_relative_socket_dir_is_refused(self, caplog):
        """It would anchor on whatever directory the daemon was started in,
        which is not a boundary anyone chose."""
        with caplog.at_level(logging.WARNING):
            parsed = self._parse({"exec_socket_dir": "run/exec"})

        assert "exec_socket_dir" not in parsed
        assert "absolute" in caplog.text

    def test_a_bare_string_shim_list_is_refused(self, caplog):
        """`shim_commands = "npm"` iterates as *characters*, so it would install
        a shim called `n` and one called `p` — the same failure
        `sandbox_ro_paths` had, and it gets the same explicit refusal."""
        with caplog.at_level(logging.WARNING):
            parsed = self._parse({"shim_commands": "npm"})

        assert "shim_commands" not in parsed

    @pytest.mark.parametrize("entry", ["../evil", "/bin/sh", "a b", "", ".hidden"])
    def test_a_shim_entry_that_is_not_a_command_name_is_dropped(self, entry):
        """The name becomes a filename in a directory on the model's PATH, so a
        `/` would write outside it."""
        parsed = self._parse({"shim_commands": ["npm", entry]})

        assert parsed["shim_commands"] == ["npm"]

    @pytest.mark.parametrize(
        "interpreter",
        ["python", "python3", "python3.12", "sh", "bash", "env",
         "git", "gh", "glab", "istota-skill"],
    )
    def test_the_sandboxs_own_machinery_cannot_be_shimmed(self, interpreter, caplog):
        """Each of these is machinery the sandbox itself runs, so shimming one
        breaks tasks that never touch a build. The network bridge is
        `/bin/sh -c "python3 {bridge_path} … & exec env … \"$@\""` inside the
        namespace with the model's PATH in force; git's credential helper is
        registered per task and exists only on the host; `gh` and `glab` on PATH
        are the policy wrapper; `istota-skill` is the skill proxy's client.

        `python3.12` is in the list because a versioned name would otherwise
        walk past a literal refusal and it is the same binary."""
        with caplog.at_level(logging.WARNING):
            parsed = self._parse({"shim_commands": ["npm", interpreter]})

        assert parsed["shim_commands"] == ["npm"]
        assert interpreter in caplog.text

    def test_make_is_merely_absent_and_an_operator_may_add_it(self):
        """The routing argument against it is real — shimming a driver inverts
        routing for everything beneath it — but it is a judgement about
        Makefiles rather than a mechanism, so the key stays open."""
        from istota.config import DEFAULT_SHIM_COMMANDS

        assert "make" not in DEFAULT_SHIM_COMMANDS
        assert self._parse({"shim_commands": ["make"]})["shim_commands"] == ["make"]

    def test_duplicates_collapse(self):
        assert self._parse({"shim_commands": ["npm", "npm"]})["shim_commands"] == ["npm"]

    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), "5", True, None]
    )
    def test_a_bad_timeout_takes_the_default(self, value):
        """`int(float("inf"))` raises OverflowError and `int(float("nan"))`
        raises ValueError, and TOML spells both."""
        assert "connect_timeout_seconds" not in self._parse(
            {"connect_timeout_seconds": value}
        )

    def test_timeouts_are_floored_rather_than_refused(self):
        parsed = self._parse(
            {"connect_timeout_seconds": 0, "idle_timeout_seconds": -1}
        )

        assert parsed["connect_timeout_seconds"] > 0
        assert parsed["idle_timeout_seconds"] >= 1

    def test_a_non_table_is_ignored(self):
        assert self._parse("devbox") == {}

    def test_the_block_loads_from_toml(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\n"
            'enabled = true\n'
            'repos_dir = "/srv/repos"\n\n'
            "[developer.container]\n"
            'shim_commands = ["npm", "cargo"]\n\n'
            "[devbox]\n"
            "enabled = true\n"
        )

        config = load_config(path)

        assert config.developer.container.shim_commands == ["npm", "cargo"]
        assert devbox_container_backend(config) is True

    def test_an_absent_block_is_the_default(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[developer]\nenabled = true\nrepos_dir = "/srv/repos"\n')

        config = load_config(path)

        assert devbox_container_backend(config) is False
