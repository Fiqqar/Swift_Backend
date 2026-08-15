from typing import Literal

from pydantic import BaseModel, Field

BatchStatus = Literal["picked_up", "delivered", "returned"]
ShipmentStatus = Literal["picked_up", "delivered", "failed", "returned"]
CodStatus = Literal["remitted"]
BillingStatus = Literal["paid", "refunded"]
HistoryEvent = Literal["received_at_hub", "departed_hub", "pod_submitted"]


class BatchAssignRequest(BaseModel):
    paket_ids: list[int] = Field(min_length=1, description="ID paket yang di-assign")
    kurir_id: int = Field(gt=0)
    hub_id: int | None = Field(default=None, gt=0)


class BatchStatusUpdate(BaseModel):
    status: BatchStatus


class ShipmentStatusUpdate(BaseModel):
    status: ShipmentStatus


class CodUpdate(BaseModel):
    status: CodStatus


class BillingUpdate(BaseModel):
    status: BillingStatus


class HistoryCreate(BaseModel):
    event: HistoryEvent
    keterangan: str | None = None
    hub_id: int | None = Field(default=None, gt=0)
    recipient_name: str | None = Field(default=None, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    photo_urls: list[str] | None = Field(
        default=None, max_length=20, description="URL foto POD (multiple, mis. Cloudinary)"
    )
