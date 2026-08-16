from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.schemas.pathfinding import PolylineCoords
from app.services.cache_service import load_penalties, store_penalty

router = APIRouter()


class WeightUpdate(BaseModel):
    edge_id: int = Field(description="Wajib. ID edge (ruas jalan) pada graf.")
    multiplier: float = Field(
        gt=0,
        description="Wajib. Faktor pengali durasi edge (>0). Nilai `inf` "
                    "menandakan edge ditutup.")


class WeightAck(BaseModel):
    status: str
    edge_id: int
    multiplier: float


class PenaltyEntry(BaseModel):
    edge_id: int
    multiplier: float


class PenaltiesResponse(BaseModel):
    penalties: list[PenaltyEntry]


@router.post("/update-weight", response_model=WeightAck,
             summary="Set/memperbarui penalti traffic sebuah edge",
             description=(
                 "Menyimpan penalti (multiplier) untuk satu edge ke Redis, "
                 "dipakai saat pathfinding untuk memperlambat/menutup jalan.\n\n"
                 "- **Wajib:** `edge_id`, `multiplier` (>0; `inf` = jalan ditutup).\n"
                 "- Error `503` bila Redis tidak tersedia."))
async def update_weight(payload: WeightUpdate, request: Request):
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis tidak tersedia")
    try:
        await store_penalty(redis, payload.edge_id, payload.multiplier)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis error: {exc}")
    return WeightAck(status="ok", edge_id=payload.edge_id, multiplier=payload.multiplier)


@router.get("/penalties", response_model=PenaltiesResponse,
            summary="Daftar penalti traffic tersimpan",
            description=(
                "Menampilkan semua penalti edge yang tersimpan di Redis "
                "(penalti manual).\n\n"
                "- Error `503` bila Redis tidak tersedia."))
async def list_penalties(request: Request):
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis tidak tersedia")
    penalties = await load_penalties(redis)
    return PenaltiesResponse(penalties=[
        PenaltyEntry(edge_id=edge_id, multiplier=multiplier)
        for edge_id, multiplier in sorted(penalties.items())
    ])


class ProviderStatus(BaseModel):
    name: str
    active: bool


class TrafficStatusResponse(BaseModel):
    enabled: bool
    mode: str
    poller_running: bool
    providers: list[ProviderStatus]
    penalty_count: int


@router.get("/status", response_model=TrafficStatusResponse,
            summary="Status modul traffic",
            description=(
                "Menampilkan status konfigurasi traffic: mode provider "
                "(`off`/`auto`/`smart_hybrid`), apakah poller berjalan, daftar "
                "provider aktif, dan jumlah penalti."))
async def traffic_status(request: Request):
    from app.services.traffic.poller import (
        should_start_poller,
        traffic_poller_running,
    )
    from app.services.traffic.provider import get_providers, provider_mode

    redis = getattr(request.app.state, "redis", None)
    penalties = await load_penalties(redis)
    providers = get_providers()
    od_penalties = {}
    if provider_mode() == "smart_hybrid":
        from app.services.traffic.smart_hybrid import load_cached_penalties
        od_penalties = await load_cached_penalties(redis)
    return TrafficStatusResponse(
        enabled=should_start_poller(),
        mode=provider_mode(),
        poller_running=traffic_poller_running(request.app),
        providers=[
            ProviderStatus(name=p.name, active=_provider_active(p))
            for p in providers
        ],
        penalty_count=len(penalties) + len(od_penalties),
    )


def _provider_active(provider) -> bool:
    for attr in ("api_key", "url"):
        if hasattr(provider, attr):
            return bool(getattr(provider, attr))
    return getattr(provider, "active", True)


class MapSegment(BaseModel):
    edge_id: int
    multiplier: float
    closure: bool
    coordinates: PolylineCoords


class TrafficMapResponse(BaseModel):
    segments: list[MapSegment]
    source: str | None = None


@router.get("/map", response_model=TrafficMapResponse,
            summary="Peta segmen jalan yang kena penalti",
            description=(
                "Menghasilkan daftar segmen jalan yang sedang kena penalti "
                "beserta koordinatnya, berguna untuk visualisasi peta traffic."))
async def traffic_map(request: Request):
    from app.services.traffic.matcher import penalized_segments
    from app.services.traffic.poller import _reference_graph
    from app.services.traffic.provider import provider_mode
    from app.services.traffic.smart_hybrid import load_cached_penalties

    redis = getattr(request.app.state, "redis", None)
    penalties = await load_penalties(redis)
    if provider_mode() == "smart_hybrid":
        merged = dict(penalties)
        merged.update(await load_cached_penalties(redis))
        penalties = merged
    graph, locations = _reference_graph(request.app)
    if not penalties or graph is None or locations is None:
        return TrafficMapResponse(segments=[])

    raw = penalized_segments(graph, locations, penalties)
    segments = [
        MapSegment(
            edge_id=seg["edge_id"],
            multiplier=seg["multiplier"],
            closure=seg["closure"],
            coordinates=seg["coordinates"],
        )
        for seg in raw
    ]
    source = getattr(
        getattr(request.app.state, "region_graph", None) or
        getattr(request.app.state, "path_graph", None),
        "source", None)
    return TrafficMapResponse(segments=segments, source=source)
