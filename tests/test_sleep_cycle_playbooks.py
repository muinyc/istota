"""Tests for sleep-cycle playbook generation (Part B, Stage 5)."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, PlaybooksConfig, SleepCycleConfig, MemorySearchConfig
from istota.memory.sleep_cycle import (
    _parse_structured_extraction,
    _playbook_slug,
    build_memory_extraction_prompt,
    cleanup_old_playbooks,
    gather_day_data,
    process_user_sleep_cycle,
    tool_summary,
)


@pytest.fixture
def pb_config(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    db.init_db(tmp_path / "test.db")
    return Config(
        db_path=tmp_path / "test.db",
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=mount,
        sleep_cycle=SleepCycleConfig(enabled=True, lookback_hours=24),
        playbooks=PlaybooksConfig(enabled=True, min_tool_calls=4),
        memory_search=MemorySearchConfig(enabled=True, auto_index_memory_files=True),
    )


def _trace(*tool_descs):
    entries = []
    for d in tool_descs:
        entries.append({"type": "tool", "text": d})
        entries.append({"type": "text", "text": "step done"})
    return json.dumps(entries)


# --- tool_summary -----------------------------------------------------------


class TestToolSummary:
    def test_counts_and_strips_emoji(self):
        tj = _trace("⚙️ gh pr create", "📄 Reading config.toml")
        count, labels = tool_summary(tj)
        assert count == 2
        assert labels[0] == "gh pr create"
        assert labels[1] == "Reading config.toml"

    def test_empty_or_malformed(self):
        assert tool_summary(None) == (0, [])
        assert tool_summary("not json") == (0, [])
        assert tool_summary("{}") == (0, [])

    def test_truncates_long_label(self):
        long = "⚙️ " + "x" * 200
        count, labels = tool_summary(json.dumps([{"type": "tool", "text": long}]))
        assert count == 1
        assert len(labels[0]) <= 80

    def test_prefers_raw_invocation_over_description(self):
        """ISSUE-174 fix 1: the verbatim command in `raw` wins over the
        paraphrased `text` so the extraction LLM sees the real invocation."""
        tj = json.dumps([{
            "type": "tool",
            "text": "Extract the Le Shrub newsletter",
            "raw": "python scripts/newsletter_extract.py clean --input inbox/x.eml",
        }])
        count, labels = tool_summary(tj)
        assert count == 1
        assert labels[0] == "python scripts/newsletter_extract.py clean --input inbox/x.eml"

    def test_raw_label_allows_longer_command(self):
        """Raw commands get a larger truncation budget than descriptions so a
        full script invocation survives whole."""
        raw = "python scripts/foo.py " + "--flag value " * 12
        tj = json.dumps([{"type": "tool", "text": "run foo", "raw": raw.strip()}])
        _c, labels = tool_summary(tj)
        assert labels[0].startswith("python scripts/foo.py --flag value")
        assert len(labels[0]) > 80


# --- _parse_structured_extraction (PLAYBOOKS section) -----------------------


class TestParsePlaybooks:
    def test_parses_playbooks_section(self):
        output = (
            "MEMORIES:\n- did a thing (2026-06-10)\n\n"
            "FACTS:\n[]\n\n"
            "TOPICS:\n{}\n\n"
            "PLAYBOOKS:\n"
            '[{"title": "Open a GitHub PR", "triggers": ["pr", "pull request"], '
            '"steps": "1. clone\\n2. gh pr create"}]'
        )
        memories, facts, topics, playbooks = _parse_structured_extraction(output)
        assert "did a thing" in memories
        assert len(playbooks) == 1
        assert playbooks[0]["title"] == "Open a GitHub PR"

    def test_missing_playbooks_section_yields_empty(self):
        output = "MEMORIES:\n- thing\n\nFACTS:\n[]\n\nTOPICS:\n{}"
        _m, _f, _t, playbooks = _parse_structured_extraction(output)
        assert playbooks == []

    def test_malformed_playbooks_yields_empty(self):
        output = "MEMORIES:\n- thing\n\nPLAYBOOKS:\nnot valid json"
        _m, _f, _t, playbooks = _parse_structured_extraction(output)
        assert playbooks == []

    def test_invalid_playbook_entries_dropped(self):
        output = (
            "MEMORIES:\n- thing\n\nPLAYBOOKS:\n"
            '[{"title": "ok", "steps": "do it"}, {"title": "", "steps": "x"}, {"title": "no steps"}]'
        )
        _m, _f, _t, playbooks = _parse_structured_extraction(output)
        assert len(playbooks) == 1
        assert playbooks[0]["title"] == "ok"


# --- prompt content ---------------------------------------------------------


class TestExtractionPrompt:
    def test_playbooks_section_present_when_enabled(self):
        prompt = build_memory_extraction_prompt(
            "alice", "data", None, "2026-06-10",
            playbooks_enabled=True, min_tool_calls=4,
        )
        assert "PLAYBOOKS:" in prompt
        assert "at least 4 tool calls" in prompt
        assert "never ships code to run" in prompt

    def test_prompt_instructs_verified_commands(self):
        """ISSUE-174 Concern 1/4: prompt must tell the model to quote the exact
        verified invocation from the Tools line, not paraphrase a script."""
        prompt = build_memory_extraction_prompt(
            "alice", "data", None, "2026-06-10",
            playbooks_enabled=True, min_tool_calls=4,
        )
        low = prompt.lower()
        # Must reference the Tools line as the source of truth for commands.
        assert "tools (" in low
        assert "verbatim" in low or "exactly as" in low
        # Must carry the script-router rule (don't re-narrate a single script).
        assert "re-narrate" in low or "internal steps" in low

    def test_playbooks_section_absent_when_disabled(self):
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-06-10")
        assert "PLAYBOOKS:" not in prompt

    def test_gather_day_data_includes_tool_summary(self, pb_config):
        with db.get_db(pb_config.db_path) as conn:
            t = db.create_task(conn, prompt="deploy", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(
                conn, t, "completed", result="done",
                execution_trace=_trace("⚙️ a", "⚙️ b", "📄 c", "📝 d", "🔍 e"),
            )
            data = gather_day_data(pb_config, conn, "alice", 24, None)
        assert "Tools (5):" in data


# --- slug -------------------------------------------------------------------


class TestPlaybookSlug:
    def test_slugify(self):
        assert _playbook_slug("Open a GitHub PR!") == "open-a-github-pr"
        assert _playbook_slug("   ") == "playbook"


# --- process_user_sleep_cycle ----------------------------------------------


_PB_OUTPUT = (
    "MEMORIES:\n- Opened a PR for the auth fix (2026-06-10, ref:1)\n\n"
    "FACTS:\n[]\n\nTOPICS:\n{}\n\n"
    "PLAYBOOKS:\n"
    '[{"title": "Open a GitHub PR via developer skill", '
    '"triggers": ["pr", "pull request", "merge"], '
    '"steps": "1. Use the developer skill\\n2. gh pr create\\n3. paste the URL back"}]'
)


class TestProcessWritesPlaybooks:
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_writes_and_indexes_playbook(self, mock_run, pb_config):
        mock_run.return_value = (True, _PB_OUTPUT)
        with db.get_db(pb_config.db_path) as conn:
            t = db.create_task(conn, prompt="open a PR", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(
                conn, t, "completed", result="PR opened",
                execution_trace=_trace("⚙️ a", "⚙️ b", "📄 c", "📝 d", "🔍 e"),
            )
            assert process_user_sleep_cycle(pb_config, conn, "alice") is True

            pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
            files = list(pb_dir.glob("*.md"))
            assert len(files) == 1
            text = files[0].read_text()
            assert "Open a GitHub PR via developer skill" in text
            assert "source: sleep_cycle" in text
            assert "gh pr create" in text

            # Indexed as source_type=playbook.
            rows = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE user_id=? AND source_type='playbook'",
                ("alice",),
            ).fetchone()
            assert rows[0] >= 1

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_disabled_writes_nothing(self, mock_run, pb_config):
        pb_config.playbooks.enabled = False
        mock_run.return_value = (True, _PB_OUTPUT)
        with db.get_db(pb_config.db_path) as conn:
            t = db.create_task(conn, prompt="open a PR", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="PR opened", execution_trace=_trace("⚙️ a"))
            process_user_sleep_cycle(pb_config, conn, "alice")
        pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
        assert not pb_dir.exists()

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_duplicate_updates_in_place(self, mock_run, pb_config):
        mock_run.return_value = (True, _PB_OUTPUT)
        for _ in range(2):
            with db.get_db(pb_config.db_path) as conn:
                t = db.create_task(conn, prompt="open a PR", user_id="alice")
                db.update_task_status(conn, t, "running")
                db.update_task_status(
                    conn, t, "completed", result="PR opened",
                    execution_trace=_trace("⚙️ a", "⚙️ b", "📄 c", "📝 d"),
                )
                process_user_sleep_cycle(pb_config, conn, "alice")
        pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
        assert len(list(pb_dir.glob("*.md"))) == 1

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_pinned_playbook_not_overwritten(self, mock_run, pb_config):
        """ISSUE-174 Concern 1: a hand-corrected `pinned: true` playbook survives
        the next same-title re-derivation instead of being clobbered."""
        mock_run.return_value = (True, _PB_OUTPUT)
        pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
        pb_dir.mkdir(parents=True)
        # Slug of the _PB_OUTPUT title.
        slug = _playbook_slug("Open a GitHub PR via developer skill")
        pinned = pb_dir / f"{slug}.md"
        pinned.write_text(
            "---\ntitle: Open a GitHub PR via developer skill\n"
            "pinned: true\nsource: human\n---\n\n"
            "# Open a GitHub PR via developer skill\n\nHUMAN CORRECTED STEP\n"
        )

        with db.get_db(pb_config.db_path) as conn:
            t = db.create_task(conn, prompt="open a PR", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(
                conn, t, "completed", result="PR opened",
                execution_trace=_trace("⚙️ a", "⚙️ b", "📄 c", "📝 d"),
            )
            process_user_sleep_cycle(pb_config, conn, "alice")

        text = pinned.read_text()
        assert "HUMAN CORRECTED STEP" in text
        assert "gh pr create" not in text


    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_pinned_playbook_reindexes_corrected_content(self, mock_run, pb_config):
        """ISSUE-174 fix 2 (Mulder/Scully): recall reads memory_chunks, so a
        pinned correction must be RE-INDEXED (not merely left on disk) or the
        model keeps getting the stale chunk. The pin skips the write but
        refreshes the index from the human-corrected file."""
        mock_run.return_value = (True, _PB_OUTPUT)
        pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
        pb_dir.mkdir(parents=True)
        slug = _playbook_slug("Open a GitHub PR via developer skill")
        pinned = pb_dir / f"{slug}.md"
        pinned.write_text(
            "---\ntitle: Open a GitHub PR via developer skill\n"
            "triggers: [pr]\npinned: true\nsource: human\n---\n\n"
            "# Open a GitHub PR via developer skill\n\nCORRECTED_INVOCATION_MARKER\n"
        )
        with db.get_db(pb_config.db_path) as conn:
            t = db.create_task(conn, prompt="open a PR", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(
                conn, t, "completed", result="PR opened",
                execution_trace=_trace("⚙️ a", "⚙️ b", "📄 c", "📝 d"),
            )
            process_user_sleep_cycle(pb_config, conn, "alice")

            rows = conn.execute(
                "SELECT content FROM memory_chunks WHERE user_id=? AND source_type='playbook'",
                ("alice",),
            ).fetchall()
        blob = "\n".join(r[0] for r in rows)
        assert "CORRECTED_INVOCATION_MARKER" in blob
        assert "gh pr create" not in blob
        # Frontmatter noise must not pollute the searchable index (Scully wart).
        assert "pinned: true" not in blob
        assert "source: human" not in blob


class TestPlaybookSearchableBody:
    def test_strips_frontmatter(self):
        from istota.memory.sleep_cycle import _playbook_searchable_body

        text = "---\ntitle: t\npinned: true\nsource: human\n---\n\n# t\n\nCMD here\n"
        body = _playbook_searchable_body(text)
        assert body == "# t\n\nCMD here"
        assert "pinned" not in body

    def test_no_frontmatter_returns_text(self):
        from istota.memory.sleep_cycle import _playbook_searchable_body

        assert _playbook_searchable_body("# just a body\n\nsteps") == "# just a body\n\nsteps"


class TestPlaybooksConfigDefaults:
    def test_retention_defaults_to_ninety_days(self):
        """ISSUE-174 Concern 3: retention window on by default (use-based prune)."""
        assert PlaybooksConfig().retention_days == 90


class TestPinnedFrontmatterParsing:
    def test_variants(self, tmp_path):
        from istota.memory.sleep_cycle import _playbook_is_pinned

        def pinned(text):
            p = tmp_path / "x.md"
            p.write_text(text)
            return _playbook_is_pinned(p)

        # Positive variants
        assert pinned("---\ntitle: t\npinned: true\n---\n\nbody")
        assert pinned("---\npinned: True\n---\nbody")
        assert pinned('---\npinned: "true"\n---\nbody')
        assert pinned("---\npinned: true  # human fix\n---\nbody")
        assert pinned("﻿---\npinned: true\n---\nbody")  # BOM
        assert pinned("\n---\npinned: true\n---\nbody")  # leading blank line
        # Negative variants
        assert not pinned("---\ntitle: t\npinned: false\n---\nbody")
        assert not pinned("---\ntitle: t\n---\n\npinned: true in the body")
        assert not pinned("no frontmatter\npinned: true")
        # No closing fence → not a valid frontmatter block (was a false positive)
        assert not pinned("---\npinned: true\nbody with no close fence")


# --- cleanup ----------------------------------------------------------------


_RETENTION_SENTINEL = ".retention_initialized"


def _pb_dir(pb_config):
    d = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_sentinel(pb_dir):
    """Bypass the grandfather one-shot so tests exercise the steady-state prune."""
    (pb_dir / _RETENTION_SENTINEL).write_text("2026-07-19")


def _backdate(path, days):
    import os
    past = time.time() - days * 86400
    os.utime(path, (past, past))


class TestCleanupPlaybooks:
    def test_retention_zero_keeps_all(self, pb_config):
        pb_dir = _pb_dir(pb_config)
        (pb_dir / "old.md").write_text("x")
        assert cleanup_old_playbooks(pb_config, "alice", 0) == 0
        assert (pb_dir / "old.md").exists()

    def test_prunes_old_files(self, pb_config):
        pb_dir = _pb_dir(pb_config)
        _seed_sentinel(pb_dir)
        old = pb_dir / "old.md"
        old.write_text("x")
        _backdate(old, 40)
        fresh = pb_dir / "fresh.md"
        fresh.write_text("y")

        assert cleanup_old_playbooks(pb_config, "alice", 30) == 1
        assert not old.exists()
        assert fresh.exists()

    def test_grandfather_first_run_deletes_nothing(self, pb_config):
        """ISSUE-174 (Mulder finding 5): the first prune after upgrade must not
        delete playbooks by stale write-mtime before any recall history exists.
        It refreshes mtimes, writes the sentinel, and prunes nothing."""
        pb_dir = _pb_dir(pb_config)
        old = pb_dir / "old.md"
        old.write_text("x")
        _backdate(old, 400)

        assert cleanup_old_playbooks(pb_config, "alice", 30) == 0
        assert old.exists()
        assert (pb_dir / _RETENTION_SENTINEL).exists()
        # mtime was refreshed to ~now, so the next run won't prune it either.
        assert old.stat().st_mtime > time.time() - 86400

    def test_pinned_file_never_pruned(self, pb_config):
        """ISSUE-174 (Mulder finding 4): a pinned human correction must survive
        the retention clock even when idle."""
        pb_dir = _pb_dir(pb_config)
        _seed_sentinel(pb_dir)
        pinned = pb_dir / "keep.md"
        pinned.write_text("---\npinned: true\n---\n\nbody")
        _backdate(pinned, 400)

        assert cleanup_old_playbooks(pb_config, "alice", 30) == 0
        assert pinned.exists()

    def test_prune_deletes_orphaned_chunks(self, pb_config):
        """ISSUE-174 (Mulder finding 2): pruning must delete the playbook's
        memory_chunks too, or recall keeps serving deleted guidance."""
        from istota.memory.search import index_file

        pb_dir = _pb_dir(pb_config)
        _seed_sentinel(pb_dir)
        stale = pb_dir / "stale.md"
        stale.write_text("# Stale\n\nORPHAN_MARKER content")
        _backdate(stale, 400)

        with db.get_db(pb_config.db_path) as conn:
            index_file(conn, "alice", str(stale), "ORPHAN_MARKER content", "playbook")
            before = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE user_id=? AND source_id=?",
                ("alice", str(stale)),
            ).fetchone()[0]
            assert before >= 1

            deleted = cleanup_old_playbooks(pb_config, "alice", 30, conn=conn)
            assert deleted == 1
            after = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE user_id=? AND source_id=?",
                ("alice", str(stale)),
            ).fetchone()[0]
        assert not stale.exists()
        assert after == 0


class TestThePruneFailsClosed:
    """ISSUE-430: turning retention on for Docker made two paths reachable.

    `playbooks.retention_days` was 0 on the Docker render, so `cleanup_old_playbooks`
    returned before doing anything on that shape. Raising it to 90 to match every
    other install arms the prune there for the first time, and a delete path that
    is newly reachable has to fail in the safe direction.
    """

    def test_an_unreadable_playbook_is_not_deleted(self, pb_config):
        """The pin check fails open, and the prune's next statement is unlink.

        `_playbook_is_pinned` answers False for an unreadable file, which is
        right for the re-derivation caller (the cost is a regenerated playbook)
        and wrong for this one. The prune loop's own `except OSError` cannot
        catch it, because the error is swallowed one frame down — so a transient
        read failure on the shared mount deleted a human-pinned playbook
        permanently, reported as nothing but a count.
        """
        pb_dir = _pb_dir(pb_config)
        _seed_sentinel(pb_dir)
        pinned = pb_dir / "keep.md"
        pinned.write_text("---\npinned: true\n---\n\nbody")
        _backdate(pinned, 400)

        real_read_text = Path.read_text

        def _fail_for_our_file(self, *a, **kw):
            if self.name == "keep.md":
                raise OSError("transient mount read failure")
            return real_read_text(self, *a, **kw)

        with patch.object(Path, "read_text", _fail_for_our_file):
            deleted = cleanup_old_playbooks(pb_config, "alice", 30)

        assert deleted == 0
        assert pinned.exists(), (
            "an unreadable playbook was deleted; the pin check must fail closed "
            "on the prune path"
        )

    def test_a_readable_unpinned_file_is_still_pruned(self, pb_config):
        """The control for the test above.

        Failing closed on a read error must not turn into failing closed on
        everything, which would make the prune a no-op and the test above pass
        for the wrong reason.
        """
        pb_dir = _pb_dir(pb_config)
        _seed_sentinel(pb_dir)
        old = pb_dir / "old.md"
        old.write_text("no frontmatter here")
        _backdate(old, 400)

        assert cleanup_old_playbooks(pb_config, "alice", 30) == 1
        assert not old.exists()


class TestTheGrandfatherDoesNotArmAPruneItCouldNotPrepare:
    """ISSUE-430: the sentinel used to be written whatever the refresh did.

    The grandfather pass refreshes every playbook's mtime so the first prune
    after retention is switched on deletes nothing. On a mount that rejects
    `utimens` — named in this module and in `executor._recall_playbooks` as a
    real environment — the refresh fails per file, and the sentinel was written
    anyway. The guarantee then held for exactly one cycle: the *second* cycle
    found the sentinel, skipped the grandfather, and pruned by stale write-mtime.
    """

    def test_a_failed_refresh_leaves_the_sentinel_unwritten(self, pb_config):
        pb_dir = _pb_dir(pb_config)
        old = pb_dir / "old.md"
        old.write_text("x")
        _backdate(old, 400)

        with patch("istota.memory.sleep_cycle.os.utime", side_effect=OSError("EPERM")):
            assert cleanup_old_playbooks(pb_config, "alice", 30) == 0

        assert old.exists()
        assert not (pb_dir / _RETENTION_SENTINEL).exists(), (
            "the sentinel was written though no mtime could be refreshed, so the "
            "next cycle would prune by stale write-mtime"
        )

    def test_the_next_cycle_still_deletes_nothing(self, pb_config):
        """The property the test above exists to protect, run forward one cycle.

        Without the fix this second call prunes the file: the sentinel is there,
        the grandfather is skipped, and the mtime is still 400 days old.
        """
        pb_dir = _pb_dir(pb_config)
        old = pb_dir / "old.md"
        old.write_text("x")
        _backdate(old, 400)

        with patch("istota.memory.sleep_cycle.os.utime", side_effect=OSError("EPERM")):
            cleanup_old_playbooks(pb_config, "alice", 30)
            assert cleanup_old_playbooks(pb_config, "alice", 30) == 0

        assert old.exists()

    def test_a_successful_refresh_still_writes_the_sentinel(self, pb_config):
        """The control: the guard must not disable the grandfather outright."""
        pb_dir = _pb_dir(pb_config)
        old = pb_dir / "old.md"
        old.write_text("x")
        _backdate(old, 400)

        assert cleanup_old_playbooks(pb_config, "alice", 30) == 0
        assert (pb_dir / _RETENTION_SENTINEL).exists()
        assert old.stat().st_mtime > time.time() - 86400
