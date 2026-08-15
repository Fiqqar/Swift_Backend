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

class RouteIncident(BaseModel):
    type: Literal["road_closure", "congestion"]
    location: Tuple[float, float]
    coordinates: List[Tuple[float, float]] = []
    multiplier: float = 1.0
    delay_minutes: float | None = None
    description: str
    provider: str | None = None

class RouteOption(BaseModel):
    route_id: int = Field(ge=1)
    is_best: bool = False
    summary: str = "Rute utama"
    distance_km: float
    duration_mins: float
    total_distance_meters: float
    route_coordinates: List[Tuple[float, float]]
    estimated_time_seconds: float | None = None
    estimated_arrival: datetime | None = None
    traffic_segments: List[TrafficRouteSegment] = []
    incidents: List[RouteIncident] = []

class RouteOptionsResponse(BaseModel):
    status: str
    total_route: int
    routes: List[RouteOption]
    source: str = "demo"
    warning: str | None = None
    graph_radius_meters: int | None = None


ServiceType = Literal["EXPRESS", "REGULAR"]


class DeliveryStop(BaseModel):
    package_id: int | None = None
    recipient_name: str = ""
    service_type: ServiceType = "REGULAR"
    alamat: str = Field(min_length=1)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class OptimizedDeliveryRouteRequest(BaseModel):
    hub_origin: Coordinate
    deliveries: List[DeliveryStop] = Field(min_length=1)
    mode: VehicleMode | None = None
    last_mile_precision: bool | None = None
    dynamic_rerouting: bool | None = None
    skip_traffic: bool = False
    return_to_hub: bool = False


class OptimizedDeliveryLeg(BaseModel):
    leg_index: int = Field(ge=0)
    stop_sequence_number: int = Field(ge=1)
    package_id: int | None = None
    recipient_name: str = ""
    service_type: ServiceType = "REGULAR"
    geometry: List[Tuple[float, float]]
    distance_km: float
    duration_mins: float
    estimated_time_seconds: float | None = None
    traffic_segments: List[TrafficRouteSegment] = []
    incidents: List[RouteIncident] = []


class OptimizedStop(BaseModel):
    stop_order: int = Field(ge=1)
    package_id: int | None = None
    recipient_name: str = ""
    service_type: ServiceType = "REGULAR"
    latitude: float
    longitude: float


class OptimizedDeliveryRouteResponse(BaseModel):
    status: str
    total_distance_km: float
    total_duration_mins: float
    total_legs: int
    stops: List[OptimizedStop]
    legs: List[OptimizedDeliveryLeg]
    source: str = "demo"
    warning: str | None = None


class GeofenceCheckRequest(BaseModel):
    current: Coordinate
    target: Coordinate
    radius_m: float = Field(default=30, gt=0)


class GeofenceCheckResponse(BaseModel):
    within_radius: bool
    distance_m: float
    radius_m: float