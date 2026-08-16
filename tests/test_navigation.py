import asyncio
import math

from app.services.navigation import (
    OFF_ROUTE_THRESHOLD_M,
    NavRegistry,
    NavSession,
    _should_traffic_reroute,
    point_to_polyline_distance_m,
    remaining_progress,
)
from app.services.polyline import encode_polyline

ROUTE = [(-6.8048, 110.8385), (-6.8052, 110.8390), (-6.8100, 110.8500)]


def _hav(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = math.radians(b[0] - a[0])
    dl = math.radians(b[1] - a[1])
    s = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(s))


def test_point_to_polyline_distance_on_route_is_small():
    on_route = (-6.8052, 110.8390)
    dist = point_to_polyline_distance_m(*on_route, ROUTE)
    assert dist < 50.0


def test_point_to_polyline_distance_far_off_route():
    far = (-6.8200, 110.8600)
    dist = point_to_polyline_distance_m(*far, ROUTE)
    assert dist > OFF_ROUTE_THRESHOLD_M


def test_point_to_polyline_empty():
    assert point_to_polyline_distance_m(-6.8, 110.8, []) == float("inf")


def test_remaining_progress_decreases():
    session = NavSession(kurir_id=1)
    session.coords = ROUTE
    session.last_position = {"lat": ROUTE[0][0], "lon": ROUTE[0][1], "speed": 10}
    early = remaining_progress(session, ROUTE[0][0], ROUTE[0][1])
    late = remaining_progress(session, ROUTE[-1][0], ROUTE[-1][1])
    assert early is not None
    assert late is not None
    assert early["remaining_distance_m"] > late["remaining_distance_m"]
    assert early["progress_pct"] < late["progress_pct"]


def test_remaining_progress_none_for_short_polyline():
    session = NavSession(kurir_id=1)
    session.coords = [ROUTE[0]]
    assert remaining_progress(session, ROUTE[0][0], ROUTE[0][1]) is None


def test_should_traffic_reroute_min_saving():
    session = NavSession(kurir_id=1)
    session.coords = ROUTE
    session.last_position = {"lat": ROUTE[0][0], "lon": ROUTE[0][1], "speed": 10}
    prog = remaining_progress(session, ROUTE[0][0], ROUTE[0][1])
    cur = prog["remaining_time_s"]
    assert _should_traffic_reroute(session, cur - 200.0) is True
    assert _should_traffic_reroute(session, cur - 10.0) is False


def test_nav_registry_single_session_per_kurir():
    async def scenario():
        registry = NavRegistry()
        await registry.set(NavSession(kurir_id=7))
        assert await registry.get(7) is not None
        await registry.set(NavSession(kurir_id=7))
        assert len(await registry.all()) == 1
        await registry.remove(7)
        assert await registry.get(7) is None
        assert await registry.all() == []
    asyncio.run(scenario())


def test_route_response_route_id_default_none():
    from app.schemas.pathfinding import RouteResponse
    resp = RouteResponse(
        status="success", total_distance_meters=100.0,
        route_coordinates=ROUTE)
    assert resp.route_id is None


def test_route_id_preserved_in_python_dump():
    from app.schemas.pathfinding import OptimizedDeliveryRouteResponse
    from app.schemas.pathfinding import OptimizedDeliveryLeg
    resp = OptimizedDeliveryRouteResponse(
        status="success",
        total_distance_km=1.0,
        total_duration_mins=2.0,
        total_legs=1,
        stops=[],
        legs=[OptimizedDeliveryLeg(
            leg_index=0, stop_sequence_number=1, geometry=ROUTE,
            distance_km=1.0, duration_mins=2.0)],
        route_id=42,
    )
    assert resp.model_dump()["route_id"] == 42
    assert resp.model_dump(mode="json")["route_id"] == 42


def test_encode_polyline_compact_for_nav_payload():
    encoded = encode_polyline(ROUTE, 5)
    assert len(encoded) < 200
