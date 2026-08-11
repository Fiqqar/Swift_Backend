import logging
import math
import os
from time import perf_counter

from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, HTTPException, Request

from app.schemas.pathfinding import (
    RouteOption,
    RouteOptionsResponse,
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
from app.services.pathfinding.route_options import (
    bump_penalties,
    max_overlap,
    merge_edge_classes,
    plan_graphs,
    route_edges,
    route_incidents,
    route_summary,
)
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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


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
    if not penalties:
        return set()
    return {eid for eid, mult in penalties.items() if mult == float("inf")}


def _traffic_segments(node_sequence, penalties) -> list[TrafficRouteSegment]:
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
                              mode: str = "car",
                              need_nodes: bool = False):
    key = _hier_cache_key(lat1, lon1, lat2, lon2, penalties, mode)
    cached = await get_route(redis, key)
    if cached is not None:
        if not need_nodes:
            return RouteResponse(**cached), None
        try:
            coords, _, _, _, node_sequence = await run_in_threadpool(
                route_hierarchical, hier, penalties, last_mile)
        except AreaNotCoveredError:
            return RouteResponse(**cached), None
        if not coords:
            node_sequence = None
        return RouteResponse(**cached), node_sequence
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
                         mode: str = "car",
                         need_nodes: bool = False):
    if plan[0] == "hierarchical":
        try:
            return await _route_hierarchical(
                plan[1], redis, penalties, lat1, lon1, lat2, lon2,
                last_mile, mode, need_nodes=need_nodes)
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
        if not need_nodes:
            return RouteResponse(**cached), None
        node_path, _ = await run_in_threadpool(
            engine_route, pg, start_node, goal_node, penalties)
        node_sequence = list(node_path) if node_path else None
        return RouteResponse(**cached), node_sequence

    t_route = perf_counter()
    node_path, total_distance = await run_in_threadpool(
        engine_route, pg, start_node, goal_node, penalties)
    logger.info("[PERF] Rust Engine Pathfinding Search: %.1f ms",
                (perf_counter() - t_route) * 1000.0)

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
                if not need_nodes:
                    return RouteResponse(**cached), None
                node_path, _ = await run_in_threadpool(
                    engine_route, pg, start_node, goal_node, penalties)
                node_sequence = list(node_path) if node_path else None
                return RouteResponse(**cached), node_sequence
            t_route = perf_counter()
            node_path, total_distance = await run_in_threadpool(
                engine_route, pg, start_node, goal_node, penalties)
            logger.info(
                "[PERF] Rust Engine Pathfinding Search (fallback): %.1f ms",
                (perf_counter() - t_route) * 1000.0)
            log_snap(logger, "destination(fallback)", lat2, lon2, goal_node,
                     pg.locations, pg.graph, pg.edge_classes)

    if not node_path:
       
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


def _normalize_mode(payload) -> str:
    raw = payload.mode or os.environ.get("VEHICLE_MODE", "car").strip().lower()
    return raw if raw in ("motorcycle", "car", "truck") else "car"


def _physical_distance(coords: list) -> float:
    total = 0.0
    for i in range(1, len(coords)):
        total += haversine_distance(coords[i - 1], coords[i])
    return total


async def _best_route(app, plan, redis, traffic_penalties,
                      payload, mode: str, lat1: float, lon1: float,
                      lat2: float, lon2: float,
                      need_nodes: bool = True):
   
    penalties = traffic_penalties
    
    from app.services.pathfinding.vehicle import blocked_penalties_for
    blocked = await run_in_threadpool(
        blocked_penalties_for, plan, mode)
    if blocked:
        penalties = dict(penalties)
        penalties.update(blocked)

    last_mile = last_mile_enabled(payload)
    try:
        response, node_sequence = await _compute_route(
            app, plan, redis, penalties, lat1, lon1, lat2, lon2,
            last_mile, mode, need_nodes=need_nodes)
    except HTTPException as e:
        if e.status_code != 400 or mode == "car":
            raise
        logger.info("[VEHICLE] Mode %s gagal (%s), fallback ke car.",
                    mode, e.detail)
        response, node_sequence = None, None

    if response is None and mode != "car":
        fallback_mode = mode
        mode = "car"
        penalties = dict(traffic_penalties)
        car_blocked = await run_in_threadpool(
            blocked_penalties_for, plan, mode)
        if car_blocked:
            penalties.update(car_blocked)
        try:
            response, node_sequence = await _compute_route(
                app, plan, redis, penalties, lat1, lon1, lat2, lon2,
                last_mile, mode, need_nodes=need_nodes)
        except HTTPException as e:
            logger.info("[VEHICLE] Fallback car juga gagal (%s).", e.detail)
            response, node_sequence = None, None
        if response is not None:
            response.warning = (response.warning or "") + (
                " Rute mode %s tidak tersedia; memakai rute mobil (car)."
                % fallback_mode)
            logger.info("[VEHICLE] Fallback %s -> car berhasil (%.0f m).",
                        fallback_mode, response.total_distance_meters)
    if response is None:
        raise HTTPException(status_code=404, detail="Rute tidak ditemukan!")

    from app.services.traffic.provider import provider_mode, traffic_enabled
    final_penalties = penalties
    if traffic_enabled() and provider_mode() == "smart_hybrid":
        from app.services.traffic.smart_hybrid import probe_corridor
        corridor = await probe_corridor(
            app, redis, response.route_coordinates)
        if corridor["congested"]:
            merged = dict(penalties)
            merged.update(corridor["penalties"])
            logger.info(
                "[TRAFFIC] Corridor macet (%d edge), re-route instan.",
                len(corridor["penalties"]))
            rerouted = await _compute_route(
                app, plan, redis, merged, lat1, lon1, lat2, lon2,
                last_mile, mode, need_nodes=need_nodes)
            if rerouted is not None and rerouted[0] is not None:
                response, node_sequence = rerouted
                final_penalties = merged

    from app.services.traffic.eta import compute_eta, estimated_arrival
    from app.services.traffic.poller import _reference_graph
    eta_graph, eta_locations = _reference_graph(app)
    response.estimated_time_seconds = compute_eta(
        eta_graph, eta_locations, response.route_coordinates, final_penalties)
    response.estimated_arrival = estimated_arrival(
        response.estimated_time_seconds)
    return response, node_sequence, final_penalties, mode


@router.post("/find-route", response_model=RouteResponse)
async def find_route(payload: RouteRequest, request: Request):
    lat1, lon1 = payload.origin.latitude, payload.origin.longitude
    lat2, lon2 = payload.destination.latitude, payload.destination.longitude
    mode = _normalize_mode(payload)
    try:
        plan = await run_in_threadpool(
            _resolve_plan, request.app, lat1, lon1, lat2, lon2)
    except AreaNotCoveredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal memuat peta: {str(e)}")

    redis = getattr(request.app.state, "redis", None)
    traffic_penalties = await get_request_penalties(
        request.app, redis, (lat1, lon1), (lat2, lon2))
    response, _node_sequence, final_penalties, mode = await _best_route(
        request.app, plan, redis, traffic_penalties, payload, mode,
        lat1, lon1, lat2, lon2)

    
    if live_tracking_enabled() or dynamic_rerouting_enabled(payload):
        request.app.state.active_route = {
            "dest": (lat2, lon2),
            "coords": response.route_coordinates,
            "plan": plan,
            "penalties": final_penalties,
            "mode": mode,
            "last_mile": last_mile_enabled(payload),
        }
    return response


def _route_option(route_id: int, response, node_sequence,
                  final_penalties, edge_classes,
                  speed_kmh, incident_delay_minutes,
                  eta_graph, eta_locations) -> RouteOption:
    from app.services.traffic.eta import compute_eta, estimated_arrival
    eta_s = response.estimated_time_seconds
    if eta_s is None:
        eta_s = compute_eta(
            eta_graph, eta_locations, response.route_coordinates,
            final_penalties)
        response.estimated_time_seconds = eta_s
        response.estimated_arrival = estimated_arrival(eta_s)
    distance_m = _physical_distance(response.route_coordinates)
    incidents = route_incidents(
        node_sequence or [], response.route_coordinates,
        final_penalties, speed_kmh, incident_delay_minutes)
    return RouteOption(
        route_id=route_id,
        is_best=(route_id == 1),
        summary=route_summary(
            node_sequence or [], response.route_coordinates, edge_classes),
        distance_km=round(distance_m / 1000.0, 2),
        duration_mins=round((eta_s or 0.0) / 60.0, 1),
        total_distance_meters=round(distance_m, 2),
        route_coordinates=response.route_coordinates,
        estimated_time_seconds=eta_s,
        estimated_arrival=response.estimated_arrival,
        traffic_segments=_traffic_segments(node_sequence, final_penalties),
        incidents=incidents,
    )


@router.post("/find-route-options", response_model=RouteOptionsResponse)
async def find_route_options(payload: RouteRequest, request: Request):
    lat1, lon1 = payload.origin.latitude, payload.origin.longitude
    lat2, lon2 = payload.destination.latitude, payload.destination.longitude
    mode = _normalize_mode(payload)
    try:
        plan = await run_in_threadpool(
            _resolve_plan, request.app, lat1, lon1, lat2, lon2)
    except AreaNotCoveredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal memuat peta: {str(e)}")

    redis = getattr(request.app.state, "redis", None)
    traffic_penalties = await get_request_penalties(
        request.app, redis, (lat1, lon1), (lat2, lon2))

    best, node_sequence, final_penalties, mode = await _best_route(
        request.app, plan, redis, traffic_penalties, payload, mode,
        lat1, lon1, lat2, lon2, need_nodes=True)

    max_routes = max(1, _env_int("ALTERNATIVE_ROUTES_MAX", 3))
    bump = max(1.0, _env_float("ALTERNATIVE_ROUTE_BUMP", 8.0))
    overlap_threshold = _env_float("ALTERNATIVE_ROUTE_MAX_OVERLAP", 0.8)
    incident_delay_minutes = max(0.0, _env_float("INCIDENT_DELAY_MINUTES", 3.0))
    last_mile = last_mile_enabled(payload)

    from app.services.traffic.eta import eta_config
    from app.services.traffic.poller import _reference_graph
    speed_kmh = eta_config()["mode_speed_kmh"]
    eta_graph, eta_locations = _reference_graph(request.app)
    edge_classes = merge_edge_classes(plan_graphs(plan))

    def build(route_id, response, ns):
        return _route_option(
            route_id, response, ns, final_penalties, edge_classes,
            speed_kmh, incident_delay_minutes, eta_graph, eta_locations)

    routes = [build(1, best, node_sequence)]
    accepted_seqs = [node_sequence]
    used_edge_sets = [route_edges(node_sequence)]

    for route_id in range(2, max_routes + 1):
        alt_penalties = bump_penalties(final_penalties, used_edge_sets, bump)
        try:
            resp, ns = await _compute_route(
                request.app, plan, redis, alt_penalties,
                lat1, lon1, lat2, lon2, last_mile, mode, need_nodes=True)
        except HTTPException:
            break
        if resp is None or ns is None:
            break
        if any(max_overlap(ns, other) >= overlap_threshold
               for other in accepted_seqs):
            break
        routes.append(build(route_id, resp, ns))
        accepted_seqs.append(ns)
        used_edge_sets.append(route_edges(ns))

    total_route = len(routes)

    return RouteOptionsResponse(
        status="success",
        total_route=total_route,
        routes=routes,
        source=best.source,
        warning=best.warning,
        graph_radius_meters=best.graph_radius_meters,
    )
