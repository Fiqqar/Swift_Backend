"""Synthetic tests: multi-stop delivery optimization (TSP) + geocoding cache.

Run:  python -m pytest tests/test_delivery_optimizer.py -q
or:   python tests/test_delivery_optimizer.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.pathfinding.delivery_optimizer import (
    ensure_express_first,
    haversine_matrix,
    optimize_stop_order,
)


def _line(steps, start_lat=-6.9, start_lon=110.0, dlat=0.0, dlon=0.0002):
    return [(start_lat + i * dlat, start_lon + i * dlon) for i in range(steps)]


def test_haversine_matrix_symmetric():
    pts = _line(4)
    m = haversine_matrix(pts)
    assert len(m) == 4 and len(m[0]) == 4
    assert m[0][0] == 0.0
    for i in range(4):
        for j in range(4):
            assert m[i][j] == m[j][i]


def test_optimize_visits_all_stops():
    hub = (-6.9, 110.0)
    deliveries = _line(5, start_lon=110.001)
    order = optimize_stop_order(hub, deliveries)
    assert sorted(order) == [0, 1, 2, 3, 4]


def test_optimize_returns_shortest_order():
    hub = (-6.9, 110.0)
    # jalan memutar: stop 0 jauh di kiri, stop 1 dekat
    deliveries = [(-6.9, 109.995), (-6.9, 110.001)]
    order = optimize_stop_order(hub, deliveries)
    # nearest-neighbor dari hub: stop 1 (dekat) dulu, lalu stop 0
    assert order == [1, 0], order


def test_express_priority_pulls_express_first():
    hub = (-6.9, 110.0)
    # stop 1 REGULAR lebih dekat (0.0004), stop 0 EXPRESS sedikit lebih jauh
    # (0.0008). Diskon 0.5 membuat biaya "menuju" express lebih kecil
    # daripada biaya menuju regular -> EXPRESS diantar lebih dulu.
    deliveries = [(-6.9, 110.0008), (-6.9, 110.0004)]
    service = ["EXPRESS", "REGULAR"]
    order = optimize_stop_order(hub, deliveries, service_types=service,
                                express_discount=0.5)
    assert order[0] == 0, order
    assert order[1] == 1


def test_express_without_discount_stays_by_distance():
    hub = (-6.9, 110.0)
    deliveries = [(-6.9, 109.99), (-6.9, 110.0005)]
    service = ["EXPRESS", "REGULAR"]
    order = optimize_stop_order(hub, deliveries, service_types=service,
                                express_discount=1.0)
    assert order == [1, 0], order


def test_ensure_express_first_stable():
    order = [2, 0, 1]
    svc = ["REGULAR", "EXPRESS", "REGULAR"]
    out = ensure_express_first(order, svc)
    assert out == [1, 2, 0]


def test_return_to_hub_keeps_order():
    hub = (-6.9, 110.0)
    deliveries = [(-6.9, 110.001), (-6.9, 110.002)]
    order_close = optimize_stop_order(hub, deliveries, return_to_hub=False)
    order_open = optimize_stop_order(hub, deliveries, return_to_hub=True)
    # untuk 2 stop dekat hub, keduanya berurutan sama saja
    assert sorted(order_close) == [0, 1]
    assert sorted(order_open) == [0, 1]


async def _fake_redis():
    class FakeRedis:
        def __init__(self):
            self.store = {}

        async def get(self, key):
            return self.store.get(key)

        async def set(self, key, value, ex=None):
            self.store[key] = value

    return FakeRedis()


def test_geocode_cache_hit_and_miss():
    from app.services import geocode

    async def run():
        redis = await _fake_redis()

        calls = []
        original = geocode._nominatim

        async def fake_nominatim(alamat):
            calls.append(alamat)
            return (-6.8, 110.84)

        geocode._nominatim = fake_nominatim
        try:
            first = await geocode.geocode_address(redis, "Jl. Test Satu, Kudus")
            assert first == (-6.8, 110.84)
            assert len(calls) == 1
            second = await geocode.geocode_address(redis, "Jl. Test Satu, Kudus")
            assert second == (-6.8, 110.84)
            assert len(calls) == 1, "harus pakai cache Redis"
        finally:
            geocode._nominatim = original

    asyncio.run(run())


def test_geocode_none_on_empty_or_failure():
    from app.services import geocode

    async def run():
        redis = await _fake_redis()

        assert await geocode.geocode_address(redis, "  ") is None

        original = geocode._nominatim

        async def fail(alamat):
            return None

        geocode._nominatim = fail
        try:
            assert await geocode.geocode_address(redis, "Alamat tak dikenal xyz") is None
            assert await geocode.geocode_address(redis, "Alamat tak dikenal xyz") is None
        finally:
            geocode._nominatim = original

    asyncio.run(run())


def test_query_variants_fallback_order():
    from app.services import geocode

    variants = geocode._query_variants(
        "Jalan Sukun Raya No.09, Besito, Gebog, Kabupaten Kudus, Jawa Tengah 59333")
    assert variants[0].startswith("Jalan Sukun Raya")
    # nomor rumah dilepas
    assert any("No.09" not in v for v in variants[1:])
    # varian kecamatan+kota ada
    assert any("Gebog" in v and "Kabupaten Kudus" in v for v in variants)
    assert variants[-1].endswith("59333")


def test_geocode_falls_back_to_coarser_query():
    from app.services import geocode

    async def run():
        redis = await _fake_redis()

        calls = []

        async def fake_nominatim(alamat):
            calls.append(alamat)
            if "Jl. Sunan Kudus No.34" in alamat:
                return None
            return (-6.798, 110.834)

        original = geocode._nominatim
        geocode._nominatim = fake_nominatim
        try:
            result = await geocode.geocode_address(
                redis, "Jl. Sunan Kudus No.34, Demaan, Kota Kudus, Jawa Tengah 59313")
            assert result == (-6.798, 110.834)
            assert len(calls) > 1, "harus mencoba query yang lebih umum"
            first_calls = len(calls)
            # hasil di-cache supaya request berikutnya langsung
            assert await geocode.geocode_address(
                redis, "Jl. Sunan Kudus No.34, Demaan, Kota Kudus, Jawa Tengah 59313") \
                == (-6.798, 110.834)
            assert len(calls) == first_calls, "tidak memanggil Nominatim lagi"
        finally:
            geocode._nominatim = original

    asyncio.run(run())


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
