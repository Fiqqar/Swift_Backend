from pydantic import BaseModel, ConfigDict, Field


class DriverReportCreate(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=300,
        description="Wajib. Deskripsi insiden/laporan kurir (maks 300 karakter; "
                    "mis. 'Jalan tertutup, banjir setinggi lutut di depan "
                    "minimarket').")
    latitude: float | None = Field(
        default=None, ge=-90, le=90, allow_inf_nan=False,
        description="Opsional. Latitude lokasi insiden (tidak boleh "
                    "NaN/±Inf). Bila kosong, diperoleh dari klasifikasi AI / "
                    "geocode alamat.")
    longitude: float | None = Field(
        default=None, ge=-180, le=180, allow_inf_nan=False,
        description="Opsional. Longitude lokasi insiden (tidak boleh "
                    "NaN/±Inf).")

    model_config = ConfigDict(json_schema_extra={
        "examples": [{
            "text": "Jalan tertutup total, pohon tumbang di Jl. Sunan Muria",
            "latitude": -6.8048,
            "longitude": 110.8385,
        }]
    })


class DriverReportResponse(BaseModel):
    id: int
    kurir_id: int
    status: str
    created_at: str | None = None
