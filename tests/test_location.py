"""Tests for location tracking: loader, DB functions, haversine, state machine, CLI."""

import io
import json
import sys
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

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

_needs_geopy = pytest.mark.skipif(not _has_geopy, reason="geopy not installed")
_needs_fastapi = pytest.mark.skipif(not _has_fastapi, reason="fastapi not installed")

from istota import db
from istota.geo import haversine
from istota.location import db as location_db

if _has_fastapi:
    from istota.webhook_receiver import resolve_place


def _init_db(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


def _init_loc_db(tmp_path, name: str = "location.db"):
    """Initialise a per-user location.db at ``tmp_path / name`` and return
    the path. Used by tests that exercise post-Stage-2 APIs whose ``db_path``
    arg now refers to the per-user file rather than framework istota.db."""
    db_path = tmp_path / name
    location_db.init_db(db_path)
    return db_path


# ===========================================================================
# DB function tests
# ===========================================================================


# Per-user equivalents of the framework db.* helper tests live in
# tests/test_location_module.py — TestLocationPingDB / TestPlaceDB /
# TestDismissedClusterDB / TestLocationStateDB were removed in Stage 4
# along with the framework helpers themselves.


@_needs_fastapi
class TestPlaceNotesAPI:
    def test_create_persists_notes(self, tmp_path):
        from istota.web_app import _location_create_place, _location_query_places

        db_path = _init_loc_db(tmp_path)
        _location_create_place(str(db_path), {
            "name": "office", "lat": 34.0, "lon": -118.0,
            "radius_meters": 100, "category": "work",
            "notes": "side entrance, 4th floor",
        })

        result = _location_query_places(str(db_path))
        assert result["places"][0]["notes"] == "side entrance, 4th floor"

    def test_create_empty_notes_stored_as_null(self, tmp_path):
        from istota.web_app import _location_create_place, _location_query_places

        db_path = _init_loc_db(tmp_path)
        _location_create_place(str(db_path), {
            "name": "office", "lat": 34.0, "lon": -118.0,
            "notes": "   ",
        })

        result = _location_query_places(str(db_path))
        assert result["places"][0]["notes"] is None

    def test_update_changes_notes(self, tmp_path):
        from istota.web_app import _location_create_place, _location_update_place

        db_path = _init_loc_db(tmp_path)
        created = _location_create_place(str(db_path), {
            "name": "office", "lat": 34.0, "lon": -118.0, "notes": "old",
        })

        result = _location_update_place(str(db_path), created["id"], {
            "notes": "new"
        })
        assert result["notes"] == "new"

    def test_update_empty_notes_clears_field(self, tmp_path):
        from istota.web_app import _location_create_place, _location_update_place

        db_path = _init_loc_db(tmp_path)
        created = _location_create_place(str(db_path), {
            "name": "office", "lat": 34.0, "lon": -118.0, "notes": "to be cleared",
        })

        result = _location_update_place(str(db_path), created["id"], {
            "notes": ""
        })
        assert result["notes"] is None


@_needs_fastapi
class TestDiscoverPlacesFiltersDismissed:
    def _seed_cluster(self, conn, lat, lon, count=15):
        for i in range(count):
            ts = f"2026-01-10T09:{i:02d}:00Z"
            # Tiny jitter so they share a rounded grid cell
            location_db.insert_ping(
                conn, ts, lat + (i % 3) * 0.00002, lon,
                accuracy=5.0, activity_type="stationary",
            )

    def test_unknown_cluster_appears(self, tmp_path):
        from istota.web_app import _location_discover_places

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            self._seed_cluster(conn, 34.0, -118.0)
            conn.commit()

        result = _location_discover_places(str(db_path), min_pings=10)
        assert len(result["clusters"]) == 1
        assert "radius_meters" in result["clusters"][0]

    def test_dismissed_cluster_is_filtered(self, tmp_path):
        from istota.web_app import _location_discover_places

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            self._seed_cluster(conn, 34.0, -118.0)
            location_db.dismiss_cluster(conn, 34.0, -118.0, 200)
            conn.commit()

        result = _location_discover_places(str(db_path), min_pings=10)
        assert result["clusters"] == []

    def test_dismissed_zone_only_affects_owner(self, tmp_path):
        from istota.web_app import _location_discover_places

        # Per-user split: alice and bob now live in separate location.db
        # files. Seed both, dismiss in alice's only, assert isolation.
        alice_db = _init_loc_db(tmp_path, name="alice.db")
        bob_db = _init_loc_db(tmp_path, name="bob.db")
        with location_db.connect(alice_db) as conn:
            self._seed_cluster(conn, 34.0, -118.0)
            location_db.dismiss_cluster(conn, 34.0, -118.0, 200)
            conn.commit()
        with location_db.connect(bob_db) as conn:
            self._seed_cluster(conn, 34.0, -118.0)
            conn.commit()

        assert _location_discover_places(str(alice_db), min_pings=10)["clusters"] == []
        assert len(_location_discover_places(str(bob_db), min_pings=10)["clusters"]) == 1

    def test_distant_dismissal_does_not_filter(self, tmp_path):
        from istota.web_app import _location_discover_places

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            self._seed_cluster(conn, 34.0, -118.0)
            # Dismissed zone in a different city
            location_db.dismiss_cluster(conn, 40.0, -73.0, 200)
            conn.commit()

        result = _location_discover_places(str(db_path), min_pings=10)
        assert len(result["clusters"]) == 1


class TestVisitDB:
    def test_insert_and_close(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 34.0, -118.0)
            vid = location_db.open_visit(conn, pid, "home", "2026-02-20T08:00:00")
            conn.commit()

            visit = location_db.get_open_visit(conn)
            assert visit is not None
            assert visit.place_name == "home"
            assert visit.exited_at is None

            location_db.close_visit(conn, vid, "2026-02-20T10:00:00")
            conn.commit()

            assert location_db.get_open_visit(conn) is None

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].exited_at == "2026-02-20T10:00:00"
            assert visits[0].duration_sec > 0

    def test_increment_ping_count(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            vid = location_db.open_visit(conn, None, "unknown", "2026-02-20T08:00:00")
            location_db.increment_visit_ping_count(conn, vid)
            location_db.increment_visit_ping_count(conn, vid)
            conn.commit()

            visit = location_db.get_open_visit(conn)
            assert visit.ping_count == 3  # 1 initial + 2 increments


@_needs_fastapi
class TestPlaceStats:
    def _add_pings(self, conn, place_id, timestamps):
        """Insert pings at a place for given ISO timestamps."""
        for ts in timestamps:
            location_db.insert_ping(
                conn, ts, 34.0, -118.0,
                accuracy=5.0, activity_type="stationary",
                place_id=place_id,
            )

    def test_no_pings(self, tmp_path):
        from istota.web_app import _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "home", 34.0, -118.0,
                radius_meters=150, category="home",
            )
            conn.commit()

        result = _location_place_stats(str(db_path), pid)
        assert result is not None
        assert result["total_visits"] == 0
        assert result["first_visit"] is None

    def test_single_visit_from_pings(self, tmp_path):
        from istota.web_app import _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "cafe", 34.0, -118.0,
                radius_meters=100, category="food",
            )
            self._add_pings(conn, pid, [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:30:00Z",
                "2026-01-10T10:00:00Z",
            ])
            conn.commit()

        result = _location_place_stats(str(db_path), pid)
        assert result["total_visits"] == 1
        assert result["avg_duration_min"] == 60
        assert result["total_duration_min"] == 60

    def test_gap_without_elsewhere_is_same_visit(self, tmp_path):
        """A long gap with no pings at other places should NOT split the visit."""
        from istota.web_app import _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "cafe", 34.0, -118.0,
                radius_meters=100, category="food",
            )
            self._add_pings(conn, pid, [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:10:00Z",
                # 2-hour gap — no pings elsewhere
                "2026-01-10T11:10:00Z",
                "2026-01-10T11:15:00Z",
                "2026-01-10T11:20:00Z",
            ])
            conn.commit()

        result = _location_place_stats(str(db_path), pid)
        assert result["total_visits"] == 1
        assert result["total_duration_min"] == 140  # 09:00 to 11:20

    def test_two_visits_split_by_elsewhere(self, tmp_path):
        """Pings at another place during a gap should split into two visits."""
        from istota.web_app import _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_cafe = location_db.add_place(
                conn, "cafe", 34.0, -118.0,
                radius_meters=100, category="food",
            )
            pid_gym = location_db.add_place(
                conn, "gym", 34.01, -118.01,
                radius_meters=100, category="gym",
            )
            self._add_pings(conn, pid_cafe, [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:10:00Z",
                "2026-01-10T09:15:00Z",
                "2026-01-10T09:20:00Z",
            ])
            self._add_pings(conn, pid_gym, [
                "2026-01-10T10:00:00Z",
                "2026-01-10T10:05:00Z",
            ])
            self._add_pings(conn, pid_cafe, [
                "2026-01-10T11:20:00Z",
                "2026-01-10T11:25:00Z",
                "2026-01-10T11:30:00Z",
                "2026-01-10T11:35:00Z",
            ])
            conn.commit()

        result = _location_place_stats(str(db_path), pid_cafe)
        assert result["total_visits"] == 2
        assert result["first_visit"] == "2026-01-10T09:00:00Z"
        assert result["last_visit"] == "2026-01-10T11:20:00Z"

    def test_walkby_filtered(self, tmp_path):
        """A visit with fewer than 3 pings (walk-by) should not count."""
        from istota.web_app import _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "cafe", 34.0, -118.0,
                radius_meters=100, category="food",
            )
            self._add_pings(conn, pid, [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
            ])
            conn.commit()

        result = _location_place_stats(str(db_path), pid)
        assert result["total_visits"] == 0

    def test_wrong_user_returns_none(self, tmp_path):
        """Per-user split: a place that doesn't exist in *this* db
        returns None. (The previous "wrong user" semantics is now
        implicit in choosing the wrong db file.)"""
        from istota.web_app import _location_place_stats

        alice_db = _init_loc_db(tmp_path, name="alice.db")
        bob_db = _init_loc_db(tmp_path, name="bob.db")
        with location_db.connect(alice_db) as conn:
            pid = location_db.add_place(
                conn, "cafe", 34.0, -118.0,
                radius_meters=100, category="food",
            )
            conn.commit()

        result = _location_place_stats(str(bob_db), pid)
        assert result is None

    def test_nonexistent_place_returns_none(self, tmp_path):
        from istota.web_app import _location_place_stats

        db_path = _init_loc_db(tmp_path)
        result = _location_place_stats(str(db_path), 9999)
        assert result is None


@_needs_fastapi
class TestPlaceUpdateReassignment:
    def test_move_place_reassigns_pings(self, tmp_path):
        """Moving a place center should reassign pings to match the new geofence."""
        from istota.web_app import _location_update_place, _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "cafe", 34.0, -118.0,
                radius_meters=50, category="food",
            )
            for ts in [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:10:00Z",
                "2026-01-10T09:15:00Z",
            ]:
                location_db.insert_ping(
                    conn, ts, 34.0001, -118.0,
                    accuracy=5.0, place_id=pid,
                )
            for ts in [
                "2026-02-10T10:00:00Z",
                "2026-02-10T10:05:00Z",
                "2026-02-10T10:10:00Z",
            ]:
                location_db.insert_ping(conn, ts, 34.001, -118.0, accuracy=5.0)
            conn.commit()

        stats = _location_place_stats(str(db_path), pid)
        assert stats["total_visits"] == 1

        _location_update_place(str(db_path), pid, {"lat": 34.001, "lon": -118.0})

        stats = _location_place_stats(str(db_path), pid)
        assert stats["total_visits"] == 1
        assert stats["first_visit"] == "2026-02-10T10:00:00Z"

    def test_radius_change_reassigns_pings(self, tmp_path):
        """Expanding radius should pick up nearby unassigned pings."""
        from istota.web_app import _location_update_place, _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "cafe", 34.0, -118.0,
                radius_meters=25, category="food",
            )
            for ts in [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:10:00Z",
            ]:
                location_db.insert_ping(conn, ts, 34.00035, -118.0, accuracy=5.0)
            conn.commit()

        stats = _location_place_stats(str(db_path), pid)
        assert stats["total_visits"] == 0

        _location_update_place(str(db_path), pid, {"radius_meters": 100})

        stats = _location_place_stats(str(db_path), pid)
        assert stats["total_visits"] == 1


# ===========================================================================
# Haversine + place resolution tests
# ===========================================================================


class TestHaversine:
    def test_same_point(self):
        assert haversine(34.0, -118.0, 34.0, -118.0) == 0.0

    def test_known_distance(self):
        # NYC to LA ~ 3944 km
        dist = haversine(40.7128, -74.0060, 34.0522, -118.2437)
        assert 3930_000 < dist < 3960_000

    def test_short_distance(self):
        # ~111 m per 0.001 degree latitude
        dist = haversine(34.000, -118.0, 34.001, -118.0)
        assert 100 < dist < 120


@_needs_fastapi
class TestResolvePlace:
    def test_within_radius(self):
        from istota.location.models import Place
        places = [Place(1, "home", 34.0, -118.0, 200, "home", "", None)]
        result = resolve_place(34.0001, -118.0001, places)
        assert result is not None
        assert result.name == "home"

    def test_outside_radius(self):
        from istota.location.models import Place
        places = [Place(1, "home", 34.0, -118.0, 50, "home", "", None)]
        result = resolve_place(35.0, -119.0, places)
        assert result is None

    def test_nearest_wins(self):
        from istota.location.models import Place
        places = [
            Place(1, "far", 34.01, -118.0, 5000, "other", "", None),
            Place(2, "near", 34.0001, -118.0001, 5000, "other", "", None),
        ]
        result = resolve_place(34.0, -118.0, places)
        assert result.name == "near"

    def test_empty_places(self):
        assert resolve_place(34.0, -118.0, []) is None


# ===========================================================================
# State machine tests
# ===========================================================================


@_needs_fastapi
class TestStateMachine:
    """Tests for the state machine logic in webhook_receiver."""

    def _process(self, conn, place_id, place, timestamp):
        from istota.webhook_receiver import _update_state_machine
        ping_id = location_db.insert_ping(conn, timestamp, 0.0, 0.0)
        _update_state_machine(conn, ping_id, place_id, place, timestamp)
        return ping_id

    def test_first_ping_at_place(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 34.0, -118.0)
            place = location_db.get_place_by_name(conn, "home")

            self._process(conn, pid, place, "2026-02-20T10:00:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id == pid
            assert state.current_visit_id is not None

            visit = location_db.get_open_visit(conn)
            assert visit.place_name == "home"

    def test_first_ping_no_place(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            self._process(conn, None, None, "2026-02-20T10:00:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id is None
            assert state.current_visit_id is None

    def test_same_place_no_transition(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 34.0, -118.0)
            place = location_db.get_place_by_name(conn, "home")

            self._process(conn, pid, place, "2026-02-20T10:00:00Z")
            self._process(conn, pid, place, "2026-02-20T10:05:00Z")
            self._process(conn, pid, place, "2026-02-20T10:10:00Z")

            visits = location_db.get_visits(conn)
            assert len(visits) == 1  # still one visit
            assert visits[0].ping_count == 3

    def test_hysteresis_prevents_single_ping_transition(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_home = location_db.add_place(conn, "home", 34.0, -118.0)
            pid_gym = location_db.add_place(conn, "gym", 34.1, -118.1)
            home = location_db.get_place_by_name(conn, "home")
            gym = location_db.get_place_by_name(conn, "gym")

            self._process(conn, pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, pid_home, home, "2026-02-20T10:05:00Z")

            self._process(conn, pid_gym, gym, "2026-02-20T10:10:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id == pid_home
            assert state.consecutive_count == 1

    def test_hysteresis_allows_transition_after_threshold(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_home = location_db.add_place(conn, "home", 34.0, -118.0)
            pid_gym = location_db.add_place(conn, "gym", 34.1, -118.1)
            home = location_db.get_place_by_name(conn, "home")
            gym = location_db.get_place_by_name(conn, "gym")

            self._process(conn, pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, pid_home, home, "2026-02-20T10:05:00Z")

            self._process(conn, pid_gym, gym, "2026-02-20T10:10:00Z")
            self._process(conn, pid_gym, gym, "2026-02-20T10:15:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id == pid_gym

            visits = location_db.get_visits(conn)
            assert len(visits) == 2
            home_visit = [v for v in visits if v.place_name == "home"][0]
            assert home_visit.exited_at is not None
            gym_visit = [v for v in visits if v.place_name == "gym"][0]
            assert gym_visit.exited_at is None

    def test_transition_from_place_to_unknown(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_home = location_db.add_place(conn, "home", 34.0, -118.0)
            home = location_db.get_place_by_name(conn, "home")

            self._process(conn, pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, pid_home, home, "2026-02-20T10:05:00Z")

            self._process(conn, None, None, "2026-02-20T10:10:00Z")
            self._process(conn, None, None, "2026-02-20T10:15:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id is None

    def test_transition_fires_without_errors(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_home = location_db.add_place(conn, "home", 34.0, -118.0)
            pid_gym = location_db.add_place(conn, "gym", 34.1, -118.1)
            home = location_db.get_place_by_name(conn, "home")
            gym = location_db.get_place_by_name(conn, "gym")

            self._process(conn, pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, pid_home, home, "2026-02-20T10:05:00Z")

            self._process(conn, pid_gym, gym, "2026-02-20T10:10:00Z")
            self._process(conn, pid_gym, gym, "2026-02-20T10:15:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id == pid_gym


# ===========================================================================
# Overland payload parsing tests
# ===========================================================================


@_needs_fastapi
class TestOverlandPayloadParsing:
    """Test that the receiver correctly parses Overland GeoJSON payloads."""

    def test_parse_feature_coordinates(self):
        """Verify coordinate extraction from GeoJSON Feature."""
        from istota.webhook_receiver import _process_feature

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [-122.030581, 37.331800],
            },
            "properties": {
                "timestamp": "2026-02-20T10:30:00-0700",
                "altitude": 80,
                "speed": 0,
                "horizontal_accuracy": 5,
                "motion": ["stationary"],
                "battery_level": 0.92,
                "wifi": "home-wifi",
            },
        }

        db_path = _init_loc_db(Path(pytest.importorskip("tempfile").mkdtemp()))
        with location_db.connect(db_path) as conn:
            _process_feature(conn, feature, [])
            conn.commit()

            pings = location_db.get_pings(conn)
            assert len(pings) == 1
            p = pings[0]
            # GeoJSON: coordinates = [lon, lat]
            assert p.lon == -122.030581
            assert p.lat == 37.331800
            assert p.accuracy == 5
            assert p.activity_type == "stationary"
            assert p.battery == 0.92
            assert p.wifi == "home-wifi"

    def test_parse_negative_speed_becomes_none(self):
        from istota.webhook_receiver import _process_feature

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [0, 0]},
            "properties": {
                "timestamp": "2026-01-01T00:00:00Z",
                "speed": -1,
                "course": -1,
            },
        }

        db_path = _init_loc_db(Path(pytest.importorskip("tempfile").mkdtemp()))
        with location_db.connect(db_path) as conn:
            _process_feature(conn, feature, [])
            conn.commit()

            p = location_db.get_latest_ping(conn)
            assert p.speed is None
            assert p.course is None

    def test_feature_with_activity_string(self):
        """Overland can send activity as a string instead of motion array."""
        from istota.webhook_receiver import _process_feature

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [0, 0]},
            "properties": {
                "timestamp": "2026-01-01T00:00:00Z",
                "activity": "other_navigation",
            },
        }

        db_path = _init_loc_db(Path(pytest.importorskip("tempfile").mkdtemp()))
        with location_db.connect(db_path) as conn:
            _process_feature(conn, feature, [])
            conn.commit()

            p = location_db.get_latest_ping(conn)
            assert p.activity_type == "other_navigation"

    def test_empty_coordinates_skipped(self):
        from istota.webhook_receiver import _process_feature

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": []},
            "properties": {"timestamp": "2026-01-01T00:00:00Z"},
        }

        db_path = _init_loc_db(Path(pytest.importorskip("tempfile").mkdtemp()))
        with location_db.connect(db_path) as conn:
            _process_feature(conn, feature, [])
            conn.commit()

            assert location_db.get_latest_ping(conn) is None


# ===========================================================================
# CLI tests
# ===========================================================================


class TestLocationCLI:
    def test_current_no_data(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_current
            import io
            from unittest.mock import MagicMock

            args = MagicMock()
            import sys
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_current(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["last_ping"] is None
            assert output["current_visit"] is None

    def test_places_lists_db(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.add_place(conn, "home", 34.0, -118.0, radius_meters=150, category="home")
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_places
            import io
            import sys
            from unittest.mock import MagicMock

            args = MagicMock()
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_places(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert len(output) == 1
            assert output[0]["name"] == "home"
            assert output[0]["radius_meters"] == 150

    def test_places_includes_id(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 34.0, -118.0, radius_meters=150, category="home")
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_places

            args = MagicMock()
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_places(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output[0]["id"] == pid

    def test_update_by_name(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.add_place(conn, "cafe", 34.0, -118.0, radius_meters=100, category="restaurant")
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = "cafe"
            args.id = None
            args.category = "food"
            args.rename = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["status"] == "ok"
            assert output["place"]["category"] == "food"
            assert output["place"]["name"] == "cafe"

        # Verify DB
        with location_db.connect(db_path) as conn:
            place = location_db.get_place_by_name(conn, "cafe")
            assert place.category == "food"

    def test_update_by_id(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "cafe", 34.0, -118.0, radius_meters=100, category="restaurant")
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = None
            args.id = pid
            args.category = "food"
            args.rename = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["status"] == "ok"
            assert output["place"]["category"] == "food"

    def test_update_rename(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.add_place(conn, "old name", 34.0, -118.0, radius_meters=100, category="other")
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = "old name"
            args.id = None
            args.rename = "new name"
            args.category = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["place"]["name"] == "new name"

        with location_db.connect(db_path) as conn:
            assert location_db.get_place_by_name(conn, "new name") is not None
            assert location_db.get_place_by_name(conn, "old name") is None

    def test_update_not_found(self, tmp_path):
        db_path = _init_loc_db(tmp_path)

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = "nonexistent"
            args.id = None
            args.category = "food"
            args.rename = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                with pytest.raises(SystemExit):
                    cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert "error" in output

    def test_update_no_changes(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.add_place(conn, "cafe", 34.0, -118.0, radius_meters=100, category="food")
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = "cafe"
            args.id = None
            args.category = None
            args.rename = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                with pytest.raises(SystemExit):
                    cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert "error" in output

    def test_delete_by_name(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.add_place(conn, "cafe", 34.0, -118.0, radius_meters=100, category="food")
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_delete

            args = MagicMock()
            args.name = "cafe"
            args.id = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_delete(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["status"] == "ok"
            assert output["deleted"] == "cafe"

        with location_db.connect(db_path) as conn:
            assert location_db.get_place_by_name(conn, "cafe") is None

    def test_delete_by_id(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "cafe", 34.0, -118.0, radius_meters=100, category="food")
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_delete

            args = MagicMock()
            args.name = None
            args.id = pid
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_delete(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["status"] == "ok"

        with location_db.connect(db_path) as conn:
            assert location_db.get_places(conn) == []

    def test_delete_not_found(self, tmp_path):
        db_path = _init_loc_db(tmp_path)

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_delete

            args = MagicMock()
            args.name = "nonexistent"
            args.id = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                with pytest.raises(SystemExit):
                    cmd_delete(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert "error" in output

    def test_history_lists_pings(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.insert_ping(conn, "2026-02-20T10:00:00Z", 34.0, -118.0,
                accuracy=5.0, activity_type="walking",
            )
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history
            import io
            import sys
            from unittest.mock import MagicMock

            args = MagicMock()
            args.limit = 10
            args.date = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_history(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert len(output) == 1
            assert output[0]["lat"] == 34.0

    def test_history_date_uses_timezone_aware_boundaries(self, tmp_path):
        """history --date should convert local day boundaries to UTC."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            # 2026-03-16 in Pacific = 2026-03-16T07:00:00Z to 2026-03-17T07:00:00Z (PDT)
            # Ping at 2026-03-16T02:00:00Z = Mar 15 7pm Pacific — outside Mar 16 local
            location_db.insert_ping(conn, "2026-03-16T02:00:00Z", 34.0, -118.0,
                accuracy=5.0, activity_type="stationary",
            )
            # Ping at 2026-03-16T20:00:00Z = Mar 16 1pm Pacific — inside Mar 16 local
            location_db.insert_ping(conn, "2026-03-16T20:00:00Z", 34.1, -118.1,
                accuracy=5.0, activity_type="walking",
            )
            # Ping at 2026-03-17T03:00:00Z = Mar 16 8pm Pacific — inside Mar 16 local
            location_db.insert_ping(conn, "2026-03-17T03:00:00Z", 34.2, -118.2,
                accuracy=5.0, activity_type="walking",
            )
            # Ping at 2026-03-17T10:00:00Z = Mar 17 3am Pacific — outside Mar 16 local
            location_db.insert_ping(conn, "2026-03-17T10:00:00Z", 34.3, -118.3,
                accuracy=5.0, activity_type="stationary",
            )
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history

            args = MagicMock()
            args.limit = 0
            args.date = "2026-03-16"
            args.tz = "America/Los_Angeles"
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_history(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            # Should only include the two pings within Mar 16 Pacific
            assert len(output) == 2
            lats = {p["lat"] for p in output}
            assert lats == {34.1, 34.2}

    def test_history_date_returns_all_pings_by_default(self, tmp_path):
        """history --date with no --limit should return all pings, not just 20."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            # Insert 30 pings spread across Mar 16 Pacific
            for i in range(30):
                ts = f"2026-03-16T{15 + (i // 6):02d}:{(i % 6) * 10:02d}:00Z"
                location_db.insert_ping(conn, ts, 34.0 + i * 0.001, -118.0,
                    accuracy=5.0, activity_type="stationary",
                )
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history

            args = MagicMock()
            args.limit = 0
            args.date = "2026-03-16"
            args.tz = "America/Los_Angeles"
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_history(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert len(output) == 30

    def test_history_date_respects_explicit_limit(self, tmp_path):
        """history --date --limit N should cap results."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for i in range(10):
                ts = f"2026-03-16T{15 + i}:00:00Z"
                location_db.insert_ping(conn, ts, 34.0, -118.0,
                    accuracy=5.0, activity_type="stationary",
                )
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history

            args = MagicMock()
            args.limit = 5
            args.date = "2026-03-16"
            args.tz = "America/Los_Angeles"
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_history(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert len(output) == 5


def _run_cmd(fn, **args):
    """Call a location CLI command and return its parsed JSON stdout.

    Args are a ``SimpleNamespace``, not a ``MagicMock``: a Mock answers every
    attribute with a truthy Mock, so a command reading an option the test never
    set silently takes a fallback branch instead of failing.
    """
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        fn(SimpleNamespace(**args))
    finally:
        sys.stdout = old_stdout
    return json.loads(captured.getvalue())


class TestAltitudeSurfacing:
    """ISSUE-218 — altitude is stored on every ping but was dropped by every reader."""

    def _seed(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            # A climb: two pings inside 2026-03-16 Pacific, one with no vertical fix.
            location_db.insert_ping(conn, "2026-03-16T20:00:00Z", 34.0, -118.0,
                altitude=335.3, accuracy=5.0, activity_type="driving",
            )
            location_db.insert_ping(conn, "2026-03-16T21:00:00Z", 34.1, -118.1,
                altitude=None, accuracy=5.0, activity_type="driving",
            )
            conn.commit()
        return db_path

    def test_history_includes_altitude(self, tmp_path):
        db_path = self._seed(tmp_path)
        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history

            output = _run_cmd(cmd_history, limit=10, date=None)

        by_ts = {p["timestamp"]: p for p in output}
        assert by_ts["2026-03-16T20:00:00Z"]["altitude"] == 335.3

    def test_history_altitude_is_null_when_no_vertical_fix(self, tmp_path):
        """~5% of real pings carry a horizontal fix only; the key must still be present."""
        db_path = self._seed(tmp_path)
        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history

            output = _run_cmd(cmd_history, limit=10, date=None)

        by_ts = {p["timestamp"]: p for p in output}
        assert by_ts["2026-03-16T21:00:00Z"]["altitude"] is None

    def test_history_date_branch_includes_altitude(self, tmp_path):
        """--date runs a second, separately-written SELECT — it must carry the column too."""
        db_path = self._seed(tmp_path)
        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history

            output = _run_cmd(
                cmd_history, limit=0, date="2026-03-16", tz="America/Los_Angeles"
            )

        assert len(output) == 2
        assert {p["altitude"] for p in output} == {335.3, None}

    def test_current_includes_altitude(self, tmp_path):
        db_path = self._seed(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.insert_ping(conn, "2026-03-16T22:00:00Z", 34.2, -118.2,
                altitude=1432.6, accuracy=5.0, activity_type="driving",
            )
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_current

            output = _run_cmd(cmd_current)

        assert output["last_ping"]["altitude"] == 1432.6


@_needs_fastapi
class TestLocationPingsAPIAltitude:
    """The web pings endpoint feeds the map; it dropped altitude the same way."""

    def _seed(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.insert_ping(conn, "2026-03-16T20:00:00Z", 34.0, -118.0,
                altitude=335.3, accuracy=5.0, activity_type="driving",
            )
            location_db.insert_ping(conn, "2026-03-16T21:00:00Z", 34.1, -118.1,
                altitude=None, accuracy=5.0, activity_type="driving",
            )
            conn.commit()
        return db_path

    def test_date_range_query_includes_altitude(self, tmp_path):
        from istota.web_app import _location_query_pings

        db_path = self._seed(tmp_path)
        result = _location_query_pings(
            str(db_path), "America/Los_Angeles",
            date="2026-03-16", start=None, end=None, limit=0,
        )

        assert result["count"] == 2
        assert [p["altitude"] for p in result["pings"]] == [335.3, None]

    def test_default_query_includes_altitude(self, tmp_path):
        """The no-date branch is a separate SELECT and needs the column too."""
        from istota.web_app import _location_query_pings

        db_path = self._seed(tmp_path)
        result = _location_query_pings(
            str(db_path), "America/Los_Angeles",
            date=None, start=None, end=None, limit=10,
        )

        assert {p["altitude"] for p in result["pings"]} == {335.3, None}

    def test_current_query_includes_altitude(self, tmp_path):
        """`LocationPing.altitude` is a required field of the shared frontend type,
        so the current-location reader has to send it too — not only the CLI twin."""
        from istota.web_app import _location_query_current

        db_path = self._seed(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.insert_ping(conn, "2026-03-16T22:00:00Z", 34.2, -118.2,
                altitude=1432.6, accuracy=5.0, activity_type="driving",
            )
            conn.commit()

        result = _location_query_current(str(db_path))

        assert result["last_ping"]["altitude"] == 1432.6


class TestLocationDiscoverDismissCLI:
    """CLI wrappers for discover, dismiss-cluster, list-dismissed, restore-dismissed, place-stats."""

    def _seed_cluster(self, conn, lat, lon, count=15):
        for i in range(count):
            ts = f"2026-01-10T09:{i:02d}:00Z"
            location_db.insert_ping(conn, ts, lat + (i % 3) * 0.00002, lon,
                accuracy=5.0, activity_type="stationary",
            )

    def _run(self, cmd, args):
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            cmd(args)
        finally:
            sys.stdout = old_stdout
        return json.loads(captured.getvalue())

    def _run_failing(self, cmd, args):
        """A not-found verb: the envelope, and the exit 1 that goes with it.

        S10 made these exit 1 rather than 0 — a call naming a place or a
        cluster that does not exist is a failed call, and a silent exit 0
        behind an error envelope is what the skill CLI facade exists to stop.
        """
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            with pytest.raises(SystemExit) as exc:
                cmd(args)
        finally:
            sys.stdout = old_stdout
        assert exc.value.code == 1
        return json.loads(captured.getvalue())

    def test_discover_finds_unassigned_cluster(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            self._seed_cluster(conn, 34.0, -118.0)
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_discover

            args = MagicMock()
            args.min_pings = 10
            output = self._run(cmd_discover, args)

        assert len(output["clusters"]) == 1
        assert "lat" in output["clusters"][0]
        assert "radius_meters" in output["clusters"][0]

    def test_discover_respects_min_pings(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            self._seed_cluster(conn, 34.0, -118.0, count=8)
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_discover

            args = MagicMock()
            args.min_pings = 20
            output = self._run(cmd_discover, args)

        assert output["clusters"] == []

    def test_dismiss_cluster_inserts_row(self, tmp_path):
        db_path = _init_loc_db(tmp_path)

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_dismiss_cluster

            args = MagicMock()
            args.lat = 34.0
            args.lon = -118.0
            args.radius = 200
            output = self._run(cmd_dismiss_cluster, args)

        assert output["status"] == "ok"
        assert output["lat"] == 34.0
        assert output["lon"] == -118.0
        assert output["radius_meters"] == 200
        assert isinstance(output["id"], int)

        with location_db.connect(db_path) as conn:
            rows = location_db.list_dismissed_clusters(conn)
            assert len(rows) == 1

    def test_list_dismissed_returns_inserted_rows(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.dismiss_cluster(conn, 34.0, -118.0, 100)
            location_db.dismiss_cluster(conn, 40.0, -73.0, 150)
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_list_dismissed

            args = MagicMock()
            output = self._run(cmd_list_dismissed, args)

        assert len(output["dismissed"]) == 2
        radii = sorted(r["radius_meters"] for r in output["dismissed"])
        assert radii == [100, 150]

    def test_restore_dismissed_deletes_row(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            cid = location_db.dismiss_cluster(conn, 34.0, -118.0, 100)
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_restore_dismissed

            args = MagicMock()
            args.cluster_id = cid
            output = self._run(cmd_restore_dismissed, args)

        assert output["status"] == "ok"
        with location_db.connect(db_path) as conn:
            assert location_db.list_dismissed_clusters(conn) == []

    def test_restore_dismissed_unknown_id(self, tmp_path):
        db_path = _init_loc_db(tmp_path)

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_restore_dismissed

            args = MagicMock()
            args.cluster_id = 9999
            output = self._run_failing(cmd_restore_dismissed, args)

        assert output["status"] == "error"

    def test_place_stats_by_id(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "cafe", 34.0, -118.0, radius_meters=100, category="food")
            for i, ts in enumerate([
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:30:00Z",
                "2026-01-10T10:00:00Z",
            ]):
                location_db.insert_ping(conn, ts, 34.0, -118.0,
                    accuracy=5.0, activity_type="stationary",
                )
                last_id = conn.execute("SELECT max(id) FROM location_pings").fetchone()[0]
                conn.execute("UPDATE location_pings SET place_id = ? WHERE id = ?", (pid, last_id))
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_place_stats

            args = MagicMock()
            args.name = None
            args.id = pid
            output = self._run(cmd_place_stats, args)

        assert output["place_id"] == pid
        assert output["total_visits"] == 1
        assert output["total_duration_min"] == 60

    def test_place_stats_by_name(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 34.0, -118.0, radius_meters=150, category="home")
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_place_stats

            args = MagicMock()
            args.name = "home"
            args.id = None
            output = self._run(cmd_place_stats, args)

        assert output["place_id"] == pid
        assert output["total_visits"] == 0

    def test_place_stats_unknown_name(self, tmp_path):
        db_path = _init_loc_db(tmp_path)

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_place_stats

            args = MagicMock()
            args.name = "ghost"
            args.id = None
            output = self._run_failing(cmd_place_stats, args)

        assert output["status"] == "error"

    def test_place_stats_other_user_cannot_read(self, tmp_path):
        # Per-user isolation is now per-file: alice's place lives in alice's
        # location.db; bob's process points at bob's empty location.db and
        # the place_id miss returns an error.
        alice_db = _init_loc_db(tmp_path / "alice", "location.db")
        with location_db.connect(alice_db) as conn:
            pid = location_db.add_place(conn, "home", 34.0, -118.0, radius_meters=150, category="home")
            conn.commit()

        bob_db = _init_loc_db(tmp_path / "bob", "location.db")

        env = {"LOCATION_DB_PATH": str(bob_db), "ISTOTA_DB_PATH": str(bob_db)}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_place_stats

            args = MagicMock()
            args.name = None
            args.id = pid
            output = self._run_failing(cmd_place_stats, args)

        assert output["status"] == "error"


# ===========================================================================
# Geocode cache DB tests
# ===========================================================================


class TestGeocodeCache:
    def test_cache_miss_returns_none(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            assert db.get_cached_geocode(conn, "123 Main St") is None

    def test_cache_and_retrieve(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_geocode(conn, "123 Main St", 34.05, -118.4)
            conn.commit()

            result = db.get_cached_geocode(conn, "123 Main St")
            assert result == (34.05, -118.4)

    def test_cache_upsert(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_geocode(conn, "123 Main St", 34.05, -118.4)
            db.cache_geocode(conn, "123 Main St", 35.0, -119.0)
            conn.commit()

            result = db.get_cached_geocode(conn, "123 Main St")
            assert result == (35.0, -119.0)


# ===========================================================================
# Attendance helper tests
# ===========================================================================


class TestVirtualLocationDetection:
    def test_zoom_link(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("https://zoom.us/j/12345") is True

    def test_google_meet(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("meet.google.com/abc-def") is True

    def test_teams(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("Microsoft Teams Meeting") is True

    def test_physical_location(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("123 Main St, San Francisco") is False

    def test_conference_room(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("Conference Room B") is False


class TestPlaceMatching:
    def test_exact_match(self):
        from istota.skills.location import _match_place
        places = [{"name": "gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("gym", places)
        assert result is not None
        assert result["name"] == "gym"

    def test_case_insensitive(self):
        from istota.skills.location import _match_place
        places = [{"name": "Downtown Gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("downtown gym", places)
        assert result is not None

    def test_substring_match_location_in_place(self):
        from istota.skills.location import _match_place
        places = [{"name": "Downtown Gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("gym", places)
        assert result is not None
        assert result["name"] == "Downtown Gym"

    def test_substring_match_place_in_location(self):
        from istota.skills.location import _match_place
        places = [{"name": "gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("The gym on 5th Ave", places)
        assert result is not None

    def test_no_match(self):
        from istota.skills.location import _match_place
        places = [{"name": "gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("dentist office", places)
        assert result is None

    def test_empty_places(self):
        from istota.skills.location import _match_place
        assert _match_place("gym", []) is None


class TestGeocodeLocation:
    def test_cache_hit(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_geocode(conn, "123 Main St", 34.05, -118.4)
            conn.commit()

            result = _geocode_location("123 Main St", conn)
            assert result == (34.05, -118.4)

    @_needs_geopy
    def test_nominatim_called_on_miss(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            mock_result = MagicMock()
            mock_result.latitude = 37.7749
            mock_result.longitude = -122.4194

            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.geocode.return_value = mock_result
                mock_nom_cls.return_value = mock_geolocator

                result = _geocode_location("San Francisco, CA", conn)
                assert result == (37.7749, -122.4194)

                # Should be cached now
                cached = db.get_cached_geocode(conn, "San Francisco, CA")
                assert cached == (37.7749, -122.4194)

    @_needs_geopy
    def test_nominatim_failure_returns_none(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.geocode.return_value = None
                mock_nom_cls.return_value = mock_geolocator

                result = _geocode_location("nonexistent place xyz", conn)
                assert result is None

    @_needs_geopy
    def test_nominatim_exception_returns_none(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.geocode.side_effect = Exception("timeout")
                mock_nom_cls.return_value = mock_geolocator

                result = _geocode_location("123 Main St", conn)
                assert result is None


# ===========================================================================
# Attendance command tests
# ===========================================================================


def _make_calendar_event(
    uid="ev1",
    summary="Meeting",
    start=None,
    end=None,
    location=None,
    all_day=False,
):
    """Create a mock CalendarEvent."""
    from istota.skills.calendar import CalendarEvent
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    if start is None:
        start = datetime(2026, 3, 1, 10, 0, tzinfo=tz)
    if end is None:
        end = datetime(2026, 3, 1, 11, 0, tzinfo=tz)
    return CalendarEvent(
        uid=uid,
        summary=summary,
        start=start,
        end=end,
        location=location,
        all_day=all_day,
    )


class TestCmdAttendance:
    def _run_attendance(self, tmp_path, events, pings=None, places=None, args_overrides=None):
        """Helper to run cmd_attendance with mocked CalDAV and DB.

        Uses two DBs to mirror production: per-user location.db for
        pings/places, framework istota.db for the global geocode cache.
        """
        from istota.skills.location import cmd_attendance

        loc_db = _init_loc_db(tmp_path, "location.db")
        framework_db = _init_db(tmp_path)  # for geocode_cache
        with location_db.connect(loc_db) as conn:
            for p in (places or []):
                location_db.add_place(
                    conn, p["name"], p["lat"], p["lon"],
                    radius_meters=p.get("radius_meters", 100),
                    category=p.get("category", "other"),
                )
            for ping in (pings or []):
                location_db.insert_ping(
                    conn, ping["timestamp"], ping["lat"], ping["lon"],
                    accuracy=ping.get("accuracy", 5.0),
                )
            conn.commit()

        env = {
            "LOCATION_DB_PATH": str(loc_db),
            "ISTOTA_DB_PATH": str(framework_db),
            "ISTOTA_USER_ID": "alice",
            "CALDAV_URL": "https://cloud.example.com/remote.php/dav",
            "CALDAV_USERNAME": "alice",
            "CALDAV_PASSWORD": "secret",
            "TZ": "America/Los_Angeles",
        }

        args = MagicMock()
        args.date = "2026-03-01"
        args.event = None
        if args_overrides:
            for k, v in args_overrides.items():
                setattr(args, k, v)

        mock_client = MagicMock()
        mock_calendars = [("Personal", "https://cal.example.com/personal")]

        with patch.dict("os.environ", env):
            with patch("istota.skills.calendar.get_caldav_client", return_value=mock_client):
                with patch("istota.skills.calendar.list_calendars", return_value=mock_calendars):
                    with patch("istota.skills.calendar.get_events", return_value=events):
                        captured = io.StringIO()
                        old_stdout = sys.stdout
                        sys.stdout = captured
                        try:
                            cmd_attendance(args)
                        finally:
                            sys.stdout = old_stdout

        return json.loads(captured.getvalue())

    def test_no_events(self, tmp_path):
        result = self._run_attendance(tmp_path, events=[])
        assert result["date"] == "2026-03-01"
        assert result["events"] == []

    def test_all_day_event_filtered(self, tmp_path):
        events = [_make_calendar_event(location="123 Main St", all_day=True)]
        result = self._run_attendance(tmp_path, events=events)
        assert result["events"] == []

    def test_no_location_filtered(self, tmp_path):
        events = [_make_calendar_event(location=None)]
        result = self._run_attendance(tmp_path, events=events)
        assert result["events"] == []

    def test_virtual_location_filtered(self, tmp_path):
        events = [_make_calendar_event(location="https://zoom.us/j/12345")]
        result = self._run_attendance(tmp_path, events=events)
        assert result["events"] == []

    def test_attendance_confirmed_with_nearby_pings(self, tmp_path):
        events = [_make_calendar_event(
            uid="dentist1",
            summary="Dentist",
            location="dentist office",
        )]
        places = [{"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 200}]
        pings = [
            {"timestamp": "2026-03-01T17:45:00Z", "lat": 34.0501, "lon": -118.4001},  # 10:45 PT, within window
            {"timestamp": "2026-03-01T18:30:00Z", "lat": 34.0502, "lon": -118.3999},  # 11:30 PT, within window
        ]
        result = self._run_attendance(tmp_path, events=events, pings=pings, places=places)
        assert len(result["events"]) == 1
        ev = result["events"][0]
        assert ev["attended"] is True
        assert ev["resolution_source"] == "place"
        assert ev["nearby_ping_count"] == 2

    def test_no_pings_no_attendance(self, tmp_path):
        events = [_make_calendar_event(
            summary="Dentist",
            location="dentist office",
        )]
        places = [{"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 200}]
        result = self._run_attendance(tmp_path, events=events, pings=[], places=places)
        assert len(result["events"]) == 1
        ev = result["events"][0]
        assert ev["attended"] is None

    def test_pings_too_far_away(self, tmp_path):
        events = [_make_calendar_event(
            summary="Dentist",
            location="dentist office",
        )]
        places = [{"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 100}]
        # Pings far from the dentist
        pings = [
            {"timestamp": "2026-03-01T18:00:00Z", "lat": 35.0, "lon": -119.0},
        ]
        result = self._run_attendance(tmp_path, events=events, pings=pings, places=places)
        ev = result["events"][0]
        assert ev["attended"] is None

    @_needs_geopy
    def test_ungeocoded_event(self, tmp_path):
        events = [_make_calendar_event(
            summary="Meeting",
            location="Some Unknown Place XYZ123",
        )]
        # No places, geocoding will fail
        with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
            mock_geolocator = MagicMock()
            mock_geolocator.geocode.return_value = None
            mock_nom_cls.return_value = mock_geolocator

            result = self._run_attendance(tmp_path, events=events)

        assert len(result["events"]) == 1
        ev = result["events"][0]
        assert ev["location_resolved"] is False
        assert ev["attended"] is None

    @_needs_geopy
    def test_geocoded_event_with_attendance(self, tmp_path):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
        events = [_make_calendar_event(
            summary="Dentist",
            location="123 Main St, LA",
            start=datetime(2026, 3, 1, 10, 0, tzinfo=tz),
            end=datetime(2026, 3, 1, 11, 0, tzinfo=tz),
        )]
        # Ping near geocoded location
        pings = [
            {"timestamp": "2026-03-01T18:00:00Z", "lat": 34.0501, "lon": -118.4001},
        ]

        mock_result = MagicMock()
        mock_result.latitude = 34.05
        mock_result.longitude = -118.4

        with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
            mock_geolocator = MagicMock()
            mock_geolocator.geocode.return_value = mock_result
            mock_nom_cls.return_value = mock_geolocator

            result = self._run_attendance(tmp_path, events=events, pings=pings)

        ev = result["events"][0]
        assert ev["attended"] is True
        assert ev["resolution_source"] == "geocode"

    def test_event_filter_by_title(self, tmp_path):
        events = [
            _make_calendar_event(uid="ev1", summary="Dentist", location="dentist office"),
            _make_calendar_event(uid="ev2", summary="Gym", location="gym"),
        ]
        places = [
            {"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 200},
            {"name": "gym", "lat": 34.1, "lon": -118.1, "radius_meters": 100},
        ]
        result = self._run_attendance(
            tmp_path, events=events, places=places,
            args_overrides={"event": "dentist"},
        )
        assert len(result["events"]) == 1
        assert result["events"][0]["summary"] == "Dentist"

    def test_event_filter_by_uid(self, tmp_path):
        events = [
            _make_calendar_event(uid="abc123", summary="Dentist", location="dentist office"),
            _make_calendar_event(uid="def456", summary="Gym", location="gym"),
        ]
        places = [
            {"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 200},
            {"name": "gym", "lat": 34.1, "lon": -118.1, "radius_meters": 100},
        ]
        result = self._run_attendance(
            tmp_path, events=events, places=places,
            args_overrides={"event": "abc123"},
        )
        assert len(result["events"]) == 1
        assert result["events"][0]["uid"] == "abc123"

    def test_place_radius_used(self, tmp_path):
        """Place with large radius should detect pings that would be outside default 200m."""
        events = [_make_calendar_event(
            summary="Park",
            location="big park",
        )]
        # Place with 2km radius
        places = [{"name": "big park", "lat": 34.05, "lon": -118.4, "radius_meters": 2000}]
        # Ping ~500m away (would fail with 200m default, but passes with 2km)
        pings = [
            {"timestamp": "2026-03-01T18:00:00Z", "lat": 34.055, "lon": -118.4},
        ]
        result = self._run_attendance(tmp_path, events=events, pings=pings, places=places)
        ev = result["events"][0]
        assert ev["attended"] is True
        assert ev["radius_meters"] == 2000


# ===========================================================================
# Reverse geocode cache DB tests
# ===========================================================================


class TestReverseGeocodeCache:
    def test_cache_miss_returns_none(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            result = db.get_reverse_geocode(conn, 34.05, -118.25)
            assert result is None

    def test_store_and_retrieve(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            data = {
                "display_name": "123 Main St, Los Angeles, CA",
                "neighborhood": "Downtown",
                "suburb": "Central LA",
                "road": "Main St",
                "city": "Los Angeles",
            }
            db.cache_reverse_geocode(conn, 34.05, -118.25, data)
            conn.commit()

            result = db.get_reverse_geocode(conn, 34.05, -118.25)
            assert result is not None
            assert result["display_name"] == "123 Main St, Los Angeles, CA"
            assert result["neighborhood"] == "Downtown"
            assert result["suburb"] == "Central LA"
            assert result["road"] == "Main St"
            assert result["city"] == "Los Angeles"

    def test_rounding_hits_same_entry(self, tmp_path):
        """Nearby coords (within ~11m) should hit the same cache entry."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            data = {
                "display_name": "Test Place",
                "neighborhood": None,
                "suburb": None,
                "road": "Test Rd",
                "city": "Test City",
            }
            db.cache_reverse_geocode(conn, 34.05001, -118.25002, data)
            conn.commit()

            # Slightly different coords that round to the same 4-decimal value
            result = db.get_reverse_geocode(conn, 34.05004, -118.25001)
            assert result is not None
            assert result["display_name"] == "Test Place"

    def test_upsert_overwrites(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            data1 = {
                "display_name": "Old Name",
                "neighborhood": None,
                "suburb": None,
                "road": None,
                "city": None,
            }
            db.cache_reverse_geocode(conn, 34.05, -118.25, data1)
            conn.commit()

            data2 = {
                "display_name": "New Name",
                "neighborhood": "New Hood",
                "suburb": None,
                "road": None,
                "city": None,
            }
            db.cache_reverse_geocode(conn, 34.05, -118.25, data2)
            conn.commit()

            result = db.get_reverse_geocode(conn, 34.05, -118.25)
            assert result["display_name"] == "New Name"
            assert result["neighborhood"] == "New Hood"


# ===========================================================================
# Reverse geocode function tests (geo.py)
# ===========================================================================


class TestReverseGeocode:
    def test_cache_hit(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_reverse_geocode(conn, 34.05, -118.25, {
                "display_name": "Cached Place",
                "neighborhood": "Hood",
                "suburb": "Sub",
                "road": "Road",
                "city": "City",
            })
            conn.commit()

            result = reverse_geocode(34.05, -118.25, conn)
            assert result["source"] == "cache"
            assert result["display_name"] == "Cached Place"

    @_needs_geopy
    def test_nominatim_called_on_miss(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            mock_result = MagicMock()
            mock_result.address = "456 Oak Ave, Pasadena, CA"
            mock_result.raw = {
                "address": {
                    "road": "Oak Ave",
                    "neighbourhood": "Old Town",
                    "suburb": "South Pasadena",
                    "city": "Pasadena",
                }
            }

            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.reverse.return_value = mock_result
                mock_nom_cls.return_value = mock_geolocator

                result = reverse_geocode(34.15, -118.14, conn)
                assert result["source"] == "nominatim"
                assert result["display_name"] == "456 Oak Ave, Pasadena, CA"
                assert result["road"] == "Oak Ave"
                assert result["neighborhood"] == "Old Town"

                # Should be cached now
                cached = db.get_reverse_geocode(conn, 34.15, -118.14)
                assert cached is not None
                assert cached["display_name"] == "456 Oak Ave, Pasadena, CA"

    @_needs_geopy
    def test_nominatim_returns_none(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.reverse.return_value = None
                mock_nom_cls.return_value = mock_geolocator

                result = reverse_geocode(0.0, 0.0, conn)
                assert result["source"] == "error"
                assert "error" in result

    @_needs_geopy
    def test_nominatim_exception(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.reverse.side_effect = Exception("timeout")
                mock_nom_cls.return_value = mock_geolocator

                result = reverse_geocode(34.05, -118.25, conn)
                assert result["source"] == "error"
                assert "timeout" in result["error"]


# ===========================================================================
# Cluster pings tests (geo.py)
# ===========================================================================


class TestFilterTransitClusters:
    """Direct unit tests for filter_transit_clusters() spatial absorption."""

    def _make_cluster(self, lat, lon, first_ts, last_ts, ping_count,
                      place_name=None, place_id=None):
        return {
            "lat": lat, "lon": lon,
            "first_ts": first_ts, "last_ts": last_ts,
            "ping_count": ping_count,
            "place_name": place_name, "place_id": place_id,
        }

    def test_absorbs_nearby_fragment_into_previous_stop(self):
        """Small cluster within merge radius of previous stop gets absorbed."""
        from istota.geo import filter_transit_clusters

        clusters = [
            # Big stop — survives filtering on its own
            self._make_cluster(34.0836, -118.3101,
                               "2026-04-08T19:07:00Z", "2026-04-08T19:21:00Z",
                               ping_count=20),
            # Small fragment — same location, indoor GPS gap
            self._make_cluster(34.0837, -118.3100,
                               "2026-04-08T19:27:00Z", "2026-04-08T19:28:00Z",
                               ping_count=2),
        ]
        stops, transit = filter_transit_clusters(clusters)
        assert len(stops) == 1
        # Fragment absorbed: ping count summed, last_ts extended
        assert stops[0]["ping_count"] == 22
        assert stops[0]["last_ts"] == "2026-04-08T19:28:00Z"
        assert transit == 0

    def test_discards_distant_fragment(self):
        """Small cluster far from previous stop is still discarded as transit."""
        from istota.geo import filter_transit_clusters

        clusters = [
            # Stop at location A
            self._make_cluster(34.0836, -118.3101,
                               "2026-04-08T19:07:00Z", "2026-04-08T19:21:00Z",
                               ping_count=20),
            # Small fragment at a different location (~1km away)
            self._make_cluster(34.0920, -118.3101,
                               "2026-04-08T19:27:00Z", "2026-04-08T19:28:00Z",
                               ping_count=2),
        ]
        stops, transit = filter_transit_clusters(clusters)
        assert len(stops) == 1
        assert stops[0]["ping_count"] == 20  # not absorbed
        assert transit == 2

    def test_no_previous_stop_to_absorb_into(self):
        """First cluster is small with no preceding stop — discarded normally."""
        from istota.geo import filter_transit_clusters

        clusters = [
            # Small cluster, nothing to absorb into
            self._make_cluster(34.0836, -118.3101,
                               "2026-04-08T19:07:00Z", "2026-04-08T19:08:00Z",
                               ping_count=2),
        ]
        stops, transit = filter_transit_clusters(clusters)
        assert len(stops) == 0
        assert transit == 2


class TestMergeConsecutiveStops:
    """Direct unit tests for merge_consecutive_stops() spatial proximity merge."""

    def _make_stop(self, location, lat, lon, first_ts, last_ts, ping_count,
                   location_source="nominatim", transit_before=0):
        return {
            "location": location,
            "location_source": location_source,
            "lat": lat, "lon": lon,
            "first_ts": first_ts, "last_ts": last_ts,
            "first_ts_local": first_ts[-9:-4] if len(first_ts) > 9 else first_ts,
            "last_ts_local": last_ts[-9:-4] if len(last_ts) > 9 else last_ts,
            "ping_count": ping_count,
            "_transit_pings_before": transit_before,
        }

    def test_merges_nearby_unnamed_stops_with_different_names(self):
        """Two consecutive stops ~20m apart with different reverse-geocoded names
        should merge (ISSUE-047 bug A)."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("East Live Oak Drive", 34.1086, -118.3099,
                            "2026-04-10T01:58:00Z", "2026-04-10T03:56:00Z", 33),
            self._make_stop("Tryon Road", 34.1087, -118.3100,
                            "2026-04-10T04:09:00Z", "2026-04-10T04:41:00Z", 11),
            self._make_stop("East Live Oak Drive", 34.1086, -118.3099,
                            "2026-04-10T05:05:00Z", "2026-04-10T05:16:00Z", 18),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 1
        assert merged[0]["ping_count"] == 62
        # Should keep the name from the longest stop
        assert merged[0]["location"] == "East Live Oak Drive"

    def test_does_not_merge_distant_unnamed_stops(self):
        """Two consecutive unnamed stops far apart should not merge."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("Elm Street", 34.05, -118.25,
                            "2026-04-10T10:00:00Z", "2026-04-10T11:00:00Z", 20),
            self._make_stop("Oak Avenue", 34.06, -118.25,
                            "2026-04-10T11:30:00Z", "2026-04-10T12:00:00Z", 15),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 2

    def test_does_not_proximity_merge_saved_places(self):
        """Two different saved places nearby should not be merged by proximity."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("Home", 34.1025, -118.3059,
                            "2026-04-10T10:00:00Z", "2026-04-10T11:00:00Z", 20,
                            location_source="saved_place"),
            self._make_stop("Neighbor", 34.1026, -118.3060,
                            "2026-04-10T11:30:00Z", "2026-04-10T12:00:00Z", 15,
                            location_source="saved_place"),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 2

    def test_proximity_merge_keeps_longer_stop_name(self):
        """When merging by proximity, the name from the longer stop is kept."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("Short Road", 34.1086, -118.3099,
                            "2026-04-10T10:00:00Z", "2026-04-10T10:10:00Z", 5),
            self._make_stop("Main Boulevard", 34.1087, -118.3100,
                            "2026-04-10T10:15:00Z", "2026-04-10T12:00:00Z", 30),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 1
        assert merged[0]["location"] == "Main Boulevard"

    def test_proximity_merge_respects_transit_threshold(self):
        """Even if nearby, stops separated by significant transit should not merge."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("Road A", 34.1086, -118.3099,
                            "2026-04-10T10:00:00Z", "2026-04-10T11:00:00Z", 20),
            self._make_stop("Road B", 34.1087, -118.3100,
                            "2026-04-10T12:00:00Z", "2026-04-10T13:00:00Z", 15,
                            transit_before=5),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 2


class TestClusterPings:
    def test_empty_input(self):
        from istota.geo import cluster_pings

        assert cluster_pings([]) == []

    def test_single_ping(self):
        from istota.geo import cluster_pings

        pings = [{"lat": 34.05, "lon": -118.25, "timestamp": "2026-03-08T10:00:00Z"}]
        result = cluster_pings(pings)
        assert len(result) == 1
        assert result[0]["ping_count"] == 1
        assert result[0]["lat"] == 34.05
        assert result[0]["first_ts"] == "2026-03-08T10:00:00Z"
        assert result[0]["last_ts"] == "2026-03-08T10:00:00Z"

    def test_two_close_pings_one_cluster(self):
        from istota.geo import cluster_pings

        # Two pings ~10m apart — well within 200m default radius
        pings = [
            {"lat": 34.05000, "lon": -118.25000, "timestamp": "2026-03-08T10:00:00Z"},
            {"lat": 34.05005, "lon": -118.25005, "timestamp": "2026-03-08T10:05:00Z"},
        ]
        result = cluster_pings(pings)
        assert len(result) == 1
        assert result[0]["ping_count"] == 2

    def test_two_distant_pings_two_clusters(self):
        from istota.geo import cluster_pings

        # Two pings ~5km apart
        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-03-08T10:00:00Z"},
            {"lat": 34.10, "lon": -118.25, "timestamp": "2026-03-08T11:00:00Z"},
        ]
        result = cluster_pings(pings)
        assert len(result) == 2
        assert result[0]["ping_count"] == 1
        assert result[1]["ping_count"] == 1

    def test_cluster_carries_place_info(self):
        from istota.geo import cluster_pings

        # 3 tagged pings meets MIN_PLACE_PINGS threshold for attribution
        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-03-08T10:00:00Z",
             "place_id": 42, "place_name": "home"},
            {"lat": 34.05001, "lon": -118.25001, "timestamp": "2026-03-08T10:05:00Z",
             "place_id": 42, "place_name": "home"},
            {"lat": 34.05002, "lon": -118.25002, "timestamp": "2026-03-08T10:10:00Z",
             "place_id": 42, "place_name": "home"},
        ]
        result = cluster_pings(pings)
        assert len(result) == 1
        assert result[0]["place_id"] == 42
        assert result[0]["place_name"] == "home"

    def test_centroid_drift_splits_route(self):
        """Many pings drifting slowly along a road should NOT merge into one cluster.

        Simulates riding ~500m along a street with 120 pings (~4m each).
        Each ping is close to the drifting centroid but far from the origin.
        The origin anchor should force a split.
        """
        from istota.geo import cluster_pings

        # 120 pings drifting ~4m each north (~480m total), like riding through
        # an intersection over ~10 minutes. Each ping only ~4m from centroid
        # so old code absorbs them all into one cluster.
        pings = [
            {"lat": 34.0500 + i * 0.000036, "lon": -118.25,
             "timestamp": f"2026-04-03T17:{i // 12:02d}:{(i % 12) * 5:02d}Z"}
            for i in range(120)
        ]
        result = cluster_pings(pings, radius_m=250)
        # Origin anchor should split this into multiple clusters
        assert len(result) >= 2
        # No single cluster should span the full route
        assert all(c["ping_count"] < 120 for c in result)

    def test_origin_anchor_forces_split(self):
        """A ping within centroid radius but beyond 1.5x origin radius must split.

        Four pings: A at origin, B1+B2 at ~178m (shifting centroid to ~119m),
        then C at ~311m from origin. C is within 200m of the centroid but
        beyond the 1.5*200=300m origin limit.
        """
        from istota.geo import cluster_pings, haversine

        a  = {"lat": 34.0500, "lon": -118.25, "timestamp": "2026-04-03T17:00:00Z"}
        b1 = {"lat": 34.0516, "lon": -118.25, "timestamp": "2026-04-03T17:01:00Z"}
        b2 = {"lat": 34.0516, "lon": -118.25, "timestamp": "2026-04-03T17:02:00Z"}
        c  = {"lat": 34.0528, "lon": -118.25, "timestamp": "2026-04-03T17:03:00Z"}

        # Verify geometry: C is within 200m of centroid(A,B1,B2) but >300m from A
        centroid_lat = (a["lat"] + b1["lat"] + b2["lat"]) / 3
        assert haversine(centroid_lat, -118.25, c["lat"], -118.25) < 200
        assert haversine(a["lat"], -118.25, c["lat"], -118.25) > 300

        result = cluster_pings([a, b1, b2, c], radius_m=200)
        assert len(result) == 2
        assert result[0]["ping_count"] == 3  # A + B1 + B2
        assert result[1]["ping_count"] == 1  # C split off by origin anchor

    def test_time_gap_splits_cluster(self):
        """Pings at the same location but >5 min apart should split."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-04-03T10:00:00Z"},
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-04-03T10:01:00Z"},
            # 10-minute gap
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-04-03T10:11:00Z"},
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-04-03T10:12:00Z"},
        ]
        result = cluster_pings(pings, max_gap_seconds=300)
        assert len(result) == 2
        assert result[0]["ping_count"] == 2
        assert result[1]["ping_count"] == 2

    def test_stationary_pings_cluster_normally(self):
        """Pings at the same spot with no time gaps stay in one cluster."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": f"2026-04-03T10:{i:02d}:00Z"}
            for i in range(20)
        ]
        result = cluster_pings(pings, radius_m=200)
        assert len(result) == 1
        assert result[0]["ping_count"] == 20


class TestDedupeNearDuplicatePings:
    """Tests for dedupe_near_duplicate_pings() — strips dual-source artifacts.

    The phone (Overland/iOS) sometimes reports two location fixes within a few
    seconds: one high-accuracy GPS fix and one low-accuracy cell/Wi-Fi fix
    anchored elsewhere. The cell/Wi-Fi ping typically has activity_type=None.
    See ISSUE-059.
    """

    def test_empty_input(self):
        from istota.geo import dedupe_near_duplicate_pings

        assert dedupe_near_duplicate_pings([]) == []

    def test_single_ping_passes_through(self):
        from istota.geo import dedupe_near_duplicate_pings

        pings = [{"timestamp": "2026-04-28T03:23:53Z", "lat": 34.1, "lon": -118.3,
                  "accuracy": 6.0, "activity_type": "walking"}]
        assert dedupe_near_duplicate_pings(pings) == pings

    def test_pings_more_than_5s_apart_both_kept(self):
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:00Z", "lat": 34.1, "lon": -118.3,
             "accuracy": 60.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:23:10Z", "lat": 34.1, "lon": -118.3,
             "accuracy": 6.0, "activity_type": "walking"},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 2

    def test_one_set_one_null_drops_null(self):
        """The most common case: cell/Wi-Fi ping has activity_type=None."""
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.10434, "lon": -118.30830,
             "accuracy": 63.0, "activity_type": "walking"},
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.10274, "lon": -118.30598,
             "accuracy": 56.0, "activity_type": None},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 1
        assert result[0]["activity_type"] == "walking"

    def test_one_null_one_set_drops_null_regardless_of_order(self):
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.10274, "lon": -118.30598,
             "accuracy": 56.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.10434, "lon": -118.30830,
             "accuracy": 63.0, "activity_type": "walking"},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 1
        assert result[0]["activity_type"] == "walking"

    def test_one_set_keeps_tagged_even_if_accuracy_worse(self):
        """activity_type wins over accuracy — confirmed by issue example
        20061/20062: null had 40m, walking had 55m, but walking is the real fix."""
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:27:21Z", "lat": 34.10456, "lon": -118.30962,
             "accuracy": 40.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:27:21Z", "lat": 34.10436, "lon": -118.30992,
             "accuracy": 55.0, "activity_type": "walking"},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 1
        assert result[0]["activity_type"] == "walking"

    def test_both_null_picks_better_accuracy(self):
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 55.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 14.0, "activity_type": None},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 1
        assert result[0]["accuracy"] == 14.0

    def test_both_null_equal_accuracy_keeps_both(self):
        """No way to distinguish — preserve raw data rather than guess."""
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 30.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.11, "lon": -118.31,
             "accuracy": 30.0, "activity_type": None},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 2

    def test_both_set_equal_accuracy_keeps_both(self):
        """Per design: rare case (18/204 in prod), keep both rather than drop a real fix."""
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 10.0, "activity_type": "driving"},
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.11, "lon": -118.31,
             "accuracy": 10.0, "activity_type": "driving"},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 2

    def test_both_set_unequal_accuracy_keeps_better(self):
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 14.0, "activity_type": "walking"},
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 5.0, "activity_type": "walking"},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 1
        assert result[0]["accuracy"] == 5.0

    def test_chain_of_three_within_window(self):
        """Three pings each within 5s of the next — chain dedup."""
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            # walking at the actual position
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.10434, "lon": -118.30830,
             "accuracy": 63.0, "activity_type": "walking"},
            # cell/Wi-Fi anchor near home — 1s later
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.10274, "lon": -118.30598,
             "accuracy": 56.0, "activity_type": None},
            # high-quality GPS — 11s after first, but only 10s after second
            # (still within 5s of nothing in kept set; should be kept)
            {"timestamp": "2026-04-28T03:23:53Z", "lat": 34.10428, "lon": -118.30889,
             "accuracy": 6.0, "activity_type": "walking"},
        ]
        result = dedupe_near_duplicate_pings(pings)
        # First two collapse to walking; third is 10s later → stays
        assert len(result) == 2
        assert all(p["activity_type"] == "walking" for p in result)

    def test_window_boundary_5s_inclusive(self):
        """A pair at exactly 5s apart should be deduped."""
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:00Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 60.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:23:05Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 6.0, "activity_type": "walking"},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 1
        assert result[0]["activity_type"] == "walking"

    def test_window_boundary_just_over_5s_keeps_both(self):
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:00Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 60.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:23:06Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 6.0, "activity_type": "walking"},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 2

    def test_zigzag_walk_collapses_cleanly(self):
        """Reproduces the 2026-04-27 issue example.

        Five pings at LA: real walking + cell/Wi-Fi anchor + real walking +
        place-matched ping pair. Expected: 3 walking pings survive (one per
        timestamp cluster), the cell anchors are dropped.
        """
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.1043368,
             "lon": -118.3082973, "accuracy": 63.0, "activity_type": "walking"},
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.10274185,
             "lon": -118.30598, "accuracy": 56.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:23:53Z", "lat": 34.104277,
             "lon": -118.3088875, "accuracy": 6.0, "activity_type": "walking"},
            {"timestamp": "2026-04-28T03:27:21Z", "lat": 34.10456,
             "lon": -118.30962, "accuracy": 40.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:27:21Z", "lat": 34.10436,
             "lon": -118.30992, "accuracy": 55.0, "activity_type": "walking"},
        ]
        result = dedupe_near_duplicate_pings(pings)
        # Pair 1 (42-43): walking wins → 1 ping
        # 53s ping: 10s after pair 1's winner → keeps standalone
        # Pair 3 (27:21 dup): walking wins → 1 ping
        assert len(result) == 3
        assert all(p["activity_type"] == "walking" for p in result)

    def test_missing_accuracy_treated_as_tie(self):
        """If both are null-activity and one lacks accuracy, can't compare → keep both."""
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.10, "lon": -118.30,
             "accuracy": None, "activity_type": None},
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 14.0, "activity_type": None},
        ]
        result = dedupe_near_duplicate_pings(pings)
        assert len(result) == 2

    def test_does_not_mutate_input(self):
        from istota.geo import dedupe_near_duplicate_pings

        pings = [
            {"timestamp": "2026-04-28T03:23:42Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 60.0, "activity_type": None},
            {"timestamp": "2026-04-28T03:23:43Z", "lat": 34.10, "lon": -118.30,
             "accuracy": 6.0, "activity_type": "walking"},
        ]
        original_len = len(pings)
        dedupe_near_duplicate_pings(pings)
        assert len(pings) == original_len


class TestClusterPlaceAttribution:
    """Place-aware clustering (option 6, ISSUE-062): a cluster is attributed to
    a place by counting per-ping place_id matches, not by the centroid. This
    sidesteps centroid contamination from walking legs and crosswalk waits and
    weeds out drive-by grazing pings via a minimum-count threshold.
    """

    def test_single_tagged_ping_does_not_anchor_place(self):
        """Drive-by: one grazing ping with place_id should not promote the
        cluster to a place — that's the phantom-stop scenario."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.10, "lon": -118.30, "timestamp": "2026-04-28T03:23:00Z"},
            {"lat": 34.10001, "lon": -118.30001, "timestamp": "2026-04-28T03:23:30Z",
             "place_id": 7, "place_name": "X"},
        ]
        result = cluster_pings(pings, radius_m=250)
        assert len(result) == 1
        assert result[0]["place_id"] is None
        assert result[0]["place_name"] is None

    def test_two_tagged_pings_below_threshold(self):
        """Two grazing pings still below threshold — slow drive-by territory."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.10, "lon": -118.30, "timestamp": "2026-04-28T03:23:00Z",
             "place_id": 7, "place_name": "X"},
            {"lat": 34.10001, "lon": -118.30001, "timestamp": "2026-04-28T03:23:10Z",
             "place_id": 7, "place_name": "X"},
            {"lat": 34.10002, "lon": -118.30002, "timestamp": "2026-04-28T03:23:20Z"},
        ]
        result = cluster_pings(pings, radius_m=250)
        assert len(result) == 1
        assert result[0]["place_id"] is None
        assert result[0]["place_name"] is None

    def test_three_tagged_pings_meets_threshold(self):
        """At MIN_PLACE_PINGS (3), the cluster takes on the place_id."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.10, "lon": -118.30, "timestamp": "2026-04-28T03:23:00Z",
             "place_id": 7, "place_name": "X"},
            {"lat": 34.10001, "lon": -118.30001, "timestamp": "2026-04-28T03:23:10Z",
             "place_id": 7, "place_name": "X"},
            {"lat": 34.10002, "lon": -118.30002, "timestamp": "2026-04-28T03:23:20Z",
             "place_id": 7, "place_name": "X"},
        ]
        result = cluster_pings(pings, radius_m=250)
        assert len(result) == 1
        assert result[0]["place_id"] == 7
        assert result[0]["place_name"] == "X"

    def test_lazy_acres_scenario(self):
        """Walking legs contaminate the centroid past the place radius, but the
        17 stationary pings inside the geofence still drive attribution.
        Reproduces the ISSUE-062 case: real-time webhook fired correct
        arrival/departure, day-summary should match."""
        from istota.geo import cluster_pings

        # 5 walk-in pings drifting east toward the store, no place_id
        # 17 stationary pings at the store (within the 75m geofence)
        # 19 walk-out pings drifting east, no place_id
        # Total: 41 pings, 17 tagged with Lazy Acres
        pings = []
        # Walk-in
        for i in range(5):
            pings.append({
                "lat": 34.1042, "lon": -118.3085 - 0.0001 * (5 - i),
                "timestamp": f"2026-04-28T03:38:{i * 10:02d}Z",
            })
        # At the store
        for i in range(17):
            pings.append({
                "lat": 34.1044, "lon": -118.3097,
                "timestamp": f"2026-04-28T03:39:{i * 10:02d}Z" if i < 6 else f"2026-04-28T03:{40 + (i - 6) // 6:02d}:{((i - 6) % 6) * 10:02d}Z",
                "place_id": 1398, "place_name": "Lazy Acres",
            })
        # Walk-out
        for i in range(19):
            pings.append({
                "lat": 34.1041, "lon": -118.3085 - 0.0001 * i,
                "timestamp": f"2026-04-28T03:{43 + i // 6:02d}:{(i % 6) * 10:02d}Z",
            })

        result = cluster_pings(pings, radius_m=250)
        # All 41 pings land in one cluster (intentional — that's the bug).
        # The fix: even though the centroid is contaminated, the 17 tagged
        # pings still attribute the cluster to Lazy Acres.
        attributed = [c for c in result if c["place_id"] == 1398]
        assert attributed, "expected at least one cluster attributed to Lazy Acres"

    def test_majority_wins_when_multiple_places(self):
        """If a cluster spans pings tagged with different place_ids, the
        most-counted one wins — provided it meets the threshold."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.10, "lon": -118.30, "timestamp": "2026-04-28T03:23:00Z",
             "place_id": 5, "place_name": "Y"},
            {"lat": 34.10001, "lon": -118.30001, "timestamp": "2026-04-28T03:23:10Z",
             "place_id": 6, "place_name": "Z"},
            {"lat": 34.10002, "lon": -118.30002, "timestamp": "2026-04-28T03:23:20Z",
             "place_id": 6, "place_name": "Z"},
            {"lat": 34.10003, "lon": -118.30003, "timestamp": "2026-04-28T03:23:30Z",
             "place_id": 6, "place_name": "Z"},
        ]
        result = cluster_pings(pings, radius_m=250)
        assert len(result) == 1
        assert result[0]["place_id"] == 6
        assert result[0]["place_name"] == "Z"

    def test_no_place_when_no_pings_have_place(self):
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.10, "lon": -118.30, "timestamp": "2026-04-28T03:23:00Z"},
            {"lat": 34.10001, "lon": -118.30001, "timestamp": "2026-04-28T03:23:30Z"},
        ]
        result = cluster_pings(pings, radius_m=250)
        assert len(result) == 1
        assert result[0]["place_id"] is None
        assert result[0]["place_name"] is None

    def test_stop_ends_when_reporting_resumes_outside_place(self):
        """A quiet tracker does not turn its last stationary ping into departure."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:24:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:29:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:34:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:38:33Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T15:45:17Z",
             "activity_type": "driving"},
        ]

        result = cluster_pings(pings)

        assert result[0]["place_name"] == "Gym"
        assert result[0]["last_ts"] == "2026-08-26T15:45:17Z"

    def test_stop_end_subtracts_travel_time_to_distant_closing_ping(self):
        """A distant resume ping bounds departure before the observation time."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:00:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:05:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:10:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1720, "lon": -118.3000, "timestamp": "2026-08-26T15:10:00Z",
             "speed": 20.0, "activity_type": "driving"},
        ]

        result = cluster_pings(pings)

        departure = datetime.fromisoformat(result[0]["last_ts"].replace("Z", "+00:00"))
        closing_ping = datetime.fromisoformat("2026-08-26T15:10:00+00:00")
        travel_seconds = (closing_ping - departure).total_seconds()
        assert 6 * 60 < travel_seconds < 7 * 60

    def test_stop_end_extension_is_capped(self):
        """A dead tracker cannot turn the next day's first ping into a day-long stop."""
        from istota.geo import MAX_STOP_EXTENSION_SECONDS, cluster_pings

        pings = [
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:00:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:05:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:10:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-27T08:00:00Z"},
        ]

        result = cluster_pings(pings)

        last_inside = datetime.fromisoformat("2026-08-26T14:10:00+00:00")
        departure = datetime.fromisoformat(result[0]["last_ts"].replace("Z", "+00:00"))
        assert (departure - last_inside).total_seconds() == MAX_STOP_EXTENSION_SECONDS

    def test_first_outside_ping_inside_cluster_closes_stop(self):
        """Nearby exit pings close a placed stop even when they do not split it."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:00:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:01:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1000, "lon": -118.3000, "timestamp": "2026-08-26T14:02:00Z",
             "place_id": 7, "place_name": "Gym"},
            {"lat": 34.1009, "lon": -118.3000, "timestamp": "2026-08-26T14:03:00Z",
             "speed": 10.0, "activity_type": "driving"},
            {"lat": 34.1012, "lon": -118.3000, "timestamp": "2026-08-26T14:04:00Z",
             "speed": 10.0, "activity_type": "driving"},
        ]

        result = cluster_pings(pings)

        assert len(result) == 1
        departure = datetime.fromisoformat(result[0]["last_ts"].replace("Z", "+00:00"))
        first_outside = datetime.fromisoformat("2026-08-26T14:03:00+00:00")
        assert 9 < (first_outside - departure).total_seconds() < 11


# ===========================================================================
# reverse-geocode CLI command tests
# ===========================================================================


class TestCmdReverseGeocode:
    def test_returns_json(self, tmp_path):
        from istota.skills.location import cmd_reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_reverse_geocode(conn, 34.05, -118.25, {
                "display_name": "Test Place",
                "neighborhood": "Hood",
                "suburb": "Sub",
                "road": "Road",
                "city": "City",
            })
            conn.commit()

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        args = MagicMock()
        args.lat = 34.05
        args.lon = -118.25

        with patch.dict("os.environ", env, clear=False):
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_reverse_geocode(args)
            finally:
                sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["source"] == "cache"
        assert result["display_name"] == "Test Place"

    @_needs_geopy
    def test_nominatim_fallback(self, tmp_path):
        from istota.skills.location import cmd_reverse_geocode

        db_path = _init_db(tmp_path)

        env = {"LOCATION_DB_PATH": str(db_path), "ISTOTA_DB_PATH": str(db_path)}
        args = MagicMock()
        args.lat = 34.15
        args.lon = -118.14

        mock_result = MagicMock()
        mock_result.address = "789 Pine St"
        mock_result.raw = {"address": {"road": "Pine St", "city": "Glendale"}}

        with patch.dict("os.environ", env, clear=False), \
             patch("geopy.geocoders.Nominatim") as mock_nom_cls:
            mock_geolocator = MagicMock()
            mock_geolocator.reverse.return_value = mock_result
            mock_nom_cls.return_value = mock_geolocator

            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_reverse_geocode(args)
            finally:
                sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["source"] == "nominatim"
        assert result["road"] == "Pine St"


# ===========================================================================
# day-summary CLI command tests
# ===========================================================================


@_needs_geopy
class TestCmdDaySummary:
    def _run_day_summary(self, tmp_path, pings=None, places=None,
                         date="2026-03-08", tz="America/Los_Angeles",
                         nominatim_results=None):
        """Helper to run cmd_day_summary with test DB and optional mocks.

        Uses two DBs to mirror production: per-user location.db for
        pings/places, framework istota.db for reverse_geocode_cache.
        """
        from istota.skills.location import cmd_day_summary

        loc_db = _init_loc_db(tmp_path, "location.db")
        framework_db = _init_db(tmp_path)  # for reverse_geocode_cache
        with location_db.connect(loc_db) as conn:
            for p in (places or []):
                location_db.add_place(
                    conn, p["name"], p["lat"], p["lon"],
                    radius_meters=p.get("radius_meters", 100),
                    category=p.get("category", "other"),
                )
            for ping in (pings or []):
                place_id = ping.get("place_id")
                location_db.insert_ping(
                    conn, ping["timestamp"], ping["lat"], ping["lon"],
                    accuracy=ping.get("accuracy", 5.0),
                    speed=ping.get("speed"),
                    activity_type=ping.get("activity_type"),
                    place_id=place_id,
                )
            conn.commit()

        env = {
            "LOCATION_DB_PATH": str(loc_db),
            "ISTOTA_DB_PATH": str(framework_db),
            "ISTOTA_USER_ID": "alice",
            "TZ": tz,
        }
        args = MagicMock()
        args.date = date
        args.tz = tz

        mock_nom = MagicMock()
        if nominatim_results:
            mock_nom.reverse.side_effect = nominatim_results
        else:
            mock_nom.reverse.return_value = None

        with patch.dict("os.environ", env, clear=False), \
             patch("geopy.geocoders.Nominatim", return_value=mock_nom):
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_day_summary(args)
            finally:
                sys.stdout = old_stdout

        return json.loads(captured.getvalue())

    def test_no_pings_empty_stops(self, tmp_path):
        result = self._run_day_summary(tmp_path)
        assert result["date"] == "2026-03-08"
        assert result["stops"] == []
        assert result["ping_count"] == 0

    def test_single_stop_at_saved_place(self, tmp_path):
        """Pings at a saved place should use the place name."""
        # March 8 in PST = UTC 2026-03-08T08:00:00Z to 2026-03-09T08:00:00Z.
        # Pings must be spaced within cluster_pings.max_gap_seconds (300s).
        places = [{"name": "home", "lat": 34.05, "lon": -118.25, "radius_meters": 150}]
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05, "lon": -118.25, "place_id": 1},
            {"timestamp": "2026-03-08T16:02:00Z", "lat": 34.0501, "lon": -118.2501, "place_id": 1},
            {"timestamp": "2026-03-08T16:04:00Z", "lat": 34.0502, "lon": -118.2502, "place_id": 1},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places)
        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "home"
        assert result["stops"][0]["location_source"] == "saved_place"
        assert result["stops"][0]["ping_count"] == 3

    def test_transit_filtered(self, tmp_path):
        """Clusters with <=2 pings and no place match should be excluded as transit."""
        pings = [
            # 3 pings at one spot (kept)
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05, "lon": -118.25},
            {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.0501, "lon": -118.2501},
            {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.0502, "lon": -118.2502},
            # 1 ping far away (filtered as transit)
            {"timestamp": "2026-03-08T17:00:00Z", "lat": 34.15, "lon": -118.35},
        ]
        result = self._run_day_summary(tmp_path, pings=pings)
        assert len(result["stops"]) == 1
        assert result["transit_pings"] == 1

    def test_proximity_place_match(self, tmp_path):
        """Cluster centroid near a saved place (within radius) uses place name."""
        places = [{"name": "cafe", "lat": 34.05, "lon": -118.25, "radius_meters": 50}]
        # Pings ~30m from saved place — within max(50, 100) = 100m
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05025, "lon": -118.25},
            {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.05027, "lon": -118.25001},
            {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.05029, "lon": -118.25002},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places)
        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "cafe"
        assert result["stops"][0]["location_source"] == "saved_place_proximity"

    def test_reverse_geocode_fallback(self, tmp_path):
        """When no place match, reverse geocode should be used."""
        mock_result = MagicMock()
        mock_result.address = "789 Elm St, Burbank, CA"
        mock_result.raw = {
            "address": {
                "road": "Elm St",
                "suburb": "Magnolia Park",
                "city": "Burbank",
            }
        }

        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.18, "lon": -118.33},
            {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.1802, "lon": -118.3302},
        ]
        result = self._run_day_summary(
            tmp_path, pings=pings,
            nominatim_results=[mock_result],
        )
        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "Magnolia Park"
        assert result["stops"][0]["suburb"] == "Magnolia Park"

    def test_consecutive_same_location_merged(self, tmp_path):
        """Two consecutive clusters at the same saved place should merge."""
        places = [{"name": "office", "lat": 34.05, "lon": -118.25, "radius_meters": 200}]
        pings = [
            # Cluster 1 at office
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05, "lon": -118.25, "place_id": 1},
            {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.0501, "lon": -118.2501, "place_id": 1},
            {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.0502, "lon": -118.2502, "place_id": 1},
            # Brief transit ping (filtered out)
            {"timestamp": "2026-03-08T17:00:00Z", "lat": 34.15, "lon": -118.35},
            # Cluster 2 at office again
            {"timestamp": "2026-03-08T18:00:00Z", "lat": 34.05, "lon": -118.25, "place_id": 1},
            {"timestamp": "2026-03-08T18:05:00Z", "lat": 34.0501, "lon": -118.2501, "place_id": 1},
            {"timestamp": "2026-03-08T18:10:00Z", "lat": 34.0502, "lon": -118.2502, "place_id": 1},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places)
        # Two clusters at "office" with transit filtered → should merge into one
        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "office"
        assert result["stops"][0]["ping_count"] == 6

    def test_same_location_not_merged_after_real_trip(self, tmp_path):
        """Home→trip→Home should show two separate Home stops, not one merged."""
        places = [
            {"name": "Home", "lat": 34.1025, "lon": -118.3059, "radius_meters": 100},
            {"name": "Restaurant", "lat": 34.076, "lon": -118.305, "radius_meters": 100},
        ]
        pings = [
            # Home cluster 1
            {"timestamp": "2026-03-09T00:50:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T00:50:30Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T00:52:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            # Driving away (many transit pings)
            {"timestamp": "2026-03-09T02:48:00Z", "lat": 34.1029, "lon": -118.3068},
            {"timestamp": "2026-03-09T02:48:10Z", "lat": 34.1017, "lon": -118.3078},
            {"timestamp": "2026-03-09T02:48:20Z", "lat": 34.1017, "lon": -118.3088},
            {"timestamp": "2026-03-09T02:48:30Z", "lat": 34.1006, "lon": -118.3093},
            {"timestamp": "2026-03-09T02:49:00Z", "lat": 34.0981, "lon": -118.3093},
            {"timestamp": "2026-03-09T02:49:30Z", "lat": 34.0960, "lon": -118.3093},
            {"timestamp": "2026-03-09T02:50:00Z", "lat": 34.0937, "lon": -118.3092},
            {"timestamp": "2026-03-09T02:51:00Z", "lat": 34.0870, "lon": -118.3092},
            {"timestamp": "2026-03-09T02:52:00Z", "lat": 34.0806, "lon": -118.3091},
            # Dinner (few pings, short dwell, no saved place nearby)
            {"timestamp": "2026-03-09T02:58:00Z", "lat": 34.070, "lon": -118.300},
            {"timestamp": "2026-03-09T02:59:00Z", "lat": 34.070, "lon": -118.300},
            # Driving back
            {"timestamp": "2026-03-09T03:37:00Z", "lat": 34.080, "lon": -118.309},
            {"timestamp": "2026-03-09T03:38:00Z", "lat": 34.087, "lon": -118.309},
            {"timestamp": "2026-03-09T03:40:00Z", "lat": 34.094, "lon": -118.309},
            {"timestamp": "2026-03-09T03:43:00Z", "lat": 34.097, "lon": -118.309},
            {"timestamp": "2026-03-09T03:45:00Z", "lat": 34.100, "lon": -118.309},
            # Home cluster 2
            {"timestamp": "2026-03-09T03:47:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T03:48:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T03:53:00Z", "lat": 34.1026, "lon": -118.3058, "place_id": 1},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places,
                                        date="2026-03-08", tz="America/Los_Angeles")
        home_stops = [s for s in result["stops"] if s["location"] == "Home"]
        assert len(home_stops) == 2, (
            f"Expected 2 Home stops (left and returned), got {len(home_stops)}: {result['stops']}"
        )

    def test_same_location_merged_after_phone_sleep(self, tmp_path):
        """Home with phone sleep gap (no transit) should merge into one stop."""
        places = [
            {"name": "Home", "lat": 34.1025, "lon": -118.3059, "radius_meters": 100},
        ]
        # Each side needs ≥3 pings tagged with the place_id to get attributed
        # (MIN_PLACE_PINGS=3); ping spacing must be ≤300s for cluster_pings to
        # keep them in one cluster on each side of the sleep gap.
        pings = [
            # Home cluster 1
            {"timestamp": "2026-03-09T00:48:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T00:50:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T00:52:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            # 2-hour gap (phone sleeping, no pings at all)
            # Home cluster 2
            {"timestamp": "2026-03-09T02:46:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T02:48:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T02:50:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places,
                                        date="2026-03-08", tz="America/Los_Angeles")
        home_stops = [s for s in result["stops"] if s["location"] == "Home"]
        assert len(home_stops) == 1, (
            f"Expected 1 merged Home stop (phone sleep, no transit), got {len(home_stops)}"
        )

    def test_indoor_gps_gaps_preserve_stop(self, tmp_path):
        """Indoor GPS gaps should not drop a stop from the summary.

        Simulates the ISSUE-043 scenario: phone at a restaurant for ~95 min
        with large gaps between pings due to indoor GPS signal loss.
        """
        lat, lon = 34.0836, -118.3101
        pings = [
            # Cluster 1: strong initial fix (7:07-7:21 PM PST = 03:07-03:21 UTC)
            *[{"timestamp": f"2026-03-09T03:{7+i:02d}:00Z", "lat": lat + i*0.00001,
               "lon": lon, "place_id": None}
              for i in range(15)],
            # 6-minute gap (indoor)
            # Cluster 2: brief fix (7:27 PM)
            {"timestamp": "2026-03-09T03:27:00Z", "lat": lat + 0.00005, "lon": lon, "place_id": None},
            # 40-minute gap (deep indoor)
            # Cluster 3: brief fix (8:07 PM)
            {"timestamp": "2026-03-09T04:07:00Z", "lat": lat - 0.00003, "lon": lon, "place_id": None},
            {"timestamp": "2026-03-09T04:08:00Z", "lat": lat - 0.00002, "lon": lon, "place_id": None},
            # 20-minute gap
            # Cluster 4: leaving (8:28-8:42 PM)
            *[{"timestamp": f"2026-03-09T04:{28+i}:00Z", "lat": lat + i*0.00001,
               "lon": lon, "place_id": None}
              for i in range(5)],
        ]
        result = self._run_day_summary(tmp_path, pings=pings,
                                        date="2026-03-08", tz="America/Los_Angeles")
        # All pings are at the same location — should be one stop
        assert len(result["stops"]) == 1, (
            f"Expected 1 stop (indoor GPS gaps), got {len(result['stops'])}: {result['stops']}"
        )
        # The stop should span the full visit
        assert result["stops"][0]["ping_count"] == len(pings)

    def test_duration_minutes_in_output(self, tmp_path):
        """Each stop should include a pre-computed duration_minutes field (ISSUE-047 bug B)."""
        places = [{"name": "home", "lat": 34.05, "lon": -118.25, "radius_meters": 150}]
        # 2 hours at home (16:00-18:00 UTC on March 8 = within PST day).
        # Pings every 5 minutes keep them in a single cluster
        # (cluster_pings.max_gap_seconds=300).
        pings = [
            {
                "timestamp": f"2026-03-08T{16 + (5 * i) // 60:02d}:{(5 * i) % 60:02d}:00Z",
                "lat": 34.05 + i * 0.00001,
                "lon": -118.25,
                "place_id": 1,
            }
            for i in range(25)  # 0, 5, 10, …, 120 minutes — 25 pings
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places)
        assert len(result["stops"]) == 1
        stop = result["stops"][0]
        assert "duration_minutes" in stop
        assert stop["duration_minutes"] == 120

    def test_duration_uses_first_ping_after_stationary_reporting_gap(self, tmp_path):
        """Day summary counts the quiet part of a saved-place visit."""
        places = [{"name": "Gym", "lat": 34.10, "lon": -118.30, "radius_meters": 150}]
        pings = [
            {"timestamp": "2026-03-08T14:24:00Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-08T14:29:00Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-08T14:34:00Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-08T14:38:33Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-08T15:45:17Z", "lat": 34.10, "lon": -118.30,
             "activity_type": "driving"},
        ]

        result = self._run_day_summary(tmp_path, pings=pings, places=places)

        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "Gym"
        assert result["stops"][0]["duration_minutes"] == 81
        assert result["stops"][0]["departed"] == "08:45"

    def test_duration_uses_recorded_speed_for_distant_closing_ping(self, tmp_path):
        places = [{"name": "Gym", "lat": 34.10, "lon": -118.30, "radius_meters": 150}]
        pings = [
            {"timestamp": "2026-03-08T14:00:00Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-08T14:05:00Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-08T14:10:00Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-08T15:10:00Z", "lat": 34.172, "lon": -118.30,
             "speed": 20.0, "activity_type": "driving"},
        ]

        result = self._run_day_summary(tmp_path, pings=pings, places=places)

        assert result["stops"][0]["duration_minutes"] == 63

    def test_closing_ping_after_local_midnight_ends_stop(self, tmp_path):
        places = [{"name": "Home", "lat": 34.10, "lon": -118.30, "radius_meters": 150}]
        pings = [
            {"timestamp": "2026-03-09T06:40:00Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-09T06:45:00Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-09T06:50:00Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-09T07:10:00Z", "lat": 34.10, "lon": -118.30,
             "activity_type": "driving"},
        ]

        result = self._run_day_summary(tmp_path, pings=pings, places=places)

        assert result["ping_count"] == 3
        assert result["stops"][0]["ping_count"] == 3
        assert result["stops"][0]["duration_minutes"] == 30
        assert result["stops"][0]["departed"] == "00:10"

    def test_duration_minutes_for_nominatim_stop(self, tmp_path):
        """duration_minutes should work for reverse-geocoded stops too."""
        mock_result = MagicMock()
        mock_result.address = "Test Place"
        mock_result.raw = {"address": {"suburb": "TestVille"}}

        # 30-minute stop with pings close enough to avoid cluster splitting
        # (max_gap_seconds=300, so keep gaps under 5 min)
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.18, "lon": -118.33},
            {"timestamp": "2026-03-08T16:04:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:08:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:12:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:16:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:20:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:24:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:28:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:30:00Z", "lat": 34.1802, "lon": -118.3302},
        ]
        result = self._run_day_summary(
            tmp_path, pings=pings,
            nominatim_results=[mock_result],
        )
        assert len(result["stops"]) == 1
        assert result["stops"][0]["duration_minutes"] == 30

    def test_nearby_stops_with_different_geocoded_names_merge(self, tmp_path):
        """ISSUE-047 scenario: GPS drift causes different road names for same location.

        Three clusters at nearly identical coordinates get different reverse-geocoded
        names. They should merge into a single stop via proximity check.
        """
        # Three nominatim results returning different road names
        results = []
        for road in ["East Live Oak Drive", "Tryon Road", "East Live Oak Drive"]:
            r = MagicMock()
            r.address = f"{road}, Los Feliz, CA"
            r.raw = {"address": {"road": road, "suburb": "Los Feliz"}}
            results.append(r)

        # Three clusters ~110m apart (within 150m merge radius), separated by
        # time gaps that cause cluster splitting. Each cluster is big enough
        # (6+ min dwell, 3+ pings) to independently survive transit filtering,
        # so they reach merge_consecutive_stops as separate stops.
        # Coords differ enough that geocode cache gives different results.
        pings = [
            # Cluster 1: "East Live Oak Drive" — 5 pings over 10 min
            *[{"timestamp": f"2026-03-09T01:{i*2:02d}:00Z",
               "lat": 34.1086, "lon": -118.3099}
              for i in range(5)],
            # > 5min gap → new cluster
            # Cluster 2: "Tryon Road" — ~110m from cluster 1, 5 pings over 10 min
            *[{"timestamp": f"2026-03-09T01:{20+i*2:02d}:00Z",
               "lat": 34.1096, "lon": -118.3099}
              for i in range(5)],
            # > 5min gap → new cluster
            # Cluster 3: "East Live Oak Drive" again, 5 pings over 10 min
            *[{"timestamp": f"2026-03-09T01:{40+i*2:02d}:00Z",
               "lat": 34.1086, "lon": -118.3099}
              for i in range(5)],
        ]
        result = self._run_day_summary(
            tmp_path, pings=pings, date="2026-03-08", tz="America/Los_Angeles",
            nominatim_results=results,
        )
        # All three clusters should merge into one stop
        assert len(result["stops"]) == 1, (
            f"Expected 1 merged stop, got {len(result['stops'])}: "
            f"{[s['location'] for s in result['stops']]}"
        )
        assert result["stops"][0]["ping_count"] == 15


# ===========================================================================
# Accuracy gate + dwell-based exit + reconciliation
# ===========================================================================


@_needs_fastapi
class TestAccuracyGate:
    """Low-accuracy pings must not be matched to places or move the state machine."""

    def _feature(self, lat, lon, ts, accuracy):
        return {
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"timestamp": ts, "horizontal_accuracy": accuracy},
        }

    def test_low_accuracy_ping_not_assigned_to_place(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.add_place(
                conn, "home", 35.629, 139.741, radius_meters=200,
            )
            places = location_db.get_places(conn)

            cfg = MagicMock()
            cfg.location.accuracy_threshold_m = 100.0
            cfg.location.visit_exit_minutes = 5.0
            monkeypatch.setattr(wr, "_config", cfg)

            feat = self._feature(35.629, 139.741, "2026-04-21T08:19:35Z", accuracy=1336)
            wr._process_feature(conn, feat, places)
            conn.commit()

            pings = location_db.get_pings(conn)
            assert len(pings) == 1
            assert pings[0].place_id is None, (
                "1336m accuracy ping should not have been assigned to the place"
            )
            assert location_db.get_open_visit(conn) is None

    def test_good_accuracy_ping_is_assigned(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "home", 35.629, 139.741, radius_meters=200,
            )
            places = location_db.get_places(conn)

            cfg = MagicMock()
            cfg.location.accuracy_threshold_m = 100.0
            cfg.location.visit_exit_minutes = 5.0
            monkeypatch.setattr(wr, "_config", cfg)

            feat = self._feature(35.629, 139.741, "2026-04-21T08:20:00Z", accuracy=15)
            wr._process_feature(conn, feat, places)
            conn.commit()

            pings = location_db.get_pings(conn)
            assert pings[0].place_id == pid

    def test_null_accuracy_passes(self, tmp_path, monkeypatch):
        """Missing accuracy shouldn't cause us to drop the ping silently."""
        from istota import webhook_receiver as wr
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "home", 35.629, 139.741, radius_meters=200,
            )
            places = location_db.get_places(conn)

            cfg = MagicMock()
            cfg.location.accuracy_threshold_m = 100.0
            cfg.location.visit_exit_minutes = 5.0
            monkeypatch.setattr(wr, "_config", cfg)

            feat = {
                "geometry": {"type": "Point", "coordinates": [139.741, 35.629]},
                "properties": {"timestamp": "2026-04-21T08:20:00Z"},
            }
            wr._process_feature(conn, feat, places)
            conn.commit()

            pings = location_db.get_pings(conn)
            assert pings[0].place_id == pid


@_needs_fastapi
class TestDwellBasedExit:
    """Brief GPS flicker out of place radius must not close an open visit."""

    def _process(self, conn, place_id, place, timestamp):
        from istota.webhook_receiver import _update_state_machine
        ping_id = location_db.insert_ping(
            conn, timestamp, 0.0, 0.0, accuracy=10.0,
            place_id=place_id,
        )
        _update_state_machine(conn, ping_id, place_id, place, timestamp)
        return ping_id

    def test_flicker_does_not_close_visit(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        cfg = MagicMock()
        cfg.location.visit_exit_minutes = 5.0
        cfg.location.accuracy_threshold_m = 100.0
        monkeypatch.setattr(wr, "_config", cfg)

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            place = location_db.get_place_by_name(conn, "home")

            self._process(conn, pid, place, "2026-04-21T10:00:00Z")
            self._process(conn, pid, place, "2026-04-21T10:00:30Z")

            self._process(conn, None, None, "2026-04-21T10:01:00Z")
            self._process(conn, pid, place, "2026-04-21T10:02:00Z")
            self._process(conn, None, None, "2026-04-21T10:03:00Z")
            self._process(conn, pid, place, "2026-04-21T10:04:00Z")
            self._process(conn, None, None, "2026-04-21T10:05:00Z")
            self._process(conn, pid, place, "2026-04-21T10:06:00Z")

            visits = location_db.get_visits(conn)
            assert len(visits) == 1, "Flicker should not create extra visits"
            assert visits[0].exited_at is None, "Visit should still be open"

    def test_continuous_away_closes_after_threshold(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        cfg = MagicMock()
        cfg.location.visit_exit_minutes = 5.0
        cfg.location.accuracy_threshold_m = 100.0
        monkeypatch.setattr(wr, "_config", cfg)

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            place = location_db.get_place_by_name(conn, "home")

            self._process(conn, pid, place, "2026-04-21T10:00:00Z")
            self._process(conn, pid, place, "2026-04-21T10:05:00Z")

            self._process(conn, None, None, "2026-04-21T10:10:00Z")
            self._process(conn, None, None, "2026-04-21T10:12:00Z")
            self._process(conn, None, None, "2026-04-21T10:14:00Z")
            self._process(conn, None, None, "2026-04-21T10:16:00Z")

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].exited_at == "2026-04-21T10:10:00Z", (
                "Exited_at should be the first away ping, not the last"
            )
            assert visits[0].duration_sec == 600

    def test_away_then_return_extends_visit(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        cfg = MagicMock()
        cfg.location.visit_exit_minutes = 5.0
        cfg.location.accuracy_threshold_m = 100.0
        monkeypatch.setattr(wr, "_config", cfg)

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            place = location_db.get_place_by_name(conn, "home")

            self._process(conn, pid, place, "2026-04-21T10:00:00Z")
            self._process(conn, pid, place, "2026-04-21T10:05:00Z")
            self._process(conn, None, None, "2026-04-21T10:06:00Z")
            self._process(conn, None, None, "2026-04-21T10:07:30Z")
            self._process(conn, pid, place, "2026-04-21T10:08:00Z")
            self._process(conn, pid, place, "2026-04-21T10:20:00Z")

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].exited_at is None, "Visit should still be open"

            state = location_db.get_location_state(conn)
            assert state.exit_started_at is None

    def test_direct_place_to_place_closes_old_opens_new(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        cfg = MagicMock()
        cfg.location.visit_exit_minutes = 5.0
        cfg.location.accuracy_threshold_m = 100.0
        monkeypatch.setattr(wr, "_config", cfg)

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_h = location_db.add_place(conn, "home", 34.0, -118.0)
            pid_g = location_db.add_place(conn, "gym", 34.1, -118.1)
            home = location_db.get_place_by_name(conn, "home")
            gym = location_db.get_place_by_name(conn, "gym")

            self._process(conn, pid_h, home, "2026-04-21T10:00:00Z")
            self._process(conn, pid_h, home, "2026-04-21T10:05:00Z")
            self._process(conn, pid_g, gym, "2026-04-21T10:06:00Z")
            self._process(conn, pid_g, gym, "2026-04-21T10:07:00Z")

            visits = location_db.get_visits(conn)
            assert len(visits) == 2
            home_visit = [v for v in visits if v.place_name == "home"][0]
            gym_visit = [v for v in visits if v.place_name == "gym"][0]
            assert home_visit.exited_at is not None
            assert gym_visit.exited_at is None


class TestReconcileVisits:
    def _ping(self, conn, ts, place_id):
        location_db.insert_ping(
            conn, ts, 0.0, 0.0, accuracy=10.0, place_id=place_id,
        )

    def test_reconciles_fragmented_visit_into_one(self, tmp_path):
        """The Shinagawa case: flicker split a single stay into many short segments."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            # 15 pings mostly at place, a handful briefly outside
            at_place = [f"2026-04-21T10:{m:02d}:00Z" for m in range(0, 30, 2)]
            for ts in at_place:
                self._ping(conn, ts, pid)
            # sprinkle a few unassigned pings in between — gaps < grace
            for ts in ("2026-04-21T10:05:30Z", "2026-04-21T10:13:30Z", "2026-04-21T10:19:30Z"):
                self._ping(conn, ts, None)
            conn.commit()

            n = location_db.reconcile_visits(conn, since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            conn.commit()

            assert n == 1
            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-21T10:00:00Z"
            assert visits[0].exited_at == "2026-04-21T10:28:00Z"
            assert visits[0].ping_count == 15

    def test_same_place_reporting_gaps_do_not_split_visit(self, tmp_path):
        """ISSUE-329: silence is not evidence that the user left a place."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "gym", 34.0, -118.0)
            timestamps = [
                "2026-01-10T09:00:00Z", "2026-01-10T09:01:00Z",
                "2026-01-10T09:02:00Z", "2026-01-10T09:04:00Z",
                "2026-01-10T09:06:00Z", "2026-01-10T09:07:00Z",
                "2026-01-10T09:08:00Z", "2026-01-10T09:09:00Z",
                "2026-01-10T09:33:00Z", "2026-01-10T09:34:00Z",
                "2026-01-10T09:36:00Z", "2026-01-10T09:39:00Z",
                "2026-01-10T09:41:00Z", "2026-01-10T09:44:00Z",
                "2026-01-10T09:47:00Z", "2026-01-10T09:51:00Z",
                "2026-01-10T10:20:00Z", "2026-01-10T10:21:00Z",
                "2026-01-10T10:22:00Z", "2026-01-10T10:23:00Z",
                "2026-01-10T10:24:00Z", "2026-01-10T10:25:00Z",
                "2026-01-10T10:26:00Z", "2026-01-10T10:27:00Z",
                "2026-01-10T10:29:00Z",
            ]
            for ts in timestamps:
                self._ping(conn, ts, pid)
            conn.commit()

            n = location_db.reconcile_visits(conn, since="2026-01-10T00:00:00Z", until="2026-01-11T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            conn.commit()

            assert n == 1
            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-01-10T09:00:00Z"
            assert visits[0].exited_at == "2026-01-10T10:29:00Z"
            assert visits[0].ping_count == 25

    def test_same_place_merge_replaces_visit_before_window(self, tmp_path):
        """A merged segment must not overlap a preserved pre-fix visit."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "gym", 34.0, -118.0)
            first_id = location_db.open_visit(
                conn, pid, "gym", "2026-01-10T08:00:00Z",
            )
            location_db.close_visit(conn, first_id, "2026-01-10T08:08:00Z")
            second_id = location_db.open_visit(
                conn, pid, "gym", "2026-01-10T10:00:00Z",
            )
            location_db.close_visit(conn, second_id, "2026-01-10T10:08:00Z")
            for minute in (0, 4, 8):
                location_db.insert_ping(
                    conn, f"2026-01-10T08:{minute:02d}:00Z", 0.0, 0.0,
                    accuracy=10.0, place_id=pid, visit_id=first_id,
                )
                location_db.insert_ping(
                    conn, f"2026-01-10T10:{minute:02d}:00Z", 0.0, 0.0,
                    accuracy=10.0, place_id=pid, visit_id=second_id,
                )
            conn.commit()

            location_db.reconcile_visits(
                conn,
                since="2026-01-10T09:00:00Z",
                until="2026-01-10T11:00:00Z",
                grace_minutes=10.0,
                min_pings=3,
                min_dwell_sec=60,
            )
            conn.commit()

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-01-10T08:00:00Z"
            assert visits[0].exited_at == "2026-01-10T10:08:00Z"
            assert visits[0].ping_count == 6

    def test_filters_walkby(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            # Only 2 pings at place — below min_pings=3
            self._ping(conn, "2026-04-21T10:00:00Z", pid)
            self._ping(conn, "2026-04-21T10:01:00Z", pid)
            conn.commit()

            n = location_db.reconcile_visits(conn, since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            assert n == 0
            assert location_db.get_visits(conn) == []

    def test_splits_on_different_place(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_a = location_db.add_place(conn, "home", 34.0, -118.0)
            pid_b = location_db.add_place(conn, "gym", 34.1, -118.1)
            for m in range(0, 10, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid_a)
            for m in range(12, 22, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid_b)
            conn.commit()

            n = location_db.reconcile_visits(conn, since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            assert n == 2
            visits = sorted(location_db.get_visits(conn), key=lambda v: v.entered_at)
            assert visits[0].place_name == "home"
            assert visits[1].place_name == "gym"

    def test_preserves_open_visit_outside_window(self, tmp_path):
        """An open visit started before `since` must be left alone."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            # Open visit entered before reconcile window
            vid = location_db.open_visit(conn, pid, "home", "2026-04-20T23:00:00Z")
            for m in range(0, 10, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            location_db.reconcile_visits(conn, since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            conn.commit()

            visits = location_db.get_visits(conn)
            # The open visit must still exist and be open
            open_ones = [v for v in visits if v.exited_at is None]
            assert len(open_ones) == 1
            assert open_ones[0].id == vid

    def test_accuracy_filter_drops_bad_pings(self, tmp_path):
        """Historical pings with accuracy > threshold are treated as unassigned."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            # One early bad-accuracy ping pinned to the place (like the 1336m Shinagawa case)
            location_db.insert_ping(conn, "2026-04-21T08:00:00Z", 35.629, 139.741,
                accuracy=1200.0, place_id=pid,
            )
            # Real visit starts later with good pings
            for m in range(30, 50, 2):
                location_db.insert_ping(conn, f"2026-04-21T08:{m:02d}:00Z", 35.629, 139.741,
                    accuracy=10.0, place_id=pid,
                )
            conn.commit()

            # Without filter: the bad ping would anchor a visit starting at 08:00
            location_db.reconcile_visits(conn, since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
                accuracy_threshold_m=100.0,
            )
            conn.commit()

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-21T08:30:00Z", (
                "Bad-accuracy ping should not have anchored the visit's entered_at"
            )
            assert visits[0].exited_at == "2026-04-21T08:48:00Z"

    def test_replaces_stale_closed_visits_in_window(self, tmp_path):
        """Existing closed visits in the window are dropped before re-derivation."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            # Seed with an incorrect, short closed visit
            stale_id = location_db.open_visit(conn, pid, "home", "2026-04-21T10:05:00Z")
            location_db.close_visit(conn, stale_id, "2026-04-21T10:07:00Z")
            # Pings showing the true longer stay
            for m in range(0, 30, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            location_db.reconcile_visits(conn, since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            conn.commit()

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].id != stale_id  # stale row deleted
            assert visits[0].entered_at == "2026-04-21T10:00:00Z"
            assert visits[0].exited_at == "2026-04-21T10:28:00Z"

    def test_idempotent_across_sliding_windows(self, tmp_path):
        """ISSUE-064: sliding window must not accumulate phantom visits.

        The daemon runs reconcile_visits every minute over a sliding window.
        When `since` advances past a visit's first ping but the last ping is
        still in window, prior runs must be cleaned up — not duplicated.
        """
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "lazy_acres", 32.78, -117.18)
            # 20 sparse pings over 11 minutes (Lazy Acres pattern)
            ping_times = [
                "2026-04-29T03:38:00Z", "2026-04-29T03:38:30Z",
                "2026-04-29T03:39:00Z", "2026-04-29T03:39:30Z",
                "2026-04-29T03:40:15Z", "2026-04-29T03:40:50Z",
                "2026-04-29T03:41:30Z", "2026-04-29T03:42:10Z",
                "2026-04-29T03:42:50Z", "2026-04-29T03:43:30Z",
                "2026-04-29T03:44:10Z", "2026-04-29T03:44:55Z",
                "2026-04-29T03:45:40Z", "2026-04-29T03:46:20Z",
                "2026-04-29T03:47:00Z", "2026-04-29T03:47:40Z",
                "2026-04-29T03:48:10Z", "2026-04-29T03:48:40Z",
                "2026-04-29T03:49:00Z", "2026-04-29T03:49:30Z",
            ]
            for ts in ping_times:
                self._ping(conn, ts, pid)
            conn.commit()

            # Three reconciler runs with `since` sliding past the visit's
            # first ping but `until` still after the last ping.
            for since, until in [
                ("2026-04-29T03:30:00Z", "2026-04-29T03:50:00Z"),
                ("2026-04-29T03:40:00Z", "2026-04-29T03:56:00Z"),
                ("2026-04-29T03:43:00Z", "2026-04-29T04:03:00Z"),
            ]:
                location_db.reconcile_visits(conn, since=since, until=until,
                    grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
                    accuracy_threshold_m=100.0,
                )
                conn.commit()

            visits = location_db.get_visits(conn)
            assert len(visits) == 1, (
                f"Expected 1 visit after sliding-window runs, got {len(visits)}: "
                f"{[(v.id, v.entered_at, v.exited_at) for v in visits]}"
            )
            assert visits[0].entered_at == "2026-04-29T03:38:00Z"
            assert visits[0].exited_at == "2026-04-29T03:49:30Z"
            assert visits[0].ping_count == 20

    def test_visit_straddling_since_not_truncated(self, tmp_path):
        """A visit whose first ping is before `since` must be reconstructed in full."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            # Visit pings span 09:50 - 10:10; reconcile window starts at 10:00.
            for m in range(50, 60, 2):
                self._ping(conn, f"2026-04-21T09:{m:02d}:00Z", pid)
            for m in range(0, 12, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            location_db.reconcile_visits(conn, since="2026-04-21T10:00:00Z", until="2026-04-21T11:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
                accuracy_threshold_m=100.0,
            )
            conn.commit()

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-21T09:50:00Z", (
                "Read-back must find the visit's true first ping outside the window"
            )
            assert visits[0].exited_at == "2026-04-21T10:10:00Z"

    def test_visit_entirely_before_window_untouched(self, tmp_path):
        """A closed visit that ended before `since` must be left alone."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            # Pre-existing closed visit from earlier in the day
            old_id = location_db.open_visit(conn, pid, "home", "2026-04-21T08:00:00Z")
            location_db.close_visit(conn, old_id, "2026-04-21T08:30:00Z")
            # Pings for a different, later visit that we will reconcile
            for m in range(0, 12, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            location_db.reconcile_visits(conn, since="2026-04-21T09:00:00Z", until="2026-04-21T11:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
                accuracy_threshold_m=100.0,
            )
            conn.commit()

            visits = sorted(location_db.get_visits(conn), key=lambda v: v.entered_at)
            assert len(visits) == 2
            assert visits[0].id == old_id
            assert visits[0].entered_at == "2026-04-21T08:00:00Z"
            assert visits[1].entered_at == "2026-04-21T10:00:00Z"

    def test_reconcile_with_pings_linked_to_old_visit(self, tmp_path):
        """Pings with visit_id pointing at the about-to-be-deleted visit must
        not trigger FOREIGN KEY constraint failed. Regression for the per-user
        location.db split: `connect()` enables `PRAGMA foreign_keys = ON`,
        which the framework istota.db never did.
        """
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            old_visit_id = location_db.open_visit(conn, pid, "home", "2026-04-21T10:00:00Z")
            location_db.close_visit(conn, old_visit_id, "2026-04-21T10:28:00Z")
            # Pings with visit_id set — the realistic state after live ingest
            for m in range(0, 30, 2):
                location_db.insert_ping(
                    conn, f"2026-04-21T10:{m:02d}:00Z", 0.0, 0.0,
                    accuracy=10.0, place_id=pid, visit_id=old_visit_id,
                )
            conn.commit()

            n = location_db.reconcile_visits(
                conn, since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            conn.commit()

            assert n == 1
            visits = location_db.get_visits(conn)
            assert len(visits) == 1

    def test_cleans_up_phantoms_from_prior_buggy_runs(self, tmp_path):
        """Pre-existing duplicate visits (from the bug) must be replaced by one."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "lazy_acres", 32.78, -117.18)
            # Three phantom visits with staggered entries, identical exits, no pings linked
            for entry in ("2026-04-29T03:38:00Z",
                          "2026-04-29T03:40:15Z",
                          "2026-04-29T03:43:30Z"):
                vid = location_db.open_visit(conn, pid, "lazy_acres", entry)
                location_db.close_visit(conn, vid, "2026-04-29T03:49:30Z")
            # Real pings (would be linked to a different visit_id in production)
            for ts in (
                "2026-04-29T03:38:00Z", "2026-04-29T03:39:00Z",
                "2026-04-29T03:42:00Z", "2026-04-29T03:45:00Z",
                "2026-04-29T03:47:00Z", "2026-04-29T03:49:30Z",
            ):
                self._ping(conn, ts, pid)
            conn.commit()

            location_db.reconcile_visits(conn, since="2026-04-29T03:00:00Z", until="2026-04-29T04:30:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
                accuracy_threshold_m=100.0,
            )
            conn.commit()

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-29T03:38:00Z"
            assert visits[0].exited_at == "2026-04-29T03:49:30Z"


class TestCurrentLastAlias:
    """`last` is an alias for `current` — the natural name an LLM reaches for."""

    def test_last_alias_parses(self):
        from istota.skills.location import build_parser
        args = build_parser().parse_args(["last"])
        assert args.command == "last"

    def test_last_alias_dispatches_to_cmd_current(self):
        from istota.skills.location import main
        with patch("istota.skills.location.cmd_current") as m, \
                patch.object(sys, "argv", ["loc", "last"]):
            main()
        assert m.called


class TestGarminImportSkill:
    """The location skill's import-garmin-tracks subcommand. In a sandbox
    (no master key) it delegates by writing a deferred op the scheduler
    runs post-task."""

    def _run(self, args, env, monkeypatch):
        import io
        import sys
        from istota import secrets_store
        from istota.skills.location import cmd_import_garmin_tracks

        # Force the delegated path deterministically.
        monkeypatch.setattr(secrets_store, "secret_key_available", lambda: False)
        with patch.dict("os.environ", env, clear=False):
            captured = io.StringIO()
            old = sys.stdout
            sys.stdout = captured
            try:
                cmd_import_garmin_tracks(args)
                code = 0
            except SystemExit as e:
                code = e.code or 0
            finally:
                sys.stdout = old
        return code, captured.getvalue()

    def test_delegated_write(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {
            "ISTOTA_USER_ID": "alice",
            "ISTOTA_DEFERRED_DIR": str(deferred),
            "ISTOTA_TASK_ID": "99",
        }
        args = MagicMock(days_back=14, dry_run=False)
        code, out = self._run(args, env, monkeypatch)
        assert code == 0
        payload = json.loads(out)
        assert payload["status"] == "ok" and payload["queued"] is True
        opfile = deferred / "task_99_garmin_import.json"
        assert opfile.exists()
        assert json.loads(opfile.read_text()) == {"days_back": 14}

    def test_delegated_dry_run_rejected(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {
            "ISTOTA_USER_ID": "alice",
            "ISTOTA_DEFERRED_DIR": str(deferred),
            "ISTOTA_TASK_ID": "99",
        }
        args = MagicMock(days_back=7, dry_run=True)
        code, out = self._run(args, env, monkeypatch)
        assert code == 1
        assert "dry-run is only available in direct mode" in json.loads(out)["error"]
        assert not (deferred / "task_99_garmin_import.json").exists()

    def test_no_task_context_errors(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        # No ISTOTA_DEFERRED_DIR / ISTOTA_TASK_ID → can't delegate.
        env = {"ISTOTA_USER_ID": "alice",
               "ISTOTA_DEFERRED_DIR": "", "ISTOTA_TASK_ID": ""}
        args = MagicMock(days_back=7, dry_run=False)
        code, out = self._run(args, env, monkeypatch)
        assert code == 1
        assert "web UI" in json.loads(out)["error"]
