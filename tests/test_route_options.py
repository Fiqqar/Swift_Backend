"""Synthetic tests: multi-route options (penalty-bump) + incident extraction.

Run:  python -m pytest tests/test_route_options.py -q
or:   python tests/test_route_options.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.pathfinding.core_a_star import edge_id, haversine_distance
from app.services.pathfinding.core_engine import route as engine_route
from app.services.pathfinding.preprocess import build_path_graph
from app.services.pathfinding.route_options import (
    bump_penalties,
    max_overlap,
    route_edges,
    route_incidents,
    route_summary,
)


def _grid():
    """Tangga 2 baris x 3 kolom; dua jalur berbeda dari node 0 ke node 2."""
    locations = {
        0: (-6.9, 110.0000),
        1: (-6.9, 110.0002),
        2: (-6.9, 110.0004),
        3: (-6.901, 110.0000),
        4: (-6.901, 110.0002),
        5: (-6.901, 110.0004),
    }
    graph = {n: {} for n in locations}
    for a, b in [(0, 1), (1, 2), (3, 4), (4, 5), (0, 3), (2, 5), (1, 4)]:
        d = haversine_distance(locations[a], locations[b])
        graph[a][b] = d
        graph[b][a] = d
    return graph, locations


def test_route_edges_simple():
    assert route_edges([0, 1, 2]) == {edge_id(0, 1), edge_id(1, 2)}
    assert route_edges([0]) == set()
    assert route_edges([]) == set()


def test_bump_penalties_preserves_inf_and_adds_new():
    base = {edge_id(1, 2): 2.0, edge_id(5, 6): float("inf")}
    used = [{edge_id(1, 2), edge_id(3, 4)}]
    out = bump_penalties(base, used, bump=8.0)
    assert out[edge_id(1, 2)] == 8.0
    assert out[edge_id(3, 4)] == 8.0
    assert out[edge_id(5, 6)] == float("inf")
    assert base[edge_id(1, 2)] == 2.0  # tidak memodifikasi input
    out2 = bump_penalties({edge_id(1, 2): 20.0}, used, bump=8.0)
    assert out2[edge_id(1, 2)] == 20.0  # max(existing, bump)


def test_max_overlap():
    assert max_overlap([1, 2, 3], [1, 2, 3]) == 1.0
    assert max_overlap([1, 2, 3], [4, 5, 6]) == 0.0
    assert max_overlap([1, 2, 3], [1, 4, 5]) == 1.0 / 3.0
    assert max_overlap([], [1, 2]) == 0.0


def test_route_summary_dominant_class():
    nodes = [0, 1, 2, 3]
    coords = [(-6.9, 110.0), (-6.9, 110.01), (-6.9, 110.02), (-6.9, 110.03)]
    ec = {
        edge_id(0, 1): "primary",
        edge_id(1, 2): "primary",
        edge_id(2, 3): "residential",
    }
    summary = route_summary(nodes, coords, ec)
    assert "Jalan Nasional" in summary
    assert route_summary([0, 1], coords, {}) == "Rute utama"


def test_route_incidents_closure_and_congestion():
    nodes = [0, 1, 2, 3]
    coords = [(-6.9, 110.0), (-6.9, 110.01), (-6.9, 110.02), (-6.9, 110.03)]
    penalties = {
        edge_id(0, 1): float("inf"),
        edge_id(1, 2): 3.0,
        edge_id(2, 3): 1.1,
    }
    incidents = route_incidents(nodes, coords, penalties,
                                speed_kmh=40.0, delay_minutes=3.0)
    assert len(incidents) == 2
    closure = incidents[0]
    assert closure["type"] == "road_closure"
    assert closure["coordinates"][0] == coords[0]
    assert closure["coordinates"][-1] == coords[1]
    congestion = incidents[1]
    assert congestion["type"] == "congestion"
    assert congestion["delay_minutes"] > 3.0
    assert "Kemacetan" in congestion["description"]


def test_route_incidents_none_when_no_delay():
    nodes = [0, 1, 2]
    coords = [(-6.9, 110.0), (-6.9, 110.0001), (-6.9, 110.0002)]
    penalties = {edge_id(0, 1): 1.5}
    assert route_incidents(nodes, coords, penalties, 40.0, 3.0) == []
    assert route_incidents([], coords, penalties, 40.0, 3.0) == []


def test_alternative_route_uses_different_path():
    graph, locations = _grid()
    pg = build_path_graph(graph, locations, -6.9, 110.0002, enable_ch=False)
    path1, _ = engine_route(pg, 0, 2, None)
    assert path1 == [0, 1, 2], path1

    bumped = bump_penalties({}, [route_edges(path1)], bump=8.0)
    path2, _ = engine_route(pg, 0, 2, bumped)
    assert path2 and path2 != path1, path2
    assert path2[1] not in (1,), "jalur alternatif tidak boleh lewat node 1"
    assert set(path2) - set(path1)  # ada bagian yang berbeda
    assert max_overlap(path1, path2) < 0.8


def test_no_alternative_returns_single_path():
    graph = {0: {}, 1: {}, 2: {}}
    locations = {
        0: (-6.9, 110.0), 1: (-6.9, 110.0002), 2: (-6.9, 110.0004),
    }
    for a, b in [(0, 1), (1, 2)]:
        d = haversine_distance(locations[a], locations[b])
        graph[a][b] = d
        graph[b][a] = d
    pg = build_path_graph(graph, locations, -6.9, 110.0002, enable_ch=False)
    path1, _ = engine_route(pg, 0, 2, None)
    bumped = bump_penalties({}, [route_edges(path1)], bump=8.0)
    path2, _ = engine_route(pg, 0, 2, bumped)
    assert path2 == path1
    assert max_overlap(path1, path2) >= 0.8  # didedupe oleh threshold


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
