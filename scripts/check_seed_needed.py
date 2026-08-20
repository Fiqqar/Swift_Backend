"""Cek apakah seeding data dummy perlu dijalankan.

Return exit code 0 jika tabel inti (kurir, hub, paket) semuanya kosong,
exit code 1 jika sudah ada data. Dipakai entrypoint.sh untuk auto-seed:

    uv run python scripts/check_seed_needed.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import func, select  # noqa: E402

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.models.hub import Hub  # noqa: E402
from app.models.kurir import Kurir  # noqa: E402
from app.models.paket import Paket  # noqa: E402

logging.basicConfig(level=logging.INFO)

CORE_TABLES = [
    ("kurir", Kurir),
    ("hub", Hub),
    ("paket", Paket),
]


async def main() -> None:
    await init_db()
    counts = {}
    async with SessionLocal() as session:
        for name, model in CORE_TABLES:
            count = (await session.execute(
                select(func.count()).select_from(model))).scalar_one()
            counts[name] = count
    print(f"Jumlah data inti: {counts}")
    needed = all(count == 0 for count in counts.values())
    print("Seed dibutuhkan." if needed else "Data sudah ada, seed dilewati.")
    sys.exit(0 if needed else 1)


if __name__ == "__main__":
    asyncio.run(main())