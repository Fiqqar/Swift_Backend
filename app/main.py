import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router

_DEFAULT_WARMUP_PAIRS = [(-6.8048, 110.8385, -6.8100, 110.8500)]


def _region_bbox() -> tuple | None:
    """Baca REGION_GRAPH_BBOX (lat1,lon1,lat2,lon2).

    Graf regional ini memakai filter jalan utama dan mencakup area yang lebih
    luas (default Kudus-Semarang-Pati-Jepara) sehingga rute jarak kota
    menengah tidak memicu scan file PBF besar per-request. Set
    REGION_GRAPH_ENABLED=0 untuk menonaktifkan.
    """
    if os.environ.get("REGION_GRAPH_ENABLED", "1") != "1":
        return None
    raw = os.environ.get("REGION_GRAPH_BBOX", "").strip()
    if raw:
        parts = [c.strip() for c in raw.split(",")]
        try:
            if len(parts) == 4:
                return tuple(float(c) for c in parts)
        except ValueError:
            pass
        logging.getLogger("app").warning(
            "REGION_GRAPH_BBOX tidak valid, pakai default: %s", raw)
    from app.services.pathfinding.graph_loader import DEFAULT_REGION_BBOX
    return DEFAULT_REGION_BBOX


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


def _prewarm_cities() -> list:
    """Baca PREWARM_CITIES (lat,lon dipisah '|').

    Kota prioritas: graf local (dari tile) dibangun di background saat
    startup agar rute dari/ke kota itu langsung instan.
    """
    raw = os.environ.get("PREWARM_CITIES", "").strip()
    if not raw:
        return []
    cities = []
    for part in raw.split("|"):
        part = part.strip()
        if not part:
            continue
        coords = [c.strip() for c in part.split(",")]
        try:
            if len(coords) == 2:
                cities.append((float(coords[0]), float(coords[1])))
            else:
                raise ValueError("harus 2 angka")
        except ValueError:
            logging.getLogger("app").warning(
                "PREWARM_CITIES tidak valid, dilewati: %s", part)
    return cities


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.pathfinding.graph_loader import (
        REGION_LEVEL,
        load_graph_covering,
        region_graph_cached,
    )
    warmup_task = None
    pairs = _warmup_pairs()
    if pairs:
        lat1, lon1, lat2, lon2 = pairs[0]
        if region_graph_cached(lat1, lon1, lat2, lon2):
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
        else:
            app.state.path_graph = None
            logging.getLogger("app").info(
                "[STARTUP] Graf kecil warm-up belum di-cache, dilewati "
                "(region graph mencakup area ini).")

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

    city_task = None
    from app.services.pathfinding.graph_loader import (
        load_local_graph_point,
        tiles_enabled,
    )
    cities = _prewarm_cities()
    if cities and tiles_enabled():
        async def _prewarm_city_graphs(items):
            for lat, lon in items:
                try:
                    await asyncio.to_thread(load_local_graph_point, lat, lon)
                    logging.getLogger("app").info(
                        "[STARTUP] Prewarm kota (%.4f,%.4f) selesai.", lat, lon)
                except Exception as exc:
                    logging.getLogger("app").warning(
                        "[STARTUP] Prewarm kota (%.4f,%.4f) gagal: %s",
                        lat, lon, exc)
        city_task = asyncio.create_task(_prewarm_city_graphs(cities))

    app.state.region_graph = None
    region_task = None
    region_box = _region_bbox()
    if region_box is not None:
        lat1, lon1, lat2, lon2 = region_box

        async def _load_region():
            rg = await asyncio.to_thread(
                load_graph_covering, lat1, lon1, lat2, lon2, REGION_LEVEL)
            app.state.region_graph = rg
            logging.getLogger("app").info(
                "[STARTUP] Region graph loaded (source=%s, radius=%s).",
                rg.source, rg.radius)

        if region_graph_cached(lat1, lon1, lat2, lon2, REGION_LEVEL):
            try:
                await _load_region()
            except Exception as exc:
                logging.getLogger("app").warning(
                    "[STARTUP] Gagal memuat region graph dari cache, "
                    "dilanjutkan di background: %s", exc)
                region_task = asyncio.create_task(_load_region())
        else:
            logging.getLogger("app").info(
                "[STARTUP] Region graph belum ada di cache, dimuat di "
                "background (jalankan scripts/prebuild_region.py untuk "
                "pre-build).")
            region_task = asyncio.create_task(_load_region())

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
        if city_task is not None:
            city_task.cancel()
        if region_task is not None:
            region_task.cancel()
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
    pg = (getattr(app.state, "region_graph", None)
          or getattr(app.state, "path_graph", None))
    demo_origin, demo_dest = _demo_pair()
    if pg is not None:
        try:
            from app.services.pathfinding.core_engine import route as engine_route
            from app.services.pathfinding.graph_loader import find_nearest_node

            # Uji rute di wilayah graf yang sedang dimuat (cepat, region-agnostik).
            radius = max(pg.radius, 1000)
            d_deg = radius / 111320.0
            candidates = [
                ((pg.ref_lat - d_deg * 0.35, pg.ref_lon - d_deg * 0.35),
                 (pg.ref_lat + d_deg * 0.35, pg.ref_lon + d_deg * 0.35)),
                ((pg.ref_lat - d_deg * 0.20, pg.ref_lon - d_deg * 0.20),
                 (pg.ref_lat + d_deg * 0.20, pg.ref_lon + d_deg * 0.20)),
                (demo_origin, demo_dest),
            ]
            node_path = None
            total_distance = 0.0
            for test_origin, test_dest in candidates:
                start = find_nearest_node(
                    test_origin[0], test_origin[1], pg.locations)
                goal = find_nearest_node(
                    test_dest[0], test_dest[1], pg.locations)
                if start is None or goal is None:
                    continue
                node_path, total_distance = engine_route(pg, start, goal)
                if node_path:
                    break
            route_test = {
                "ok": node_path is not None,
                "total_distance_meters": round(total_distance, 2) if node_path else None,
                "route_points": len(node_path) if node_path else 0,
                "source": pg.source,
                "warning": pg.warning,
                "graph_radius_meters": pg.radius,
                "graph_bbox": getattr(pg, "bbox", None),
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

    from app.services.pathfinding.graph_loader import (
        base_available,
        base_bbox,
        base_loaded,
        tiles_enabled,
    )
    hierarchical_info = {
        "tiles_enabled": tiles_enabled(),
        "base_available": base_available(),
        "base_loaded": base_loaded(),
        "base_bbox": base_bbox(),
    }
    hierarchical_test = getattr(app.state, "hierarchical_test", None)
    if (hierarchical_test is None and base_loaded() and tiles_enabled()):
        try:
            from app.services.pathfinding.hierarchical import (
                build_hierarchical,
                route_hierarchical,
            )
            hier = build_hierarchical(
                -6.8048, 110.8385, -6.1751, 106.8650)
            coords, total, _src, warn = route_hierarchical(hier)
            hierarchical_test = {
                "ok": bool(coords),
                "total_distance_meters": round(total, 2) if coords else None,
                "route_points": len(coords) if coords else 0,
                "warning": warn,
            }
            app.state.hierarchical_test = hierarchical_test
        except Exception as exc:
            hierarchical_test = {"ok": False, "error": str(exc)}
            app.state.hierarchical_test = hierarchical_test

    return JSONResponse({
        "status": "ok",
        "app": app.title,
        "version": app.version,
        "osmnx_available": osmnx_available,
        "osmnx_error": osmnx_error,
        "pbf_available": pbf_available_local,
        "overpass_reachable": overpass_reachable,
        "route_test": route_test,
        "hierarchical": hierarchical_info,
        "hierarchical_test": hierarchical_test,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


app.mount("/", StaticFiles(directory="app/ui", html=True), name="ui")
