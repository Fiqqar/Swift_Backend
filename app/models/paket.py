from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

EXPRESS_KEYWORDS = {
    "express", "same_day", "same-day", "next_day", "next-day",
}


def normalize_service_type(jenis_pengiriman: str | None) -> str:
    key = (jenis_pengiriman or "").strip().lower()
    return "EXPRESS" if key in EXPRESS_KEYWORDS else "REGULAR"


class Paket(Base):
    __tablename__ = "paket"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    nama: Mapped[str] = mapped_column(String(255))
    nomor_telepon: Mapped[str] = mapped_column(String(20))
    alamat: Mapped[str] = mapped_column(String(500))
    resi: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    jenis_pengiriman: Mapped[str] = mapped_column(String(50))
    service_type: Mapped[str] = mapped_column(String(20), default="REGULAR")
    cod: Mapped[bool] = mapped_column(Boolean, default=False)
    harga: Mapped[float] = mapped_column(Float, default=0.0)
    ongkir: Mapped[float] = mapped_column(Float, default=0.0)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    catatan: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
