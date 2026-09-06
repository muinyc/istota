"""What `poll_talk_conversations` does to the framework database while it waits
on Nextcloud (ISSUE-406).

The poller opens a synchronous `sqlite3` connection and awaits the network
inside it. `db.get_db` commits at the end of the `with`, and SQLite's default
deferred transaction starts at the first write — so any write in the room loop
takes a WAL write lock that is then held across every later await in the same
block. Every other writer in the daemon queues behind it for as long as
Nextcloud takes to answer.

That much is a reading of the code. Whether it *happens*, and for how long, was
inference when the issue was filed, and the issue says so: measure first, and
decide the restructure on the numbers. This file covers the measurement.

**The room half has since been fixed and its cases moved.** The measurement
established what the issue could not: the room loop's awaits recur rather than
being first-encounter work, so an empty group room paid two round trips of lock
time on every cycle for ever. That block is now read → close → await → reopen
to write, and `tests/test_talk_poll_room_txn_split.py` is where the room pass is
covered — including the recurrence finding, which it keeps by counting the round
trips rather than by measuring the lock they no longer hold. What is left here is
the `results` block, which cannot be split the same way (`ingest_message` has to
commit in the same transaction as the cursor advance) and so still holds every
await this instrument was built to count.

**These tests assert on a log line because the log line is the deliverable.**
`talk_poll_txn` is a data format, not chatter: fixed key order, `key=value`, on
its own logger, so a series can be pulled out of the journal whole and lined up
against the `ReadTimeout` records the issue is trying to explain. A rename is a
breaking change.

**These do not use `fake_talk`**, which is otherwise the rule for anything
patching `get_talk_client` (`.claude/rules/testbed.md`). The subject here is how
long a call takes, so each method needs a delay under the test's control, and
the double has no way to express one; nothing here asserts on a token, so the
misroute the double exists to catch is not in scope. Adding per-method delays to
the double for this would be the better answer if a second file ever wants them.

**A case asserting that a threshold was *not* crossed does not use the host's
clock** (ISSUE-442). Both of `_report_poll_txn`'s arms are wall-clock
comparisons — a single await past `_TXN_AWAIT_FLOOR_SECONDS`, or a hold past
`_TXN_HOLD_WARN_SECONDS` — so on a loaded machine either can fire on evidence
about the host rather than about the poll. Both were observed doing it, and that
is the failure this issue was finally left open for.

The split is by what a case is asserting, not by which class it is in. A case
that wants a delay *measured* keeps the real clock and a real `asyncio.sleep`,
because that delay is its subject. A case that wants a delay *not* measured
takes `_FakeClock`, which makes "a warm hit is cheap" a fact of the test rather
than a hope about the host — and additionally pins `_TXN_HOLD_WARN_SECONDS`
where the hold is long enough to reach it, which is the 300-message case alone.
The counting rule all three quiet cases depend on is pinned directly, without
going through the poller, in `TestTheFloorIsPerAwaitAndNotOnTheTotal`.
"""

import asyncio
import logging
import time

import pytest
from unittest.mock import AsyncMock, patch

from istota import db
from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.transport.talk import inbound as poller
from istota.transport.talk.inbound import poll_talk_conversations


@pytest.fixture(autouse=True)
def _reset_poller_caches():
    poller._participant_cache.clear()
    poller._conversation_cache = None
    poller._dm_token_cache.clear()
    poller._last_full_sweep = None
    yield
    poller._participant_cache.clear()
    poller._conversation_cache = None
    poller._dm_token_cache.clear()
    poller._last_full_sweep = None


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    config = Config()
    config.db_path = db_path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.talk = TalkConfig(enabled=True, bot_username="istota")
    config.nextcloud = NextcloudConfig(
        url="https://nc.test", username="istota", app_password="pass",
    )
    config.users = {"alice": UserConfig()}
    config.scheduler = SchedulerConfig()
    return config


def _msg(msg_id=100, actor_id="alice", message="hello"):
    return {
        "id": msg_id,
        "actorId": actor_id,
        "actorType": "users",
        "message": message,
        "messageType": "comment",
        "messageParameters": {},
        "timestamp": 1700000000,
    }


async def _poll(config, *, conversations, messages, participants=None,
                participants_delay=0.0, history=None, history_delay=0.0,
                latest_id=None, latest_delay=0.0):
    """Drive one poll cycle with each network call's cost under the test's control."""

    async def _participants(_token):
        if participants_delay:
            await asyncio.sleep(participants_delay)
        return participants or []

    async def _history(_token, **_kw):
        if history_delay:
            await asyncio.sleep(history_delay)
        return history or []

    async def _latest(_token):
        if latest_delay:
            await asyncio.sleep(latest_delay)
        return latest_id

    with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
        instance = MockClient.return_value
        instance.list_conversations = AsyncMock(return_value=conversations)
        instance.poll_messages = AsyncMock(return_value=messages)
        instance.send_message = AsyncMock()
        instance.get_participants = _participants
        instance.fetch_chat_history = _history
        instance.get_latest_message_id = _latest
        return await poll_talk_conversations(config)


def _txn_lines(caplog):
    return [
        r for r in caplog.records
        if r.name == "istota.transport.talk.txn"
        and r.getMessage().startswith("talk_poll_txn ")
    ]


def _fields(record) -> dict[str, str]:
    parts = record.getMessage().split()[1:]
    return dict(p.split("=", 1) for p in parts)


class _FakeClock:
    """Stand-in for the `time` module inside the poller's namespace, so an
    await's measured cost is the test's to decide rather than the host's.

    `monotonic` advances `step` per call and nothing else moves it, so an await
    is charged `step` times one plus however many clock reads happen inside it —
    `_get_participants` makes one, checking its cache TTL. That is why `step` is
    set well below `_TXN_AWAIT_FLOOR_SECONDS` rather than just under it: the
    multiplier is a property of the call graph, and a caller should not have to
    track it to stay under the floor.

    **Substituted for the module reference in `inbound`, never for
    `time.monotonic` itself.** The running event loop reads that same function,
    and a process-wide patch hands the loop a clock that jumps. Everything the
    poller reads other than `monotonic` delegates to the real module.

    The two cache TTLs it also feeds (`_participant_cache`, `_last_full_sweep`)
    are stamped on the real clock by whatever ran before the patch, so under the
    fake they read as hugely negative ages: a warm cache stays warm and a sweep
    stays due, which is what both call sites want here. A case that depends on
    the first of those asserts it rather than resting on it — an entry the poller
    refilled carries a new timestamp.

    Every other attribute delegates to the real module, **except a clock the
    poller does not read today**. `monotonic` is the only one it reads, and a
    later `perf_counter` or `monotonic_ns` added beside it would silently take
    the real clock and quietly make this class a no-op for that call site. There
    is no way to keep such a reader honest by delegating, so it is refused.
    """

    _REFUSED = ("monotonic_ns", "perf_counter", "perf_counter_ns", "process_time")

    def __init__(self, step: float):
        self.step = step
        self.now = 0.0

    def monotonic(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def __getattr__(self, name):
        if name in self._REFUSED:
            raise AttributeError(
                f"the poller read time.{name}, which _FakeClock does not "
                f"control — extend it rather than measuring that call on the "
                f"host's clock (ISSUE-442)"
            )
        return getattr(time, name)


def _settled_room(config, token="room1"):
    """A room the poller has seen before: registered, with a cursor and cached
    history.

    All three matter. Without the registry row the room loop takes the
    first-sight branch and fetches participants *there*, which warms the cache
    and leaves the results loop with nothing to wait for — so a test meaning to
    measure the results loop measures the room loop instead and reads 0ms.
    """
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, token, "alice", origin="talk", name="team")
        db.add_room_binding(conn, token, "talk", token)
        db.set_talk_poll_state(conn, token, 50)
        db.upsert_talk_messages(conn, token, [_msg(msg_id=1)])


class TestTheHoldIsRecorded:
    async def test_a_network_call_inside_the_results_transaction_is_named(
        self, config, caplog,
    ):
        """The steady-state exposure, and the only one that recurs.

        The three awaits in the room loop are all first-encounter work. This one
        — the participant fetch that decides whether a room needs an @mention —
        runs per message on a TTL of five minutes, inside a transaction that
        `upsert_talk_messages` and `set_talk_poll_state` have already made a
        writer.
        """
        _settled_room(config)

        with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
            await _poll(
                config,
                conversations=[{"token": "room1", "type": 2, "name": "team"}],
                messages=[_msg(msg_id=100)],
                participants=[{"actorId": "a"}, {"actorId": "b"}, {"actorId": "c"}],
                participants_delay=0.05,
            )

        lines = _txn_lines(caplog)
        assert lines, "the transaction held a network await and said nothing"
        fields = _fields(lines[-1])
        assert fields["phase"] == "results"
        assert int(fields["awaits"]) >= 1
        assert int(fields["await_ms"]) >= 40
        # The hold is at least as long as the wait inside it, which is the whole
        # claim: the connection was open for the duration of the round trip.
        assert int(fields["held_ms"]) >= int(fields["await_ms"])

    async def test_a_long_hold_is_a_warning_and_a_short_one_is_not(
        self, config, caplog,
    ):
        """An operator reads warnings; a series is read out of the journal on
        purpose. Both exist, and the threshold is what separates them."""
        _settled_room(config)

        with patch.object(poller, "_TXN_HOLD_WARN_SECONDS", 0.02):
            with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
                await _poll(
                    config,
                    conversations=[{"token": "room1", "type": 2, "name": "team"}],
                    messages=[_msg(msg_id=100)],
                    participants=[{"actorId": "a"}],
                    participants_delay=0.05,
                )
        results = [r for r in _txn_lines(caplog)
                   if _fields(r)["phase"] == "results"]
        assert [r.levelno for r in results] == [logging.WARNING]

        caplog.clear()
        poller._participant_cache.clear()
        with patch.object(poller, "_TXN_HOLD_WARN_SECONDS", 30.0):
            with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
                await _poll(
                    config,
                    conversations=[{"token": "room1", "type": 2, "name": "team"}],
                    messages=[_msg(msg_id=101)],
                    participants=[{"actorId": "a"}],
                    participants_delay=0.05,
                )
        assert [_fields(r)['phase'] for r in _txn_lines(caplog)
                if r.levelno == logging.WARNING] == []


class TestTheQuietCaseSaysNothing:
    async def test_a_cache_hit_is_not_reported_as_a_round_trip(
        self, config, caplog,
    ):
        """`_get_participants` is awaited whether or not it goes to the network,
        so counting entries rather than round trips would put a line on every
        cycle of every busy room and drown the ones that mean something.

        The control is the first half: the same poll with a cold cache does
        report, so an empty second half is the cache and not a broken fixture.
        That half keeps the real clock, because a real 50ms delay is what it is
        asserting on. The warm half takes `_FakeClock` for the reason
        `test_many_cache_hits_do_not_sum_into_a_round_trip` does — a warm await
        is charged on wall time, and a starved event loop can stretch one past
        the floor on evidence about the host rather than about the cache
        (ISSUE-442). **This case is where that was observed first**, at
        `awaits=1 await_ms=8` under load, on three awaits rather than nine
        hundred: the per-await arm needs one unlucky await and does not care how
        many there were, so a low count is a lower rate and not a safe margin.
        """
        _settled_room(config)

        conversations = [{"token": "room1", "type": 2, "name": "team"}]
        with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
            await _poll(
                config, conversations=conversations, messages=[_msg(msg_id=100)],
                participants=[{"actorId": "a"}], participants_delay=0.05,
            )
        assert _txn_lines(caplog), "cold cache must report, or the control is dead"

        caplog.clear()
        cached_at = poller._participant_cache["room1"][1]
        clock = _FakeClock(poller._TXN_AWAIT_FLOOR_SECONDS * 0.01)
        with patch.object(poller, "time", clock):
            with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
                await _poll(
                    config, conversations=conversations, messages=[_msg(msg_id=101)],
                    participants=[{"actorId": "a"}], participants_delay=0.05,
                )
        assert _txn_lines(caplog) == []
        # The 50ms delay above is no longer what makes a miss visible: under
        # `_FakeClock` a miss would sleep for real and still be charged `step`.
        # An unrestamped cache entry is what says the hit happened.
        assert poller._participant_cache["room1"][1] == cached_at, (
            "the participant cache was refilled, so this was not a cache hit"
        )

    async def test_many_cache_hits_do_not_sum_into_a_round_trip(
        self, config, caplog,
    ):
        """Why the floor is per await rather than on the total.

        `_get_participants` is awaited once per message. At 25µs a warm hit,
        a couple of hundred messages sum past any floor worth setting, and a
        total-based rule would then report a transaction that never touched the
        network — on the busiest rooms, which are exactly the ones an
        investigator would look at first.

        The sibling above is the positive control: same shape, cold cache, one
        message, and it does report.

        **Neither threshold is left on the host's clock (ISSUE-442).**
        `_report_poll_txn` emits on either of two arms, and this test is about
        neither of them firing: an await past `_TXN_AWAIT_FLOOR_SECONDS`, or a
        hold past `_TXN_HOLD_WARN_SECONDS`. Three hundred messages is enough
        work that both became coin flips on a loaded machine, and both were
        observed losing — a 1096ms hold against the 1s threshold (idle: 147ms),
        and separately a single warm await stretched to 8ms against the 5ms
        floor. Neither says anything about the counting rule; a starved event
        loop really did take that long, and the instrument was right both times.

        So the hold threshold is pinned, the way the warn-threshold test above
        pins it, and the awaits are measured on `_FakeClock`, which charges each
        one 50µs or 100µs — the same order as the 25µs a warm hit costs in
        production, and unlike it, a number the host cannot move. The rule
        itself is pinned once more, directly, in
        `TestTheFloorIsPerAwaitAndNotOnTheTotal` below.
        """
        _settled_room(config)
        conversations = [{"token": "room1", "type": 2, "name": "team"}]

        # Warm the cache, and take the line that produces out of the way.
        await _poll(
            config, conversations=conversations, messages=[_msg(msg_id=100)],
            participants=[{"actorId": "a"}], participants_delay=0.05,
        )

        cached_at = poller._participant_cache["room1"][1]
        clock = _FakeClock(poller._TXN_AWAIT_FLOOR_SECONDS * 0.01)
        with patch.object(poller, "time", clock), \
                patch.object(poller, "_TXN_HOLD_WARN_SECONDS", 30.0):
            with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
                await _poll(
                    config, conversations=conversations,
                    messages=[_msg(msg_id=200 + i) for i in range(300)],
                    participants=[{"actorId": "a"}],
                )

        results = [ln for ln in _txn_lines(caplog)
                   if _fields(ln)["phase"] == "results"]
        assert results == [], "300 cache hits were reported as network waiting"
        # Two ways an empty list means nothing, and neither is visible in it.
        # The poll has to have run — the sibling below makes the same point
        # about itself — and these have to have been cache *hits*: this poll
        # passes no `participants_delay`, so a miss returns instantly, and under
        # `_FakeClock` it would not be charged even if it were slow. A refill
        # restamps the entry, so an unchanged timestamp is the witness.
        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "room1") == 499
        assert poller._participant_cache["room1"][1] == cached_at, (
            "the participant cache was refilled, so these were not cache hits"
        )

    async def test_a_poll_with_nothing_to_do_reports_nothing(self, config, caplog):
        """`_txn_lines == []` is equally true of a poll that never ran, so the
        second assertion is what makes the first one mean "quiet".

        On `_FakeClock` for the same reason as its two siblings, and the reason
        is the await count rather than the hold: this poll makes two awaits at
        about 1µs and 4µs, so the hold arm was never the exposure here, but one
        of those two stretching past the floor is the same coin flip the case
        above was losing. Nothing here asserts on a duration, so there is
        nothing for the real clock to contribute.
        """
        _settled_room(config)

        clock = _FakeClock(poller._TXN_AWAIT_FLOOR_SECONDS * 0.01)
        with patch.object(poller, "time", clock):
            with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
                await _poll(
                    config,
                    conversations=[{"token": "room1", "type": 1, "name": "alice"}],
                    messages=[_msg(msg_id=100)],
                )

        assert _txn_lines(caplog) == []
        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "room1") == 100


class TestTheFloorIsPerAwaitAndNotOnTheTotal:
    """The counting rule the two cache-hit cases above rest on, stated without
    going through the poller at all.

    Those cases assert the rule's *effect* — that a poll full of warm hits
    produces no line. This asserts the rule: what `_await_in_txn` charges, and
    what it declines to. The distinction earns a class because the effect is
    reachable by more than one cause, and a single one of these two tests
    pinpoints which.

    On the real clock the effect was also not enough. Those cases' 900 warm
    awaits used to sum to about 4ms against the 5ms floor, so a total-based rule
    would have passed them and the premise their docstrings state was a
    production claim they did not reproduce. `_FakeClock` fixed that in passing —
    the same 900 awaits now sum to about 60ms, twelve times the floor — but it
    fixed it as a side effect of a step chosen for headroom, which is not
    something to leave a rule resting on.
    """

    async def test_sub_floor_awaits_are_not_counted_however_many_there_are(self):
        hold = poller._TxnHold(label="results", opened=0.0)
        step = poller._TXN_AWAIT_FLOOR_SECONDS * 0.4
        rounds = 100

        async def _cache_hit():
            return None

        clock = _FakeClock(step)
        with patch.object(poller, "time", clock):
            for _ in range(rounds):
                await poller._await_in_txn(hold, _cache_hit())

        assert hold.awaits == 0
        assert hold.await_seconds == 0.0
        # An edit guard, not a discriminator: it is arithmetic over two literals
        # this function sets, so it can only fail if someone changes them. That
        # is worth catching — halving `rounds` would quietly make the test
        # vacuous — but what shows the assertion above can fail is the counted
        # case below, not this line.
        assert rounds * step > poller._TXN_AWAIT_FLOOR_SECONDS, (
            "the sub-floor awaits no longer sum past the floor, so a total-based "
            "rule would pass this too and it has stopped testing anything"
        )

    async def test_a_single_await_past_the_floor_is_counted(self):
        """The control for the case above: same machinery, one await over the
        floor, and it is charged. Without it, `awaits == 0` is equally true of a
        counter that never increments."""
        hold = poller._TxnHold(label="results", opened=0.0)
        step = poller._TXN_AWAIT_FLOOR_SECONDS * 1.1

        async def _round_trip():
            return None

        clock = _FakeClock(step)
        with patch.object(poller, "time", clock):
            await poller._await_in_txn(hold, _round_trip())

        assert hold.awaits == 1
        assert hold.await_seconds == pytest.approx(step)


class TestTheReporterNeverRaises:
    def test_a_broken_clock_costs_a_line_and_not_the_poll(self):
        """One caller is the daemon's busiest loop and the call sits in a
        `finally`, where a raise would replace whatever the block was already
        propagating.

        The `assert` is the whole point: without it the test passes if the
        emission rule ever stops reaching the logger for this hold, at which
        point it exercises the `except` it is named for not at all — the
        no-op-indistinguishable-from-success shape `.claude/rules/testbed.md`
        records.
        """
        hold = poller._TxnHold(label="results", opened=0.0)
        hold.awaits = 1
        hold.await_seconds = 1.0

        with patch.object(
            poller._POLL_TXN_LOGGER, "warning", side_effect=RuntimeError("boom"),
        ) as warn:
            poller._report_poll_txn(hold, held_seconds=99.0)

        assert warn.call_count == 1
