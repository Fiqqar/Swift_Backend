import asyncio

from fastapi import APIRouter, HTTPException

from app.schemas.pathfinding import RouteRequest, RouteResponse
from app.services.pathfinding.core_a_star import shortest_path
from app.services.pathfinding.graph_loader import (
    find_nearest_node,
    load_graph_covering,
)

router = APIRouter()


@router.post("/find-route", response_model=RouteResponse)
async def find_route(payload: RouteRequest):
    lat1, lon1 = payload.origin.latitude, payload.origin.longitude
    lat2, lon2 = payload.destination.latitude, payload.destination.longitude
    try:
        pg = await asyncio.to_thread(load_graph_covering, lat1, lon1, lat2, lon2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal memuat peta: {str(e)}")

    start_node = await asyncio.to_thread(find_nearest_node, lat1, lon1, pg.locations)
    goal_node = await asyncio.to_thread(find_nearest_node, lat2, lon2, pg.locations)

    if start_node is None or goal_node is None:
        raise HTTPException(status_code=400, detail="Lokasi di luar jangkauan peta!")

    node_path, total_distance = await asyncio.to_thread(
        shortest_path, pg, start_node, goal_node)

    if not node_path:
        raise HTTPException(status_code=404, detail="Rute tidak ditemukan!")

    route_coords = [pg.locations[node_id] for node_id in node_path]

    return RouteResponse(
        status="success",
        total_distance_meters=round(total_distance, 2),
        route_coordinates=route_coords,
        source=pg.source,
        warning=pg.warning,
        graph_radius_meters=pg.radius,
    )
