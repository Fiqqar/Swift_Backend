import logging

from fastapi import APIRouter, Request

from app.api.v1.response import err, ok
from app.models.driver_report import DriverReport
from app.schemas.driver_report import DriverReportCreate

router = APIRouter(tags=["Driver Report"])

logger = logging.getLogger("pathfinding")


def _token_kurir_id(request: Request) -> int | None:
    """Ekstrak kurir_id dari token JWT (Authorization Bearer) bila ada."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    from app.core.security import decode_access_token

    payload = decode_access_token(auth[7:])
    if payload is None:
        return None
    try:
        return int(payload["sub"])
    except (KeyError, ValueError):
        return None


@router.post("/driver-reports")
async def create_driver_report(request: Request,
                               payload: DriverReportCreate):
    """Terima laporan kurir (insiden jalan) untuk dievaluasi agent.

    Body: `DriverReportCreate`. Auth: Bearer token (kurir). Laporan disimpan
    dengan status `pending` dan diproses `internal_report_agent` di background.
    """
    kurir_id = _token_kurir_id(request)
    if kurir_id is None:
        return err("Token tidak ada atau tidak valid", 401)
    from app.core.database import SessionLocal

    async with SessionLocal() as session:
        report = DriverReport(
            kurir_id=kurir_id,
            text=payload.text,
            latitude=payload.latitude,
            longitude=payload.longitude,
        )
        session.add(report)
        await session.commit()
        await session.refresh(report)
    return ok("Laporan diterima", {
        "id": report.id,
        "kurir_id": report.kurir_id,
        "status": report.status,
        "created_at": (report.created_at.isoformat()
                       if report.created_at else None),
    })
