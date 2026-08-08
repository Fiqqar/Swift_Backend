"""Hierarchical routing 3 lapisan (Gmaps-style):

    local(origin, gang) -> base(jalan utama) -> local(dest, gang)

Lapisan local memakai graf level-1 dari tile (semua _DRIVE_HIGHWAYS, presisi
hingga gang). Lapisan base memakai graf jalan utama seluruh Jawa (level 3)
yang dimuat lazy. Penyatuan antar lapisan dilakukan lewat node ID OSM yang
global (portal = node yang ada di local DAN base).

Rute dihitung sebagai:
  Dijkstra origin -> portalA (di localA)
  + A*/bidirectional di base portalA -> portalB
  + Dijkstra portalB -> dest (di localB)
"""

import heapq
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.services.pathfinding.core_a_star import (
    edge_id,
    haversine_distance,
)
from app.services.pathfinding.core_engine import route as engine_route
from app.services.pathfinding.graph_loader import (
    AreaNotCoveredError,
    _LOCAL_RADIUS,
    _LOCAL_RADIUS_MAX,
    find_nearest_node,
    load_base_graph,
    load_local_graph_point,
)

logger = logging.getLogger("pathfinding")

_MAX_SNAP_DIST = float(os.environ.get("MAX_SNAP_DIST", "1500"))
_MAX_PAIR_PRODUCT = 100000
_GROWTH_FACTOR = 1.5
# Bangun graf local origin & tujuan secara paralel (pyosmium lepas GIL).
_PARALLEL_LOCAL = os.environ.get("HIER_PARALLEL_LOCAL", "1") == "1"


@dataclass
class HierarchicalResult:
    origin: tuple
    dest: tuple
    local_origin: object
    local_dest: object
    base: object
    portal_origin: set
    portal_dest: set
    warning: str | None = None
    fallback_origin: bool = False
    fallback_dest: bool = False

    @property
    def source(self) -> str:
        return "hierarchical"


def _local_with_portal(lat: float, lon: float, base_ids: set, warnings: list):
    """PathGraph lokal di sekitar titik + portal ke base.

    Radius diperbesar bertahap sampai ada portal (jalan utama) dalam jangkauan.
    Bila tetap kosong, catat fallback: titik di-snap langsung ke base.
    """
    radius = _LOCAL_RADIUS
    pg = load_local_graph_point(lat, lon, radius, level=1)
    portal = set(pg.graph) & base_ids
    while not portal and radius < _LOCAL_RADIUS_MAX:
        radius = min(int(radius * _GROWTH_FACTOR), _LOCAL_RADIUS_MAX)
        pg = load_local_graph_point(lat, lon, radius, level=1)
        portal = set(pg.graph) & base_ids
    if not portal:
        warnings.append(
            "Lokasi (%.4f,%.4f) jauh dari jalan utama; "
            "segmen memakai titik jalan utama terdekat" % (lat, lon))
        return pg, set(), True
    return pg, portal, False


def build_hierarchical(lat1: float, lon1: float,
                       lat2: float, lon2: float) -> HierarchicalResult:
    """Siapkan lapisan localA/base/localB + portal untuk pasangan O->D."""
    base = load_base_graph()
    base_ids = set(base.graph)
    warnings: list = []
    endpoints = [(lat1, lon1), (lat2, lon2)]

    def _build(lat, lon):
        return _local_with_portal(lat, lon, base_ids, warnings)

    results = []
    if len(endpoints) > 1 and _PARALLEL_LOCAL:
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(_build, lat, lon) for lat, lon in endpoints]
            results = [f.result() for f in futs]
    else:
        results = [_build(lat, lon) for lat, lon in endpoints]

    local_a, portal_a, fb_a = results[0]
    local_b, portal_b, fb_b = results[1]
    return HierarchicalResult(
        origin=(lat1, lon1),
        dest=(lat2, lon2),
        local_origin=local_a,
        local_dest=local_b,
        base=base,
        portal_origin=portal_a,
        portal_dest=portal_b,
        warning="; ".join(warnings) or None,
        fallback_origin=fb_a,
        fallback_dest=fb_b,
    )


def _dijkstra(graph: dict, source: int, penalties: dict | None = None):
    """Dijkstra penuh dari source. Kembalikan (dist, parent)."""
    dist = {source: 0.0}
    parent = {}
    heap = [(0.0, source)]
    closed = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in closed:
            continue
        closed.add(u)
        for v, w in graph.get(u, {}).items():
            if penalties:
                w = w * penalties.get(edge_id(u, v), 1.0)
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                parent[v] = u
                heapq.heappush(heap, (nd, v))
    return dist, parent


def _reverse_graph(graph: dict) -> dict:
    rev = {}
    for u, neigh in graph.items():
        for v, w in neigh.items():
            rev.setdefault(v, {})[u] = w
    return rev


def _reconstruct(parent: dict, start: int, goal: int) -> list:
    path = [goal]
    n = goal
    guard = 0
    while n in parent and n != start and guard < 1000000:
        n = parent[n]
        path.append(n)
        guard += 1
    path.reverse()
    return path


def _reconstruct_rev(parent: dict, start: int, goal: int) -> list:
    """Path start->goal dari parent hasil Dijkstra pada graf terbalik."""
    path = [start]
    n = start
    guard = 0
    while n != goal and n in parent and guard < 1000000:
        n = parent[n]
        path.append(n)
        guard += 1
    return path


def _best_pair(portal_a: list, portal_b: list, dist_a: dict, dist_b: dict,
               locations: dict):
    """Pilih (portalA, portalB) yang meminimalkan perkiraan total biaya."""
    if len(portal_a) * len(portal_b) > _MAX_PAIR_PRODUCT:
        portal_a = sorted(portal_a, key=lambda p: dist_a[p])[:64]
        portal_b = sorted(portal_b, key=lambda p: dist_b[p])[:64]
    best = None
    best_cost = float("inf")
    for pa in portal_a:
        la = dist_a[pa]
        loc_a = locations[pa]
        for pb in portal_b:
            c = la + dist_b[pb] + haversine_distance(loc_a, locations[pb])
            if c < best_cost:
                best_cost = c
                best = (pa, pb)
    return best


def route_hierarchical(result: HierarchicalResult,
                       penalties: dict | None = None):
    """Hitung rute 3 lapisan. Kembalikan (coords, total_dist, source, warning).

    Bila graf lokal terpecah (portal ada di graf tapi tidak terjangkau dari
    titik snap), ujung tsb di-degrade ke snap jalan utama (base) — bukan error.
    """
    base = result.base
    base_locations = base.locations
    origin = result.origin
    dest = result.dest
    warnings: list = []

    def _snap(pg_locations: dict, lat: float, lon: float) -> int | None:
        nid = find_nearest_node(lat, lon, pg_locations)
        if nid is None:
            return None
        if haversine_distance((lat, lon), pg_locations[nid]) > _MAX_SNAP_DIST:
            return None
        return nid

    # --- Snap origin & dest ----------------------------------------------
    start_a = _snap(result.local_origin.locations, origin[0], origin[1]) \
        if result.portal_origin else None
    goal_b = _snap(result.local_dest.locations, dest[0], dest[1]) \
        if result.portal_dest else None

    # --- Dijkstra lokal origin & dest (paralel) ---------------------------
    def _dijk_a():
        if start_a is not None:
            return _dijkstra(result.local_origin.graph, start_a, penalties)
        return None

    def _dijk_b():
        if goal_b is not None:
            rev = _reverse_graph(result.local_dest.graph)
            return _dijkstra(rev, goal_b, penalties)
        return None

    if start_a is not None and goal_b is not None:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_a = ex.submit(_dijk_a)
            fut_b = ex.submit(_dijk_b)
            d_a, d_b = fut_a.result(), fut_b.result()
    else:
        d_a, d_b = _dijk_a(), _dijk_b()

    # --- Reachability portal; degrade ujung terpecah ke base --------------
    if start_a is not None:
        dist_a, parent_a = d_a
        cand_a = [pa for pa in result.portal_origin if pa in dist_a]
        if cand_a:
            portal_a, local_a = cand_a, True
        else:
            start_a = _snap(base_locations, origin[0], origin[1])
            if start_a is None:
                raise AreaNotCoveredError("Origin tidak terhubung ke jalan utama")
            dist_a, parent_a = {start_a: 0.0}, {}
            portal_a, local_a = [start_a], False
            warnings.append(
                "Origin tak terjangkau jalan utama; memakai jalan utama terdekat")
    else:
        start_a = _snap(base_locations, origin[0], origin[1])
        if start_a is None:
            raise AreaNotCoveredError("Origin tidak dekat graf jalan")
        dist_a, parent_a = {start_a: 0.0}, {}
        portal_a, local_a = [start_a], False

    if goal_b is not None:
        dist_b, parent_b = d_b
        cand_b = [pb for pb in result.portal_dest if pb in dist_b]
        if cand_b:
            portal_b, local_b = cand_b, True
        else:
            goal_b = _snap(base_locations, dest[0], dest[1])
            if goal_b is None:
                raise AreaNotCoveredError("Destinasi tidak terhubung ke jalan utama")
            dist_b, parent_b = {goal_b: 0.0}, {}
            portal_b, local_b = [goal_b], False
            warnings.append(
                "Destinasi tak terjangkau jalan utama; memakai jalan utama terdekat")
    else:
        goal_b = _snap(base_locations, dest[0], dest[1])
        if goal_b is None:
            raise AreaNotCoveredError("Destinasi tidak dekat graf jalan")
        dist_b, parent_b = {goal_b: 0.0}, {}
        portal_b, local_b = [goal_b], False

    # --- Pilih portal & rute tengah di base ------------------------------
    if local_a and local_b:
        pair = _best_pair(portal_a, portal_b, dist_a, dist_b, base_locations)
        if pair is None:
            raise AreaNotCoveredError("Tidak ada portal yang terjangkau")
        pa, pb = pair
    else:
        pa = min(portal_a, key=lambda p: dist_a[p])
        pb = min(portal_b, key=lambda p: dist_b[p])

    path_mid, cost_mid = engine_route(base, pa, pb, penalties)
    if not path_mid:
        raise AreaNotCoveredError("Rute tengah tidak ditemukan")

    # --- Rekonstruksi path lokal -----------------------------------------
    if local_a:
        path_a = _reconstruct(parent_a, start_a, pa)
        coords_a = [result.local_origin.locations[n] for n in path_a]
    else:
        path_a = [pa]
        coords_a = [base_locations[pa]]
    if local_b:
        path_b = _reconstruct_rev(parent_b, pb, goal_b)
        coords_b = [result.local_dest.locations[n] for n in path_b[1:]]
    else:
        path_b = [pb]
        coords_b = []

    # --- Koordinat + total -------------------------------------------------
    coords = coords_a
    for nid in path_mid[1:]:
        coords.append(base_locations[nid])
    coords.extend(coords_b)

    total = dist_a[pa] + cost_mid + dist_b[pb]

    warning = result.warning
    if warnings:
        warning = (warning + "; " if warning else "") + "; ".join(warnings)
    return coords, total, result.source, warning
