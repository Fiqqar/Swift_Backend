from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_kurir, get_session
from app.api.v1.response import err, ok
from app.core.security import create_access_token, verify_password
from app.models.kurir import Kurir
from app.schemas.auth import LoginRequest

router = APIRouter(prefix="/auth", tags=["Auth"])


def _kurir_data(kurir: Kurir) -> dict:
    return {
        "id": kurir.id,
        "nama": kurir.nama,
        "username": kurir.username,
        "nomor_telepon": kurir.nomor_telepon,
        "kendaraan": kurir.kendaraan,
    }


@router.post("/login")
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)):
    username = (payload.username or "").strip()
    if not username or not payload.password:
        return err("Username dan password wajib diisi", 422)

    result = await session.execute(select(Kurir).where(Kurir.username == username))
    kurir = result.scalar_one_or_none()
    if kurir is None or not kurir.is_active or not kurir.password_hash:
        return err("Username atau password salah", 401)
    if not verify_password(payload.password, kurir.password_hash):
        return err("Username atau password salah", 401)

    token = create_access_token(kurir.id, kurir.username or "")
    return ok("Login berhasil", {"token": token, "kurir": _kurir_data(kurir)})


@router.get("/me")
async def me(kurir: Kurir = Depends(get_current_kurir)):
    return ok("Berhasil mengambil data kurir", {"kurir": _kurir_data(kurir)})
