import logging
import math
import os
from time import perf_counter

from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, HTTPException, Request

from app.schemas.pathfinding import RouteRequest, RouteResponse
from app.services.cache_service import (
    get_route,
    graph_scope,
    load_penalties,
    route_key,
    set_route,
)
from app.services.pathfinding.core_a_star import haversine_distance
from app.services.pathfinding.core_engine import route as engine_route
from app.services.pathfinding.graph_loader import (
    AreaNotCoveredError,
    _HIERARCHICAL_MIN_M,
    _LOCAL_ROUTE_MAX_M,
    find_nearest_node,
    hierarchical_available,
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

router = APIRouter()

logger = logging.getLogger("pathfinding")

_COVER_MARGIN = 600.0
_MAX_SNAP_DIST = float(os.environ.get("MAX_SNAP_DIST", "1500"))


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

    Urutan:
      0. local level-1 (gang) dari tile bila jarak pendek (<= LOCAL_ROUTE_MAX_KM)
      1. path_graph (warmup kecil)
      2. region_graph (bbox region)
      3. hierarchical (tile local + base jalan utama) bila jarak jauh
      4. load_graph_covering (satu graf penutup) bila dynamic/prewarm
      5. fail-fast (di luar cakupan / non-PBF)
    """
    dist = haversine_distance((lat1, lon1), (lat2, lon2))
    if (dist <= _LOCAL_ROUTE_MAX_M
            and tiles_enabled()
            and tiles_contain(lat1, lon1, lat2, lon2)):
        return ("graph", load_local_graph_covering(lat1, lon1, lat2, lon2,
                                                   level=1))
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
    if (dist > _HIERARCHICAL_MIN_M
            and hierarchical_available(lat1, lon1, lat2, lon2)):
        return ("hierarchical", build_hierarchical(lat1, lon1, lat2, lon2))
    if (os.environ.get("OSMNX_ALLOW_DYNAMIC_LOAD", "0") == "1"
            or region_graph_cached(lat1, lon1, lat2, lon2)):
        return ("graph", load_graph_covering(lat1, lon1, lat2, lon2))
    raise AreaNotCoveredError(
        "Area di luar cakupan peta yang dimuat. Pre-warm cache dengan "
        "`python scripts/prewarm_route.py <lat1> <lon1> <lat2> <lon2>` "
        "atau perbesar REGION_GRAPH_BBOX di .env.")


def _hier_cache_key(lat1: float, lon1: float,
                    lat2: float, lon2: float, penalties) -> str:
    q = (round(lat1, 4), round(lon1, 4), round(lat2, 4), round(lon2, 4))
    scope = "hier:%s:%s:%s:%s" % q
    return route_key(scope, 0, 0, penalties)


async def _route_hierarchical(hier, redis, penalties,
                              lat1: float, lon1: float,
                              lat2: float, lon2: float):
    """Hitung rute hierarchical (dengan cache Redis). None bila tak ada rute."""
    key = _hier_cache_key(lat1, lon1, lat2, lon2, penalties)
    cached = await get_route(redis, key)
    if cached is not None:
        return RouteResponse(**cached)
    try:
        coords, total, source, warning = await run_in_threadpool(
            route_hierarchical, hier, penalties)
    except AreaNotCoveredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not coords:
        return None
    response = RouteResponse(
        status="success",
        total_distance_meters=round(total, 2),
        route_coordinates=coords,
        source=source,
        warning=warning,
        graph_radius_meters=None,
    )
    await set_route(redis, key, response.model_dump())
    return response


@router.post("/find-route", response_model=RouteResponse)
async def find_route(payload: RouteRequest, request: Request):
    lat1, lon1 = payload.origin.latitude, payload.origin.longitude
    lat2, lon2 = payload.destination.latitude, payload.destination.longitude
    try:
        plan = await run_in_threadpool(
            _resolve_plan, request.app, lat1, lon1, lat2, lon2)
    except AreaNotCoveredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal memuat peta: {str(e)}")

    redis = getattr(request.app.state, "redis", None)
    penalties = await load_penalties(redis)

    if plan[0] == "hierarchical":
        response = await _route_hierarchical(
            plan[1], redis, penalties, lat1, lon1, lat2, lon2)
        if response is None:
            raise HTTPException(status_code=404, detail="Rute tidak ditemukan!")
        return response

    pg = plan[1]
    start_node = await run_in_threadpool(
        find_nearest_node, lat1, lon1, pg.locations)
    goal_node = await run_in_threadpool(
        find_nearest_node, lat2, lon2, pg.locations)

    if start_node is None or goal_node is None:
        raise HTTPException(status_code=400, detail="Lokasi di luar jangkauan peta!")

    d_start = haversine_distance((lat1, lon1), pg.locations[start_node])
    d_goal = haversine_distance((lat2, lon2), pg.locations[goal_node])
    if d_start > _MAX_SNAP_DIST or d_goal > _MAX_SNAP_DIST:
        raise HTTPException(
            status_code=400,
            detail="Lokasi di luar jangkauan peta (tidak ada jalan di sekitar titik)!",
        )

    scope = graph_scope(pg)
    key = route_key(scope, start_node, goal_node, penalties)

    cached = await get_route(redis, key)
    if cached is not None:
        return RouteResponse(**cached)

    t_route = perf_counter()
    node_path, total_distance = await run_in_threadpool(
        engine_route, pg, start_node, goal_node, penalties)
    logger.info("[PERF] Rust Engine Pathfinding Search: %.1f ms",
                (perf_counter() - t_route) * 1000.0)

    if not node_path:
        # Graf lokal (tile) dapat terputus di batas tile/rect. Cadangan:
        # rute hierarchical (base graph tersambung lintas Jawa).
        if getattr(pg, "source", "").startswith("tile:"):
            try:
                hier = await run_in_threadpool(
                    build_hierarchical, lat1, lon1, lat2, lon2)
            except AreaNotCoveredError as e:
                raise HTTPException(status_code=400, detail=str(e))
            response = await _route_hierarchical(
                hier, redis, penalties, lat1, lon1, lat2, lon2)
            if response is not None:
                return response
        raise HTTPException(status_code=404, detail="Rute tidak ditemukan!")

    route_coords = [pg.locations[node_id] for node_id in node_path]

    response = RouteResponse(
        status="success",
        total_distance_meters=round(total_distance, 2),
        route_coordinates=route_coords,
        source=pg.source,
        warning=pg.warning,
        graph_radius_meters=pg.radius,
    )
    await set_route(redis, key, response.model_dump())
    return response
