# Desain: Real-time Tracking Posisi Kurir (GPS)

> Dokumen desain (design doc). Tidak ada perubahan kode dalam dokumen ini —
> hanya spesifikasi untuk implementasi di masa depan.

## 1. Latar Belakang & Masalah

Saat ini endpoint `/api/v1/pathfinding/find-optimized-delivery-route` selalu
menghitung rute mulai dari **hub** (`hub_origin`), bukan dari posisi kurir:

- `app/api/v1/endpoints/pathfinding.py:625` — `hub = payload.hub_origin` dipakai
  sebagai titik awal TSP dan leg pertama (`prev = hub`, baris 671).
- Payload `OptimizedDeliveryRouteRequest` hanya berisi `hub_origin` +
  `deliveries` (`app/schemas/pathfinding.py:78`), tidak ada konsep posisi kurir.

Dampak:
1. Jika kurir sudah berangkat dari hub, urutan stop hasil TSP dihitung dari titik
   yang salah → rute tidak optimal.
2. Frontend "mengakali" dengan mengirim posisi GPS kurir (`curPos`) ke field
   `hub_origin` saat recalculating (`app/ui/delivery.js:233`). Ini hack yang
   membuktikan backend seharusnya punya konsep posisi kurir secara eksplisit.
3. Infrastruktur sudah disiapkan sebagian tapi mati:
   - `ENABLE_LIVE_TRACKING` (`pathfinding.py:109`) + `app.state.active_route`
     tidak pernah dikonsumsi.
   - `_MAX_OFF_ROUTE_DISTANCE_METERS` (`pathfinding.py:73`) tidak pernah dipakai.
   - `docs/feature/live_update_position.md` hanya 11 baris, belum diimplementasi.
   - Paket `websockets==16.1.1` sudah terpasang namun tidak digunakan.

## 2. Tujuan

- Menyediakan jalur (transport) untuk menerima posisi kurir secara **real-time**.
- Menyimpan posisi terbaru kurir di **Redis** (cache, tanpa persisten ke DB).
- Menjadikan posisi kurir sebagai **titik awal alternatif** rute
  `/find-optimized-delivery-route` (menggantikan `hub_origin` bila tersedia).
- Meletakkan fondasi untuk fitur turunan: *dynamic rerouting* saat menyimpang,
  geofence server-side, dan dashboard monitoring armada.

### Non-goals (di luar lingkup dokumen ini)

- Penyimpanan riwayat posisi permanen di Postgres (keputusan: **Redis saja**).
- Map matching penuh (hanya membahas integrasi dengan utilitas snapping yang ada).
- Implementasi kode — dokumen ini murni spesifikasi.

## 3. Arsitektur

```
┌─────────────────┐        WS handshake + JWT        ┌──────────────────────┐
│  Driver App UI  │ ───────────────────────────────► │  FastAPI WebSocket   │
│ (GPS + bearing) │     WS /ws/driver/position        │  /ws/driver/position │
└─────────────────┘                                   └──────────┬───────────┘
                                                                 │ validasi + snap
                                                                 ▼
                                                        ┌──────────────────────┐
                                                        │        Redis         │
                                                        │ driver:pos:{id}  (hash│
                                                        │  lat, lon, bearing,  │
                                                        │  speed, ts)  TTL     │
                                                        └──────────┬───────────┘
                                                                   │ baca
                                   ┌───────────────────────────────┴───────────────┐
                                   ▼                                               ▼
                     ┌──────────────────────────┐                    ┌──────────────────────────┐
                     │ Pathfinding              │                    │ Dashboard Dispatch       │
                     │ find-optimized-delivery- │                    │ (peta armada real-time)  │
                     │ route (courier_position) │                    │ GET /kurir/positions     │
                     └──────────────────────────┘                    └──────────────────────────┘
```

Aliran data:
1. Driver App membuka koneksi WebSocket `WS /ws/driver/position` dengan token
   JWT pada header (auth kurir via `current_kurir`, `app/api/v1/dependencies.py:21`).
2. App mengirim pesan JSON `{ type: "position", lat, lon, bearing, speed }`
   setiap beberapa detik.
3. FastAPI memvalidasi koordinat, melakukan **snapping** opsional ke edge jalan
   terdekat, lalu menulis hash ke Redis.
4. Konsumen (pathfinding & dashboard) membaca posisi terbaru dari Redis.

## 4. Desain WebSocket

### 4.1 Endpoint

- Path: `WS /api/v1/driver/position` (konsisten dengan prefix `/api/v1`)
  atau `WS /api/v1/ws/driver/position`.
- Auth: token JWT via header `Authorization: Bearer <token>` saat handshake.
  Server menolak koneksi bila token invalid (status 401/close code).
- Rate limit: maksimal ~1 pesan / 3 detik per koneksi (pesan di luar itu diabaikan).

### 4.2 Format pesan (JSON text)

**Client → Server:**

```json
{
  "type": "position",
  "lat": -6.200000,
  "lon": 106.816666,
  "bearing": 132.5,
  "speed": 28.4,
  "ts": 1755259200
}
```

| Field     | Tipe   | Wajib | Deskripsi                          |
|-----------|--------|-------|------------------------------------|
| `type`    | string | ya    | `"position"` (atau `"ping"` utk keep-alive) |
| `lat`     | float  | ya    | Latitude (-90..90)                 |
| `lon`     | float  | ya    | Longitude (-180..180)              |
| `bearing` | float  | tidak | Arah gerak derajat (0-360)         |
| `speed`   | float  | tidak | Kecepatan km/jam                   |
| `ts`      | int    | tidak | Unix timestamp; default = server now |

**Server → Client:**

```json
{
  "type": "ack",
  "ok": true,
  "snapped": [-6.20005, 106.81671],
  "ts": 1755259200
}
```

- `snapped` diisi koordinat hasil map-matching bila snapping diaktifkan.
- Jika validasi gagal: `{ "type": "error", "ok": false, "detail": "..." }`.

## 5. Penyimpanan Redis (tanpa DB)

- Key: `driver:pos:{kurir_id}` — hash.
- Fields:

| Field      | Tipe   | Contoh          |
|------------|--------|-----------------|
| `lat`      | float  | `-6.200000`     |
| `lon`      | float  | `106.816666`    |
| `bearing`  | float  | `132.5`         |
| `speed`    | float  | `28.4`          |
| `ts`       | int    | `1755259200`    |
| `snapped`  | string | `"-6.20005,106.81671"` |

- TTL: `KURIR_POS_TTL_SECONDS` (default `600` = 10 menit) → `EXPIRE` tiap update.
  Posisi yang kedaluwarsa dianggap "tidak melaporkan" (offline).
- Tidak ada tabel baru di Postgres; `init_db()` (`app/core/database.py`) tidak berubah.
- Client Redis memakai `app/core/redis.py` (`get_redis()`).

Contoh operasi:
- Tulis: `HSET driver:pos:{id} lat ... lon ... ts ...` lalu `EXPIRE driver:pos:{id} 600`.
- Baca: `HGETALL driver:pos:{id}`.

### Geofence state key (per paket)

- Key: `driver:geofence:{kurir_id}:{package_id}` — string.
- Value: `"inside"` atau `"outside"`.
- TTL: `KURIR_GEOFENCE_TTL_SECONDS` (default `86400` = 24 jam, mengikuti durasi
  aktif sesi/batch pengiriman; atau reuse `KURIR_POS_TTL_SECONDS` bila batch
  berdurasi lebih pendek). TTL diperpanjang tiap kali key diperbarui
  (`SET ... EX <ttl>`).

Contoh operasi:
- Set: `SET driver:geofence:1:12 inside EX 86400`.
- Baca: `GET driver:geofence:1:12`.
- Hapus saat paket terkirim: `DEL driver:geofence:1:12`.

## 6. Integrasi dengan Pathfinding

### 6.1 Titik awal alternatif (fallback chain)

Tambahkan field opsional `courier_position: Coordinate | None` pada
`OptimizedDeliveryRouteRequest` (`app/schemas/pathfinding.py:78`).

Penentuan titik awal rute (`start_point`) memakai **hirarki prioritas**
berikut (fallback chain):

| Prioritas | Sumber                                | Kondisi                                  |
|-----------|---------------------------------------|------------------------------------------|
| 1         | `courier_position` (payload request)  | diberikan eksplisit (bukan null)         |
| 2         | `HGETALL driver:pos:{kurir_id}`       | Redis punya posisi terbaru belum kedaluwarsa |
| 3         | `hub_origin` (payload request)        | fallback terakhir (perilaku eksisting)   |

Implementasi di `find_optimized_delivery_route` (`pathfinding.py:609`):
- `start = courier_position or redis_position or hub_origin`
- `optimize_stop_order(start, ...)` dan `prev = start` (ganti baris 671).

Sehingga `hub_origin` bisa dijadikan **opsional** bila prioritas 1 atau 2
tersedia (hindari breaking change: biarkan ketiganya opsional, minimal
klien memastikan satu dari tiga sumber ada).

### 6.2 Pemanfaatan Redis oleh pathfinding

- `redis_position` dibaca dari `HGETALL driver:pos:{kurir_id}` **hanya** jika
  prioritas 1 (`courier_position`) tidak diberikan.
- `kurir_id` diambil dari token JWT kurir yang aktif (`current_kurir`,
  `app/api/v1/dependencies.py:21`) — bukan dari body.
- Posisi Redis dianggap valid bila field `lat` & `lon` ada dan `ts` masih
  segar (TTL `KURIR_POS_TTL_SECONDS` belum lewat). Jika kosong/kedaluwarsa →
  lanjut ke prioritas 3 (`hub_origin`).

### 6.3 Map matching / snapping

- Snap koordinat mentah ke edge jalan terdekat memakai utilitas yang sudah ada:
  `snap_point_to_graph` (`app/services/pathfinding/snap.py:87`),
  `k_nearest_nodes` (`snap.py:24`), atau `find_nearest_node`
  (`app/services/pathfinding/graph_loader.py`).
- Snapping **wajib dijalankan di thread pool** (`run_in_threadpool`) agar tidak
  memblokir event loop WebSocket.
- Jika graph belum siap (base graph tidak tersedia), lewati snapping dan simpan
  koordinat mentah dengan flag `snapped=false`.

## 6.4 Geofence Detection Server-Side (Push, bukan Polling)

Alternatif pengganti pola polling klien `/geofence-check`. Server mendeteksi
sendiri saat kurir mendekati stop menggunakan posisi dari WebSocket.

### Alur

```
Driver App            WS /driver/position           FastAPI                 Redis / DB
    │  position{lat,lon} ──────────────────────────► │
    │                                               │ snap + HSET driver:pos
    │                                               │ baca daftar stop belum terkirim
    │                                               │ (Redis cache / DB batch aktif)
    │                                               │ hitung haversine ke tiap stop
    │                                               │ GET driver:geofence:{id}:{pid}
    │                                               │   → cek transisi state di Redis
    │◄────────────────── geofence_enter ────────────│ (hanya saat outside→inside)
```

1. Driver App mengirim `{ "type": "position", ... }` (lihat 4.2).
2. Server snap & simpan ke Redis, lalu hitung jarak haversine ke **semua stop
   yang belum terkirim** pada batch aktif kurir.
3. Untuk tiap stop, server membaca state Redis
   (`GET driver:geofence:{kurir_id}:{package_id}`):
   - Jika jarak ≤ radius (default 30 m) dan state **bukan** `"inside"` →
     `SET ... inside`, lalu push `geofence_enter`.
   - Jika jarak > radius dan state **adalah** `"inside"` →
     `SET ... outside`, lalu push `geofence_exit`.
   - Jika tidak terjadi transisi → tidak ada event (anti-spam).
4. UI mencocokkan `package_id` pada event dengan stop aktif; jika cocok,
   aktifkan tombol "Konfirmasi Terkirim".

### Resolusi target stop

- Daftar stop = shipment batch aktif kurir dengan status **bukan** `delivered`
  (`assigned`, `picked_up`, `failed`, `returned`).
- Koordinat tiap stop diambil dari `Paket.latitude/longitude`
  (`app/models/paket.py:31`) lewat relasi `Shipment → Batch → Kurir`
  (pola query scoping sama dengan `GET /shipments`,
  `app/api/v1/endpoints/shipments.py:377-393`).
- Tidak perlu urutan rute tersimpan di server — UI yang mencocokkan
  `package_id` dengan stop aktif.

### Format event push (Server → Client)

```json
{
  "type": "geofence_enter",
  "package_id": 12,
  "distance_m": 22.4,
  "radius_m": 30
}
```

```json
{
  "type": "geofence_exit",
  "package_id": 12,
  "distance_m": 45.1,
  "radius_m": 30
}
```

### State machine per stop (di Redis, bukan in-memory)

- State tiap `package_id` disimpan di Redis:
  `driver:geofence:{kurir_id}:{package_id}` = `"inside"` / `"outside"`
  (lihat Bagian 5).
- Sebelum mengirim `geofence_enter`/`geofence_exit`, server **wajib**:
  1. `GET driver:geofence:{kurir_id}:{package_id}` (baca state saat ini),
  2. hitung transisi berdasarkan jarak vs radius,
  3. `SET ... EX <ttl>` dengan state baru,
  4. kirim event hanya bila terjadi transisi.
- **Handling reconnect WS:** karena state tersimpan di Redis (bukan memori
  koneksi), saat kurir *reconnect* dan masih berada di dalam radius, state
  tetap terbaca `"inside"` → tidak ada transisi → **tidak ada `geofence_enter`
  duplikat** (anti-spam).
- TTL mengikuti durasi sesi/batch aktif (`KURIR_GEOFENCE_TTL_SECONDS`, default
  `86400`); key dihapus saat paket berstatus `delivered`.

### Cache Redis per kurir

- Key: `driver:stops:{kurir_id}` — daftar stop belum terkirim (JSON:
  `[{package_id, lat, lon}]`), TTL pendek (mis. `KURIR_STOPS_TTL_SECONDS=300`).
- Invalidasi saat `PATCH /shipments/{id}/status` menandai `delivered`
  (`app/api/v1/endpoints/shipments.py:443`) → `DEL driver:stops:{kurir_id}`.
- Bila cache kosong, query DB sekali lalu tulis cache (backfill).

### Peran `/geofence-check` (keep as fallback)

Endpoint `/geofence-check` (`app/api/v1/endpoints/pathfinding.py:767`)
**dipertahankan** sebagai:

- Fallback bila koneksi WebSocket terputus / `ENABLE_LIVE_TRACKING=False`.
- Verifikasi manual sebelum POD (double-check server-side).
- Kompatibilitas API untuk klien lama.

### Perbandingan polling vs push

| Aspek        | Polling `/geofence-check`      | WS push server-side            |
|--------------|--------------------------------|--------------------------------|
| Latency      | Tergantung interval fetch      | Real-time (event-driven)       |
| Bandwidth    | Request HTTP berulang          | Hanya 1 koneksi + event kecil  |
| Ketahanan    | Tetap jalan tanpa WS           | Bergantung koneksi WS          |
| Keandalan    | Bergantung klien jujur kirim posisi | Server punya posisi otoritatif |

## 7. Konfigurasi (env)

| Variabel                       | Default | Deskripsi                                  |
|--------------------------------|---------|--------------------------------------------|
| `ENABLE_LIVE_TRACKING`         | `False` | Master switch; bila `False`, endpoint WS menolak/ignored |
| `KURIR_POS_TTL_SECONDS`        | `600`   | TTL posisi kurir di Redis                  |
| `KURIR_GEOFENCE_TTL_SECONDS`   | `86400` | TTL state geofence per paket di Redis (sesuai durasi sesi/batch aktif) |
| `KURIR_POS_MAX_RATE_SECONDS`   | `3`     | Rate limit pesan posisi per koneksi        |
| `MAX_OFF_ROUTE_DISTANCE_METERS`| (lihat `pathfinding.py:73`) | Ambang deteksi off-route utk fitur turunan |

Toggle `ENABLE_LIVE_TRACKING` sudah dibaca oleh `live_tracking_enabled()`
(`pathfinding.py:109-110`).

## 8. Keamanan & Rate Limiting

- WebSocket divalidasi dengan JWT (`current_kurir`); kurir hanya bisa menulis
  key `driver:pos:{kurir_id}` miliknya sendiri.
- Validasi koordinat: `lat ∈ [-90, 90]`, `lon ∈ [-180, 180]`; reject di luar.
- Rate limit: abaikan pesan jika selisih `ts` < `KURIR_POS_MAX_RATE_SECONDS`.
- Batasi jumlah koneksi aktif per kurir (1 koneksi, koneksi baru menggantikan yang lama).
- Tidak ada secret yang ditulis ke Redis selain koordinat (data non-sensitif).

## 9. Testing Plan

Ikuti pola test yang sudah ada:

| Test                                        | Pola        | File target |
|---------------------------------------------|-------------|-------------|
| Unit: parsing & validasi pesan WS           | sintetis    | `tests/test_courier_position.py` |
| Unit: Redis `set_pos` / `get_pos`           | sintetis    | `tests/test_courier_position.py` |
| Unit: pemilihan titik awal (courier vs hub) | sintetis    | `tests/test_delivery_optimizer.py` |
| Unit: geofence state machine (transisi masuk/keluar) | sintetis | `tests/test_courier_position.py` |
| Unit: anti-spam — reconnect WS tidak memicu `geofence_enter` duplikat (state Redis tetap `inside`) | sintetis | `tests/test_courier_position.py` |
| Unit: fallback chain titik awal (courier → Redis → hub) | sintetis    | `tests/test_delivery_optimizer.py` |
| Unit: resolusi daftar stop belum terkirim   | sintetis    | `tests/test_courier_position.py` |
| Integrasi: WS handshake tanpa token ditolak | `httpx`+ASGI | `tests/test_delivery_flow.py` |
| Integrasi: posisi kurir dipakai sbg titik awal rute | `httpx`+DB | `tests/test_delivery_flow.py` |
| Integrasi: WS push `geofence_enter` saat kurir ≤30m dari stop | `httpx`+DB | `tests/test_delivery_flow.py` |

## 10. Checklist Status Implementasi

| # | Item                                                        | Status |
|---|-------------------------------------------------------------|--------|
| 1 | `Kurir` tanpa field lokasi (tidak perlu, Redis-only)        | ✅ OK   |
| 2 | Flag `ENABLE_LIVE_TRACKING` di `pathfinding.py:109`         | ✅ Ada  |
| 3 | `app.state.active_route` — belum dikonsumsi                 | ⚠️ Mati |
| 4 | `_MAX_OFF_ROUTE_M` di `pathfinding.py:73` — belum dipakai   | ⚠️ Mati |
| 5 | Paket `websockets==16.1.1` terpasang                        | ✅ Ada  |
| 6 | Client Redis `app/core/redis.py`                            | ✅ Ada  |
| 7 | Auth JWT `current_kurir` (`dependencies.py:21`)             | ✅ Ada  |
| 8 | Snapping `snap_point_to_graph` (`snap.py:87`)               | ✅ Ada  |
| 9 | Endpoint WS `/api/v1/ws/driver/position`                    | ✅ Ada  (`app/api/v1/endpoints/tracking.py`) |
| 10| Schema `courier_position` di request pathfinding            | ✅ Ada  (`app/schemas/pathfinding.py:80`) |
| 11| Pemakaian posisi kurir sbg titik awal rute                  | ✅ Ada  (`_resolve_delivery_start`) |
| 12| Konsumsi `HGETALL driver:pos:*` di pathfinding/dashboard    | ✅ Ada  (`get_kurir_position_latlon`) |
| 13| Test untuk posisi kurir                                     | ✅ Ada  (`tests/test_courier_position.py`) |
| 14| Push `geofence_enter`/`geofence_exit` via WS                | ✅ Ada  (`_geofence_check`) |
| 15| State machine geofence di Redis (`driver:geofence:{id}:{pid}`) + anti-spam reconnect | ✅ Ada  (`geofence_event`) |
| 16| Cache stop belum terkirim `driver:stops:{id}` + invalidasi  | ✅ Ada  (`_get_stops` + `_invalidate_tracking_cache`) |
| 17| Fallback chain titik awal (courier → Redis → hub)           | ✅ Ada  (`resolve_start_point`) |
| 18| `/geofence-check` dipertahankan sebagai fallback            | ✅ Ada  |

> Implementasi dasar selesai. Langkah 6 (dynamic rerouting memakai
> `_MAX_OFF_ROUTE_M` + `app.state.active_route`) belum dikerjakan —
> tetap menjadi fitur turunan.

## 11. Langkah Implementasi yang Disarankan

> Status: langkah 1-5, 7 sudah diimplementasikan. Tinggal langkah 6 (fitur turunan).

1. ✅ Helper Redis `set_kurir_position` / `get_kurir_position` + geofence state
   + stops cache (`app/services/tracking.py`).
2. ✅ Endpoint WebSocket `WS /api/v1/ws/driver/position` + auth JWT
   (guard oleh `ENABLE_LIVE_TRACKING`).
3. ✅ `courier_position` opsional pada `OptimizedDeliveryRouteRequest`
   (`app/schemas/pathfinding.py`) dan **fallback chain** titik awal:
   `courier_position → HGETALL driver:pos → hub_origin`.
4. ✅ Geofence server-side: resolusi stop belum terkirim
   (cache `driver:stops:{id}`), hitung haversine, **state machine per paket di
   Redis** (`driver:geofence:{kurir_id}:{package_id}` + TTL `KURIR_GEOFENCE_TTL_SECONDS`),
   push `geofence_enter`/`geofence_exit` hanya saat transisi (anti-spam
   reconnect WS).
5. ✅ Invalidasi cache `driver:stops:{id}` dan `DEL driver:geofence:{kurir_id}:{package_id}`
   pada `PATCH /shipments/{id}/status` (`shipments.py:443`) saat status `delivered`.
6. ⏳ (Fitur turunan) Implementasikan dynamic rerouting memakai
   `_MAX_OFF_ROUTE_M` dan `app.state.active_route`.
7. ✅ Tambahkan test sesuai bagian 9 (`tests/test_courier_position.py`).