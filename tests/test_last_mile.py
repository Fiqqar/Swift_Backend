"""Synthetic tests: disconnected last-mile routing fallback + traffic spans.

Run:  python -m pytest tests/test_last_mile.py -q
or:   python tests/test_last_mile.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.v1.endpoints.pathfinding import _traffic_segments
from app.services.pathfinding.connectivity import (
    nearest_node_reaching,
    reaches,
    resolve_goal,
)
from app.services.pathfinding.core_a_star import edge_id, haversine_distance
from app.services.pathfinding.core_engine import route as engine_route
from app.services.pathfinding.graph_loader import (
    _build_demo_grid,
    _way_kept,
    find_nearest_node,
)
from app.services.pathfinding.hierarchical import (
    HierarchicalResult,
    route_hierarchical,
)
from app.services.pathfinding.preprocess import build_path_graph
from app.services.pathfinding.snap import (
    adjacent_highway_classes,
    k_nearest_nodes,
    snap_point_to_graph,
)


def _main_road():
    """Jalan utama node 0..9 (garis lurus)."""
    lat, lon0 = -6.9, 110.0
    locations = {}
    graph = {}
    for i in range(10):
        locations[i] = (lat, lon0 + i * 0.0002)
        graph[i] = {}
    for i in range(9):
        a, b = i, i + 1
        d = haversine_distance(locations[a], locations[b])
        graph[a][b] = d
        graph[b][a] = d
    return graph, locations


def _island_graph():
    """Jalan utama node 0..9 + pulau terpisah node 100/101 (disconnected)."""
    graph, locations = _main_road()
    for i, dx in enumerate((0.0030, 0.0032)):
        nid = 100 + i
        locations[nid] = (-6.9, 110.0 + dx)
        graph[nid] = {}
    a, b = 100, 101
    d = haversine_distance(locations[a], locations[b])
    graph[a][b] = d
    graph[b][a] = d
    return graph, locations


def test_reachability_island():
    graph, _ = _island_graph()
    assert reaches(graph, 0, 9)
    assert not reaches(graph, 0, 100)
    assert not reaches(graph, 0, 101)


def test_resolve_goal_skips_island():
    graph, locations = _island_graph()
    goal_coord = (-6.9, 110.00305)
    goal_node = find_nearest_node(goal_coord[0], goal_coord[1], locations)
    assert goal_node in (100, 101), goal_node
    resolved, dist, fallback = resolve_goal(
        graph, locations, 0, goal_coord, goal_node)
    assert fallback is True
    assert resolved in range(10), resolved
    assert dist is not None and dist < 800.0


def test_route_engine_recovers_via_fallback():
    graph, locations = _island_graph()
    goal_coord = (-6.9, 110.00305)
    goal_node = find_nearest_node(goal_coord[0], goal_coord[1], locations)
    assert goal_node is not None
    path, cost = engine_route(build_path_graph(
        graph, locations, -6.9, 110.0, enable_ch=False), 0, goal_node)
    assert not path or cost == float("inf")
    resolved, _, _ = resolve_goal(graph, locations, 0, goal_coord, goal_node)
    path2, cost2 = engine_route(build_path_graph(
        graph, locations, -6.9, 110.0, enable_ch=False), 0, resolved)
    assert path2 and cost2 < float("inf")


def test_nearest_node_reaching_portal():
    graph, locations = _island_graph()
    portal = {0}
    coord = (-6.9, 110.00305)
    node, dist = nearest_node_reaching(graph, locations, portal, coord)
    assert node in range(10), node
    assert reaches(graph, node, 0)


def test_traffic_segments_spans():
    nodes = [0, 1, 2, 3, 4]
    penalties = {edge_id(1, 2): 2.5}
    spans = _traffic_segments(nodes, penalties)
    assert len(spans) == 1
    assert spans[0].start_index == 1
    assert spans[0].end_index == 2
    assert spans[0].multiplier == 2.5
    assert _traffic_segments([0, 1], {}) == []
    assert _traffic_segments([], {edge_id(0, 1): 2.0}) == []


def test_snap_helpers():
    graph, locations = _island_graph()
    near = k_nearest_nodes(-6.9, 110.0001, locations, k=3, max_dist=800)
    assert near and near[0][0] == 0
    assert len(near) <= 3
    proj = snap_point_to_graph(graph, locations, -6.9, 110.0001, 0)
    assert proj is not None
    classes = adjacent_highway_classes(graph, None, 0)
    assert classes == []


def test_way_kept_permissive():
    assert _way_kept({"highway": "residential", "access": "destination"})
    assert _way_kept({"highway": "service", "access": "permissive"})
    assert _way_kept({"highway": "residential", "access": "residential"})
    assert _way_kept({"highway": "living_street"})
    assert _way_kept({"highway": "residential", "access": "no"})  # keep-all


def test_demo_grid_vehicle_blocking_still_works():
    g, locs, ec = _build_demo_grid(-6.2, 106.8, 3000)
    pg = build_path_graph(g, locs, -6.2, 106.8, enable_ch=False, edge_classes=ec)
    assert len(ec) > 0
    assert adjacent_highway_classes(pg.graph, ec, list(locs)[0])


def _hier_result():
    base_g, base_loc = _main_road()
    base = build_path_graph(base_g, base_loc, -6.9, 110.0009, enable_ch=False)
    local_origin = build_path_graph(base_g, base_loc, -6.9, 110.0,
                                    enable_ch=False)

    gd = {n: dict(base_g[n]) for n in range(5, 10)}
    ld = {n: base_loc[n] for n in range(5, 10)}
    ld[100] = (-6.9, 110.0030)
    ld[101] = (-6.9, 110.0032)
    gd[100] = {}
    gd[101] = {}
    d = haversine_distance(ld[100], ld[101])
    gd[100][101] = d
    gd[101][100] = d
    local_dest = build_path_graph(gd, ld, -6.9, 110.0031, enable_ch=False)

    return HierarchicalResult(
        origin=(-6.9, 110.0000),
        dest=(-6.9, 110.00305),
        local_origin=local_origin,
        local_dest=local_dest,
        base=base,
        portal_origin=set(range(10)),
        portal_dest=set(range(5, 10)),
        warning=None,
        fallback_origin=False,
        fallback_dest=False,
    )


def test_route_hierarchical_dest_fallback_reaches_main():
    result = _hier_result()
    coords, total, source, warning, nodes = route_hierarchical(result)
    assert coords, "rute harus tetap ditemukan walau destinasi terputus"
    assert source == "hierarchical"
    assert nodes and nodes[-1] in range(5, 10), nodes
    assert nodes[-1] not in (100, 101)
    assert warning and "disesuaikan" in warning, warning
    assert total > 0


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
