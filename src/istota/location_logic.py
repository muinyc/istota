"""Location query helpers shared between the web API and the location skill.

These functions are pure SQL + lightweight math — no FastAPI/HTTP/auth
dependencies — so they can be called from both `web_app.py` and skill
subprocesses.

Per-user split: every helper takes a path to the per-user
``location.db`` (no ``user_id``). The file is the user scope.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from .geo import haversine
from .location import db as location_db


def _location_place_stats(db_path: str | Path, place_id: int) -> dict | None:
    """Visit statistics for a place, derived from ping data.

    Groups pings into visits by checking whether the user was seen
    elsewhere during gaps. A gap only splits a visit if there are pings
    at a different place (or unassigned pings far away) in between —
    GPS dropout while stationary indoors doesn't break a visit. Walk-bys
    (< 3 pings) are filtered out.
    """
    with location_db.connect(Path(db_path)) as conn:
        place = location_db.get_place_by_id(conn, place_id)
        if not place:
            return None

        rows = conn.execute(
            """
            SELECT timestamp FROM location_pings
            WHERE place_id = ?
            ORDER BY timestamp ASC
            """,
            (place_id,),
        ).fetchall()

        if not rows:
            return {
                "place_id": place_id,
                "total_visits": 0,
                "first_visit": None,
                "last_visit": None,
                "avg_duration_min": None,
                "total_duration_min": None,
                "longest_visit_min": None,
            }

        min_pings = 3  # filter out walk-bys
        segments: list[tuple[str, str, int]] = []
        visit_start = rows[0]["timestamp"]
        prev_ts = visit_start
        ping_count = 1

        for row in rows[1:]:
            ts = row["timestamp"]
            elsewhere = conn.execute(
                """
                SELECT 1 FROM location_pings
                WHERE place_id IS NOT ? AND place_id IS NOT NULL
                  AND timestamp > ? AND timestamp < ?
                LIMIT 1
                """,
                (place_id, prev_ts, ts),
            ).fetchone()
            if elsewhere:
                segments.append((visit_start, prev_ts, ping_count))
                visit_start = ts
                ping_count = 1
            else:
                ping_count += 1
            prev_ts = ts
        segments.append((visit_start, prev_ts, ping_count))

        visits = [(s, e) for s, e, c in segments if c >= min_pings]

        if not visits:
            return {
                "place_id": place_id,
                "total_visits": 0,
                "first_visit": None,
                "last_visit": None,
                "avg_duration_min": None,
                "total_duration_min": None,
                "longest_visit_min": None,
            }

        durations_sec = []
        for start, end in visits:
            try:
                dur = (
                    datetime.fromisoformat(end) - datetime.fromisoformat(start)
                ).total_seconds()
                durations_sec.append(dur)
            except (ValueError, TypeError):
                durations_sec.append(0)

        total_sec = sum(durations_sec)
        avg_sec = total_sec / len(durations_sec) if durations_sec else 0
        longest_sec = max(durations_sec) if durations_sec else 0

        return {
            "place_id": place_id,
            "total_visits": len(visits),
            "first_visit": visits[0][0],
            "last_visit": visits[-1][0],
            "avg_duration_min": round(avg_sec / 60),
            "total_duration_min": round(total_sec / 60),
            "longest_visit_min": round(longest_sec / 60),
        }


def _location_list_dismissed(db_path: str | Path) -> dict:
    with location_db.connect(Path(db_path)) as conn:
        rows = location_db.list_dismissed_clusters(conn)
        return {
            "dismissed": [
                {
                    "id": r.id,
                    "lat": r.lat,
                    "lon": r.lon,
                    "radius_meters": r.radius_meters,
                    "dismissed_at": r.dismissed_at,
                }
                for r in rows
            ]
        }


def _location_dismiss_cluster(db_path: str | Path, data: dict) -> dict:
    radius = int(data.get("radius_meters", 100))
    with location_db.connect(Path(db_path)) as conn:
        cluster_id = location_db.dismiss_cluster(
            conn, float(data["lat"]), float(data["lon"]), radius,
        )
        conn.commit()
        return {
            "id": cluster_id,
            "lat": float(data["lat"]),
            "lon": float(data["lon"]),
            "radius_meters": radius,
        }


def _location_restore_dismissed(db_path: str | Path, cluster_id: int) -> bool:
    with location_db.connect(Path(db_path)) as conn:
        deleted = location_db.restore_dismissed_cluster(conn, cluster_id)
        conn.commit()
        return deleted


def _location_discover_places(
    db_path: str | Path, min_pings: int = 10,
) -> dict:
    """Find clusters of stationary pings not assigned to any place."""
    with location_db.connect(Path(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT ROUND(lat, 4) as rlat, ROUND(lon, 4) as rlon,
                   AVG(lat) as avg_lat, AVG(lon) as avg_lon,
                   COUNT(*) as cnt,
                   MIN(timestamp) as first_seen, MAX(timestamp) as last_seen
            FROM location_pings
            WHERE place_id IS NULL
              AND (activity_type IS NULL OR activity_type = 'stationary')
            GROUP BY rlat, rlon
            HAVING cnt >= ?
            ORDER BY cnt DESC
            """,
            (max(3, min_pings // 3),),
        ).fetchall()

        points = [
            {"lat": r["avg_lat"], "lon": r["avg_lon"], "count": r["cnt"],
             "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
            for r in rows
        ]

        clusters: list[dict] = []
        used = [False] * len(points)
        for i, p in enumerate(points):
            if used[i]:
                continue
            cluster_lat = p["lat"] * p["count"]
            cluster_lon = p["lon"] * p["count"]
            cluster_count = p["count"]
            first = p["first_seen"]
            last = p["last_seen"]
            members = [(p["lat"], p["lon"])]
            used[i] = True

            for j in range(i + 1, len(points)):
                if used[j]:
                    continue
                if haversine(p["lat"], p["lon"], points[j]["lat"], points[j]["lon"]) <= 200:
                    cluster_lat += points[j]["lat"] * points[j]["count"]
                    cluster_lon += points[j]["lon"] * points[j]["count"]
                    cluster_count += points[j]["count"]
                    members.append((points[j]["lat"], points[j]["lon"]))
                    if points[j]["first_seen"] < first:
                        first = points[j]["first_seen"]
                    if points[j]["last_seen"] > last:
                        last = points[j]["last_seen"]
                    used[j] = True

            if cluster_count >= min_pings:
                center_lat = cluster_lat / cluster_count
                center_lon = cluster_lon / cluster_count
                spread = max(
                    (haversine(center_lat, center_lon, mlat, mlon)
                     for mlat, mlon in members),
                    default=0.0,
                )
                radius_meters = int(min(300, max(50, round(spread + 25))))
                clusters.append({
                    "lat": center_lat,
                    "lon": center_lon,
                    "total_pings": cluster_count,
                    "first_seen": first,
                    "last_seen": last,
                    "radius_meters": radius_meters,
                })

        existing = conn.execute(
            "SELECT lat, lon, radius_meters FROM places"
        ).fetchall()
        dismissed = conn.execute(
            "SELECT lat, lon, radius_meters FROM dismissed_clusters"
        ).fetchall()
        filtered = []
        for c in clusters:
            too_close = False
            for ep in existing:
                dist = haversine(c["lat"], c["lon"], ep["lat"], ep["lon"])
                if dist <= max(ep["radius_meters"], 200):
                    too_close = True
                    break
            if too_close:
                continue
            for dz in dismissed:
                if haversine(c["lat"], c["lon"], dz["lat"], dz["lon"]) <= dz["radius_meters"]:
                    too_close = True
                    break
            if not too_close:
                filtered.append(c)

        return {"clusters": filtered}


# ===========================================================================
# The query pipeline both surfaces read (F2)
# ===========================================================================
#
# ``day_summary``, ``current``, ``history`` and ``places`` used to exist
# twice — once in ``skills/location/__init__.py`` for the model and once
# in ``web_app.py`` for the browser — and the two copies had drifted: only
# the web copy snapped a stop to its saved place's centre, only the skill
# copy carried the address parts and ``duration_minutes``, and an empty
# day came back under two different key sets. Nothing chose any of that.
#
# What stays at each surface is what is genuinely the surface's: the JSON
# envelope it has always printed, its own limit default, and — for
# ``location_history`` — the sort direction, which under a ``LIMIT``
# selects a different set of rows and is therefore a query parameter
# rather than a leftover difference. The reverse-geocode cache lives in
# the framework database, which the two surfaces reach by different
# routes, so it arrives as the ``geocode`` callable.
#
# Pinned by tests/test_location_surface_parity.py.


def resolve_timezone(tz: str | tzinfo | None) -> tuple[tzinfo, str]:
    """Return ``(zone, name)`` for a timezone given as a name or an object.

    An unresolvable name falls back to ``America/Los_Angeles`` — the
    behaviour both copies already had — while the *name* returned is the
    one that was asked for. That asymmetry is deliberate: the payload's
    ``timezone`` field reports the request, so a caller can see that what
    it asked for is not what it got.

    ``""`` is therefore reported as ``""`` rather than as the default it
    resolves to, which is not a nicety: `_get_location_config` hands the
    web route an empty string for a user with no timezone on their
    profile, and the copy this replaced reported it verbatim. Only
    ``None`` — nobody asked — reports the default as the answer.
    """
    if tz is None or isinstance(tz, str):
        name = "America/Los_Angeles" if tz is None else tz
        try:
            return ZoneInfo(name), name
        except Exception:
            return ZoneInfo("America/Los_Angeles"), name
    return tz, getattr(tz, "key", str(tz))


def utc_day_bounds(day: str, zone: tzinfo) -> tuple[str, str]:
    """The UTC half-open bounds ``[since, until)`` of one local calendar day."""
    day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
    day_end = day_start + timedelta(days=1)
    return (
        day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        day_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def location_current(db_path: str | Path, *, tz: str | tzinfo | None = None) -> dict:
    """The most recent ping, and the visit that is still open.

    ``tz`` is accepted for symmetry with the other three entry points and
    is not read: every timestamp in this payload is the stored UTC string,
    and the one derived value — how long the open visit has been running —
    is a duration, which no timezone changes.
    """
    del tz
    with location_db.connect(Path(db_path)) as conn:
        row = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.altitude, lp.accuracy,
                   lp.activity_type, lp.battery, lp.wifi,
                   p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            ORDER BY lp.timestamp DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            return {"last_ping": None, "current_visit": None}

        last_ping = {
            "timestamp": row["timestamp"],
            "lat": row["lat"],
            "lon": row["lon"],
            # Metres as the device reported them; what they are measured
            # against varies by source and is not recorded. Null where the
            # fix was horizontal only, where the device flagged it
            # vertically invalid, and where the point is one the client
            # declared rather than measured (ISSUE-229).
            "altitude": row["altitude"],
            "accuracy": row["accuracy"],
            "activity_type": row["activity_type"],
            "battery": row["battery"],
            "wifi": row["wifi"],
            "place": row["place_name"],
        }

        visit_row = conn.execute(
            """
            SELECT place_name, entered_at, ping_count
            FROM visits
            WHERE exited_at IS NULL
            ORDER BY entered_at DESC LIMIT 1
            """
        ).fetchone()
        current_visit = None
        if visit_row:
            entered = visit_row["entered_at"]
            try:
                entered_dt = datetime.fromisoformat(entered)
                if entered_dt.tzinfo is None:
                    entered_dt = entered_dt.replace(tzinfo=timezone.utc)
                duration_min = int(
                    (datetime.now(timezone.utc) - entered_dt).total_seconds() / 60
                )
            except (ValueError, TypeError):
                duration_min = None
            current_visit = {
                "place_name": visit_row["place_name"],
                "entered_at": entered,
                "duration_minutes": duration_min,
                "ping_count": visit_row["ping_count"],
            }

        return {"last_ping": last_ping, "current_visit": current_visit}


_PING_COLUMNS = """
    SELECT lp.timestamp, lp.lat, lp.lon, lp.altitude, lp.accuracy,
           lp.activity_type, lp.speed, lp.battery,
           p.name as place_name
    FROM location_pings lp
    LEFT JOIN places p ON lp.place_id = p.id
"""


def location_history(
    db_path: str | Path,
    *,
    since: str | None,
    until: str | None,
    limit: int,
    order: str = "asc",
) -> dict:
    """Pings across ``[since, until)``, or the most recent ``limit`` of them.

    ``order`` applies to the bounded query only. Unbounded, the question
    is "the newest ``limit`` pings", and the descending sort is what
    *selects* them rather than how they are presented — so that branch is
    always newest-first, as both copies already were.

    Bounded, the direction is a real parameter: with a ``LIMIT`` it picks
    the start of the window or its end. The map draws a polyline and reads
    the day forwards; the skill reads it newest-first, matching its own
    unbounded branch.

    ``limit`` of 0 means no limit on the bounded query and 100 on the
    unbounded one, which is what each caller relies on today.

    An ``order`` that is neither raises rather than defaulting. Under a
    ``LIMIT`` the direction selects a different set of rows, so a typo
    silently answering with the other end of the day is the one failure
    here that reads as data rather than as a bug.
    """
    if order not in ("asc", "desc"):
        raise ValueError(f"order must be 'asc' or 'desc', got {order!r}")
    direction = "ASC" if order == "asc" else "DESC"
    with location_db.connect(Path(db_path)) as conn:
        if since and until:
            query = (
                _PING_COLUMNS
                + " WHERE lp.timestamp >= ? AND lp.timestamp < ?"
                + f" ORDER BY lp.timestamp {direction}"
            )
            params: list = [since, until]
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, params).fetchall()
        else:
            rows = conn.execute(
                _PING_COLUMNS + " ORDER BY lp.timestamp DESC LIMIT ?",
                (limit or 100,),
            ).fetchall()

        pings = [
            {
                "timestamp": r["timestamp"],
                "lat": r["lat"],
                "lon": r["lon"],
                # See location_current for the three reasons this is null.
                "altitude": r["altitude"],
                "accuracy": r["accuracy"],
                "place": r["place_name"],
                "activity_type": r["activity_type"],
                "speed": r["speed"],
                "battery": r["battery"],
            }
            for r in rows
        ]
        return {"pings": pings, "count": len(pings)}


def location_places(db_path: str | Path) -> dict:
    with location_db.connect(Path(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, name, lat, lon, radius_meters, category, notes "
            "FROM places ORDER BY name"
        ).fetchall()
        return {
            "places": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "radius_meters": r["radius_meters"],
                    "category": r["category"],
                    "notes": r["notes"],
                }
                for r in rows
            ]
        }


def location_day_summary(
    db_path: str | Path,
    *,
    day: date | str | None = None,
    tz: str | tzinfo | None = None,
    saved_places: list[dict] | None = None,
    geocode: Callable[[float, float], dict] | None = None,
) -> dict:
    """One local day's stops, named and timed.

    ``day`` defaults to today in ``tz`` and ``saved_places`` to the places
    table in ``db_path``; both are parameters so a caller that has already
    read them need not read them twice.

    ``geocode`` resolves a coordinate to an address dict. It is injected
    because the reverse-geocode cache lives in the framework database,
    which the skill reaches through ``ISTOTA_DB_PATH`` and the web app
    through the loaded config. ``None`` means no reverse geocoding is
    available and an unnamed stop is reported as ``unknown``.
    """
    from .geo import (
        cluster_pings,
        dedupe_near_duplicate_pings,
        filter_transit_clusters,
        merge_consecutive_stops,
    )

    zone, tz_name = resolve_timezone(tz)
    if day is None:
        target_date = datetime.now(zone).strftime("%Y-%m-%d")
    elif isinstance(day, str):
        target_date = day
    else:
        target_date = day.isoformat()

    since_utc, until_utc = utc_day_bounds(target_date, zone)

    with location_db.connect(Path(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.activity_type, lp.accuracy, lp.speed,
                   lp.place_id, p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            WHERE lp.timestamp >= ? AND lp.timestamp < ?
            ORDER BY lp.timestamp ASC
            """,
            (since_utc, until_utc),
        ).fetchall()

        if not rows:
            return {
                "date": target_date,
                "timezone": tz_name,
                "ping_count": 0,
                "transit_pings": 0,
                "stops": [],
            }

        # A stop still running at local midnight has no departure inside
        # the window; the first later ping somewhere else is what ends it.
        closing_ping = None
        last_place_id = rows[-1]["place_id"]
        if last_place_id is not None:
            closing_row = conn.execute(
                """
                SELECT lp.timestamp, lp.lat, lp.lon, lp.activity_type, lp.accuracy, lp.speed,
                       lp.place_id, p.name as place_name
                FROM location_pings lp
                LEFT JOIN places p ON lp.place_id = p.id
                WHERE lp.timestamp >= ? AND (lp.place_id IS NULL OR lp.place_id != ?)
                ORDER BY lp.timestamp ASC
                LIMIT 1
                """,
                (until_utc, last_place_id),
            ).fetchone()
            if closing_row is not None:
                closing_ping = dict(closing_row)

        pings = dedupe_near_duplicate_pings([dict(r) for r in rows])
        clusters = cluster_pings(pings, radius_m=250, closing_ping=closing_ping)

        if saved_places is None:
            saved_places = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, name, lat, lon, radius_meters FROM places"
                ).fetchall()
            ]

    stops, transit_pings = filter_transit_clusters(clusters)

    for stop in stops:
        if stop["place_name"]:
            stop["location"] = stop["place_name"]
            stop["location_source"] = "saved_place"
            # Report the place's own centre rather than the centroid of
            # whichever pings landed: two visits to one place otherwise
            # plot a few metres apart.
            for sp in saved_places:
                if sp["name"] == stop["place_name"]:
                    stop["lat"] = sp["lat"]
                    stop["lon"] = sp["lon"]
                    break
        else:
            matched = False
            for sp in saved_places:
                dist = haversine(stop["lat"], stop["lon"], sp["lat"], sp["lon"])
                if dist <= max(sp["radius_meters"], 100):
                    stop["location"] = sp["name"]
                    stop["location_source"] = "saved_place_proximity"
                    stop["lat"] = sp["lat"]
                    stop["lon"] = sp["lon"]
                    matched = True
                    break

            if not matched:
                geo = geocode(stop["lat"], stop["lon"]) if geocode else {}
                stop["location"] = (
                    geo.get("suburb")
                    or geo.get("neighborhood")
                    or geo.get("road")
                    or geo.get("city")
                    or "unknown"
                )
                stop["location_source"] = geo.get("source", "unknown")
                stop["road"] = geo.get("road")
                stop["neighborhood"] = geo.get("neighborhood")
                stop["suburb"] = geo.get("suburb")

        for key in ("first_ts", "last_ts"):
            try:
                utc_dt = datetime.fromisoformat(stop[key]).replace(tzinfo=timezone.utc)
                stop[key + "_local"] = utc_dt.astimezone(zone).strftime("%H:%M")
            except Exception:
                stop[key + "_local"] = stop[key]

    merged = merge_consecutive_stops(stops)

    for s in merged:
        try:
            first = datetime.fromisoformat(s["first_ts"]).replace(tzinfo=timezone.utc)
            last = datetime.fromisoformat(s["last_ts"]).replace(tzinfo=timezone.utc)
            s["duration_minutes"] = int((last - first).total_seconds() / 60)
        except (ValueError, TypeError):
            s["duration_minutes"] = None

    return {
        "date": target_date,
        "timezone": tz_name,
        "ping_count": len(pings),
        "transit_pings": transit_pings,
        "stops": [
            {
                "location": s["location"],
                "location_source": s.get("location_source"),
                "road": s.get("road"),
                "neighborhood": s.get("neighborhood"),
                "suburb": s.get("suburb"),
                "arrived": s.get("first_ts_local"),
                "departed": s.get("last_ts_local"),
                "duration_minutes": s.get("duration_minutes"),
                "ping_count": s["ping_count"],
                "lat": round(s["lat"], 5),
                "lon": round(s["lon"], 5),
            }
            for s in merged
        ],
    }
