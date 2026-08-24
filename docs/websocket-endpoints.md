# WebSocket Endpoints — TEST2 Pathfinding

Dokumen lengkap untuk semua WebSocket endpoint di TEST2 API Engine. Semua WS menggunakan JWT authentication via query param `token` atau header `Authorization: Bearer`.

---

## 1. Konfigurasi Environment Variables

Variabel ini dikontrol melalui file `.env` dan mengontrol perilaku navigasi real-time:

### Deteksi Off-Route & Reroute

| Variabel | Default | Deskripsi |
|---|---|---|
| `OFF_ROUTE_THRESHOLD_M` | `40` | Ambang jarak (meter) kurir ke polyline rute aktif yang memicu `off_route_warning` dan evaluasi reroute. **Ini pemicu utama deteksi deviasi** — bukan `NAV_TURN_NOTIFY_DISTANCE_M`. |
| `REROUTE_COOLDOWN_SECONDS` | `30` | Cooldown (detik) antar auto-reroute untuk mencegah route flickering. |
| `AUTO_REROUTE` | `1` | `1` = rute baru langsung diterapkan (`auto_rerouted`); `0` = hanya kirim `reroute_available`, klien memutuskan. |
| `TRAFFIC_REROUTE_INTERVAL_SECONDS` | `30` | Interval background worker mengevaluasi ulang traffic per sesi. |
| `TRAFFIC_REROUTE_MIN_SAVING_SECONDS` | `120` | Hemat waktu minimal (detik) agar reroute traffic diterapkan. |

### Dynamic Stop Re-Ordering

| Variabel | Default | Deskripsi |
|---|---|---|
| `OFF_ROUTE_REORDER_THRESHOLD_M` | `50` | Ambang off-route (meter) yang memicu evaluasi re-order urutan stop. |
| `OFF_ROUTE_MAJOR_THRESHOLD_M` | `300` | Ambang off-route mayor (meter) untuk memicu switch active stop. |
| `REORDER_MIN_DISTANCE_SAVING_PCT` | `20` | Minimal penghematan jarak jalan (%) agar reorder diterapkan. |
| `REORDER_ACTIVE_SWITCH_DISTANCE_PCT` | `40` | Minimal % stop baru lebih dekat untuk switch active stop. |
| `REORDER_COOLDOWN_SECONDS` | `60` | Cooldown antar reorder (detik). Di-bypass saat test mode. |
| `MAX_REORDERS_PER_ROUTE` | `3` | Maksimal reorder per rute aktif. |
| `REORDER_STABILITY_WINDOW` | `2` | Jumlah kandidat reorder sama berturut-turut sebelum dieksekusi. Di-bypass saat test mode. |

### Progress & Turn-by-Turn

| Variabel | Default | Deskripsi |
|---|---|---|
| `NAV_PROGRESS_MIN_INTERVAL_SECONDS` | `3` | Interval minimal (detik) pengiriman `route_progress` ke klien. |
| `NAV_POS_MAX_RATE_SECONDS` | `3` | Rate limit pesan posisi per koneksi WS (detik antar update). Pesan lebih cepat dari ini diabaikan. |
| `NAV_TURN_NOTIFY_DISTANCE_M` | `150` | Jarak (meter) ke titik belokan sebelum sistem mengirim field `next_maneuver` di `route_progress` dan pesan `turn_by_turn`. **Hanya untuk notifikasi belokan** — bukan deteksi deviasi. |

### AI Agent (opsional)

| Variabel | Default | Deskripsi |
|---|---|---|
| `AI_REROUTE_ENABLED` | `0` | Keputusan reroute via LLM Agent (Gemini) dengan function calling; logika deterministik menjadi fallback. |
| `GEMINI_REROUTE_TIMEOUT_S` | `15` | Batas waktu (detik) satu keputusan LLM sebelum fallback deterministik. |
| `GEMINI_API_KEY` / `GEMINI_API_KEY_2` | — | API key primer & cadangan; bila keduanya habis kuota (429), sistem fallback ke logika deterministik. |

---

## 2. WS /api/v1/ws/driver/position — Tracking Posisi Kurir

**Route**: `WS /api/v1/ws/driver/position?token=<JWT>`  
**Authentication**: JWT di query param `token` atau header `Authorization: Bearer <token>`  
**Design**: Koneksi dua arah persisten; client mengirim pesan, server membalas & push event ke Redis.

### Message Flow (Client → Server)

**Kirim `ping`:**
```json
{"type": "ping"}
```
→ Dikirim klien secara berkala untuk menjaga koneksi tetap hidup.

**Kirim `position`:**
```json
{"type": "position", "lat": -6.8060, "lon": 110.8390, "bearing": 90, "speed": 20}
```
→ Dikirim klien setiap kali posisi berubah (dibatasi rate limit `KURIR_POS_MAX_RATE_SECONDS`).

### Message yang Diterima (Server → Client)

**Balasan `ack`:**
```json
{"type": "ack", "ok": true, "stored": true, "snapped": [-6.806, 110.839], "ts": 1726478400}
```

**Event `geofence_enter`:**
```json
{"type": "geofence_enter", "package_id": 1, "distance_m": 2.0, "radius_m": 30}
```
→ Dikirim ketika kurir memasuki radius geofence stop paket (default 30 m).

**Event `geofence_exit`:**
```json
{"type": "geofence_exit", "package_id": 1, "distance_m": 45.2, "radius_m": 30}
```
→ Dikirim ketika kurir keluar radius geofence stop paket.

### Rate Limit

- Pesan `position` yang dikirim < `KURIR_POS_MAX_RATE_SECONDS` (default 3 dtk) dari update sebelumnya → diabaikan, `ack.stored: false`.

---

## 3. WS /api/v1/ws/navigation — Navigasi & Auto-Reroute Real-time

**Route**: `WS /api/v1/ws/navigation?token=<JWT>`  
**Authentication**: JWT di query param `token` atau header `Authorization: Bearer <token>`  
**Design**: Koneksi dua arah persisten; client mengirim posisi, server membalas progress, deteksi off-route, auto-reroute, dan dynamic stop re-ordering.

> ⚠️ **Koneksi akan ditolak dengan kode `1008`** bila `ENABLE_LIVE_NAVIGATION=0`, dan kode `4401` bila token JWT tidak valid/tidak ada.

---

### 3.1 Message Client → Server

#### `ping`
```json
{"type": "ping"}
```
→ Respons: `{"type": "ack", "ok": true, "ts": ...}`

#### `start_navigation`
```json
{"type": "start_navigation", "route_id": 1, "leg_index": 0}
```
→ Mengaktifkan sesi navigasi dari snapshot rute di Redis (`driver:nav:{kurir_id}`) yang diisi oleh HTTP endpoint `find-route` / `find-optimized-delivery-route`.  
→ `leg_index` opsional (default `0`) untuk multi-leg: mulai dari stop tertentu.

#### `location_update`
```json
{
  "type": "location_update",
  "lat": -6.8060,
  "lng": 110.8390,
  "bearing": 90,
  "speed": 20,
  "current_route_id": 1,
  "test_mode": false
}
```

| Field | Wajib | Deskripsi |
|---|---|---|
| `lat`, `lng` | ✅ | Posisi kurir saat ini. |
| `current_route_id` | Opsional | Bila dikirim dan tidak cocok dengan `session.route_id`, server membalas `error` (proteksi posisi untuk rute yang salah). |
| `test_mode` | Opsional | `true` = turunkan seluruh ambang re-ordering untuk pengujian UI demo (lihat [Section 5](#5-dynamic-stop-re-ordering--urutan-paket-dinamis)). Rate limit `NAV_POS_MAX_RATE_SECONDS` tetap berlaku. |

#### `complete_leg` / `pod_submitted`
```json
{"type": "complete_leg"}
```
→ Dikirim setelah kurir menyelesaikan satu pengiriman (POD terkirim/geofence terkonfirmasi). Server memindahkan navigasi ke leg berikutnya dari snapshot multi-leg. Respons bisa berupa `ack` (leg baru), `route_complete` (semua selesai), atau `error`.

---

### 3.2 Message Server → Client

#### `ack` (respons `start_navigation`)
```json
{
  "type": "ack", "ok": true,
  "route_id": 1,
  "leg_index": 0,
  "kind": "multi",
  "total_distance_m": 12500.0,
  "total_eta_s": 1500.0,
  "total_legs": 3,
  "off_route_threshold_m": 40.0,
  "polyline": "<encoded active leg>",
  "ts": 1726478400
}
```
→ Konfirmasi navigasi aktif; `polyline` adalah geometri leg aktif yang harus digambar klien.

#### `ack` (respons `complete_leg`)
```json
{
  "type": "ack", "ok": true, "action": "complete_leg",
  "route_id": 1,
  "leg_index": 1,
  "kind": "multi",
  "package_id": 2,
  "recipient_name": "Siti",
  "dest": [-6.81, 110.85],
  "polyline": "<encoded leg baru>",
  "off_route_threshold_m": 40.0,
  "total_distance_m": 9500.0,
  "total_eta_s": 1100.0,
  "total_legs": 3,
  "ts": 1726478500
}
```
→ Navigasi berpindah ke leg berikutnya; klien harus mengganti polyline aktif.

#### `error`
```json
{"type": "error", "ok": false, "detail": "<pesan kesalahan>"}
```

#### `route_progress`
Dikirim minimal setiap `NAV_PROGRESS_MIN_INTERVAL_SECONDS` (default 3 dtk):
```json
{
  "type": "route_progress", "ok": true,
  "route_id": 1,
  "leg_index": 0,
  "remaining_distance_m": 500.0,
  "remaining_time_s": 300.0,
  "progress_pct": 45.5,
  "current_speed_kmh": 40.0,
  "average_speed_kmh": 38.5,
  "eta_timestamp": 1726479000,
  "traffic_level": "free",
  "leg_context": {
    "package_id": 1,
    "recipient_name": "Budi",
    "stop_sequence": 1,
    "total_legs": 3
  },
  "next_maneuver": {
    "instruction": "Belok kanan ke Jalan Nasional",
    "type": "turn_right",
    "distance_m": 85.0,
    "street_name": "Jalan Nasional",
    "bearing_change": 92,
    "road_class": "primary"
  }
}
```

| Field | Keterangan |
|---|---|
| `progress_pct` | Persentase progres 0–100 relatif terhadap polyline leg aktif. |
| `eta_timestamp` | Unix timestamp perkiraan tiba (`now + remaining_time_s`). |
| `traffic_level` | `"free"` / level kemacetan edge saat ini. |
| `leg_context` | Hanya ada untuk rute `multi`; konteks paket tujuan aktif. |
| `next_maneuver` | Ada bila jarak ke belokan < `NAV_TURN_NOTIFY_DISTANCE_M` (150m); lihat Section 6. |

#### `turn_by_turn`
Pesan khusus saat mendekati manuver:
```json
{
  "type": "turn_by_turn", "ok": true,
  "route_id": 1, "leg_index": 0,
  "maneuver": {"instruction": "Belok kiri", "type": "turn_left", "distance_m": 85.0}
}
```

#### `off_route_warning`
```json
{
  "type": "off_route_warning", "ok": true,
  "route_id": 1,
  "distance_m": 55.3,
  "threshold_m": 40.0,
  "ts": 1726478400
}
```
→ Dikirim pertama kali kurir melenceng > `OFF_ROUTE_THRESHOLD_M` (**40 m**) dari polyline aktif (edge-triggered, sekali per masuk kondisi off-route).

#### `auto_rerouted`
```json
{
  "type": "auto_rerouted",
  "ok": true,
  "route_id": 1,
  "leg_index": 0,
  "polyline": "<encoded rute baru>",
  "saving_s": 140.0,
  "eta_s": 600.0,
  "applied": true,
  "reason": "off_route",
  "steps": [
    {
      "distance_m": 150.0,
      "duration_s": 25.0,
      "polyline": "<encoded segmen step>",
      "instruction": {
        "instruction": "Belok kanan ke Jalan Nasional",
        "type": "turn_right",
        "street_name": "Jalan Nasional",
        "bearing_change": 92,
        "road_class": "primary",
        "is_exit": false
      }
    }
  ]
}
```
→ Dikirim ketika sistem menghitung rute baru dari posisi kurir ke destinasi leg aktif dan langsung menerapkannya (`AUTO_REROUTE=1`).

| Field | Keterangan |
|---|---|
| `polyline` | Geometri rute baru **penuh** (encoded, precision 5). Klien mengganti seluruh garis lama. |
| `steps` | Array turn-by-turn hasil `extract_steps()` — **setiap step memiliki `polyline` sub-segmennya sendiri** (bukan array koordinat mentah). |
| `saving_s` | Perkiraan hemat waktu (detik) dibanding sisa rute lama. |
| `reason` | Alasan reroute: `"off_route"` / `"traffic"` / alasan keputusan AI. |

Setelah event ini, server juga mengevaluasi **dynamic stop re-ordering** (Section 5) — bila terpicu, event `stops_reordered` menyusul.

#### `reroute_available`
Struktur identik `auto_rerouted` namun `"applied": false`. Dikirim bila `AUTO_REROUTE=0`; klien dapat menerapkannya manual dengan menggambar ulang dari field `polyline`.

#### `route_complete`
```json
{"type": "route_complete", "ok": true, "route_id": 1, "ts": 1726479000}
```
→ Semua leg multi-stop telah diselesaikan; snapshot rute di Redis dibersihkan.

---

## 4. Alur Event Off-Route → Reroute → Reorder

```
location_update (lat, lon)
        │
        ▼
hitung jarak ke polyline aktif (point_to_polyline_distance_m)
        │
   ┌────┴─────────────────────────────────┐
   │ dist <= 40m                          │ dist > 40m (OFF_ROUTE_THRESHOLD_M)
   ▼                                      ▼
(kirim route_progress            off_route_warning (edge-trigger)
 berkala)                               │
                                        ▼
                        [opsional] AI Agent decide_reroute()
                        (hint="off_route", timeout 15s;
                         action: apply/ignore/defer;
                         fallback deterministik bila
                         timeout/error/kuota habis)
                                        │
                                        ▼
                          compute_reroute(posisi → dest aktif)
                                        │
                              response != null?
                              ├── ya → kirim auto_rerouted
                              │         │
                              │         ▼
                              │  maybe_reorder_stops_on_off_route()
                              │         │
                              │    terpicu? → kirim stops_reordered
                              │
                              └── tidak → tidak ada event rute baru
```

---

## 5. Dynamic Stop Re-Ordering — Urutan Paket Dinamis

Fitur ini hanya aktif untuk rute `kind: "multi"` dengan ≥ 2 stop tersisa. Ketika kurir off-route, sistem dapat **mengurutkan ulang sisa paket** dan/atau **memindahkan active stop** ke paket yang secara geografis lebih efisien.

### 5.1 Dua Jenis Reorder

| Jenis | Kondisi Pemicu (mode normal) | Efek |
|---|---|---|
| **Standard reorder** | Urutan sisa stop tidak lagi optimal | Urutan `legs` snapshot di Redis diubah; **active stop tetap**. |
| **Major reorder (switch active)** | off-route > `OFF_ROUTE_MAJOR_THRESHOLD_M` (**300m**) **ATAU** stop baru ≥ `REORDER_ACTIVE_SWITCH_DISTANCE_PCT` (**40%**) lebih dekat daripada stop aktif | Active stop dipindah: `leg_index += 1`, polyline aktif diganti, event menyertakan detail switch. |

**Prioritas paket**: EXPRESS selalu diutamakan — pencarian nearest-first hanya dilakukan di antara paket EXPRESS yang tersisa; REGULAR nearest-only bila EXPRESS sudah habis.

**Sumber kecepatan estimasi durasi**: GPS realtime kurir (`last_position.speed`, bila > 0) → default per-mode env `SPEED_KMH_MOTORCYCLE` (35) / `SPEED_KMH_CAR` (40) / `SPEED_KMH_TRUCK` (30) → `DEFAULT_SPEED_KMH` (40). `duration_mins`/`estimated_time_seconds` pada `legs[0]` (estimasi posisi→stop baru) memakai nilai ini; `legs[i>0]` memprioritaskan `eta_s` hasil routing engine dari snapshot.

### 5.2 Mode Normal vs Test Mode

Flag `"test_mode": true` pada `location_update` menurunkan ambang untuk memudahkan pengujian UI demo (klik sembarang di peta langsung memicu reorder):

| Parameter | Mode Normal | Test Mode |
|---|---|---|
| Ambang evaluasi reorder | `OFF_ROUTE_REORDER_THRESHOLD_M` = 50 m | −1 (selalu picu) |
| Major switch off-route | > 300 m | > 80 m |
| Switch jika lebih dekat | ≥ 40% | ≥ 20% |
| Minimal penghematan jarak | ≥ 20% | ≥ 20% |
| Stability window (kandidat sama berturut) | `REORDER_STABILITY_WINDOW` = 2 | bypass |
| Cooldown reorder | `REORDER_COOLDOWN_SECONDS` = 60 s | bypass |
| Batas reorder (`MAX_REORDERS_PER_ROUTE`) | 3 per rute | 3 per rute |

### 5.3 Event `stops_reordered` (server → client)

```json
{
  "type": "stops_reordered",
  "ok": true,
  "route_id": 1,
  "reorder_reason": "off_route_major_switch_active_test_auto_nearest",

  "new_stop_order": [
    {"package_id": 2, "recipient_name": "Siti", "service_type": "EXPRESS",
     "stop_order": 1, "dest": [-6.8100, 110.8500]},
    {"package_id": 3, "recipient_name": "Andi", "service_type": "REGULAR",
     "stop_order": 2, "dest": [-6.8200, 110.8600]}
  ],

  "legs": [
    {"leg_index": 0, "geometry": "<encoded>", "distance_km": 1.2, "duration_mins": 5},
    {"leg_index": 1, "geometry": "<encoded>", "distance_km": 2.3, "duration_mins": 8}
  ],

  "active_leg_index": 0,
  "current_position": [-6.8060, 110.8390],

  "active_leg_changed": true,
  "active_stop_switched": true,

  "switched_from": {"package_id": 1, "recipient_name": "Budi"},
  "switched_to": {"package_id": 2, "recipient_name": "Siti",
                  "dest": [-6.8100, 110.8500]},

  "polyline": "<encoded active leg baru>",
  "ts": 1726478400
}
```

### 5.4 Panduan Field untuk Klien

| Field | Wajib Ditangani Klien? | Aksi |
|---|---|---|
| `new_stop_order` | ✅ | Bangun ulang daftar paket UI sesuai urutan baru (`stop_order` sudah dinomori ulang backend). |
| `active_stop_switched` | ✅ | Bila `true`: tampilkan notifikasi/warning bahwa tujuan utama berganti; ganti target navigasi ke `switched_to.dest`. |
| `active_leg_changed` + `polyline` | ✅ | Ganti polyline aktif dengan `polyline` (red dashed menuju stop baru). |
| `legs` | Disarankan | Gambar sisa rute (biru solid) dari `legs[i].geometry` untuk i > `active_leg_index`. |
| `current_position` | Info | Posisi kurir yang memicu reorder (`null` bila bukan major switch). |

**Nilai `reorder_reason`:**

| Nilai | Arti |
|---|---|
| `off_route_closer_to_next_stop` | Standard reorder — urutan sisa stop berubah, active stop tetap. |
| `off_route_major_switch_active` | Major reorder — active stop berpindah. |
| Suffix `_test` / `_test_auto_nearest` | Dipicu lewat `test_mode: true`. |

### 5.5 Interaksi dengan Event Lain

- `stops_reordered` **selalu menyusul** `auto_rerouted` pada siklus off-route yang sama.
- Snapshot Redis `driver:nav:{kurir_id}` diperbarui backend — `complete_leg` berikutnya akan mengikuti urutan baru.
- Cooldown reorder independen dari cooldown reroute (`REROUTE_COOLDOWN_SECONDS`).

---

## 6. Fitur Turn-by-Turn Navigasi

### Field `next_maneuver` di `route_progress`

Muncul ketika jarak ke titik belokan < `NAV_TURN_NOTIFY_DISTANCE_M` (**150 m**):

```json
"next_maneuver": {
  "instruction": "Belok kanan ke Jalan Nasional",
  "type": "turn_right",
  "distance_m": 85.0,
  "street_name": "Jalan Nasional",
  "bearing_change": 92,
  "road_class": "primary"
}
```

Nilai `type`: `continue`, `turn_left`, `turn_right`, `turn_slight_left`, `turn_slight_right`, `uturn`, `roundabout_exit`, `arrive`.

### Pesan `turn_by_turn`

Pesan terpisah dikirim ketika mendekati manuver (kondisi sama dengan munculnya `next_maneuver`):

```json
{"type": "turn_by_turn", "ok": true, "route_id": 1, "leg_index": 0,
 "maneuver": {"instruction": "Belok kiri", "type": "turn_left", "distance_m": 85.0}}
```

### Alur Data Turn-by-Turn

1. `remaining_progress(session, lat, lon)` di `app/services/navigation.py` menghitung progres + `next_maneuver` dari `get_next_maneuver(session.steps, current_step_idx)`.
2. Endpoint WS menyebarkan dictionary prog ke JSON `route_progress` (`**prog`).
3. Bila `next_maneuver.distance_m <= NAV_TURN_NOTIFY_DISTANCE_M`, pesan `turn_by_turn` juga dikirim.
4. Manuver dihasilkan `app/services/pathfinding/maneuvers.py` dari bearing change antar titik rute; teks instruksi Bahasa Indonesia ("Lurus ke …", "Belok kiri ke …", "Anda telah tiba di tujuan").

> ⚠️ `next_maneuver` hanyalah informasi edukatif. Kurir tetap wajib mematuhi lalu lintas dan kondisi jalan.

---

## 7. Ringkasan Lengkap

### Tabel Pesan

| Message | Arah | Dikirim/Diterima Saat |
|---------|------|----------------------|
| `ping` | C→S | Periodik menjaga koneksi; respons `ack`. |
| `start_navigation` | C→S | Aktifkan sesi navigasi; respons `ack` + polyline leg aktif. |
| `location_update` | C→S | Setiap ≥ `NAV_POS_MAX_RATE_SECONDS` (3 dtk); respons `route_progress` + event lain bila terpicu. Flag `test_mode` opsional. |
| `complete_leg` / `pod_submitted` | C→S | Setelah POD satu stop; respons `ack` leg baru / `route_complete`. |
| `ack` | S→C | Konfirmasi `start_navigation` / `complete_leg` / `ping`. |
| `route_progress` | S→C | Berkala setiap location_update (≥ 3 dtk); termasuk `leg_context`, `next_maneuver`. |
| `turn_by_turn` | S→C | Jarak ke belokan < 150 m. |
| `off_route_warning` | S→C | Jarak ke polyline > `OFF_ROUTE_THRESHOLD_M` (40 m), edge-triggered. |
| `auto_rerouted` | S→C | Rute baru diterapkan otomatis (termasuk field `steps` turn-by-turn). |
| `reroute_available` | S→C | `AUTO_REROUTE=0`; rute alternatif menunggu keputusan klien. |
| `stops_reordered` | S→C | Urutan paket diubah pasca off-route; lihat Section 5. |
| `route_complete` | S→C | Semua leg multi-stop selesai. |
| `error` | S→C | Validasi gagal / snapshot tidak cocok / token invalid. |

### Perbandingan Dua Endpoint WS

| Fitur | WS driver/position | WS navigation |
|---|---|---|
| **Tujuan** | Tracking posisi GPS kurir | Navigasi, auto-reroute, turn-by-turn, re-ordering |
| **Pesan utama** | `position` → `ack` + geofence events | `start_navigation` / `location_update` / `complete_leg` |
| **Push events** | `geofence_enter`, `geofence_exit` | `off_route_warning`, `auto_rerouted`, `reroute_available`, `stops_reordered`, `route_complete` |
| **Rate limit** | `KURIR_POS_MAX_RATE_SECONDS` (3 dtk) | `NAV_POS_MAX_RATE_SECONDS` (3 dtk) |
| **TTL Redis** | `driver:pos:{kurir_id}` (10 menit default) | `driver:nav:{kurir_id}` (default 1 jam) |
| **Auth** | `?token=<JWT>` / `Authorization: Bearer` | `?token=<JWT>` / `Authorization: Bearer` |
| **Status endpoint** | `GET /api/v1/ws/driver/position/status` | `GET /api/v1/ws/navigation/status` |
| **Fitur tambahan** | Geofence detection | Turn-by-turn + dynamic stop re-ordering |

---

## 8. Cara Test Semua WS Endpoint

```bash
# 1. Login dulu
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"joko","password":"rahasia123"}'

# 2. Cek status WS
curl http://localhost:8000/api/v1/ws/driver/position/status
curl http://localhost:8000/api/v1/ws/navigation/status
# expect: enabled=true, off_route_threshold_m=40.0, auto_reroute=true, dsb.

# 3. Test WS tracking
wscat -c "ws://localhost:8000/api/v1/ws/driver/position?token=<TOKEN>"
# Kirim: {"type":"position","lat":-6.8060,"lon":110.8390}

# 4. Test WS navigation dasar
wscat -c "ws://localhost:8000/api/v1/ws/navigation?token=<TOKEN>"
# Kirim: {"type":"start_navigation","route_id":1,"leg_index":0}
# Kirim: {"type":"location_update","lat":-6.8060,"lng":110.8390,"current_route_id":1}
# Pantau: route_progress, off_route_warning (deviasi > 40m),
#         auto_rerouted (rute baru), turn_by_turn (belokan < 150m)

# 5. Test dynamic stop re-ordering (test mode — threshold diturunkan)
# Kirim lokasi jauh dari rute dengan flag test_mode:
# {"type":"location_update","lat":-6.82,"lng":110.87,"current_route_id":1,"test_mode":true}
# Pantau: stops_reordered dengan new_stop_order + legs + active_leg_index

# 6. Test complete_leg (multi-stop)
# Kirim: {"type":"complete_leg"}
# Pantau: ack (action=complete_leg) atau route_complete
```

---

## 9. Referensi Kode

Dokumentasi ini dibuat berdasarkan kode di:

- `app/api/v1/endpoints/navigation.py` — Handler WS navigation (auth, message routing, event emission)
- `app/services/navigation.py` — Logika inti: `NavSession`, `remaining_progress`, `compute_reroute`, `advance_leg`, `maybe_reorder_stops_on_off_route`
- `app/services/pathfinding/maneuvers.py` — Ekstraksi turn-by-turn (`extract_steps`, `get_next_maneuver`)
- `app/services/pathfinding/delivery_optimizer.py` — Optimizer urutan stop (`optimize_stop_order_hybrid`, prioritas EXPRESS)
- `app/services/polyline.py` — Encode/decode polyline precision 5
- `app/services/tracking.py` — Snapshot rute Redis (`driver:nav:{kurir_id}`)
- `app/core/logging.py` — Log structlog dan correlation ID

Environment variables utama: `OFF_ROUTE_THRESHOLD_M`, `OFF_ROUTE_REORDER_THRESHOLD_M`, `OFF_ROUTE_MAJOR_THRESHOLD_M`, `REORDER_*`, `NAV_PROGRESS_MIN_INTERVAL_SECONDS`, `NAV_TURN_NOTIFY_DISTANCE_M`, `NAV_POS_MAX_RATE_SECONDS`, `AUTO_REROUTE`, `AI_REROUTE_ENABLED`.
