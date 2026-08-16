# API Reference — TEST2 Pathfinding

Dokumentasi endpoint API untuk frontend. Base URL: `http://localhost:8000`

Semua endpoint ada juga di OpenAPI spec yang bisa di-fetch langsung:

| URL | Isi |
|---|---|
| `/openapi.json` | Spec OpenAPI (JSON) — bisa dipakai generate client/types |
| `/docs` | Swagger UI (interaktif, bisa coba request langsung) |
| `/redoc` | ReDoc (dokumentasi baca saja) |

> Frontend bisa `fetch('/openapi.json')` lalu generate TypeScript types
> pakai tool seperti [openapi-typescript](https://openapi-ts.dev).

---

## Konvensi Response

Endpoint non-pathfinding memakai envelope seragam:

```json
{ "success": true, "message": "Pesan", "data": { ... } }
```

Error:

```json
{ "success": false, "message": "Alasan error" }
```

### Status code yang umum

| Code | Arti |
|---|---|
| 200 | OK |
| 201 | Created |
| 401 | Token tidak valid / belum login |
| 404 | Data tidak ditemukan |
| 409 | Konflik (mis. kurir sudah punya batch aktif) |
| 422 | Validasi body gagal |
| 500 | Error server |

---

## Auth (Kurir)

### POST `/api/v1/auth/login`

Login kurir, dapat JWT token.

**Request**
```json
{ "username": "kurir1", "password": "rahasia" }
```

**Response 200**
```json
{
  "success": true,
  "message": "Login berhasil",
  "data": {
    "token": "<JWT>",
    "kurir": {
      "id": 1,
      "nama": "Budi",
      "username": "kurir1",
      "nomor_telepon": "0812...",
      "kendaraan": "motorcycle"
    }
  }
}
```

**Error**: `401` username/password salah.

---

### GET `/api/v1/auth/me`

Ambil data kurir yang login. **Wajib header:**
```
Authorization: Bearer <JWT>
```

**Response 200**
```json
{
  "success": true,
  "message": "Berhasil mengambil data kurir",
  "data": { "kurir": { "id": 1, "nama": "Budi", "username": "kurir1", "nomor_telepon": "0812...", "kendaraan": "motorcycle" } }
}
```

**Error**: `401` token tidak ada/tidak valid.

---

## Pathfinding

### POST `/api/v1/pathfinding/find-route`

Cari satu rute terbaik antara dua titik koordinat.

**Request**
```json
{
  "origin": { "latitude": -6.8048, "longitude": 110.8385 },
  "destination": { "latitude": -6.8100, "longitude": 110.8500 },
  "mode": "car",
  "last_mile_precision": true,
  "dynamic_rerouting": false
}
```

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `origin.latitude` | float | ya | -90 s/d 90 |
| `origin.longitude` | float | ya | -180 s/d 180 |
| `destination.latitude` | float | ya | |
| `destination.longitude` | float | ya | |
| `mode` | string | tidak | `motorcycle`, `car`, `truck` (default `car`) |
| `last_mile_precision` | bool | tidak | snap titik awal/akhir ke jalan |
| `dynamic_rerouting` | bool | tidak | aktifkan re-route saat ada kemacetan |

**Response 200**
```json
{
  "status": "success",
  "total_distance_meters": 1234.56,
  "route_coordinates": "_p~iF~ps|U_ulLnnqC_mqNvxq`@",
  "source": "tile:semarang",
  "warning": null,
  "graph_radius_meters": 5000,
  "estimated_time_seconds": 521,
  "estimated_arrival": "2026-08-14T12:30:00+07:00",
  "traffic_segments": [
    { "start_index": 3, "end_index": 8, "multiplier": 1.8 }
  ]
}
```

| Field | Keterangan |
|---|---|
| `route_coordinates` | **Encoded polyline string** (Google Maps / Mapbox, precision 5) dari titik `[lat, lon]` berurutan. Decode dengan `decodePolyline` |
| `traffic_segments` | Indeks di titik hasil-decode `route_coordinates` yang terkena multiplier lalu lintas (urutan & jumlah titik identik dengan array asli sebelum di-encode) |
| `estimated_arrival` | Perkiraan tiba (WIB) |
| `warning` | Catatan (mis. fallback mode kendaraan) |

**Error**: `400` area di luar peta / lokasi tak terjangkau, `404` rute tidak ditemukan.

---

### POST `/api/v1/pathfinding/find-route-options`

Cari beberapa opsi rute (rute utama + alternatif). Request body sama dengan
`find-route`.

**Response 200**
```json
{
  "status": "success",
  "total_route": 3,
  "source": "tile:semarang",
  "warning": null,
  "graph_radius_meters": 5000,
  "routes": [
    {
      "route_id": 1,
      "is_best": true,
      "summary": "via Jl. Pandanaran, Jl. Pahlawan",
      "distance_km": 3.12,
      "duration_mins": 8.7,
      "total_distance_meters": 3120.4,
      "route_coordinates": "_p~iF~ps|U_ulLnnqC_mqNvxq`@",
      "estimated_time_seconds": 521,
      "estimated_arrival": "2026-08-14T12:30:00+07:00",
      "traffic_segments": [],
      "incidents": [
        {
          "type": "congestion",
          "location": [-6.8070, 110.8420],
          "coordinates": "_yxnF`pb|U",
          "multiplier": 1.8,
          "delay_minutes": 3.0,
          "description": "Kepadatan lalu lintas",
          "provider": "smart_hybrid"
        }
      ]
    }
  ]
}
```

> `routes[0].route_id = 1` & `is_best = true` adalah rute tercepat.
> Jumlah opsi maksimal diatur env `ALTERNATIVE_ROUTES_MAX` (default 3).

---

## Traffic

### POST `/api/v1/traffic/update-weight`

Set penalti/berat sebuah edge (mis. ditutup atau macet).

**Request**
```json
{ "edge_id": 12345, "multiplier": 3.0 }
```

> `multiplier = inf` berarti edge tertutup. Butuh Redis aktif.

**Response 200**
```json
{ "status": "ok", "edge_id": 12345, "multiplier": 3.0 }
```

**Error**: `503` Redis tidak tersedia.

---

### GET `/api/v1/traffic/penalties`

List semua penalti yang sedang aktif.

**Response 200**
```json
{
  "penalties": [
    { "edge_id": 12345, "multiplier": 3.0 },
    { "edge_id": 99999, "multiplier": null }
  ]
}
```

---

### GET `/api/v1/traffic/status`

Status layanan lalu lintas.

**Response 200**
```json
{
  "enabled": true,
  "mode": "smart_hybrid",
  "poller_running": true,
  "providers": [ { "name": "tomtom", "active": true } ],
  "penalty_count": 12
}
```

---

### GET `/api/v1/traffic/map`

Segmen jalan yang terkena penalti (untuk overlay di map).

**Response 200**
```json
{
  "segments": [
    {
      "edge_id": 12345,
      "multiplier": 3.0,
      "closure": false,
      "coordinates": "_p~iF~ps|U_ulLnnqC"
    }
  ],
  "source": "tile:semarang"
}
```

---

## Batch

### POST `/api/v1/batches` — Assign batch ke kurir

**Request**
```json
{
  "paket_ids": [1, 2, 3],
  "kurir_id": 1,
  "hub_id": 2
}
```

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `paket_ids` | array int | ya | minimal 1 |
| `kurir_id` | int | ya | |
| `hub_id` | int | tidak | opsional |

**Response 201**
```json
{
  "success": true,
  "message": "Batch BATCH-20260814-0001 dibuat: 3 paket di-assign ke kurir Budi",
  "data": {
    "batch": {
      "id": 1,
      "batch_no": "BATCH-20260814-0001",
      "status": "assigned",
      "assigned_at": "2026-08-14T04:00:00Z",
      "hub": { "id": 2, "nama": "Hub Semarang", "alamat": "Jl. ..." },
      "kurir": { "id": 1, "nama": "Budi", "nomor_telepon": "0812...", "kendaraan": "motorcycle" }
    },
    "shipments": [
      {
        "shipment_id": 10,
        "resi": "PKT000001",
        "paket": { "id": 1, "nama": "Paket A", "alamat": "Jl. ...", "jenis_pengiriman": "reguler" },
        "status": "assigned",
        "cod": { "status": "pending", "amount": 150000, "collected_at": null, "remitted_at": null },
        "billing": { "ongkir": 15000.0, "status": "unpaid", "paid_at": null },
        "picked_up_at": null,
        "delivered_at": null
      }
    ]
  }
}
```

**Error**: `404` kurir/paket/hub tidak ditemukan, `409` kurir punya batch aktif
atau paket sudah punya shipment aktif.

---

### GET `/api/v1/batches` — List batch

Query params (semua opsional): `status`, `kurir_id`, `hub_id`.

**Response 200**
```json
{
  "success": true,
  "message": "2 batch ditemukan",
  "data": [
    {
      "id": 1,
      "batch_no": "BATCH-20260814-0001",
      "status": "assigned",
      "total_paket": 3,
      "assigned_at": "2026-08-14T04:00:00Z",
      "hub": { "id": 2, "nama": "Hub Semarang", "alamat": "Jl. ..." },
      "kurir": { "id": 1, "nama": "Budi", "nomor_telepon": "0812...", "kendaraan": "motorcycle" }
    }
  ]
}
```

---

### GET `/api/v1/batches/{batch_id}` — Detail batch

**Response 200**: `data.batch` + `data.shipments[]` (format shipment sama seperti
di assign batch, lengkap dengan `picked_up_at` & `delivered_at`).

---

### PATCH `/api/v1/batches/{batch_id}/status`

Update status batch.

**Request**
```json
{ "status": "picked_up" }
```

`status` yang valid tergantung status saat ini:

| status saat ini | status tujuan yang valid |
|---|---|
| `assigned` | `picked_up`, `returned` |
| `picked_up` | `delivered`, `returned` |
| `delivered` | — |

**Response 200**
```json
{ "success": true, "message": "Status batch BATCH-20260814-0001 menjadi picked_up", "data": { "id": 1, "status": "picked_up" } }
```

**Error**: `409` transisi status tidak valid.

> `picked_up` juga meng-update semua shipment di batch ke `picked_up` dan
> membuat tracking history.

---

## Shipment

### GET `/api/v1/shipments` — List shipment

Query params (opsional): `status`, `batch_id`, `kurir_id`.

**Response 200**
```json
{
  "success": true,
  "message": "3 shipment ditemukan",
  "data": [
    {
      "shipment_id": 10,
      "resi": "PKT000001",
      "paket": { "id": 1, "nama": "Paket A", "alamat": "Jl. ...", "jenis_pengiriman": "reguler" },
      "status": "assigned",
      "cod": { "status": "pending", "amount": 150000, "collected_at": null, "remitted_at": null },
      "billing": { "ongkir": 15000.0, "status": "unpaid", "paid_at": null },
      "picked_up_at": null,
      "delivered_at": null,
      "batch_no": "BATCH-20260814-0001",
      "kurir_id": 1
    }
  ]
}
```

---

### GET `/api/v1/shipments/{shipment_id}` — Detail shipment

**Response 200**: `data` = objek shipment + `data.batch` (id, batch_no, status,
hub, kurir).

---

### PATCH `/api/v1/shipments/{shipment_id}/status`

**Request**
```json
{ "status": "delivered" }
```

Transisi valid:

| status saat ini | status tujuan yang valid |
|---|---|
| `assigned` | `picked_up`, `returned` |
| `picked_up` | `delivered`, `failed`, `returned` |

**Response 200**
```json
{ "success": true, "message": "Status shipment 10 menjadi delivered", "data": { "shipment_id": 10, "status": "delivered" } }
```

> `delivered` otomatis menandai COD `pending` → `collected`.
> `failed`/`returned` menandai COD menjadi `not_applicable`.

---

### PATCH `/api/v1/shipments/{shipment_id}/cod`

Tandai COD sudah di-setor (remitted). Hanya valid kalau COD `collected`.

**Request**
```json
{ "status": "remitted" }
```

**Response 200**
```json
{
  "success": true,
  "message": "COD ditandai remitted",
  "data": { "shipment_id": 10, "cod": { "status": "remitted", "amount": 150000, "collected_at": "2026-08-14T...", "remitted_at": "2026-08-14T..." } }
}
```

**Error**: `409` COD belum `collected`.

---

### PATCH `/api/v1/shipments/{shipment_id}/billing`

Tandai billing sudah dibayar / di-refund.

**Request**
```json
{ "status": "paid" }
```

`status`: `paid` atau `refunded`.

**Response 200**
```json
{
  "success": true,
  "message": "Billing ditandai paid",
  "data": { "shipment_id": 10, "billing": { "ongkir": 15000.0, "status": "paid", "paid_at": "2026-08-14T..." } }
}
```

---

### GET `/api/v1/shipments/{shipment_id}/tracking`

Riwayat tracking paket (timeline).

**Response 200**
```json
{
  "success": true,
  "message": "Tracking berhasil diambil",
  "data": {
    "resi": "PKT000001",
    "status": "delivered",
    "history": [
      {
        "event": "received_at_hub",
        "keterangan": "Paket tiba di hub",
        "hub": { "id": 2, "nama": "Hub Semarang", "alamat": "Jl. ..." },
        "waktu": "2026-08-14T02:00:00Z"
      },
      {
        "event": "picked_up",
        "keterangan": "Paket diambil kurir",
        "hub": null,
        "waktu": "2026-08-14T04:00:00Z"
      }
    ]
  }
}
```

---

### POST `/api/v1/shipments/{shipment_id}/history`

Tambah event history manual (mis. tiba/depart hub).

**Request**
```json
{
  "event": "received_at_hub",
  "keterangan": "Paket tiba di hub Kudus",
  "hub_id": 3
}
```

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `event` | string | ya | `received_at_hub` atau `departed_hub` |
| `keterangan` | string | tidak | |
| `hub_id` | int | tidak | |

**Response 201**
```json
{
  "success": true,
  "message": "History ditambahkan",
  "data": { "id": 5, "shipment_id": 10, "event": "received_at_hub", "keterangan": "Paket tiba di hub Kudus", "waktu": "2026-08-14T06:00:00Z" }
}
```

---

## Master Data (dropdown)

### GET `/api/v1/kurir`

List kurir aktif.

**Response 200**
```json
{
  "success": true,
  "message": "3 kurir ditemukan",
  "data": [
    { "id": 1, "nama": "Budi", "nomor_telepon": "0812...", "kendaraan": "motorcycle" }
  ]
}
```

---

### GET `/api/v1/hubs`

List hub aktif.

**Response 200**
```json
{
  "success": true,
  "message": "2 hub ditemukan",
  "data": [
    { "id": 2, "nama": "Hub Semarang", "alamat": "Jl. ..." }
  ]
}
```

---

## Lainnya

### GET `/health`

Health check server + status graph pathfinding.

**Response 200**
```json
{
  "status": "ok",
  "app": "TEST2 API Engine",
  "version": "1.0.0",
  "osmnx_available": true,
  "osmnx_error": null,
  "pbf_available": true,
  "overpass_reachable": true,
  "route_test": { "ok": true, "total_distance_meters": 1234.56, "route_points": 45, "source": "tile:semarang", "warning": null, "graph_radius_meters": 5000, "graph_bbox": [...] },
  "hierarchical": { "tiles_enabled": true, "base_available": true, "base_loaded": true, "base_bbox": null },
  "hierarchical_test": { "ok": true, "total_distance_meters": 78000.0, "route_points": 1200, "warning": null },
  "timestamp": "2026-08-14T04:00:00+00:00"
}
```

---

## Catatan integrasi frontend

1. **Auth**: login → simpan `token` → kirim header `Authorization: Bearer <token>`
   di request `/auth/me`. Endpoint lain (batch/shipment/traffic) tidak wajib token.
2. **CORS**: kalau frontend beda origin, tambahkan origin ke env `CORS_ORIGINS`
   (comma-separated) di `.env` lalu restart.
3. **Generate types**: `npx openapi-typescript http://localhost:8000/openapi.json -o src/types/api.d.ts`
4. **Polyline route**: `route_coordinates` (dan `geometry`, `incidents[].coordinates`,
   segmen traffic) adalah **encoded polyline** (precision 5). Decode dulu sebelum
   dipakai sebagai koordinat Leaflet/Mapbox — contoh: `decodePolyline(route_coordinates)`.
5. **Timezone**: `estimated_arrival` dan semua field waktu memakai
   ISO-8601 UTC (akhiran `Z`).
