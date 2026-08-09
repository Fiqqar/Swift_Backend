import logging
import math
import os
from time import perf_counter

from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, HTTPException, Request

from app.schemas.pathfinding import (
    RouteRequest,
    RouteResponse,
    TrafficRouteSegment,
)
from app.services.cache_service import (
    get_route,
    graph_scope,
    route_key,
    set_route,
)
from app.services.pathfinding.connectivity import resolve_goal
from app.services.pathfinding.core_a_star import (
    edge_id,
    haversine_distance,
)
from app.services.pathfinding.core_engine import route as engine_route
from app.services.pathfinding.graph_loader import (
    AreaNotCoveredError,
    _LOCAL_ROUTE_MAX_M,
    base_available,
    find_nearest_node,
    hierarchical_available,
    load_base_graph,
    load_graph_covering,
    load_local_graph_covering,
    region_graph_cached,
    tiles_contain,
    tiles_enabled,
)
from app.services.pathfinding.hierarchical import (
    build_hierarchical,
    route_hierarchical,
)
from app.services.pathfinding.snap import log_snap, snap_point_to_graph
from app.services.traffic.smart_hybrid import get_request_penalties

router = APIRouter()

logger = logging.getLogger("pathfinding")

_COVER_MARGIN = 600.0
_MAX_SNAP_DIST = float(os.environ.get("MAX_SNAP_DIST", "1500"))

_MAX_OFF_ROUTE_M = float(os.environ.get("MAX_OFF_ROUTE_DISTANCE_METERS", "30"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw in ("1", "true", "True", "TRUE", "yes", "on")


def last_mile_enabled(payload) -> bool:
    if payload.last_mile_precision is not None:
        return bool(payload.last_mile_precision)
    return _env_bool("ENABLE_LAST_MILE_PRECISION", True)


def dynamic_rerouting_enabled(payload) -> bool:
    if payload.dynamic_rerouting is not None:
        return bool(payload.dynamic_rerouting)
    return _env_bool("ENABLE_DYNAMIC_REROUTING", False)


def live_tracking_enabled() -> bool:
    return _env_bool("ENABLE_LIVE_TRACKING", False)


def _blocked_edge_ids(penalties: dict | None) -> set:
    """Edge dengan penalty inf (diblokir penuh, mis. filter mode kendaraan)."""
    if not penalties:
        return set()
    return {eid for eid, mult in penalties.items() if mult == float("inf")}


def _traffic_segments(node_sequence, penalties) -> list[TrafficRouteSegment]:
    """Span route_coordinates yang kena traffic (untuk pewarnaan overlay).

    node_sequence = daftar node OSM per titik route_coordinates. Setiap
    pasangan node berurutan dipetakan ke edge_id lalu dicocokkan dgn penalties
    traffic; indeks yang kena digabung menjadi rentang [start_index, end_index].
    """
    if not node_sequence or len(node_sequence) < 2 or not penalties:
        return []
    spans = []
    cur = None
    for i in range(len(node_sequence) - 1):
        eid = edge_id(node_sequence[i], node_sequence[i + 1])
        mult = penalties.get(eid)
        if mult is not None:
            if cur is None:
                cur = [i, i + 1]
            else:
                cur[1] = i + 1
        elif cur is not None:
            spans.append(tuple(cur))
            cur = None
    if cur is not None:
        spans.append(tuple(cur))
    return [TrafficRouteSegment(start_index=s[0], end_index=s[1],
                                multiplier=penalties[
                                    edge_id(node_sequence[s[0]],
                                            node_sequence[s[0] + 1])])
            for s in spans]


def _covers(pg, lat: float, lon: float) -> bool:
    if pg is None:
        return False
    bbox = getattr(pg, "bbox", None)
    if bbox is not None:
        minlon, minlat, maxlon, maxlat = bbox
        dlat = _COVER_MARGIN / 111320.0
        dlon = dlat / max(0.1, math.cos(math.radians((minlat + maxlat) / 2.0)))
        return (minlon - dlon <= lon <= maxlon + dlon
                and minlat - dlat <= lat <= maxlat + dlat)
    d = haversine_distance((pg.ref_lat, pg.ref_lon), (lat, lon))
    return d <= pg.radius + _COVER_MARGIN


def _resolve_plan(app, lat1: float, lon1: float, lat2: float, lon2: float):
    """Pilih strategi rute. Kembalikan ("graph", pg) atau ("hierarchical", h).

    Prioritas ujung origin/dest adalah presisi GANG (residential), sehingga
    semua rute > LOCAL_ROUTE_MAX_KM memakai hierarchical (graf gang level-1
    di kedua ujung + base jalan utama di tengah). Base/region hanya fallback
    bila tile/hierarchical tidak tersedia.

    Urutan:
      0. covering level-1 (gang) dari tile bila jarak pendek (<= LOCAL_ROUTE_MAX_KM)
      1. hierarchical (tile gang lokal + base jalan utama) untuk jarak lebih jauh
      2. path_graph (warmup kecil)  [fallback, tanpa gang]
      3. region_graph (bbox region) [fallback, tanpa gang]
      4. base_graph (jalan utama se-Jawa) [fallback terakhir]
      5. load_graph_covering (satu graf penutup) bila dynamic/prewarm
      6. fail-fast (di luar cakupan / non-PBF)
    """
    dist = haversine_distance((lat1, lon1), (lat2, lon2))
    if (dist <= _LOCAL_ROUTE_MAX_M
            and tiles_enabled()
            and tiles_contain(lat1, lon1, lat2, lon2)):
        return ("graph", load_local_graph_covering(lat1, lon1, lat2, lon2,
                                                   level=1))
    if (dist > _LOCAL_ROUTE_MAX_M
            and hierarchical_available(lat1, lon1, lat2, lon2)):
        try:
            return ("hierarchical", build_hierarchical(lat1, lon1, lat2, lon2))
        except AreaNotCoveredError as e:
            logger.info(
                "Hierarchical tak tersedia (%s), fallback ke graf tanpa gang.",
                e)
    plan = _resolve_fallback_graph(app, lat1, lon1, lat2, lon2)
    if plan is not None:
        return plan
    raise AreaNotCoveredError(
        "Area di luar cakupan peta yang dimuat. Pre-warm cache dengan "
        "`python scripts/prewarm_route.py <lat1> <lon1> <lat2> <lon2>` "
        "atau perbesar REGION_GRAPH_BBOX di .env.")


def _resolve_fallback_graph(app, lat1: float, lon1: float,
                            lat2: float, lon2: float):
    """Graf fallback (tanpa gang) bila hierarchical gagal/tak tersedia.
    Urutan: path_graph (warmup) -> region_graph -> base_graph -> covering."""
    preload = getattr(app.state, "path_graph", None)
    if (preload is not None
            and _covers(preload, lat1, lon1)
            and _covers(preload, lat2, lon2)):
        return ("graph", preload)
    region = getattr(app.state, "region_graph", None)
    if (region is not None
            and _covers(region, lat1, lon1)
            and _covers(region, lat2, lon2)):
        return ("graph", region)
    if base_available():
        base = load_base_graph()
        if (_covers(base, lat1, lon1) and _covers(base, lat2, lon2)):
            return ("graph", base)
    if (os.environ.get("OSMNX_ALLOW_DYNAMIC_LOAD", "0") == "1"
            or region_graph_cached(lat1, lon1, lat2, lon2)):
        return ("graph", load_graph_covering(lat1, lon1, lat2, lon2))
    return None


def _hier_cache_key(lat1: float, lon1: float,
                    lat2: float, lon2: float, penalties, mode: str = "car") -> str:
    q = (round(lat1, 4), round(lon1, 4), round(lat2, 4), round(lon2, 4))
    scope = "hier:%s:%s:%s:%s:%s" % ((mode,) + q)
    return route_key(scope, 0, 0, penalties)


async def _route_hierarchical(hier, redis, penalties,
                              lat1: float, lon1: float,
                              lat2: float, lon2: float,
                              last_mile: bool = True,
                              mode: str = "car"):
    """Hitung rute hierarchical (dengan cache Redis). None bila tak ada rute."""
    key = _hier_cache_key(lat1, lon1, lat2, lon2, penalties, mode)
    cached = await get_route(redis, key)
    if cached is not None:
        return RouteResponse(**cached), None
    try:
        coords, total, source, warning, node_sequence = await run_in_threadpool(
            route_hierarchical, hier, penalties, last_mile)
    except AreaNotCoveredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not coords:
        return None, None
    response = RouteResponse(
        status="success",
        total_distance_meters=round(total, 2),
        route_coordinates=coords,
        source=source,
        warning=warning,
        graph_radius_meters=None,
        traffic_segments=_traffic_segments(node_sequence, penalties),
    )
    await set_route(redis, key, response.model_dump())
    return response, node_sequence


async def _compute_route(app, plan, redis, penalties,
                         lat1: float, lon1: float,
                         lat2: float, lon2: float,
                         last_mile: bool = True,
                         mode: str = "car"):
    """Hitung rute sesuai plan dengan penalties tertentu.

    Menangani plan hierarchical (dengan fallback graf tanpa gang bila
    hierarchical gagal 400) dan plan graf (dengan cadangan hierarchical bila
    graf lokal terputus). Kembalikan (RouteResponse|None, node_sequence|None);
    node_sequence dipakai untuk pewarnaan segmen traffic.
    """
    if plan[0] == "hierarchical":
        try:
            return await _route_hierarchical(
                plan[1], redis, penalties, lat1, lon1, lat2, lon2,
                last_mile, mode)
        except HTTPException as e:
            if e.status_code != 400:
                raise
            logger.info(
                "Hierarchical gagal (%s), fallback ke graf tanpa gang.",
                e.detail)
            fb = _resolve_fallback_graph(app, lat1, lon1, lat2, lon2)
            if fb is None:
                raise
            plan = fb

    pg = plan[1]
    start_node = await run_in_threadpool(
        find_nearest_node, lat1, lon1, pg.locations)
    goal_node = await run_in_threadpool(
        find_nearest_node, lat2, lon2, pg.locations)

    log_snap(logger, "origin", lat1, lon1, start_node,
             pg.locations, pg.graph, pg.edge_classes)
    log_snap(logger, "destination", lat2, lon2, goal_node,
             pg.locations, pg.graph, pg.edge_classes)

    if start_node is None or goal_node is None:
        raise HTTPException(status_code=400, detail="Lokasi di luar jangkauan peta!")

    d_start = haversine_distance((lat1, lon1), pg.locations[start_node])
    d_goal = haversine_distance((lat2, lon2), pg.locations[goal_node])
    if d_start > _MAX_SNAP_DIST or d_goal > _MAX_SNAP_DIST:
        raise HTTPException(
            status_code=400,
            detail="Lokasi di luar jangkauan peta (tidak ada jalan di sekitar titik)!",
        )

    scope = "%s:%s" % (mode, graph_scope(pg))
    key = route_key(scope, start_node, goal_node, penalties)

    cached = await get_route(redis, key)
    if cached is not None:
        return RouteResponse(**cached), None

    t_route = perf_counter()
    node_path, total_distance = await run_in_threadpool(
        engine_route, pg, start_node, goal_node, penalties)
    logger.info("[PERF] Rust Engine Pathfinding Search: %.1f ms",
                (perf_counter() - t_route) * 1000.0)

    # Goal terputus (no path): coba node tujuan alternatif terdekat yang masih
    # terjangkau dari komponen graf utama (pulau terpisah, docs: last-mile).
    if not node_path:
        new_goal, alt_dist, _ = await run_in_threadpool(
            resolve_goal, pg.graph, pg.locations, start_node,
            (lat2, lon2), goal_node, _blocked_edge_ids(penalties))
        if new_goal is not None and new_goal != goal_node:
            logger.info(
                "[SNAP] Destinasi terputus; fallback ke node %s "
                "(%.0f m dari titik)", new_goal, alt_dist)
            goal_node = new_goal
            key = route_key(scope, start_node, goal_node, penalties)
            cached = await get_route(redis, key)
            if cached is not None:
                return RouteResponse(**cached), None
            t_route = perf_counter()
            node_path, total_distance = await run_in_threadpool(
                engine_route, pg, start_node, goal_node, penalties)
            logger.info(
                "[PERF] Rust Engine Pathfinding Search (fallback): %.1f ms",
                (perf_counter() - t_route) * 1000.0)
            log_snap(logger, "destination(fallback)", lat2, lon2, goal_node,
                     pg.locations, pg.graph, pg.edge_classes)

    if not node_path:
        # Graf lokal (tile) dapat terputus di batas tile/rect. Cadangan:
        # rute hierarchical (base graph tersambung lintas Jawa).
        if getattr(pg, "source", "").startswith("tile:"):
            try:
                hier = await run_in_threadpool(
                    build_hierarchical, lat1, lon1, lat2, lon2)
            except AreaNotCoveredError as e:
                raise HTTPException(status_code=400, detail=str(e))
            response, node_sequence = await _route_hierarchical(
                hier, redis, penalties, lat1, lon1, lat2, lon2,
                last_mile, mode)
            if response is not None:
                return response, node_sequence
        return None, None

    node_sequence = list(node_path)
    route_coords = [pg.locations[node_id] for node_id in node_path]

    # Presisi ujung: proyeksikan origin & destination ke ruas jalan terdekat
    # (bukan hanya node terdekat) agar koordinat akhir menempel pada jalan.
    if last_mile:
        start_proj = await run_in_threadpool(
            snap_point_to_graph, pg.graph, pg.locations, lat1, lon1, start_node)
        goal_proj = await run_in_threadpool(
            snap_point_to_graph, pg.graph, pg.locations, lat2, lon2, goal_node)
        route_coords[0] = start_proj
        route_coords[-1] = goal_proj
        total_distance += haversine_distance((lat1, lon1), start_proj)
        total_distance += haversine_distance((lat2, lon2), goal_proj)

    response = RouteResponse(
        status="success",
        total_distance_meters=round(total_distance, 2),
        route_coordinates=route_coords,
        source=pg.source,
        warning=pg.warning,
        graph_radius_meters=pg.radius,
        traffic_segments=_traffic_segments(node_sequence, penalties),
    )
    await set_route(redis, key, response.model_dump())
    return response, node_sequence


@router.post("/find-route", response_model=RouteResponse)
async def find_route(payload: RouteRequest, request: Request):
    lat1, lon1 = payload.origin.latitude, payload.origin.longitude
    lat2, lon2 = payload.destination.latitude, payload.destination.longitude
    raw_mode = payload.mode or os.environ.get("VEHICLE_MODE", "car").strip().lower()
    mode = raw_mode if raw_mode in ("motorcycle", "car", "truck") else "car"
    try:
        plan = await run_in_threadpool(
            _resolve_plan, request.app, lat1, lon1, lat2, lon2)
    except AreaNotCoveredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal memuat peta: {str(e)}")

    redis = getattr(request.app.state, "redis", None)
    penalties = await get_request_penalties(
        request.app, redis, (lat1, lon1), (lat2, lon2))

    # Vehicle transport mode: blokir edge yang tidak diizinkan untuk mode
    # kendaraan (motorcycle/car/truck) via penalty inf (lihat docs/feature/
    # verhicle_transport.md). Penalty digabung dengan penalti traffic.
    from app.services.pathfinding.vehicle import blocked_penalties_for
    blocked = await run_in_threadpool(
        blocked_penalties_for, plan, mode)
    if blocked:
        penalties = dict(penalties)
        penalties.update(blocked)

    last_mile = last_mile_enabled(payload)
    response, node_sequence = await _compute_route(
        request.app, plan, redis, penalties, lat1, lon1, lat2, lon2,
        last_mile, mode)
    if response is None:
        raise HTTPException(status_code=404, detail="Rute tidak ditemukan!")

    # Bounded corridor sampling: probe TomTom di sepanjang corridor rute
    # awal; bila macet (ratio > 1.5), update bobot edge di RAM dan re-route
    # sekali untuk rute paling cepat.
    from app.services.traffic.provider import provider_mode, traffic_enabled
    final_penalties = penalties
    if traffic_enabled() and provider_mode() == "smart_hybrid":
        from app.services.traffic.smart_hybrid import probe_corridor
        corridor = await probe_corridor(
            request.app, redis, response.route_coordinates)
        if corridor["congested"]:
            merged = dict(penalties)
            merged.update(corridor["penalties"])
            logger.info(
                "[TRAFFIC] Corridor macet (%d edge), re-route instan.",
                len(corridor["penalties"]))
            rerouted = await _compute_route(
                request.app, plan, redis, merged, lat1, lon1, lat2, lon2,
                last_mile, mode)
            if rerouted is not None and rerouted[0] is not None:
                response, node_sequence = rerouted
                final_penalties = merged

    # Estimasi waktu tempuh (ETA), dipengaruhi traffic (penalty multiplier).
    from app.services.traffic.eta import compute_eta, estimated_arrival
    from app.services.traffic.poller import _reference_graph
    eta_graph, eta_locations = _reference_graph(request.app)
    response.estimated_time_seconds = compute_eta(
        eta_graph, eta_locations, response.route_coordinates, final_penalties)
    response.estimated_arrival = estimated_arrival(
        response.estimated_time_seconds)

    # Live tracking + dynamic rerouting: simpan rute aktif agar posisi driver
    # real-time dapat dibandingkan dan di-reroute otomatis (docs/feature/
    # dynamic_rerouting.md, live_update_position.md).
    if live_tracking_enabled() or dynamic_rerouting_enabled(payload):
        request.app.state.active_route = {
            "dest": (lat2, lon2),
            "coords": response.route_coordinates,
            "plan": plan,
            "penalties": final_penalties,
            "mode": mode,
            "last_mile": last_mile,
        }
    return response
