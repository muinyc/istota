"""The scheduler's periodic checks, as one table (F33).

``run_daemon`` used to state each check three times: a ``last_*`` clock seeded
about 130 lines above the loop, an ``if now - clock >= interval`` block inside
it, and — for nine of them — a re-inlined copy of the same body in
``run_scheduler``. ``build_interval_gates`` is now the one statement.

The consolidation claims no behaviour change, and two properties carry that
claim. **Every field binding is preserved**, including the four gates that read
``briefing_check_interval`` for something that is not a briefing; renaming those
config keys is an operator-visible change across four files and is deliberately
out of scope. **Every ordering is preserved**, which matters because some gates
depend on it — shared files are organized before TASKS.md is polled so the
poller finds them in place.

``EXPECTED_BINDINGS`` below was extracted mechanically from ``run_daemon``
*before* the change (``ea049668``), by matching every ``now - last_x >=`` gate
condition in source order. It is the baseline, not a restatement of the new
table: a binding silently altered while the table was written fails here.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

import pytest

from istota import scheduler as sched
from istota.config import (
    Config,
    DeveloperConfig,
    MemorySearchConfig,
    NextcloudConfig,
    SchedulerConfig,
    SecurityConfig,
    WebConfig,
)
from istota.scheduler import (
    IntervalGate,
    _run_interval_gates_once,
    _tick_interval_gates,
    build_interval_gates,
    seed_interval_clocks,
)

# (gate name, config.scheduler field). ``None`` marks a gate whose interval is
# not a config field: `travel-timezone` reads the module constant
# TRAVEL_TZ_CHECK_INTERVAL, `status-write` a literal 60, and
# `backup-stale-alert` is not an interval gate at all (it ran on every tick and
# still does — it is in the table only so its position, immediately after the
# snapshot gate, stays stated in one place).
EXPECTED_BINDINGS: list[tuple[str, str | None]] = [
    ("briefings", "briefing_check_interval"),
    ("shared-blocks", "briefing_check_interval"),
    ("briefing-triggers", "tasks_file_poll_interval"),
    ("scheduled-jobs", "briefing_check_interval"),
    ("sleep-cycles", "briefing_check_interval"),
    ("travel-timezone", None),
    ("email-poll", "email_poll_interval"),
    ("shared-files", "shared_file_check_interval"),
    ("tasks-file-poll", "tasks_file_poll_interval"),
    ("cleanup", "briefing_check_interval"),
    ("status-write", None),
    ("db-health", "db_health_check_interval"),
    ("doctor", "doctor_check_interval"),
    ("worktree-reap", "worktree_reap_interval"),
    ("sandbox-cache-sweep", "sandbox_cache_sweep_interval"),
    ("avatar-import", "avatar_import_interval"),
    ("skill-overlay-reindex", "skill_overlay_reindex_interval"),
    ("db-backup", "db_backup_interval"),
    ("backup-stale-alert", None),
    ("scheduler-stats", "scheduler_stats_interval"),
    ("host-pressure-breadcrumb", "host_pressure_breadcrumb_interval_seconds"),
    ("host-pressure-sample", "host_pressure_sample_interval_seconds"),
    ("heartbeats", "heartbeat_check_interval"),
]

# The four that read a briefing key for something that is not a briefing. Named
# here rather than merely tolerated, so a reader meeting the table knows the
# mismatch is a preserved fact and not an accident of the extraction.
KNOWN_FIELD_MISMATCHES = {
    "shared-blocks",
    "scheduled-jobs",
    "sleep-cycles",
    "cleanup",
}

# Backgrounded via `_spawn_background_check`. These names reach the log
# (`background_check_still_running name=%s`) and key the in-flight registry, so
# they are not free to change.
EXPECTED_BACKGROUND = {
    "sleep-cycles",
    "travel-timezone",
    "email-poll",
    "db-health",
    "doctor",
    "worktree-reap",
    "sandbox-cache-sweep",
    "avatar-import",
    "skill-overlay-reindex",
    "db-backup",
    "heartbeats",
}

EXPECTED_OVERLAP_EXPECTED = {
    "sleep-cycles",
    "travel-timezone",
    "email-poll",
    "heartbeats",
}

# What `run_scheduler` ran inline before the change, in its own source order.
EXPECTED_ONE_SHOT = [
    "briefings",
    "shared-blocks",
    "scheduled-jobs",
    "sleep-cycles",
    "travel-timezone",
    "email-poll",
    "shared-files",
    "tasks-file-poll",
    "heartbeats",
]


def _gates(config: Config | None = None) -> list[IntervalGate]:
    return build_interval_gates(config if config is not None else Config())


def _by_name(config: Config | None = None) -> dict[str, IntervalGate]:
    return {g.name: g for g in _gates(config)}


# ---------------------------------------------------------------------------
# The bindings
# ---------------------------------------------------------------------------


class TestTheFieldBindings:
    def test_the_table_matches_the_pre_change_bindings_in_order(self):
        """Names *and* field bindings *and* order, as one list comparison.

        Set equality would pass on a reordering, and some gates depend on the
        order — `shared-files` runs before `tasks-file-poll` so the poller finds
        the files in place.
        """
        assert [(g.name, g.field) for g in _gates()] == EXPECTED_BINDINGS

    def test_exactly_four_gates_read_the_briefing_interval_for_something_else(self):
        briefing_readers = {
            g.name for g in _gates() if g.field == "briefing_check_interval"
        }
        assert briefing_readers == {"briefings"} | KNOWN_FIELD_MISMATCHES

    def test_every_named_field_exists_on_the_scheduler_config(self):
        """The binding is `getattr(config.scheduler, field)`, so a typo would be
        an AttributeError on a tick rather than at import."""
        sched_config = SchedulerConfig()
        for gate in _gates():
            if gate.field is None:
                continue
            assert hasattr(sched_config, gate.field), gate.name

    def test_a_fieldless_gate_states_a_fixed_interval(self):
        for gate in _gates():
            if gate.field is None:
                assert gate.fixed_interval is not None, gate.name

    def test_the_interval_is_derived_from_the_field_not_restated(self):
        config = Config(scheduler=SchedulerConfig(briefing_check_interval=4242))
        by_name = {g.name: g for g in build_interval_gates(config)}
        for name in {"briefings"} | KNOWN_FIELD_MISMATCHES:
            assert by_name[name].interval(config) == 4242, name

    def test_the_travel_gate_reads_the_module_constant(self):
        assert (
            _by_name()["travel-timezone"].interval(Config())
            == sched.TRAVEL_TZ_CHECK_INTERVAL
        )

    def test_the_status_file_keeps_its_literal_sixty_seconds(self):
        assert _by_name()["status-write"].interval(Config()) == 60


class TestTheDispatchShape:
    def test_the_background_set_is_unchanged(self):
        assert {g.name for g in _gates() if g.background} == EXPECTED_BACKGROUND

    def test_the_overlap_expected_set_is_unchanged(self):
        assert {
            g.name for g in _gates() if g.overlap_expected
        } == EXPECTED_OVERLAP_EXPECTED

    def test_overlap_expected_implies_background(self):
        """It is an argument to `_spawn_background_check`; on a synchronous gate
        it would be a field nothing reads."""
        for gate in _gates():
            if gate.overlap_expected:
                assert gate.background, gate.name

    def test_the_one_shot_set_and_order_match_what_run_scheduler_ran(self):
        assert [g.name for g in _gates() if g.one_shot] == EXPECTED_ONE_SHOT


class TestTheClockSeeds:
    def test_most_gates_are_due_on_the_first_tick(self):
        config = Config()
        seeded_elsewhere = {"doctor", "scheduler-stats", "db-backup"}
        for gate in _gates(config):
            if gate.name in seeded_elsewhere:
                continue
            assert gate.seed(config) == 0.0, gate.name

    def test_the_doctor_and_stats_clocks_start_at_now(self):
        """Seeded to now, not 0. The boot doctor run already swept, and a stats
        line emitted while startup state is still hydrating is noise."""
        config = Config()
        before = time.time()
        by_name = {g.name: g for g in build_interval_gates(config)}
        for name in ("doctor", "scheduler-stats"):
            assert by_name[name].seed(config) >= before, name

    def test_the_backup_clock_is_seeded_from_the_persisted_stamp(self, monkeypatch):
        """Not 0. Without this the clock reset every boot and a host deploying
        more than once a day never backed up."""
        from istota import db_backup

        monkeypatch.setattr(db_backup, "last_backup_time", lambda config: 1234.5)
        config = Config()
        by_name = {g.name: g for g in build_interval_gates(config)}
        assert by_name["db-backup"].seed(config) == 1234.5

    def test_seed_interval_clocks_covers_every_gate_exactly_once(self):
        config = Config()
        gates = build_interval_gates(config)
        clocks = seed_interval_clocks(gates, config)
        assert sorted(clocks) == sorted(g.name for g in gates)
        assert len(clocks) == len(gates)


class TestTheEnablingConditions:
    """Everything in the gate condition that is not the clock.

    A `bool(interval)` term belongs here rather than in the interval, because an
    interval of 0 otherwise reads as "due every tick" instead of "off".
    """

    @pytest.mark.parametrize(
        "name,field",
        [
            ("doctor", "doctor_check_interval"),
            ("scheduler-stats", "scheduler_stats_interval"),
            ("host-pressure-breadcrumb", "host_pressure_breadcrumb_interval_seconds"),
            ("host-pressure-sample", "host_pressure_sample_interval_seconds"),
        ],
    )
    def test_a_zero_interval_disables_rather_than_firing_every_tick(self, name, field):
        off = Config(scheduler=SchedulerConfig(**{field: 0}))
        on = Config(scheduler=SchedulerConfig(**{field: 60}))
        gate = {g.name: g for g in build_interval_gates(off)}[name]
        assert gate.enabled(off) is False
        assert gate.enabled(on) is True

    def test_the_unconditional_gates_are_unconditional(self):
        config = Config()
        always = {
            "briefings",
            "shared-blocks",
            "briefing-triggers",
            "scheduled-jobs",
            "sleep-cycles",
            "shared-files",
            "tasks-file-poll",
            "cleanup",
            "status-write",
            "db-health",
            "heartbeats",
        }
        by_name = {g.name: g for g in build_interval_gates(config)}
        for name in always:
            assert by_name[name].enabled(config) is True, name

    def test_travel_follows_the_location_switch(self):
        gate = _by_name()["travel-timezone"]
        off = Config()
        off.location.enabled = False
        on = Config()
        on.location.enabled = True
        assert gate.enabled(off) is False
        assert gate.enabled(on) is True

    def test_email_follows_the_email_switch(self):
        gate = _by_name()["email-poll"]
        off = Config()
        off.email.enabled = False
        on = Config()
        on.email.enabled = True
        assert gate.enabled(off) is False
        assert gate.enabled(on) is True

    def test_worktree_reap_needs_all_four_terms(self, tmp_path):
        gate = _by_name()["worktree-reap"]

        def _config(**overrides):
            dev = DeveloperConfig(
                enabled=True,
                repos_dir=str(tmp_path / "repos"),
                worktree_reap_enabled=True,
            )
            for key, value in overrides.items():
                setattr(dev, key, value)
            return Config(
                developer=dev,
                scheduler=SchedulerConfig(worktree_reap_interval=60),
            )

        assert gate.enabled(_config()) is True
        assert gate.enabled(_config(enabled=False)) is False
        assert gate.enabled(_config(repos_dir="")) is False
        assert gate.enabled(_config(worktree_reap_enabled=False)) is False
        off = _config()
        off.scheduler.worktree_reap_interval = 0
        assert gate.enabled(off) is False

    def test_avatar_import_needs_web_nextcloud_and_the_switch(self):
        gate = _by_name()["avatar-import"]

        def _config(*, web=True, url="https://nc.invalid", switch=True):
            return Config(
                web=WebConfig(enabled=web, avatar_import_from_nextcloud=switch),
                nextcloud=NextcloudConfig(url=url),
                scheduler=SchedulerConfig(avatar_import_interval=60),
            )

        assert gate.enabled(_config()) is True
        assert gate.enabled(_config(web=False)) is False
        assert gate.enabled(_config(url="")) is False
        assert gate.enabled(_config(switch=False)) is False
        no_interval = _config()
        no_interval.scheduler.avatar_import_interval = 0
        assert gate.enabled(no_interval) is False

    def test_overlay_reindex_needs_memory_search_and_a_mount(self, tmp_path):
        gate = _by_name()["skill-overlay-reindex"]

        def _config(*, enabled=True, auto=True, mount=True):
            return Config(
                memory_search=MemorySearchConfig(
                    enabled=enabled, auto_index_memory_files=auto
                ),
                nextcloud_mount_path=(tmp_path / "mount") if mount else None,
                scheduler=SchedulerConfig(skill_overlay_reindex_interval=60),
            )

        assert gate.enabled(_config()) is True
        assert gate.enabled(_config(enabled=False)) is False
        assert gate.enabled(_config(auto=False)) is False
        assert gate.enabled(_config(mount=False)) is False
        no_interval = _config()
        no_interval.scheduler.skill_overlay_reindex_interval = 0
        assert gate.enabled(no_interval) is False

    def test_the_cache_sweep_needs_a_resolvable_root(self, monkeypatch, tmp_path):
        gate = _by_name()["sandbox-cache-sweep"]
        config = Config(
            security=SecurityConfig(sandbox_cache_sweep_enabled=True),
            scheduler=SchedulerConfig(sandbox_cache_sweep_interval=60),
        )
        monkeypatch.setattr(
            sched, "sandbox_cache_sweep_root", lambda c: (tmp_path, None)
        )
        assert gate.enabled(config) is True
        monkeypatch.setattr(sched, "sandbox_cache_sweep_root", lambda c: None)
        assert gate.enabled(config) is False

    @pytest.mark.parametrize("name", ["db-backup", "backup-stale-alert"])
    def test_the_backup_gates_follow_the_backup_switch(self, name):
        gate = _by_name()[name]
        on = Config(
            scheduler=SchedulerConfig(db_backup_enabled=True, db_backup_interval=60)
        )
        off = Config(
            scheduler=SchedulerConfig(db_backup_enabled=False, db_backup_interval=60)
        )
        no_interval = Config(
            scheduler=SchedulerConfig(db_backup_enabled=True, db_backup_interval=0)
        )
        assert gate.enabled(on) is True
        assert gate.enabled(off) is False
        assert gate.enabled(no_interval) is False


# ---------------------------------------------------------------------------
# The runner, driven by a fake clock
# ---------------------------------------------------------------------------


def _recording_gate(name, *, interval, **kwargs):
    seen: list[float] = []

    def _run(now: float) -> None:
        seen.append(now)

    return IntervalGate(name=name, run=_run, fixed_interval=interval, **kwargs), seen


# A fake clock has to start somewhere above the seeds, because the seeds are
# wall-clock values: a gate seeded 0.0 is "due on the first tick" only because
# the real `now` is ~1.8e9. A fake drive starting at 0.0 would make every such
# gate wait out its first interval, which is the opposite of what the seeding
# says.
CLOCK_BASE = 2_000_000.0


def _drive(gates, config, times):
    clocks = seed_interval_clocks(gates, config)
    inflight: dict[str, threading.Thread] = {}
    for now in times:
        _tick_interval_gates(gates, clocks, config, float(now), inflight)
    return clocks


class TestTheRunnerOverAFakeClock:
    def test_a_zero_seed_is_due_on_the_first_tick(self):
        """What "seeded to 0" buys, stated as behaviour rather than as a value.

        `now` is wall-clock, so a clock of 0.0 is always more than any interval
        in the past — a fresh daemon sweeps immediately instead of waiting out
        its first interval.
        """
        gate, seen = _recording_gate("day", interval=86400)
        _drive([gate], Config(), [CLOCK_BASE])
        assert seen == [CLOCK_BASE]

    def test_a_gate_fires_once_per_interval(self):
        gate, seen = _recording_gate("ten", interval=10)
        _drive([gate], Config(), [CLOCK_BASE + i for i in range(0, 51)])
        assert seen == [CLOCK_BASE + t for t in (0, 10, 20, 30, 40, 50)]

    def test_two_intervals_keep_independent_clocks(self):
        fast, fast_seen = _recording_gate("fast", interval=2)
        slow, slow_seen = _recording_gate("slow", interval=5)
        _drive([fast, slow], Config(), [CLOCK_BASE + i for i in range(0, 11)])
        assert fast_seen == [CLOCK_BASE + t for t in (0, 2, 4, 6, 8, 10)]
        assert slow_seen == [CLOCK_BASE + t for t in (0, 5, 10)]

    def test_a_zero_interval_gate_fires_on_every_tick(self):
        """`backup-stale-alert`'s shape: it ran unconditionally each tick and
        still does."""
        gate, seen = _recording_gate("every", interval=0)
        _drive([gate], Config(), [CLOCK_BASE + i for i in range(0, 5)])
        assert seen == [CLOCK_BASE + t for t in range(0, 5)]

    def test_a_disabled_gate_never_fires_and_its_clock_never_moves(self):
        gate, seen = _recording_gate("off", interval=1, enabled=lambda c: False)
        clocks = _drive([gate], Config(), [CLOCK_BASE + i for i in range(0, 10)])
        assert seen == []
        assert clocks["off"] == 0.0

    def test_a_seed_of_now_holds_the_first_fire_for_a_full_interval(self):
        gate, seen = _recording_gate("later", interval=10, seed=lambda c: 100.0)
        _drive([gate], Config(), (100.0, 105.0, 109.0, 110.0, 120.0))
        assert seen == [110.0, 120.0]

    def test_gates_run_in_table_order_within_one_tick(self):
        order: list[str] = []
        gates = [
            IntervalGate(
                name=name,
                run=lambda now, n=name: order.append(n),
                fixed_interval=0,
            )
            for name in ("a", "b", "c", "d")
        ]
        _drive(gates, Config(), [CLOCK_BASE])
        assert order == ["a", "b", "c", "d"]


def _raise(now):
    raise RuntimeError("boom")


class TestTheRunnersErrorPolicy:
    def test_a_failing_gate_is_logged_and_the_tick_continues(self, caplog):
        after: list[str] = []
        gates = [
            IntervalGate(
                name="bad",
                run=_raise,
                fixed_interval=0,
                on_error="Error checking things: %s",
            ),
            IntervalGate(
                name="after",
                run=lambda now: after.append("ran"),
                fixed_interval=0,
            ),
        ]
        with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
            _drive(gates, Config(), [CLOCK_BASE])
        assert after == ["ran"]
        assert any(
            "Error checking things: boom" in r.getMessage() for r in caplog.records
        )

    def test_the_clock_advances_even_when_the_body_raised(self):
        """What the inline gates did: `last_x = now` sat after the try/except,
        so a failing check waited out its interval rather than retrying every
        tick."""
        gate = IntervalGate(
            name="bad", run=_raise, fixed_interval=10, on_error="Error: %s"
        )
        clocks = _drive([gate], Config(), [CLOCK_BASE])
        assert clocks["bad"] == CLOCK_BASE

    def test_a_gate_with_no_on_error_propagates(self):
        """The bare `_spawn_background_check` call sites had no guard, and this
        preserves that rather than quietly widening it."""
        gate = IntervalGate(name="bare", run=_raise, fixed_interval=0)
        with pytest.raises(RuntimeError):
            _drive([gate], Config(), [CLOCK_BASE])


def _sync_spawn(recorder):
    def _spawn(name, fn, inflight, *, overlap_expected=False):
        recorder.append((name, overlap_expected))
        fn()
        return True

    return _spawn


class TestBackgroundDispatch:
    def test_a_background_gate_goes_through_spawn_background_check(self, monkeypatch):
        spawned: list[tuple[str, bool]] = []
        monkeypatch.setattr(sched, "_spawn_background_check", _sync_spawn(spawned))
        ran: list[float] = []
        gate = IntervalGate(
            name="bg",
            run=lambda now: ran.append(now),
            fixed_interval=0,
            background=True,
            overlap_expected=True,
        )
        _drive([gate], Config(), [CLOCK_BASE])
        assert spawned == [("bg", True)]
        assert ran == [CLOCK_BASE]

    def test_the_real_table_spawns_exactly_the_background_set(self, monkeypatch):
        """Names and overlap flags as they reach `_spawn_background_check`.

        Every gate is forced enabled and its body replaced with a recorder, so
        this exercises the runner's dispatch decision and nothing else.
        """
        spawned: list[tuple[str, bool]] = []
        monkeypatch.setattr(sched, "_spawn_background_check", _sync_spawn(spawned))
        config = Config()
        gates = [
            replace(g, run=lambda now: None, enabled=lambda c: True, seed=lambda c: 0.0)
            for g in build_interval_gates(config)
        ]
        _drive(gates, config, [CLOCK_BASE])
        assert {name for name, _ in spawned} == EXPECTED_BACKGROUND
        assert {
            name for name, overlap in spawned if overlap
        } == EXPECTED_OVERLAP_EXPECTED


class TestTheRealTableOverAFakeClock:
    """The whole table, driven for an hour of fake time.

    Bodies are replaced with recorders and every gate forced enabled, so what is
    under test is the cadence each row declares — not what any check does.
    """

    TICKS = 121
    STEP = 30  # one hour of fake time

    def _prepare(self, monkeypatch):
        seen: dict[str, list[float]] = {}
        order: list[str] = []

        def _make(name):
            seen[name] = []

            def _run(now, n=name):
                seen[n].append(now)
                order.append(n)

            return _run

        monkeypatch.setattr(
            sched, "_spawn_background_check", _sync_spawn([])
        )
        config = Config()
        gates = [
            replace(g, run=_make(g.name), enabled=lambda c: True)
            for g in build_interval_gates(config)
        ]
        return config, gates, seen, order

    def test_each_gate_fires_at_its_own_declared_cadence(self, monkeypatch):
        config, gates, seen, _ = self._prepare(monkeypatch)
        clocks = seed_interval_clocks(gates, config)
        for i in range(self.TICKS):
            _tick_interval_gates(
                gates, clocks, config, CLOCK_BASE + i * self.STEP, {}
            )

        for gate in gates:
            fired = seen[gate.name]
            if gate.seed(config) > 0:
                # `doctor` and `scheduler-stats` seed at wall-clock now, far
                # ahead of this fake clock, so they never come due. That is the
                # seeding those two rows exist to state.
                assert fired == [], gate.name
                continue
            expected = []
            last = gate.seed(config)
            interval = gate.interval(config)
            for i in range(self.TICKS):
                now = CLOCK_BASE + i * self.STEP
                if now - last >= interval:
                    expected.append(now)
                    last = now
            assert fired == expected, gate.name

    def test_an_hour_of_ticks_never_reorders_the_gates(self, monkeypatch):
        """Within any tick where several gates are due, table order holds.

        Checked across the whole drive rather than on one tick, because a runner
        that sorted or bucketed would still pass a single-tick check.
        """
        config, gates, _, order = self._prepare(monkeypatch)
        rank = {g.name: i for i, g in enumerate(gates)}
        clocks = seed_interval_clocks(gates, config)
        for i in range(self.TICKS):
            order.clear()
            _tick_interval_gates(
                gates, clocks, config, CLOCK_BASE + i * self.STEP, {}
            )
            ranks = [rank[n] for n in order]
            assert ranks == sorted(ranks), f"tick {i}: {order}"


# ---------------------------------------------------------------------------
# The one-shot path
# ---------------------------------------------------------------------------


def _recorded_table(config, order):
    return [
        replace(g, run=lambda now, n=g.name: order.append(n))
        for g in build_interval_gates(config)
    ]


class TestTheOneShotRunner:
    def test_it_runs_exactly_the_one_shot_gates_in_table_order(self):
        order: list[str] = []
        config = Config()
        config.location.enabled = True
        config.email.enabled = True
        _run_interval_gates_once(_recorded_table(config, order), config)
        assert order == EXPECTED_ONE_SHOT

    def test_it_honours_the_enabling_conditions(self):
        order: list[str] = []
        config = Config()
        config.location.enabled = False
        config.email.enabled = False
        _run_interval_gates_once(_recorded_table(config, order), config)
        assert order == [
            n for n in EXPECTED_ONE_SHOT if n not in {"travel-timezone", "email-poll"}
        ]

    def test_it_never_backgrounds(self, monkeypatch):
        """One-shot mode has no dispatch loop to starve, and a daemon thread
        would die with the process before it finished."""
        spawned: list[str] = []
        monkeypatch.setattr(
            sched,
            "_spawn_background_check",
            lambda name, fn, inflight, **kw: spawned.append(name),
        )
        config = Config()
        _run_interval_gates_once(_recorded_table(config, []), config)
        assert spawned == []

    def test_it_ignores_clocks_and_intervals(self):
        """A single pass runs every one-shot gate it has, whatever the interval
        says — which is what `run_scheduler` did with no clocks at all."""
        order: list[str] = []
        config = Config(scheduler=SchedulerConfig(briefing_check_interval=10**9))
        _run_interval_gates_once(_recorded_table(config, order), config)
        assert "briefings" in order

    def test_the_two_bare_one_shot_gates_still_propagate(self):
        """`check_briefings` and `check_scheduled_jobs` were unguarded in
        `run_scheduler`, so a failure there aborted the pass — while the daemon
        loop logged past both. That asymmetry is what the two error fields
        exist to preserve.
        """
        by_name = _by_name()
        for name in ("briefings", "scheduled-jobs"):
            assert by_name[name].one_shot_on_error is None, name
            assert by_name[name].on_error is not None, name

    def test_a_one_shot_gate_with_a_message_is_logged_not_raised(self, caplog):
        after: list[str] = []
        gates = [
            IntervalGate(
                name="loud",
                run=_raise,
                fixed_interval=0,
                one_shot=True,
                one_shot_on_error="Error doing the thing: %s",
            ),
            IntervalGate(
                name="after",
                run=lambda now: after.append("ran"),
                fixed_interval=0,
                one_shot=True,
            ),
        ]
        with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
            _run_interval_gates_once(gates, Config())
        assert after == ["ran"]
        assert any("Error doing the thing" in r.getMessage() for r in caplog.records)

    def test_a_one_shot_gate_with_no_message_propagates(self):
        gate = IntervalGate(
            name="bare", run=_raise, fixed_interval=0, one_shot=True
        )
        with pytest.raises(RuntimeError):
            _run_interval_gates_once([gate], Config())


# ---------------------------------------------------------------------------
# The pin: neither loop may state a gate again
# ---------------------------------------------------------------------------


class TestNeitherLoopRestatesAGate:
    """A grep-shaped guard, in the shape `tests/test_lint_scope.py` uses.

    The audit's premise is that ten prose "this is a copy of X" comments did not
    stop the copies drifting. What stops a twenty-third `if now - last_x >=`
    block from appearing in `run_daemon`, or a tenth re-inlined body in
    `run_scheduler`, is a test that fails when one does.
    """

    def _source(self, fn):
        """The function's code with `#` comments stripped.

        A comment naming the shape that was removed — "what used to be
        twenty-two `if now - last_x >= interval` blocks" — is documentation, not
        a relapse, and a raw text search cannot tell the two apart.
        """
        import inspect

        lines = []
        for line in inspect.getsource(fn).splitlines():
            head, _, _ = line.partition("#")
            lines.append(head)
        return "\n".join(lines)

    def test_the_daemon_loop_holds_no_named_clock_variables(self):
        import re

        # `pressure_state["last_alert"]` is a cooldown window, not a gate
        # clock, so the pattern is an *assignment* to a bare `last_*` local.
        source = self._source(sched.run_daemon)
        assigned = re.findall(r"^\s*(last_\w+)\s*=", source, re.MULTILINE)
        assert assigned == [], (
            f"run_daemon carries clock locals again ({assigned}) — the gate "
            "clocks live in `seed_interval_clocks`, keyed by gate name"
        )
        assert not re.search(r"now\s*-\s*last_", source), (
            "run_daemon states a gate condition again — it belongs in the table"
        )
        assert "_tick_interval_gates(" in source

    def test_the_daemon_loop_spawns_nothing_directly(self):
        """Every background check reaches `_spawn_background_check` through the
        table's `background` flag, so the loop states no spawn of its own."""
        source = self._source(sched.run_daemon)
        assert "_spawn_background_check(" not in source

    def test_the_one_shot_path_re_inlines_no_gate_body(self):
        source = self._source(sched.run_scheduler)
        assert "_run_interval_gates_once(" in source
        for name in (
            "check_briefings(",
            "check_shared_blocks(",
            "check_scheduled_jobs(",
            "_run_sleep_cycles(",
            "check_travel_timezone(",
            "_run_email_poll(",
            "discover_and_organize_shared_files(",
            "poll_all_tasks_files(",
            "_run_heartbeat_checks(",
        ):
            assert name not in source, (
                f"run_scheduler re-inlines {name} — it is a `one_shot` row in "
                "the gate table, and a second copy is how the two paths drift"
            )
