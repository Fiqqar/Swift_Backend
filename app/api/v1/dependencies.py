from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.core.security import decode_access_token

bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Masukkan token dari endpoint POST /api/v1/auth/login",
)


async def get_session() -> AsyncIterator[AsyncSession]:
    try:
        async with SessionLocal() as session:
            yield session
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database tidak tersedia, coba lagi nanti",
        ) from exc


async def current_kurir(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
):
    """Mengambil kurir aktif dari token Bearer; 401 bila tidak valid."""
    from app.models.kurir import Kurir

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token tidak ada",
        )
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token tidak valid",
        )
    try:
        kurir_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token tidak valid",
        )
    kurir = await session.get(Kurir, kurir_id)
    if kurir is None or not kurir.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Akun tidak aktif",
        )
    return kurir


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