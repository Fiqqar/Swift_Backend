"""Synthetic tests: tracking posisi kurir (Redis) + geofence state machine.

Run:  python -m pytest tests/test_courier_position.py -q
or:   python tests/test_courier_position.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.tracking import (
    geofence_event,
    geofence_key,
    pos_key,
    resolve_start_point,
    stops_key,
)


class FakeRedis:
    """Fake Redis async minimal untuk unit test (hash, string, expire)."""

    def __init__(self):
        self.hash_store = {}
        self.str_store = {}
        self.expire_ttl = {}

    async def hset(self, key, mapping=None):
        store = self.hash_store.setdefault(key, {})
        for k, v in (mapping or {}).items():
            store[k] = v

    async def hgetall(self, key):
        return dict(self.hash_store.get(key, {}))

    async def expire(self, key, ttl):
        self.expire_ttl[key] = ttl

    async def get(self, key):
        return self.str_store.get(key)

    async def set(self, key, value, ex=None):
        self.str_store[key] = value
        if ex is not None:
            self.expire_ttl[key] = ex

    async def delete(self, key):
        self.str_store.pop(key, None)
        self.hash_store.pop(key, None)


def _run(coro):
    return asyncio.run(coro)


def test_pos_key_format():
    assert pos_key(7) == "driver:pos:7"


def test_geofence_key_format():
    assert geofence_key(7, 12) == "driver:geofence:7:12"


def test_stops_key_format():
    assert stops_key(7) == "driver:stops:7"


def test_set_and_get_kurir_position():
    from app.services.tracking import get_kurir_position, set_kurir_position

    async def run():
        redis = FakeRedis()
        await set_kurir_position(redis, 7, -6.2, 106.8166, bearing=132.5,
                                 speed=28.4, snapped=(-6.20005, 106.81671))
        pos = await get_kurir_position(redis, 7)
        assert pos["lat"] == -6.2
        assert pos["lon"] == 106.8166
        assert pos["bearing"] == 132.5
        assert pos["speed"] == 28.4
        assert pos["snapped"] == "-6.20005,106.81671"
        assert redis.expire_ttl[pos_key(7)] > 0

    _run(run())


def test_get_kurir_position_none_when_empty():
    from app.services.tracking import get_kurir_position

    async def run():
        assert await get_kurir_position(FakeRedis(), 7) is None
        assert await get_kurir_position(None, 7) is None

    _run(run())


def test_get_kurir_position_latlon():
    from app.services.tracking import get_kurir_position_latlon, set_kurir_position

    async def run():
        redis = FakeRedis()
        await set_kurir_position(redis, 7, -6.2, 106.8166)
        assert await get_kurir_position_latlon(redis, 7) == (-6.2, 106.8166)
        assert await get_kurir_position_latlon(FakeRedis(), 99) is None

    _run(run())


def test_geofence_state_roundtrip():
    from app.services.tracking import (
        get_geofence_state,
        set_geofence_state,
    )

    async def run():
        redis = FakeRedis()
        assert await get_geofence_state(redis, 7, 12) is None
        await set_geofence_state(redis, 7, 12, "inside")
        assert await get_geofence_state(redis, 7, 12) == "inside"
        assert redis.expire_ttl[geofence_key(7, 12)] > 0

    _run(run())


def test_geofence_enter_only_on_transition():
    # di luar -> masuk radius: geofence_enter
    ev = geofence_event("outside", True, 12, 22.4, 30)
    assert ev == {"type": "geofence_enter", "package_id": 12,
                  "distance_m": 22.4, "radius_m": 30}
    # sudah inside, tetap inside: tidak ada event (anti-spam)
    assert geofence_event("inside", True, 12, 22.4, 30) is None
    # belum pernah ada state, masuk radius: enter (anggap cold start)
    assert geofence_event(None, True, 12, 22.4, 30)["type"] == "geofence_enter"


def test_geofence_exit_only_when_was_inside():
    assert geofence_event("inside", False, 12, 45.1, 30) == {
        "type": "geofence_exit", "package_id": 12,
        "distance_m": 45.1, "radius_m": 30}
    # sudah di luar, tetap di luar: tidak ada event
    assert geofence_event("outside", False, 12, 45.1, 30) is None
    # cold start di luar radius: tidak ada event (hindari spam awal)
    assert geofence_event(None, False, 12, 45.1, 30) is None


def test_geofence_event_rounds_distance():
    ev = geofence_event("outside", True, 5, 22.4567, 30)
    assert ev["distance_m"] == 22.46


def test_resolve_start_point_priority_redis():
    start = resolve_start_point((-6.2, 106.8), (-6.3, 106.9), (-6.7, 110.8))
    assert start == (-6.3, 106.9)


def test_resolve_start_point_courier_when_redis_empty():
    start = resolve_start_point((-6.2, 106.8), None, (-6.7, 110.8))
    assert start == (-6.2, 106.8)


def test_resolve_start_point_fallback_hub():
    start = resolve_start_point(None, None, (-6.7, 110.8))
    assert start == (-6.7, 110.8)


def test_resolve_start_point_all_none():
    assert resolve_start_point(None, None, None) is None


def test_del_geofence_state():
    from app.services.tracking import (
        del_geofence_state,
        get_geofence_state,
        set_geofence_state,
    )

    async def run():
        redis = FakeRedis()
        await set_geofence_state(redis, 7, 12, "inside")
        await del_geofence_state(redis, 7, 12)
        assert await get_geofence_state(redis, 7, 12) is None

    _run(run())


def test_stops_cache_roundtrip():
    from app.services.tracking import (
        get_stops_cache,
        set_stops_cache,
    )

    async def run():
        redis = FakeRedis()
        stops = [{"package_id": 1, "lat": -6.2, "lon": 106.8}]
        await set_stops_cache(redis, 7, stops)
        assert await get_stops_cache(redis, 7) == stops

    _run(run())


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failures += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print("%d/%d passed" % (len(tests) - failures, len(tests)))
    sys.exit(1 if failures else 0)
