# Test Case Gabungan: Posisi Driver Realtime + Real-time Navigation & Auto-Reroute

Runbook manual untuk menguji **dua alur WebSocket dalam satu skenario berkelanjutan**:

```
WS /api/v1/ws/driver/position  (tracking)   → Redis driver:pos:{kurir_id}  (+ geofence)
WS /api/v1/ws/navigation       (navigation) → Redis driver:pos:{kurir_id}
                                              + driver:nav:{kurir_id} (snapshot rute)
                                              + route_progress / off_route_warning
                                              + auto_rerouted / reroute_available
```

Alur yang diuji:

1. **Fallback chain titik awal rute** (`_resolve_delivery_start`,
   `app/api/v1/endpoints/pathfinding.py:149`):

   | Prioritas | Sumber | Kondisi |
   |---|---|---|
   | 1 | `courier_position` (payload) | diberikan eksplisit |
   | 2 | `HGETALL driver:pos:{kurir_id}` | posisi terbaru dari WebSocket belum kedaluwarsa (TTL `KURIR_POS_TTL_SECONDS`) |
   | 3 | `hub_origin` (payload) | fallback terakhir |

2. **Snapshot rute aktif** (`driver:nav:{kurir_id}`, TTL `KURIR_NAV_TTL_SECONDS`)
   dibuat otomatis oleh endpoint HTTP bila `dynamic_rerouting: true`:
   - `POST /api/v1/pathfinding/find-route`
   - `POST /api/v1/pathfinding/find-route-options`
   - `POST /api/v1/pathfinding/find-optimized-delivery-route`

3. **Navigation WS**: `start_navigation` → `ack` berisi polyline leg aktif;
   `location_update` → `route_progress`, deteksi off-route, dan auto-reroute.

4. **Sinergi lintas-WS**: `location_update` pada WS navigation **juga menulis**
   `driver:pos:{kurir_id}` (`app/api/v1/endpoints/navigation.py:164`) sehingga satu
   koneksi navigation sudah memenuhi prioritas-2 fallback chain — WS tracking
   tidak wajib dibuka.

Base URL: `http://localhost:8000` • WS URL: `ws://localhost:8000`

---

## Pola Message WS (ringkasan)

Runbook ini memakai **dua** WebSocket. Keduanya persisten dua arah (**bukan
one-shot webhook**): client yang mengirim pesan, server membalas.

### `WS /api/v1/ws/navigation` — request (client → server)

| Message | Field | Balasan |
|---|---|---|
| `ping` | — | `ack` |
| `start_navigation` | `route_id` (wajib), `leg_index` (opsional, default 0) | `ack` + polyline leg aktif; `error` bila snapshot tak cocok |
| `location_update` | `lat` + `lng`/`lon` (wajib); `bearing`/`speed`/`current_route_id` opsional | `route_progress`; `off_route_warning` + `auto_rerouted`/`reroute_available` bila off-route |
| `complete_leg` / `pod_submitted` | — | `ack action=complete_leg` (leg baru) / `route_complete` (leg terakhir) |

> `current_route_id` di `location_update` tidak dibaca server. Detail alur & aturan
> `leg_index`: `docs/testcase/runbook_ai_reroute_agent.md` → "Pola Message & Aturan WS".

### `WS /api/v1/ws/driver/position` — request (client → server)

| Message | Field | Balasan |
|---|---|---|
| `ping` | — | `ack` |
| `position` | `lat` + `lon` (wajib; pakai `lon`, bukan `lng`); `bearing`/`speed` opsional | `ack` (`stored`/`snapped`); push `geofence_enter`/`geofence_exit` |

## 0. Prasyarat & Setup

1. Pastikan `.env` berisi:
   ```ini
   ENABLE_LIVE_TRACKING=1
   ENABLE_LIVE_NAVIGATION=1
   AUTO_REROUTE=1
   REDIS_HOST=redis
   REDIS_PORT=6379
   ```
2. Jalankan stack:
   ```bash
   docker compose up --build
   ```
   (db + redis + app; app di port 8000).
3. Verifikasi kedua fitur aktif:
   ```bash
   curl http://localhost:8000/api/v1/ws/driver/position/status
   # expect: {"enabled": true, "redis_connected": true, "max_rate_seconds": 3.0}

   curl http://localhost:8000/api/v1/ws/navigation/status
   # expect: {"enabled": true, "redis_connected": true, "off_route_threshold_m": 40.0,
   #          "reroute_cooldown_s": 30.0, "auto_reroute": true, "max_rate_seconds": 3.0}
   ```
   > `redis_connected` harus `true` pada keduanya. Bila `false`, posisi / snapshot
   > tidak akan tersimpan (lihat Troubleshooting).

## 1. Seed Data

Jalankan dari root repo (butuh DB up):

```bash
uv run python scripts/seed_kurir.py     # login: joko / rahasia123
uv run python scripts/seed_hub.py
uv run python scripts/seed_paket.py     # paket RESI-2026-0001..0005 (Kudus)
```

Opsional (hanya untuk langkah geofence di Bagian 10):

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

## 3. Hitung Rute Multi-Stop (Kunci: Membuat Snapshot Navigation)

> **PRASYARAT KRITIS** (ini penyebab paling umum gagal):
> 1. HTTP call **wajib** menyertakan header `Authorization: Bearer <token>`
>    yang **sama** dengan token yang dipakai di WebSocket. `kurir_id` diambil
>    dari token ini (pathfinding.py `_token_kurir_id`), **bukan** dari body.
> 2. `dynamic_rerouting: true` di payload **dianjurkan** tetapi hanya **wajib**
>    bila `ENABLE_LIVE_NAVIGATION=0`. Dengan `ENABLE_LIVE_NAVIGATION=1` (sudah
>    di-set di Bagian 0), snapshot `driver:nav:{kurir_id}` dibuat otomatis walau
>    field ini dihilangkan (`nav_enabled()` di `pathfinding.py:121`).

```bash
curl -X POST http://localhost:8000/api/v1/pathfinding/find-optimized-delivery-route \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "hub_origin": {"latitude": -6.8048, "longitude": 110.8385},
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
    "dynamic_rerouting": true,
    "skip_traffic": true
  }'
```

**Kriteria lulus:**

- Status `200`.
- Respons memuat **`route_id`** (angka positif) — dipakai sebagai
  `route_id` di WS navigation.
- `legs[0].geometry` berupa string encoded polyline precision 5.
- `stops` berisi semua stop (urutan TSP, EXPRESS didahulukan).

Cek snapshot di Redis:

```bash
docker compose exec redis redis-cli GET driver:nav:{KURIR_ID}
```

Harap melihat JSON berisi `route_id`, `kind:"multi"`, `mode`, `last_mile`,
`total_distance_m`, `total_eta_s`, dan `legs[]` (masing-masing punya `index`,
`stop_sequence_number`, `encoded`, `dest`, `eta_s`).

> Bila `route_id` di respons `null` atau `driver:nav:{KURIR_ID}` kosong → cek
> `ENABLE_LIVE_NAVIGATION=1` (atau sertakan `dynamic_rerouting: true`) dan pastikan
> token HTTP & WS sama (lihat Prasyarat Kritis).

## 4. Navigation WS — Start Navigation

Hubungkan ke `ws://localhost:8000/api/v1/ws/navigation?token=<TOKEN>`, lalu kirim:

```json
{"type":"start_navigation","route_id":<ROUTE_ID>,"leg_index":0}
```

**Kriteria lulus:** terima `ack`:

```json
{ "type":"ack","ok":true,"route_id":<ROUTE_ID>,"leg_index":0,
  "kind":"multi","off_route_threshold_m":40.0,
  "polyline":"<encoded polyline leg 0>","ts":... }
```

Decode `polyline` untuk mengambil koordinat yang dipakai pada langkah berikutnya
(contoh decoder Python di Bagian 5).

## 5. Location Update Mengikuti Jalur (Progress + Sinergi Tracking)

Kirim beberapa `location_update` di sepanjang polyline leg 0, dengan interval
≥ `NAV_POS_MAX_RATE_SECONDS` (default 3 dtk) antar pesan:

```python
import asyncio, json, time
import websockets

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

POLYLINE = "<polyline dari ack Bagian 4>"
ROUTE_ID = <ROUTE_ID>

async def main():
    TOKEN = "<TOKEN_DARI_LOGIN>"
    uri = f"ws://localhost:8000/api/v1/ws/navigation?token={TOKEN}"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "start_navigation",
                                  "route_id": ROUTE_ID, "leg_index": 0}))
        print(await ws.recv())   # ack

        pts = decode_polyline(POLYLINE)
        step = max(1, len(pts) // 5)
        for i in range(0, len(pts), step):
            p = pts[i]
            await ws.send(json.dumps({
                "type": "location_update",
                "lat": p[0], "lng": p[1],
                "bearing": 90, "speed": 20,
                "current_route_id": ROUTE_ID,
            }))
            print(await ws.recv())   # route_progress
            time.sleep(3.5)          # >= NAV_POS_MAX_RATE_SECONDS

asyncio.run(main())
```

**Kriteria lulus:**

- Setiap `location_update` membalas `route_progress` berisi `remaining_distance_m`,
  `remaining_time_s`, `progress_pct` yang **menurun/meningkat monoton** (progress_pct
  naik seiring mendekati dest leg).
- `HGETALL driver:pos:{KURIR_ID}` kini **terisi** (lat/lon/ts) — **tanpa pernah**
  membuka `WS /api/v1/ws/driver/position`. Ini bukti sinergi lintas-WS.

```bash
docker compose exec redis redis-cli HGETALL driver:pos:{KURIR_ID}
```

## 6. Deviasi → Off-route Warning + Auto-reroute

Kirim `location_update` yang sengaja **menyimpang** lebih dari
`OFF_ROUTE_THRESHOLD_M` (default 40 m) dari polyline leg aktif, misalnya geser
`+0.0015` derajat ke samping (~150-250 m, tergantung geometri polyline):

```json
{"type":"location_update","lat":<lat+0.0015>,"lng":<lng+0.0015>,
 "bearing":90,"speed":20,"current_route_id":<ROUTE_ID>}
```

> `current_route_id` **tidak dibaca server**. `distance_m` dihitung sebagai jarak
> tegak lurus titik ke polyline — tidak bisa dipatok tepat; yang penting
> `distance_m > threshold_m`.

**Kriteria lulus:** terima dua pesan:

```json
{ "type":"off_route_warning","ok":true,"route_id":<ROUTE_ID>,
  "distance_m": <angka > 40.0>, "threshold_m": 40.0, "ts":... }

{ "type":"auto_rerouted","ok":true,"route_id":<ROUTE_ID>,
  "polyline":"<encoded polyline baru>","saving_s":...,
  "eta_s":..., "applied":true, "reason":"off_route" }
```

> `auto_rerouted` **tidak selalu muncul** bila `compute_reroute` gagal (area di
> luar graf yang dimuat / graf belum di-prewarm) atau cooldown masih aktif. Untuk
> memastikan muncul, pre-warm area leg aktif:
> ```bash
> uv run python scripts/prewarm_route.py <lat_awal> <lon_awal> <lat_akhir> <lon_akhir>
> ```
> dan tunggu `REROUTE_COOLDOWN_SECONDS` (default 30 dtk) sejak reroute terakhir.

## 7. Kontrol Cooldown — Tidak Ada Reroute Ganda

Masih dalam `REROUTE_COOLDOWN_SECONDS` (default 30 dtk) sejak `auto_rerouted`,
kirim lagi `location_update` yang menyimpang. Harapkan:

- `off_route_warning` **tidak** terkirim lagi (edge-trigger: `off_route_active`
  masih `true`).
- `auto_rerouted` **tidak** terkirim (cooldown belum lewat).

**Kriteria lulus:** hanya balasan `route_progress` (atau tidak ada pesan), bukan
warning/reroute.

## 8. Kontrol `AUTO_REROUTE=0` — Mode Notify (Opsional)

1. Set `AUTO_REROUTE=0` di `.env`, restart app.
2. Ulangi Bagian 6 (deviasi > threshold).

**Kriteria lulus:** server mengirim `reroute_available` dengan `applied:false`
(bukan `auto_rerouted`):

```json
{ "type":"reroute_available","ok":true,"route_id":<ROUTE_ID>,
  "polyline":"<encoded polyline baru>","saving_s":...,
  "eta_s":..., "applied":false, "reason":"off_route" }
```

Keputusan menerapkan ada di klien (server tetap menghitung, tidak mengganti
`session.coords`).

## 9. Sinergi Lintas-WS — Prioritas-2 Tanpa Tracking WS

> Ini kunci penggabungan: posisi yang dikirim via `location_update` (WS
> navigation) **dipakai sebagai titik awal** fallback chain rute.

1. Pastikan posisi terakhir dari Bagian 5 masih tersimpan:
   ```bash
   docker compose exec redis redis-cli HGETALL driver:pos:{KURIR_ID}
   ```
2. Panggil `find-optimized-delivery-route` **dengan token**, **tanpa**
   `hub_origin` dan **tanpa** `courier_position` (posisi diambil dari Redis):
   ```bash
   curl -X POST http://localhost:8000/api/v1/pathfinding/find-optimized-delivery-route \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer $TOKEN" \
     -d '{
       "deliveries": [
         {"package_id": null, "recipient_name": "Agus",
          "service_type": "REGULAR", "alamat": "Jl. Sukun Raya, Kudus",
          "latitude": -6.75346, "longitude": 110.84357}
       ],
       "mode": "motorcycle",
       "skip_traffic": true
     }'
   ```

**Kriteria lulus:**

- Status `200`.
- Titik pertama hasil-decode `legs[0].geometry` ≈ posisi `location_update`
  terakhir yang dikirim via WS navigation (setelah snapping, selisih ≤ jarak snap
  maks), **bukan** Hub Kudus `(-6.8048, 110.8385)`.

## 10. Opsional — Push Geofence (Tetap via Tracking WS)

Geofence hanya diimplementasikan di `WS /api/v1/ws/driver/position` (tracking).
Ringkasan (detail lengkap: `docs/testcase/runbook_ws_realtime_route.md` Bagian 8):

1. Buka `WS /api/v1/ws/driver/position?token=<TOKEN>`.
2. Kirim `{"type":"position","lat":-6.8040,"lon":110.8380}` → hanya `ack`
   (belum ada event geofence).
3. Kirim `{"type":"position","lat":-6.75345,"lon":110.84355}` (dekat paket
   RESI-2026-0001, jarak ≤ 30 m) → terima `geofence_enter`:
   ```json
   { "type":"geofence_enter","package_id":<id paket>,"distance_m":2.0,"radius_m":30 }
   ```
4. Kirim posisi sama lagi → **tidak ada** duplikat `geofence_enter`.
5. Kirim posisi menjauh (>30 m) → terima `geofence_exit`.

## 11. Troubleshooting

| Gejala | Penyebab & Solusi |
|---|---|
| `WS navigation` ditutup kode `1008` | `ENABLE_LIVE_NAVIGATION` = 0. Set `1` di `.env`, restart app. |
| `WS driver/position` ditutup kode `1008` | `ENABLE_LIVE_TRACKING` = 0. Set `1` di `.env`, restart app. |
| Keduanya ditutup kode `4401` | Token invalid/tidak ada. Login ulang, pastikan `?token=...` benar. |
| `route_id` di respons `find-optimized-delivery-route` = `null` | Snapshot tidak dibuat: cek `ENABLE_LIVE_NAVIGATION=1` (atau sertakan `dynamic_rerouting: true`), token HTTP & WS harus sama. |
| `start_navigation` → `error "Route snapshot tidak ditemukan / tidak cocok"` | Snapshot `driver:nav:{kurir_id}` belum ada (TTL `KURIR_NAV_TTL_SECONDS` habis) atau `route_id` salah. Hitung ulang rute, pastikan token sama. |
| `start_navigation` → `error "Polyline leg tidak valid"` | Snapshot `legs[leg_index]` tidak punya `encoded`/`dest` valid (data korup). |
| `ack` berisi `"stored": false` / posisi tidak tersimpan | Redis tidak terhubung (`redis_connected: false`). Jalankan app di docker network (`REDIS_HOST=redis`). |
| `route_progress` tidak muncul | `last_progress_push` throttle `NAV_PROGRESS_MIN_INTERVAL_SECONDS` (3 dtk) — kirim lebih lambat. |
| `off_route_warning` hanya sekali walau terus menyimpang | Edge-trigger: `off_route_active` masih `true` sampai kurir kembali ke jalur. |
| `auto_rerouted` tidak muncul padahal off-route | Cooldown `REROUTE_COOLDOWN_SECONDS` aktif, atau `compute_reroute` gagal (area di luar graf/prewarm). Tunggu cooldown, pre-warm area leg. |
| WS via HTTP `GET` → `404` | Normal. Route WS hanya menerima handshake WebSocket, bukan HTTP. |
| `400 "Area di luar cakupan peta..."` | Region graph belum ter-prebuild. Jalankan `uv run python scripts/prewarm_route.py <lat1> <lon1> <lat2> <lon2>`. |
| Posisi tidak kepakai padahal sudah kirim WS | TTL `KURIR_POS_TTL_SECONDS` habis (>10 menit default) → kirim posisi ulang. |

## 12. Kriteria Lulus Keseluruhan

- [ ] `GET /api/v1/ws/driver/position/status` → `enabled:true` **dan** `redis_connected:true`.
- [ ] `GET /api/v1/ws/navigation/status` → `enabled:true` **dan** `redis_connected:true`.
- [ ] `find-optimized-delivery-route` + `dynamic_rerouting:true` + token → `200` dengan `route_id`; `driver:nav:{kurir_id}` terisi.
- [ ] `start_navigation` → `ack` berisi polyline leg aktif.
- [ ] `location_update` sepanjang jalur → `route_progress` monoton; `driver:pos:{kurir_id}` ikut terisi (tanpa tracking WS).
- [ ] Deviasi > threshold → `off_route_warning` + `auto_rerouted` (`applied:true`, `reason:"off_route"`).
- [ ] Dalam cooldown → tidak ada warning/reroute ganda.
- [ ] `AUTO_REROUTE=0` → `reroute_available` dengan `applied:false`.
- [ ] Rute tanpa `hub_origin`/`courier_position` + token → `200`, leg pertama berawal dari posisi WS navigation (prioritas-2).
- [ ] (Opsional) `geofence_enter`/`geofence_exit` via tracking WS tanpa duplikat.