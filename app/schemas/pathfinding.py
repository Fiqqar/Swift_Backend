from datetime import datetime
from typing import List, Literal, Tuple

from pydantic import BaseModel, Field

class Coordinate(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

VehicleMode = Literal["motorcycle", "car", "truck"]

class RouteRequest(BaseModel):
    origin: Coordinate
    destination: Coordinate
    mode: VehicleMode | None = None
    last_mile_precision: bool | None = None
    dynamic_rerouting: bool | None = None

class TrafficRouteSegment(BaseModel):
    start_index: int = Field(ge=0)
    end_index: int = Field(ge=0)
    multiplier: float = Field(gt=0)

class RouteResponse(BaseModel):
    status: str
    total_distance_meters: float
    route_coordinates: List[Tuple[float, float]]
    source: str = "demo"
    warning: str | None = None
    graph_radius_meters: int | None = None
    estimated_time_seconds: float | None = None
    estimated_arrival: datetime | None = None
    traffic_segments: List[TrafficRouteSegment] = []