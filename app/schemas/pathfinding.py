from pydantic import BaseModel
from typing import List, Tuple

class Coordinate(BaseModel):
    latitude: float
    longitude: float

class RouteRequest(BaseModel):
    origin: Coordinate
    destination: Coordinate

class RouteResponse(BaseModel):
    status: str
    total_distance_meters: float
    route_coordinates: List[Tuple[float, float]]
    source: str = "demo"
    warning: str | None = None
    graph_radius_meters: int | None = None