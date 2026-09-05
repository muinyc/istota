"""The two location surfaces answer the same question the same way.

The location query pipeline used to exist twice: once in
``skills/location/__init__.py`` for the model and once in ``web_app.py``
for the browser. The two copies drifted — the web copy snapped a stop to
its saved place's centre and the skill copy did not, the skill copy
carried the ``road``/``neighborhood``/``suburb`` enrichment and
``duration_minutes`` and the web copy dropped them, and an empty day came
back under two different key sets. Neither divergence was anybody's
decision; they are what two copies do.

So this file is a pin rather than a feature test. It works in three
layers, because the obvious one alone does not hold the property:

* The equality assertions catch a surface growing a branch its twin does
  not have — a wrapper argument diverging, an envelope being rebuilt.
  They cannot catch a re-inlining, because two identical copies are
  equal; that is what the third layer is for.
* The single-surface assertions pin each divergence this stage resolved
  against the copy that lost, on the surface that gained it. Those are
  the ones that fail against the pre-change code.
* `TestNeitherSurfaceQueriesPingsItself` is the grep-shaped guard the
  spec's test strategy names, and it is what actually fails when a
  second copy comes back.

Two differences survive on purpose and are asserted rather than
normalised away, because each is a property of the *surface* and not of
the pipeline:

* The skill prints a bare JSON list for ``history`` and ``places`` where
  the web route returns ``{"pings": [...]}`` / ``{"places": [...]}``.
  Those are the shapes each caller already reads.
* A date-filtered ``history`` comes back newest-first for the skill and
  oldest-first for the map, which draws a polyline from it. With a
  ``LIMIT`` the sort direction selects a different set, so it is a real
  query parameter, not a leftover.

Each surface gets its own framework database. The reverse-geocode cache
lives there, so a shared one would answer the second surface from cache
and label the stop ``location_source: "cache"`` against the first
surface's ``"nominatim"`` — a fixture artefact that reads exactly like a
divergence.
"""

import io
import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

try:
    import geopy  # noqa: F401
    _has_geopy = True
except ImportError:
    _has_geopy = False

try:
    import fastapi  # noqa: F401
    _has_fastapi = True
except ImportError:
    _has_fastapi = False

pytestmark = [
    pytest.mark.skipif(not _has_geopy, reason="geopy not installed"),
    pytest.mark.skipif(not _has_fastapi, reason="fastapi not installed"),
]

from istota import db as framework_db_mod
from istota.location import db as location_db

TZ = "America/Los_Angeles"
DAY = "2026-03-08"

# 2026-03-08 is the US spring-forward Sunday, chosen for that: the local day
# is 23 hours, so it spans 2026-03-08T08:00:00Z to 2026-03-09T07:00:00Z and
# not to 08:00Z. OUT_OF_DAY_PINGS sits in the hour between the two, which is
# what gives that arithmetic a witness — a naive 24-hour window would sweep it
# in. Both copies got this right; nothing in the suite said so.
PLACES = [{"name": "home", "lat": 34.05, "lon": -118.25, "radius_meters": 150}]
PINGS = [
    # A stop at the saved place, centred slightly off it so the web copy's
    # snap-to-centre shows up in the rounded output coordinates.
    {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.0510, "lon": -118.2510,
     "place_id": 1, "battery": 0.82, "wifi": "home-wifi"},
    {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.0511, "lon": -118.2511,
     "place_id": 1, "battery": 0.81, "wifi": "home-wifi"},
    {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.0512, "lon": -118.2512,
     "place_id": 1, "battery": 0.80, "wifi": "home-wifi"},
    # One transit ping between the two stops.
    {"timestamp": "2026-03-08T17:00:00Z", "lat": 34.10, "lon": -118.30,
     "activity_type": "driving", "speed": 18.0},
    # A stop with no saved place — this one goes through reverse geocoding.
    {"timestamp": "2026-03-08T18:00:00Z", "lat": 34.1800, "lon": -118.3300,
     "battery": 0.66, "wifi": None},
    {"timestamp": "2026-03-08T18:05:00Z", "lat": 34.1801, "lon": -118.3301,
     "battery": 0.65, "wifi": None},
    {"timestamp": "2026-03-08T18:10:00Z", "lat": 34.1802, "lon": -118.3302,
     "battery": 0.64, "wifi": None, "altitude": 141.5},
]

# 07:30Z on the 9th: past the local day's real end (07:00Z) and inside a naive
# 24-hour one. Nothing that asks for 2026-03-08 may return it.
OUT_OF_DAY_PINGS = [
    {"timestamp": "2026-03-09T07:30:00Z", "lat": 34.2000, "lon": -118.4000,
     "battery": 0.50, "wifi": None},
]


def _nominatim_result():
    result = MagicMock()
    result.address = "789 Elm St, Burbank, CA"
    result.raw = {"address": {
        "road": "Elm St",
        "neighbourhood": "Chandler",
        "suburb": "Magnolia Park",
        "city": "Burbank",
    }}
    return result


def _seed(tmp_path, name):
    """One location.db plus a framework.db of its own."""
    loc_db = tmp_path / f"{name}-location.db"
    location_db.init_db(loc_db)
    framework_db = tmp_path / f"{name}-istota.db"
    framework_db_mod.init_db(framework_db)

    with location_db.connect(loc_db) as conn:
        for place in PLACES:
            location_db.add_place(
                conn, place["name"], place["lat"], place["lon"],
                radius_meters=place["radius_meters"], category="other",
            )
        for ping in PINGS + OUT_OF_DAY_PINGS:
            location_db.insert_ping(
                conn, ping["timestamp"], ping["lat"], ping["lon"],
                altitude=ping.get("altitude"),
                accuracy=ping.get("accuracy", 5.0),
                speed=ping.get("speed"),
                battery=ping.get("battery"),
                wifi=ping.get("wifi"),
                activity_type=ping.get("activity_type"),
                place_id=ping.get("place_id"),
            )
        conn.execute(
            "INSERT INTO visits (place_id, place_name, entered_at, ping_count) "
            "VALUES (?, ?, ?, ?)",
            (1, "home", "2026-03-08T16:00:00Z", 3),
        )
        conn.commit()
    return loc_db, framework_db


def _run_skill(fn, loc_db, framework_db, **args):
    """Call a skill subcommand and parse the JSON it prints."""
    env = {"LOCATION_DB_PATH": str(loc_db), "ISTOTA_DB_PATH": str(framework_db)}
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        with patch.dict("os.environ", env, clear=False), \
                patch("geopy.geocoders.Nominatim",
                      return_value=MagicMock(reverse=MagicMock(
                          return_value=_nominatim_result()))):
            fn(SimpleNamespace(**args))
    finally:
        sys.stdout = old_stdout
    return json.loads(captured.getvalue())


def _run_web(fn, framework_db, *call_args, **call_kwargs):
    """Call a web query helper with ``_config`` pointed at its framework db."""
    from istota import web_app

    with patch.object(web_app, "_config", SimpleNamespace(db_path=framework_db)), \
            patch("geopy.geocoders.Nominatim",
                  return_value=MagicMock(reverse=MagicMock(
                      return_value=_nominatim_result()))):
        return fn(*call_args, **call_kwargs)


class TestDaySummaryParity:
    def test_the_two_surfaces_return_the_same_day_summary(self, tmp_path):
        from istota.skills.location import cmd_day_summary
        from istota.web_app import _location_query_day_summary

        skill_loc, skill_fw = _seed(tmp_path, "skill")
        web_loc, web_fw = _seed(tmp_path, "web")

        skill = _run_skill(cmd_day_summary, skill_loc, skill_fw, date=DAY, tz=TZ)
        web = _run_web(_location_query_day_summary, web_fw,
                       str(web_loc), TZ, DAY)

        assert skill == web

    def test_the_fixture_day_actually_exercises_both_branches(self, tmp_path):
        """Guard on the fixture, not the product.

        Parity is trivially true over a payload with no stops in it. This
        pins that the day carries one saved-place stop (the snapping
        branch) and one reverse-geocoded stop (the enrichment branch), so
        the assertion above is comparing something.
        """
        from istota.web_app import _location_query_day_summary

        web_loc, web_fw = _seed(tmp_path, "web")
        summary = _run_web(_location_query_day_summary, web_fw,
                           str(web_loc), TZ, DAY)

        sources = [s["location_source"] for s in summary["stops"]]
        assert "saved_place" in sources
        assert "nominatim" in sources
        assert summary["transit_pings"] >= 1

    def test_a_saved_place_stop_is_snapped_to_the_place_centre(self, tmp_path):
        """The behaviour the skill gains: pings 15m off centre report the centre."""
        from istota.skills.location import cmd_day_summary

        loc_db, framework_db = _seed(tmp_path, "skill")
        summary = _run_skill(cmd_day_summary, loc_db, framework_db, date=DAY, tz=TZ)

        home = [s for s in summary["stops"] if s["location"] == "home"]
        assert len(home) == 1
        assert (home[0]["lat"], home[0]["lon"]) == (34.05, -118.25)

    def test_a_geocoded_stop_carries_the_address_parts(self, tmp_path):
        """The behaviour the web copy gains."""
        from istota.web_app import _location_query_day_summary

        web_loc, web_fw = _seed(tmp_path, "web")
        summary = _run_web(_location_query_day_summary, web_fw,
                           str(web_loc), TZ, DAY)

        geocoded = [s for s in summary["stops"] if s["location_source"] == "nominatim"]
        assert len(geocoded) == 1
        assert geocoded[0]["road"] == "Elm St"
        assert geocoded[0]["neighborhood"] == "Chandler"
        assert geocoded[0]["suburb"] == "Magnolia Park"
        assert geocoded[0]["duration_minutes"] == 10

    def test_the_skill_reports_duration_minutes_too(self, tmp_path):
        """The other half of the pin: the skill is where this one came from.

        Asserted per surface rather than only through the equality, because
        equality is satisfied by both surfaces being wrong together.
        """
        from istota.skills.location import cmd_day_summary

        loc_db, framework_db = _seed(tmp_path, "skill")
        summary = _run_skill(cmd_day_summary, loc_db, framework_db, date=DAY, tz=TZ)

        # 53 rather than the 10 minutes of pings: ISSUE-332's closing ping —
        # the transit fix at 17:00Z — is where the user was last seen, so the
        # stop runs to the estimated departure rather than to its last ping.
        home = [s for s in summary["stops"] if s["location"] == "home"]
        assert home[0]["duration_minutes"] == 53

    def test_the_local_day_ends_before_a_naive_twenty_four_hours_would(self, tmp_path):
        """Spring forward: the day is 23 hours and the 07:30Z ping is outside it."""
        from istota.skills.location import cmd_day_summary
        from istota.web_app import _location_query_day_summary

        skill_loc, skill_fw = _seed(tmp_path, "skill")
        web_loc, web_fw = _seed(tmp_path, "web")

        skill = _run_skill(cmd_day_summary, skill_loc, skill_fw, date=DAY, tz=TZ)
        web = _run_web(_location_query_day_summary, web_fw, str(web_loc), TZ, DAY)

        assert skill["ping_count"] == web["ping_count"] == len(PINGS)

    def test_an_empty_day_returns_the_same_keys_on_both_surfaces(self, tmp_path):
        from istota.skills.location import cmd_day_summary
        from istota.web_app import _location_query_day_summary

        skill_loc, skill_fw = _seed(tmp_path, "skill")
        web_loc, web_fw = _seed(tmp_path, "web")

        skill = _run_skill(cmd_day_summary, skill_loc, skill_fw,
                           date="2026-03-01", tz=TZ)
        web = _run_web(_location_query_day_summary, web_fw,
                       str(web_loc), TZ, "2026-03-01")

        assert skill == web
        assert skill == {
            "date": "2026-03-01", "timezone": TZ,
            "stops": [], "ping_count": 0, "transit_pings": 0,
        }


class TestCurrentParity:
    def test_the_two_surfaces_return_the_same_current_position(self, tmp_path):
        from istota.skills.location import cmd_current
        from istota.web_app import _location_query_current

        skill_loc, skill_fw = _seed(tmp_path, "skill")
        web_loc, web_fw = _seed(tmp_path, "web")

        skill = _run_skill(cmd_current, skill_loc, skill_fw)
        web = _run_web(_location_query_current, web_fw, str(web_loc))

        # duration_minutes on an open visit is measured against "now", so
        # the two calls can straddle a minute boundary. Everything else is
        # compared verbatim.
        assert skill["last_ping"] == web["last_ping"]
        assert skill["current_visit"].keys() == web["current_visit"].keys()
        assert skill["current_visit"]["place_name"] == web["current_visit"]["place_name"]
        assert skill["current_visit"]["entered_at"] == web["current_visit"]["entered_at"]
        assert skill["current_visit"]["ping_count"] == web["current_visit"]["ping_count"]

    def test_current_reports_the_wifi_the_ping_carried(self, tmp_path):
        """The web copy selected the column and dropped it on the way out."""
        from istota.web_app import _location_query_current

        web_loc, web_fw = _seed(tmp_path, "web")
        current = _run_web(_location_query_current, web_fw, str(web_loc))

        assert "wifi" in current["last_ping"]

    def test_the_skill_still_reports_wifi(self, tmp_path):
        """The surface `wifi` came from. Pinned so the winner cannot flip."""
        from istota.skills.location import cmd_current

        loc_db, framework_db = _seed(tmp_path, "skill")
        current = _run_skill(cmd_current, loc_db, framework_db)

        assert "wifi" in current["last_ping"]

    def test_an_empty_database_answers_the_same_on_both_surfaces(self, tmp_path):
        from istota.skills.location import cmd_current
        from istota.web_app import _location_query_current

        empty_loc = tmp_path / "empty-location.db"
        location_db.init_db(empty_loc)
        empty_fw = tmp_path / "empty-istota.db"
        framework_db_mod.init_db(empty_fw)

        skill = _run_skill(cmd_current, empty_loc, empty_fw)
        web = _run_web(_location_query_current, empty_fw, str(empty_loc))

        assert skill == web == {"last_ping": None, "current_visit": None}


class TestPlacesParity:
    def test_the_two_surfaces_return_the_same_places(self, tmp_path):
        from istota.skills.location import cmd_places
        from istota.web_app import _location_query_places

        skill_loc, skill_fw = _seed(tmp_path, "skill")
        web_loc, web_fw = _seed(tmp_path, "web")

        skill = _run_skill(cmd_places, skill_loc, skill_fw)
        web = _run_web(_location_query_places, web_fw, str(web_loc))

        # The envelope is the surface's, the list is the pipeline's.
        assert skill == web["places"]
        assert skill[0]["name"] == "home"


class TestHistoryParity:
    def test_the_two_surfaces_return_the_same_pings_for_a_day(self, tmp_path):
        from istota.skills.location import cmd_history
        from istota.web_app import _location_query_pings

        skill_loc, skill_fw = _seed(tmp_path, "skill")
        web_loc, web_fw = _seed(tmp_path, "web")

        skill = _run_skill(cmd_history, skill_loc, skill_fw,
                           date=DAY, tz=TZ, limit=0)
        web = _run_web(_location_query_pings, web_fw, str(web_loc), TZ,
                       DAY, None, None, 0)

        by_ts = lambda rows: sorted(rows, key=lambda p: p["timestamp"])  # noqa: E731
        assert by_ts(skill) == by_ts(web["pings"])
        assert len(skill) == len(PINGS)

    def test_each_surface_keeps_its_own_sort_direction(self, tmp_path):
        """Not a divergence: with a LIMIT the direction selects a different set.

        The map draws a polyline and wants the day forwards; the skill's
        undated branch is newest-first and its dated branch matches it.
        """
        from istota.skills.location import cmd_history
        from istota.web_app import _location_query_pings

        skill_loc, skill_fw = _seed(tmp_path, "skill")
        web_loc, web_fw = _seed(tmp_path, "web")

        skill = _run_skill(cmd_history, skill_loc, skill_fw,
                           date=DAY, tz=TZ, limit=0)
        web = _run_web(_location_query_pings, web_fw, str(web_loc), TZ,
                       DAY, None, None, 0)

        assert skill[0]["timestamp"] > skill[-1]["timestamp"]
        assert web["pings"][0]["timestamp"] < web["pings"][-1]["timestamp"]

    def test_under_a_limit_the_direction_selects_a_different_set(self, tmp_path):
        """The justification for keeping `order` per surface, asserted.

        Without a LIMIT the two directions hold the same rows reversed, so
        the test above would pass even if the direction were presentational.
        With one, the map takes the start of the day and the skill the end —
        different rows, which is what makes this a query parameter.
        """
        from istota.skills.location import cmd_history
        from istota.web_app import _location_query_pings

        skill_loc, skill_fw = _seed(tmp_path, "skill")
        web_loc, web_fw = _seed(tmp_path, "web")

        skill = _run_skill(cmd_history, skill_loc, skill_fw,
                           date=DAY, tz=TZ, limit=2)
        web = _run_web(_location_query_pings, web_fw, str(web_loc), TZ,
                       DAY, None, None, 2)

        assert [p["timestamp"] for p in web["pings"]] == [
            PINGS[0]["timestamp"], PINGS[1]["timestamp"],
        ]
        assert [p["timestamp"] for p in skill] == [
            PINGS[-1]["timestamp"], PINGS[-2]["timestamp"],
        ]

    def test_an_unknown_sort_direction_raises(self, tmp_path):
        """A typo must not answer with the other end of the day."""
        from istota.location_logic import location_history

        loc_db, _ = _seed(tmp_path, "skill")
        with pytest.raises(ValueError):
            location_history(str(loc_db), since=None, until=None,
                             limit=5, order="ascending")

    def test_the_undated_branch_agrees_on_both_surfaces(self, tmp_path):
        from istota.skills.location import cmd_history
        from istota.web_app import _location_query_pings

        skill_loc, skill_fw = _seed(tmp_path, "skill")
        web_loc, web_fw = _seed(tmp_path, "web")

        skill = _run_skill(cmd_history, skill_loc, skill_fw,
                           date=None, tz=TZ, limit=3)
        web = _run_web(_location_query_pings, web_fw, str(web_loc), TZ,
                       None, None, None, 3)

        assert skill == web["pings"]
        assert len(skill) == 3


class TestSharedHelpers:
    """The two widened parameters, each of which has a branch no surface takes.

    `location_day_summary` accepts a `date` for `day` and a `tzinfo` for `tz`
    as well as the strings both callers pass. Untested those are two branches
    that exist on the strength of a type annotation.
    """

    def test_a_date_object_names_the_same_day_as_its_iso_string(self, tmp_path):
        from istota.location_logic import location_day_summary

        loc_db, _ = _seed(tmp_path, "skill")
        as_string = location_day_summary(str(loc_db), day=DAY, tz=TZ)
        as_date = location_day_summary(str(loc_db), day=date(2026, 3, 8), tz=TZ)

        assert as_date == as_string

    def test_a_tzinfo_object_reports_its_own_key(self, tmp_path):
        from istota.location_logic import location_day_summary

        loc_db, _ = _seed(tmp_path, "skill")
        summary = location_day_summary(
            str(loc_db), day=DAY, tz=ZoneInfo(TZ),
        )

        assert summary["timezone"] == TZ

    def test_an_unset_timezone_is_reported_as_asked_rather_than_as_resolved(self):
        """`_get_location_config` hands the web route "" for an unset profile.

        The copy this replaced reported that verbatim, so the payload's
        `timezone` field keeps saying what was asked for. Only `None` — nobody
        asked at all — reports the default as the answer.
        """
        from istota.location_logic import resolve_timezone

        zone, name = resolve_timezone("")
        assert name == ""
        assert getattr(zone, "key", None) == "America/Los_Angeles"

        _, default_name = resolve_timezone(None)
        assert default_name == "America/Los_Angeles"


class TestNeitherSurfaceQueriesPingsItself:
    """The guard that actually fails when a second copy comes back.

    Every assertion above is satisfied by two identical copies, because two
    identical copies are equal. This is the one that is not: the pipeline is
    `location_logic`'s, so neither surface may read `location_pings` for a
    query or reach for the clustering primitives. The spec's test strategy
    names a grep-shaped guard for exactly this, following
    `tests/test_lint_scope.py`.

    Deliberately not a whole-tree sweep. The ingest path, the migrator, the
    Garmin importer and the travel-timezone detector all read that table for
    their own reasons; what this stage removed was the *query pipeline* being
    written twice, so the guard names the two surfaces it was written in.
    """

    SURFACES = (
        "src/istota/skills/location/__init__.py",
        "src/istota/web_app.py",
    )

    # A statement against the table, rather than the bare table name — which
    # also appears in `api_location_pings`, the route's own function name.
    STATEMENTS = (
        "FROM location_pings",
        "UPDATE location_pings",
        "INTO location_pings",
    )

    # What each surface still legitimately does with the table. None of it is
    # the query pipeline: the first two are the place-editing routes
    # reassigning `place_id`, the third is `cmd_learn` reading the newest ping
    # to site a new saved place, the fourth is the admin dashboard's
    # freshness probe.
    ALLOWED_SUBSTRINGS = (
        "UPDATE location_pings SET place_id",
        "SELECT id, lat, lon FROM location_pings",
        "SELECT lat, lon, accuracy, timestamp FROM location_pings ",
        "SELECT MAX(timestamp) AS ts FROM location_pings",
    )

    CLUSTERING = (
        "cluster_pings",
        "filter_transit_clusters",
        "merge_consecutive_stops",
        "dedupe_near_duplicate_pings",
    )

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def test_no_surface_reaches_for_the_clustering_primitives(self):
        root = self._repo_root()
        offenders = []
        for rel in self.SURFACES:
            body = (root / rel).read_text(encoding="utf-8")
            for name in self.CLUSTERING:
                if name in body:
                    offenders.append(f"{rel}: {name}")
        assert offenders == [], (
            "the day-summary pipeline belongs to location_logic; a surface "
            f"reaching for it directly is the second copy coming back: {offenders}"
        )

    def test_no_surface_runs_an_unaccounted_ping_query(self):
        root = self._repo_root()
        offenders = []
        for rel in self.SURFACES:
            for lineno, line in enumerate(
                (root / rel).read_text(encoding="utf-8").splitlines(), 1
            ):
                if not any(stmt in line for stmt in self.STATEMENTS):
                    continue
                if any(allowed in line for allowed in self.ALLOWED_SUBSTRINGS):
                    continue
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert offenders == [], (
            "a surface is reading location_pings for itself again; move the "
            f"query into location_logic or account for it here: {offenders}"
        )

    def test_the_guard_can_fail(self):
        """The allowlist must not be wide enough to swallow a real re-inlining.

        The line below is verbatim what `location_history` and
        `location_day_summary` both contain; if a surface grew it back, the
        guard has to catch it.
        """
        reinlined = "            FROM location_pings lp"
        assert any(stmt in reinlined for stmt in self.STATEMENTS)
        assert not any(a in reinlined for a in self.ALLOWED_SUBSTRINGS)

        # And the route's own name must not read as a query.
        route = "async def api_location_pings("
        assert not any(stmt in route for stmt in self.STATEMENTS)


def test_a_fixture_day_is_a_real_date():
    """Cheap guard: the fixture timestamps have to sit inside the target day."""
    assert datetime.strptime(DAY, "%Y-%m-%d")
