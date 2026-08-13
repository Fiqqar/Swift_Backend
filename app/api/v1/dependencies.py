from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.core.security import decode_access_token


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def get_current_kurir(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    from app.models.kurir import Kurir

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token tidak ada")
    payload = decode_access_token(auth[7:])
    if payload is None:
        raise HTTPException(status_code=401, detail="Token tidak valid")
    try:
        kurir_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Token tidak valid")
    kurir = await session.get(Kurir, kurir_id)
    if kurir is None or not kurir.is_active:
        raise HTTPException(status_code=401, detail="Akun tidak aktif")
    return kurir
