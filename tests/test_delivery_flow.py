"""Integration test alur pengiriman kurir terhadap database PostgreSQL real.

Menjalankan init_db() (membuat tabel kalau belum ada), membuat data dummy
sendiri (kurir, hub, 2 paket), menguji alur lengkap lewat API, lalu membersihkan
data tsb (tidak menyentuh data yang sudah ada).

    uv run pytest tests/test_delivery_flow.py -q

Membutuhkan PostgreSQL yang sesuai konfigurasi `.env` (DB_*) dan jalankan
dari root repo.
"""

import asyncio
import os
import sys

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _BASE)


def _load_dotenv():
    env_path = os.path.join(_BASE, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

from sqlalchemy import text  # noqa: E402

from app.core.database import engine, init_db  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402

import httpx  # noqa: E402


def _rand():
    return os.urandom(4).hex()


async def _cleanup(ids):
    kurir_ids, hub_id, paket_ids = ids["kurir_ids"], ids["hub_id"], ids["paket_ids"]
    batch_ids, shipment_ids = ids["batch_ids"], ids["shipment_ids"]
    async with engine.begin() as conn:
        for sid in shipment_ids:
            await conn.execute(
                text("DELETE FROM tracking_history WHERE shipment_id = :s"), {"s": sid})
        for bid in batch_ids:
            await conn.execute(
                text("DELETE FROM shipment WHERE batch_id = :b"), {"b": bid})
            await conn.execute(text("DELETE FROM batch WHERE id = :b"), {"b": bid})
        for pid in paket_ids:
            await conn.execute(text("DELETE FROM paket WHERE id = :p"), {"p": pid})
        await conn.execute(text("DELETE FROM hub WHERE id = :h"), {"h": hub_id})
        for kid in kurir_ids:
            await conn.execute(text("DELETE FROM kurir WHERE id = :k"), {"k": kid})


async def _run():
    await init_db()

    base = _rand()
    phone1, phone2 = f"08{base}1", f"08{base}2"
    user1, user2 = f"pytest_{base}a", f"pytest_{base}b"
    resi_a, resi_b = f"TST-{base}-A", f"TST-{base}-B"

    ids = {
        "kurir_ids": [], "hub_id": None, "paket_ids": [],
        "batch_ids": [], "shipment_ids": [],
    }
    try:
        async with engine.begin() as conn:
            k1 = (await conn.execute(text(
                "INSERT INTO kurir (nama, nomor_telepon, kendaraan, username, "
                "password_hash, is_active) "
                "VALUES ('Test Kurir', :phone, 'motorcycle', :user, :pw, true) "
                "RETURNING id"),
                {"phone": phone1, "user": user1, "pw": hash_password("rahasia123")}
            )).scalar()
            k2 = (await conn.execute(text(
                "INSERT INTO kurir (nama, nomor_telepon, kendaraan, username, "
                "password_hash, is_active) "
                "VALUES ('Test Kurir 2', :phone, 'car', :user, :pw, true) "
                "RETURNING id"),
                {"phone": phone2, "user": user2, "pw": hash_password("rahasia123")}
            )).scalar()
            hub_id = (await conn.execute(text(
                "INSERT INTO hub (nama, is_active) VALUES ('Test Hub', true) "
                "RETURNING id"))).scalar()
            p1 = (await conn.execute(text(
                "INSERT INTO paket (nama, nomor_telepon, alamat, resi, "
                "jenis_pengiriman, cod, harga, ongkir) "
                "VALUES ('Pkt COD', :phone, 'Jl. Test 1', :resi, 'reguler', "
                "true, 125000, 15000) RETURNING id"),
                {"phone": phone1, "resi": resi_a})).scalar()
            p2 = (await conn.execute(text(
                "INSERT INTO paket (nama, nomor_telepon, alamat, resi, "
                "jenis_pengiriman, cod, harga, ongkir) "
                "VALUES ('Pkt Biasa', :phone, 'Jl. Test 2', :resi, 'reguler', "
                "false, 0, 20000) RETURNING id"),
                {"phone": phone1, "resi": resi_b})).scalar()
        ids.update(kurir_ids=[k1, k2], hub_id=hub_id, paket_ids=[p1, p2])

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test") as c:
            r = await c.post("/api/v1/auth/login",
                             json={"username": user1, "password": "rahasia123"})
            assert r.status_code == 200 and r.json()["success"], r.text
            token = r.json()["data"]["token"]

            r = await c.get("/api/v1/auth/me",
                            headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200, r.text
            assert r.json()["data"]["kurir"]["nama"] == "Test Kurir", r.text

            r = await c.get("/api/v1/kurir")
            assert r.status_code == 200, r.text
            assert any(x["nama"] == "Test Kurir" for x in r.json()["data"]), r.text
            r = await c.get("/api/v1/hubs")
            assert r.status_code == 200, r.text
            assert any(x["nama"] == "Test Hub" for x in r.json()["data"]), r.text

            r = await c.post("/api/v1/batches",
                             json={"kurir_id": k1, "paket_ids": [p1, p2],
                                   "hub_id": hub_id})
            assert r.status_code == 201, r.text
            body = r.json()["data"]
            bid = body["batch"]["id"]
            sids = [s["shipment_id"] for s in body["shipments"]]
            ids.update(batch_ids=[bid], shipment_ids=sids)
            assert len(sids) == 2, sids

            # kurir sama double batch -> 409
            r = await c.post("/api/v1/batches",
                             json={"kurir_id": k1, "paket_ids": [p1]})
            assert r.status_code == 409, r.text
            # paket yang sudah aktif tidak bisa ditugaskan lagi -> 409
            r = await c.post("/api/v1/batches",
                             json={"kurir_id": k2, "paket_ids": [p1]})
            assert r.status_code == 409, r.text

            r = await c.patch(f"/api/v1/batches/{bid}/status",
                              json={"status": "picked_up"})
            assert r.status_code == 200, r.text
            r = await c.get(f"/api/v1/shipments?batch_id={bid}")
            assert r.status_code == 200, r.text
            items = r.json()["data"]
            assert len(items) == 2, r.text
            assert all(s["status"] == "picked_up" for s in items), r.text

            cod_sid = next(s["shipment_id"] for s in items
                           if s["cod"]["status"] == "pending")
            norm_sid = next(s["shipment_id"] for s in items
                            if s["cod"]["status"] == "not_applicable")

            r = await c.patch(f"/api/v1/shipments/{cod_sid}/status",
                              json={"status": "delivered"})
            assert r.status_code == 200, r.text
            r = await c.get(f"/api/v1/shipments/{cod_sid}")
            assert r.json()["data"]["cod"]["status"] == "collected", r.text

            r = await c.patch(f"/api/v1/shipments/{norm_sid}/status",
                              json={"status": "delivered"})
            assert r.status_code == 200, r.text

            r = await c.get(f"/api/v1/shipments/{cod_sid}/tracking")
            assert r.status_code == 200, r.text
            events = [h["event"] for h in r.json()["data"]["history"]]
            assert "picked_up" in events and "delivered" in events, events

            r = await c.patch(f"/api/v1/shipments/{cod_sid}/cod",
                              json={"status": "remitted"})
            assert r.status_code == 200, r.text
            r = await c.patch(f"/api/v1/shipments/{cod_sid}/billing",
                              json={"status": "paid"})
            assert r.status_code == 200, r.text

            r = await c.get(f"/api/v1/batches/{bid}")
            assert r.status_code == 200, r.text
            assert len(r.json()["data"]["shipments"]) == 2, r.text

            # error auth seragam
            r = await c.get("/api/v1/auth/me")
            assert r.status_code == 401, r.text
            assert r.json()["success"] is False, r.text
            assert "message" in r.json(), r.text
    finally:
        await _cleanup(ids)


def test_delivery_flow():
    asyncio.run(_run())