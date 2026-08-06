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

_DEMO_ORIGIN = (-6.1754, 106.8272)
_DEMO_DESTINATION = (-6.1830, 106.8360)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.pathfinding.graph_loader import load_graph_covering
    try:
        pg = await asyncio.to_thread(
            load_graph_covering,
            _DEMO_ORIGIN[0], _DEMO_ORIGIN[1],
            _DEMO_DESTINATION[0], _DEMO_DESTINATION[1],
        )
        app.state.path_graph = pg
    except Exception:
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
        import osmnx 
        osmnx_available = True
    except Exception as exc:
        osmnx_error = f"{type(exc).__name__}: {exc}"

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
    try:
        from app.services.pathfinding.core_a_star import run_a_star
        from app.services.pathfinding.graph_loader import (
            load_osm_graph_by_point,
            find_nearest_node,
            auto_radius,
        )

        dist_meters = auto_radius(
            _DEMO_ORIGIN[0], _DEMO_ORIGIN[1],
            _DEMO_DESTINATION[0], _DEMO_DESTINATION[1],
        )
        graph, locations, source, warning = load_osm_graph_by_point(
            lat=_DEMO_ORIGIN[0], lon=_DEMO_ORIGIN[1], dist_meters=dist_meters
        )
        start = find_nearest_node(_DEMO_ORIGIN[0], _DEMO_ORIGIN[1], locations)
        goal = find_nearest_node(_DEMO_DESTINATION[0], _DEMO_DESTINATION[1], locations)
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
        "overpass_reachable": overpass_reachable,
        "route_test": route_test,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


app.mount("/", StaticFiles(directory="app/ui", html=True), name="ui")
