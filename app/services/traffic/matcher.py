import logging
import math

from shapely.geometry import LineString
from shapely.strtree import STRtree

from app.services.pathfinding.core_a_star import edge_id

logger = logging.getLogger("pathfinding")

_DEFAULT_TOLERANCE_M = 15.0

_INDEX_CACHE: dict[int, tuple] = {}

_EDGE_GEOM_CACHE: dict[int, dict[int, list]] = {}


def _edge_index(graph: dict, locations: dict):
    key = id(graph)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    lines: list[LineString] = []
    ids: list[int] = []
    edge_geom: dict[int, list] = {}
    for u, neighbors in graph.items():
        if u not in locations:
            continue
        for v in neighbors:
            if v not in locations:
                continue
            try:
                lines.append(LineString([
                    (locations[u][1], locations[u][0]),
                    (locations[v][1], locations[v][0]),
                ]))
                ids.append(edge_id(u, v))
                edge_geom[ids[-1]] = [locations[u], locations[v]]
            except Exception:
                continue
    if not ids:
        _INDEX_CACHE[key] = (None, [])
        _EDGE_GEOM_CACHE[key] = {}
        return None, []
    tree = STRtree(lines)
    _INDEX_CACHE[key] = (tree, ids)
    _EDGE_GEOM_CACHE[key] = edge_geom
    if len(_INDEX_CACHE) > 16:
        oldest = next(iter(_INDEX_CACHE))
        _INDEX_CACHE.pop(oldest, None)
        _EDGE_GEOM_CACHE.pop(oldest, None)
    return tree, ids


def snap_segment(graph: dict, locations: dict,
                 coordinates: list[tuple[float, float]],
                 tolerance_m: float | None = None) -> list[int]:
    if not coordinates or len(coordinates) < 2:
        return []
    tol = tolerance_m if tolerance_m is not None else _DEFAULT_TOLERANCE_M
    tree, ids = _edge_index(graph, locations)
    if tree is None:
        return []
    lat1, lon1 = coordinates[0]
    lat2, lon2 = coordinates[-1]
    coslat = math.cos(math.radians((lat1 + lat2) / 2.0))
    dlat = tol / 111320.0
    dlon = tol / (111320.0 * max(0.1, coslat))

    geometry = LineString([(lon, lat) for lat, lon in coordinates])
    buffered = geometry.buffer(max(dlat, dlon))

    hits: set[int] = set()
    for i in tree.query(buffered):
        hits.add(ids[int(i)])
    return sorted(hits)


def penalized_segments(graph: dict, locations: dict,
                       penalties: dict[int, float]):
    _edge_index(graph, locations)
    edge_geom = _EDGE_GEOM_CACHE.get(id(graph), {})
    segments: list[dict] = []
    for eid, multiplier in penalties.items():
        coords = edge_geom.get(eid)
        if coords is None:
            continue
        segments.append({
            "edge_id": eid,
            "multiplier": multiplier,
            "closure": multiplier == float("inf") or multiplier == float("-inf"),
            "coordinates": coords,
        })
    return segments
