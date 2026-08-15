# Spesifikasi Teknis — TEST2 API Engine

Dokumen ini merangkum spesifikasi implementasi **saat ini** pada project TEST2 API Engine, dengan fokus pada arsitektur, pipeline pathfinding, dan optimisasi yang sudah diterapkan.

---

## 1. Arsitektur Umum

| Bagian | Teknologi |
|---|---|
| Bahasa | Python 3.12 |
| Framework | FastAPI (async) + Uvicorn |
| Manajemen env & deps | uv (`pyproject.toml`, `uv.lock`, `requirements.txt`) |
| Data peta | OSM via Overpass API (`osmnx`), fallback demo grid |
| Engine pathfinding | Rust (PyO3 0.29 + Maturin) `_rust_engine` + Python fallback |
| Caching | Memori, disk (pickle), Redis (`redis.asyncio`, `protocol=2`) |
| Database | SQLAlchemy 2.0 async (target PostGIS), model `RouteHistory` |
| UI | Statis (Leaflet CDN) di `app/ui` — `StaticFiles(html=True)` |

Struktur direktori:

```
app/
  main.py                         # lifespan, CORS, /health, mount UI
  core/
    database.py                   # engine async SQLAlchemy + SessionLocal
    redis.py                      # client redis.asyncio (REDIS_URL / REDIS_HOST/PORT)
  models/
    route_history.py              # ORM RouteHistory (belum di-wire)
  schemas/
    pathfinding.py                # Pydantic V2 (Coordinate, RouteRequest, RouteResponse)
  api/v1/
    router.py                     # daftar router (pathfinding, traffic)
    endpoints/
      pathfinding.py              # POST /find-route
      traffic.py                  # POST /update-weight, GET /penalties
  services/
    cache_service.py              # route key, get/set, penalties helpers
    pathfinding/
      graph_loader.py             # load OSM/demo + cache memori/disk
      preprocess.py               # PathGraph, ALT landmarks, ContractionHierarchy
      core_a_star.py              # A*, ALT A*, Bidirectional ALT, edge_id, shortest_path
      core_engine.py              # wrapper Rust → Python fallback
src/
  lib.rs                          # Rust: RustGraph + run_bidirectional_dijkstra
Cargo.toml                        # crate _rust_engine (pyo3 0.29, cdylib)
pyproject.toml                    # build-system maturin + [tool.maturin] + tool.uv.package=false
```

---

## 2. Pipeline Pathfinding

### 2.1 Load Graf
- `load_graph_covering(lat1, lon1, lat2, lon2)`:
  - radius = `min(MAX, max(BASE, jarak_haversine × 1.25 + 500))`
  - `BASE_RADIUS = 3000`, `MAX_RADIUS = 50000` (env `OSMNX_MAX_RADIUS`)
  - Jika area dasar (3000 m) mencakup kedua titik → langsung pakai.
- Sumber: `osmnx.graph_from_point(..., network_type="drive")` via Overpass
  (env `OSMNX_OVERPASS_URL`, default `https://overpass-api.de/api`).
- Edge one-way hanya `u→v`; non-oneway dibentuk dua arah.
- Fallback: **demo grid 6×6** + `warning` bila OSM gagal/tak terjangkau.

### 2.2 Caching Graf (3 lapis)
1. Memori: `_GRAPH_CACHE`, `_LOCATIONS_CACHE`, `_PG_CACHE` (dilindungi `_CACHE_LOCK` RLock).
2. Disk: pickle di `OSMNX_DISK_CACHE` (default `cache/pathfinding`),
   key ter-snap ke grid `OSMNX_CACHE_GRID` (0.005°):
   - `g_{lat}_{lon}_{radius}.pkl` — graf mentah
   - `pg_v2_{lat}_{lon}_{radius}.pkl` — PathGraph siap pakai
3. Jaringan Overpass.

### 2.3 Preprocess (`build_path_graph`)
- `precompute_geo` → sin/cos per node untuk haversine cepat.
- **ALT landmarks** (`k=8`): greedy "farthest-apart" + `landmark_dists` (Dijkstra penuh per landmark).
- **Contraction Hierarchies** hanya bila graf **tidak directed** (`OSMNX_ENABLE_CH=1`).
  - Priority: `shortcuts - degree`.
  - Witness search dibatasi budget `OSMNX_CH_WITNESS_NODES` (2500) → build cepat.

### 2.4 Runtime Routing (`core_engine.route`)
Urutan pemilihan algoritma:
1. **Rust Bidirectional Dijkstra** (`_rust_engine.RustGraph.route`) — jika ekstensi terpasang.
   - `RustGraph` di-cache per `PathGraph` (atribut `_rust_graph`).
   - GIL dilepas (`py.detach`) — komputasi tidak memblokir event-loop.
2. **Fallback Python** (`shortest_path`):
   - penalti aktif → **A\*** (bobot × multiplier via `edge_id` Cantor)
   - ada CH → **CH query** (bidirectional di graf "up")
   - ada landmarks & undirected → **Bidirectional ALT**
   - ada landmarks → **ALT A\***
   - selainnya → **A\*** (heuristic haversine).

### 2.5 Penalti Lalu Lintas
- Disimpan di Redis hash `traffic:penalties` (`edge_id → multiplier`).
- `edge_id(u, v)` = **Cantor pairing** deterministik, identik Python & Rust:
  ```
  a = u if u >= 0 else 2*(-u)-1
  b = v if v >= 0 else 2*(-v)-1
  edge_id = (a+b)*(a+b+1)//2 + b
  ```
- Penalti dimuat tiap request dan diterapkan saat relaksasi edge.

### 2.6 Cache Rute (Redis)
- Key: `route:{scope}:{start_node}:{goal_node}:{penalty_sig}`
  - `scope = {ref_lat:.6f}_{ref_lon:.6f}_{radius}` (cegah tabrakan antar-area peta)
  - `penalty_sig` = SHA-1 16-char dari pasangan `edge_id:multiplier` terurut; `"0"` bila kosong.
- TTL 300 detik. Redis tidak tersedia → hitung langsung (graceful).

---

## 3. Endpoint

| Method | Path | Deskripsi | Error |
|---|---|---|---|
| `GET` | `/` | UI (Leaflet) | — |
| `GET` | `/health` | Status app + OSM + Overpass + route_test (A*) | 500 bila gagal |
| `POST` | `/api/v1/pathfinding/find-route` | Hitung rute | 400 di luar jangkauan, 404 tak ditemukan, 500 gagal muat |
| `POST` | `/api/v1/traffic/update-weight` | Set penalti `{edge_id, multiplier>0}` | 503 tanpa Redis |
| `GET` | `/api/v1/traffic/penalties` | Daftar penalti terurut | 503 tanpa Redis |

`RouteRequest`: `{origin:{latitude,longitude}, destination:{latitude,longitude}}`
`RouteResponse`: `{status, total_distance_meters, route_coordinates: EncodedPolyline (precision 5), source, warning, graph_radius_meters}`

---

## 4. Optimisasi yang Diterapkan

1. **Lifespan preload** — graf preload ke `app.state.path_graph` saat startup; handler tidak membaca disk/DB/Overpass.
2. **Async + threadpool** — endpoint `async def`; komputasi CPU-bound via `asyncio.to_thread`; Rust melepas GIL.
3. **Serialisasi** — native FastAPI (keputusan: `ORJSONResponse` dihapus karena deprecated; `orjson` tetap terpasang).
4. **Pydantic V2** — skema ringan, koordinat `Tuple[float,float]`.
5. **Multi-tier algoritma** — CH → Bidirectional ALT → ALT A* → A*; penalti memaksa A*.
6. **Rust Bidirectional Dijkstra** — adjacency dense `Vec<Vec<Edge>>`, reverse adjacency, terminasi `mu`, penalti-aware, load-once.
7. **Redis route cache** — key berbasis scope + signature penalti, TTL 300.
8. **CH witness budget** — mempercepat preprocessing tanpa menurunkan kualitas signifikan.
9. **Graceful degradation** — OSM/Overpass, DB, Redis, Rust semuanya punya fallback tanpa mematikan app.

---

## 5. Konfigurasi Lingkungan (`.env.example`)

| Variabel | Default | Keterangan |
|---|---|---|
| `OSMNX_OVERPASS_URL` | `https://overpass-api.de/api` | Mirror Overpass |
| `OSMNX_MAX_RADIUS` | `50000` | Radius maksimum (m) |
| `OSMNX_CACHE_GRID` | `0.005` | Snap grid cache (derajat) |
| `OSMNX_DISK_CACHE` | `cache/pathfinding` | Direktori cache disk |
| `OSMNX_ENABLE_CH` | `1` | Aktifkan Contraction Hierarchies |
| `OSMNX_CH_WITNESS_NODES` | `2500` | Budget witness search CH |
| `REDIS_URL` | `redis://{REDIS_HOST}:{REDIS_PORT}` | Override koneksi Redis |
| `DATABASE_URL` | dari `DB_USER/PASSWORD/HOST/PORT/NAME` | Override koneksi DB |
| `CORS_ORIGINS` | `http://localhost:8000` | Daftar origin (koma) |

---

## 6. Status & Pekerjaan Menyusul

| Item | Status |
|---|---|
| Redis integration (`redis_integration.md`) | ✅ Selesai & terverifikasi |
| Rust core engine (`rust_core_engine.md`) | ✅ Selesai lokal; build Docker belum |
| FastAPI optimization (`fastapi_optimize.md`) | ✅ #1 lifespan, #2 async+to_thread, #4 Pydantic V2; #3 orjson → native |
| Riwayat rute (endpoint + simpan `RouteHistory`) | ⏳ Belum di-wire |
| Docker builder stage (Rust + maturin) | ⏳ Menyusul |
| `.env` | ❌ Tidak boleh dibuka/diubah (aturan user) |
