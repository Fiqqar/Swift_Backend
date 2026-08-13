"""Seed data dummy untuk tabel hub.

Menjalankan init_db() dulu (membuat tabel bila belum ada), lalu menyisipkan
beberapa baris hub contoh. Aman dijalankan ulang (skip jika nama sudah ada):

    uv run python scripts/seed_hub.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.models.hub import Hub  # noqa: E402

logging.basicConfig(level=logging.INFO)

DUMMY_HUBS = [
    {"nama": "Hub Kudus", "alamat": "Jl. Raya Kudus, Kudus, Jawa Tengah",
     "latitude": -6.8048, "longitude": 110.8385},
    {"nama": "Hub Semarang", "alamat": "Jl. Pandanaran, Semarang, Jawa Tengah",
     "latitude": -6.9617, "longitude": 110.4195},
    {"nama": "Hub Ngawi", "alamat": "Jl. Ahmad Yani, Ngawi, Jawa Timur",
     "latitude": -7.4022, "longitude": 111.4447},
]


async def main() -> None:
    await init_db()
    inserted = 0
    existing = 0
    async with SessionLocal() as session:
        for item in DUMMY_HUBS:
            exists = await session.execute(
                select(Hub.id).where(Hub.nama == item["nama"]))
            if exists.scalar() is not None:
                existing += 1
                continue
            session.add(Hub(**item))
            inserted += 1
        await session.commit()
    print(f"Seed selesai: {inserted} baris baru, {existing} sudah ada.")


if __name__ == "__main__":
    asyncio.run(main())