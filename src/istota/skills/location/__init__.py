"""Location tracking skill — GPS data from Overland iOS app.

CLI:
    python -m istota.skills.location current
    python -m istota.skills.location history [--limit N] [--date YYYY-MM-DD] [--tz TZ]
    python -m istota.skills.location places
    python -m istota.skills.location learn NAME [--category CAT] [--radius N] [--notes TXT]
    python -m istota.skills.location update (--name NAME | --id ID) [--rename NEW] [--category CAT] [--radius N] [--notes TXT] [--lat N] [--lon N]
    python -m istota.skills.location delete (--name NAME | --id ID)
    python -m istota.skills.location reverse-geocode --lat N --lon N
    python -m istota.skills.location day-summary --date YYYY-MM-DD [--tz TZ]
    python -m istota.skills.location discover [--min-pings N]
    python -m istota.skills.location dismiss-cluster --lat N --lon N [--radius M]
    python -m istota.skills.location list-dismissed
    python -m istota.skills.location restore-dismissed CLUSTER_ID
    python -m istota.skills.location place-stats (--name NAME | --id ID)

Per-user split: per-user GPS data lives in ``location.db`` resolved
via ``LOCATION_DB_PATH`` (set by the ``setup_env`` hook below). Two
subcommands also need the framework-side geocode caches and read
``ISTOTA_DB_PATH`` for those.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def setup_env(ctx) -> dict[str, str]:
    """Inject LOCATION_DB_PATH for the per-user location.db.

    Self-gates on ``Config.is_module_enabled(user_id, "location")``.
    Returns ``{}`` (no env contribution) when the module is disabled
    for the user, when nextcloud_mount_path is unset, or when any
    other resolution gate fails.
    """
    from istota import location as _location  # noqa: PLC0415

    config = ctx.config
    user_id = ctx.task.user_id
    try:
        loc_ctx = _location.resolve_for_user(user_id, config)
    except _location.UserNotFoundError:
        return {}
    # Lazy init so the CLI works even before a GPS ping has ever arrived.
    # Mirrors the webhook receiver and the /location/* web routes, both of
    # which init_db before connecting. Without this a read on a fresh box
    # raises "unable to open database file" (the dir doesn't exist yet).
    _location.init_db(loc_ctx.db_path)
    return {"LOCATION_DB_PATH": str(loc_ctx.db_path)}


def _get_location_db_path() -> str:
    db_path = os.environ.get("LOCATION_DB_PATH", "")
    if not db_path:
        print(json.dumps({
            "status": "error", "error": "LOCATION_DB_PATH not set",
        }))
        sys.exit(1)
    return db_path


def _get_framework_db_path() -> str:
    """Path to framework istota.db — only needed for geocode caches."""
    db_path = os.environ.get("ISTOTA_DB_PATH", "")
    if not db_path:
        print(json.dumps({
            "status": "error", "error": "ISTOTA_DB_PATH not set",
        }))
        sys.exit(1)
    return db_path


def _connect_location() -> sqlite3.Connection:
    """Open a raw connection to per-user location.db.

    Subcommands use raw connections (rather than the package's
    contextmanager) because they read+commit+close inline; the manager
    pattern is awkward when the conn outlives a single ``with`` block.
    """
    conn = sqlite3.connect(_get_location_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _tz_name(args) -> str:
    return getattr(args, "tz", None) or os.environ.get("TZ", "America/Los_Angeles")


def cmd_current(args):
    from istota.location_logic import location_current

    print(json.dumps(location_current(_get_location_db_path())))


def cmd_history(args):
    from istota.location_logic import location_history, resolve_timezone, utc_day_bounds

    since = until = None
    if args.date:
        zone, _ = resolve_timezone(_tz_name(args))
        since, until = utc_day_bounds(args.date, zone)
        limit = args.limit or 0
    else:
        limit = args.limit or 20

    result = location_history(
        _get_location_db_path(),
        since=since,
        until=until,
        limit=limit,
        # Newest first, matching the undated branch. The map reads the same
        # window forwards; see location_history for why that is a parameter.
        order="desc",
    )
    print(json.dumps(result["pings"]))


def cmd_places(args):
    from istota.location_logic import location_places

    print(json.dumps(location_places(_get_location_db_path())["places"]))


def cmd_learn(args):
    from istota.location import db as location_db

    conn = _connect_location()

    name = args.name
    radius = args.radius or 100
    category = args.category or "other"
    notes = (getattr(args, "notes", None) or "").strip() or None

    cursor = conn.execute(
        "SELECT lat, lon, accuracy, timestamp FROM location_pings "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if not row:
        print(json.dumps({"status": "error", "error": "No location pings found"}))
        conn.close()
        sys.exit(1)

    lat, lon = row["lat"], row["lon"]
    location_db.upsert_place(
        conn, name, lat, lon,
        radius_meters=radius, category=category, notes=notes,
    )
    conn.commit()
    conn.close()

    print(json.dumps({
        "status": "ok",
        "place": name,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "radius_meters": radius,
        "notes": notes,
        "message": f"Saved '{name}' at {lat:.4f}, {lon:.4f}.",
    }))


def _resolve_place(conn, name=None, place_id=None):
    """Find a place by name or ID. Returns (place, error_msg)."""
    from istota.location import db as location_db

    if place_id is not None:
        place = location_db.get_place_by_id(conn, place_id)
        if not place:
            return None, f"No place found with ID {place_id}"
        return place, None
    if name:
        place = location_db.get_place_by_name(conn, name)
        if not place:
            return None, f"No place found with name '{name}'"
        return place, None
    return None, "Specify --name or --id"


def cmd_update(args):
    from istota.location import db as location_db

    conn = _connect_location()
    place, err = _resolve_place(conn, name=args.name, place_id=args.id)
    if err:
        print(json.dumps({"status": "error", "error": err}))
        conn.close()
        sys.exit(1)

    updates: dict = {}
    clear_notes = False
    if args.rename is not None:
        updates["name"] = args.rename
    if args.category is not None:
        updates["category"] = args.category
    if args.radius is not None:
        updates["radius_meters"] = args.radius
    if args.notes is not None:
        n = args.notes.strip()
        if n:
            updates["notes"] = n
        else:
            clear_notes = True
    if args.lat is not None:
        updates["lat"] = args.lat
    if args.lon is not None:
        updates["lon"] = args.lon

    if not updates and not clear_notes:
        print(json.dumps({"status": "error", "error": "No changes specified"}))
        conn.close()
        sys.exit(1)

    if updates:
        location_db.update_place(conn, place.id, **updates)
    if clear_notes:
        conn.execute("UPDATE places SET notes = NULL WHERE id = ?", (place.id,))
    conn.commit()

    updated = location_db.get_place_by_id(conn, place.id)
    conn.close()

    print(json.dumps({
        "status": "ok",
        "place": {
            "id": updated.id,
            "name": updated.name,
            "lat": updated.lat,
            "lon": updated.lon,
            "radius_meters": updated.radius_meters,
            "category": updated.category,
            "notes": updated.notes,
        },
    }))


def cmd_delete(args):
    from istota.location import db as location_db

    conn = _connect_location()
    place, err = _resolve_place(conn, name=args.name, place_id=args.id)
    if err:
        print(json.dumps({"status": "error", "error": err}))
        conn.close()
        sys.exit(1)

    place_name = place.name
    location_db.nullify_place_on_pings(conn, place.id)
    location_db.delete_place_by_id(conn, place.id)
    conn.commit()
    conn.close()

    print(json.dumps({"status": "ok", "deleted": place_name}))


_VIRTUAL_LOCATION_PATTERNS = [
    "zoom.us", "zoom", "meet.google", "teams.microsoft",
    "teams", "webex", "skype", "hangouts", "facetime",
    "google meet", "microsoft teams",
]


def _is_virtual_location(location: str) -> bool:
    loc_lower = location.lower()
    return any(p in loc_lower for p in _VIRTUAL_LOCATION_PATTERNS)


def _match_place(location_text: str, places):
    loc_lower = location_text.lower()
    for place in places:
        if place["name"].lower() in loc_lower or loc_lower in place["name"].lower():
            return {
                "name": place["name"],
                "lat": place["lat"],
                "lon": place["lon"],
                "radius_meters": place["radius_meters"],
            }
    return None


def _geocode_location(location_text: str, framework_conn):
    """Resolve location text to lat/lon via cache or Nominatim.

    Cache reads/writes go to framework istota.db (cross-user dedup).
    """
    from istota.db import get_cached_geocode, cache_geocode

    cached = get_cached_geocode(framework_conn, location_text)
    if cached:
        return cached

    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent="istota")
        result = geolocator.geocode(location_text, timeout=10)
        if result:
            cache_geocode(
                framework_conn, location_text,
                result.latitude, result.longitude,
            )
            framework_conn.commit()
            return (result.latitude, result.longitude)
    except Exception:
        pass

    return None


def cmd_attendance(args):
    """Cross-reference calendar events with GPS pings.

    Triple-DB: per-user location.db for pings/places, framework
    istota.db for the geocode cache, CalDAV for events.
    """
    from istota.geo import haversine
    from istota.skills.calendar import (
        CalendarEvent,
        get_caldav_client,
        list_calendars,
        get_events,
    )
    from istota.location import db as location_db
    from istota.location_logic import resolve_timezone
    from datetime import timedelta

    conn = _connect_location()
    framework_conn = sqlite3.connect(_get_framework_db_path())
    framework_conn.row_factory = sqlite3.Row

    tz, _ = resolve_timezone(os.environ.get("TZ", "America/Los_Angeles"))

    # The day bounds below are deliberately not `utc_day_bounds`: CalDAV wants
    # aware datetimes and this window starts from a `date`, where that helper
    # returns the UTC strings a ping query binds.
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = datetime.now(tz).date()

    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    caldav_url = os.environ.get("CALDAV_URL", "")
    caldav_user = os.environ.get("CALDAV_USERNAME", "")
    caldav_pass = os.environ.get("CALDAV_PASSWORD", "")

    if not all([caldav_url, caldav_user, caldav_pass]):
        print(json.dumps({"status": "error", "error": "CalDAV credentials not set (CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD)"}))
        conn.close()
        framework_conn.close()
        sys.exit(1)

    # ISSUE-101: DAVClient owns urllib3 pools whose watchdog threads
    # leak unless close() is called. Use try/finally because the function
    # already branches into sys.exit on errors.
    client = get_caldav_client(caldav_url, caldav_user, caldav_pass)
    try:
        try:
            calendars = list_calendars(client)
        except Exception as e:
            print(json.dumps({"status": "error", "error": f"Failed to list calendars: {e}"}))
            conn.close()
            framework_conn.close()
            sys.exit(1)

        all_events: list[CalendarEvent] = []
        for cal_name, cal_url in calendars:
            try:
                events = get_events(client, cal_url, day_start, day_end)
                all_events.extend(events)
            except Exception:
                continue
    finally:
        client.close()

    filtered = []
    for ev in all_events:
        if ev.all_day:
            continue
        if not ev.location:
            continue
        if _is_virtual_location(ev.location):
            continue
        if args.event:
            query = args.event.lower()
            if query != ev.uid.lower() and query not in ev.summary.lower():
                continue
        filtered.append(ev)

    if not filtered:
        print(json.dumps({"date": str(target_date), "events": []}))
        conn.close()
        framework_conn.close()
        return

    places_rows = conn.execute(
        "SELECT name, lat, lon, radius_meters FROM places"
    ).fetchall()
    places = [dict(r) for r in places_rows]

    default_radius = 200

    results = []
    for ev in filtered:
        event_lat, event_lon, radius = None, None, default_radius
        source = None

        place_match = _match_place(ev.location, places)
        if place_match:
            event_lat = place_match["lat"]
            event_lon = place_match["lon"]
            radius = place_match["radius_meters"]
            source = "place"
        else:
            coords = _geocode_location(ev.location, framework_conn)
            if coords:
                event_lat, event_lon = coords
                source = "geocode"

        entry = {
            "summary": ev.summary,
            "uid": ev.uid,
            "start": ev.start.isoformat(),
            "end": ev.end.isoformat(),
            "location": ev.location,
            "location_resolved": source is not None,
            "resolution_source": source,
        }

        if event_lat is None:
            entry["attended"] = None
            results.append(entry)
            continue

        entry["event_lat"] = round(event_lat, 6)
        entry["event_lon"] = round(event_lon, 6)
        entry["radius_meters"] = radius

        ev_start = ev.start
        ev_end = ev.end
        if ev_start.tzinfo is None:
            ev_start = ev_start.replace(tzinfo=tz)
        if ev_end.tzinfo is None:
            ev_end = ev_end.replace(tzinfo=tz)

        window_start = (ev_start - timedelta(minutes=30)).astimezone(timezone.utc)
        window_end = (ev_end + timedelta(minutes=30)).astimezone(timezone.utc)
        ping_since = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        ping_until = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        pings = location_db.get_pings(
            conn, since=ping_since, until=ping_until, limit=1000,
        )

        nearby_pings = []
        for ping in pings:
            dist = haversine(event_lat, event_lon, ping.lat, ping.lon)
            if dist <= radius:
                nearby_pings.append(ping)

        if nearby_pings:
            entry["attended"] = True
            entry["first_nearby_ping"] = nearby_pings[-1].timestamp
            entry["last_nearby_ping"] = nearby_pings[0].timestamp
            entry["nearby_ping_count"] = len(nearby_pings)
        else:
            entry["attended"] = None

        results.append(entry)

    print(json.dumps({"date": str(target_date), "events": results}))
    conn.close()
    framework_conn.close()


def cmd_reverse_geocode(args):
    """Reverse-geocode a lat/lon.

    Cache lookup goes to framework istota.db; this subcommand doesn't
    touch the per-user location.db at all.
    """
    from istota.geo import reverse_geocode

    framework_conn = sqlite3.connect(_get_framework_db_path())
    framework_conn.row_factory = sqlite3.Row
    try:
        result = reverse_geocode(args.lat, args.lon, framework_conn)
        print(json.dumps(result, indent=2))
    finally:
        framework_conn.close()


def cmd_day_summary(args):
    """Day summary with reverse-geocoded place names.

    Reads pings from per-user location.db; resolves ``unknown`` stops
    via reverse_geocode against the framework istota.db cache.
    """
    from istota.geo import reverse_geocode
    from istota.location import db as location_db
    from istota.location_logic import location_day_summary

    framework_path = Path(_get_framework_db_path())
    with location_db.with_geocode_conn(framework_path) as framework_conn:
        result = location_day_summary(
            _get_location_db_path(),
            day=args.date or None,
            tz=_tz_name(args),
            geocode=lambda lat, lon: reverse_geocode(lat, lon, framework_conn),
        )
    print(json.dumps(result, indent=2))


def cmd_discover(args):
    """Find clusters of stationary pings not assigned to any place."""
    from istota.location_logic import _location_discover_places

    db_path = _get_location_db_path()
    min_pings = getattr(args, "min_pings", None) or 10
    result = _location_discover_places(db_path, min_pings=min_pings)
    print(json.dumps(result, indent=2))


def cmd_dismiss_cluster(args):
    """Mark a cluster zone as dismissed so it stops surfacing in discover."""
    from istota.location_logic import _location_dismiss_cluster

    db_path = _get_location_db_path()
    radius = getattr(args, "radius", None) or 100
    result = _location_dismiss_cluster(
        db_path,
        {"lat": args.lat, "lon": args.lon, "radius_meters": radius},
    )
    print(json.dumps({"status": "ok", **result}, indent=2))


def cmd_list_dismissed(args):
    """List all dismissed cluster zones."""
    from istota.location_logic import _location_list_dismissed

    db_path = _get_location_db_path()
    result = _location_list_dismissed(db_path)
    print(json.dumps(result, indent=2))


def cmd_restore_dismissed(args):
    """Un-dismiss a cluster zone by id."""
    from istota.location_logic import _location_restore_dismissed

    db_path = _get_location_db_path()
    deleted = _location_restore_dismissed(db_path, args.cluster_id)
    if not deleted:
        print(json.dumps({"status": "error", "error": "dismissed cluster not found"}))
        return
    print(json.dumps({"status": "ok", "id": args.cluster_id}, indent=2))


def cmd_place_stats(args):
    """Visit statistics for a place."""
    from istota.location_logic import _location_place_stats
    from istota.location import db as location_db

    db_path = _get_location_db_path()

    place_id = getattr(args, "id", None)
    if place_id is None:
        conn = _connect_location()
        try:
            place = location_db.get_place_by_name(conn, args.name)
        finally:
            conn.close()
        if not place:
            print(json.dumps({
                "status": "error", "error": f"place '{args.name}' not found",
            }))
            return
        place_id = place.id

    result = _location_place_stats(db_path, place_id)
    if result is None:
        print(json.dumps({
            "status": "error", "error": "place not found",
        }))
        return
    print(json.dumps(result, indent=2))


def cmd_import_garmin_tracks(args):
    """Import Garmin watch GPS tracks into location history.

    Two modes, mirroring the health ``garmin-sync`` split:

    * **Direct** — ``ISTOTA_SECRET_KEY`` is in env (operator shell / the
      scheduler daemon). The importer decrypts the Garmin token blob and
      runs inline; the JSON result is printed.
    * **Delegated** — the key isn't present (sandboxed LLM Bash call from
      web chat / Talk). The master key needed to decrypt the Garmin tokens
      is stripped, so the command writes a deferred op
      (``task_<id>_garmin_import.json``) that the scheduler runs post-task in
      the daemon process, where the key lives. The user gets a notification
      with the result. (This branch is now only reachable from a genuinely
      unsandboxed caller that lacks the key: a sandboxed task's
      ``istota-skill`` call runs host-side through the proxy, and location.db
      is not in the sandbox at all.)
    """
    from istota import secrets_store

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        print(json.dumps({"status": "error", "error": "ISTOTA_USER_ID not set"}))
        sys.exit(1)
    days_back = args.days_back

    if secrets_store.secret_key_available():
        from istota.health import garmin as gm
        from istota.location.garmin_import import ImportOptions, import_tracks
        fw = os.environ.get("ISTOTA_DB_PATH", "")
        if not fw:
            print(json.dumps({"status": "error", "error": "ISTOTA_DB_PATH not set"}))
            sys.exit(1)
        try:
            result = import_tracks(
                user_id, framework_db_path=Path(fw),
                options=ImportOptions(days_back=days_back, dry_run=args.dry_run),
            )
        except gm.GarminAuthError as e:
            print(json.dumps({
                "status": "error",
                "error": f"Garmin not connected ({e}). Connect it in "
                         "Settings → Connected services.",
            }))
            sys.exit(1)
        except gm.GarminRateLimited:
            print(json.dumps({
                "status": "error",
                "error": "Garmin rate-limited — try again later.",
            }))
            sys.exit(1)
        print(json.dumps({"status": "ok", **result.to_dict()}))
        return

    # Delegated path.
    if args.dry_run:
        print(json.dumps({
            "status": "error",
            "error": "--dry-run is only available in direct mode (an operator "
                     "shell). From chat, run the real import or use the web UI.",
        }))
        sys.exit(1)
    deferred = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    task_id = os.environ.get("ISTOTA_TASK_ID", "")
    if not deferred or not task_id:
        print(json.dumps({
            "status": "error",
            "error": "Garmin track import needs ISTOTA_SECRET_KEY (direct) or a "
                     "task context to delegate. Use the web UI 'Import GPS "
                     "tracks' button under Settings → Connected services.",
        }))
        sys.exit(1)
    path = Path(deferred) / f"task_{task_id}_garmin_import.json"
    path.write_text(json.dumps({"days_back": days_back}), encoding="utf-8")
    print(json.dumps({
        "status": "ok",
        "queued": True,
        "message": f"Garmin track import queued (last {days_back} days). It runs "
                   "right after this task; new watch-recorded tracks will appear "
                   "in your location history, and you'll get a notification with "
                   "the result.",
    }))


def build_parser():
    parser = argparse.ArgumentParser(description="Location tracking CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("current", aliases=["last"], help="Current location and visit")

    hist = sub.add_parser("history", help="Recent location pings")
    hist.add_argument("--limit", type=int, default=0)
    hist.add_argument("--date", help="Filter by date (YYYY-MM-DD)")
    hist.add_argument("--tz", help="Timezone (default: TZ env var or America/Los_Angeles)")

    sub.add_parser("places", help="List known places")

    learn = sub.add_parser("learn", help="Save current location as a named place")
    learn.add_argument("name", help="Place name")
    learn.add_argument("--category", default="other", help="Place category")
    learn.add_argument("--radius", type=int, default=100, help="Geofence radius in meters")
    learn.add_argument("--notes", help="Optional free-text notes")

    update = sub.add_parser("update", help="Update an existing place")
    update_target = update.add_mutually_exclusive_group(required=True)
    update_target.add_argument("--name", help="Place name to update")
    update_target.add_argument("--id", type=int, help="Place ID to update")
    update.add_argument("--rename", help="New name")
    update.add_argument("--category", help="New category")
    update.add_argument("--radius", type=int, help="New radius in meters")
    update.add_argument("--notes", help="New notes")
    update.add_argument("--lat", type=float, help="New latitude")
    update.add_argument("--lon", type=float, help="New longitude")

    delete = sub.add_parser("delete", help="Delete a place")
    delete_target = delete.add_mutually_exclusive_group(required=True)
    delete_target.add_argument("--name", help="Place name to delete")
    delete_target.add_argument("--id", type=int, help="Place ID to delete")

    attend = sub.add_parser("attendance", help="Check calendar attendance via GPS")
    attend.add_argument("--date", help="Date to check (YYYY-MM-DD, default: today)")
    attend.add_argument("--event", help="Filter by event UID or title substring")

    rgeo = sub.add_parser("reverse-geocode", help="Reverse geocode a lat/lon pair")
    rgeo.add_argument("--lat", type=float, required=True)
    rgeo.add_argument("--lon", type=float, required=True)

    dsum = sub.add_parser("day-summary", help="Day summary with reverse-geocoded locations")
    dsum.add_argument("--date", help="Date (YYYY-MM-DD, default: today)")
    dsum.add_argument("--tz", help="Timezone (default: TZ env var or America/Los_Angeles)")

    disc = sub.add_parser("discover", help="Find unknown clusters of stationary pings")
    disc.add_argument("--min-pings", dest="min_pings", type=int, default=10,
                      help="Minimum pings for a cluster to surface (default 10)")

    dismiss = sub.add_parser("dismiss-cluster", help="Dismiss a cluster zone so it stops appearing in discover")
    dismiss.add_argument("--lat", type=float, required=True)
    dismiss.add_argument("--lon", type=float, required=True)
    dismiss.add_argument("--radius", type=int, default=100, help="Dismissal radius in meters (default 100)")

    sub.add_parser("list-dismissed", help="List dismissed cluster zones")

    restore = sub.add_parser("restore-dismissed", help="Un-dismiss a cluster zone by id")
    restore.add_argument("cluster_id", type=int, help="Dismissed cluster id")

    pstats = sub.add_parser("place-stats", help="Visit statistics for a place")
    pstats_target = pstats.add_mutually_exclusive_group(required=True)
    pstats_target.add_argument("--name", help="Place name")
    pstats_target.add_argument("--id", type=int, help="Place ID")

    gimport = sub.add_parser(
        "import-garmin-tracks",
        help="Import Garmin watch GPS tracks into location history "
             "(fills gaps where the phone tracker has no data)",
    )
    gimport.add_argument("--days-back", type=int, default=30,
                         help="How many days back to import (default 30)")
    gimport.add_argument("--dry-run", action="store_true",
                         help="Report what would import without writing "
                              "(direct/operator mode only)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "current": cmd_current,
        "last": cmd_current,  # alias — natural name an LLM reaches for
        "history": cmd_history,
        "places": cmd_places,
        "learn": cmd_learn,
        "update": cmd_update,
        "delete": cmd_delete,
        "attendance": cmd_attendance,
        "reverse-geocode": cmd_reverse_geocode,
        "day-summary": cmd_day_summary,
        "discover": cmd_discover,
        "dismiss-cluster": cmd_dismiss_cluster,
        "list-dismissed": cmd_list_dismissed,
        "restore-dismissed": cmd_restore_dismissed,
        "place-stats": cmd_place_stats,
        "import-garmin-tracks": cmd_import_garmin_tracks,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)
