"""Test: event stops_reordered harus mengirim distance/duration NYATA.

Regresi utk bug legs_with_geometry yang meng-hardcode duration_mins=0 dan
membaca field distance_km/duration_mins yang tidak pernah ada di snapshot
Redis (snapshot hanya punya encoded/dest/eta_s).

Juga mencakup unit test _resolve_speed_kmh (prioritas GPS > per-mode > global).

Run:  python tests/test_reorder_legs_metrics.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.navigation import (
    NavSession,
    _resolve_speed_kmh,
    maybe_reorder_stops_on_off_route,
)
from app.services.polyline import encode_polyline


class MockRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True


class MockState:
    def __init__(self, redis):
        self.redis = redis


class MockApp:
    def __init__(self, redis):
        self.state = MockState(redis)


class MockWS:
    """Menangkap semua pesan yang dikirim server ke klien."""

    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _make_session(kurir_id=1):
    s = NavSession(kurir_id=kurir_id)
    s.kind = "multi"
    s.leg_index = 0
    s.total_legs = 3
    s.route_id = 99
    s.mode = "motorcycle"
    s.last_mile = True
    # Active leg polyline: hub -> dekat stop A (agar off-route jelas)
    s.coords = [(-6.90, 110.000), (-6.90, 110.001)]
    s.dest = (-6.901, 110.002)          # stop A (aktif)
    s.current_package_id = 101
    s.current_recipient = "Stop A"
    return s


def _make_nav_snapshot():
    """Snapshot Redis persis seperti struktur asli endpoint delivery:
    legs hanya berisi {index, stop_sequence_number, package_id,
    recipient_name, encoded, dest, eta_s} — TANPA distance_km/duration_mins."""
    coords_a = [(-6.901, 110.002), (-6.9015, 110.0025)]   # leg aktif (A)
    coords_b = [(-6.898, 110.002), (-6.8975, 110.0025)]   # leg ke B
    coords_c = [(-6.8975, 110.0025), (-6.897, 110.003)]   # leg ke C
    return {
        "route_id": 99,
        "kind": "multi",
        "mode": "motorcycle",
        "last_mile": True,
        "total_distance_m": 3000.0,
        "total_eta_s": 400.0,
        "legs": [
            {"index": 0, "stop_sequence_number": 1, "package_id": 101,
             "recipient_name": "Stop A", "encoded": encode_polyline(coords_a, 5),
             "dest": list(coords_a[-1]), "eta_s": 120.0},
            {"index": 1, "stop_sequence_number": 2, "package_id": 102,
             "recipient_name": "Stop B", "encoded": encode_polyline(coords_b, 5),
             "dest": list(coords_b[-1]), "eta_s": 180.0},
            {"index": 2, "stop_sequence_number": 3, "package_id": 103,
             "recipient_name": "Stop C", "encoded": encode_polyline(coords_c, 5),
             "dest": list(coords_c[-1]), "eta_s": 240.0},
        ],
    }


async def test_reordered_legs_have_real_metrics():
    session = _make_session()
    redis = MockRedis()
    # get_nav_route melakukan json.loads(raw) — snapshot harus berupa STRING JSON.
    redis.data[f"driver:nav:{session.kurir_id}"] = json.dumps(_make_nav_snapshot())
    app = MockApp(redis)
    ws = MockWS()
    session.ws = ws

    # Klik tes dekat Stop B (EXPRESS di antara sisa stop) — major switch.
    reordered = await maybe_reorder_stops_on_off_route(
        app, redis, session, -6.8978, 110.0024, 150.0, test_mode=True)

    assert reordered is True, "reorder harus terpicu di test mode"

    events = [m for m in ws.sent if m.get("type") == "stops_reordered"]
    assert events, "event stops_reordered harus terkirim"

    legs = events[0]["legs"]
    assert len(legs) >= 2, f"minimal 2 legs, dapat {len(legs)}"

    for i, leg in enumerate(legs[1:], start=1):   # leg pertama boleh estimasi
        assert leg["duration_mins"] > 0, (
            f"legs[{i}].duration_mins masih 0 — regresi hardcode!")
        assert leg["distance_km"] > 0, (
            f"legs[{i}].distance_km masih 0 — snapshot eta_s/geometry tak dipakai!")
        assert leg.get("estimated_time_seconds") is not None, (
            f"legs[{i}].estimated_time_seconds hilang")

    # eta_s snapshot leg B/C = 180s/240s -> durasi ~3.0 / ~4.0 menit
    b, c = legs[1], legs[2]
    assert abs(b["duration_mins"] - 3.0) < 0.2, f"dur B {b['duration_mins']}"
    assert abs(c["duration_mins"] - 4.0) < 0.2, f"dur C {c['duration_mins']}"

    print("PASS: test_reordered_legs_have_real_metrics")


_SPEED_ENV_KEYS = (
    "SPEED_KMH_MOTORCYCLE", "SPEED_KMH_CAR", "SPEED_KMH_TRUCK",
    "DEFAULT_SPEED_KMH", "MODE_AVG_SPEED_KMH",
)


def _with_env(overrides: dict):
    """Decorator-ish context: set env keys, kembalikan restore fn."""
    saved = {k: os.environ.get(k) for k in _SPEED_ENV_KEYS}
    for k, v in overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)

    def _restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return _restore


def _session_with_speed(speed, mode="motorcycle"):
    s = NavSession(kurir_id=999)
    s.mode = mode
    if speed is not None:
        s.last_position = {"lat": -6.9, "lon": 110.0, "speed": speed}
    else:
        s.last_position = {"lat": -6.9, "lon": 110.0}
    return s


def test_resolver_gps_priority():
    """GPS realtime > 0 harus menang atas default per-mode/global."""
    s = _session_with_speed(25.0)
    restore = _with_env({"SPEED_KMH_MOTORCYCLE": 35, "DEFAULT_SPEED_KMH": 40})
    try:
        assert _resolve_speed_kmh(s) == 25.0
    finally:
        restore()
    print("PASS: test_resolver_gps_priority")


def test_resolver_invalid_gps_falls_to_mode_default():
    """GPS invalid (string/<=0) -> default per-mode."""
    restore = _with_env({"SPEED_KMH_MOTORCYCLE": 35})
    try:
        for bad in ("abc", 0, -5):
            s = _session_with_speed(bad)
            assert _resolve_speed_kmh(s) == 35.0, f"speed={bad!r}"
    finally:
        restore()
    print("PASS: test_resolver_invalid_gps_falls_to_mode_default")


def test_resolver_no_position_per_mode():
    """Tanpa last_position.speed -> default per-mode (env-overridable)."""
    restore = _with_env({
        "SPEED_KMH_MOTORCYCLE": None,
        "SPEED_KMH_CAR": None,
        "SPEED_KMH_TRUCK": None,
    })
    try:
        assert _resolve_speed_kmh(_session_with_speed(None, "motorcycle")) == 35.0
        assert _resolve_speed_kmh(_session_with_speed(None, "car")) == 40.0
        assert _resolve_speed_kmh(_session_with_speed(None, "truck")) == 30.0

        # Env per-mode menimpa literal default
        os.environ["SPEED_KMH_MOTORCYCLE"] = "45"
        assert _resolve_speed_kmh(_session_with_speed(None, "motorcycle")) == 45.0
    finally:
        restore()
    print("PASS: test_resolver_no_position_per_mode")


def test_resolver_unknown_mode_global_default():
    """Mode tak dikenal -> DEFAULT_SPEED_KMH env -> konstanta 40."""
    restore = _with_env({"DEFAULT_SPEED_KMH": None})
    try:
        s = _session_with_speed(None)
        s.mode = "rocket"
        assert _resolve_speed_kmh(s) == 40.0

        os.environ["DEFAULT_SPEED_KMH"] = "50"
        assert _resolve_speed_kmh(s) == 50.0
    finally:
        restore()
    print("PASS: test_resolver_unknown_mode_global_default")


async def test_reordered_legs_first_leg_follows_gps_speed():
    """Leg estimasi (index 0) mengikuti GPS speed kurir, bukan env global."""
    session = _make_session()
    session.last_position = {"lat": -6.90, "lon": 110.000, "speed": 20.0}
    redis = MockRedis()
    redis.data[f"driver:nav:{session.kurir_id}"] = json.dumps(_make_nav_snapshot())
    app = MockApp(redis)
    ws = MockWS()
    session.ws = ws

    ok = await maybe_reorder_stops_on_off_route(
        app, redis, session, -6.8978, 110.0024, 150.0, test_mode=True)
    assert ok is True

    legs = [m for m in ws.sent if m.get("type") == "stops_reordered"][0]["legs"]

    from app.services.pathfinding.core_a_star import haversine_distance
    dist_m = sum(haversine_distance(session.coords[i], session.coords[i + 1])
                 for i in range(len(session.coords) - 1))
    expected_min = round(dist_m / (20.0 / 3.6) / 60.0, 1)
    got = legs[0]["duration_mins"]
    assert abs(got - expected_min) < 0.05, (
        f"leg0 dur {got} != expected@GPS20km/h {expected_min}")
    print("PASS: test_reordered_legs_first_leg_follows_gps_speed")


async def main():
    test_resolver_gps_priority()
    test_resolver_invalid_gps_falls_to_mode_default()
    test_resolver_no_position_per_mode()
    test_resolver_unknown_mode_global_default()
    await test_reordered_legs_have_real_metrics()
    await test_reordered_legs_first_leg_follows_gps_speed()


if __name__ == "__main__":
    asyncio.run(main())
