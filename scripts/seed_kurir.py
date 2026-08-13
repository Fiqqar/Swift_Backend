"""Seed data dummy untuk tabel kurir.

Menjalankan init_db() dulu (membuat tabel bila belum ada), lalu menyisipkan
beberapa baris kurir contoh + akun login (username & password hash). Aman
dijalankan ulang (skip jika nomor_telepon sudah ada):

    uv run python scripts/seed_kurir.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.kurir import Kurir  # noqa: E402

logging.basicConfig(level=logging.INFO)

DUMMY_KURIR = [
    {"nama": "Joko Susilo", "nomor_telepon": "081234567890",
     "kendaraan": "motorcycle", "username": "joko",
     "password": "rahasia123"},
    {"nama": "Budi Hartono", "nomor_telepon": "082198765432",
     "kendaraan": "car", "username": "budi",
     "password": "rahasia123"},
    {"nama": "Sari Wulandari", "nomor_telepon": "085677889900",
     "kendaraan": "truck", "username": "sari",
     "password": "rahasia123"},
]


async def main() -> None:
    await init_db()
    inserted = 0
    existing = 0
    async with SessionLocal() as session:
        for item in DUMMY_KURIR:
            exists = await session.execute(
                select(Kurir.id).where(Kurir.nomor_telepon == item["nomor_telepon"]))
            if exists.scalar() is not None:
                existing += 1
                continue
            session.add(Kurir(
                nama=item["nama"],
                nomor_telepon=item["nomor_telepon"],
                kendaraan=item["kendaraan"],
                username=item["username"],
                password_hash=hash_password(item["password"]),
            ))
            inserted += 1
        await session.commit()
    print(f"Seed selesai: {inserted} baris baru, {existing} sudah ada.")


if __name__ == "__main__":
    asyncio.run(main())