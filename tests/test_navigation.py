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
from app.services.pathfinding.maneuvers import (
    extract_steps,
    find_current_step,
    get_next_maneuver,
    get_traffic_level,
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


# --- advance_leg (multi-leg POD advance) ---

def _multi_nav(route_id=1):
    from app.services.polyline import decode_polyline

    ROUTE2 = [(-6.8100, 110.8500), (-6.8150, 110.8550), (-6.8200, 110.8600)]
    return {
        "route_id": route_id,
        "kind": "multi",
        "legs": [
            {
                "index": 0,
                "package_id": 1,
                "recipient_name": "Budi",
                "encoded": encode_polyline(ROUTE, 5),
                "dest": ROUTE[-1],
            },
            {
                "index": 1,
                "package_id": 2,
                "recipient_name": "Siti",
                "encoded": encode_polyline(ROUTE2, 5),
                "dest": ROUTE2[-1],
            },
        ],
    }


def test_advance_leg_moves_to_next_leg():
    from app.services.navigation import advance_leg
    from app.services.polyline import decode_polyline

    nav = _multi_nav()
    session = NavSession(kurir_id=1)
    session.route_id = 1
    session.kind = "multi"
    session.leg_index = 0
    session.coords = decode_polyline(nav["legs"][0]["encoded"])
    session.dest = nav["legs"][0]["dest"]

    result = advance_leg(session, nav)
    assert result is not None
    assert result["leg_index"] == 1
    assert result["package_id"] == 2
    assert session.leg_index == 1
    assert session.dest == tuple(nav["legs"][1]["dest"])
    assert session.coords == decode_polyline(nav["legs"][1]["encoded"])
    assert session.off_route_active is False
    assert session.cooldown_until == 0.0


def test_advance_leg_last_leg_returns_done():
    from app.services.navigation import advance_leg

    nav = _multi_nav()
    session = NavSession(kurir_id=1)
    session.route_id = 1
    session.kind = "multi"
    session.leg_index = 1
    result = advance_leg(session, nav)
    assert result == {"done": True}


def test_advance_leg_single_kind_returns_done():
    from app.services.navigation import advance_leg

    nav = _multi_nav()
    nav["kind"] = "single"
    session = NavSession(kurir_id=1)
    session.route_id = 1
    session.kind = "single"
    session.leg_index = 0
    result = advance_leg(session, nav)
    assert result == {"done": True}


def test_advance_leg_route_id_mismatch_returns_none():
    from app.services.navigation import advance_leg

    session = NavSession(kurir_id=1)
    session.route_id = 999
    session.kind = "multi"
    assert advance_leg(session, _multi_nav(route_id=1)) is None


def test_advance_leg_no_legs_returns_none():
    from app.services.navigation import advance_leg

    session = NavSession(kurir_id=1)
    session.route_id = 1
    session.kind = "multi"
    assert advance_leg(session, {"route_id": 1, "kind": "multi",
                                 "legs": []}) is None


# --- Maneuver extraction tests ---

def _simple_route():
    """Create a simple L-shaped route for testing."""
    return [
        (-6.8048, 110.8385),  # Start
        (-6.8050, 110.8390),  # Straight
        (-6.8052, 110.8395),  # Turn right
        (-6.8055, 110.8398),  # Straight
        (-6.8060, 110.8400),  # Turn left
        (-6.8065, 110.8405),  # End
    ]


def _node_sequence_for_route(route_coords):
    """Generate a simple node sequence for the route."""
    return list(range(len(route_coords)))


def test_extract_steps_straight_route():
    """Test maneuver extraction for a straight route."""
    route_coords = [(-6.8048, 110.8385), (-6.8050, 110.8390), (-6.8052, 110.8395)]
    node_seq = [0, 1, 2]
    edge_classes = {}
    edge_names = {}

    steps = extract_steps(node_seq, route_coords, edge_classes, edge_names)

    assert len(steps) == 2
    # Last step should be arrive
    assert steps[-1]["instruction"]["type"] == "arrive"
    assert steps[-1]["instruction"]["instruction"] == "Anda telah tiba di tujuan"


def test_extract_steps_with_turns():
    """Test maneuver extraction detects turns."""
    route_coords = _simple_route()
    node_seq = _node_sequence_for_route(route_coords)

    # Create edge classes and names for each segment
    edge_classes = {
        1: "residential",   # 0->1
        3: "residential",   # 1->2
        6: "residential",   # 2->3
        10: "residential",  # 3->4
        15: "residential",  # 4->5
    }
    edge_names = {
        1: "Jl. A",
        3: "Jl. B",
        6: "Jl. B",
        10: "Jl. C",
        15: "Jl. C",
    }

    steps = extract_steps(node_seq, route_coords, edge_classes, edge_names)

    assert len(steps) >= 3
    # Check that we have turn instructions (not just continue/arrive)
    turn_types = [s["instruction"]["type"] for s in steps[:-1]]
    assert any(t in ("turn_left", "turn_right", "turn_slight_left", "turn_slight_right") for t in turn_types)


def test_extract_steps_empty_inputs():
    """Test extract_steps handles empty inputs."""
    assert extract_steps([], [], {}, {}) == []
    assert extract_steps([0], [(-6.8, 110.8)], {}, {}) == []
    assert extract_steps([0, 1], [], {}, {}) == []


def test_find_current_step():
    """Test finding current step based on traveled distance."""
    route_coords = _simple_route()
    node_seq = _node_sequence_for_route(route_coords)
    edge_classes = {1: "residential", 3: "residential"}
    edge_names = {1: "Jl. A", 3: "Jl. A"}

    steps = extract_steps(node_seq, route_coords, edge_classes, edge_names)
    total_dist = sum(s["distance_m"] for s in steps)

    # At start
    idx = find_current_step(steps, route_coords[0][0], route_coords[0][1], 0)
    assert idx == 0

    # At middle
    idx = find_current_step(steps, route_coords[2][0], route_coords[2][1], total_dist / 2)
    assert idx >= 0

    # At end
    idx = find_current_step(steps, route_coords[-1][0], route_coords[-1][1], total_dist)
    assert idx == len(steps) - 1


def test_get_next_maneuver():
    """Test getting next maneuver."""
    route_coords = _simple_route()
    node_seq = _node_sequence_for_route(route_coords)
    edge_classes = {1: "residential", 3: "residential"}
    edge_names = {1: "Jl. A", 3: "Jl. A"}

    steps = extract_steps(node_seq, route_coords, edge_classes, edge_names)

    # First step
    next_m = get_next_maneuver(steps, 0)
    assert next_m is not None
    assert "distance_m" in next_m

    # Last step
    next_m = get_next_maneuver(steps, len(steps) - 1)
    assert next_m is None


def test_get_traffic_level():
    """Test traffic level classification."""
    # Free flow
    assert get_traffic_level({}, 1) == "free"
    assert get_traffic_level({1: 1.0}, 1) == "free"
    assert get_traffic_level({1: 1.2}, 1) == "free"

    # Moderate
    assert get_traffic_level({1: 1.5}, 1) == "moderate"
    assert get_traffic_level({1: 2.0}, 1) == "moderate"

    # Heavy
    assert get_traffic_level({1: 2.5}, 1) == "heavy"
    assert get_traffic_level({1: 3.0}, 1) == "heavy"
    assert get_traffic_level({1: float("inf")}, 1) == "heavy"


def test_remaining_progress_new_fields():
    """Test remaining_progress returns new fields."""
    session = NavSession(kurir_id=1)
    session.coords = _simple_route()
    session.last_position = {"lat": _simple_route()[0][0], "lon": _simple_route()[0][1], "speed": 30}

    prog = remaining_progress(session, _simple_route()[0][0], _simple_route()[0][1])

    assert prog is not None
    assert "current_speed_kmh" in prog
    assert "average_speed_kmh" in prog
    assert "eta_timestamp" in prog
    assert "traffic_level" in prog
    assert prog["current_speed_kmh"] == 30.0
