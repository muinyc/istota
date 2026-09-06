"""The scheduler pass that keeps per-skill overlays searchable (ISSUE-343).

An overlay is a file the *user* writes, so there is no write path to index
from. The memory CLI's per-write reindex went with the overlay write verbs, and
it never covered the authoring mode the file is actually for — a text-editor
edit over Nextcloud called no CLI and was never indexed. A periodic full
directory pass is the only seam that sees every route.

It went into `process_user_sleep_cycle` first. Review found the gate one level
up, and `test_it_does_not_depend_on_the_sleep_cycle` is the assertion that
records why it moved: `check_sleep_cycles` returns before doing anything when
`sleep_cycle.enabled` is false, and again when the primary brain's breaker is
open, and a reindex that makes no brain call has no business behind either.
"""

from __future__ import annotations

from unittest.mock import patch

from istota import db
from istota.config import Config, MemorySearchConfig, SchedulerConfig, UserConfig


SKILLS = ("developer", "notes")


def _config(tmp_path, **overrides):
    bundled = tmp_path / "bundled"
    for skill in SKILLS:
        d = bundled / skill
        d.mkdir(parents=True, exist_ok=True)
        (d / "skill.md").write_text(
            f"---\nname: {skill}\ndescription: the {skill} skill\n---\n\n# {skill}\n"
        )
    ops = tmp_path / "ops_skills"
    ops.mkdir(exist_ok=True)
    db.init_db(tmp_path / "test.db")
    return Config(
        db_path=tmp_path / "test.db",
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=tmp_path / "mount",
        bundled_skills_dir=bundled,
        skills_dir=ops,
        users={"alice": UserConfig()},
        memory_search=MemorySearchConfig(enabled=True, auto_index_memory_files=True),
        **overrides,
    )


def _overlays(config, user_id="alice"):
    d = (
        config.nextcloud_mount_path
        / "Users" / user_id / config.bot_dir_name / "config" / "skills"
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(config):
    from istota.scheduler import check_skill_overlay_reindex

    with patch("istota.memory.search.ensure_vec_table", return_value=False), \
         patch("istota.memory.search.enable_vec_extension", return_value=False):
        return check_skill_overlay_reindex(config)


def _rows(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT source_id, content FROM memory_chunks "
            "WHERE source_type = 'skill_overlay'"
        ).fetchall()


class TestTheIndexingPass:
    def test_a_hand_edited_overlay_becomes_searchable(self, tmp_path):
        config = _config(tmp_path)
        (_overlays(config) / "developer.md").write_text(
            "- The full suite takes about an hour on this host.\n"
        )
        assert _run(config) == ["alice"]
        rows = _rows(config)
        assert len(rows) == 1
        assert rows[0][0].endswith("config/skills/developer.md")
        assert "about an hour" in rows[0][1]

    def test_a_deleted_overlay_loses_its_rows(self, tmp_path):
        # `skill_overlay` is outside EPHEMERAL_SOURCE_TYPES, so nothing else
        # ever reclaims these rows; without this pass a rule the user deleted
        # stays searchable forever.
        config = _config(tmp_path)
        overlay = _overlays(config) / "developer.md"
        overlay.write_text("- A rule that is about to be deleted.\n")
        _run(config)
        assert len(_rows(config)) == 1

        overlay.unlink()
        _run(config)
        assert _rows(config) == []

    def test_an_edit_replaces_rather_than_accumulates(self, tmp_path):
        config = _config(tmp_path)
        overlay = _overlays(config) / "developer.md"
        overlay.write_text("- first version of the rule\n")
        _run(config)
        overlay.write_text("- second version of the rule\n")
        _run(config)

        rows = _rows(config)
        assert len(rows) == 1
        assert "second version" in rows[0][1]
        assert "first version" not in rows[0][1]

    def test_an_overlay_that_does_not_bind_is_not_indexed(self, tmp_path):
        # Indexing a file that reaches no prompt would have `!search` return a
        # rule that is not in force anywhere.
        config = _config(tmp_path)
        (_overlays(config) / "develper.md").write_text("- a typo'd file\n")
        assert _run(config) == []
        assert _rows(config) == []

    def test_every_configured_user_is_swept(self, tmp_path):
        config = _config(tmp_path)
        config.users = {"alice": UserConfig(), "bob": UserConfig()}
        (_overlays(config, "alice") / "developer.md").write_text("- alice's rule\n")
        (_overlays(config, "bob") / "notes.md").write_text("- bob's rule\n")

        assert sorted(_run(config)) == ["alice", "bob"]
        bodies = " ".join(r[1] for r in _rows(config))
        assert "alice's rule" in bodies and "bob's rule" in bodies


class TestTheGates:
    def test_it_does_not_depend_on_the_sleep_cycle(self, tmp_path):
        """The finding that moved this out of `process_user_sleep_cycle`.

        `memory_search.enabled` and `sleep_cycle.enabled` are independent
        settings, so search-on / sleep-cycle-off is a supported deployment. Run
        from inside the sleep cycle, that deployment indexed no overlay at all,
        where index-on-write had.
        """
        from istota.config import SleepCycleConfig

        config = _config(tmp_path, sleep_cycle=SleepCycleConfig(enabled=False))
        (_overlays(config) / "developer.md").write_text("- a rule\n")
        assert _run(config) == ["alice"]
        assert len(_rows(config)) == 1

    def test_it_does_not_consult_the_brain_breaker(self, tmp_path):
        """The other half of the same finding: this pass makes no model call,
        so a usage-limit cooldown must not cost it a run. Asserted by making
        the breaker report unavailable and requiring the pass to proceed —
        `check_sleep_cycles` returns `[]` under exactly this condition."""
        config = _config(tmp_path)
        (_overlays(config) / "developer.md").write_text("- a rule\n")

        with patch(
            "istota.brain._fallback.primary_brain_unavailable",
            return_value=(False, "usage_limit"),
        ):
            assert _run(config) == ["alice"]
        assert len(_rows(config)) == 1

    def test_it_is_skipped_when_indexing_is_off(self, tmp_path):
        config = _config(tmp_path)
        config.memory_search = MemorySearchConfig(
            enabled=True, auto_index_memory_files=False
        )
        (_overlays(config) / "developer.md").write_text("- a rule\n")
        assert _run(config) == []
        assert _rows(config) == []

    def test_a_mountless_deployment_is_a_no_op_not_a_crash(self, tmp_path):
        """`nextcloud_mount_path` is None on an rclone-remote deployment, and
        `None / "Users/…"` is a TypeError. On a scheduler cadence that raise
        would be swallowed by the pass's own `except Exception` and reported
        nowhere, so it is guarded rather than caught."""
        config = _config(tmp_path)
        config.nextcloud_mount_path = None
        assert config.use_mount is False
        assert _run(config) == []

    def test_one_users_failure_does_not_cost_the_others(self, tmp_path):
        config = _config(tmp_path)
        config.users = {"alice": UserConfig(), "bob": UserConfig()}
        (_overlays(config, "bob") / "notes.md").write_text("- bob's rule\n")

        from istota.memory import search as search_mod
        real = search_mod.reindex_skill_overlays

        def _boom(conn, cfg, user_id):
            if user_id == "alice":
                raise RuntimeError("alice's tree is unreadable")
            return real(conn, cfg, user_id)

        with patch.object(search_mod, "reindex_skill_overlays", _boom):
            assert _run(config) == ["bob"]
        assert len(_rows(config)) == 1

    def test_it_never_raises(self, tmp_path):
        """It runs on a scheduler thread; an escape takes the check down."""
        config = _config(tmp_path)
        config.db_path = tmp_path / "nonexistent" / "missing.db"
        assert _run(config) == []


class TestTheSchedulerWiring:
    def test_the_interval_defaults_to_six_hours(self):
        assert SchedulerConfig().skill_overlay_reindex_interval == 21600

    def test_a_zero_interval_disables_the_tick(self, tmp_path):
        """Read off the loop's gate rather than asserted about: a 0 interval
        must skip the spawn, not spawn something that returns early.

        The gate is a row in the scheduler's interval table (F33), so this reads
        the row — the field it binds and the predicate that decides whether it
        runs at all — instead of the loop's source text.
        """
        from istota import scheduler

        gate = {
            g.name: g for g in scheduler.build_interval_gates(_config(tmp_path))
        }["skill-overlay-reindex"]
        assert gate.field == "skill_overlay_reindex_interval"
        assert gate.background

        off = _config(
            tmp_path, scheduler=SchedulerConfig(skill_overlay_reindex_interval=0)
        )
        on = _config(
            tmp_path, scheduler=SchedulerConfig(skill_overlay_reindex_interval=60)
        )
        assert gate.enabled(off) is False
        assert gate.enabled(on) is True
