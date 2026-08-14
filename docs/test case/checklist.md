# Checklist Audit Codebase & Kesiapan Production (FastAPI, Docker & OSMnx)

Berikut adalah template checklist audit komprehensif yang bisa kamu simpan sebagai file `.md` (misalnya `AUDIT_CHECKLIST.md`) untuk melakukan verifikasi kode proyek secara bertahap.

---

## 1. Startup & Lifespan Performance Check

| Parameter Pengecekan | Status (`[ ]`) | Catatan / Target Implementasi |
| :--- | :---: | :--- |
| **Non-blocking Startup** | `[ ]` | Fungsi `lifespan` / `@app.on_event("startup")` bebas dari proses pengunduhan peta OSMnx atau komputasi geospasial berat. |
| **Lazy Loading OSMnx** | `[ ]` | Inisialisasi atau pemanggilan graph OSMnx dilakukan *on-demand* di dalam route/background task, bukan di level global/startup. |
| **Database Connection Timeout** | `[ ]` | Koneksi ke PostGIS/SQLAlchemy memiliki opsi `connect_args={"timeout": ...}` agar tidak menggantung tanpa batas jika DB belum siap. |
| **Async Healthcheck** | `[ ]` | Pengecekan status DB & Redis di endpoint `/health` menggunakan perintah *ping* atau query ringan (`SELECT 1`). |

---

## 2. Docker & Containerization Standards

| Parameter Pengecekan | Status (`[ ]`) | Catatan / Target Implementasi |
| :--- | :---: | :--- |
| **Network Hostname** | `[ ]` | String koneksi DB dan Redis di `.env` memakai nama service Docker (`db`, `redis`), bukan `localhost` atau `127.0.0.1`. |
| **Port Mapping & Expose** | `[ ]` | Dockerfile mencantumkan `EXPOSE 8000` dan `docker-compose.yml` mengisolasi port host ke `- "8000:8000"`. |
| **Layer Caching Optimization** | `[ ]` | File `requirements.txt` di-copy dan di-install **sebelum** perintah `COPY . .` untuk memanfaatkan Docker layer cache. |
| **Pythondontwritebytecode** | `[ ]` | Variabel `PYTHONDONTWRITEBYTECODE=1` dan `PYTHONUNBUFFERED=1` sudah terkonfigurasi di Dockerfile. |

---

## 3. Handling Geospasial & OSMnx Integration

| Parameter Pengecekan | Status (`[ ]`) | Catatan / Target Implementasi |
| :--- | :---: | :--- |
| **C/Spatial Dependencies** | `[ ]` | Package sistem C (`libgeos-dev`, `libgdal-dev`, atau `proj-bin`) sudah terinstal di Dockerfile sebelum `pip install`. |
| **Overpass API Timeout** | `[ ]` | Konfigurasi `ox.settings.requests_timeout` atau batas waktu query OSMnx diatur dengan nilai wajar (misal 30–60 detik). |
| **Memory / Cache Management** | `[ ]` | Hasil query graph OSMnx disimpan di memori/Redis cache (`_GRAPH_CACHE`) untuk menghindari request berulang ke Overpass API. |
| **Exception Handling OSMnx** | `[ ]` | Terdapat blok `try-except` khusus untuk menangkap error koneksi network atau graph kosong dari OSMnx (`EmptyOverpassResponse`). |

---

## 4. Prompt Siap Pakai untuk Audit Otomatis (Paste ke AI)

Gunakan prompt di bawah ini untuk meminta AI menganalisis kode proyekmu berdasarkan standar di atas:

> **Role:** Senior Backend & Geospatial Engineer (FastAPI + Docker + OSMnx)  
> **Task:** Lakukan audit menyeluruh pada file `main.py`, `Dockerfile`, dan `docker-compose.yml` milik saya.  
> 
> **Panduan Audit:**  
> 1. Periksa apakah ada proses *blocking* di `lifespan` / *startup* yang menyebabkan delay pada `localhost:8000`.  
> 2. Pastikan koneksi ke PostGIS/Redis di dalam Docker menggunakan nama service, bukan `localhost`.  
> 3. Berikan nilai kesiapan proyek (Skala 1-10) beserta rekomendasi *refactoring* kode yang langsung bisa di-copy-paste.  
> 
> **File Proyek:**  
> ```dockerfile  
> 
FROM python:3.12.9

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY . .

EXPOSE 8000
    
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--proxy-headers", "--port", "8000"]  
> ```  
> ```python  
> import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router

_DEFAULT_WARMUP_PAIRS = [(-6.1754, 106.8272, -6.1830, 106.8360)]


def _warmup_pairs() -> list:
    """Baca WARMUP_ANCHORS (lat1,lon1[,lat2,lon2] dipisah '|').

    Format 2 angka = satu titik; 4 angka = pasangan origin->dest.
    Default: pasangan Monas seperti sebelumnya.
    """
    raw = os.environ.get("WARMUP_ANCHORS", "").strip()
    if not raw:
        return list(_DEFAULT_WARMUP_PAIRS)
    pairs = []
    for part in raw.split("|"):
        part = part.strip()
        if not part:
            continue
        coords = [c.strip() for c in part.split(",")]
        try:
            if len(coords) == 2:
                lat, lon = float(coords[0]), float(coords[1])
                pairs.append((lat, lon, lat, lon))
            elif len(coords) == 4:
                pairs.append(tuple(float(c) for c in coords))
            else:
                raise ValueError("harus 2 atau 4 angka")
        except ValueError:
            logging.getLogger("app").warning(
                "WARMUP_ANCHORS tidak valid, dilewati: %s", part)
    return pairs or list(_DEFAULT_WARMUP_PAIRS)


def _demo_pair():
    pairs = _warmup_pairs()
    lat1, lon1, lat2, lon2 = pairs[0]
    return (lat1, lon1), (lat2, lon2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.pathfinding.graph_loader import load_graph_covering
    warmup_task = None
    pairs = _warmup_pairs()
    if pairs:
        lat1, lon1, lat2, lon2 = pairs[0]
        try:
            pg = await asyncio.to_thread(
                load_graph_covering, lat1, lon1, lat2, lon2)
            app.state.path_graph = pg
            logging.getLogger("app").info(
                "[STARTUP] Pre-loaded path graph into RAM successfully "
                "(source=%s, radius=%s).", pg.source, pg.radius)
        except Exception as exc:
            logging.getLogger("app").warning(
                "[STARTUP] Pre-load path graph gagal: %s", exc)
            app.state.path_graph = None

        if len(pairs) > 1:
            async def _warmup_remaining(rest):
                for pair in rest:
                    try:
                        await asyncio.to_thread(load_graph_covering, *pair)
                        logging.getLogger("app").info(
                            "[STARTUP] Warm-up %s selesai.", pair)
                    except Exception as exc:
                        logging.getLogger("app").warning(
                            "[STARTUP] Warm-up %s gagal: %s", pair, exc)
            warmup_task = asyncio.create_task(_warmup_remaining(pairs[1:]))
    else:
        app.state.path_graph = None

    from app.core.database import SessionLocal, dispose_db, init_db
    try:
        await init_db()
        app.state.sessionmaker = SessionLocal
    except Exception as exc:
        logging.getLogger("app").warning(
            "Inisialisasi database gagal, app tetap berjalan: %s", exc)

    from app.core.redis import close_redis, get_redis
    redis_client = None
    try:
        redis_client = get_redis()
        await redis_client.ping()
        app.state.redis = redis_client
    except Exception as exc:
        app.state.redis = None
        logging.getLogger("app").warning(
            "Redis tidak tersedia, cache rute dinonaktifkan: %s", exc)

    try:
        yield
    finally:
        if warmup_task is not None:
            warmup_task.cancel()
        await close_redis(redis_client)
        try:
            await dispose_db()
        except Exception as exc:
            logging.getLogger("app").warning("Penutupan database gagal: %s", exc)


def _cors_origins() -> list[str]:
    raw = os.environ.get("CORS_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:8000"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


app = FastAPI(
    title="TEST2 API Engine",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
def health():
    try:
        return _build_health()
    except Exception as exc:
        return JSONResponse({
            "status": "error",
            "app": app.title,
            "version": app.version,
            "error": f"{type(exc).__name__}: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, status_code=500)


def _build_health():
    osmnx_available = False
    osmnx_error = None
    try:
        import osmnx  # noqa: F401
        osmnx_available = True
    except Exception as exc:
        osmnx_error = f"{type(exc).__name__}: {exc}"

    from app.services.pathfinding.graph_loader import pbf_available
    pbf_available_local = False
    try:
        pbf_available_local = bool(pbf_available())
    except Exception:
        pbf_available_local = False

    overpass_url = os.environ.get("OSMNX_OVERPASS_URL", "https://overpass-api.de/api").rstrip("/")
    overpass_reachable = False
    try:
        import httpx
        response = httpx.get(
            f"{overpass_url}/status",
            timeout=8,
            headers={"User-Agent": "Test2HealthCheck/1.0 (pathfinding)"},
        )
        overpass_reachable = response.status_code == 200
    except Exception:
        overpass_reachable = False

    route_test = None
    pg = getattr(app.state, "path_graph", None)
    demo_origin, demo_dest = _demo_pair()
    if pg is not None:
        try:
            from app.services.pathfinding.core_engine import route as engine_route
            from app.services.pathfinding.graph_loader import find_nearest_node

            # Uji rute di wilayah graf yang sedang dimuat (cepat, region-agnostik).
            radius = max(pg.radius, 1000)
            d_deg = radius / 111320.0 * 0.35
            test_origin = (pg.ref_lat - d_deg, pg.ref_lon - d_deg)
            test_dest = (pg.ref_lat + d_deg, pg.ref_lon + d_deg)
            start = find_nearest_node(
                test_origin[0], test_origin[1], pg.locations)
            goal = find_nearest_node(
                test_dest[0], test_dest[1], pg.locations)
            if start is None or goal is None:
                raise ValueError("Titik terdekat tidak ditemukan")
            node_path, total_distance = engine_route(pg, start, goal)
            route_test = {
                "ok": node_path is not None,
                "total_distance_meters": round(total_distance, 2) if node_path else None,
                "route_points": len(node_path) if node_path else 0,
                "source": pg.source,
                "warning": pg.warning,
                "graph_radius_meters": pg.radius,
            }
        except Exception as exc:
            route_test = {"ok": False, "error": str(exc)}

    if route_test is None:
        try:
            from app.services.pathfinding.core_a_star import run_a_star
            from app.services.pathfinding.graph_loader import (
                load_osm_graph_by_point,
                find_nearest_node,
                auto_radius,
            )

            dist_meters = auto_radius(
                demo_origin[0], demo_origin[1],
                demo_dest[0], demo_dest[1],
            )
            graph, locations, source, warning = load_osm_graph_by_point(
                lat=demo_origin[0], lon=demo_origin[1],
                dist_meters=dist_meters,
                origin=demo_origin, dest=demo_dest,
            )
            start = find_nearest_node(demo_origin[0], demo_origin[1], locations)
            goal = find_nearest_node(demo_dest[0], demo_dest[1], locations)
            if start is None or goal is None:
                raise ValueError("Titik terdekat tidak ditemukan")
            node_path, total_distance = run_a_star(graph, locations, start, goal)
            route_test = {
                "ok": node_path is not None,
                "total_distance_meters": round(total_distance, 2) if node_path else None,
                "route_points": len(node_path) if node_path else 0,
                "source": source,
                "warning": warning,
                "graph_radius_meters": dist_meters,
            }
        except Exception as exc:
            route_test = {"ok": False, "error": str(exc)}

    return JSONResponse({
        "status": "ok",
        "app": app.title,
        "version": app.version,
        "osmnx_available": osmnx_available,
        "osmnx_error": osmnx_error,
        "pbf_available": pbf_available_local,
        "overpass_reachable": overpass_reachable,
        "route_test": route_test,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


app.mount("/", StaticFiles(directory="app/ui", html=True), name="ui") 
> ```