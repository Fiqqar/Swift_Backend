import asyncio
import logging
import math
import os
from time import perf_counter

from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, HTTPException, Request

from app.schemas.pathfinding import (
    Coordinate,
    DeliveryStop,
    GeofenceCheckRequest,
    GeofenceCheckResponse,
    OptimizedDeliveryLeg,
    OptimizedDeliveryRouteRequest,
    OptimizedDeliveryRouteResponse,
    OptimizedStop,
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
from app.services.polyline import encode_polyline
from app.services.tracking import (
    get_kurir_position_latlon,
    set_nav_route,
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

_DELIVERY_LEG_CONCURRENCY = max(1, _env_int("DELIVERY_LEG_CONCURRENCY", 5))

_ORDER_TOP_K = max(1, _env_int("DELIVERY_ORDER_TOP_K", 3))


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


def nav_enabled(payload) -> bool:
    if payload.dynamic_rerouting is not None:
        return bool(payload.dynamic_rerouting)
    return _env_bool("ENABLE_LIVE_NAVIGATION", False)


def _next_nav_route_id(app) -> int:
    if not hasattr(app.state, "_nav_route_counter"):
        app.state._nav_route_counter = 0
    app.state._nav_route_counter += 1
    return app.state._nav_route_counter


def _token_kurir_id(request: Request) -> int | None:
    """Ekstrak kurir_id dari token JWT (Authorization Bearer) bila ada."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    from app.core.security import decode_access_token
    payload = decode_access_token(auth[7:])
    if payload is None:
        return None
    try:
        return int(payload["sub"])
    except (KeyError, ValueError):
        return None


async def _resolve_delivery_start(request: Request, redis,
                                  payload) -> tuple[float, float, str]:
    """Hirarki fallback titik awal rute (design doc Bagian 6.1).

    Mengembalikan `(lat, lon, source)` dengan `source`:
      1. Prioritas 1: posisi kurir terbaru dari Redis `driver:pos:{kurir_id}`
         (diisi Webhook/WebSocket; kurir_id dari token JWT yang aktif) —
         `source = "webhook"`.
      2. Prioritas 2: `courier_position` dari payload request (eksplisit) —
         dipakai hanya bila posisi webhook tidak tersedia —
         `source = "courier_position"`.
      3. Prioritas 3: `hub_origin` (perilaku eksisting) —
         `source = "hub_origin"`.
    """
    kurir_id = _token_kurir_id(request)
    if kurir_id is not None:
        pos = await get_kurir_position_latlon(redis, kurir_id)
        if pos is not None:
            return (pos[0], pos[1], "webhook")

    if payload.courier_position is not None:
        return (payload.courier_position.latitude,
                payload.courier_position.longitude,
                "courier_position")

    if payload.hub_origin is not None:
        return (payload.hub_origin.latitude, payload.hub_origin.longitude,
                "hub_origin")

    if kurir_id is None:
        detail = (
            "Titik awal rute tidak tersedia: header `Authorization: Bearer "
            "<token>` kurir tidak ada/tidak valid, posisi Redis tidak bisa "
            "dibaca, dan `hub_origin` tidak diberikan."
        )
    else:
        detail = (
            "Titik awal rute tidak tersedia: posisi kurir %s tidak ditemukan "
            "di Redis (kirim posisi via WebSocket `WS "
            "/api/v1/ws/driver/position`) dan `hub_origin` tidak diberikan."
            % kurir_id
        )
    raise HTTPException(status_code=400, detail=detail)


async def _process_single_leg(app, redis, semaphore, leg_index: int,
                              stop: dict, prev_coord: tuple,
                              mode: str, last_mile, need_nodes: bool,
                              payload, incident_delay_minutes: float,
                              speed_kmh: float,
                              eta_graph, eta_locations) -> dict:
    """Proses satu leg rute (A* + TomTom + ETA) secara independen.

    Dijalankan paralel dengan konkurrensi dibatasi `semaphore`. Mengembalikan
    dict hasil leg; melempar HTTPException bila plan/area atau rute gagal.
    """
    from app.services.traffic.eta import compute_eta

    dest = stop["coordinate"]
    async with semaphore:
        try:
            plan = await run_in_threadpool(
                _resolve_plan, app,
                prev_coord[0], prev_coord[1], dest[0], dest[1])
        except AreaNotCoveredError as e:
            raise HTTPException(status_code=400, detail=str(e))

        leg_payload = RouteRequest(
            origin=Coordinate(latitude=prev_coord[0], longitude=prev_coord[1]),
            destination=Coordinate(latitude=dest[0], longitude=dest[1]),
            mode=mode,
            last_mile_precision=last_mile,
            dynamic_rerouting=payload.dynamic_rerouting,
        )
        traffic_penalties = {}
        if not payload.skip_traffic:
            traffic_penalties = await get_request_penalties(
                app, redis, prev_coord, dest)
        response, node_sequence, final_penalties, _m = await _best_route(
            app, plan, redis, traffic_penalties, leg_payload, mode,
            prev_coord[0], prev_coord[1], dest[0], dest[1],
            need_nodes=need_nodes, skip_traffic=payload.skip_traffic)
        if response is None:
            raise HTTPException(status_code=404,
                                detail="Rute tidak ditemukan ke stop "
                                       f"{stop['recipient_name'] or stop['package_id']}")

        distance_m = _physical_distance(response.route_coordinates)
        eta_s = response.estimated_time_seconds
        if eta_s is None:
            eta_s = compute_eta(eta_graph, eta_locations,
                                response.route_coordinates, final_penalties)
        incidents = route_incidents(
            node_sequence or [], response.route_coordinates,
            final_penalties, speed_kmh, incident_delay_minutes)
        leg = OptimizedDeliveryLeg(
            leg_index=leg_index,
            stop_sequence_number=leg_index + 1,
            package_id=stop["package_id"],
            recipient_name=stop["recipient_name"],
            service_type=stop["service_type"],
            geometry=response.route_coordinates,
            distance_km=round(distance_m / 1000.0, 2),
            duration_mins=round((eta_s or 0.0) / 60.0, 1),
            estimated_time_seconds=eta_s,
            traffic_segments=_traffic_segments(node_sequence, final_penalties),
            incidents=incidents,
        )
        return {
            "leg": leg,
            "distance_m": distance_m,
            "duration_s": eta_s or 0.0,
            "warning": response.warning,
            "source": response.source,
        }


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
                      need_nodes: bool = True,
                      skip_traffic: bool = False):
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
    if (not skip_traffic and traffic_enabled()
            and provider_mode() == "smart_hybrid"):
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


async def _ordering_road_distance(app, redis, mode: str, last_mile: bool,
                                  lat1: float, lon1: float,
                                  lat2: float, lon2: float) -> float:
    """Jarak jalan nyata (meter) untuk penentuan urutan stop (ordering).

    Memakai rute dasar (tanpa traffic) via `_compute_route` + cache Redis
    (`route:*`, TTL 300s) sehingga hasilnya akurat sekaligus cepat pada
    request berikutnya. Bila area tidak tercakup / rute gagal, fallback ke
    jarak haversine agar penentuan urutan tidak pernah crash (error rute
    yang sebenarnya tetap ditampilkan saat leg final dihitung).
    """
    fallback = haversine_distance((lat1, lon1), (lat2, lon2))
    try:
        plan = await run_in_threadpool(
            _resolve_plan, app, lat1, lon1, lat2, lon2)
    except AreaNotCoveredError:
        return fallback
    try:
        response, _nodes = await _compute_route(
            app, plan, redis, {}, lat1, lon1, lat2, lon2,
            last_mile=last_mile, mode=mode, need_nodes=False)
    except Exception as exc:
        logger.info(
            "[ORDER] Rute ordering (%.4f,%.4f)->(%.4f,%.4f) gagal (%s); "
            "fallback haversine.", lat1, lon1, lat2, lon2, exc)
        return fallback
    if response is None or response.total_distance_meters is None:
        return fallback
    return float(response.total_distance_meters)


@router.post("/find-route", response_model=RouteResponse,
             summary="Hitung rute terbaik antara dua titik",
             description=(
                 "Menghitung rute optimal (distance + ETA) dari `origin` ke "
                 "`destination`.\n\n"
                 "- **Wajib:** `origin`, `destination` (Coordinate).\n"
                 "- **Opsional:** `mode` (motorcycle/car/truck), "
                 "`last_mile_precision`, `dynamic_rerouting`.\n\n"
                 "Error: `400` di luar jangkauan peta, `404` rute tidak "
                 "ditemukan, `500` gagal memuat peta."))
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
    if nav_enabled(payload):
        route_id = _next_nav_route_id(request.app)
        response.route_id = route_id
        await set_nav_route(redis, _token_kurir_id(request) or 0, {
            "route_id": route_id,
            "kind": "single",
            "mode": mode,
            "last_mile": last_mile_enabled(payload),
            "total_distance_m": round(
                _physical_distance(response.route_coordinates), 1),
            "total_eta_s": round(response.estimated_time_seconds or 0.0, 1),
            "legs": [{
                "index": 0,
                "stop_sequence_number": 1,
                "package_id": None,
                "recipient_name": "Destination",
                "encoded": encode_polyline(response.route_coordinates, 5),
                "dest": (lat2, lon2),
                "eta_s": round(response.estimated_time_seconds or 0.0, 1),
            }],
        })
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


@router.post("/find-route-options", response_model=RouteOptionsResponse,
             summary="Hitung beberapa alternatif rute",
             description=(
                 "Menghitung rute terbaik + alternatif (dengan penalti untuk "
                 "menghindari overlap) antara `origin` dan `destination`.\n\n"
                 "- **Wajib:** `origin`, `destination`.\n"
                 "- **Opsional:** `mode`, `last_mile_precision`, "
                 "`dynamic_rerouting`.\n\n"
                 "Jumlah alternatif dikontrol env `ALTERNATIVE_ROUTES_MAX` "
                 "(default 3)."))
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
    overlap_threshold = _env_float("ALTERNATIVE_ROUTE_MAX_OVERLAP", 0.7)
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

    response = RouteOptionsResponse(
        status="success",
        total_route=total_route,
        routes=routes,
        source=best.source,
        warning=best.warning,
        graph_radius_meters=best.graph_radius_meters,
    )

    if nav_enabled(payload):
        best_opt = routes[0]
        route_id = _next_nav_route_id(request.app)
        response.route_id = route_id
        await set_nav_route(redis, _token_kurir_id(request) or 0, {
            "route_id": route_id,
            "kind": "single",
            "mode": mode,
            "last_mile": last_mile,
            "total_distance_m": round(
                _physical_distance(best_opt.route_coordinates), 1),
            "total_eta_s": round(best_opt.estimated_time_seconds or 0.0, 1),
            "legs": [{
                "index": 0,
                "stop_sequence_number": 1,
                "package_id": None,
                "recipient_name": "Destination",
                "encoded": encode_polyline(best_opt.route_coordinates, 5),
                "dest": (lat2, lon2),
                "eta_s": round(best_opt.estimated_time_seconds or 0.0, 1),
            }],
        })
    return response


@router.post("/find-optimized-delivery-route",
             response_model=OptimizedDeliveryRouteResponse,
             summary="Rute pengantaran multi-stop (TSP)",
             description=(
                 "Menentukan urutan stop paling efisien (hybrid greedy, EXPRESS "
                 "diutamakan) lalu menghitung leg rute sungguhan per pasangan stop.\n\n"
                 "- **Wajib:** `deliveries` (minimal 1).\n"
                 "- **Titik awal (fallback chain):** posisi kurir dari Redis "
                 "`driver:pos:{kurir_id}` (prioritas 1, dari token JWT, diisi "
                 "Webhook/WebSocket) → `courier_position` di payload (prioritas "
                 "2) → `hub_origin` (prioritas 3). "
                 "Minimal satu dari `courier_position`/`hub_origin` wajib ada.\n"
                 "- **Urutan stop (hybrid greedy):** dari posisi saat ini ambil "
                 "top-K kandidat terdekat (haversine, env `DELIVERY_ORDER_TOP_K` "
                 "default 3), hitung jarak jalan nyata untuk kandidat itu (cache "
                 "Redis), pilih yang paling efisien, lalu ulangi dari stop "
                 "terpilih hingga stop terakhir — persis perilaku kurir "
                 "`dari lokasi sekarang cari yang terdekat/efisien`.\n"
                 "- **Opsional:** `mode`, `last_mile_precision`, "
                 "`dynamic_rerouting`, `skip_traffic`, `return_to_hub`.\n\n"
                 "- Alamat tanpa `latitude`/`longitude` di-geocode (Nominatim).\n"
                 "- Error `400` bila geocode gagal / area tidak tercakup, `404` "
                 "bila rute ke sebuah stop tidak ditemukan.\n\n"
                 "### Performa\n"
                 "- Leg diproses **paralel** dengan konkurrensi terbatas "
                 "(env `DELIVERY_LEG_CONCURRENCY`, default 5).\n"
                 "- Jarak nyata untuk ordering memakai **cache rute Redis**; "
                 "request pertama lebih lambat (~N×K hit A*), request berikutnya "
                 "instan.\n"
                 "- Set `skip_traffic: true` untuk **memotong probe jaringan "
                 "eksternal TomTom** di tiap leg (A* murni) — respons jauh "
                 "lebih cepat, cocok untuk demo/testing."))
async def find_optimized_delivery_route(payload: OptimizedDeliveryRouteRequest,
                                        request: Request):
    """Rute pengantaran multi-stop dari posisi kurir ke banyak penerima.

    - Meng-geocode alamat tiap delivery (Nominatim + cache Redis) bila koordinat
      tidak diberikan inline.
    - Menentukan urutan stop (hybrid greedy) berbasis jarak jalan nyata dengan
      prioritas EXPRESS, selalu dimulai dari posisi kurir saat ini.
    - Menghitung leg sungguhan hanya untuk pasangan stop berurutan, diproses
      paralel dengan konkurrensi terbatas.
    """
    from app.services.geocode import geocode_address
    from app.services.pathfinding.delivery_optimizer import (
        optimize_stop_order_hybrid,
    )

    redis = getattr(request.app.state, "redis", None)
    hub = ((payload.hub_origin.latitude, payload.hub_origin.longitude)
           if payload.hub_origin is not None else None)
    _start = await _resolve_delivery_start(request, redis, payload)
    start = (_start[0], _start[1])
    start_source = _start[2]
    mode = _normalize_mode(payload)
    last_mile = last_mile_enabled(payload)
    need_nodes = True

    stops = []
    failed = []
    for d in payload.deliveries:
        lat = d.latitude
        lon = d.longitude
        if lat is None or lon is None:
            coords = await geocode_address(redis, d.alamat)
            if coords is None:
                failed.append(d.alamat)
                continue
            lat, lon = coords
        stops.append({
            "package_id": d.package_id,
            "recipient_name": d.recipient_name,
            "service_type": d.service_type,
            "coordinate": (lat, lon),
        })
    if failed:
        raise HTTPException(
            status_code=400,
            detail="Gagal geocode alamat: " + "; ".join(failed[:5]) +
                   ("; ..." if len(failed) > 5 else ""),
        )

    service_types = [s["service_type"] for s in stops]
    order = await optimize_stop_order_hybrid(
        start, [s["coordinate"] for s in stops],
        service_types=service_types,
        road_cost_fn=lambda o, d: _ordering_road_distance(
            request.app, redis, mode, last_mile, o[0], o[1], d[0], d[1]),
        top_k=_ORDER_TOP_K,
        return_to_hub=payload.return_to_hub,
    )
    ordered = [stops[i] for i in order]

    incident_delay_minutes = max(0.0, _env_float("INCIDENT_DELAY_MINUTES", 3.0))
    from app.services.traffic.poller import _reference_graph
    from app.services.traffic.eta import eta_config
    eta_graph, eta_locations = _reference_graph(request.app)
    speed_kmh = eta_config()["mode_speed_kmh"]

    leg_specs = []
    prev_coord = start
    for stop in ordered:
        leg_specs.append((prev_coord, stop))
        prev_coord = stop["coordinate"]
    if payload.return_to_hub and ordered:
        leg_specs.append((prev_coord, {
            "package_id": None,
            "recipient_name": "Hub",
            "service_type": "REGULAR",
            "coordinate": hub if hub is not None else start,
        }))

    semaphore = asyncio.Semaphore(_DELIVERY_LEG_CONCURRENCY)
    results = await asyncio.gather(
        *(_process_single_leg(
            request.app, redis, semaphore, leg_index, stop, prev_coord,
            mode, last_mile, need_nodes, payload,
            incident_delay_minutes, speed_kmh, eta_graph, eta_locations)
          for leg_index, (prev_coord, stop) in enumerate(leg_specs)),
        return_exceptions=True,
    )

    first_error = next((r for r in results if isinstance(r, BaseException)), None)
    if first_error is not None:
        raise first_error

    legs = []
    total_dist = 0.0
    total_dur = 0.0
    warnings = []
    sources = []
    for r in results:
        legs.append(r["leg"])
        total_dist += r["distance_m"]
        total_dur += r["duration_s"]
        if r["warning"]:
            warnings.append(r["warning"])
        sources.append(r["source"])

    stops_resp = []
    for seq, idx in enumerate(order, start=1):
        stops_resp.append(OptimizedStop(
            stop_order=seq,
            package_id=stops[idx]["package_id"],
            recipient_name=stops[idx]["recipient_name"],
            service_type=stops[idx]["service_type"],
            latitude=stops[idx]["coordinate"][0],
            longitude=stops[idx]["coordinate"][1],
        ))

    response = OptimizedDeliveryRouteResponse(
        status="success",
        total_distance_km=round(total_dist / 1000.0, 2),
        total_duration_mins=round(total_dur / 60.0, 1),
        total_legs=len(legs),
        stops=stops_resp,
        legs=legs,
        source=sources[0] if sources else "demo",
        warning="; ".join(dict.fromkeys(warnings)) or None,
        start_source=start_source,
    )

    if nav_enabled(payload):
        route_id = _next_nav_route_id(request.app)
        response.route_id = route_id
        await set_nav_route(redis, _token_kurir_id(request) or 0, {
            "route_id": route_id,
            "kind": "multi",
            "mode": mode,
            "last_mile": last_mile,
            "total_distance_m": round(total_dist, 1),
            "total_eta_s": round(total_dur, 1),
            "legs": [
                {
                    "index": l.leg_index,
                    "stop_sequence_number": l.stop_sequence_number,
                    "package_id": l.package_id,
                    "recipient_name": l.recipient_name,
                    "encoded": encode_polyline(l.geometry, 5),
                    "dest": (l.geometry[-1][0], l.geometry[-1][1])
                            if l.geometry else None,
                    "eta_s": round(l.estimated_time_seconds or 0.0, 1),
                }
                for l in legs
            ],
        })
    return response


@router.post("/geofence-check", response_model=GeofenceCheckResponse,
             summary="Cek geofence posisi vs target stop",
             description=(
                 "Cek apakah posisi kurir (`current`) berada dalam radius "
                 "geofencing terhadap target stop (`target`) menggunakan jarak "
                 "haversine.\n\n"
                 "- **Wajib:** `current`, `target` (Coordinate).\n"
                 "- **Opsional:** `radius_m` (default 30 m).\n\n"
                 "Khusus fallback/verifikasi manual — untuk deteksi real-time "
                 "gunakan WebSocket `WS /api/v1/ws/driver/position`."))
async def geofence_check(payload: GeofenceCheckRequest):
    """Cek apakah posisi kurir berada dalam radius geofencing sebuah stop.

    Menggunakan jarak haversine (straight-line). Default radius 30 meter
    sesuai alur POD (Geofence Trigger Radius <= 30m).
    """
    d = haversine_distance(
        (payload.current.latitude, payload.current.longitude),
        (payload.target.latitude, payload.target.longitude),
    )
    return GeofenceCheckResponse(
        within_radius=d <= payload.radius_m,
        distance_m=round(d, 2),
        radius_m=payload.radius_m,
    )
