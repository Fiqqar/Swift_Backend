from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DriverReport(Base):
    """Laporan kurir (insiden jalan) yang diproses oleh internal report agent.

    Alur: status `pending` -> `processed` (berdampak, telah dievaluasi) /
    `ignored` (di bawah ambang severity / koordinat tidak tersedia).
    """

    __tablename__ = "driver_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kurir_id: Mapped[int] = mapped_column(Integer, index=True)
    text: Mapped[str] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    radius_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
