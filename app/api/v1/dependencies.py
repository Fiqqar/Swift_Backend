from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.core.security import decode_access_token


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def current_kurir_or_error(
    request: Request, session: AsyncSession
) -> tuple:
    """Balikin (kurir, None) kalau token valid, atau (None, pesan error)."""
    from app.models.kurir import Kurir

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, "Token tidak ada"
    payload = decode_access_token(auth[7:])
    if payload is None:
        return None, "Token tidak valid"
    try:
        kurir_id = int(payload["sub"])
    except (KeyError, ValueError):
        return None, "Token tidak valid"
    kurir = await session.get(Kurir, kurir_id)
    if kurir is None or not kurir.is_active:
        return None, "Akun tidak aktif"
    return kurir, None
