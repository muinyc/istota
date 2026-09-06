"""Shared test fixtures for istota tests."""

import logging
import os
from pathlib import Path

import pytest


from .support.env_isolation import (
    NO_PROXY_NAMES,
    NO_PROXY_VALUE,
    NO_SCRUB_FLAG,
    SUITE_ENV_DEFAULTS,
    scrubbed_env_names,
)
from .support import testmon_compat

# At import, and this file is the right place for it in both directions.
# Early enough: testmon reaches the patched method from `pytest_configure`
# (`determine_stable`, on every filename already in `.testmondata`) as well as
# from `pytest_runtest_logreport`, and an initial conftest is loaded before
# either — verified by putting the dotless name in the data file and taking
# this line out, which fails in `_do_configure`. Late enough is not a question:
# what it raises there is an INTERNALERROR, not a test failure, so the session
# dies with every test already passed. A no-op when testmon is not installed.
# See `tests/support/testmon_compat.py`.
testmon_compat.install()


def _load_dotenv():
    """Load .env file from project root into os.environ (simple key=value parser).

    Runs at import, i.e. before collection, and is therefore *not* undone by
    the `_scrub_ambient_env` fixture below — see that fixture for which of the
    two wins where they overlap.
    """
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            os.environ.setdefault(key, value)


_load_dotenv()

# Set at import as well as by `_scrub_ambient_env`, because module-scope code
# in a test file runs during collection, before any fixture. Each is documented
# in `tests/support/env_isolation.py`.
for _name, _value in SUITE_ENV_DEFAULTS.items():
    os.environ.setdefault(_name, _value)

from istota import db
from istota import logging_setup
from istota.config import Config, UserConfig


@pytest.fixture(autouse=True)
def _scrub_ambient_env(monkeypatch, request):
    """Take the ambient shell's istota config out of every test (ISSUE-301).

    The suite reset every process global it knew about and no part of the
    environment, so a shell carrying real config changed its answers: thirty of
    the thirty-two failures on the deployment host were this, and none of them
    was about the code. The list of what goes, what is forced to a fixed value,
    and the reasoning behind each rule, is in `tests/support/env_isolation.py`.

    **Where this and `_load_dotenv` disagree, this wins, and that is a decision
    rather than an accident of ordering.** `_load_dotenv` runs at import and
    copies the repo-root `.env` into `os.environ`; this fixture runs per test
    and deletes the scrubbed names, so for anything on the scrub list the
    dotenv load has no effect on a test body. That is the right way round: a
    developer's `.env` is their *deployment* config, and a suite whose result
    depends on it is the bug being fixed here — `.env` on this machine sets
    `BROWSER_HOST`, and it is exactly the kind of value a test asserting on a
    default must not see.

    `_load_dotenv` is not thereby pointless, and is deliberately left alone.
    It still feeds every name off the scrub list, and it still feeds the
    scrubbed ones to module-scope code, which runs at collection before any
    fixture: `tests/test_browse_integration.py` reads `BROWSER_HOST` at import
    to decide whether to skip, and that is the intended way to consume one of
    these. What changes is that a *test body* can no longer be reached by one.

    Uses `monkeypatch` rather than mutating `os.environ` so the shell is put
    back between tests. That matters for the higher-scoped fixtures created
    lazily part-way through a session — `tests/image/test_upgrade.py` reaches
    one through `request.getfixturevalue`, and the compose boot in `stack`
    snapshots `os.environ` for its child processes.

    One hole worth knowing about: `monkeypatch` is a single per-test object
    shared with the test body, so a test calling `monkeypatch.undo()` reverses
    this fixture's work along with its own. Use `monkeypatch.context()` or a
    local `pytest.MonkeyPatch()` instead of `undo()`.
    """
    if os.environ.get(NO_SCRUB_FLAG) == "1":
        # The one caller is the negative control in `tests/test_env_isolation.py`,
        # which has to watch the reported tests actually go red — a test that
        # asserts against the behaviour of a separately-configured process
        # tells you nothing about whether it can fail. Nothing else should set
        # this: it restores the state ISSUE-301 was filed about.
        return
    for name in sorted(scrubbed_env_names(os.environ)):
        monkeypatch.delenv(name, raising=False)
    # After the scrub, not before: these match the `ISTOTA_` prefix and are
    # meant to. The suite's own value beats the shell's.
    for name, value in SUITE_ENV_DEFAULTS.items():
        monkeypatch.setenv(name, value)
    devbox_user = request.config.getoption("--devbox-user")
    if devbox_user:
        monkeypatch.setenv("ISTOTA_USER_ID", devbox_user)
    # Forced, not scrubbed — see `env_isolation.NO_PROXY_VALUE` for why the
    # proxy variables themselves are left alone.
    for name in NO_PROXY_NAMES:
        monkeypatch.setenv(name, NO_PROXY_VALUE)


@pytest.fixture(autouse=True)
def _reset_istota_logging():
    """Take the `istota` logger's handlers and level out of every test.

    `logging` is a process global that this file did not reset, and one test
    could poison every later one in the same xdist worker. `istota.cli.main`
    calls `setup_logging`, which raises the `istota` logger to INFO and adds a
    `StreamHandler` bound to `sys.stderr` **as it is at that moment**; a
    `logging_setup._initialized` flag then makes it permanent. A test calling
    `cli.main()` under `capsys` binds that handler to the capture object
    pytest closes at teardown, so from then on every `istota.*` record at INFO
    or above raises inside `emit`, and `logging.Handler.handleError` prints
    `--- Logging error ---` plus a traceback to the *current* `sys.stderr`.

    Inside a `click.testing.CliRunner` invocation the current `sys.stderr` is
    that invocation's own buffer, and click's `Result.output` is a mix of
    stdout and stderr — so the traceback lands in the value the test parses.
    That is what broke 37 of the 39 tests in `tests/test_feeds_cli.py` in a
    full-suite run: `ensure_initialised` logs one INFO line per feeds database
    (`feeds_image_dedup_backfilled`), on the first CLI invocation and never
    again, so the only two survivors were the two tests that parse a *second*
    invocation's output. Reproduces in under two seconds as
    `pytest tests/test_cli_session.py tests/test_feeds_cli.py -n0`.

    Restoring rather than clearing, so a test that installs its own handler on
    `istota` still sees it for the whole of its own body.
    """
    logger = logging.getLogger("istota")
    handlers = list(logger.handlers)
    level = logger.level
    initialized = logging_setup._initialized
    try:
        yield
    finally:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logging_setup._initialized = initialized


@pytest.fixture(autouse=True)
def _skip_dac_tests_as_root(request):
    """Root bypasses the permission bits these tests are made of.

    A `chmod 0o500` directory is still writable by uid 0, and a `chmod 0o000`
    file is still readable, so a test asserting "this fails" asserts nothing
    and reports a failure that says nothing about the code. The developer host
    runs as a normal user and never notices; `scripts/test-linux.sh` runs as
    root in a container and does.

    `geteuid` via `getattr`: it does not exist on Windows, and an autouse
    fixture that raised `AttributeError` would error every test in the suite
    rather than skip two. -1 is nobody, so nothing skips.
    """
    if request.node.get_closest_marker("requires_dac"):
        if getattr(os, "geteuid", lambda: -1)() == 0:
            pytest.skip("running as root: POSIX permission bits do not constrain this process")


@pytest.fixture(autouse=True)
def _no_network_symbol_lookups(monkeypatch):
    """Portfolio auto-classification's default fetch is a live yfinance
    lookup, and an import triggers it — so any test that reaches a portfolio
    import would otherwise hit the network. Root-level rather than scoped to
    ``tests/money/``, since the import path is reachable from the web-route
    and skill tests too. Tests exercising the lookup path inject their own
    fetch; ``TestFetchSymbolInfo`` captures the real function at import time.
    """
    try:
        from istota.money import portfolio_autoclass
    except Exception:
        # Money extra absent, or its import chain unhappy. Broad on purpose:
        # this fixture is purely defensive and runs before every test in the
        # suite, so anything it raises fails thousands of unrelated tests
        # with a traceback pointing at the wrong place.
        return

    monkeypatch.setattr(
        portfolio_autoclass, "fetch_symbol_info", lambda symbol, **kwargs: None
    )


@pytest.fixture(autouse=True)
def _no_subscription_usage_lookups(monkeypatch):
    """The plan-utilization poll reads a real credential and makes a real request.

    ``doctor.run_checks`` runs ``runtime.subscription_usage`` with
    ``probe=True``, and several tests sweep the whole registry. Left alone, on a
    developer's macOS laptop that reads the real keychain credential through a
    ``security find-generic-password`` subprocess and then issues a live GET to
    api.anthropic.com, once per sweeping test. In CI nothing resolves and the
    check SKIPs, so the sweep would also mean two different things on two
    machines. (Whether the keychain lookup answers silently or asks the operator
    for authorization depends on that item's ACL, which is another reason not to
    find out from inside a test run.)

    Same shape as ``_no_network_symbol_lookups`` above, and the same escape
    hatch: ``tests/test_subscription_usage.py`` exercises the module itself and
    reinstates the functions it captured at import time, and
    ``TestSubscriptionUsage`` in ``tests/test_doctor.py`` reinstates
    ``get_snapshot`` behind a wrapper that injects a stub transport.

    Both the entry point *and* the network leaf are neutralized: a test that
    reinstates one and forgets the other still cannot reach the endpoint.
    """
    try:
        from istota import subscription_usage
    except Exception:
        # Broad on purpose: this runs before every test in the suite, so
        # anything it raises fails thousands of unrelated tests with a
        # traceback pointing at the wrong place.
        return

    def _no_credential(config, **kwargs):
        return subscription_usage.UsageSnapshot(
            fetched_at=0.0, source="none", error=subscription_usage.NO_CREDENTIAL_ERROR
        )

    def _refuse(url, headers, timeout):
        raise AssertionError("a test reached the real usage endpoint; inject a transport")

    monkeypatch.setattr(subscription_usage, "get_snapshot", _no_credential)
    monkeypatch.setattr(subscription_usage, "_urllib_transport", _refuse)


@pytest.fixture
def db_path(tmp_path):
    """Initialize a real SQLite database using schema.sql and return its path."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def db_conn(db_path):
    """Yield a database connection with row factory set."""
    with db.get_db(db_path) as conn:
        yield conn


@pytest.fixture
def fake_talk(db_path):
    """A `FakeTalkClient` behind every `get_talk_client` binding.

    `get_talk_client` is imported at module level in two places —
    `transport/talk/__init__.py` and `transport/talk/inbound.py` — so this
    patches **both names**. Patching only the package one leaves the entire
    poller and `_post_ack` talking to the real factory, which is a hole shaped
    exactly like the bug the double exists to catch;
    `tests/test_support_talk_double.py` has a control for it.

    It also patches `async_runtime.get_talk_client` itself, which is what
    *reaches* the one remaining function-local importer — `commands`' `!search`
    — since a function-local import resolves the name at call time. Reached is
    not covered: `!search` calls `search_messages`, which is on no seam and not
    on the double, so it gets an `AttributeError` rather than an answer. All
    three go through `talk_bot_client`, which clears any bearer token left on
    the shared instance by a `fake_talk_web` construction, so the singleton
    reads as the basic-auth bot client whichever test ran before.

    `web_app._delete_from_talk`'s bot leg used to be the second importer and is
    now one of `web_app`'s own constructions (ISSUE-407) — the singleton belongs
    to the runtime loop and that function runs on uvicorn's.

    It does **not** reach `web_app`'s eight direct `TalkClient(...)`
    constructions. Those need `fake_talk_web`, below.

    **Not autouse.** An autouse patch would change every existing test's
    behaviour in one commit and make the conversion unreviewable.

    Bound to the `db_path` fixture, which `make_config` also builds on
    (`tmp_path / "test.db"`), so a test using both gets a double reading the
    same database. A test whose database is elsewhere assigns
    `fake_talk.db_path`; the binding lookup happens per call, so that takes
    effect immediately. Point it at a database `db.init_db` has run against —
    the double raises `BrokenTalkDouble`, which is a `BaseException` and so
    escapes the product's `except Exception`, rather than letting a missing
    `room_bindings` table read as a 404.

    It also clears the two process-lifetime caches that sit *in front of* the
    seams, because a cache hit is a call the double never sees. Both are module
    globals nothing else resets, and an xdist worker runs many tests in one
    process: `scheduler._channel_name_cache` (token -> displayName, no
    expiry) and `transport.talk.inbound._participant_cache` (TTL). Cleared
    on the way in and on the way out, so this fixture neither inherits nor
    leaves a hit.
    """
    from unittest.mock import patch

    from istota import scheduler as scheduler_module
    from istota.transport.talk import inbound as talk_inbound

    from .support.talk_double import FakeTalkClient, talk_bot_client

    client = FakeTalkClient(db_path)
    bot = talk_bot_client(client)
    scheduler_module._channel_name_cache.clear()
    talk_inbound._participant_cache.clear()
    with patch("istota.transport.talk.get_talk_client", bot), \
         patch("istota.transport.talk.inbound.get_talk_client", bot), \
         patch("istota.async_runtime.get_talk_client", bot):
        yield client
    scheduler_module._channel_name_cache.clear()
    talk_inbound._participant_cache.clear()


@pytest.fixture
def fake_talk_web(fake_talk):
    """The same double, additionally behind `web_app`'s own constructions.

    `web_app.py` builds `TalkClient(...)` directly in eight places, most with a
    per-user OAuth bearer token — the promote path that *creates* a promoted
    room, the post-as-user mirror, the read push and pull, the rename
    propagation, the message delete's two legs and the liveness probe. There is
    no factory to patch, so the class itself is the seam: this replaces
    `istota.talk.TalkClient`, which every one of those sites imports
    function-locally and therefore resolves at call time.

    Depends on `fake_talk` rather than repeating it, so a web test gets one
    double behind both — which is what `_delete_from_talk` needs, since it tries
    the user's client and then the bot's.

    Two things a web test still has to do itself: point the module global
    (`web_app._config`) at a config on the same `db_path`, and store a token for
    the user, since `web_tokens.feature_enabled` gates every one of these paths
    before a client is built.
    """
    from unittest.mock import patch

    from .support.talk_double import talk_client_factory

    with patch("istota.talk.TalkClient", talk_client_factory(fake_talk)):
        yield fake_talk


@pytest.fixture
def make_task():
    """Factory fixture that creates Task dataclass instances with defaults."""
    def _make_task(**overrides):
        defaults = {
            "id": 1,
            "prompt": "test prompt",
            "user_id": "testuser",
            "source_type": "cli",
            "status": "pending",
        }
        defaults.update(overrides)
        return db.Task(**defaults)
    return _make_task


@pytest.fixture
def make_config(tmp_path):
    """Factory fixture that creates Config instances with tmp paths."""
    def _make_config(**overrides):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        index_file = skills_dir / "_index.toml"
        if not index_file.exists():
            index_file.write_text("")

        mount_path = tmp_path / "mount"
        mount_path.mkdir(exist_ok=True)

        defaults = {
            "db_path": tmp_path / "test.db",
            "temp_dir": tmp_path / "temp",
            "skills_dir": skills_dir,
            "nextcloud_mount_path": mount_path,
        }
        defaults.update(overrides)
        return Config(**defaults)
    return _make_config


@pytest.fixture
def make_user_config():
    """Factory fixture that creates UserConfig instances with defaults."""
    def _make_user_config(**overrides):
        defaults = {
            "display_name": "Test User",
            "email_addresses": [],
            "timezone": "UTC",
            "briefings": [],
        }
        defaults.update(overrides)
        return UserConfig(**defaults)
    return _make_user_config


@pytest.fixture(autouse=True)
def _reset_async_runtime_singletons():
    """Isolate the process-global persistent asyncio runtime + TalkClient.

    These singletons (``istota.async_runtime._RUNTIME`` / ``_TALK_CLIENT``)
    persist across tests within an xdist worker. A test that lazily starts the
    runtime or opens the shared client and doesn't reset it would leak that
    state into the next test on the same worker (e.g. a returned-singleton whose
    httpx pool is already open). Reset before and after every test so isolation
    doesn't depend on each Talk-touching test remembering to clean up. Cheap for
    the vast majority of tests that never touch the runtime: the reset helpers
    early-return when the globals are still ``None``.
    """
    from istota.async_runtime import reset_async_runtime, reset_talk_client

    reset_talk_client()
    reset_async_runtime()
    yield
    reset_talk_client()
    reset_async_runtime()


@pytest.fixture
def outbound_gate_off(monkeypatch, tmp_path):
    """Put the outbound approval gate in ``off`` mode for a send-mechanics test.

    The gate runs for real and answers "send" — it is not patched out. Tests
    about how `send` / `reply` build a message have nothing to say about the
    policy, and the default floor (`untrusted`) would hold every one of their
    fixture recipients. The gate's own behaviour is covered in
    ``test_outbound_gate.py`` and ``test_outbound_gate_fires.py``.

    Also isolates the catch-all-pattern warning latch, a process-global set that
    would otherwise carry across tests in an xdist worker.
    """
    from istota import outbound_policy
    from istota.config import Config, EmailConfig

    db_path = tmp_path / "gate-off.db"
    db.init_db(db_path)
    cfg = Config(
        db_path=db_path,
        email=EmailConfig(enabled=True, outbound_approval_floor="off"),
        users={"alice": UserConfig(display_name="Alice")},
    )
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    outbound_policy._warned_catch_all.clear()
    yield cfg
    outbound_policy._warned_catch_all.clear()


@pytest.fixture(autouse=True)
def _reset_expunge_warning_latch():
    """Isolate ``skills.email._expunge_warned_hosts``.

    A process-global "warned about this host already" set, so any test that
    drives a mailbox without UIDPLUS seeds it for the rest of that xdist
    worker. Tests must be order-independent, and a test asserting on that
    warning would otherwise pass or fail on who ran first.
    """
    from istota.skills import email as email_skill

    email_skill._expunge_warned_hosts.clear()
    yield
    email_skill._expunge_warned_hosts.clear()


@pytest.fixture(autouse=True)
def _reset_brain_refusal_latch():
    """Isolate ``brain._WARNED_REFUSALS``.

    Same shape and same reason as the latch above: a process-global set of
    routing refusals already warned about, so any test that resolves a refused
    brain pin seeds it for the rest of that xdist worker and a test asserting
    on the warning passes or fails on who ran first.
    """
    from istota import brain as brain_mod

    brain_mod._WARNED_REFUSALS.clear()
    yield
    brain_mod._WARNED_REFUSALS.clear()


@pytest.fixture(autouse=True)
def _reset_process_globals():
    """Isolate the three process-globals that survive a test.

    Same shape and same reason as ``_reset_brain_refusal_latch`` above, which
    already generalises the rule: a process-global that one test seeds and the
    next inherits, so a test passes or fails on who ran first. These three are
    that, and each one has been observed producing a failure:

    1. The ``[models.aliases]`` table (``brain._roles``) and
    2. the per-model capability overrides (``llm.catalog._OVERRIDES``).

       Both are installed as a **side effect of ``load_config``** (``config.py``,
       at the end of the parse), so the leak is not confined to tests that reach
       for aliases deliberately: any test anywhere that loads a config carrying
       one of those blocks seeds the table for every later test in that xdist
       worker. A test that builds a ``Config(...)`` directly never resets it and
       silently inherits whichever table ran before it. Observed as three tests
       in ``test_model_override.py`` resolving ``smart`` to another test's native
       model, via ``NativeBrain.resolve_alias``, which consults
       ``get_alias_override_target`` *before* the built-in role.

    3. The primary-availability breaker (``brain._fallback._BREAKER``).

       ``reset_availability_breaker`` has always existed for this, and around
       thirty tests call it by hand. Those calls are now belt-and-braces rather
       than the mechanism — **do not add a thirty-first**; this fixture covers
       it. They are left in place deliberately: removing them is a nine-file
       diff that would bury the fix, and they are harmless where they are.
       Observed as ``test_executor_fallback``'s cooldown assertions reading a
       breaker another test had tripped.

    Cleared on entry *and* exit. Clearing only on exit leaves the first test in
    a worker exposed to whatever import-time work preceded it, and entering
    dirty is as wrong as leaving dirty. Function-scoped for the same reason:
    the leak is per-test, so anything wider reintroduces it within the group.

    Deliberately **not** covered: ``subscription_usage._NO_CREDENTIAL_AT``. It
    is keyed by data dir and tests use a per-test ``tmp_path``, so no collision
    has been observed. Each entry above earned its place with a mechanism and a
    victim; one added on suspicion could never be removed, because nothing would
    go red if it were wrong.
    """
    from istota.brain._fallback import reset_availability_breaker
    from istota.brain._roles import set_alias_overrides
    from istota.llm import catalog as catalog_mod

    def _clear():
        set_alias_overrides({})
        catalog_mod._OVERRIDES = {}
        reset_availability_breaker()

    _clear()
    yield
    _clear()


def pytest_addoption(parser):
    """Options for the image and real-devbox tiers.

    Lives here rather than in ``tests/image/conftest.py`` because pytest only
    honours ``pytest_addoption`` in an *initial* conftest — the rootdir's and
    the testpaths' — and a subdirectory conftest is loaded after argument
    parsing has already happened.

    The development machine is arm64 and production is amd64. A native build is
    fast and an emulated one is not, so native is the default and amd64 is an
    explicit opt-in taken before a release. ``ISTOTA_TEST_PLATFORM`` is the
    environment-variable form, for the shell drivers.
    """
    parser.addoption(
        "--platform",
        action="store",
        default=None,
        metavar="PLATFORM",
        help=(
            "Docker platform for the image tier, e.g. amd64 or linux/amd64. "
            "Defaults to native, or to $ISTOTA_TEST_PLATFORM."
        ),
    )
    parser.addoption(
        "--devbox-user",
        action="store",
        default=None,
        metavar="USER",
        help="require the real-devbox integration tier for this user",
    )


def resolve_platform(config) -> str:
    """`--platform`, else `$ISTOTA_TEST_PLATFORM`, else native.

    A bare architecture is accepted and normalized — `amd64` is what a person
    types and `linux/amd64` is what Docker wants, and getting that wrong builds
    natively while the tag claims otherwise.

    Here rather than in ``tests/image/conftest.py`` because three Docker tiers
    now read it. The smoke tier used to import it across package boundaries
    (``from ..image.conftest import resolve_platform``), which meant one tier's
    fixtures depended on another's conftest for a five-line pure function; the
    option it reads is declared just above, so this is where it belongs.
    """
    raw = config.getoption("--platform") or os.environ.get("ISTOTA_TEST_PLATFORM") or ""
    raw = raw.strip()
    if not raw:
        return ""
    return raw if "/" in raw else f"linux/{raw}"


# --- Deployment tiers: the stack fixtures both shapes share ----------------
#
# Hoisted here from `tests/smoke/conftest.py` in Stage 3 of the
# deployment-testbed spec, at the point the reason for hoisting appeared:
# `tests/full/` needs the same `stacks` / `stack` pair, and a fixture defined in
# a sibling package's conftest is invisible to another. What stays down in
# `tests/smoke/conftest.py` is what is specific to the *lean* shape — its image
# tag and the negative control's image.
#
# **Nothing here is autouse**, and that is the constraint that shaped it. The
# sweep and the exec measurement were autouse session fixtures while they lived
# under `tests/smoke/`, where they only ever applied to that directory. At the
# rootdir an autouse session fixture runs on *every* `uv run pytest`, and the
# sweep shells out to `docker info`. They are requested by `stacks` instead, so
# they still run exactly once and only when a stack is actually asked for.

import hashlib  # noqa: E402 - this file's imports are split by section, above
import time  # noqa: E402
from dataclasses import replace  # noqa: E402

from testbed import probe as probe_support  # noqa: E402
from testbed import profiles  # noqa: E402
from testbed import stack as stack_support  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
LEAN_COMPOSE_FILE = REPO / "docker" / "docker-compose.test.yml"
RENDER_CONFIG = REPO / "docker" / "istota" / "render-config.sh"
LEAN_PREBUILT_OVERLAY = REPO / "docker" / "docker-compose.test.prebuilt.yml"
FULL_COMPOSE_FILE = REPO / "docker" / "docker-compose.yml"
TESTBED_OVERLAY = REPO / "testbed" / "compose" / "testbed.yml"

LEAN_READY_TIMEOUT = 120

#: The tiers that must run `-n0`, and therefore the ones the guard below covers.
SERIAL_TIER_MARKERS = ("smoke", "full", "testbed")

#: The markers `addopts` deselects on an ordinary `uv run pytest`.
#:
#: Restated here because pytest's `-m` and testmon's selection are mutually
#: exclusive: testmon switches its selection off entirely the moment it sees a
#: marker expression ("selection automatically deactivated because -m was
#: used"), so an incremental run cannot express the default deselection the way
#: the default run does. `ISTOTA_DESELECT_TIERS=1` applies the same set through
#: the collection hook instead, which leaves `-m` free for testmon.
#:
#: `tests/test_tier_deselection.py` fails if this falls out of step with
#: addopts — the direction that matters is a marker deselected there and
#: missing here, which would have an incremental run building Docker images.
DISCRETIONARY_MARKERS = (
    "integration", "live", "linux", "image", "smoke", "full", "testbed", "ml",
)

#: Every compose project these tiers create starts with it, which is what makes
#: the session-start sweep able to find leftovers without touching anything else
#: — a developer's own demo or red-team stack is never named this.
PROJECT_PREFIX = "istota-testbed-"

#: The prefix the smoke tier used before Stage 3 gave both shapes one pool.
#: Swept as well as the current one, so a stack left behind by a run from before
#: the rename is still reclaimed rather than surviving forever.
LEGACY_PROJECT_PREFIXES = ("istota-smoke-",)

_XDIST_MESSAGE = (
    "the smoke, full and testbed tiers must run with -n0. Session-scoped "
    "fixtures are per-worker, so N workers would each build the image and bring "
    "up their own stacks under one project prefix, race the same daemon, and "
    "sweep each other's projects. The wire tier is milder and still wrong: N "
    "workers would each start a mail container, and the assertions there are "
    "about what is in a mailbox."
)

# What a test gets when it declares no `script` marker. One plain answer, which
# is enough for any scenario that only needs a task to complete —
# `test_lean_stack.py` asserts on this exact string.
DEFAULT_SCRIPT = [{"text": "the scripted answer"}]


def lean_image_tag() -> str:
    """One image tag per checkout, shared by every lean stack in the session.

    Compose names a built image after the project, and the project is unique per
    stack so an interrupted run's containers are never adopted by the next
    session. Images are not reclaimed by `down --volumes`, so that left one
    permanent tag per stack. A single tag collapses them.

    Scoped by checkout path rather than fixed, because work in this repo runs in
    parallel git worktrees: two of them sharing a tag means the second
    `up --build` moves it out from under the first run's containers, mid-run.
    Same reasoning as `tests/image/conftest._tag_for`.

    The full shape needs no equivalent. Its `build:` blocks name no `image:`, so
    compose tags them `<project>-<service>` and each stack gets its own.
    """
    digest = hashlib.sha256(str(REPO).encode()).hexdigest()[:8]
    return f"istota-test/lean:{digest}"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Fail early when a serial tier is selected under xdist.

    `trylast` matters because this hook is also where `-m` deselection happens —
    without it the unfiltered item list is what arrives, and an ordinary
    `uv run pytest` fails with a usage error about a tier it had already
    deselected.

    **It cannot see a real parallel run**, which is the actual scenario. Under
    `-n 2` the controller never calls this (it holds no items) and xdist clears
    `numprocesses` in the workers so they do not re-fan-out. `_require_no_xdist`
    is the check that binds.
    """
    if os.environ.get("ISTOTA_DESELECT_TIERS", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        keep, drop = [], []
        for item in items:
            target = drop if any(
                item.get_closest_marker(m) for m in DISCRETIONARY_MARKERS
            ) else keep
            target.append(item)
        if drop:
            config.hook.pytest_deselected(items=drop)
            items[:] = keep

    if not any(
        item.get_closest_marker(marker)
        for item in items
        for marker in SERIAL_TIER_MARKERS
    ):
        return

    workers = getattr(config.option, "numprocesses", None)
    distribution = config.getoption("dist", "no")
    if workers or distribution not in ("no", None):
        raise pytest.UsageError(
            f"{_XDIST_MESSAGE} (saw -n {workers}, --dist {distribution})"
        )


def _require_no_xdist(config) -> None:
    """Refuse inside an xdist worker.

    `workerinput` is set by xdist on the worker's config and absent in a
    single-process run — the only signal that survives into the place where the
    damage would be done.
    """
    if hasattr(config, "workerinput"):
        worker = config.workerinput.get("workerid", "?")
        pytest.fail(
            f"{_XDIST_MESSAGE} (running in xdist worker {worker})", pytrace=False
        )


def require_docker() -> None:
    if not stack_support.docker_available():
        pytest.skip("no Docker daemon available")


@pytest.fixture(scope="session")
def _sweep_leftover_stacks():
    """Reclaim stacks an earlier run was killed before tearing down.

    A unique project name per stack stops one run from adopting another's
    containers mid-flight, but it also means nothing ever reclaims them: a killed
    session leaves a container and a named volume behind for good. One sweep at
    the first stack request closes that, scoped by prefix so it can never touch a
    developer's own stack.
    """
    if stack_support.docker_available():
        for prefix in (PROJECT_PREFIX, *LEGACY_PROJECT_PREFIXES):
            stack_support.sweep_projects(prefix)
    yield


@pytest.fixture(scope="session")
def _measure_probe_exec(request):
    """Report what the tier spent inside `docker compose exec`.

    Open question 4 in the deployment-testbed spec asks whether a `Probe` query
    per poll is fast enough once one stack serves a whole session, and answers it
    with a measurement rather than a long-lived reader process nobody has shown
    is needed. This is that measurement, and it stays because the answer changes
    as the tier grows — a number printed on every run is what makes a regression
    visible before it is a complaint. Stage 2 measured 31% for the lean shape;
    the full shape has a longer session and the same counters.

    The span opens at the first stack request and closes at session teardown, so
    a `-m smoke` or `-m full` run reports a fraction of the thing that was
    actually running.
    """
    probe_support.reset_exec_stats()
    started = time.monotonic()
    yield
    stats = probe_support.exec_stats()
    elapsed = time.monotonic() - started
    if not stats.calls or elapsed <= 0:
        return
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - only under a custom -p
        return
    reporter.write_line(
        f"probe: {stats.calls} `docker compose exec` call(s), "
        f"{stats.seconds:.1f}s of {elapsed:.1f}s "
        f"({stats.seconds / elapsed:.0%} of the tier), "
        f"{stats.seconds / stats.calls * 1000:.0f}ms each"
    )


def _keep_scope() -> str:
    """One kept credential set per checkout, matching the kept project name.

    `StackPool._compose_args_full` derives the project from the compose file's
    resolved path for the same reason: two worktrees sharing a kept volume set
    would each boot the other's half-provisioned Nextcloud.
    """
    return hashlib.sha256(str(FULL_COMPOSE_FILE.resolve()).encode()).hexdigest()[:8]


def _report_boot_times(config, pool) -> None:
    """Print where a cold boot went, once, at session end.

    Open question 2 asks whether the provisioned volume set needs snapshotting,
    and says it should be settled against Stage 3's measurement rather than
    against the "roughly ten minutes" a comment remembers. A number nobody has to
    instrument for is what makes that possible later.
    """
    if not pool.boot_times:
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - only under a custom -p
        return
    for profile, service, seconds in pool.boot_times:
        reporter.write_line(f"boot: {profile} waited {seconds:.0f}s on {service}")


@pytest.fixture(scope="session")
def stacks(pytestconfig, tmp_path_factory, _sweep_leftover_stacks, _measure_probe_exec):
    """Lazily-started, session-scoped stacks, keyed by profile name.

    Nothing is booted here. The pool starts a stack the first time a test
    declares its profile, so a run selecting only the forge scenarios never pays
    for a `base` stack, and one selecting only lean scenarios never pays the full
    shape's cold boot — and `close_all` tears down whatever ended up running.

    One pool for both shapes rather than one per tier, so a session that happened
    to select from both sweeps once and tears down once.
    """
    _require_no_xdist(pytestconfig)
    require_docker()

    keep = bool(os.environ.get("ISTOTA_TESTBED_KEEP"))
    pool = stack_support.StackPool(
        workdir=tmp_path_factory.mktemp("testbed"),
        lean=stack_support.LeanShape(
            compose_file=LEAN_COMPOSE_FILE,
            render_script=RENDER_CONFIG,
            image=lean_image_tag(),
            prebuilt_overlay=LEAN_PREBUILT_OVERLAY,
            ready_timeout=LEAN_READY_TIMEOUT,
        ),
        full=stack_support.FullShape(
            compose_file=FULL_COMPOSE_FILE,
            overlay=TESTBED_OVERLAY,
            keep=keep,
            # Outside the checkout, with the other machine-wide test state:
            # these are real generated passwords, and the repo's pre-commit hook
            # exists because credentials end up in trees.
            keep_dir=Path.home() / ".cache" / "istota-testbed" / _keep_scope(),
        ),
        platform=resolve_platform(pytestconfig),
        project_prefix=PROJECT_PREFIX,
    )
    try:
        yield pool
    finally:
        pool.close_all()
        _report_boot_times(pytestconfig, pool)


@pytest.fixture
def stack(request, stacks):
    """The stack for the profile this test declared, reset and quiescent.

    `reset` runs *before* the test rather than after, so a failed test's state is
    still there to inspect and the next test is still clean.

    `no-forge` is the one profile whose image cannot be written down: it is
    derived from whichever image the session actually built. The tag is filled in
    here, and `getfixturevalue` rather than a fixture argument so a run with no
    negative control in it never builds the second image — and so this fixture,
    which now lives at the rootdir, does not have to see a lean-only fixture that
    still lives under `tests/smoke/`.

    The reset's watermark is stashed as `stack.mark`, because the instant it is
    taken is the one that matters: after this test's reset and before anything it
    does. A scenario taking its own would take it after `submit`, which is too
    late for the row it wants to prove was never written.
    """
    marker = request.node.get_closest_marker("profile")
    name = marker.args[0] if marker and marker.args else profiles.BASE.name
    fresh = bool(marker.kwargs.get("fresh")) if marker else False

    profile = profiles.by_name(name)
    if profile.name == profiles.NO_FORGE.name:
        profile = replace(profile, image=request.getfixturevalue("no_forge_image"))

    running = stacks.get(profile, fresh=fresh)
    script_marker = request.node.get_closest_marker("script")
    turns = (
        list(script_marker.args[0])
        if script_marker and script_marker.args
        else list(DEFAULT_SCRIPT)
    )
    try:
        try:
            # `pytrace=False`, because a reset that could not quiesce is a
            # harness condition rather than a code defect, and a traceback through
            # three fixture frames buries the one line that says which task ids
            # were still in flight. `testbed` raises rather than calling
            # `pytest.fail` itself — it is an installable package two repos
            # outside this one consume — so the translation happens here.
            running.mark = running.reset(turns)
        except (TimeoutError, stack_support.StackError) as exc:
            pytest.fail(str(exc), pytrace=False)
        yield running
    finally:
        if fresh:
            stacks.release(running)
