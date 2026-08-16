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
from unittest.mock import patch  # noqa: E402


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
                "jenis_pengiriman, service_type, cod, harga, ongkir) "
                "VALUES ('Pkt COD', :phone, 'Jl. Test 1', :resi, 'express', "
                "'EXPRESS', true, 125000, 15000) RETURNING id"),
                {"phone": phone1, "resi": resi_a})).scalar()
            p2 = (await conn.execute(text(
                "INSERT INTO paket (nama, nomor_telepon, alamat, resi, "
                "jenis_pengiriman, service_type, cod, harga, ongkir) "
                "VALUES ('Pkt Biasa', :phone, 'Jl. Test 2', :resi, 'reguler', "
                "'REGULAR', false, 0, 20000) RETURNING id"),
                {"phone": phone1, "resi": resi_b})).scalar()
            p3 = (await conn.execute(text(
                "INSERT INTO paket (nama, nomor_telepon, alamat, resi, "
                "jenis_pengiriman, service_type, cod, harga, ongkir) "
                "VALUES ('Pkt Lain', :phone, 'Jl. Test 3', :resi, 'reguler', "
                "'REGULAR', false, 0, 10000) RETURNING id"),
                {"phone": phone2, "resi": f"TST-{base}-C"})).scalar()
        ids.update(kurir_ids=[k1, k2], hub_id=hub_id, paket_ids=[p1, p2, p3])

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test") as c:
            r = await c.post("/api/v1/auth/login",
                             json={"username": user1, "password": "rahasia123"})
            assert r.status_code == 200 and r.json()["success"], r.text
            token = r.json()["data"]["token"]

            r = await c.post("/api/v1/auth/login",
                             json={"username": user2, "password": "rahasia123"})
            assert r.status_code == 200 and r.json()["success"], r.text
            token2 = r.json()["data"]["token"]

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
            stypes = {s["paket"]["service_type"] for s in body["shipments"]}
            assert stypes == {"EXPRESS", "REGULAR"}, stypes

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
            r = await c.get(f"/api/v1/shipments?batch_id={bid}",
                            headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200, r.text
            items = r.json()["data"]
            assert len(items) == 2, r.text
            assert all(s["status"] == "picked_up" for s in items), r.text

            # batch kedua milik kurir lain (k2), paket p3
            r = await c.post("/api/v1/batches",
                             json={"kurir_id": k2, "paket_ids": [p3],
                                   "hub_id": hub_id})
            assert r.status_code == 201, r.text
            bid2 = r.json()["data"]["batch"]["id"]
            ids["batch_ids"].append(bid2)

            # GET /shipments otomatis filter berdasarkan token yang login
            r = await c.get("/api/v1/shipments")
            assert r.status_code == 401, r.text
            assert r.json()["success"] is False, r.text

            r = await c.get("/api/v1/shipments",
                            headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200, r.text
            k1_items = r.json()["data"]
            assert len(k1_items) == 2, r.text
            assert all(i["kurir_id"] == k1 for i in k1_items), r.text

            r = await c.get("/api/v1/shipments",
                            headers={"Authorization": f"Bearer {token2}"})
            assert r.status_code == 200, r.text
            k2_items = r.json()["data"]
            assert len(k2_items) == 1, r.text
            assert all(i["kurir_id"] == k2 for i in k2_items), r.text

            # token user2 tapi batch_id milik k1 -> tetap kosong
            r = await c.get(f"/api/v1/shipments?batch_id={bid}",
                            headers={"Authorization": f"Bearer {token2}"})
            assert r.status_code == 200, r.text
            assert r.json()["data"] == [], r.text

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

            r = await c.post(f"/api/v1/shipments/{cod_sid}/history",
                             json={"event": "pod_submitted",
                                   "recipient_name": "Penerima Test",
                                   "latitude": -6.8048, "longitude": 110.8385,
                                   "photo_urls": [
                                       "https://res.cloudinary.com/example/pod1.jpg",
                                       "https://res.cloudinary.com/example/pod2.jpg"]})
            assert r.status_code == 201, r.text
            r = await c.get(f"/api/v1/shipments/{cod_sid}/tracking")
            assert r.status_code == 200, r.text
            pod = next((h for h in r.json()["data"]["history"]
                        if h["event"] == "pod_submitted"), None)
            assert pod is not None, r.text
            assert pod["recipient_name"] == "Penerima Test", pod
            assert len(pod["photo_urls"]) == 2, pod
            assert pod["latitude"] == -6.8048, pod

            # upload POD via Cloudinary (mock; tidak butuh credential asli)
            fake_url = "https://res.cloudinary.com/test/pod-mock.jpg"
            u1, u2 = fake_url + "1", fake_url + "2"
            with patch("app.api.v1.endpoints.shipments.cloudinary_configured",
                       return_value=True), \
                 patch("app.api.v1.endpoints.shipments.upload_image",
                       side_effect=[u1, u2]):
                r = await c.post(
                    f"/api/v1/shipments/{cod_sid}/history/photo",
                    files=[
                        ("files", ("pod1.jpg", b"\xff\xd8\xff\xe0test1", "image/jpeg")),
                        ("files", ("pod2.png", b"\x89PNG\r\n\x1a\ntest2", "image/png")),
                    ],
                    data={"recipient_name": "Budi",
                          "latitude": "-6.8048", "longitude": "110.8385"})
                assert r.status_code == 201, r.text
                d = r.json()["data"]
                assert d["event"] == "pod_submitted", d
                assert d["photo_urls"] == [u1, u2], d
                assert d["recipient_name"] == "Budi", d
                assert d["latitude"] == -6.8048, d

            r = await c.get(f"/api/v1/shipments/{cod_sid}/tracking")
            assert r.status_code == 200, r.text
            pods = [h for h in r.json()["data"]["history"]
                    if h["event"] == "pod_submitted"]
            assert len(pods) == 2, pods
            assert any(h["photo_urls"] == [u1, u2] for h in pods), pods

            # shipment tidak ada -> 404
            with patch("app.api.v1.endpoints.shipments.cloudinary_configured",
                       return_value=True):
                r = await c.post(
                    "/api/v1/shipments/999999/history/photo",
                    files=[("files", ("pod.jpg", b"\xff\xd8\xff\xe0test", "image/jpeg"))])
                assert r.status_code == 404, r.text

            # Cloudinary belum dikonfigurasi -> 503
            with patch("app.api.v1.endpoints.shipments.cloudinary_configured",
                       return_value=False):
                r = await c.post(
                    f"/api/v1/shipments/{cod_sid}/history/photo",
                    files=[("files", ("pod.jpg", b"\xff\xd8\xff\xe0test", "image/jpeg"))])
                assert r.status_code == 503, r.text

            # tipe file tidak didukung (Content-Type) -> 415
            with patch("app.api.v1.endpoints.shipments.cloudinary_configured",
                       return_value=True):
                r = await c.post(
                    f"/api/v1/shipments/{cod_sid}/history/photo",
                    files=[("files", ("pod.txt", b"hello", "text/plain"))])
                assert r.status_code == 415, r.text

            # Content-Type dipalsukan (shell.php.jpg) tapi isi bukan gambar -> 415
            with patch("app.api.v1.endpoints.shipments.cloudinary_configured",
                       return_value=True):
                r = await c.post(
                    f"/api/v1/shipments/{cod_sid}/history/photo",
                    files=[("files", ("shell.php.jpg",
                                      b"<?php system($_GET['cmd']); ?>",
                                      "image/jpeg"))])
                assert r.status_code == 415, r.text
                assert "bukan file gambar" in r.json()["message"], r.text

            # salah satu file tidak valid -> seluruh request ditolak (all-or-nothing)
            with patch("app.api.v1.endpoints.shipments.cloudinary_configured",
                       return_value=True), \
                 patch("app.api.v1.endpoints.shipments.upload_image",
                       return_value=fake_url):
                r = await c.post(
                    f"/api/v1/shipments/{cod_sid}/history/photo",
                    files=[
                        ("files", ("ok.jpg", b"\xff\xd8\xff\xe0ok", "image/jpeg")),
                        ("files", ("bad.txt", b"not an image", "text/plain")),
                    ])
                assert r.status_code == 415, r.text
            r = await c.get(f"/api/v1/shipments/{cod_sid}/tracking")
            assert r.status_code == 200, r.text
            pods_after = [h for h in r.json()["data"]["history"]
                          if h["event"] == "pod_submitted"]
            assert len(pods_after) == len(pods), pods_after  # tidak ada riwayat baru

            # terlalu banyak file -> 400
            with patch("app.api.v1.endpoints.shipments.cloudinary_configured",
                       return_value=True):
                r = await c.post(
                    f"/api/v1/shipments/{cod_sid}/history/photo",
                    files=[("files", ("p.jpg", b"\xff\xd8\xff\xe0x", "image/jpeg"))
                           for _ in range(6)])
                assert r.status_code == 400, r.text

            # file kosong -> 400
            with patch("app.api.v1.endpoints.shipments.cloudinary_configured",
                       return_value=True):
                r = await c.post(
                    f"/api/v1/shipments/{cod_sid}/history/photo",
                    files=[("files", ("empty.jpg", b"", "image/jpeg"))])
                assert r.status_code == 400, r.text

            r = await c.post("/api/v1/pathfinding/geofence-check",
                             json={"current": {"latitude": -6.2, "longitude": 106.8},
                                   "target": {"latitude": -6.20001,
                                              "longitude": 106.80001},
                                   "radius_m": 30})
            assert r.status_code == 200, r.text
            assert r.json()["within_radius"] is True, r.text
            r = await c.post("/api/v1/pathfinding/geofence-check",
                             json={"current": {"latitude": -6.2, "longitude": 106.8},
                                   "target": {"latitude": -6.21, "longitude": 106.81},
                                   "radius_m": 30})
            assert r.json()["within_radius"] is False, r.text

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