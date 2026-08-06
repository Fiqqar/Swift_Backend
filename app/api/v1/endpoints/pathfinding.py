import asyncio

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
    find_nearest_node,
    load_graph_covering,
)

router = APIRouter()

_COVER_MARGIN = 600.0


def _covers(pg, lat: float, lon: float) -> bool:
    if pg is None:
        return False
    d = haversine_distance((pg.ref_lat, pg.ref_lon), (lat, lon))
    return d <= pg.radius + _COVER_MARGIN


def _resolve_graph(app, lat1: float, lon1: float, lat2: float, lon2: float):
    preload = getattr(app.state, "path_graph", None)
    if (preload is not None
            and _covers(preload, lat1, lon1)
            and _covers(preload, lat2, lon2)):
        return preload
    return load_graph_covering(lat1, lon1, lat2, lon2)


@router.post("/find-route", response_model=RouteResponse)
async def find_route(payload: RouteRequest, request: Request):
    lat1, lon1 = payload.origin.latitude, payload.origin.longitude
    lat2, lon2 = payload.destination.latitude, payload.destination.longitude
    try:
        pg = await asyncio.to_thread(_resolve_graph, request.app, lat1, lon1, lat2, lon2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal memuat peta: {str(e)}")

    start_node = await asyncio.to_thread(find_nearest_node, lat1, lon1, pg.locations)
    goal_node = await asyncio.to_thread(find_nearest_node, lat2, lon2, pg.locations)

    if start_node is None or goal_node is None:
        raise HTTPException(status_code=400, detail="Lokasi di luar jangkauan peta!")

    redis = getattr(request.app.state, "redis", None)
    penalties = await load_penalties(redis)
    scope = graph_scope(pg)
    key = route_key(scope, start_node, goal_node, penalties)

    cached = await get_route(redis, key)
    if cached is not None:
        return RouteResponse(**cached)

    node_path, total_distance = await asyncio.to_thread(
        engine_route, pg, start_node, goal_node, penalties)

    if not node_path:
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
