# Desain: Real-time Navigation & Auto-Rerouting (WebSocket)

> Dokumen implementasi fitur. Kontrak WS dijelaskan lengkap di Swagger
> (`/api/v1/ws/navigation`); dokumen ini fokus ke desain, state, dan alur.

## 1. Ringkasan

Kurir mendapatkan panduan real-time saat menyusuri rute yang sudah dihitung:
- **Progress** (`route_progress`) — sisa jarak, sisa waktu, persen progres.
- **Off-route detection** (`off_route_warning`) — kurir keluar jalur polyline rute.
- **Auto-rerouting berbasis traffic** (`auto_rerouted`) — background worker menilai
  ulang traffic tiap interval dan menerapkan rute yang lebih cepat.
- **Multi-stop** — navigasi per leg aktif pada rute optimasi pengantaran.

Semua geometri dikirim sebagai **encoded polyline precision 5** (`app/services/polyline.py`)
agar frame WS tetap kecil (< 2 KB).

## 2. Endpoint

- `WS /api/v1/ws/navigation` — koneksi navigation (1 koneksi/kurir).
- `GET /api/v1/ws/navigation/status` — status konfigurasi (enabled, threshold,
  cooldown, auto_reroute).
- Rute aktif di-snapshot oleh endpoint HTTP:
  - `POST /api/v1/pathfinding/find-route`
  - `POST /api/v1/pathfinding/find-route-options` (best route)
  - `POST /api/v1/pathfinding/find-optimized-delivery-route`

Guard: `ENABLE_LIVE_NAVIGATION` (default `0`). Bila `0`, handshake ditutup kode
`1008`. Autentikasi JWT: query `?token=` atau header `Authorization: Bearer`.

## 3. Snapshot rute (Redis)

Key `driver:nav:{kurir_id}` (TTL `KURIR_NAV_TTL_SECONDS`, default 3600 s),
diisi saat rute dihitung:

```json
{
  "route_id": 42,
  "kind": "single" | "multi",
  "mode": "motorcycle",
  "last_mile": true,
  "total_distance_m": 5321.0,
  "total_eta_s": 782.0,
  "legs": [
    {
      "index": 0,
      "stop_sequence_number": 1,
      "package_id": null,
      "recipient_name": "Destination",
      "encoded": "<polyline precision 5>",
      "dest": [-6.81, 110.85],
      "eta_s": 782.0
    }
  ]
}
```

- `kind="single"`: satu leg (find-route / find-route-options).
- `kind="multi"`: satu entry per leg (find-optimized-delivery-route); klien
  menyebut `leg_index` pada `start_navigation`.

## 4. Pesan WS

### Dari klien

| type | field | keterangan |
|------|-------|-----------|
| `ping` | — | balas `ack` |
| `start_navigation` | `route_id`, `leg_index?` | muat snapshot dari Redis, set polyline aktif |
| `location_update` | `lat`, `lng`, `bearing?`, `speed?`, `current_route_id` | simpan posisi + evaluasi |

Rate limit: `NAV_POS_MAX_RATE_SECONDS` (default 3 s) per koneksi.

### Dari server

| type | field penting | keterangan |
|------|--------------|-----------|
| `ack` | `route_id`, `leg_index`, `polyline`, `off_route_threshold_m` | setelah start |
| `error` | `detail` | JSON/koordinat invalid, route tidak cocok |
| `route_progress` | `remaining_distance_m`, `remaining_time_s`, `progress_pct` | throttle `NAV_PROGRESS_MIN_INTERVAL_SECONDS` |
| `off_route_warning` | `distance_m`, `threshold_m` | edge-trigger (hanya saat transisi) |
| `auto_rerouted` | `polyline`, `saving_s`, `eta_s`, `applied:true` | AUTO_REROUTE=1: langsung diterapkan |
| `reroute_available` | `polyline`, `saving_s`, `eta_s`, `applied:false` | AUTO_REROUTE=0: klien memutuskan |

## 5. Logika

### Off-route detection

`point_to_polyline_distance_m(lat, lon, coords)` (`app/services/navigation.py`)
memproyeksikan posisi kurir ke tiap segmen polyline (skala `cos(lat)`). Jika
jarak > `OFF_ROUTE_THRESHOLD_M` (default 40 m) → `off_route_warning` (sekali,
edge-trigger) dan, bila cooldown lewat, **auto-reroute instan** memakai
`_resolve_plan` + `_best_route` yang sama dengan endpoint pathfinding.

### Auto-reroute traffic (background)

`navigation_worker` (di-start `app/main.py` bila `ENABLE_LIVE_NAVIGATION=1`,
pola `traffic_poller`):
- Setiap `TRAFFIC_REROUTE_INTERVAL_SECONDS` (default 30 s), evaluasi tiap sesi.
- Hitung ulang rute dari posisi kini ke dest aktif dengan traffic aktif.
- Terapkan hanya bila hemat ≥ `TRAFFIC_REROUTE_MIN_SAVING_SECONDS` (default 120 s).
- Cooldown `REROUTE_COOLDOWN_SECONDS` (default 30 s) mencegah route flickering.

## 6. Env vars

| Variable | Default | Keterangan |
|----------|---------|-----------|
| `ENABLE_LIVE_NAVIGATION` | `0` | Master switch; `0` → WS ditutup 1008 |
| `KURIR_NAV_TTL_SECONDS` | `3600` | TTL snapshot rute di Redis |
| `OFF_ROUTE_THRESHOLD_M` | `40` | Ambang off-route (meter) |
| `REROUTE_COOLDOWN_SECONDS` | `30` | Cooldown auto-reroute |
| `TRAFFIC_REROUTE_INTERVAL_SECONDS` | `30` | Interval evaluasi ulang traffic |
| `TRAFFIC_REROUTE_MIN_SAVING_SECONDS` | `120` | Hemat minimum agar reroute diterapkan |
| `AUTO_REROUTE` | `1` | 1=auto terapkan; 0=kirim `reroute_available` |
| `NAV_POS_MAX_RATE_SECONDS` | `3` | Rate limit `location_update` |
| `NAV_PROGRESS_MIN_INTERVAL_SECONDS` | `3` | Throttle `route_progress` |

## 7. File

| File | Peran |
|------|-------|
| `app/services/navigation.py` | NavSession, NavRegistry, geometry, compute_reroute, worker |
| `app/api/v1/endpoints/navigation.py` | WS handler + status |
| `app/services/tracking.py` | `set/get/clear_nav_route` (Redis) |
| `app/api/v1/endpoints/pathfinding.py` | snapshot nav di 3 endpoint + `route_id` |
| `app/schemas/pathfinding.py` | field `route_id` (RouteResponse, RouteOptionsResponse, OptimizedDeliveryRouteResponse) |
| `app/main.py` | registry + worker + OpenAPI WS path |
| `app/ui/app.js`, `app/ui/delivery.js` | UI navigasi (simulasi GPS + progress + reroute) |