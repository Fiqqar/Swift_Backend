from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BatchStatus = Literal["picked_up", "delivered", "returned"]
ShipmentStatus = Literal["picked_up", "delivered", "failed", "returned"]
CodStatus = Literal["remitted"]
BillingStatus = Literal["paid", "refunded"]
HistoryEvent = Literal["received_at_hub", "departed_hub", "pod_submitted"]


class BatchAssignRequest(BaseModel):
    paket_ids: list[int] = Field(
        min_length=1,
        description="Wajib. Daftar ID paket yang di-assign ke kurir (minimal 1).")
    kurir_id: int = Field(
        gt=0,
        description="Wajib. ID kurir penerima batch. Kurir harus aktif dan "
                    "belum punya batch aktif.")
    hub_id: int | None = Field(
        default=None, gt=0,
        description="Opsional. ID hub tempat paket diambil (Drop Point).")

    model_config = ConfigDict(json_schema_extra={
        "examples": [{
            "paket_ids": [1, 2, 3],
            "kurir_id": 1,
            "hub_id": 1,
        }]
    })


class BatchStatusUpdate(BaseModel):
    status: BatchStatus = Field(
        description="Wajib. Status baru batch. Transisi yang valid: "
                    "`assigned → picked_up → delivered`, atau `returned` "
                    "dari `assigned`/`picked_up`. Meng-update semua shipment "
                    "dalam batch secara otomatis.")


class ShipmentStatusUpdate(BaseModel):
    status: ShipmentStatus = Field(
        description="Wajib. Status baru shipment. Transisi yang valid: "
                    "`assigned → picked_up → delivered`, plus `failed` (dari "
                    "picked_up) dan `returned` (dari assigned/picked_up). "
                    "Menulis TrackingHistory.")


class CodUpdate(BaseModel):
    status: CodStatus = Field(
        description="Wajib. Hanya `remitted` (dana COD telah disetor). "
                    "Membutuhkan status COD `collected` sebelumnya "
                    "(otomatis saat shipment delivered).")


class BillingUpdate(BaseModel):
    status: BillingStatus = Field(
        description="Wajib. Status billing: `paid` (sudah dibayar) atau "
                    "`refunded` (dikembalikan).")


class HistoryCreate(BaseModel):
    event: HistoryEvent = Field(
        description="Wajib. Event tracking: `received_at_hub`, `departed_hub`, "
                    "atau `pod_submitted`.")
    keterangan: str | None = Field(
        default=None,
        description="Opsional. Keterangan/deskripsi event.")
    hub_id: int | None = Field(
        default=None, gt=0,
        description="Opsional. ID hub terkait event (mis. saat diterima/keluar hub).")
    recipient_name: str | None = Field(
        default=None, max_length=255,
        description="Opsional. Nama penerima (umumnya untuk event pod_submitted).")
    latitude: float | None = Field(
        default=None, ge=-90, le=90,
        description="Opsional. Latitude titik saat event terjadi.")
    longitude: float | None = Field(
        default=None, ge=-180, le=180,
        description="Opsional. Longitude titik saat event terjadi.")
    photo_urls: list[str] | None = Field(
        default=None, max_length=20,
        description="Opsional. URL foto POD (multiple, mis. dari Cloudinary).")
