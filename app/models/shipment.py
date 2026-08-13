from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Shipment(Base):
    __tablename__ = "shipment"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batch.id"), index=True
    )
    paket_id: Mapped[int] = mapped_column(
        ForeignKey("paket.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="assigned")
    cod_status: Mapped[str] = mapped_column(String(20), default="pending")
    cod_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    cod_collected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cod_remitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ongkir: Mapped[float] = mapped_column(Float, default=0.0)
    billing_status: Mapped[str] = mapped_column(String(20), default="unpaid")
    billing_paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    picked_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
