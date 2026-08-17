# Test Case: Rute Pengiriman Paket dengan Data Realtime WebSocket

Runbook manual untuk menguji alur:

```
WS /api/v1/ws/driver/position (token JWT)  →  Redis driver:pos:{kurir_id}
POST /api/v1/pathfinding/find-optimized-delivery-route
   + header Authorization: Bearer <token>
   + TANPA courier_position & TANPA hub_origin
   → titik awal rute diambil dari posisi realtime di Redis (prioritas 2)
```

Target yang diuji (fallback chain di `_resolve_delivery_start`,
`app/api/v1/endpoints/pathfinding.py:129-154`):

| Prioritas | Sumber | Kondisi |
|---|---|---|
| 1 | `courier_position` (payload) | diberikan eksplisit |
| 2 | `HGETALL driver:pos:{kurir_id}` | posisi terbaru dari WebSocket belum kedaluwarsa (TTL `KURIR_POS_TTL_SECONDS`) |
| 3 | `hub_origin` (payload) | fallback terakhir |

Base URL: `http://localhost:8000` • WS URL: `ws://localhost:8000`

---

## Pola Message WS (ringkasan)

`WS /api/v1/ws/driver/position` adalah koneksi persisten dua arah (**bukan
one-shot webhook**): client yang mengirim pesan, server membalas.

| Message (client → server) | Field | Balasan |
|---|---|---|
| `ping` | — | `ack` `{"type":"ack","ok":true,"ts":...}` |
| `position` | `lat` + `lon` (wajib; pakai `lon`, bukan `lng`), `bearing`/`speed` (opsional) | `ack` dengan `stored`/`warning`/`snapped`; push `geofence_enter`/`geofence_exit` |

> - Pesan `position` berikutnya **diabaikan** bila dikirim <
>   `KURIR_POS_MAX_RATE_SECONDS` (default 3 dtk) dari update sebelumnya.
> - `position` dengan `lat`/`lon` tidak valid / tidak ada → `error`.
> - `type` tak dikenal → diabaikan senyap; JSON tidak valid → `error`.

## 0. Prasyarat & Setup

1. Pastikan `.env` berisi:
   ```ini
   ENABLE_LIVE_TRACKING=1
   REDIS_HOST=redis
   REDIS_PORT=6379
   ```
2. Jalankan stack:
   ```bash
   docker compose up --build
   ```
   (db + redis + app; app di port 8000).
3. Verifikasi live-tracking aktif:
   ```bash
   curl http://localhost:8000/api/v1/ws/driver/position/status
   # expect: {"enabled": true, "redis_connected": true, "max_rate_seconds": 3.0}
   ```
   > `redis_connected` harus `true`. Bila `false`, posisi yang dikirim via WS
   > tidak akan tersimpan (lihat Bagian 5 & Troubleshooting).

## 1. Seed Data

Jalankan dari root repo (butuh DB up):

```bash
uv run python scripts/seed_kurir.py     # login: joko / rahasia123
uv run python scripts/seed_hub.py
uv run python scripts/seed_paket.py     # paket RESI-2026-0001..0005 (Kudus)
```

Opsional (hanya untuk langkah geofence di Bagian 8):

```bash
uv run python scripts/seed_batch.py     # BATCH-SEED-2026-0001 milik joko
uv run python scripts/seed_shipment.py
```

## 2. Login & Catat Kurir ID

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"joko","password":"rahasia123"}'
```

Ambil `data.token` dan `data.kurir.id` dari respons. Contoh dengan PowerShell:

```powershell
$r = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/auth/login `
  -ContentType "application/json" -Body '{"username":"joko","password":"rahasia123"}'
$TOKEN = $r.data.token
$KURIR_ID = $r.data.kurir.id
Write-Output "TOKEN=$TOKEN"
Write-Output "KURIR_ID=$KURIR_ID"
```

Cek profil (validasi token):

```bash
curl http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN"
```

## 3. Kirim Posisi Realtime via WebSocket

Hubungkan ke WS dengan token, lalu kirim pesan posisi.

**Opsi A — Python (`websockets`, sudah terpasang):**

```python
import asyncio, json
import websockets

async def main():
    TOKEN = "<TOKEN_DARI_LOGIN>"
    uri = f"ws://localhost:8000/api/v1/ws/driver/position?token={TOKEN}"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "type": "position",
            "lat": -6.8060, "lon": 110.8390,
            "bearing": 90, "speed": 20,
        }))
        print(await ws.recv())   # expect {"type":"ack","ok":true,"stored":true,...}

        await ws.send(json.dumps({"type": "ping"}))
        print(await ws.recv())   # expect {"type":"ack","ok":true,"ts":...}

asyncio.run(main())
```

**Opsi B — `wscat`:**

```bash
npx wscat -c "ws://localhost:8000/api/v1/ws/driver/position?token=<TOKEN>"
# lalu kirim:
# {"type":"position","lat":-6.8060,"lon":110.8390,"bearing":90,"speed":20}
# expect: {"type":"ack","ok":true,"stored":true,...}
```

**Opsi C — Postman WebSocket:** `ws://localhost:8000/api/v1/ws/driver/position?token=<TOKEN>`.

> Catatan rate-limit: pesan `position` berikutnya diabaikan bila dikirim <
> `KURIR_POS_MAX_RATE_SECONDS` (default 3 detik) dari update sebelumnya.

## 4. Verifikasi Data di Redis

```bash
docker compose exec redis redis-cli HGETALL driver:pos:{KURIR_ID}
```

Harap melihat hash berisi `lat`, `lon`, `ts` (plus `bearing`/`speed`/`snapped`
bila terkirim / snapping aktif):

```
1) "lat"
2) "-6.806000"
3) "lon"
4) "110.839000"
5) "ts"
6) "1755..."
```

## 5. Panggil Rute TANPA hub_origin/courier_position (kunci pengujian)

> **PRASYARAT KRITIS** (ini penyebab paling umum gagal):
> 1. HTTP call **wajib** menyertakan header `Authorization: Bearer <token>`
>    yang **sama** dengan token yang dipakai di WebSocket. `kurir_id` diambil
>    dari token ini (pathfinding.py `_token_kurir_id`), **bukan** dari body.
>    - Di Postman: pastikan variabel `{{access_token}}` sudah terisi (jalankan
>      request `POST /auth/login` dulu) dan auth request ini memakai Bearer
>      token tersebut.
>    - Tanpa header/token valid → prioritas-2 dilewati → error `400`.
> 2. Posisi harus **benar-benar tersimpan** di Redis `driver:pos:{kurir_id}`.
>    - `ack` WS `ok:true` **tidak menjamin** tersimpan. Cek
>      `docker compose exec redis redis-cli HGETALL driver:pos:{KURIR_ID}`.
>    - Bila `GET /api/v1/ws/driver/position/status` memberi
>      `redis_connected: false`, posisi tidak akan pernah tersimpan (Redis
>      mati / hostname `redis` tidak resolve saat app dijalankan di luar
>      docker network).
>    - TTL default `KURIR_POS_TTL_SECONDS=600` (10 menit) — kirim posisi
>      ulang bila sudah lewat.

```bash
curl -X POST http://localhost:8000/api/v1/pathfinding/find-optimized-delivery-route \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "deliveries": [
      {"package_id": null, "recipient_name": "Agus",
       "service_type": "REGULAR", "alamat": "Jl. Sukun Raya, Kudus",
       "latitude": -6.75346, "longitude": 110.84357},
      {"package_id": null, "recipient_name": "Siti",
       "service_type": "EXPRESS", "alamat": "Jl. Bae-Besito, Kudus",
       "latitude": -6.72909, "longitude": 110.85232}
    ],
    "mode": "motorcycle",
    "last_mile_precision": true,
    "skip_traffic": true
  }'
```

**Kriteria lulus:**

- Status `200`.
- Titik pertama hasil-decode `legs[0].geometry` ≈ posisi yang dikirim via WS
  `(-6.8060, 110.8390)` (setelah snapping ke jalan terdekat, selisihnya ≤ jarak
  snap maks), **bukan** Hub Kudus `(-6.8048, 110.8385)`.
- `stops` berisi semua stop (urutan TSP, EXPRESS didahulukan).

Cek cepat dengan Python:

```python
import httpx

def decode_polyline(encoded, precision=5):
    factor = 10 ** precision
    coords, index, lat, lng = [], 0, 0, 0
    def delta(i):
        shift = result = 0
        while True:
            b = ord(encoded[i]) - 63; i += 1
            result |= (b & 0x1F) << shift; shift += 5
            if b < 0x20: break
        return (~(result >> 1) if (result & 1) else (result >> 1)), i
    while index < len(encoded):
        dlat, index = delta(index)
        dlng, index = delta(index)
        lat += dlat; lng += dlng
        coords.append((lat / factor, lng / factor))
    return coords

body = {"deliveries": [{"recipient_name": "Agus", "service_type": "REGULAR",
        "alamat": "Jl. Sukun Raya, Kudus",
        "latitude": -6.75346, "longitude": 110.84357},
       {"recipient_name": "Siti", "service_type": "EXPRESS",
        "alamat": "Jl. Bae-Besito, Kudus",
        "latitude": -6.72909, "longitude": 110.85232}],
       "mode": "motorcycle", "skip_traffic": True}
r = httpx.post("http://localhost:8000/api/v1/pathfinding/find-optimized-delivery-route",
               json=body, headers={"Authorization": "Bearer " + TOKEN})
data = r.json()
print("start leg 1:", decode_polyline(data["legs"][0]["geometry"])[0])  # harus ~(-6.8060, 110.8390)
print("total km:", data["total_distance_km"], "| legs:", data["total_legs"])
```

## 6. Kontrol Negatif — Tanpa Posisi WS (harus 400)

1. Hapus posisi kurir dari Redis:
   ```bash
   docker compose exec redis redis-cli DEL driver:pos:{KURIR_ID}
   ```
2. Panggil endpoint yang sama **dengan token** tapi tanpa `hub_origin`
   dan tanpa `courier_position`:
   ```bash
   curl -X POST http://localhost:8000/api/v1/pathfinding/find-optimized-delivery-route \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"deliveries": [{"recipient_name": "Agus", "alamat": "Jl. Test",
           "latitude": -6.75346, "longitude": 110.84357}]}'
   ```

   **Kriteria lulus:** status `400`. Detail pesan bervariasi sesuai penyebab:
   - Token valid tapi posisi Redis kosong →
     `"Titik awal rute tidak tersedia: posisi kurir {id} tidak ditemukan di Redis (kirim posisi via WebSocket WS /api/v1/ws/driver/position) dan hub_origin tidak diberikan."`
   - Header token tidak ada/tidak valid →
     `"Titik awal rute tidak tersedia: header Authorization: Bearer <token> kurir tidak ada/tidak valid, posisi Redis tidak bisa dibaca, dan hub_origin tidak diberikan."`

## 7. Kontrol Prioritas-1 — `courier_position` Eksplisit

Kirim ulang posisi via WS atau tidak, lalu panggil dengan `courier_position`
yang **berbeda**:

```bash
curl -X POST http://localhost:8000/api/v1/pathfinding/find-optimized-delivery-route \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "courier_position": {"latitude": -6.8075, "longitude": 110.8405},
    "deliveries": [{"recipient_name": "Agus", "alamat": "Jl. Test",
                     "latitude": -6.75346, "longitude": 110.84357}]
  }'
```

**Kriteria lulus:** `200` dan titik pertama hasil-decode `legs[0].geometry` ≈
`(-6.8075, 110.8405)` (menang atas posisi Redis/hub).

## 8. Opsional — Push Geofence (Perlu Batch + Shipment Seed)

Dengan `seed_batch.py` + `seed_shipment.py` (kurir `joko`, batch
`BATCH-SEED-2026-0001`, paket RESI-2026-0001 & 0002):

1. Kirim posisi jauh dari semua stop:
   `{"type":"position","lat":-6.8040,"lon":110.8380}` → hanya `ack`
   (belum ada event geofence).
2. Kirim posisi mendekati koordinat paket RESI-2026-0001
   `(-6.75346, 110.84357)` hingga jarak ≤ 30 m (radius `_GEOFENCE_RADIUS_M`):
   `{"type":"position","lat":-6.75345,"lon":110.84355}`.

   **Kriteria lulus:** terima event:
   ```json
   { "type": "geofence_enter", "package_id": <id paket>, "distance_m": 2.0, "radius_m": 30 }
   ```
3. Kirim posisi yang sama lagi → **tidak ada** `geofence_enter` duplikat
   (anti-spam, state `inside` masih tersimpan di Redis).
4. Kirim posisi menjauh (>30 m) → terima `geofence_exit`.

## 9. Troubleshooting

| Gejala | Penyebab & Solusi |
|---|---|
| `400 "Titik awal rute tidak tersedia: posisi kurir ... tidak ditemukan di Redis ..."` | Token valid tapi posisi WS tidak ada/kedaluwarsa di Redis. Kirim posisi via WS (pastikan `ack.stored: true`), cek `HGETALL driver:pos:{id}`, jangan lewat TTL 10 menit. |
| `400 "Titik awal rute tidak tersedia: header Authorization ... tidak ada/tidak valid ..."` | Header `Authorization: Bearer <token>` tidak terkirim/diisi (mis. `{{access_token}}` Postman belum ter-set atau token expire). Login ulang lalu set variabel. |
| `ack` berisi `"stored": false` + `"warning": "Redis tidak tersedia..."` | Redis tidak terhubung ke app (`redis_connected: false`). Jalankan app di docker network (`REDIS_HOST=redis`) atau perbaiki REDIS_URL. |
| WS ditutup kode `1008` | `ENABLE_LIVE_TRACKING` = 0. Set `1` di `.env`, restart app. |
| WS ditutup kode `4401` | Token invalid/tidak ada. Login ulang, pastikan `?token=...` benar. |
| WS via HTTP `GET` → `404` | Normal. Route WS hanya menerima handshake WebSocket, bukan HTTP. |
| `400 "Area di luar cakupan peta..."` | Region graph belum ter-prebuild. Jalankan `uv run python scripts/prewarm_route.py <lat1> <lon1> <lat2> <lon2>` atau tunggu region graph dimuat saat startup. |
| `ack` tanpa `snapped` | Graph belum siap; posisi mentah tetap tersimpan (`snapped: null`). |
| Posisi tidak kepakai padahal sudah kirim WS | TTL `KURIR_POS_TTL_SECONDS` habis (>10 menit default) → kirim posisi ulang. |

## 10. Kriteria Lulus Keseluruhan

- [ ] `GET /api/v1/ws/driver/position/status` → `enabled: true` **dan** `redis_connected: true`.
- [ ] WS menerima posisi → `ack ok:true` **dengan `stored: true`**.
- [ ] Redis `driver:pos:{kurir_id}` terisi (lat/lon/ts).
- [ ] Rute tanpa `hub_origin`/`courier_position` + token → `200`, leg pertama
      berawal dari posisi WS.
- [ ] Kontrol negatif (Redis kosong, tanpa hub/courier) → `400`.
- [ ] `courier_position` eksplisit menang atas posisi Redis.
- [ ] (Opsional) `geofence_enter`/`geofence_exit` push tanpa duplikat.
