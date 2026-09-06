"""FastAPI webhook receiver for istota.

Run as: uvicorn istota.webhook_receiver:app --host 127.0.0.1 --port 8765

Currently handles:
- /webhooks/location — Overland GPS location data
"""

import logging
import signal
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import location
from .build_info import build_description
from .config import load_config
from .location.models import LocationContext
from .timestamps import iso_now

logger = logging.getLogger("istota.webhook_receiver")

# Module-level state, populated on startup. The three dicts are rebound
# atomically under ``_lock`` by ``reload_config`` so any reader holding
# the lock sees a consistent snapshot.
_config = None
_token_map: dict[str, str] = {}                        # token -> user_id
_user_contexts: dict[str, LocationContext] = {}        # user_id -> ctx
_places_cache: dict[str, list] = {}                     # user_id -> places
_lock = threading.Lock()

# mtime of the ingest-reload sentinel as of the last rebuild. See
# ``_maybe_reload_for_signal``.
_sentinel_stamp: float = 0.0

# Hysteresis threshold: consecutive pings at new place before opening a visit
HYSTERESIS_THRESHOLD = 2

# Fallbacks when config hasn't been loaded (e.g., tests that call state-machine
# helpers directly). The webhook path always uses config values.
DEFAULT_ACCURACY_THRESHOLD_M = 100.0
DEFAULT_VISIT_EXIT_MINUTES = 5.0

# How a stay's worth of *declared* points is thinned at ingest (ISSUE-349).
#
# A declared point is a coordinate the iOS shell asserts while the device is on
# the configured home SSID, not a fix it took (ISSUE-229, which put the marker
# on the row). It is a constant by construction, so re-sending it says nothing
# the first one did not. The shell used to send it every 60 s for as long as the
# device stayed put, on the reasoning that iOS would eventually pause the
# location stream — measured over a month, it does not: 26,713 of 39,319 native
# pings were declared, all at one coordinate, 99.4% under 70 s apart, one stay
# running 2,326 points across 42 hours.
#
# The shell now establishes a stay in a few points and then stops standard
# location updates until the device leaves, which is the real fix — it saves the
# GPS as well as the rows, and a filter that discards a fix has still paid for
# it. This is the backstop, not a mirror of that rule: a released build stays on
# the phone for at least a TestFlight cycle, and every one of them keeps sending
# a point a minute. It is deliberately looser than the client, because the
# server cannot tell a stationary device from a quiet one and guessing wrong
# here deletes visits.
#
# The floors are not tuning knobs, they are what two downstream readers need.
# `_update_state_machine` opens a visit on HYSTERESIS_THRESHOLD consecutive
# pings at a place, and `location.db.reconcile_visits` discards a segment of
# fewer than `min_pings` (3) or shorter than `min_dwell_sec` (60) as a walk-by.
# So the first points of a stay are load-bearing and are never thinned; a rule
# that dropped them would delete visits, which is a far worse failure than the
# duplicate rows it set out to prevent.
DECLARED_ESTABLISHING_POINTS = 3
DECLARED_KEEPALIVE_SEC = 900.0
# ~11 cm. The coordinate arrives as the same JSON float every time, so this is
# an exactness tolerance rather than a spatial one; a reconfigured zone moves by
# orders of magnitude more, and a *measured* fix never reaches this test at all
# because the marker gates it first.
DECLARED_SAME_POINT_EPS_DEG = 1e-6


def _parse_ts(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def is_redundant_declared_point(
    recent: list, timestamp: str, lat: float, lon: float,
) -> bool:
    """Would storing this declared point add anything?

    ``recent`` is the newest ``DECLARED_ESTABLISHING_POINTS`` stored rows,
    newest first, each carrying ``timestamp``/``lat``/``lon``/``wifi_zone``.
    The caller has already established that the *incoming* point is declared.

    Suppress only when all of these hold, and read each one as a guard rather
    than a condition — any of them failing means something happened that a
    stored row should record:

    - There are already enough rows to have established the stay. Fewer means
      this point is one of the establishing ones.
    - Every one of them is declared, at this same coordinate. A measured fix or
      a different coordinate in the run means the device went somewhere and
      came back, or the zone was reconfigured, and the stay starts again.
    - The newest of them is less than the keepalive old. Past that, one point
      goes in so the stay stays visible to the live views and the day keeps a
      battery reading near its end.

    An out-of-order point (older than the newest stored row) is never
    suppressed: the run this reasons about is the tail of the table, and a
    backfilled point is not part of it. Same for an unparseable timestamp —
    the answer to "I cannot tell" is to store it.
    """
    if len(recent) < DECLARED_ESTABLISHING_POINTS:
        return False
    for row in recent:
        if not row["wifi_zone"]:
            return False
        if (abs(row["lat"] - lat) > DECLARED_SAME_POINT_EPS_DEG
                or abs(row["lon"] - lon) > DECLARED_SAME_POINT_EPS_DEG):
            return False
    try:
        gap = (_parse_ts(timestamp) - _parse_ts(recent[0]["timestamp"])).total_seconds()
    except (ValueError, TypeError):
        return False
    return 0 <= gap < DECLARED_KEEPALIVE_SEC


def _recent_pings_for_declared_check(conn: sqlite3.Connection) -> list:
    """The tail of the table, newest first. Indexed by
    ``idx_location_pings_time``, and only ever run for a declared point, so a
    measured fix pays nothing for this."""
    return conn.execute(
        "SELECT timestamp, lat, lon, wifi_zone FROM location_pings "
        "ORDER BY timestamp DESC LIMIT ?",
        (DECLARED_ESTABLISHING_POINTS,),
    ).fetchall()


def _get_user_db_path(user_id: str):
    """Resolve the per-user ``location.db`` path for an ingesting user.

    Caller must hold (or acquire+release) ``_lock`` around the dict read
    if it cares about a reload happening between the lookup and the open.
    Once the path is in hand, the DB file itself is stable across reloads
    — only the in-memory dict gets rebound.
    """
    return _user_contexts[user_id].db_path


def reload_config() -> None:
    """Reload config, token map, user contexts, and places cache.

    Tokens come from the encrypted ``secrets`` table per user; the
    location module gate decides which users get scanned. Users without
    an ingest token (or with the location module disabled, or with no
    nextcloud mount configured) don't appear in any of the three dicts
    and their requests fall through to the 403 path.

    The three dicts are rebound under ``_lock`` as a single block so any
    reader holding the lock sees a consistent snapshot.
    """
    from . import secrets_store  # noqa: PLC0415
    from .location import ingest_signal  # noqa: PLC0415

    global _config, _token_map, _user_contexts, _places_cache, _sentinel_stamp
    _config = load_config()

    # Read the sentinel *before* the secrets, not after: a token written
    # between the two would otherwise be marked as already-loaded and would
    # not apply until the next write.
    stamp = ingest_signal.reload_stamp(_config.db_path)

    token_map: dict[str, str] = {}
    user_contexts: dict[str, LocationContext] = {}
    places_cache: dict[str, list] = {}

    for user_id in location.list_users(_config):
        try:
            ctx = location.resolve_for_user(user_id, _config)
        except location.UserNotFoundError as e:
            logger.warning(
                "skipping user '%s' for location ingest: %s", user_id, e,
            )
            continue
        # Lazily ensure the per-user file exists so the first ping after
        # provisioning lands in a real DB (Stage 2's lazy init pairs
        # with Stage 3's data copy).
        location.init_db(ctx.db_path)
        tok = secrets_store.get_secret(
            _config.db_path, user_id, "overland", "ingest_token",
        )
        if not tok:
            continue
        token_map[tok] = user_id
        user_contexts[user_id] = ctx
        with location.connect(ctx.db_path) as conn:
            places_cache[user_id] = location.db.get_places(conn)

    with _lock:
        _token_map = token_map
        _user_contexts = user_contexts
        _places_cache = places_cache
        _sentinel_stamp = stamp

    logger.info(
        "Loaded location config: %d user(s) with tokens", len(_token_map),
    )


def _maybe_reload_for_signal() -> None:
    """Rebuild the token map if the web process has provisioned a token.

    The two live in different processes, so the web app stamps a sentinel
    file (see :mod:`istota.location.ingest_signal`) and this runs one
    ``os.stat`` per ingest request to notice. Without it a token generated
    in the browser 403s until the receiver is restarted.

    The new stamp is claimed *before* the reload rather than after, so
    concurrent uvicorn threads produce one rebuild rather than one each. A
    reload that then fails stays claimed: it means config loading is broken,
    and re-attempting it on every subsequent ping would rescan every user's
    DB in a tight loop. SIGHUP and restart remain the way out.
    """
    global _sentinel_stamp

    config = _config
    if config is None:
        return

    from .location import ingest_signal  # noqa: PLC0415

    stamp = ingest_signal.reload_stamp(config.db_path)
    if not stamp:
        return
    with _lock:
        if stamp <= _sentinel_stamp:
            return
        _sentinel_stamp = stamp

    logger.info("location ingest reload signalled; rebuilding token map")
    try:
        reload_config()
    except Exception:
        logger.exception("failed to reload location ingest config")


def _reload_config_on_signal() -> None:
    """SIGHUP reload that survives a config that no longer loads.

    `load_config` raises on a malformed config (`[email]
    outbound_approval_floor` is a deliberate hard failure — no fallback value
    for a security floor is safe to pick). Failing at startup is intended;
    letting the exception escape a *signal handler* into arbitrary main-thread
    bytecode is not. `_maybe_reload_for_signal` above already catches for the
    ingest path — this gives SIGHUP the same treatment, keeping the running
    token map rather than taking the receiver down on an operator's typo.
    """
    try:
        reload_config()
    except Exception:
        logger.exception(
            "SIGHUP config reload failed, keeping the previously loaded config",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The revision this process imported, not the one the checkout holds now.
    # See `build_info`.
    logger.info("STARTUP Running %s", build_description())
    reload_config()
    signal.signal(signal.SIGHUP, lambda *_: _reload_config_on_signal())
    yield


app = FastAPI(title="Istota Webhook Receiver", lifespan=lifespan)

location_router = APIRouter(prefix="/webhooks/location")


@location_router.post("")
async def receive_location(
    request: Request,
    token: str = Query(default=""),
):
    """Receive Overland GPS batch payload."""
    # Resolve token from query param or Authorization header
    auth_token = token
    if not auth_token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            auth_token = auth_header[7:].strip()

    if not auth_token:
        return JSONResponse({"error": "missing token"}, status_code=401)

    # Before the lookup, not after: a token provisioned seconds ago should
    # work on its first use, which is exactly when someone is watching.
    _maybe_reload_for_signal()

    with _lock:
        user_id = _token_map.get(auth_token)
        db_path = (
            _user_contexts[user_id].db_path
            if user_id and user_id in _user_contexts else None
        )

    if not user_id or db_path is None:
        return JSONResponse({"error": "invalid token"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    locations = body.get("locations", [])
    if not locations:
        return JSONResponse({"result": "ok"})

    try:
        with location.connect(db_path) as conn:
            # Refresh places from DB (picks up web UI changes without restart)
            places = location.db.get_places(conn)
            with _lock:
                _places_cache[user_id] = places

            for feature in locations:
                _process_feature(conn, feature, places)
            conn.commit()
    except Exception:
        logger.exception("Error processing location batch for %s", user_id)
        return JSONResponse({"error": "processing error"}, status_code=500)

    return JSONResponse({"result": "ok"})


app.include_router(location_router)


def _process_feature(
    conn: sqlite3.Connection,
    feature: dict,
    places: list,
) -> None:
    """Process a single GeoJSON Feature from Overland.

    ``conn`` is the per-user ``location.db`` connection, so no
    ``user_id`` parameter is needed — the file is the user scope.
    """
    geom = feature.get("geometry", {})
    coords = geom.get("coordinates", [])
    if len(coords) < 2:
        return

    lon, lat = coords[0], coords[1]
    props = feature.get("properties", {})

    timestamp = props.get("timestamp", iso_now())

    # Extract motion/activity — Overland uses "motion" array and/or "activity" string
    motion = props.get("motion", [])
    activity = props.get("activity", "")
    if motion and isinstance(motion, list):
        activity_type = motion[0]  # primary motion state
    elif activity:
        activity_type = activity
    else:
        activity_type = None

    speed = props.get("speed")
    if speed is not None and speed < 0:
        speed = None

    course = props.get("course")
    if course is not None and course < 0:
        course = None

    accuracy = props.get("horizontal_accuracy")
    vertical_accuracy = props.get("vertical_accuracy")

    # A wifi-zone point is a coordinate the client *declares* while the device
    # is on the configured home SSID, not a fix it took — so it has no
    # altitude, and the -1 the shell puts in that slot is its "unknown" rather
    # than a datum. Match on the marker the client already sends, never on the
    # value: -1 m is a legitimate altitude (Death Valley, the Salton Sea, most
    # of the Netherlands).
    wifi_zone = bool(props.get("wifi_zone"))

    if wifi_zone and is_redundant_declared_point(
        _recent_pings_for_declared_check(conn), timestamp, lat, lon,
    ):
        # The stay is already on record and nothing about it has changed.
        # Returning before the insert also skips the state machine, which is
        # correct: this point is not an arrival, and running it again would
        # only re-count a place the machine is already sitting on.
        return

    altitude = props.get("altitude")
    if wifi_zone:
        # Both halves go: the shell sends a vertical accuracy of 0 with the
        # declared point, and an accuracy of zero metres beside a NULL altitude
        # would tell a later reader the device took a perfect vertical fix.
        altitude = None
        vertical_accuracy = None
    elif vertical_accuracy is not None and vertical_accuracy < 0:
        # iOS reports a negative vertical accuracy to say the altitude beside
        # it is invalid — the same negative-sentinel convention speed and
        # course are scrubbed by above. The signal itself is kept, since it is
        # the reason the altitude is gone.
        altitude = None

    # Accuracy gate: only use good pings for place matching and state updates.
    # Low-accuracy pings are still stored for history so the map isn't empty.
    threshold = (
        _config.location.accuracy_threshold_m
        if _config is not None else DEFAULT_ACCURACY_THRESHOLD_M
    )
    low_accuracy = accuracy is not None and accuracy > threshold

    if low_accuracy:
        place = None
        place_id = None
    else:
        place = resolve_place(lat, lon, places)
        place_id = place.id if place else None

    ping_id = location.db.insert_ping(
        conn, timestamp, lat, lon,
        altitude=altitude,
        accuracy=accuracy,
        vertical_accuracy=vertical_accuracy,
        speed=speed,
        course=course,
        battery=props.get("battery_level"),
        activity_type=activity_type,
        wifi=props.get("wifi"),
        wifi_zone=wifi_zone,
        place_id=place_id,
        source="overland",
        client_id=props.get("client_id") or None,
    )

    if ping_id == 0:
        # Already stored under this client_id — a batch the device sent, and
        # kept, because our acknowledgement never arrived. Returning here is
        # the point of the id: running the state machine again would count a
        # resent point as a second arrival and manufacture a visit.
        return

    if low_accuracy:
        # Don't let a jittery ping move the state machine. The ping keeps its
        # place_id=NULL for history and stats.
        return

    _update_state_machine(conn, ping_id, place_id, place, timestamp)


def _update_state_machine(
    conn: sqlite3.Connection,
    ping_id: int,
    new_place_id: int | None,
    new_place,
    timestamp: str,
) -> None:
    """Run the state machine for visit tracking.

    Uses two asymmetric thresholds:
    - opening a visit: ``HYSTERESIS_THRESHOLD`` consecutive pings at the new
      place (filters walk-bys and single-ping GPS spikes).
    - closing an open visit: continuous "away" time must reach
      ``visit_exit_minutes`` (filters GPS drift while stationary). A single
      ping back at the place resets the away clock.
    """
    state = location.db.get_location_state(conn)
    exit_minutes = (
        _config.location.visit_exit_minutes
        if _config is not None else DEFAULT_VISIT_EXIT_MINUTES
    )

    if state is None:
        visit_id = None
        if new_place_id is not None:
            visit_id = location.db.open_visit(
                conn, new_place_id, new_place.name, timestamp,
            )

        location.db.set_location_state(
            conn,
            current_place_id=new_place_id,
            current_visit_id=visit_id,
            consecutive_count=1,
            last_ping_place_id=new_place_id,
            exit_started_at=None,
        )
        location.db.update_ping_place(conn, ping_id, new_place_id, visit_id)
        return

    current_place_id = state.current_place_id
    current_visit_id = state.current_visit_id

    if current_place_id is not None and new_place_id == current_place_id:
        # Back at (or still at) the current place — clear exit timer.
        if current_visit_id is not None:
            location.db.increment_visit_ping_count(conn, current_visit_id)
        location.db.set_location_state(
            conn,
            current_place_id=current_place_id,
            current_visit_id=current_visit_id,
            consecutive_count=0,
            last_ping_place_id=new_place_id,
            exit_started_at=None,
        )
        location.db.update_ping_place(
            conn, ping_id, new_place_id, current_visit_id,
        )
        return

    # This ping is away from (or different from) the current place.
    # 1) Check if the current visit should close based on dwell exit.
    # 2) Independently, build up hysteresis for opening a new visit.

    exit_started_at = state.exit_started_at
    should_close = False
    close_exit_ts = timestamp

    if current_visit_id is not None:
        if exit_started_at is None:
            exit_started_at = timestamp
        away_sec = (_parse_ts(timestamp) - _parse_ts(exit_started_at)).total_seconds()
        if away_sec >= exit_minutes * 60:
            should_close = True
            close_exit_ts = exit_started_at

    if new_place_id == state.last_ping_place_id:
        consecutive = state.consecutive_count + 1
    else:
        consecutive = 1

    open_new = (
        new_place_id is not None
        and new_place is not None
        and consecutive >= HYSTERESIS_THRESHOLD
    )

    # Opening at a *different* named place always closes the old visit, even
    # if the dwell threshold isn't met yet — the user clearly moved.
    if open_new and current_visit_id is not None:
        should_close = True
        # If we never recorded an exit start (user teleported directly from
        # place A to place B), fall back to this ping's timestamp.
        close_exit_ts = exit_started_at or timestamp

    if should_close:
        location.db.close_visit(conn, current_visit_id, close_exit_ts)
        current_place_id = None
        current_visit_id = None
        exit_started_at = None

    if open_new:
        new_visit_id = location.db.open_visit(
            conn, new_place_id, new_place.name, timestamp,
        )
        location.db.set_location_state(
            conn,
            current_place_id=new_place_id,
            current_visit_id=new_visit_id,
            consecutive_count=0,
            last_ping_place_id=new_place_id,
            exit_started_at=None,
        )
        location.db.update_ping_place(conn, ping_id, new_place_id, new_visit_id)
        return

    location.db.set_location_state(
        conn,
        current_place_id=current_place_id,
        current_visit_id=current_visit_id,
        consecutive_count=consecutive,
        last_ping_place_id=new_place_id,
        exit_started_at=exit_started_at,
    )
    # Ping keeps its observed place_id; visit_id follows the open visit if any.
    location.db.update_ping_place(conn, ping_id, new_place_id, current_visit_id)


# =============================================================================
# Haversine distance
# =============================================================================

from istota.geo import haversine  # noqa: E402


def resolve_place(lat: float, lon: float, places: list) -> object | None:
    """Find the nearest place within its radius. Returns Place or None."""
    best = None
    best_dist = float("inf")

    for place in places:
        dist = haversine(lat, lon, place.lat, place.lon)
        if dist <= place.radius_meters and dist < best_dist:
            best = place
            best_dist = dist

    return best
