"""Seed data dummy untuk tabel shipment.

Menjalankan init_db() dulu (membuat tabel bila belum ada), lalu menyisipkan
baris shipment contoh yang merujuk batch hasil scripts/seed_batch.py dan
paket hasil scripts/seed_paket.py. Aman dijalankan ulang (skip jika shipment
untuk pasangan batch+paket sudah ada):

    uv run --env-file .env python scripts/seed_shipment.py
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.models.batch import Batch  # noqa: E402
from app.models.paket import Paket  # noqa: E402
from app.models.shipment import Shipment  # noqa: E402

logging.basicConfig(level=logging.INFO)

DUMMY_SHIPMENTS = [
    {"batch_no": "BATCH-SEED-2026-0001", "resi": "RESI-2026-0001"},
    {"batch_no": "BATCH-SEED-2026-0001", "resi": "RESI-2026-0002"},
    {"batch_no": "BATCH-SEED-2026-0002", "resi": "RESI-2026-0003"},
    {"batch_no": "BATCH-SEED-2026-0002", "resi": "RESI-2026-0004"},
]


async def main() -> None:
    await init_db()
    inserted = 0
    existing = 0
    skipped = 0
    async with SessionLocal() as session:
        for item in DUMMY_SHIPMENTS:
            batch = (await session.execute(
                select(Batch).where(Batch.batch_no == item["batch_no"]))
            ).scalar_one_or_none()
            if batch is None:
                print(f"Skip resi {item['resi']}: batch '{item['batch_no']}' "
                      "belum ada. Jalankan scripts/seed_batch.py dulu.")
                skipped += 1
                continue
            paket = (await session.execute(
                select(Paket).where(Paket.resi == item["resi"]))
            ).scalar_one_or_none()
            if paket is None:
                print(f"Skip resi {item['resi']}: paket belum ada. "
                      "Jalankan scripts/seed_paket.py dulu.")
                skipped += 1
                continue
            exists = await session.execute(
                select(Shipment.id).where(
                    Shipment.batch_id == batch.id,
                    Shipment.paket_id == paket.id,
                )
            )
            if exists.scalar() is not None:
                existing += 1
                continue
            shipment = Shipment(
                batch_id=batch.id,
                paket_id=paket.id,
                status=batch.status,
                cod_status="pending" if paket.cod else "not_applicable",
                cod_amount=paket.harga if paket.cod else None,
                ongkir=paket.ongkir or 0.0,
                billing_status="unpaid",
            )
            if batch.status == "picked_up":
                shipment.picked_up_at = (
                    batch.picked_up_at or datetime.now(timezone.utc)
                )
            session.add(shipment)
            inserted += 1
        await session.commit()
    print(f"Seed selesai: {inserted} baris baru, {existing} sudah ada, "
          f"{skipped} dilewati.")


if __name__ == "__main__":
    asyncio.run(main())