# Test Case: AI Reroute Agent (Google Gemini) — Real-time Navigation

Runbook manual untuk menguji **keputusan auto-reroute berbasis LLM Agent** yang
menyatu dengan real-time navigation (`WS /api/v1/ws/navigation`).

```
Alur end-to-end (HTTP + WS) — panah = arah message (client→server / server→client):

[1] HTTP POST /api/v1/auth/login ─────────────────────► token + kurir_id
[2] HTTP POST /api/v1/pathfinding/find-optimized-delivery-route
    (Authorization: Bearer <token>) ──────────────────► route_id
    └─ snapshot tersimpan di Redis: driver:nav:{kurir_id}
       (route_id, kind "multi", legs[])
[3] WS /api/v1/ws/navigation?token=<token>
    client ──► {"type":"start_navigation","route_id":<id>,"leg_index":0}
    server ◄── {"type":"ack", ..., "polyline":"<polyline leg aktif>"}

[4] Loop posisi (interval >= 3 dtk):
    client ──► {"type":"location_update","lat":...,"lng":...,"bearing":...,"speed":...}
    ├─ on-route            → server ◄── {"type":"route_progress"}
    └─ off-route (> 40 m)  → decide_reroute(..., hint="off_route")
                              └─ app/services/ai_agent.py (Google Gemini + Function Calling)
                                 ├─ apply  → off_route_warning + auto_rerouted (rute baru)
                                 ├─ ignore → tanpa event (anti-spam)
                                 ├─ defer  → off_route_warning (tanpa reroute)
                                 └─ None (disabled/error/timeout/breaker) → fallback
                                    deterministik (threshold OFF_ROUTE_THRESHOLD_M /
                                    TRAFFIC_REROUTE_MIN_SAVING_SECONDS)

[5] Sesudah tiba di ujung leg aktif (POD):
    client ──► {"type":"complete_leg"}  (alias "pod_submitted")
    server ◄── {"type":"ack","action":"complete_leg","leg_index":<N+1>,
                "polyline":"<polyline leg berikutnya>"}
    ... leg terakhir selesai → server ◄── {"type":"route_complete"} (snapshot dihapus)

[6] Jalur paralel: navigation_worker (tiap 30 dtk, hint traffic) →
    decide_reroute(..., hint="traffic") → jalur keputusan sama seperti [4].
```

Keputusan yang dihasilkan `{"action": "apply"|"ignore"|"defer", "reason": "..."}`
atau **`None`** sebagai sinyal *fallback* ke logika threshold lama
(`TRAFFIC_REROUTE_MIN_SAVING_SECONDS`, `OFF_ROUTE_THRESHOLD_M`).

Fitur hardening yang ikut diuji:

- **Backup API key** (`GEMINI_API_KEY_2`): rotasi otomatis hanya saat key 1 kena
  rate-limit HTTP 429 / `RESOURCE_EXHAUSTED`.
- **Semaphore konkuransi** (`GEMINI_MAX_CONCURRENT`): batasi panggilan LLM paralel.
- **Circuit breaker** (`GEMINI_BREAKER_THRESHOLD`, `GEMINI_BREAKER_RESET_S`):
  skip AI sementara setelah kegagalan beruntun.
- **Sanitasi `reason`**: strip karakter kontrol, batas 500 karakter.

Base URL: `http://localhost:8000` • WS URL: `ws://localhost:8000`

---

## Pola Message & Aturan WS (pahami sebelum tes)

> **Bukan "one-shot webhook".** `WS /api/v1/ws/navigation` adalah koneksi persisten
> dua arah. `start_navigation` dikirim **sekali** sebagai handshake (dibalas `ack` +
> polyline leg aktif), lalu **client wajib terus mengirim `location_update`**
> (telemetri GPS, interval ≥ 3 dtk) — tanpa ini tidak ada `route_progress` dan tidak
> ada deteksi off-route. Satu-satunya kiriman mandiri server adalah
> `navigation_worker` tiap 30 dtk (push `auto_rerouted` bila ada rute lebih cepat).

### Request message (client → server)

| Message | Field | Keterangan |
|---|---|---|
| `ping` | — | Heartbeat → dibalas `ack`. |
| `start_navigation` | `route_id` (wajib), `leg_index` (opsional, default 0) | Muat snapshot dari Redis → `ack` berisi polyline leg aktif. Gagal (error) bila snapshot tak ada / `route_id` tak cocok. |
| `location_update` | `lat` + `lng` (atau `lon`) wajib; `bearing`, `speed`, `current_route_id` opsional | Telemetri posisi. `current_route_id` **tidak dibaca server**. Memicu `route_progress`, deteksi off-route, auto-reroute. |
| `complete_leg` | — | POD selesai → pindah ke leg berikutnya. |
| `pod_submitted` | — | Alias `complete_leg`. |

> Message dengan `type` tak dikenal diabaikan senyap; JSON tidak valid →
> `error` `{"type":"error","ok":false,"detail":"JSON tidak valid"}`.

### Balasan (server → client)

| Type | Kapan dikirim |
|---|---|
| `ack` | `ping`, `start_navigation` (polyline leg aktif), `complete_leg` (leg baru). |
| `route_progress` | Setiap `location_update` (bila rute aktif & interval ≥ 3 dtk): `remaining_distance_m`, `remaining_time_s`, `progress_pct`. |
| `off_route_warning` | Deviasi > `OFF_ROUTE_THRESHOLD_M` dari polyline aktif (edge-trigger, sekali per masuk off-route). |
| `auto_rerouted` / `reroute_available` | Keputusan AI `apply` (off-route) atau worker traffic; `applied` = `AUTO_REROUTE`. |
| `route_complete` | Semua leg selesai (via `complete_leg`). |
| `error` | Validasi gagal / snapshot tak ditemukan / JSON invalid. |

### Aturan `leg_index`

- `leg_index` **0-based** = indeks array `legs[]` pada respons `find-optimized-delivery-route`
  (dan `legs[].index` di snapshot Redis). `stop_sequence_number` = `leg_index + 1` (1-based).
- Dest sebuah leg = stop yang dituju leg tsb (`recipient_name`/`package_id`). Bila
  `return_to_hub: true`, ada leg terakhir menuju Hub (`package_id: null`).
- `start_navigation` cukup memakai `leg_index: 0` (mulai dari awal); setelah itu server
  maju otomatis via `complete_leg` — client tidak perlu menghitung index sendiri.
- `ack` `complete_leg` mengembalikan `leg_index` + `dest` + `polyline` leg baru.

### Contoh alur (route_id 1; leg: Budi → Siti → Hub)

```
client: {"type":"start_navigation","route_id":1,"leg_index":0}
server: {"type":"ack","ok":true,"route_id":1,"leg_index":0,"kind":"multi","polyline":"<leg 0>",...}

client: {"type":"location_update","lat":-6.81,"lng":110.85,"bearing":90,"speed":20}
server: {"type":"route_progress","ok":true,"route_id":1,"leg_index":0,"progress_pct":...,...}
        (bila deviasi > 40 m) → {"type":"off_route_warning",...} [±] {"type":"auto_rerouted",...}

client: {"type":"complete_leg"}                                     # POD Budi
server: {"type":"ack","action":"complete_leg","leg_index":1,"recipient_name":"Siti","dest":[...],"polyline":"<leg 1>",...}
client: {"type":"complete_leg"}                                     # POD Siti
server: {"type":"ack","action":"complete_leg","leg_index":2,"recipient_name":"Hub",...}
client: {"type":"complete_leg"}                                     # kembali hub
server: {"type":"route_complete","ok":true,"route_id":1,"ts":...}   # snapshot dihapus
```

## 0. Prasyarat & Setup

1. Pastikan `.env` berisi:

   ```ini
   ENABLE_LIVE_NAVIGATION=1
   AUTO_REROUTE=1
   REDIS_HOST=redis
   REDIS_PORT=6379

   # --- AI Agent ---
   AI_REROUTE_ENABLED=1
   GEMINI_API_KEY=<API_KEY_1>
   # GEMINI_API_KEY_2=<API_KEY_2>   # opsional, cadangan rate-limit
   GEMINI_MODEL=gemini-2.5-flash
   GEMINI_REROUTE_TIMEOUT_S=2.0
   GEMINI_MAX_CONCURRENT=3
   GEMINI_BREAKER_THRESHOLD=3
   GEMINI_BREAKER_RESET_S=30
   ```

   > **PENTING**: env AI dibaca saat *import* (`app/services/ai_agent.py`). Setelah
   > mengubah nilai AI, **restart app** (bukan sekadar reload worker).

2. Jalankan stack:

   ```bash
   docker compose up --build
   ```

   (db + redis + app; app di port 8000.)

3. Verifikasi status navigation (kini ada blok `ai`):

   ```bash
   curl http://localhost:8000/api/v1/ws/navigation/status
   ```

   **Harapkan:**

   ```json
   { "enabled": true, "redis_connected": true, "off_route_threshold_m": 40.0,
     "reroute_cooldown_s": 30.0, "auto_reroute": true, "max_rate_seconds": 3.0,
     "ai": { "enabled": true, "backup_key_available": true,
             "circuit_breaker_open": false, "concurrent_slots": 3,
             "timeout_s": 2.0 } }
   ```

   - `ai.enabled` harus `true` (butuh `AI_REROUTE_ENABLED=1` **dan** `GEMINI_API_KEY`).
   - `ai.backup_key_available` `true` bila `GEMINI_API_KEY_2` diisi.
   - `ai.circuit_breaker_open` harus `false` saat start.

> Biaya & latensi: dengan AI aktif, setiap keputusan memanggil Gemini (nyata,
> berbayar). Untuk pengujian circuit breaker gunakan key **invalid** (Bagian 8)
> agar tidak membakar kuota.

## 1. Seed Data

Jalankan dari root repo (butuh DB up):

```bash
uv run python scripts/seed_kurir.py     # login: joko / rahasia123
uv run python scripts/seed_hub.py
uv run python scripts/seed_paket.py     # paket RESI-2026-0001..0005 (Kudus)
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

## 3. Buat Snapshot Rute (Bearer token + `ENABLE_LIVE_NAVIGATION=1`)

> **PRASYARAT KRITIS**:
> 1. HTTP call **wajib** menyertakan header `Authorization: Bearer <token>` yang
>    **sama** dengan token di WebSocket (`kurir_id` diambil dari token).
> 2. `dynamic_rerouting: true` di payload **dianjurkan** tetapi hanya **wajib**
>    bila `ENABLE_LIVE_NAVIGATION=0`. Dengan `ENABLE_LIVE_NAVIGATION=1` (sudah
>    di-set di Bagian 0), snapshot `driver:nav:{kurir_id}` dibuat otomatis walau
>    field ini dihilangkan (cek `nav_enabled()` di `pathfinding.py`).

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

**Kriteria lulus:** status `200`, respons memuat `route_id` (angka positif),
`legs[0].geometry` encoded polyline, dan snapshot terisi di Redis:

```bash
docker compose exec redis redis-cli GET driver:nav:{KURIR_ID}
```

## 4. Navigation WS — Start + Ikuti Jalur

Hubungkan ke `ws://localhost:8000/api/v1/ws/navigation?token=<TOKEN>`, lalu kirim:

```json
{"type":"start_navigation","route_id":<ROUTE_ID>,"leg_index":0}
```

**Harapkan `ack`** berisi `polyline` leg aktif. Kirim `location_update` sepanjang
polyline (interval ≥ 3 dtk antar pesan) untuk memantapkan posisi sebelum deviasi:

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

## 5. Multi-Leg & POD Advance (`complete_leg` / `pod_submitted`)

Rute dari `find-optimized-delivery-route` bersifat **multi-leg** (`kind: "multi"`):
satu rute = beberapa leg pengiriman, dan tiap leg berakhir di `dest` milik
`recipient_name` / `package_id` tertentu. Setelah tiba di akhir leg aktif dan paket
diserahkan (POD), client mengirim `complete_leg` (alias `pod_submitted`) → server
memindah pointer ke leg berikutnya dan membalas `ack` berisi polyline leg baru.
Pada leg **terakhir**, server mengirim `route_complete` dan membersihkan snapshot.

**Contoh alur message (client → server / server → client):**

```json
{"type":"complete_leg"}
```

```json
{ "type": "ack", "ok": true, "action": "complete_leg", "route_id": 1,
  "leg_index": 1, "kind": "multi",
  "package_id": "<package_id>", "recipient_name": "Siti",
  "dest": [ <dest leg 1> ], "polyline": "<encoded polyline leg 1>",
  "off_route_threshold_m": 40.0, "ts": ... }
```

```json
{ "type": "route_complete", "ok": true, "route_id": 1, "ts": ... }
```

**Langkah tes (lanjutan Bagian 4, sesudah leg 0 selesai):**

1. Ikuti polyline leg aktif sampai mendekati `dest` (ujung polyline).
2. Kirim `{"type":"complete_leg"}` — atau `{"type":"pod_submitted"}` (diperlakukan
   sama oleh server).
3. **Harapkan** `ack` dengan `action:"complete_leg"` dan `leg_index` bertambah
   (+1), serta `polyline` berisi leg baru → jadikan acuan segmen berikutnya.
4. Ulangi hingga leg terakhir → harapkan **`route_complete`** (bukan `ack`).

**Verifikasi snapshot bersih (Redis):**

```bash
docker compose exec redis redis-cli GET driver:nav:{KURIR_ID}
# sesudah route_complete → (nil)
```

**Error case yang perlu dicoba:**

| Input | Harapkan |
|---|---|
| `complete_leg` sebelum `start_navigation` | `{"type":"error","ok":false,"detail":"Tidak ada rute aktif untuk diselesaikan."}` |
| `complete_leg` saat snapshot hilang / `route_id` tidak cocok | `{"type":"error","ok":false,"detail":"Snapshot rute tidak ditemukan di Redis."}` atau `"...tidak cocok / leg tidak valid."` |
| Rute satu-leg (`kind:"single"`) + `complete_leg` | langsung `route_complete` (tanpa `ack complete_leg`) |

> `complete_leg` tidak butuh body tambahan; kurir diidentifikasi dari token WS.

## 6. Skenario A — Off-route: AI `apply`

Kirim `location_update` yang menyimpang > `OFF_ROUTE_THRESHOLD_M` (default 40 m),
mis. geser `+0.0015` derajat ke samping (~150-250 m, tergantung geometri polyline):

```json
{"type":"location_update","lat":<lat+0.0015>,"lng":<lng+0.0015>,
 "bearing":90,"speed":20,"current_route_id":<ROUTE_ID>}
```

> `current_route_id` **tidak dibaca server** — hanya `lat`/`lng`/`bearing`/`speed`
> yang dipakai. `distance_m` dihitung sebagai jarak tegak lurus titik ke polyline,
> jadi tidak bisa dipatok tepat; yang penting `distance_m > threshold_m`.

**Kriteria lulus** (payload sama seperti mode deterministik):

```json
{ "type":"off_route_warning","ok":true,"route_id":<ROUTE_ID>,
  "distance_m": <angka > 40.0>, "threshold_m": 40.0, "ts":... }

{ "type":"auto_rerouted","ok":true,"route_id":<ROUTE_ID>,
  "polyline":"<encoded polyline baru>","saving_s":...,
  "eta_s":..., "applied":true, "reason":"off_route" }
```

**Cara memastikan keputusan berasal dari AI:**

> Payload WS `reason` off-route selalu `"off_route"` (hardcoded). Konfirmasi jalur
> AI dari **log server** (`docker compose logs -f app`):
>
> - Tidak ada `[AI] decide_reroute(off_route) error/timeout` → panggilan Gemini sukses.
> - Tidak ada `[NAV] AI abaikan off-route ...` / `[NAV] AI tunda ...` → keputusan
>   model = `apply`.
> - Bila ada `[AI] decide_reroute(off_route) timeout setelah 2.0s.` → model lambat
>   melewati `GEMINI_REROUTE_TIMEOUT_S`; fallback deterministik yang menangani
>   (perilaku tetap benar, hanya bukan AI).

> Untuk memastikan `auto_rerouted` muncul, pre-warm area leg aktif:
> `uv run python scripts/prewarm_route.py <lat1> <lon1> <lat2> <lon2>` dan tunggu
> `REROUTE_COOLDOWN_SECONDS` (30 dtk).

## 7. Skenario B — Off-route: AI `ignore` / `defer`

Output model **non-deterministik**. Uji dengan mengulang deviasi dan verifikasi
konsistensi perilaku terhadap keputusan yang di-log:

| Deviasi | Cenderung dihasilkan model | Perilaku yang harus tampil |
|---|---|---|
| ~45 m (tepat di atas threshold 40 m) | `ignore` (dianggap GPS noise) | Tidak ada `off_route_warning`; log `[NAV] AI abaikan off-route kurir X (<reason>).` |
| ~800 m (jelas keluar jalur) | `apply` | `off_route_warning` + `auto_rerouted` |
| sedang (mis. ~150 m) | `defer` atau `apply` | `defer` → `off_route_warning` **tanpa** `auto_rerouted`; log `[NAV] AI tunda reroute kurir X (<reason>).` |

**Kriteria lulus:**

- Setiap keputusan yang di-log punya `reason` natural-language yang valid
  (non-kosong, tidak berisi karakter kontrol / baris aneh — ter-sanitasi).
- Perilaku WS sesuai kolom kanan untuk keputusan yang sama.
- `ignore` → `off_route_active` di-set `false`, tidak ada reroute (anti-spam).

## 8. Skenario C — Circuit Breaker (Deterministik, tanpa biaya kuota)

Tujuan: buktikan AI berhenti sementara setelah kegagalan beruntun, lalu pulih.

1. Set `GEMINI_API_KEY=INVALID_KEY` (sengaja salah) di `.env`, restart app.
   > Key invalid → error (bukan rate-limit) → dihitung sebagai kegagalan breaker.
   > Rotasi backup **tidak** terjadi (by design hanya HTTP 429 memicu backup).
2. Pastikan `ai.enabled: true` di status (gate hanya cek keberadaan string).
3. Kirim deviasi berulang (Skenario A) beberapa kali. Setiap percobaan:
   - Log: `[AI] decide_reroute(off_route) error: ...`
   - Fallback deterministik tetap berjalan → `off_route_warning` + `auto_rerouted`
     (jika `compute_reroute` sukses) → **alur WS tidak terganggu**.
4. Setelah `GEMINI_BREAKER_THRESHOLD` (default 3) kegagalan beruntun:

   **Harapkan log:**

   ```
   [AI] Circuit breaker terbuka selama 30s (3 kegagalan beruntun).
   ```

   **Dan status:**

   ```bash
   curl http://localhost:8000/api/v1/ws/navigation/status
   # "ai": { ..., "circuit_breaker_open": true, ... }
   ```

5. Saat breaker terbuka, kirim deviasi lagi → **AI di-skip**:

   ```
   [AI] Circuit breaker terbuka; skip AI reroute.
   ```

   Fallback deterministik tetap menangani (WS tetap merespons).
6. Tunggu `GEMINI_BREAKER_RESET_S` (default 30 dtk) → breaker menutup otomatis:

   ```bash
   curl http://localhost:8000/api/v1/ws/navigation/status   # circuit_breaker_open: false
   ```

   Percobaan berikutnya kembali mencoba Gemini (gagal lagi → breaker terbuka lagi).

**Kriteria lulus:** breaker terbuka tepat setelah threshold, status endpoint
mencerminkannya, dan fallback deterministik tidak pernah terblokir.

## 9. Skenario D — Backup Key (Opsional, hanya saat 429 nyata)

Rotasi ke `GEMINI_API_KEY_2` hanya dipicu **HTTP 429 / RESOURCE_EXHAUSTED**.
Key invalid (403) / error lain **tidak** memicu rotasi.

**Verifikasi utama (otomatis, tanpa jaringan):**

```bash
uv run python -m pytest tests/test_ai_agent.py -q
```

Kasus: primary 429 → backup dipakai; non-429 → propagasi tanpa backup; sukses →
backup tidak dipanggil.

**Verifikasi manual (opsional, butuh kuota nyata):**

1. Isi `GEMINI_API_KEY` dan `GEMINI_API_KEY_2` keduanya valid.
2. Picu rate-limit nyata (mis. banyak permintaan beruntun / kuota RPD habis).
3. Amati log saat key 1 kena 429:

   ```
   [AI] Rate limit key 1, memakai key cadangan.
   ```

   Keputusan tetap dihasilkan (key 2 sukses) atau `None` → fallback bila keduanya
   kena 429.

> Unit test juga memverifikasi semaphore dilepas setelah panggilan dan sanitasi
> `reason` (strip kontrol, truncate 500 karakter).

## 10. Skenario E — Regresi: AI Nonaktif = Perilaku Lama

1. Set `AI_REROUTE_ENABLED=0` (atau kosongkan `GEMINI_API_KEY`), restart app.
2. `GET /api/v1/ws/navigation/status` → `ai.enabled: false`.
3. Ulangi Skenario A (deviasi > threshold).

**Kriteria lulus:** `off_route_warning` + `auto_rerouted` tetap muncul persis
seperti mode deterministik; tidak ada log `[AI] ...` sama sekali (gate `decide_reroute`
langsung mengembalikan `None`).

## 11. Uji Otomatis (Regression)

```bash
uv run python -m pytest tests/test_ai_agent.py tests/test_navigation.py tests/test_route_options.py -q
```

> Gunakan `python -m pytest` (bukan `uv run pytest`) agar `app` dapat di-import
> (modul app ada di repo root).

## 12. Troubleshooting

| Gejala | Penyebab & Solusi |
|---|---|
| `ai.enabled: false` padahal `.env` sudah diisi | Env dibaca saat import — **restart app** setelah mengubah `AI_REROUTE_ENABLED`/`GEMINI_API_KEY`. |
| `ai.circuit_breaker_open: true` | Ada kegagalan beruntun baru-baru ini. Tunggu `GEMINI_BREAKER_RESET_S` (30 dtk) atau restart (reset state in-memory). |
| Log `[AI] decide_reroute(off_route) error: ...` | Key invalid / kuota habis / network. Periksa key & `GEMINI_MODEL`. |
| Log `[AI] decide_reroute(...) timeout setelah 2.0s.` | Model lambat melewati `GEMINI_REROUTE_TIMEOUT_S`. Naikkan timeout atau turunkan `GEMINI_MAX_CONCURRENT`. |
| `auto_rerouted` tidak muncul walau AI `apply` | `compute_reroute` gagal (area luar graf / belum pre-warm) atau cooldown aktif. Pre-warm area, tunggu cooldown. |
| `WS navigation` ditutup kode `1008` | `ENABLE_LIVE_NAVIGATION` = 0. Set `1`, restart. |
| `route_id` di respons = `null` | Snapshot tidak dibuat: cek `ENABLE_LIVE_NAVIGATION=1` (atau sertakan `dynamic_rerouting:true`), token HTTP & WS harus sama. |
| `start_navigation` → `error "Route snapshot tidak ditemukan..."` | Snapshot `driver:nav:{kurir_id}` TTL habis / `route_id` salah / token beda. Hitung ulang rute. |
| `complete_leg` → `error "Tidak ada rute aktif..."` | Kirim `start_navigation` dulu sebelum `complete_leg`. |
| `complete_leg` → `error "Snapshot rute tidak ditemukan..."` | Snapshot TTL habis / `route_id` tidak cocok / token beda. Hitung ulang rute. |
| `route_complete` tidak muncul setelah leg terakhir | Pastikan leg yang diselesaikan memang leg terakhir (indeks terbesar) dan snapshot masih ada; cek log WS. |
| `route_progress` tidak muncul | Throttle `NAV_PROGRESS_MIN_INTERVAL_SECONDS` (3 dtk) — kirim lebih lambat. |
| `400 "Area di luar cakupan peta..."` | Region graph belum di-prebuild. `uv run python scripts/prewarm_route.py <lat1> <lon1> <lat2> <lon2>`. |

## 13. Kriteria Lulus Keseluruhan

- [ ] `GET /api/v1/ws/navigation/status` → `ai.enabled:true`, `circuit_breaker_open:false`, `concurrent_slots:3`.
- [ ] Snapshot rute dibuat (token sama + `ENABLE_LIVE_NAVIGATION=1`; dianjurkan sertakan `dynamic_rerouting:true`) → `route_id` terisi.
- [ ] `start_navigation` → `ack` berisi polyline leg aktif.
- [ ] Deviasi > threshold → `off_route_warning` + `auto_rerouted`; log mengonfirmasi keputusan AI (`apply`/`ignore`/`defer`) sesuai perilaku WS.
- [ ] `ignore` → tidak ada warning (anti-spam), log `[NAV] AI abaikan ...`.
- [ ] `defer` → warning tanpa reroute, log `[NAV] AI tunda ...`.
- [ ] Multi-leg: `complete_leg`/`pod_submitted` → `ack action=complete_leg` dengan `leg_index` naik & polyline leg baru; leg terakhir → `route_complete`; snapshot terhapus dari Redis.
- [ ] Circuit breaker: terbuka setelah `GEMINI_BREAKER_THRESHOLD` kegagalan, status `circuit_breaker_open:true`, AI di-skip, fallback deterministik tetap jalan, menutup kembali setelah `GEMINI_BREAKER_RESET_S`.
- [ ] Backup key: unit test `test_ai_agent.py` lolos (rotasi hanya saat 429).
- [ ] `AI_REROUTE_ENABLED=0` → `ai.enabled:false`, perilaku identik deterministik.
- [ ] Uji otomatis: `uv run python -m pytest tests/test_ai_agent.py tests/test_navigation.py tests/test_route_options.py -q` → semua lolos.