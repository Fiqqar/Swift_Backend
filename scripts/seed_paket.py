"""Seed data dummy untuk tabel paket.

Menjalankan init_db() dulu (membuat tabel bila belum ada), lalu menyisipkan
beberapa baris paket contoh. Aman dijalankan ulang (skipsi jika resi sudah ada):

    uv run python scripts/seed_paket.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.models.paket import Paket, normalize_service_type  # noqa: E402

logging.basicConfig(level=logging.INFO)

DUMMY_PAKETS = [
    {"nama": "Agus Santoso", "nomor_telepon": "081234567890",
     "alamat": "Jalan Sukun Raya No.09, Besito Kulon, Besito, Kec. Gebog, Kabupaten Kudus, Jawa Tengah 59333",
     "resi": "RESI-2026-0001", "jenis_pengiriman": "reguler",
     "cod": True, "harga": 250000, "ongkir": 15000,
     "catatan": "Hubungi sebelum antar"},
    {"nama": "Siti Rahayu", "nomor_telepon": "081398765432",
     "alamat": "6RWV+M2H, Jl. Bae-Besito, Besito Kulon, Jurang, Kec. Gebog, Kabupaten Kudus, Jawa Tengah 59333",
     "resi": "RESI-2026-0002", "jenis_pengiriman": "express",
     "cod": False, "harga": 0, "ongkir": 20000, "catatan": None},
    {"nama": "Andi Wijaya", "nomor_telepon": "082111223344",
     "alamat": "MQMC+QWR Pesona Tropis Cirendeu, Jl. Cirendeu Indah I, Pisangan, Ciputat Timur, South Tangerang City, Banten 15419",
     "resi": "RESI-2026-0003", "jenis_pengiriman": "same_day",
     "cod": True, "harga": 500000, "ongkir": 25000,
     "catatan": "Barang fragile, jangan ditumpuk"},
    {"nama": "Dewi Lestari", "nomor_telepon": "085622334455",
     "alamat": "Jl. Sunan Kudus No.34, Kudus, Demaan, Kec. Kota Kudus, Kabupaten Kudus, Jawa Tengah 59313",
     "resi": "RESI-2026-0004", "jenis_pengiriman": "reguler",
     "cod": False, "harga": 0, "ongkir": 15000, "catatan": None},
    {"nama": "Usman Bin Myra", "nomor_telepon": "087833445566",
     "alamat": "Jl. Ahmad Yani No.58, Balong Barat, Beran, Kec. Ngawi, Kabupaten Ngawi, Jawa Timur 63216",
     "resi": "RESI-2026-0005", "jenis_pengiriman": "kargo",
     "cod": True, "harga": 1200000, "ongkir": 35000, "catatan": "COD tunai"},
]


async def main() -> None:
    await init_db()
    inserted = 0
    updated = 0
    async with SessionLocal() as session:
        for item in DUMMY_PAKETS:
            exists = await session.execute(
                select(Paket).where(Paket.resi == item["resi"]))
            paket = exists.scalar_one_or_none()
            if paket is not None:
                ongkir = float(item["ongkir"] or 0)
                service_type = normalize_service_type(item["jenis_pengiriman"])
                if paket.ongkir != ongkir or paket.service_type != service_type:
                    paket.ongkir = ongkir
                    paket.service_type = service_type
                    updated += 1
                continue
            session.add(Paket(
                **item,
                service_type=normalize_service_type(item["jenis_pengiriman"]),
            ))
            inserted += 1
        await session.commit()
    print(f"Seed selesai: {inserted} baris baru, {updated} diupdate, "
          f"{len(DUMMY_PAKETS) - inserted} sudah ada.")


if __name__ == "__main__":
    asyncio.run(main())
