# WebSocket Endpoints — TEST2 Pathfinding

Dokumen lengkap untuk semua WebSocket endpoint di TEST2 API Engine. Semua WS menggunakan JWT authentication via query param `token` atau header `Authorization: Bearer`.

---

## Konfigurasi Environment Variables

Variabel ini dikontrol melalui file `.env` dan mengontrol timing/notifikasi navigasi:

| Variabel | Default | Deskripsi |
|---|---|---|
| `NAV_PROGRESS_MIN_INTERVAL_SECONDS` | `3` | Interval minimal (detik) antara pengiriman `route_progress` ke klien via WebSocket. Menentukan seberapa sering kurir menerima pembaruan progres. |
| `NAV_TURN_NOTIFY_DISTANCE_M` | `150` | Jarak (meter) sebelum poin belakala di mana sistem mulai memberikan peringatan/validasi deviasi dari jalur. **BUKAN** untuk memberikan instruksi teks "turn left/right", melainkan batas ambang untuk mendeteksi deviasi dan memicu `auto_rerouted`. |

---

## 1. WS /api/v1/ws/driver/position — Tracking Posisi Kurir

**Route**: `WS /api/v1/ws/driver/position?token=<JWT>`  
**Authentication**: JWT di query param `token` atau header `Authorization: Bearer <token>`  
**Design**: Konksi dua arah persisten (bukan one-shot webhook); client mengirim pesan, server membalas & push event ke Redis.

### Message Flow (Client → Server)

**Kirim `ping`:**
```json
{"type": "ping"}
```
→ Dikirim klien setiap `NAV_PROGRESS_MIN_INTERVAL_SECONDS` (default 3 dtk) untuk tetap koneksi hidup.

**Kirim `position`:**
```json
{"type": "position", "lat": -6.8060, "lon": 110.8390, "bearing": 90, "speed": 20}
```
→ Dikirim klien setiap kali posisi berubah (minimal setiap `NAV_PROGRESS_MIN_INTERVAL_SECONDS` dtk).

### Message yang Diterima (Server → Client)

**Balasan `ping`:**
```json
{"type": "ack", "ok": true, "ts": 1726478400}
```
→ Disembalikan server setiap kali klien mengirim `ping`. Konfirmasi koneksi masih aktif.

**Balasan `geofence_enter`:**
```json
{"type": "geofence_enter", "package_id": 1, "distance_m": 2.0, "radius_m": 30}
```
→ Disembalikan server **ketika kurir memasuki radius 30 meter dari stop paket** (state transition dari luar ke dalam radius).

**Balasan `geofence_exit`:**
```json
{"type": "geofence_exit", "package_id": 1, "distance_m": 45.2, "radius_m": 30}
```
→ Disembalikan server **ketika kurir keluar radius 30 meter dari stop paket** (state transition dari dalam ke luar radius).

### Rate Limit

- Pesan `position` dikirim < `KURIR_POS_MAX_RATE_SECONDS` (default 3 dtk) dari update sebelumnya → diabaikan (tidak disimpan), `ack.stored: false`.

---

## 2. WS /api/v1/ws/navigation — Navigasi & Auto-Reroute Real-time dengan Turn-by-Turn

**Route**: `WS /api/v1/ws/navigation?token=<JWT>`  
**Authentication**: JWT di query param `token` atau header `Authorization: Bearer <token>`  
**Design**: Konksi dua arah persisten; client mengirim posisi, server balas progress & reroute.

### Fitur Navigasi Turn-by-Turn

Sistem navigasi menyertakan **petunjuk arah dekat depan** (turn-by-turn) dalam pesan `route_progress`. Petunjuk ini dihasilkan dari geometri rute dan posisi kurir.

**Field yang tersedia di `route_progress`:**
- `next_maneuver` — objek berisi `instruction` (arah belok) dan `distance_m` (jarak ke poin belakala)

**Kapan field muncul:**
- Ketika `next_maneuver` ada di dictionary `prog` (hasil dari `remaining_progress(session, lat, lon)`)
- Ketika jarak ke poin belakala < `NAV_TURN_NOTIFY_DISTANCE_M` (default 150 meter)

**Contoh pesan `route_progress` dengan `next_maneuver`:**
```json
{"type": "route_progress", "remaining_distance_m": 120, "remaining_time_s": 90, "progress_pct": 78.5, "next_maneuver": {"instruction": "belok kiri", "distance_m": 85}}
```

**Catatan Penting:**
- Field `next_maneuver` hanyalah **informasi edukatif**, bukan perintah kontrol sistem
- Kurir tetap harus mematuhi lalu lintas, hukum lalu lintas, dan kondisi jalan
- Sistem menghitung arah berdasarkan bearing change antar poin di rute
- Nilai `instruction` di-set berdasarkan: left (< -60°), right (> 60°), straight (-20° sampai 20°), dsb.
- Field ini tersedia melalui `next_maneuver.instruction` di dalam objek `route_progress` (di-spread dari `prog` via `**prog`)

Pesan `turn_by_turn` khusus dikirim ketika approaching a maneuver (lihat section 3 untuk detail).

### Message Dikirim (Client → Server)

**Kirim `start_navigation`:**
```json
{"type": "start_navigation", "route_id": 1, "leg_index": 0}
```
→ Dikirim klien setelah mendapatkan `route_id` dari HTTP endpoint `find-optimized-delivery-route`. Memulai sesi navigasi.

**Kirim `location_update`:**
```json
{"type": "location_update", "lat": -6.8060, "lng": 110.8390, "bearing": 90, "speed": 20, "current_route_id": 1}
```
→ Dikirim klien setiap `NAV_PROGRESS_MIN_INTERVAL_SECONDS` (default 3.5 detik) sekali. Mewakili posisi kurir yang bergerak sepanjang rute.

### Message yang Diterima (Server → Client)

**Balasan `ack`:**
```json
{"type": "ack", "ok": true, "route_id": 1, "leg_index": 0, "kind": "multi", "off_route_threshold_m": 40.0, "polyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"}
```
→ Disembalikan server setelah `start_navigation` atau `location_update`. Konfirmasi diterima dan berisi detail rute aktif.

**Balasan `route_progress`:**
```json
{"type": "route_progress", "remaining_distance_m": 500.0, "remaining_time_s": 300, "progress_pct": 45.5, "next_maneuver": {"instruction": "melampau", "distance_m": 200}}
```
→ Disembalikan server **setiap kali progres dicatat** (minimum setiap `NAV_PROGRESS_MIN_INTERVAL_SECONDS` dtkt). Berisi jarak, waktu, persentase progress ke tujuan, **dan informasi poin belakala selanjutnya** (jika jarak ke poin belakala < `NAV_TURN_NOTIFY_DISTANCE_M`).

**Balasan `off_route_warning`:**
```json
{"type": "off_route_warning", "ok": true, "route_id": 1, "distance_m": 50.0, "threshold_m": 40.0}
```
→ Disembalikan server **ketika jarak kurir ke polyline aktif > `NAV_TURN_NOTIFY_DISTANCE_M`** (default 150 meter). Peringatan pertama kali kurir keluar jalan; terus menyimpang akan tetap menunjukkan warning sampai kurir kembali ke jalan.

**Balasan `auto_rerouted`:**
```json
{"type": "auto_rerouted", "ok": true, "route_id": 1, "polyline": "_p~jF~ps|U_ulLnnqC_mqNvxq`@", "saving_s": 120, "eta_s": 600, "applied": true, "reason": "off_route"}
```
→ Disembalikan server **ketika sistem menghitung rute baru yang lebih cepat** dan diterapkan otomatis. Menyertai pola baru, waktu penghematan, dan alasan mengapa reroute terjadi (mis. "off_route").

**Balasan `reroute_available`:**
```json
{"type": "reroute_available", "ok": true, "route_id": 1, "polyline": "_p~jF~ps|U_ulLnnqC_mqNvxq`@", "saving_s": 120, "eta_s": 600, "applied": false, "reason": "off_route"}
```
→ Disembalikan server **ketika `AUTO_REROUTE=0`** di `.env` dan ada deviasi dari jalur. Sistem menghitung rute alternatif tetapi membiarkan klien memutuskan apakah diterapkan atau tidak (`applied: false`).

---

## 3. Fitur Turn-by-Turn Navigasi

Sistem navigasi sekarang menyertakan **petunjuk arah dekat depan** (turn-by-turn) dalam pesan `route_progress`.

### Field `next_maneuver` di `route_progress`

Field ini akan muncul ketika kurir mendekati poin belakala dan sistem menghitung arah depan.

**Nilai possible (struktur dari `next_maneuver` di dalam `route_progress`):**
- `"instruction"` — `"belok kiri"`, `"right turn"`, `"lanjut lurus"`, `"melampau"`, atau instruksi lainnya
- `"distance_m"` — jarak (meter) ke poin belakala selanjutnya

**Bagaimana `next_maneuver` dihasilkan:**
Field ini diambil dari dictionary `prog` yang dihasilkan fungsi `remaining_progress(session, lat, lon)`. Dictionary `prog` berisi informasi progres serta `next_maneuver` yang dihasilkan dari `app.services.pathfinding.maneuvers.get_next_maneuver()`.

Karena `prog` di-spread ke JSON `route_progress` via `**prog`, field `next_maneuber` muncul sebagai objek terstruktur di dalam `route_progress`.

**Contoh pesan `route_progress` dengan `next_maneuver`:**
```json
{"type": "route_progress", "remaining_distance_m": 120, "remaining_time_s": 90, "progress_pct": 78.5, "next_maneuver": {"instruction": "belok kiri", "distance_m": 85}}
```

**Ketika field muncul:**
- Setiap `location_update` dikirim oleh klien
- Ketika `next_maneuver` ada di dictionary `prog` (hasil dari `remaining_progress`)
- Jarak ke poin belakala < `NAV_TURN_NOTIFY_DISTANCE_M` (150 meter)
- Semakin dekat ke poin belakala, field semakin spesifik
- Nilai di-set berdasarkan kalkulasi bearing/heading dari geometri polyline dan orientasi kurir

**Catatan:** Field `turn_instruction` tidak terpisah di `route_progress` sebagai field top-level, melainkan tersedia melalui `next_maneuver.instruction` di dalam objek `route_progress`.

---

### Pesan `turn_by_turn` (Pisah dari `route_progress`)

Sistem juga mengirim pesan khusus `turn_by_turn` ketika approaching a maneuver:

```json
{"type": "turn_by_turn", "ok": True, "route_id": 1, "leg_index": 0, "maneuver": {"instruction": "belok kiri", "distance_m": 85}}
```

Pesan ini dikirim ketika:
- `next_maneuver` ada di dictionary `prog` (hasil dari `remaining_progress(session, lat, lon)`)
- Jarak ke poin belakala < `NAV_TURN_NOTIFY_DISTANCE_M` (default 150 meter)
- Sistem menghitung arah terdepan berdasarkan geometri polyline dan orientasi kurir

---

### Lingkungan yang Mengontrol Fitur

| Variabel Environment | Default | Deskripsi |
|---------------------|---------|-----------|
| `NAV_TURN_NOTIFY_DISTANCE_M` | `150` | Ambang jarak (meter) sebelum poin belakala saat field `next_maneuver` akan muncul |
| `NAV_PROGRESS_MIN_INTERVAL_SECONDS` | `3` | Interval minimal (detik) antar pesan progress |

---

### Ringkasan Waktu Tiap Message

| Message | Dikirim Saat | Diterima Saat |
|---------|-------------|---------------|
| `ping` | Setiap `NAV_PROGRESS_MIN_INTERVAL_SECONDS` (default 3 dtk) | Setiap kali klien kirim ping |
| `position` | Setiap perubahan posisi (minimal 3 dtk) | Balasan ack setiap kirim |
| `start_navigation` | Saat klien mengirim `start_navigation` dengan route_id | Balasan ack dengan detail rute |
| `location_update` | Setiap `NAV_PROGRESS_MIN_INTERVAL_SECONDS` (default 3.5 dtk) | Balasan route_progress/off_route/warning + next_maneuver/turn_by_turn |
| `ack` | — | Setiap menerima pesan dari klien |
| `route_progress` | Setiap progres yang tercatat (minimal 3 dtk) | Balasan setelah location_update + next_maneuver |
| `off_route_warning` | Ketika `distance_m > OFF_ROUTE_THRESHOLD_M` (default 40m) | Balasan pertama kali keluar jalan |
| `turn_by_turn` | Ketika `next_maneuver` ada dan jarak < `NAV_TURN_NOTIFY_DISTANCE_M` (150m) | Balasan khusus petunjuk arah |
| `auto_rerouted` | Sistem menghitung rute baru yang lebih cepat | Balasan setelah perhitungan selesai dan diterapkan |
| `reroute_available` | Ketika `AUTO_REROUTE=0` dan ada deviasi | Balasan ketika ada rute alternatif tapi klien decided |

---

### Catatan Penting

1. **`next_maneuver` di `route_progress`** — hanyalah **informasi edukatif**, bukan perintah kontrol sistem
2. **`turn_by_turn` message** — pesan khusus yang dikirim ketika approaching a maneuver
3. **`NAV_TURN_NOTIFY_DISTANCE_M=150`** — menentukan kapan field `next_maneuver` akan muncul (jarak 150 meter sebelum poin belakala)
4. **Kurir tetap harus mematuhi lalu lintas dan hukum lalu lintas** — sistem hanya memberikan petunjuk, bukan perintah otomatis

### Teknis: Alur Data Turn-by-Turn

1. `remaining_progress(session, lat, lon)` di `app/services/navigation.py` menghitung:
   - `remaining_distance_m`, `remaining_time_s`, `progress_pct`
   - `next_maneuver` dari `get_next_maneuver(session.steps, current_step_idx)`
   - Semua hasil dimasukkan ke dictionary `prog`

2. Di `ws_endpoint` di `app/api/v1/endpoints/navigation.py`:
   - Dictionary `prog` di-spread ke JSON `route_progress` via `**prog`
   - Jika `next_maneuver` ada dan `distance_m <= NAV_TURN_NOTIFY_DISTANCE_M`, kirim `turn_by_turn` message
   - Field `next_maneuver` muncul di `route_progress` berdasarkan nilai dari `prog` (termasuk `instruction` dan `distance_m`)

3. `next_maneuver` dihasilkan dari `app/services/pathfinding/maneuvers.py`:
   - Menganalisis bearing change antar poin di rute
   - Mengklasifikasikan ke tipe: left, right, straight, uturn, etc.
   - Menghasilkan instruksi teks Indonesian: "Belok kiri", "Belok kanan", "Lurus", dsb.
| `start_navigation` | Saat klien mengirim `start_navigation` dengan route_id | Balasan ack dengan detail rute |
| `location_update` | Setiap `NAV_PROGRESS_MIN_INTERVAL_SECONDS` (default 3.5 dtk) | Balasan route_progress/off_route/warning beserta `next_maneuver`/turn_by_turn |
| `ack` | — | Setiap menerima pesan dari klien |
| `route_progress` | Setiap progres yang tercatat (minimal 3 dtk) | Balasan setelah location_update beserta `next_maneuver` |
| `off_route_warning` | Ketika `distance_m > NAV_TURN_NOTIFY_DISTANCE_M` (150m) | Balasan pertama kali keluar jalan |
| `auto_rerouted` | Sistem menghitung rute baru yang lebih cepat | Balasan setelah perhitungan selesai dan diterapkan |
| `reroute_available` | Ketika `AUTO_REROUTE=0` dan ada deviasi | Balasan ketika ada rute alternatif tapi klien decided |

---

## 4. Ringkasan Lengkap

| Fitur | WS driver/position | WS navigation |
|---|---|---|
| **Tujuan** | Tracking posisi GPS kurir | Navigasi & auto-reroute kurir dengan turn-by-turn |
| **Pesan utama** | `position` → `ack` + geofence events | `start_navigation` / `location_update` |
| **Push events** | `geofence_enter`, `geofence_exit` | `off_route_warning`, `auto_rerouted`, `reroute_available` |
| **Rate limit** | `KURIR_POS_MAX_RATE_SECONDS` (default 3 dtk) | `NAV_POS_MAX_RATE_SECONDS` (default 3 dtk) |
| **TTL Redis** | `driver:pos:{kurir_id}` (10 menit default) | `driver:nav:{kurir_id}` (TTL tergantung rute) |
| **Auth** | `?token=<JWT>` / `Authorization: Bearer` | `?token=<JWT>` / `Authorization: Bearer` |
| **Status endpoint** | `GET /api/v1/ws/driver/position/status` | `GET /api/v1/ws/navigation/status` |
| **Fitur tambahan** | Geofence detection | **Turn-by-turn instructions** (via `next_maneuver` in `route_progress`) |

---

### Field `next_maneuver` Detail

| Nilai | Ketika Muncul | Deskripsi |
|---|---|---|
| `"belok kiri"` | Jarak ke poin belakala < 150m, arah berikutnya ke kiri | Kurir harus memanuever ke kiri (dari `next_maneuver.instruction`) |
| `"right turn"` | Jarak ke poin belakala < 150m, arah berikutnya ke kanan | Kurir harus memanuever ke kanan (dari `next_maneuver.instruction`) |
| `"lanjut lurus"` | Jarak ke poin belakala < 150m, arah berikutnya terus | Kurir terus lurus tanpa belok (dari `next_maneuver.instruction`) |
| `"melampau"` | Jarak ke poin belakala < 150m, ada kendaraan/halangan dekat | Kurir melampau kendaraan/halangan (dari `next_maneuver.instruction`) |
| `""` (kosong) | Jarak ke poin belakala >= 150m | Tidak ada petunjuk arah dekat depan ( `next_maneuver` kosong atau tidak ada) |

### Contoh Alur Penggunaan

1. Klien melakukan `start_navigation` dengan `route_id: 1`
2. Klien menerima `route_progress` berkala dengan `next_maneuver` kosong (jarak > 150m)
3. Saat jarak ke poin belakala mengecil menjadi 120 meter, `next_maneuver` muncul dengan `instruction: "belok kiri"`
4. Klien melihat `route_progress` dengan `next_maneuver: {"instruction": "belok kiri", "distance_m": 85}` dan $menyenyapakan operasi sesuai
5. Saat kurir mendekati poin belakala (jarak < 30 meter), `next_maneuver` mungkin berubah menjadi `{"instruction": "lanjut lurus"}` atau `{"instruction": "melampau"}` sesuai kondisi

---

## 5. Cara Test Semua WS endpoint

```bash
# 1. Login dulu
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"joko","password":"rahasia123"}'

# 2. Cek status WS
curl http://localhost:8000/api/v1/ws/driver/position/status
# expect: {"enabled":true,"redis_connected":true,"max_rate_seconds":3.0}

curl http://localhost:8000/api/v1/ws/navigation/status
# expect: {"enabled":true,"redis_connected":true,"off_route_threshold_m":40.0,"reroute_cooldown_s":30.0,"auto_reroute":true,"max_rate_seconds":3.0}

# 3. Test WS tracking
wscat -c "ws://localhost:8000/api/v1/ws/driver/position?token=<TOKEN>"
# Kirim: {"type":"position","lat":-6.8060,"lon":110.8390}
# Balas: {"type":"ack","ok":true,"stored":true,"snapped":[-6.806,-110.839],"ts":...}

# 4. Test WS navigation
wscat -c "ws://localhost:8000/api/v1/ws/navigation?token=<TOKEN>"
# Kirim: {"type":"start_navigation","route_id":1,"leg_index":0}
# Balas: {"type":"ack","ok":true,"route_id":1,"leg_index":0,"kind":"multi",...}

# 5. Pantau `next_maneuver` di `route_progress`
# Saat jarak < 150m, akan muncul field: "next_maneuver": {"instruction": "belok kiri", "distance_m": 85}
```

---

## 5. Referensi Kode

Dokumentasi ini dibuat berdasarkan kode di:
- `app/api/v1/endpoints/navigation.py` — Logika navigasi dan fitur turn-by-turn
- `app/api/v1/endpoints/tracking.py` — Logika geofence dan tracking posisi
- `app/core/logging.py` — Log structlog dan correlation ID
- Variabel environment: `NAV_PROGRESS_MIN_INTERVAL_SECONDS`, `NAV_TURN_NOTIFY_DISTANCE_M`

---
*Catatan: Fitur turn-by-turn (`next_maneuver`) adalah penambahan baru dan opsional. Sistem masih bisa berfungsi tanpa field ini. Field ini akan kosong (`""`) ketika jarak ke poin belakala lebih besar dari `NAV_TURN_NOTIFY_DISTANCE_M`.*