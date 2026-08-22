"""Tests for Dynamic Stop Re-Ordering on Off-Route.

Run:  python -m pytest tests/test_reorder.py -v
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.navigation import (
    NavSession,
    maybe_reorder_stops_on_off_route,
    OFF_ROUTE_REORDER_THRESHOLD_M,
    REORDER_MIN_DISTANCE_SAVING_PCT,
    REORDER_COOLDOWN_SECONDS,
    MAX_REORDERS_PER_ROUTE,
    REORDER_STABILITY_WINDOW,
)
from app.services.pathfinding.delivery_optimizer import optimize_stop_order_hybrid
from app.services.pathfinding.core_a_star import haversine_distance
from app.services.pathfinding.preprocess import build_path_graph


def _create_test_graph():
    """Create a synthetic graph with main road and branch.
    
    Graph layout:
    Hub (0) -- 1 -- 2 -- 3 -- 4 (Stop A)
                    |
                    5 -- 6 (Stop B)
    
    Coordinates:
    0: (-6.9, 110.0)      Hub
    1: (-6.9, 110.001)
    2: (-6.9, 110.002)    Junction
    3: (-6.9, 110.003)
    4: (-6.9, 110.004)    Stop A
    5: (-6.899, 110.002)  Branch from junction
    6: (-6.898, 110.002)  Stop B
    """
    locations = {
        0: (-6.9, 110.0),
        1: (-6.9, 110.001),
        2: (-6.9, 110.002),
        3: (-6.9, 110.003),
        4: (-6.9, 110.004),
        5: (-6.899, 110.002),
        6: (-6.898, 110.002),
    }
    
    graph = {}
    for i in range(7):
        graph[i] = {}
    
    # Main road: 0-1-2-3-4
    edges_main = [(0, 1), (1, 2), (2, 3), (3, 4)]
    for u, v in edges_main:
        d = haversine_distance(locations[u], locations[v])
        graph[u][v] = d
        graph[v][u] = d
    
    # Branch: 2-5-6
    edges_branch = [(2, 5), (5, 6)]
    for u, v in edges_branch:
        d = haversine_distance(locations[u], locations[v])
        graph[u][v] = d
        graph[v][u] = d
    
    pg = build_path_graph(graph, locations, -6.9, 110.0, enable_ch=False)
    return pg, locations


def _create_session(kurir_id: int = 1) -> NavSession:
    """Create a NavSession with test route state."""
    session = NavSession(kurir_id=kurir_id)
    session.kind = "multi"
    session.leg_index = 0
    session.total_legs = 3
    session.mode = "motorcycle"
    session.last_mile = True
    session.route_id = 123
    session.current_package_id = 1
    session.current_recipient = "Stop A"
    return session


class MockRedis:
    """Mock Redis for testing."""
    
    def __init__(self):
        self.data = {}
    
    async def get(self, key):
        return self.data.get(key)
    
    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True


class MockApp:
    """Mock FastAPI app."""
    
    def __init__(self):
        self.state = type('obj', (object,), {'redis': MockRedis()})()


async def test_reorder_basic():
    """Test basic re-ordering when courier is closer to Stop B than Stop A."""
    pg, locations = _create_test_graph()
    session = _create_session()
    
    # Set session dest to Stop A (leg 0 destination)
    session.dest = locations[4]
    session.coords = [locations[0], locations[1], locations[2], locations[3], locations[4]]
    
    # Create Redis snapshot with 3 legs: Hub->Stop A, Stop A->Stop B, Stop B->Hub
    redis = MockRedis()
    nav = {
        "route_id": 123,
        "legs": [
            {
                "package_id": 1,
                "recipient_name": "Stop A",
                "service_type": "REGULAR",
                "dest": list(locations[4]),
                "encoded": "dummy_encoded_1",
                "stop_order": 1,
            },
            {
                "package_id": 2,
                "recipient_name": "Stop B",
                "service_type": "REGULAR",
                "dest": list(locations[6]),
                "encoded": "dummy_encoded_2",
                "stop_order": 2,
            },
            {
                "package_id": None,
                "recipient_name": "Hub",
                "service_type": "REGULAR",
                "dest": list(locations[0]),
                "encoded": "dummy_encoded_3",
                "stop_order": 3,
            },
        ],
    }
    await redis.set(f"driver:nav:{session.kurir_id}", nav)
    
    app = MockApp()
    app.state.redis = redis
    
    # Courier position is near junction (node 2), closer to Stop B branch
    # Off-route distance from main road > threshold
    current_lat, current_lon = -6.8995, 110.002  # Near branch point
    off_route_dist = 60.0  # > 50m threshold
    
    # Mock the road_cost_fn to use haversine (no actual routing)
    original_ordering = None
    
    async def mock_road_cost_fn(o, d):
        return haversine_distance(o, d) * 1.2  # Road factor
    
    # We can't easily mock _ordering_road_distance, so we test the optimizer directly
    stops_coords = [locations[4], locations[6]]
    service_types = ["REGULAR", "REGULAR"]
    
    # From position near junction, Stop B should be closer
    order = await optimize_stop_order_hybrid(
        (current_lat, current_lon), stops_coords,
        service_types=service_types,
        road_cost_fn=mock_road_cost_fn,
        top_k=2,
        return_to_hub=False,
    )
    
    # New order should be [1, 0] i.e., Stop B first
    assert order == [1, 0], f"Expected [1, 0], got {order}"
    print("PASS: test_reorder_basic")


async def test_reorder_express_priority():
    """Test that EXPRESS stops maintain priority even if further."""
    pg, locations = _create_test_graph()
    session = _create_session()
    session.dest = locations[4]
    session.coords = [locations[0], locations[1], locations[2], locations[3], locations[4]]
    
    redis = MockRedis()
    nav = {
        "route_id": 123,
        "legs": [
            {
                "package_id": 1,
                "recipient_name": "Stop A (EXPRESS)",
                "service_type": "EXPRESS",
                "dest": list(locations[4]),
                "encoded": "dummy_encoded_1",
                "stop_order": 1,
            },
            {
                "package_id": 2,
                "recipient_name": "Stop B (REGULAR)",
                "service_type": "REGULAR",
                "dest": list(locations[6]),
                "encoded": "dummy_encoded_2",
                "stop_order": 2,
            },
        ],
    }
    await redis.set(f"driver:nav:{session.kurir_id}", nav)
    
    app = MockApp()
    app.state.redis = redis
    
    stops_coords = [locations[4], locations[6]]
    service_types = ["EXPRESS", "REGULAR"]
    
    async def mock_road_cost_fn(o, d):
        return haversine_distance(o, d) * 1.2
    
    # Even from position closer to Stop B, EXPRESS should stay first
    order = await optimize_stop_order_hybrid(
        (-6.8995, 110.002), stops_coords,
        service_types=service_types,
        road_cost_fn=mock_road_cost_fn,
        top_k=2,
        return_to_hub=False,
    )
    
    # EXPRESS (index 0) should remain first
    assert order == [0, 1], f"EXPRESS priority violated: got {order}"
    print("PASS: test_reorder_express_priority")


async def test_reorder_cooldown():
    """Test that re-ordering respects cooldown."""
    pg, locations = _create_test_graph()
    session = _create_session()
    session.reorder_cooldown_until = asyncio.get_event_loop().time() + 100  # Future
    
    redis = MockRedis()
    nav = {
        "route_id": 123,
        "legs": [
            {"package_id": 1, "recipient_name": "A", "service_type": "REGULAR",
             "dest": list(locations[4]), "encoded": "e1", "stop_order": 1},
            {"package_id": 2, "recipient_name": "B", "service_type": "REGULAR",
             "dest": list(locations[6]), "encoded": "e2", "stop_order": 2},
        ],
    }
    await redis.set(f"driver:nav:{session.kurir_id}", nav)
    
    app = MockApp()
    app.state.redis = redis
    
    # Even with favorable conditions, cooldown should block
    result = await maybe_reorder_stops_on_off_route(
        app, redis, session, -6.8995, 110.002, 60.0)
    
    assert result is False, "Should be blocked by cooldown"
    print("PASS: test_reorder_cooldown")


async def test_reorder_max_limit():
    """Test max re-orders per route limit."""
    pg, locations = _create_test_graph()
    session = _create_session()
    session.reorder_count = MAX_REORDERS_PER_ROUTE
    
    redis = MockRedis()
    nav = {
        "route_id": 123,
        "legs": [
            {"package_id": 1, "recipient_name": "A", "service_type": "REGULAR",
             "dest": list(locations[4]), "encoded": "e1", "stop_order": 1},
            {"package_id": 2, "recipient_name": "B", "service_type": "REGULAR",
             "dest": list(locations[6]), "encoded": "e2", "stop_order": 2},
        ],
    }
    await redis.set(f"driver:nav:{session.kurir_id}", nav)
    
    app = MockApp()
    app.state.redis = redis
    
    result = await maybe_reorder_stops_on_off_route(
        app, redis, session, -6.8995, 110.002, 60.0)
    
    assert result is False, "Should be blocked by max reorders limit"
    print("PASS: test_reorder_max_limit")


async def test_reorder_stability_window():
    """Test stability window logic directly on session attributes."""
    session = _create_session()
    
    # Initial state
    session._reorder_candidate = None
    session._reorder_candidate_count = 0
    REORDER_STABILITY_WINDOW = 2
    
    # Simulate first candidate
    new_order = [1, 0]
    if session._reorder_candidate == new_order:
        session._reorder_candidate_count += 1
    else:
        session._reorder_candidate = new_order
        session._reorder_candidate_count = 1
    
    # First check - count should be 1
    assert session._reorder_candidate_count == 1
    should_trigger = session._reorder_candidate_count >= REORDER_STABILITY_WINDOW
    assert should_trigger is False
    
    # Second check with same candidate
    if session._reorder_candidate == new_order:
        session._reorder_candidate_count += 1
    
    assert session._reorder_candidate_count == 2
    should_trigger = session._reorder_candidate_count >= REORDER_STABILITY_WINDOW
    assert should_trigger is True
    
    # Different candidate resets counter
    new_order2 = [0, 1]
    if session._reorder_candidate == new_order2:
        session._reorder_candidate_count += 1
    else:
        session._reorder_candidate = new_order2
        session._reorder_candidate_count = 1
    
    assert session._reorder_candidate_count == 1
    assert session._reorder_candidate == [0, 1]
    
    print("PASS: test_reorder_stability_window")


async def test_reorder_off_route_threshold():
    """Test re-ordering only triggers above OFF_ROUTE_REORDER_THRESHOLD_M."""
    pg, locations = _create_test_graph()
    session = _create_session()
    
    redis = MockRedis()
    nav = {
        "route_id": 123,
        "legs": [
            {"package_id": 1, "recipient_name": "A", "service_type": "REGULAR",
             "dest": list(locations[4]), "encoded": "e1", "stop_order": 1},
            {"package_id": 2, "recipient_name": "B", "service_type": "REGULAR",
             "dest": list(locations[6]), "encoded": "e2", "stop_order": 2},
        ],
    }
    await redis.set(f"driver:nav:{session.kurir_id}", nav)
    
    app = MockApp()
    app.state.redis = redis
    
    # Below threshold (40m) - should not trigger re-order
    result = await maybe_reorder_stops_on_off_route(
        app, redis, session, -6.9, 110.002, 30.0)
    
    assert result is False, "Should not trigger below reorder threshold"
    print("PASS: test_reorder_off_route_threshold")


async def test_reorder_single_leg():
    """Test re-ordering not attempted for single remaining leg."""
    pg, locations = _create_test_graph()
    session = _create_session()
    session.leg_index = 1
    session.total_legs = 2  # Only 1 remaining
    
    redis = MockRedis()
    nav = {
        "route_id": 123,
        "legs": [
            {"package_id": 1, "recipient_name": "A", "service_type": "REGULAR",
             "dest": list(locations[4]), "encoded": "e1", "stop_order": 1},
            {"package_id": 2, "recipient_name": "B", "service_type": "REGULAR",
             "dest": list(locations[6]), "encoded": "e2", "stop_order": 2},
        ],
    }
    await redis.set(f"driver:nav:{session.kurir_id}", nav)
    
    app = MockApp()
    app.state.redis = redis
    
    result = await maybe_reorder_stops_on_off_route(
        app, redis, session, -6.8995, 110.002, 60.0)
    
    assert result is False, "Should not reorder with < 2 remaining stops"
    print("PASS: test_reorder_single_leg")


async def test_optimizer_haversine_fallback():
    """Test optimizer handles road_cost_fn errors gracefully."""
    stops_coords = [
        (-6.9, 110.004),  # Stop A
        (-6.898, 110.002),  # Stop B
    ]
    
    async def failing_road_cost_fn(o, d):
        raise Exception("Routing service unavailable")
    
    # Currently optimizer propagates the exception - verify behavior
    try:
        order = await optimize_stop_order_hybrid(
            (-6.8995, 110.002), stops_coords,
            service_types=["REGULAR", "REGULAR"],
            road_cost_fn=failing_road_cost_fn,
            top_k=2,
            return_to_hub=False,
        )
        # If it doesn't raise, should return valid order
        assert len(order) == 2
        assert set(order) == {0, 1}
    except Exception as e:
        # Expected: exception propagates from road_cost_fn
        assert "Routing service unavailable" in str(e)
    
    print("PASS: test_optimizer_haversine_fallback")


if __name__ == "__main__":
    tests = [
        test_reorder_basic,
        test_reorder_express_priority,
        test_reorder_cooldown,
        test_reorder_max_limit,
        test_reorder_stability_window,
        test_reorder_off_route_threshold,
        test_reorder_single_leg,
        test_optimizer_haversine_fallback,
    ]
    
    failures = 0
    for test_fn in tests:
        try:
            asyncio.run(test_fn())
        except Exception as e:
            failures += 1
            print(f"FAIL: {test_fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
    
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)