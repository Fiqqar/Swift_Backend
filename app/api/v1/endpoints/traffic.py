from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.cache_service import load_penalties, store_penalty

router = APIRouter()


class WeightUpdate(BaseModel):
    edge_id: int
    multiplier: float = Field(gt=0)


class WeightAck(BaseModel):
    status: str
    edge_id: int
    multiplier: float


class PenaltyEntry(BaseModel):
    edge_id: int
    multiplier: float


class PenaltiesResponse(BaseModel):
    penalties: list[PenaltyEntry]


@router.post("/update-weight", response_model=WeightAck)
async def update_weight(payload: WeightUpdate, request: Request):
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis tidak tersedia")
    try:
        await store_penalty(redis, payload.edge_id, payload.multiplier)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis error: {exc}")
    return WeightAck(status="ok", edge_id=payload.edge_id, multiplier=payload.multiplier)


@router.get("/penalties", response_model=PenaltiesResponse)
async def list_penalties(request: Request):
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis tidak tersedia")
    penalties = await load_penalties(redis)
    return PenaltiesResponse(penalties=[
        PenaltyEntry(edge_id=edge_id, multiplier=multiplier)
        for edge_id, multiplier in sorted(penalties.items())
    ])
