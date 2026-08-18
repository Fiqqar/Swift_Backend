import logging

from fastapi import APIRouter, Request

from app.api.v1.response import err, ok
from app.models.driver_report import DriverReport
from app.schemas.driver_report import DriverReportCreate
from app.services import ai_agent

router = APIRouter(tags=["Driver Report"])

logger = logging.getLogger("pathfinding")

REPORT_RATE_LIMIT_MAX = max(
    1, ai_agent._env_int("REPORT_RATE_LIMIT_MAX", 5))
REPORT_RATE_LIMIT_WINDOW_S = max(
    1, ai_agent._env_int("REPORT_RATE_LIMIT_WINDOW_S", 300))


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


async def _rate_limited(redis, kurir_id: int) -> bool:
    """True bila kurir melebihi batas laporan per jendela waktu (fail-open).

    Pakai Redis `INCR` + `EXPIRE` pada key `rl:driver-report:{kurir_id}`.
    Bila Redis tidak tersedia / gagal, kembalikan False (tidak dibatasi) agar
    fitur tetap berjalan — filosofi non-fatal yang sama seperti modul lain.
    """
    if redis is None:
        return False
    key = "rl:driver-report:%s" % kurir_id
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, REPORT_RATE_LIMIT_WINDOW_S)
        return count > REPORT_RATE_LIMIT_MAX
    except Exception as exc:
        logger.warning("[REPORT] Rate limit Redis gagal; skip: %s", exc)
        return False


@router.post("/driver-reports")
async def create_driver_report(request: Request,
                               payload: DriverReportCreate):
    """Terima laporan kurir (insiden jalan) untuk dievaluasi agent.

    Body: `DriverReportCreate` (text <= 300 char, lat/lng tanpa NaN/Inf).
    Auth: Bearer token (kurir). Laporan disimpan dengan status `pending` dan
    diproses `internal_report_agent` di background. Rate-limit: maks
    `REPORT_RATE_LIMIT_MAX` (default 5) laporan per kurir per
    `REPORT_RATE_LIMIT_WINDOW_S` (default 300 detik) via Redis.
    """
    kurir_id = _token_kurir_id(request)
    if kurir_id is None:
        return err("Token tidak ada atau tidak valid", 401)
    redis = getattr(request.app.state, "redis", None)
    if await _rate_limited(redis, kurir_id):
        return err("Terlalu banyak laporan, coba lagi nanti", 429)
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
