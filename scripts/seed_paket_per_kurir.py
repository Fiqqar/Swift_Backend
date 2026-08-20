"""Seed paket per akun kurir (pinpoint map kurir).

Membuat paket dari daftar koordinat untuk tiap kurir, lalu men-assign-nya
via batch + shipment. Kurir harus sudah ada (scripts/seed_kurir.py). Aman
dijalankan ulang (skip jika resi/batch_no/shipment sudah ada):

    uv run python scripts/seed_kurir.py
    uv run python scripts/seed_paket_per_kurir.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.models.batch import Batch  # noqa: E402
from app.models.kurir import Kurir  # noqa: E402
from app.models.paket import Paket  # noqa: E402
from app.models.shipment import Shipment  # noqa: E402

logging.basicConfig(level=logging.INFO)

PAKET_PER_KURIR = {
    "joko": [
        (-6.753330, 110.843806),
        (-6.753602, 110.843317),
        (-6.753843, 110.842758),
        (-6.753334, 110.842941),
        (-6.753345, 110.842430),
        (-6.753473, 110.841475),
    ],
    "budi": [
        (-6.751477, 110.843848),
        (-6.751251, 110.843877),
        (-6.751619, 110.843760),
        (-6.751121, 110.843906),
        (-6.751009, 110.843639),
        (-6.7511883814804765, 110.84325184839764),
    ],
    "sari": [
        (-6.753833, 110.842745),
        (-6.757545, 110.844605),
        (-6.762151, 110.847320),
        (-6.767684, 110.848811),
        (-6.769359, 110.855085),
        (-6.770193, 110.860248),
        (-6.774935, 110.865966),
        (-6.778162, 110.867311),
        (-6.778028, 110.865704),
        (-6.778997, 110.865018),
    ],
}

HUB_NAMA = "Hub Kudus"
ALAMAT_TEMPLATE = "Jl. Contoh No.{n}, Kudus, Jawa Tengah"


async def _get_or_create_batch(session, username: str) -> tuple[Batch, bool]:
    batch_no = f"BATCH-AKUN-{username.upper()}"
    exists = await session.execute(
        select(Batch).where(Batch.batch_no == batch_no))
    batch = exists.scalar_one_or_none()
    if batch is not None:
        return batch, False
    kurir = (await session.execute(
        select(Kurir).where(Kurir.username == username))
    ).scalar_one_or_none()
    if kurir is None:
        return None, False
    batch = Batch(
        batch_no=batch_no,
        kurir_id=kurir.id,
        status="assigned",
        assigned_by="seed",
    )
    session.add(batch)
    await session.flush()
    return batch, True


async def main() -> None:
    await init_db()
    inserted_paket = 0
    inserted_shipment = 0
    skipped = 0
    missing_kurir = []
    async with SessionLocal() as session:
        for username, coords in PAKET_PER_KURIR.items():
            batch, batch_created = await _get_or_create_batch(session, username)
            if batch is None:
                missing_kurir.append(username)
                skipped += len(coords)
                continue

            for idx, (lat, lon) in enumerate(coords, start=1):
                resi = f"RESI-{username.upper()}-{idx:04d}"
                exists = await session.execute(
                    select(Paket.id).where(Paket.resi == resi))
                if exists.scalar() is not None:
                    skipped += 1
                    continue

                paket = Paket(
                    nama=f"Penerima {username.upper()} {idx:02d}",
                    nomor_telepon=f"08{1000000000 + idx}",
                    alamat=ALAMAT_TEMPLATE.format(n=idx),
                    resi=resi,
                    jenis_pengiriman="reguler",
                    service_type="REGULAR",
                    cod=False,
                    harga=0.0,
                    ongkir=15000.0,
                    latitude=lat,
                    longitude=lon,
                    catatan=None,
                )
                session.add(paket)
                await session.flush()
                inserted_paket += 1

                exists = await session.execute(
                    select(Shipment.id).where(
                        Shipment.batch_id == batch.id,
                        Shipment.paket_id == paket.id,
                    )
                )
                if exists.scalar() is not None:
                    skipped += 1
                    continue
                session.add(Shipment(
                    batch_id=batch.id,
                    paket_id=paket.id,
                    status="assigned",
                    cod_status="not_applicable",
                    ongkir=paket.ongkir,
                    billing_status="unpaid",
                ))
                inserted_shipment += 1

            if batch_created:
                print(f"Batch {batch.batch_no} dibuat untuk kurir {username}.")
        await session.commit()
    print(
        f"Seed selesai: {inserted_paket} paket baru, "
        f"{inserted_shipment} shipment baru, {skipped} dilewati."
    )
    if missing_kurir:
        print(
            f"Kurir tidak ditemukan: {', '.join(missing_kurir)}. "
            "Jalankan scripts/seed_kurir.py dulu."
        )


if __name__ == "__main__":
    asyncio.run(main())