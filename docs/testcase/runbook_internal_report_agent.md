# Test Case: Internal Report Agent (Laporan Kurir → Reroute)

Runbook manual untuk menguji **agent laporan kurir**: laporan insiden jalan dari
satu kurir diklasifikasi AI (severity + lokasi), dicocokkan ke rute aktif kurir
lain, lalu memicu reroute + push event WS bila berdampak.

```
Alur end-to-end (HTTP + WS) — panah = arah message (client→server / server→client):

[A] HTTP POST /api/v1/auth/login (kurir B) ─────────────► token + kurir_id B
[B] HTTP POST /api/v1/pathfinding/find-optimized-delivery-route (kurir B)
    (Authorization: Bearer <token B>) ──────────────────► route_id B (snapshot Redis)
[C] WS /api/v1/ws/navigation?token=<token B> + start_navigation ──► rute aktif

[D] HTTP POST /api/v1/driver-reports (kurir A, dekat rute B)
    { text, latitude, longitude } ───────────────────────► { id, status:"pending" }
     └─ insert tabel driver_report (status pending)

[E] Worker internal_report_agent (tiap INTERNAL_REPORT_INTERVAL_S):
    └─ process_pending_reports() → tiap laporan pending:
       _classify_report()  → Gemini: {severity, lat, lng, radius_m, reason}
       └─ severity < REPORT_SEVERITY_MIN  → status "ignored"  (skip)
       └─ koordinat: lat/lng report → klasifikasi → geocode_address(text) → tanpa
          koordinat → status "ignored"
       └─ _snap_incidents_to_edges() → penalti per edge jalan
       └─ cocokkan NavRegistry.all() → jarak insiden ke polyline sesi
          ≤ REPORT_IMPACT_RADIUS_M  (default 500 m)
       └─ compute_reroute(..., extra_penalties) → rute baru
          └─ server ◄── WS kurir B:
             {"type":"auto_rerouted"|"reroute_available","reason":"Laporan kurir #<id>: …",
              "source":"driver_report", ...}
       status laporan → "processed" (atau "ignored"), processed_at diisi
```

Berbeda dengan reroute traffic (`_should_traffic_reroute` mensyaratkan penghematan
≥ `TRAFFIC_REROUTE_MIN_SAVING_SECONDS`), **reroute laporan kurir langsung
diterapkan** begitu rute baru berhasil ditemukan (tanpa threshold penghematan),
tetapi tetap memakai cooldown anti-flicker `REROUTE_COOLDOWN_SECONDS` dan update
`session.coords` ke rute baru.

Base URL: `http://localhost:8000` • WS URL: `ws://localhost:8000`

---

## Referensi Message & Aturan (pahami sebelum tes)

### Endpoint laporan

`POST /api/v1/driver-reports` (auth: `Authorization: Bearer <token kurir>`)

| Field | Tipe | Keterangan |
|---|---|---|
| `text` | string (min 1) | **Wajib.** Deskripsi insiden jalan (mis. "Pohon tumbang, jalan tertutup total"). |
| `latitude` | float \| null | Opsional. Koordinat insiden. Bila kosong, diperoleh dari klasifikasi AI atau geocode alamat. |
| `longitude` | float \| null | Opsional. |

Respons `ok`: `{"success":true,"message":"Laporan diterima","data":{"id":…,"kurir_id":…,"status":"pending","created_at":…}}`.
Tanpa token / token invalid → `err` 401 `{"success":false,...}`.

### Status laporan (tabel `driver_report`)

| Status | Arti |
|---|---|
| `pending` | Baru disubmit, belum diproses agent. |
| `processed` | Diproses (baik berdampak maupun tidak — tidak ada sesi cocok / tanpa penalti). |
| `ignored` | Severity < `REPORT_SEVERITY_MIN`, atau koordinat tidak dapat ditentukan. |

### Event WS yang dipicu (server → client, kurir yang rutenya terdampak)

```json
{ "type": "auto_rerouted", "route_id": 12, "polyline": "<encoded>",
  "eta_s": 482.0, "saving_s": -18.5, "applied": true,
  "reason": "Laporan kurir #5: Pohon tumbang, jalan tertutup total",
  "source": "driver_report" }
```

- `type` = `auto_rerouted` bila `AUTO_REROUTE=1`, selain itu `reroute_available`.
- `saving_s` dihitung dari sisa waktu rute lama (via `remaining_progress`) dikurangi
  `eta_s` rute baru — **bisa negatif** (laporan memaksa hindari jalan, bukan memilih
  rute tercepat); itu wajar karena tidak ada threshold hemat waktu.
- `applied` = `AUTO_REROUTE`. `source` selalu `"driver_report"` (pembeda dari event
  traffic / off-route yang tanpa `source`).

## 0. Prasyarat & Setup

1. Pastikan `.env` berisi:

   ```ini
   ENABLE_LIVE_NAVIGATION=1
   AUTO_REROUTE=1
   REDIS_HOST=redis
   REDIS_PORT=6379
   DATABASE_URL=postgresql+asyncpg://test:test@db:5432/test2

   # --- Internal Report Agent ---
   INTERNAL_REPORT_AGENT_ENABLED=1
   INTERNAL_REPORT_INTERVAL_S=30
   REPORT_IMPACT_RADIUS_M=500
   REPORT_SEVERITY_MIN=HIGH
   REPORT_BATCH=20

   # --- AI (klasifikasi laporan) ---
   GEMINI_API_KEY=<API_KEY>
   GEMINI_MODEL=gemini-2.5-flash
   ```

   > **PENTING**: env dibaca saat import (`app/services/internal_report_agent.py`).
   > Setelah mengubah nilai, **restart app**.

2. Jalankan stack:

   ```bash
   docker compose up --build
   ```

3. Log startup mengonfirmasi agent aktif (hanya bila `INTERNAL_REPORT_AGENT_ENABLED=1`):

   ```
   [REPORT] Agent aktif (interval 30s, severity min HIGH).
   ```

## 1. Seed Data

```bash
uv run python scripts/seed_kurir.py     # login: joko / rahasia123 (dan lainnya)
uv run python scripts/seed_hub.py
uv run python scripts/seed_paket.py
```

## 2. Login Dua Kurir (kurir B = yang sedang navigasi; kurir A = pelapor)

```powershell
$rA = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/auth/login `
  -ContentType "application/json" -Body '{"username":"joko","password":"rahasia123"}'
$TOKEN_B = $rA.data.token; $KURIR_B = $rA.data.kurir.id

# login kurir kedua (mis. username kurir lain dari seed_kurir.py) sebagai pelapor
$rB = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/auth/login `
  -ContentType "application/json" -Body '{"username":"<kurir2>","password":"<pass>"}'
$TOKEN_A = $rB.data.token; $KURIR_A = $rB.data.kurir.id
```

## 3. Buat Rute Aktif Kurir B (multi-leg)

Sama seperti runbook AI reroute — POST `find-optimized-delivery-route` dengan
`Authorization: Bearer $TOKEN_B`, catat `route_id`. Verifikasi snapshot:

```bash
docker compose exec redis redis-cli GET driver:nav:{KURIR_B}
```

Hubungkan WS kurir B dan `start_navigation`, lalu `location_update` di sepanjang
polyline (≥ 3 dtk antar pesan) supaya `last_position` sesi terisi — **tanpa
`last_position`, sesi tidak dievaluasi oleh agent**.

## 4. Submit Laporan Kurir A di Dekat Rute B

Ambil sebuah titik `(lat, lng)` di tengah polyline rute B (jarak ke polyline
< `REPORT_IMPACT_RADIUS_M`), lalu:

```bash
curl -X POST http://localhost:8000/api/v1/driver-reports \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN_A" \
  -d '{
    "text": "Pohon tumbang menutup jalan, kendaraan tidak bisa lewat",
    "latitude": <lat>, "longitude": <lng>
  }'
```

**Kriteria lulus:** `200` dengan `data.status:"pending"`, `data.id` positif.

Cek baris di DB:

```bash
docker compose exec db psql -U test -d test2 -c \
  "SELECT id, kurir_id, status, severity FROM driver_report ORDER BY id DESC LIMIT 5;"
```

## 5. Tunggu Worker Proses (interval)

Tunggu ≥ `INTERNAL_REPORT_INTERVAL_S` (30 dtk). Log agent:

```
[REPORT] 1 laporan diproses.
[REPORT] Kurir 7 auto_rerouted karena laporan #5.
```

**Harapkan di WS kurir B** (rute terdampak):

```json
{ "type":"auto_rerouted","route_id":12,"polyline":"<encoded>",
  "eta_s":..., "saving_s":..., "applied":true,
  "reason":"Laporan kurir #5: Pohon tumbang menutup jalan, kendaraan tidak bisa lewat",
  "source":"driver_report" }
```

**Kriteria lulus:**

- WS kurir B menerima event `auto_rerouted` dengan `source:"driver_report"`.
- `reason` memuat `Laporan kurir #<id>` dan cuplikan teks laporan.
- Laporan di DB kini `status:"processed"`, `severity:"HIGH"`, `processed_at` terisi.
- `session.coords` kurir B sudah = rute baru (kirim `location_update` berikutnya →
  `route_progress` mengikuti polyline baru).

## 6. Gate Severity — Laporan Ringan Diabaikan

1. Submit laporan dengan teks insiden ringan (mis. "Ada genangan kecil di bahu jalan,
   masih bisa lewat") di titik dekat rute B.
2. Tunggu siklus worker.

**Harapkan:**

- Log `[REPORT] 1 laporan diproses.` (laporan tetap diproses, hanya diabaikan).
- **Tidak ada** event WS `auto_rerouted`/`reroute_available` untuk kurir B.
- DB: `status:"ignored"`, `severity:"MEDIUM"` (bila model menilai MEDIUM; klasifikasi
  non-deterministik — pastikan severity hasil benar-benar < `REPORT_SEVERITY_MIN`).

> Bila model menilai HIGH walau teks ringan, ganti teks jadi jelas LOW/MEDIUM
> (mis. "Parkir truk, lewatnya masih lancar"). Alternatif deterministik: set
> `REPORT_SEVERITY_MIN=BLOCKING` di `.env`, restart, lalu semua laporan non-BLOCKING
> diabaikan.

## 7. Tanpa Koordinat — Fallback Geocode / Ditolak

1. Submit laporan **tanpa** `latitude`/`longitude`, teks berisi alamat yang bisa
   di-geocode (mis. "Jalan Sunan Muria dekat alun-alun Kudus, jalan ditutup karena
   pasar tumpah").
2. Tunggu worker. Karena `REPORT_SEVERITY_MIN=HIGH`, laporan akan diproses:
   - Bila klasifikasi AI berhasil memberi `lat`/`lng` → diproses seperti biasa.
   - Bila tidak (dan `geocode_address` gagal / Nominatim tidak aktif) → `ignored`.

**Harapkan:** `status` di DB = `processed` **atau** `ignored` (dengan koordinat
berhasil/tidak). Untuk uji deterministik "tanpa koordinat": pakai teks acak tanpa
lokasi (mis. "jaringan rusak") sambil `REPORT_SEVERITY_MIN=HIGH` dan AI tidak memberi
lat/lng → `ignored`.

## 8. Rute Jauh — Tanpa Dampak (processed, affected 0)

1. Submit laporan HIGH di lokasi yang jauh dari semua rute aktif (jarak ke setiap
   polyline sesi > `REPORT_IMPACT_RADIUS_M`), atau pastikan tidak ada kurir sedang
   navigasi.
2. Tunggu worker.

**Harapkan:** laporan `status:"processed"`, **tanpa** event WS. Log:
```
[REPORT] 1 laporan diproses.
```

## 9. Uji Otomatis (Regression)

```bash
uv run python -m pytest tests/test_internal_report_agent.py -q
```

> Gunakan `python -m pytest` (bukan `uv run pytest`) agar `app` dapat di-import.
> Cakupan: parser klasifikasi, gate severity, `_classify_report` (mock), `_handle_report`
> (HIGH matched → event terkirim; MEDIUM → ignored; tanpa koordinat → ignored;
> tanpa penalti / tanpa sesi cocok → processed affected 0), `process_pending_reports`,
> `_token_kurir_id`, model & schema.

Setelahnya jalankan regresi terkait (navigation & pathfinding):

```bash
uv run python -m pytest tests/test_navigation.py tests/test_route_options.py tests/test_ai_agent.py -q
```

## 10. Troubleshooting

| Gejala | Penyebab & Solusi |
|---|---|
| Log `[REPORT] Agent aktif ...` tidak muncul | `INTERNAL_REPORT_AGENT_ENABLED=0` atau env dibaca saat import — set `1`, restart. |
| Laporan tetap `pending` lama | Interval belum lewat (`INTERNAL_REPORT_INTERVAL_S`) atau agent nonaktif. Cek log `[REPORT]`. |
| Event WS tidak terkirim padahal laporan HIGH | Sesi target tanpa `last_position` (belum kirim `location_update`), jarak insiden > `REPORT_IMPACT_RADIUS_M`, `compute_reroute` gagal (area belum pre-warm: `uv run python scripts/prewarm_route.py <lat1> <lon1> <lat2> <lon2>`), atau cooldown aktif. |
| `status:"ignored"` walau laporan jelas berdampak | `REPORT_SEVERITY_MIN` terlalu tinggi, atau koordinat tidak terselesaikan (tidak ada lat/lng report, AI tanpa lat/lng, geocode gagal). |
| `severity` null / `MEDIUM` mengejutkan | Klasifikasi AI non-deterministik. Ulangi dengan teks lebih tegas (mis. "jalan tertutup total / tidak bisa lewat"). |
| Reroute diterapkan walau rute baru lebih lambat | **By design**: laporan kurir langsung diterapkan (tanpa threshold hemat). Gunakan `REPORT_IMPACT_RADIUS_M` / severity untuk mengendalikan. |
| Agent crash saat siklus | Satu laporan gagal tidak mematikan siklus (di-catch per laporan). Periksa log `[REPORT] Gagal proses laporan ...`. |
| Tidak ada penalti dari `_snap_incidents_to_edges` | Titik insiden tidak mengenai edge graf (jalan gang). Perbesar radius / pastikan titik di jalan utama. |

## 11. Kriteria Lulus Keseluruhan

- [ ] `POST /api/v1/driver-reports` tanpa token → `err` 401; dengan token → `200`, `status:"pending"`.
- [ ] Laporan HIGH di dekat rute aktif → WS kurir lain menerima `auto_rerouted`/`reroute_available` dengan `source:"driver_report"` dan `reason:"Laporan kurir #<id>: …"`.
- [ ] Severity < `REPORT_SEVERITY_MIN` → `ignored`, tanpa event.
- [ ] Tanpa koordinat (dan geocode gagal) → `ignored`; dengan koordinat valid → diproses.
- [ ] Insiden jauh dari semua rute aktif → `processed` tanpa event.
- [ ] Semua laporan akhirnya berstatus `processed`/`ignored` dengan `processed_at` terisi.
- [ ] Regresi: `uv run python -m pytest tests/test_internal_report_agent.py tests/test_navigation.py tests/test_route_options.py -q` → semua lolos.
