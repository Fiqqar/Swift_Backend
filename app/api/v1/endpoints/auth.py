from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import current_kurir_or_error, get_session
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


@router.post("/login", summary="Login kurir",
             description=(
                 "Autentikasi kurir dengan `username` + `password`. "
                 "Mengembalikan token JWT yang dipakai sebagai "
                 "`Authorization: Bearer <token>` untuk endpoint yang "
                 "membutuhkan autentikasi kurir (mis. `GET /api/v1/shipments`).\n\n"
                 "- **Wajib:** `username`, `password` (tidak boleh kosong).\n"
                 "- Error `401` bila kredensial salah, `422` bila kosong."))
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)):
    username = (payload.username or "").strip()
    if not username or not payload.password:
        return err("Username dan password wajib diisi", 422)

    try:
        result = await session.execute(select(Kurir).where(Kurir.username == username))
        kurir = result.scalar_one_or_none()
    except (SQLAlchemyError, OSError):
        return err("Database tidak tersedia, coba lagi nanti", 503)
    if kurir is None or not kurir.is_active or not kurir.password_hash:
        return err("Username atau password salah", 401)
    if not verify_password(payload.password, kurir.password_hash):
        return err("Username atau password salah", 401)

    token = create_access_token(kurir.id, kurir.username or "")
    return ok("Login berhasil", {"token": token, "kurir": _kurir_data(kurir)})


@router.get("/me", summary="Profil kurir dari token",
            description=(
                "Mengambil data kurir aktif berdasarkan token JWT pada header "
                "`Authorization: Bearer <token>`.\n\n"
                "- **Wajib:** header `Authorization: Bearer <token>`.\n"
                "- Error `401` bila token tidak ada/tidak valid/akun nonaktif."))
async def me(request: Request, session: AsyncSession = Depends(get_session)):
    kurir, error = await current_kurir_or_error(request, session)
    if error:
        return err(error, 401)
    return ok("Berhasil mengambil data kurir", {"kurir": _kurir_data(kurir)})
