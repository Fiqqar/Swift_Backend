"""Seed data dummy untuk tabel batch.

Menjalankan init_db() dulu (membuat tabel bila belum ada), lalu menyisipkan
beberapa baris batch contoh yang merujuk kurir & hub hasil seed
(scripts/seed_kurir.py & scripts/seed_hub.py). Aman dijalankan ulang
(skip jika batch_no sudah ada):

    uv run --env-file .env python scripts/seed_batch.py
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
from app.models.hub import Hub  # noqa: E402
from app.models.kurir import Kurir  # noqa: E402

logging.basicConfig(level=logging.INFO)

DUMMY_BATCHES = [
    {"batch_no": "BATCH-SEED-2026-0001", "username": "joko",
     "hub_nama": "Hub Kudus", "status": "assigned"},
    {"batch_no": "BATCH-SEED-2026-0002", "username": "budi",
     "hub_nama": "Hub Semarang", "status": "picked_up"},
]


async def main() -> None:
    await init_db()
    inserted = 0
    existing = 0
    skipped = 0
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        for item in DUMMY_BATCHES:
            exists = await session.execute(
                select(Batch.id).where(Batch.batch_no == item["batch_no"]))
            if exists.scalar() is not None:
                existing += 1
                continue
            kurir = (await session.execute(
                select(Kurir).where(Kurir.username == item["username"]))
            ).scalar_one_or_none()
            if kurir is None:
                print(f"Skip {item['batch_no']}: kurir '{item['username']}' "
                      "belum ada. Jalankan scripts/seed_kurir.py dulu.")
                skipped += 1
                continue
            hub = (await session.execute(
                select(Hub).where(Hub.nama == item["hub_nama"]))
            ).scalar_one_or_none()
            if hub is None:
                print(f"Skip {item['batch_no']}: hub '{item['hub_nama']}' "
                      "belum ada. Jalankan scripts/seed_hub.py dulu.")
                skipped += 1
                continue
            batch = Batch(
                batch_no=item["batch_no"],
                kurir_id=kurir.id,
                hub_id=hub.id,
                status=item["status"],
                assigned_by="seed",
            )
            if item["status"] == "picked_up":
                batch.picked_up_at = now
            session.add(batch)
            inserted += 1
        await session.commit()
    print(f"Seed selesai: {inserted} baris baru, {existing} sudah ada, "
          f"{skipped} dilewati.")


if __name__ == "__main__":
    asyncio.run(main())