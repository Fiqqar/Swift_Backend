# Task: Fix Critical PBF Parsing Performance Bottleneck (Pre-loading & Disk Caching)

## Konteks & Masalah
Saat ini, pencarian rute dalam kota (seperti Monas ke Pulomas, Jakarta) membutuhkan waktu > 300 detik.
Bottleneck utama BUKAN pada algoritma pathfinding Rust, melainkan pada pemrosesan file `.osm.pbf` oleh `pyrosm`:
1. System membaca dan meng-scan file `.osm.pbf` raksasa dari disk SETIAP KALI ada HTTP request masuk (Cache Miss).
2. Konversi Pyrosm DataFrame ke NetworkX MultiDiGraph bersifat single-threaded dan memakan CPU/RAM yang sangat tinggi di runtime.

Kita harus memindahkan pemrosesan PBF dari HTTP Request Loop ke **Startup Lifespan Pre-loading** dan **Persistent Disk Caching**.

---

## Technical Requirements & Architecture

### 1. Persistent Disk Cache untuk Graph (`.pkl`)
- Sebelum `pyrosm` membaca file PBF, selalu cek apakah file Cache Disk `.pkl` (misal: `cache/pathfinding/pg_v2_{snap_grid}.pkl`) sudah ada.
- Jika file `.pkl` sudah ada di disk, langsung `pickle.load()` file tersebut. **DILARANG memanggil `pyrosm` jika disk cache hit**.
- Jika file `.pkl` belum ada, jalankan `pyrosm`, buat `PathGraph`, lalu SERTA-MERTA simpan hasil `PathGraph` tersebut ke file `.pkl` agar request berikutnya instant.

### 2. Pre-loading Primary Regional Graph di Lifespan (`app/main.py`)
- Pada fungsi `lifespan` FastAPI (saat aplikasi booting):
  - Muat secara otomatis (preload) area graf operasional utama (misal: Bbox Jabodetabek atau Jawa Tengah) dan simpan di `app.state.path_graph`.
  - Inisialisasi dan *warm-up* `_rust_engine.RustGraph` di memori RAM.
- Handler endpoint `POST /find-route` HARUS mengutamakan penggunaan graf di `app.state.path_graph` jika koordinat request berada di dalam cakupan Bbox preload.

### 3. Log Performance Profiling
Tambahkan logging durasi eksekusi menggunakan `time.perf_counter()` di `graph_loader.py` dan `pathfinding.py` dengan format:
- `[PERF] Disk Cache Check: X ms`
- `[PERF] Pyrosm PBF Parse (Cold Start): X ms`
- `[PERF] PathGraph Build: X ms`
- `[PERF] Rust Engine Pathfinding Search: X ms`

---

## Instructions for Agent / Refactoring Code

1. **Refactor `app/services/pathfinding/graph_loader.py`:**
   - Bungkus logika pemuatan dari PBF dengan pemeriksaan disk cache `.pkl` berbasis Bbox Snap Grid (`OSMNX_CACHE_GRID`).
   - Pastikan jika file `.pkl` ada, `pyrosm` sama sekali tidak di-import/diinisialisasi.

2. **Refactor `app/main.py` (Lifespan Startup):**
   - Tambahkan fungsi preload untuk memuat graf utama dari PBF / Disk Cache saat startup server.
   - Set log informasi: `[STARTUP] Pre-loaded path graph into RAM successfully.`

3. **Verify Rust Engine Usage:**
   - Pastikan bahwa setelah `PathGraph` didapat (baik dari RAM/Disk Cache), proses routing MURNI dieksekusi oleh `_rust_engine.RustGraph.route`.
   - Pastikan GIL dilepas (`py.detach`) sehingga tidak memblokir event loop Uvicorn.

---

## Output Expectation
- Request pertama (Cold Start tanpa cache): PBF di-parse dan langsung di-cache ke disk.
- Request kedua dan seterusnya (Hot Start / Cache Hit): Membaca Disk Cache / RAM, dengan latency response **< 100 ms**.