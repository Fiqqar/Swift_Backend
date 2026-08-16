from datetime import datetime
from typing import Annotated, List, Literal, Tuple

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.services.polyline import encode_polyline

PolylineCoords = Annotated[
    List[Tuple[float, float]],
    PlainSerializer(lambda v: encode_polyline(v, 5),
                    return_type=str, when_used="json"),
]

class Coordinate(BaseModel):
    latitude: float = Field(
        ge=-90, le=90,
        description="Wajib. Latitude titik dalam derajat desimal (rentang -90 s/d 90).")
    longitude: float = Field(
        ge=-180, le=180,
        description="Wajib. Longitude titik dalam derajat desimal (rentang -180 s/d 180).")

VehicleMode = Literal["motorcycle", "car", "truck"]


class RouteRequest(BaseModel):
    origin: Coordinate = Field(
        description="Wajib. Titik asal rute.")
    destination: Coordinate = Field(
        description="Wajib. Titik tujuan rute.")
    mode: VehicleMode | None = Field(
        default=None,
        description="Opsional. Jenis kendaraan: `motorcycle`/`car`/`truck`. "
                    "Default: env `VEHICLE_MODE` (umumnya `car`). "
                    "Mempengaruhi pembatasan jalan yang bisa dilalui.")
    last_mile_precision: bool | None = Field(
        default=None,
        description="Opsional. Bila True, snap titik awal/akhir ke gang (last-mile) "
                    "untuk rute yang lebih presisi sampai depan alamat. "
                    "Default: env `ENABLE_LAST_MILE_PRECISION`.")
    dynamic_rerouting: bool | None = Field(
        default=None,
        description="Opsional. Bila True, aktifkan evaluasi rerouting dinamis. "
                    "Default: env `ENABLE_DYNAMIC_REROUTING`.")

    model_config = ConfigDict(json_schema_extra={
        "examples": [{
            "origin": {"latitude": -6.8048, "longitude": 110.8385},
            "destination": {"latitude": -6.8100, "longitude": 110.8500},
            "mode": "motorcycle",
            "last_mile_precision": True,
        }]
    })


class TrafficRouteSegment(BaseModel):
    start_index: int = Field(ge=0, description="Indeks awal segmen pada route_coordinates.")
    end_index: int = Field(ge=0, description="Indeks akhir segmen pada route_coordinates.")
    multiplier: float = Field(gt=0, description="Faktor pengali durasi segmen (>0).")

class RouteResponse(BaseModel):
    status: str
    total_distance_meters: float
    route_coordinates: PolylineCoords
    source: str = "demo"
    warning: str | None = None
    graph_radius_meters: int | None = None
    estimated_time_seconds: float | None = None
    estimated_arrival: datetime | None = None
    traffic_segments: List[TrafficRouteSegment] = []

class RouteIncident(BaseModel):
    type: Literal["road_closure", "congestion"]
    location: Tuple[float, float]
    coordinates: PolylineCoords = []
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
    route_coordinates: PolylineCoords
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
    package_id: int | None = Field(
        default=None,
        description="Opsional. ID paket yang diantar pada stop ini.")
    recipient_name: str = Field(
        default="",
        description="Opsional. Nama penerima untuk tampilan.")
    service_type: ServiceType = Field(
        default="REGULAR",
        description="Opsional. Jenis layanan: `EXPRESS` atau `REGULAR`. "
                    "Paket EXPRESS diutamakan (diurut lebih dulu) oleh optimizer TSP.")
    alamat: str = Field(
        min_length=1,
        description="Wajib. Alamat tujuan. Dipakai geocode (Nominatim) bila "
                    "`latitude`/`longitude` tidak diberikan.")
    latitude: float | None = Field(
        default=None, ge=-90, le=90,
        description="Opsional. Latitude stop. Bila kosong, di-geocode dari `alamat`.")
    longitude: float | None = Field(
        default=None, ge=-180, le=180,
        description="Opsional. Longitude stop. Bila kosong, di-geocode dari `alamat`.")


class OptimizedDeliveryRouteRequest(BaseModel):
    hub_origin: Coordinate | None = Field(
        default=None,
        description="Opsional. Titik awal rute = lokasi Hub. Dipakai sebagai "
                    "FALLBACK TERAKHIR bila `courier_position` dan posisi Redis "
                    "kurir tidak tersedia (perilaku eksisting).")
    courier_position: Coordinate | None = Field(
        default=None,
        description="Opsional. Posisi kurir saat ini — PRIORITAS TERTINGGI sebagai "
                    "titik awal rute. Menggantikan `hub_origin` bila diberikan.")
    deliveries: List[DeliveryStop] = Field(
        min_length=1,
        description="Wajib. Daftar stop pengantaran (minimal 1).")
    mode: VehicleMode | None = Field(
        default=None,
        description="Opsional. Jenis kendaraan: `motorcycle`/`car`/`truck`. "
                    "Default: env `VEHICLE_MODE`.")
    last_mile_precision: bool | None = Field(
        default=None,
        description="Opsional. Snap tiap leg ke gang (last-mile). "
                    "Default: env `ENABLE_LAST_MILE_PRECISION`.")
    dynamic_rerouting: bool | None = Field(
        default=None,
        description="Opsional. Aktifkan evaluasi rerouting dinamis per leg. "
                    "Default: env `ENABLE_DYNAMIC_REROUTING`.")
    skip_traffic: bool = Field(
        default=False,
        description="Opsional. Bila True, lewati pemakaian data lalu lintas. "
                    "Default: False.")
    return_to_hub: bool = Field(
        default=False,
        description="Opsional. Bila True, tambahkan leg pulang kembali ke hub "
                    "di akhir rute. Default: False.")

    model_config = ConfigDict(json_schema_extra={
        "examples": [{
            "hub_origin": {"latitude": -6.8048, "longitude": 110.8385},
            "courier_position": {"latitude": -6.8050, "longitude": 110.8390},
            "deliveries": [
                {
                    "package_id": 1,
                    "recipient_name": "Budi",
                    "service_type": "EXPRESS",
                    "alamat": "Jl. Kudus No. 1, Kota Kudus",
                    "latitude": -6.8100,
                    "longitude": 110.8500,
                },
                {
                    "package_id": 2,
                    "recipient_name": "Siti",
                    "service_type": "REGULAR",
                    "alamat": "Jl. Besito, Gebog, Kabupaten Kudus",
                },
            ],
            "mode": "motorcycle",
            "last_mile_precision": True,
            "skip_traffic": False,
            "return_to_hub": True,
        }]
    })


class OptimizedDeliveryLeg(BaseModel):
    leg_index: int = Field(ge=0)
    stop_sequence_number: int = Field(ge=1)
    package_id: int | None = None
    recipient_name: str = ""
    service_type: ServiceType = "REGULAR"
    geometry: PolylineCoords
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
    current: Coordinate = Field(
        description="Wajib. Posisi saat ini (mis. GPS kurir).")
    target: Coordinate = Field(
        description="Wajib. Titik target yang dicek (mis. koordinat stop).")
    radius_m: float = Field(
        default=30, gt=0,
        description="Opsional. Radius geofence dalam meter. Default: 30 m "
                    "(sesuai alur POD Geofence Trigger Radius <= 30m).")


class GeofenceCheckResponse(BaseModel):
    within_radius: bool
    distance_m: float
    radius_m: float
