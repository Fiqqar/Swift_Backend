# Rekap Review — Sistem Navigasi & Auto-Rerouting (Living Document)

**Terakhir diupdate:** setelah analisa batch ke-4 (`connectivity.py`, `core_a_star.py`, `core_engine.py`, `graph_loader.py`, `hierarchical.py`, `maneuvers.py`, `preprocess.py`, `route_options.py`, `snap.py`, `vehicle.py`, `delivery_optimizer.py`, `pbf_registry.py`).

**Status:** SEMUA file yang disebut sebagai dependency dari file-file sebelumnya sudah dianalisa. Tidak ada gap tersisa dari sisi cakupan modul pathfinding inti. Klarifikasi: klaim awal soal `route_options.py:132` hardcode speed 40.0 sudah diverifikasi — **bukan bug**, itu fallback defensif standar (`if speed_kmh <= 0: speed_kmh = 40.0`), sama seperti pola di `eta.py`.

**Update terbaru:** `navigation.py` dikirim ulang dengan perbaikan speed sudah diterapkan. **#2 dikonfirmasi FIXED**, #10 ditutup sebagai "bukan bug" (perilaku benar by design). Namun **#1, #3, #4, #5, #6, #12, #17 di file yang sama masih belum diperbaiki** — lihat status per-item di bawah.

---

## File yang Sudah Dianalisa

| File | Temuan terkait |
|---|---|
| `app/services/navigation.py` | #1, #2, #3, #4, #5, #6 |
| `app/services/rag_traffic.py` | #8, #9 |
| `app/services/tracking.py` | #10 |
| `app/services/ai_agent.py` | #11, #12, #13, #14, #15 |
| `app/services/polyline.py` | Tidak ada temuan |
| `app/services/geocode.py` | #20 |
| `app/services/reports.py` (internal report agent) | #12 (perluasan), #21 |
| `app/api/v1/endpoints/navigation.py` (WS endpoint) | #12 (perluasan) |
| `app/api/v1/endpoints/pathfinding.py` | #16, #17, #19, #22, #23 |
| `app/api/v1/endpoints/shipments.py` | Tidak ada temuan signifikan |
| `app/api/v1/endpoints/tracking.py` (WS driver position) | Catatan minor (lihat bawah) |
| `app/api/v1/endpoints/traffic.py` | Tidak ada temuan signifikan |
| `app/api/v1/endpoints/driver_reports.py` | #21 |
| `app/api/v1/endpoints/uploads.py` | Tidak ada temuan |
| `app/api/v1/dependencies.py` | Tidak ada temuan |
| `app/api/v1/router.py` | Tidak ada temuan |
| `app/core/metrics.py` | Tidak ada temuan |
| `app/services/traffic/eta.py` | #16 (temuan kunci — sumber asli pola speed hardcoded) |
| `app/services/traffic/smart_hybrid.py` (dikirim 3x identik) | Tidak ada temuan baru |
| `app/services/traffic/tomtom.py` | Tidak ada temuan |
| `app/services/traffic/internal.py` | Tidak ada temuan |
| `app/services/traffic/matcher.py` | Tidak ada temuan |
| `app/services/traffic/poller.py` | Tidak ada temuan |
| `app/services/traffic/provider.py` | Tidak ada temuan |
| `app/services/cache_service.py` | Catatan minor (TTL hardcoded, lihat bawah) |
| `app/services/pathfinding/route_options.py` | Klarifikasi klaim awal (bukan bug, lihat catatan di atas) |
| `app/services/pathfinding/delivery_optimizer.py` | Tidak ada temuan signifikan |
| `app/services/pathfinding/maneuvers.py` | Tidak ada temuan signifikan (default `speed_kmh=40.0` di `extract_steps` konsisten, dipanggil dengan speed dari session) |
| `app/services/pathfinding/graph_loader.py` | #26 |
| `app/services/pathfinding/vehicle.py` | #27 |
| `app/services/pathfinding/core_a_star.py` | Tidak ada temuan signifikan |
| `app/services/pathfinding/core_engine.py` | #25 |
| `app/services/pathfinding/hierarchical.py` | Tidak ada temuan signifikan — desain fallback snapping cukup solid |
| `app/services/pathfinding/connectivity.py` | Tidak ada temuan signifikan |
| `app/services/pathfinding/snap.py` | Tidak ada temuan signifikan |
| `app/services/pathfinding/preprocess.py` | Catatan minor (`_ALT_LOCK` global, lihat bawah) |
| `app/services/pathfinding/pbf_registry.py` | Tidak ada temuan signifikan |

## File yang Belum Dianalisa (Gap)

Tidak ada — seluruh dependency yang disebut oleh file-file yang sudah dikirim sudah tercakup.

---

## 🔴 Prioritas Tinggi

### #1 — `navigation.py`: `NameError` scoping risk pada `haversine_distance`
Lokasi: `maybe_reorder_stops_on_off_route()`. Import bersyarat `haversine_distance` dipakai di luar blok kondisinya (termasuk closure `_leg_metrics`). Kalau `session.dest` falsy, crash `NameError`.
**Fix:** pindahkan import ke awal fungsi, unconditional.
**Status: MASIH BELUM DI-FIX** — dikonfirmasi ulang setelah update speed (#2): import `haversine_distance` masih kondisional (`if current_active_dest: from ... import haversine_distance`), dan `_leg_metrics()` versi baru pun masih memanggil `haversine_distance(...)` bare (bukan `_hav` yang sudah di-import unconditional di langkah 7). Risiko crash sama persis seperti sebelumnya — **ini sekarang jadi bug tersisa paling mendesak di file ini**, karena #2 sudah beres duluan.

### #2 — `navigation.py`: Speed hardcoded dari env di `_leg_metrics`, tidak konsisten
`speed_kmh` di `_leg_metrics()` diambil dari env (`MODE_AVG_SPEED_KMH`/`DEFAULT_SPEED_KMH`), bukan dari `session.last_position`/`session.mode` seperti fungsi lain di file yang sama.
**Status: ✅ FIXED (dikonfirmasi).** Helper `_resolve_speed_kmh(session)` sudah diimplementasikan dan dipakai konsisten di 3 tempat:
```python
def _resolve_speed_kmh(session: "NavSession") -> float:
    if session.last_position and session.last_position.get("speed"):
        try:
            v = float(session.last_position["speed"])
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    mode_defaults = {
        "motorcycle": _env_float("SPEED_KMH_MOTORCYCLE", 35.0),
        "car": _env_float("SPEED_KMH_CAR", 40.0),
        "truck": _env_float("SPEED_KMH_TRUCK", 30.0),
    }
    ...
```
- `remaining_progress()`, `compute_reroute()` (untuk `extract_steps`), dan `_leg_metrics()` (lewat `reorder_speed_kmh` yang di-resolve **sekali** sebelum loop, bukan berulang per leg — praktik baik) semuanya kini pakai sumber yang sama.
- `MODE_AVG_SPEED_KMH` sudah dihapus dari rantai (Opsi A sesuai kesepakatan), dengan komentar dokumentasi eksplisit yang mencatat bahwa env itu kini hanya relevan untuk `traffic/eta.py` — secara implisit mengakui gap #16 tanpa memperbaikinya di sana (masih terbuka, lihat #16).
- **Bonus:** temuan #10 (speed=0 dianggap falsy) ternyata bukan bug — `if v > 0: return v` memang sengaja menolak speed≤0 karena tidak bisa dipakai hitung waktu tempuh (div-by-zero/infinite). Perilaku ini benar by design, bukan celah. #10 ditutup sebagai "clarified, not a bug."

### #8 — `rag_traffic.py`: Throttle ingestion bypass total untuk kota tanpa berita
`_ensure_city_news()` hanya cek interval kalau `_store.has_city(city)` True; kota tanpa berita relevan tidak pernah masuk store, sehingga throttle tidak pernah aktif → risiko spam Gemini Grounding API.
**Fix:** catat waktu cek terakhir terlepas dari hasil ingestion. **Status: belum di-fix.**

### #11 — `ai_agent.py`: Tool AI melanggar kontrak "pure", bisa korupsi state navigasi asli
`_tool_get_remaining_progress()` memanggil `remaining_progress()` yang memutasi `session.traveled_distance_m`/`session.current_step_index` sebagai side effect — AI bisa memanggil dengan lat/lng eksploratif dan merusak state kurir asli. **Status: belum di-fix.**

### #12 — Race condition pada `NavSession`, `session.lock` tak pernah dipakai (DIPERLUAS)
**Update setelah batch ke-3:** bukan cuma soal `session.mode` di `ai_agent.py` — ternyata **4 call site independen** bisa memutasi field `NavSession` yang sama secara konkuren tanpa lock sama sekali:
1. `navigation_worker` (background loop, `navigation.py`, tiap `TRAFFIC_REROUTE_INTERVAL_SECONDS`)
2. `_handle_location_update` di WS endpoint `navigation.py` (saat off-route terdeteksi)
3. `internal_report_agent` → `_handle_report` (`reports.py`) — laporan kurir lain bisa memicu reroute kurir ini
4. Tool AI `_tool_compute_reroute` (`ai_agent.py`)

Semua memanggil `compute_reroute()` (yang menulis `session.node_sequence`, `session.steps`, dst) dan/atau langsung menimpa `session.coords`/`session.cooldown_until`. `NavSession.lock` (`asyncio.Lock()`) didefinisikan di `__init__` tapi **tidak pernah dipakai** (`async with session.lock` tidak muncul di kode manapun yang sudah dianalisa).
**Fix:** bungkus seluruh titik mutasi session (bukan cuma `session.mode`) dengan `async with session.lock:`. **Status: belum di-fix — severity dinaikkan karena cakupan lebih luas dari perkiraan awal.**

### #16 — BARU — `eta.py`: Sumber asli pola "speed hardcoded", dipakai seluruh sistem
```python
speed_kmh = cfg["mode_speed_kmh"] if cfg["custom"] else _env_float("DEFAULT_SPEED_KMH", 40.0)
```
`compute_eta()` adalah fungsi ETA resmi yang dipakai `_best_route` (pathfinding.py) — sumber `estimated_time_seconds` di semua endpoint (`find-route`, `find-route-options`, `find-optimized-delivery-route`, dan transitif oleh `navigation.py`). Secara default (`ENABLE_CUSTOM_ETA=False`), ETA **selalu pakai `DEFAULT_SPEED_KMH` flat**, mengabaikan `MODE_AVG_SPEED_KMH` sepenuhnya. Bahkan saat `ENABLE_CUSTOM_ETA=true`, `mode_speed_kmh` tetap **satu angka global**, bukan per mode kendaraan (motorcycle/car/truck) — nama env var menyesatkan sejak sumbernya, bukan cuma duplikasi yang diperkenalkan `navigation.py`.
**Implikasi untuk fix #2:** helper `_resolve_speed_kmh(session)` yang direncanakan sebaiknya dikoordinasikan dengan `eta_config()`, idealnya `eta_config()` sendiri direfaktor agar benar-benar per-mode. **Status: belum di-fix.**

### #17 — BARU — `navigation.py`: Mode fallback dari `_best_route` diabaikan `compute_reroute`
```python
response, node_sequence, final_penalties, _m = await _best_route(...)
```
`_best_route` bisa fallback `motorcycle`/`truck` → `car` bila mode asli gagal routing (lihat `pathfinding.py`), dan mengembalikan mode final yang benar-benar dipakai. Tapi `compute_reroute()` di `navigation.py` membuang nilai ini (`_m` diberi underscore, tak dipakai) — `session.mode` tidak diperbarui meski rute aktif sebenarnya pakai mode berbeda. Berdampak ke akurasi resolusi speed (terkait #2) dan info mode yang ditampilkan ke client.
**Fix:** `compute_reroute` sebaiknya update `session.mode = _m` bila berbeda dari mode asli, dan beri sinyal (mis. warning event) ke client. **Status: belum di-fix.**

### #19 — BARU — `pathfinding.py`: Tabrakan data lintas-pengguna via `driver:nav:0`
Muncul identik di 3 endpoint (`find-route`, `find-route-options`, `find-optimized-delivery-route`):
```python
await set_nav_route(redis, _token_kurir_id(request) or 0, {...})
```
Request tanpa autentikasi (`_token_kurir_id` return `None`) tapi `nav_enabled(payload)` True → fallback ke `kurir_id=0`. Semua request anonim seperti ini menimpa Redis key `driver:nav:0` yang sama — snapshot rute satu pemanggil anonim bisa tertimpa/bocor ke pemanggil anonim lain yang request bersamaan.
**Fix:** jangan buat snapshot nav sama sekali bila `kurir_id` tidak diketahui (return early / skip `set_nav_route`), atau generate ID sementara unik (mis. UUID) bukan fallback ke `0`. **Status: belum di-fix. Perlu konfirmasi seberapa sering endpoint ini dipanggil tanpa auth di production.**

### #25 — BARU — `core_engine.py`: Reachability pre-check Python murni di setiap panggilan `route()`
```python
def route(pg, start_node, goal_node, penalties=None):
    blocked_edges = {eid for eid, mult in penalties.items() if mult == float("inf")} if penalties else set()
    if not reaches(pg.graph, start_node, goal_node, blocked_edges):
        return None, float('inf')
    if RUST_AVAILABLE:
        try:
            path, cost = _rust_graph_for(pg).route(start_node, goal_node, penalties)
            ...
```
`reaches()` (di `connectivity.py`) adalah BFS/DFS pure Python yang jalan **sebelum** Rust engine sempat dicoba, di **setiap** panggilan `route()` — termasuk untuk graf besar/dense. Ini sebagian mengurangi manfaat performa dari compiled Rust pathfinder, karena hot path tetap melewati pre-check Python di awal.
**Fix potensial:** pertimbangkan skip pre-check ini kalau `RUST_AVAILABLE` (biarkan Rust engine yang menentukan "no path" secara native, biasanya lebih cepat), atau cache hasil reachability per (start,goal,blocked_edges) bila dipanggil berulang untuk pasangan yang sama. **Status: belum diverifikasi dampak nyata — perlu profiling untuk konfirmasi signifikansinya di graf produksi.**

### #26 — BARU — `graph_loader.py`/`preprocess.py`: Contraction Hierarchy hilang diam-diam saat reload dari cache disk
```python
def _serialize_pg(pg) -> dict:
    return {  # "ch" TIDAK disertakan
        "graph": pg.graph, "locations": pg.locations, ...
    }

def _deserialize_pg(data) -> "PathGraph":
    pg = PathGraph(..., ch=None, ...)  # selalu None
    return pg
```
`pg.ch` (Contraction Hierarchy, dibangun mahal via `build_ch()` saat `build_path_graph(..., enable_ch=True)`) **sengaja dikecualikan** dari serialisasi cache disk. Setiap kali `PathGraph` di-load ulang dari file pickle (`load_path_graph`, `load_local_graph_point`, dll — bukan dari LRU cache in-memory), `ch` selalu `None`, dan tidak pernah di-rebuild ulang maupun diberi warning log.
**Dampak:** di `shortest_path()` (`core_a_star.py`), kalau `pg.ch is None`, sistem fallback ke ALT/bidirectional/A* biasa — jalur ini hanya tereksekusi kalau Rust engine (`_rust_engine`) tidak tersedia/gagal. Jadi selama Rust engine aktif, dampaknya tertutupi; tapi di environment tanpa ekstensi Rust terkompilasi, performa routing bisa turun signifikan tanpa ada sinyal di log yang menjelaskan kenapa.
**Fix potensial:** log warning saat `ch is None` pasca-deserialize kalau `enable_ch` semestinya `True`, atau rebuild CH secara lazy sekali saat pertama kali dibutuhkan (mirip pola `_ensure_alt` yang sudah ada untuk landmark/ALT).

### #27 — BARU — `vehicle.py`: Filter mode kendaraan diam-diam dilewati kalau `edge_classes` kosong
```python
def blocked_penalties_for(plan, mode: str) -> dict:
    allowed = _ALLOWED_BY_MODE.get(mode, _CAR)
    ...
    for pg in graphs:
        edge_classes = _pg_edge_classes(pg)
        if not edge_classes:
            _log_stale_graph(pg, logger)  # cuma warning, lanjut tanpa blocking
            continue
        ...
```
Kalau `PathGraph` tidak punya `edge_classes` (graf lama/demo/hasil build tanpa metadata jalan), filter mode kendaraan **dilewati sepenuhnya** untuk graf itu — semua edge dianggap "boleh dilalui". Sudah ada log warning (`_log_stale_graph`), tapi request routing tetap lanjut dan berhasil — motor bisa saja terarahkan lewat jalan tol tanpa terdeteksi sebagai pelanggaran, kalau graf yang dipakai kebetulan tidak punya `edge_classes`.
**Fix potensial:** untuk mode selain "car", pertimbangkan menolak request (400) alih-alih diam-diam melanjutkan tanpa filter, atau pastikan semua graf produksi selalu punya `edge_classes` (validasi saat build).

### #28 — BARU — `navigation.py`: ETA/`remaining_time_s` naik-turun tidak beraturan di `route_progress` WS
**Ditemukan lewat laporan perilaku, bukan pembacaan kode langsung — perlu diverifikasi dengan data log produksi.**

Lokasi: `remaining_progress()`, sumber event `route_progress` yang dikirim tiap `NAV_PROGRESS_MIN_INTERVAL_SECONDS` (default 3 detik). Tiga kontributor yang teridentifikasi:

1. **Speed instan, bukan rata-rata bergerak.**
```python
speed_kmh = _resolve_speed_kmh(session)
remaining_time_s = remaining / (speed_kmh / 3.6)
```
`_resolve_speed_kmh` (lihat #2) memakai `session.last_position["speed"]` — kecepatan sesaat dari GPS, bukan smoothed/rata-rata. Kurir berhenti (lampu merah) → speed 0 → fallback ke default per-mode → lanjut jalan → speed lompat ke nilai GPS asli lagi. Karena `remaining_time_s` dihitung ulang langsung dari nilai instan ini tiap 3 detik, hasilnya naik-turun tajam alih-alih menurun mulus.

2. **Pencarian "nearest point" di polyline tidak forward-only.**
```python
nearest = 0
nearest_d = float("inf")
for i, c in enumerate(coords):
    d = _haversine((lat, lon), c)
    if d < nearest_d:
        nearest_d = d
        nearest = i
```
Mencari titik terdekat ke **seluruh** polyline tanpa syarat harus maju dari index sebelumnya. Untuk rute yang punya bagian saling berdekatan secara jarak lurus (jalan berkelok, roundabout, jalan sejajar/frontage road, U-turn), posisi GPS yang sama bisa ke-snap ke index yang jauh berbeda antar update — `remaining_distance_m` (dan otomatis `remaining_time_s`) bisa melompat walau kurir bergerak maju terus secara riil.

3. **Sumber speed berpindah diam-diam.** Kalau field `speed` kadang tidak dikirim client (device tertentu, GPS drop sesaat), `_resolve_speed_kmh` langsung lompat dari "speed GPS asli" ke "default per-mode" (35/40/30 km/h) — dua sumber yang bisa beda jauh nilainya, menambah lonjakan tiap kali field itu hilang-muncul.

4. **Reroute mengganti total rute sekaligus.** Tiap auto-reroute berhasil (traffic tiap 30 detik, off-route, atau laporan kurir lain via `internal_report_agent`), `session.coords` diganti total — jarak & durasi total berubah drastis dalam satu event, bukan noise kecil bertahap. Ini perilaku yang diharapkan (bukan bug), tapi kontribusi terhadap "lonjakan" yang terlihat user perlu dibedakan dari 3 poin di atas saat debugging.

**Bukan penyebab:** `average_speed_kmh` di response cuma alias dari `speed_kmh` (lihat komentar kode `"Could compute from start time, but we don't have it"`) — field-nya salah nama (bukan rata-rata sungguhan), tapi ini tidak menambah fluktuasi, cuma informasi yang menyesatkan namanya.

**Verifikasi teknis:** dikonfirmasi ulang, keempat penyebab akurat dengan referensi baris kode yang sesuai implementasi terbaru.

**Rencana fix disepakati (urut prioritas eksekusi):**
- **P1 — EWMA speed smoothing:** tambah `session.smoothed_speed_kmh` di `NavSession`, dipakai sebagai sumber utama di `_resolve_speed_kmh()` alih-alih raw GPS speed langsung.
- **P2 — Forward-only nearest-point search:** simpan `session._last_nearest_idx`, mulai scan dari `max(0, session._last_nearest_idx - 2)` (toleransi mundur untuk noise GPS) bukan scan ulang seluruh polyline tiap kali.
- **P3 — Dead-band push di WS endpoint:** skip kirim `route_progress` bila `remaining_distance_m` berubah < 15m dari update sebelumnya (mengurangi noise yang sampai ke client, bukan cuma di sumbernya).
- **P4 (opsional, defense-in-depth):** EMA tambahan di sisi frontend/mobile.

**Dua syarat wajib sebelum eksekusi P2 (ditemukan saat review rencana implementasi):**

1. **`session._last_nearest_idx` harus di-reset ke `0` di SETIAP titik yang me-reassign `session.coords` total**, kalau tidak, index lama menunjuk ke lokasi salah di array baru — bisa jadi index out-of-range, atau lebih berbahaya: index valid tapi secara fisik salah tempat (silent wrong-answer). Titik yang wajib ditambahkan resetnya:
   - `compute_reroute()` — tiap sukses reroute
   - `_evaluate_session()` (`navigation_worker`) — saat reroute traffic diterapkan
   - `advance_leg()` — pindah leg (tinggal tambah 1 baris ke blok reset yang sudah ada)
   - `_apply_route()` di WS endpoint (`_handle_start_navigation`)
   - `maybe_reorder_stops_on_off_route()` — bagian MAJOR RE-ORDER

2. **Field baru ini kena race condition yang sama dengan #12** (dibaca-tulis dari `navigation_worker`, WS handler, `internal_report_agent`, tool AI secara konkuren). **P2 harus dikerjakan bersamaan dengan fix #12** (session lock), bukan terpisah duluan — supaya field baru langsung terlindungi lock sejak awal, tidak menambah utang teknis race condition baru.

**Catatan minor (opsional, bukan blocker):** toleransi mundur berbasis jumlah index (±2) bukan jarak fisik — kepadatan titik polyline tidak merata (padat di persimpangan, jarang di jalan lurus), jadi toleransi efektifnya bisa sangat bervariasi antar bagian rute. Cukup aman untuk v1; kalau masih ada laporan lompat di rute tertentu, pertimbangkan ganti ke toleransi berbasis jarak kumulatif (mis. "boleh mundur maksimal 30m").

**Status: rencana P1-P4 disepakati dengan syarat tambahan di atas; belum dieksekusi.**

---

## 🟡 Prioritas Sedang

### #3 — `navigation.py`: Blok kode terduplikasi persis
Guard clause `# 1. Only for multi-leg...` dan blok `# 12. Determine re-order type...` masing-masing muncul 2x berturut-turut.

### #4 — `navigation.py`: Variabel di-assign tapi tidak dipakai
`recipient_names`, `old_recipient` — cek apakah `switched_from` di payload WS sudah ambil nilai yang benar.

### #5 — `navigation.py`: Nama variabel `remaining_legs` dipakai ulang dengan makna berbeda

### #6 — `navigation.py`: `traffic_level` di `remaining_progress()` efektif selalu `"free"`

### #9 — `rag_traffic.py`: Cache evaluasi berita pakai ID posisional, bukan hash konten

### #10 — `tracking.py` ↔ `navigation.py`: kecepatan `0.0` dianggap falsy (perlu `is not None`)

### #13 — `ai_agent.py`: Fallback destinasi jadi dead code di `_tool_get_traffic_penalties`
`float(args.get("lat2", args.get("dest_lat")))` — kalau keduanya `None`, `TypeError` duluan sebelum pengecekan fallback ke `session.dest` sempat jalan.

### #20 — BARU — `geocode.py`: Semaphore geocoding global dipakai bersama forward & reverse geocode
```python
_geocode_semaphore = asyncio.Semaphore(1)
```
Dipakai baik oleh `_nominatim()` (geocode alamat pengiriman) maupun `_nominatim_reverse()` (deteksi kota RAG di `rag_traffic.py`), keduanya serialize lewat lock yang sama + `GEOCODE_RATE_DELAY` (default 1 detik). Traffic RAG yang sering melakukan reverse-geocode (dipicu tiap evaluasi reroute traffic bila `RAG_NEWS_DYNAMIC_CITY=True`) berpotensi memperlambat `geocode_address()` yang dipanggil `find-optimized-delivery-route` untuk alamat tanpa koordinat.
**Fix opsional:** pisahkan semaphore forward vs reverse geocode, atau prioritaskan permintaan geocode alamat pengiriman di atas reverse-geocode kota RAG.

### #21 — BARU — `driver_reports.py`/`reports.py`: Rate-limit fail-open → eksposur kuota Gemini
```python
async def _rate_limited(redis, kurir_id: int) -> bool:
    if redis is None:
        return False
    ...
```
Kalau Redis tidak tersedia, rate limiting laporan kurir otomatis nonaktif (fail-open, konsisten dengan filosofi non-fatal di modul lain) — tapi setiap laporan yang lolos memicu 1 panggilan klasifikasi Gemini (`_classify_report`). Pola risikonya sama dengan #8 (rag_traffic throttle bypass), meski di sini dampaknya sedikit dibatasi oleh `REPORT_BATCH` (default 20) per siklus `internal_report_agent`.

---

## 🟢 Minor / Housekeeping

### #7 / #14 / #15 — (lihat rekap sebelumnya, belum berubah)

### #22 — BARU — `pathfinding.py`: `_MAX_OFF_ROUTE_M` kemungkinan dead code
```python
_MAX_OFF_ROUTE_M = float(os.environ.get("MAX_OFF_ROUTE_DISTANCE_METERS", "30"))
```
Didefinisikan di level modul tapi tidak terlihat dipakai di kode manapun yang sudah dikirim — perlu dicek apakah dipakai di `route_options.py` atau modul lain yang belum dikirim, atau memang sisa refactor.

### #23 — BARU — `pathfinding.py`: `nav_enabled()` vs `dynamic_rerouting_enabled()` nyaris duplikat
```python
def nav_enabled(payload) -> bool:
    if payload.dynamic_rerouting is not None:
        return bool(payload.dynamic_rerouting)
    return _env_bool("ENABLE_LIVE_NAVIGATION", False)

def dynamic_rerouting_enabled(payload) -> bool:
    if payload.dynamic_rerouting is not None:
        return bool(payload.dynamic_rerouting)
    return _env_bool("ENABLE_DYNAMIC_REROUTING", False)
```
Satu field payload (`dynamic_rerouting`) mengontrol dua subsistem berbeda (cache `active_route` di `app.state` vs pembuatan snapshot Redis untuk navigasi WS) sekaligus — kalau field itu diisi eksplisit, caller tidak bisa mengaktifkan satu tanpa yang lain lewat payload; harus lewat 2 env var terpisah yang berbeda cakupannya (server-wide, bukan per-request).

### Catatan tambahan (tidak masuk tabel prioritas)
- `smart_hybrid.py` dikirim 3x dengan isi identik — tidak ada perbedaan, tidak memengaruhi analisa.
- `cache_service.py`: `ROUTE_TTL = 300` hardcoded konstanta, tidak env-configurable, berbeda dengan TTL lain di sistem yang hampir semuanya baca dari env — sangat minor.
- `tracking.py` (WS driver position) `_try_snap`: memanggil `load_base_graph()` langsung tanpa `run_in_threadpool` di dalam fungsi async — berpotensi blocking event loop kalau fungsi ini mahal/tidak ter-cache; **belum bisa dipastikan** karena isi `graph_loader.py` belum dikirim.
- `_normalize_mode` di `pathfinding.py` (menerima `payload` object) vs di `ai_agent.py` (menerima `str`) — dikonfirmasi memang dipakai sesuai kontraknya masing-masing, bukan bug aktif, tapi tetap jebakan maintenance (nama sama, kontrak beda).
- `preprocess.py`: `_ALT_LOCK = threading.RLock()` adalah lock **global** (bukan per-`PathGraph`) yang dipakai `_ensure_alt()` untuk membangun landmark/ALT preprocessing. Request konkuren yang butuh ALT preprocessing untuk 2 graf **berbeda** akan saling menunggu (serialize) walau tidak ada data yang benar-benar dibagi — bukan bug korektness, cuma suboptimal untuk paralelisme di beban tinggi.
- `route_options.py:132` (klaim awal user) — **sudah diverifikasi, bukan bug**: `if speed_kmh <= 0: speed_kmh = 40.0` di `route_incidents()` adalah fallback defensif atas parameter yang sudah diteruskan pemanggil, bukan sumber utama nilai speed.

---

## Ringkasan Prioritas (Update Berjalan)

| # | File | Isu | Severity | Status |
|---|---|---|---|---|
| 12 | navigation.py, reports.py, ai_agent.py | Race condition `NavSession`, lock tak pernah dipakai (4 call site) | **Tinggi (diperluas)** | Belum di-fix |
| 19 | pathfinding.py | Tabrakan Redis key `driver:nav:0` untuk request anonim | **Tinggi (baru)** | Belum di-fix |
| 1 | navigation.py | `NameError` scoping `haversine_distance` | Tinggi | Belum di-fix |
| 2 | navigation.py | Speed hardcoded, tidak konsisten antar fungsi | Tinggi | ✅ **FIXED** — `_resolve_speed_kmh` diimplementasikan & dikonfirmasi |
| 16 | eta.py | Sumber asli pola speed hardcoded, dipakai seluruh sistem | **Tinggi (baru)** | Belum di-fix |
| 8 | rag_traffic.py | Throttle ingestion bypass → spam API | Tinggi | Belum di-fix |
| 11 | ai_agent.py | Tool AI mutasi state navigasi asli | Tinggi | Belum di-fix |
| 17 | navigation.py | Mode fallback `_best_route` diabaikan `compute_reroute` | Sedang (baru) | Belum di-fix |
| 20 | geocode.py | Semaphore geocoding global forward+reverse | Sedang (baru) | Belum di-fix |
| 21 | driver_reports.py/reports.py | Rate-limit fail-open → eksposur kuota Gemini | Sedang (baru) | Belum di-fix |
| 6 | navigation.py | `traffic_level` selalu `"free"` | Sedang | Belum di-fix |
| 5 | navigation.py | Reuse nama `remaining_legs` beda makna | Sedang | Belum di-fix |
| 9 | rag_traffic.py | Cache key posisional, rapuh | Sedang | Belum di-fix |
| 10 | tracking.py/navigation.py | speed=0 dianggap falsy | Sedang | ✅ **Ditutup — bukan bug**, perilaku benar by design (lihat #2) |
| 13 | ai_agent.py | Fallback destinasi dead code | Sedang | Belum di-fix |
| 3 | navigation.py | Blok kode terduplikasi | Rendah | Belum di-fix |
| 4 | navigation.py | Variabel tak terpakai (cek `switched_from`) | Rendah | Belum di-fix |
| 14 | ai_agent.py | `_agent_loop` tak cek `response is None` | Rendah | Belum di-fix |
| 15 | lintas modul | Dua `_normalize_mode` beda kontrak (dikonfirmasi bukan bug aktif) | Rendah | Info saja |
| 22 | pathfinding.py | `_MAX_OFF_ROUTE_M` kemungkinan dead code | Rendah (baru) | Perlu verifikasi |
| 23 | pathfinding.py | `nav_enabled` vs `dynamic_rerouting_enabled` nyaris duplikat | Rendah (baru) | Info saja |
| 26 | graph_loader.py/preprocess.py | CH hilang diam-diam saat reload dari cache disk | Sedang (baru) | Belum di-fix, dampak tergantung ketersediaan Rust engine |
| 25 | core_engine.py | Reachability pre-check Python di setiap `route()` | Rendah-Sedang (baru) | Perlu profiling untuk konfirmasi dampak |
| 27 | vehicle.py | Filter mode kendaraan dilewati kalau `edge_classes` kosong | Rendah-Sedang (baru) | Belum di-fix |
| 28 | navigation.py | ETA/`remaining_time_s` naik-turun tidak beraturan di `route_progress` WS | Sedang (baru) | Perlu verifikasi data log sebelum eksekusi fix |

---

## Next Steps

**Rencana eksekusi batch disepakati** (menggantikan urutan longgar sebelumnya):

```
Batch 1 — Critical Safety + Quick Wins:
  #1  NameError haversine scoping       (fix kecil, risiko crash tinggi)
  #19 Redis key collision driver:nav:0  (data integrity)
  #3  Hapus duplikasi blok              (sekalian, file sama dengan #1)

Batch 2 — Concurrency:
  #12 session.lock wrapping             (race condition, 4 call site)
  #11 AI tool state mutation guard
  #28-P2 Forward-only nearest search    (DIGABUNG ke batch ini — field baru
                                          _last_nearest_idx kena race yang
                                          sama seperti #12, harus dilindungi
                                          lock sejak awal, bukan ditambahkan
                                          terpisah lalu di-lock belakangan)

Batch 3 — UX Stabilization:
  #28-P1 EWMA speed smoothing           (paling berdampak ke UX)
  #28-P3 Dead-band push di WS endpoint
  #17    Mode fallback update ke session.mode (akurasi speed per-mode)

Batch 4 — Resiliensi API:
  #8  RAG throttle bypass fix
  #21 Rate-limit fail-open (opsional)

Batch 5 — Performance (opsional):
  #26 CH lazy rebuild
  #25 Skip reachability pre-check bila Rust aktif
  #27 Vehicle filter validation
```

**Syarat implementasi Batch 2 (#28-P2)** — wajib direset di semua titik yang me-reassign `session.coords`: `compute_reroute()`, `_evaluate_session()`, `advance_leg()`, `_apply_route()` (WS endpoint), `maybe_reorder_stops_on_off_route()` (MAJOR RE-ORDER). Lihat detail lengkap di #28.

1. Cakupan modul sudah lengkap — tidak ada lagi file dependency yang belum dianalisa dari yang sudah dikirim.
2. **#2 sudah FIXED** — 1 dari 5 temuan prioritas tinggi selesai.
3. Untuk #28-P1/P3, sebelum eksekusi: ambil sampel log `route_progress` (`remaining_time_s`, `current_speed_kmh`) dari beberapa sesi WS aktif untuk konfirmasi kontribusi relatif tiap penyebab, guna validasi urutan prioritas di Batch 3.
4. Kalau ada modul baru di luar cakupan saat ini (mis. `models/`, `schemas/`, `core/security.py`, `core/database.py`, `cloudinary_service.py`) yang ingin direview, atau ada file lain yang sudah diperbaiki untuk diverifikasi ulang, kirim kapan saja — dokumen ini akan terus diupdate di tempat yang sama.