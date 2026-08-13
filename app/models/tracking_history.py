from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TrackingHistory(Base):
    __tablename__ = "tracking_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("shipment.id"), index=True
    )
    event: Mapped[str] = mapped_column(String(50))
    keterangan: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hub_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
