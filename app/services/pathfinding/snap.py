import math

from app.services.pathfinding.core_a_star import (
    _closest_point_on_segment,
    edge_id,
    haversine_distance,
)

_LOC_BUCKET = 1.0 / 1000.0
_MAX_RING = 512
_SNAP_HOPS = 2
_SNAP_MAX_NODES = 64


def _grid_index(locations: dict) -> dict:
    scale = int(round(1.0 / _LOC_BUCKET))
    idx = {}
    for nid, (lat, lon) in locations.items():
        b = (int(round(lat * scale)), int(round(lon * scale)))
        idx.setdefault(b, []).append(nid)
    return idx


def k_nearest_nodes(lat: float, lon: float, locations: dict,
                    k: int = 8, max_dist: float = 800.0) -> list:
    if not locations:
        return []
    scale = int(round(1.0 / _LOC_BUCKET))
    idx = _grid_index(locations)
    bc = (int(round(lat * scale)), int(round(lon * scale)))
    cands: list = []
    best_limit = max_dist
    for r in range(0, _MAX_RING):
        ring_best = None
        cells = []
        for c in range(bc[1] - r, bc[1] + r + 1):
            for rr in (bc[0] - r, bc[0] + r):
                cells.append((rr, c))
        for rr in range(bc[0] - r + 1, bc[0] + r):
            for c in (bc[1] - r, bc[1] + r):
                cells.append((rr, c))
        for cell in cells:
            for nid in idx.get(cell, ()):
                d = haversine_distance((lat, lon), locations[nid])
                if d > best_limit:
                    continue
                ring_best = min(ring_best, d) if ring_best is not None else d
                cands.append((d, nid))
        if ring_best is not None and ring_best <= r * _LOC_BUCKET * 111320.0:
            break
        if r * _LOC_BUCKET * 111320.0 > best_limit:
            break
    cands.sort(key=lambda t: t[0])
    return [(nid, d) for d, nid in cands[:k]]


def adjacent_highway_classes(graph: dict, edge_classes: dict | None,
                             node_id: int) -> list:
    if not edge_classes:
        return []
    classes = set()
    for nbr in graph.get(node_id, ()):
        cls = edge_classes.get(edge_id(node_id, nbr))
        if cls:
            classes.add(cls)
    return sorted(classes)


def _collect_near_nodes(graph: dict, node_id: int, max_nodes: int) -> set:
    seen = {node_id}
    frontier = [node_id]
    for _ in range(_SNAP_HOPS):
        nxt = []
        for u in frontier:
            for v in graph.get(u, ()):
                if v not in seen:
                    seen.add(v)
                    nxt.append(v)
                    if len(seen) >= max_nodes:
                        return seen
        frontier = nxt
        if not frontier:
            break
    return seen


def snap_point_to_graph(graph: dict, locations: dict, lat: float, lon: float,
                        node_id: int) -> tuple:
    if node_id not in locations:
        return (lat, lon)
    near = _collect_near_nodes(graph, node_id, _SNAP_MAX_NODES)
    best = locations[node_id]
    best_d = haversine_distance((lat, lon), best)
    for u in near:
        if u not in locations:
            continue
        for v in graph.get(u, ()):
            if v not in locations:
                continue
            proj = _closest_point_on_segment((lat, lon), locations[u],
                                             locations[v])
            d = haversine_distance((lat, lon), proj)
            if d < best_d:
                best_d = d
                best = proj
    return best


def log_snap(logger, label: str, lat: float, lon: float, node_id,
             locations: dict, graph: dict, edge_classes: dict | None) -> None:
    if node_id is None or node_id not in locations:
        logger.info("[SNAP] %s (%f, %f) -> tidak ada node", label, lat, lon)
        return
    snapped = locations[node_id]
    dist = haversine_distance((lat, lon), snapped)
    classes = adjacent_highway_classes(graph, edge_classes, node_id)
    logger.info(
        "[SNAP] %s asal (%f, %f) -> node %s di (%f, %f), "
        "jarak=%.1f m, highway=%s",
        label, lat, lon, node_id, snapped[0], snapped[1], dist,
        classes or "?")
