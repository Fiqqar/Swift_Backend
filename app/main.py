import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router

_DEFAULT_WARMUP_PAIRS = [(-6.8048, 110.8385, -6.8100, 110.8500)]


def _region_bbox() -> tuple | None:
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
    cover_task = None
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

        from app.services.pathfinding.graph_loader import (
            load_local_graph_covering,
        )
        _cov_off = float(os.environ.get("PREWARM_COVER_OFFSET_DEG", "0.03"))
        _cov_n = int(os.environ.get("PREWARM_COVER_CELLS", "2"))

        async def _prewarm_cover_cells(items):
            pairs = [(0.0, 0.0), (_cov_off, _cov_off), (_cov_off, -_cov_off),
                     (-_cov_off, _cov_off), (-_cov_off, -_cov_off)]
            for lat, lon in items:
                for dlat, dlon in pairs[:_cov_n]:
                    try:
                        await asyncio.to_thread(
                            load_local_graph_covering, lat, lon,
                            lat + dlat, lon + dlon, level=1)
                        logging.getLogger("app").info(
                            "[STARTUP] Prewarm cover (%.4f,%.4f) cell "
                            "(%.3f,%.3f) selesai.", lat, lon, dlat, dlon)
                    except Exception as exc:
                        logging.getLogger("app").warning(
                            "[STARTUP] Prewarm cover (%.4f,%.4f) cell "
                            "(%.3f,%.3f) gagal: %s", lat, lon, dlat, dlon, exc)

        if _cov_off > 0 and _cov_n > 0:
            cover_task = asyncio.create_task(_prewarm_cover_cells(cities))

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

    base_task = None
    from app.services.pathfinding.graph_loader import base_available, load_base_graph
    if base_available():
        async def _load_base():
            await asyncio.to_thread(load_base_graph)
            logging.getLogger("app").info("[STARTUP] Base graph loaded.")
        base_task = asyncio.create_task(_load_base())

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

    traffic_task = None
    if redis_client is not None:
        from app.services.traffic.poller import (
            should_start_poller,
            traffic_poller,
        )
        if should_start_poller():
            traffic_task = asyncio.create_task(traffic_poller(app, redis_client))
            app.state.traffic_task = traffic_task

    nav_task = None
    from app.services.navigation import NavRegistry, navigation_worker
    app.state.nav_registry = NavRegistry()
    if os.environ.get("ENABLE_LIVE_NAVIGATION", "0") == "1":
        nav_task = asyncio.create_task(navigation_worker(app))
        app.state.nav_task = nav_task

    try:
        yield
    finally:
        if warmup_task is not None:
            warmup_task.cancel()
        if city_task is not None:
            city_task.cancel()
        if cover_task is not None:
            cover_task.cancel()
        if region_task is not None:
            region_task.cancel()
        if base_task is not None:
            base_task.cancel()
        if traffic_task is not None:
            traffic_task.cancel()
        if nav_task is not None:
            nav_task.cancel()
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
    description=(
        "API pathfinding & pengiriman (delivery) dengan engine rute "
        "ALT + CH, hierarchical routing (graf gang level-1 dari tile lokal), "
        "traffic TomTom, dan manajemen kurir/batch/shipment.\n\n"
        "Gunakan tombol **Authorize** untuk memasukkan token dari "
        "`POST /api/v1/auth/login` agar endpoint yang membutuhkan autentikasi "
        "dapat diuji."
    ),
    version="1.0.0",
    openapi_tags=[
        {
            "name": "Pathfinding",
            "description": "Pencarian rute, route options, hierarchical & "
                           "last-mile (gang).",
        },
        {
            "name": "Auth",
            "description": "Login kurir dan profil token.",
        },
        {
            "name": "Shipment",
            "description": "Manajemen shipment, batch, billing, tracking.",
        },
        {
            "name": "Traffic",
            "description": "Peta kepadatan dan status lalu lintas.",
        },
        {
            "name": "Tracking",
            "description": "Real-time tracking posisi kurir (WebSocket) dan "
                           "status live-tracking.",
        },
        {
            "name": "Navigation",
            "description": "Real-time navigation & auto-rerouting kurir "
                           "(WebSocket): progress rute, off-route warning, "
                           "reroute berbasis traffic.",
        },
    ],
    lifespan=lifespan,
    docs_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_OPENAPI_SCHEMA = None


def _openapi():
    global _OPENAPI_SCHEMA
    if _OPENAPI_SCHEMA is not None:
        return _OPENAPI_SCHEMA
    schema = FastAPI.openapi(app)
    schema.setdefault("components", {}).setdefault(
        "securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "Masukkan token dari endpoint `POST /api/v1/auth/login`. "
            "Contoh: `POST /api/v1/auth/login` dengan body "
            "`{\"username\": \"...\", \"password\": \"...\"}` lalu salin token "
            "`access_token` dari respons."
        ),
    }
    schema.setdefault("security", []).insert(
        0, {"BearerAuth": []})
    schema.setdefault("paths", {})["/api/v1/ws/driver/position"] = {
        "get": {
            "tags": ["Tracking"],
            "summary": "WS Real-time posisi kurir (WebSocket)",
            "description": (
                "**PENTING — JANGAN tekan \"Try it out\".**\n\n"
                "Endpoint ini adalah **WebSocket**, bukan HTTP biasa. Route "
                "hanya menanggapi handshake `ws://`/`wss://`. Jika diuji lewat "
                "tombol \"Try it out\", Postman request HTTP, atau dibuka di "
                "browser, akan muncul **404 Not Found** — itu **normal dan "
                "diharapkan**, karena tidak ada route HTTP di path ini.\n\n"
                "### Prasyarat pengujian\n"
                "1. Aktifkan fitur: set `ENABLE_LIVE_TRACKING=1` di `.env` lalu "
                "restart server. Tanpa ini handshake WS berhasil tetapi koneksi "
                "langsung ditutup kode `1008`.\n"
                "2. Ambil token: `POST /api/v1/auth/login` → salin `data.token` "
                "dari respons.\n"
                "3. Verifikasi status: `GET /api/v1/ws/driver/position/status` "
                "harus mengembalikan `{\"enabled\": true}`.\n\n"
                "### URL\n"
                "`ws://localhost:8000/api/v1/ws/driver/position`\n\n"
                "### Cara konek (klien WebSocket, bukan HTTP)\n"
                "- **wscat:** `wscat -c "
                "\"ws://localhost:8000/api/v1/ws/driver/position?token=<JWT>\"`\n"
                "- **Python:** `uv run python -m websockets "
                "\"ws://localhost:8000/api/v1/ws/driver/position?token=<JWT>\"`\n"
                "- **Postman:** New → WebSocket Request → "
                "`ws://localhost:8000/api/v1/ws/driver/position`, isi query "
                "`token` atau header `Authorization: Bearer <JWT>`.\n\n"
                "### Autentikasi (handshake)\n"
                "- Query param `?token=<JWT>` **atau** header "
                "`Authorization: Bearer <JWT>` (token dari "
                "`POST /api/v1/auth/login`).\n"
                "- Gagal auth → server tutup koneksi kode `4401`.\n"
                "- Bila `ENABLE_LIVE_TRACKING=0` → tutup kode `1008`.\n\n"
                "### Pesan dari klien\n"
                "- `{\"type\":\"ping\"}` → server balas "
                "`{\"type\":\"ack\",\"ok\":true,\"ts\":<unix>}`.\n"
                "- `{\"type\":\"position\",\"lat\":-6.80,\"lon\":110.83,"
                "\"bearing\":90,\"speed\":10}` → simpan ke Redis "
                "`driver:pos:{kurir_id}`, snap best-effort, cek geofence.\n\n"
                "### Pesan dari server\n"
                "- `{\"type\":\"ack\",\"ok\":true,\"snapped\":[lat,lon]|null,"
                "\"ts\":<unix>}` — posisi diterima.\n"
                "- `{\"type\":\"error\",\"ok\":false,\"detail\":\"...\"}` — "
                "JSON/koordinat tidak valid.\n"
                "- `{\"type\":\"geofence_enter\",\"package_id\":1,"
                "\"distance_m\":12.5,\"radius_m\":30}` — masuk radius stop.\n"
                "- `{\"type\":\"geofence_exit\",\"package_id\":1,"
                "\"distance_m\":45.2,\"radius_m\":30}` — keluar radius stop.\n\n"
                "### Keterbatasan\n"
                "- Rate limit `KURIR_POS_MAX_RATE_SECONDS` (default 3 s) per "
                "koneksi.\n"
                "- Geofence: radius 30 m; state machine per paket di Redis "
                "`driver:geofence:{kurir_id}:{package_id}` (event hanya dikirim "
                "saat transisi state)."
            ),
            "security": [{"BearerAuth": []}],
            "responses": {
                "200": {"description": "Koneksi WebSocket berlangsung"}
            },
        }
    }
    schema.setdefault("paths", {})["/api/v1/ws/navigation"] = {
        "get": {
            "tags": ["Navigation"],
            "summary": "WS Real-time navigation & auto-rerouting (WebSocket)",
            "description": (
                "**PENTING — JANGAN tekan \"Try it out\".**\n\n"
                "Endpoint ini adalah **WebSocket**, bukan HTTP biasa. Route "
                "hanya menanggapi handshake `ws://`/`wss://`. Jika diuji lewat "
                "tombol \"Try it out\", Postman request HTTP, atau dibuka di "
                "browser, akan muncul **404 Not Found** — itu **normal dan "
                "diharapkan**, karena tidak ada route HTTP di path ini.\n\n"
                "### Prasyarat pengujian\n"
                "1. Aktifkan fitur: set `ENABLE_LIVE_NAVIGATION=1` di `.env` lalu "
                "restart server. Tanpa ini handshake WS berhasil tetapi koneksi "
                "langsung ditutup kode `1008`.\n"
                "2. Ambil token: `POST /api/v1/auth/login` → salin `data.token`.\n"
                "3. Hitung rute dulu: `POST /api/v1/pathfinding/find-route` "
                "(atau `/find-optimized-delivery-route`) dengan header Bearer. "
                "Respons berisi `route_id` yang dipakai sebagai `current_route_id`.\n"
                "4. Verifikasi status: `GET /api/v1/ws/navigation/status` harus "
                "mengembalikan `{\"enabled\": true}`.\n\n"
                "### URL\n"
                "`ws://localhost:8000/api/v1/ws/navigation`\n\n"
                "### Autentikasi (handshake)\n"
                "- Query param `?token=<JWT>` **atau** header "
                "`Authorization: Bearer <JWT>`.\n"
                "- Gagal auth → server tutup koneksi kode `4401`.\n"
                "- Bila `ENABLE_LIVE_NAVIGATION=0` → tutup kode `1008`.\n\n"
                "### Pesan dari klien\n"
                "- `{\"type\":\"ping\"}` → balas `ack`.\n"
                "- `{\"type\":\"start_navigation\",\"route_id\":<id>,"
                "\"leg_index\":0}` → muat snapshot rute dari Redis "
                "`driver:nav:{kurir_id}`; balas `ack` berisi polyline leg aktif.\n"
                "- `{\"type\":\"location_update\",\"lat\":-6.80,\"lng\":110.83,"
                "\"bearing\":90,\"speed\":10,\"current_route_id\":<id>}` → "
                "simpan posisi, kirim `route_progress`, cek off-route, dan "
                "auto-reroute bila perlu.\n\n"
                "### Pesan dari server\n"
                "- `{\"type\":\"ack\",\"ok\":true,...}` — handshake pesan sukses.\n"
                "- `{\"type\":\"route_progress\",\"remaining_distance_m\":..,"
                "\"remaining_time_s\":..,\"progress_pct\":..}`.\n"
                "- `{\"type\":\"off_route_warning\",\"distance_m\":..,"
                "\"threshold_m\":40}` — kurir keluar jalur rute aktif.\n"
                "- `{\"type\":\"auto_rerouted\"|\"reroute_available\","
                "\"polyline\":\"<encoded>\",\"saving_s\":..,\"eta_s\":..,"
                "\"applied\":true}` — rute baru lebih cepat / kurir off-route.\n\n"
                "### Keterbatasan\n"
                "- Rate limit `NAV_POS_MAX_RATE_SECONDS` (default 3 s) per "
                "koneksi.\n"
                "- Cooldown reroute `REROUTE_COOLDOWN_SECONDS` (default 30 s) "
                "mencegah route flickering."
            ),
            "security": [{"BearerAuth": []}],
            "responses": {
                "200": {"description": "Koneksi WebSocket berlangsung"}
            },
        }
    }
    _OPENAPI_SCHEMA = schema
    return schema


app.openapi = _openapi


_POSTMAN_BUTTON = """
<style>
  #postman-export-btn {
    position: fixed;
    top: 84px;
    right: 20px;
    z-index: 9999;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 14px;
    border: none;
    border-radius: 4px;
    background: #ff6c37;
    color: #fff;
    font-size: 13px;
    font-weight: 600;
    font-family: sans-serif;
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,.25);
  }
  #postman-export-btn:hover { background: #e65a28; }
  #postman-toast {
    position: fixed;
    top: 130px;
    right: 20px;
    z-index: 10000;
    max-width: 320px;
    padding: 10px 14px;
    border-radius: 4px;
    background: #323232;
    color: #fff;
    font-size: 12px;
    font-family: sans-serif;
    line-height: 1.4;
    box-shadow: 0 2px 8px rgba(0,0,0,.35);
    opacity: 0;
    transition: opacity .25s ease;
    pointer-events: none;
  }
  #postman-toast.show { opacity: 1; }
</style>
<button id="postman-export-btn" type="button">
  Export to Postman
</button>
<div id="postman-toast"></div>
<script>
  (function () {
    var btn = document.getElementById("postman-export-btn");
    var toast = document.getElementById("postman-toast");
    function showToast(msg, isError) {
      toast.textContent = msg;
      toast.style.background = isError ? "#c62828" : "#323232";
      toast.classList.add("show");
      setTimeout(function () { toast.classList.remove("show"); }, 6000);
    }
    function downloadJson(url, filename) {
      return fetch(url)
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function (data) {
          var blob = new Blob(
            [JSON.stringify(data, null, 2)],
            { type: "application/json" }
          );
          var objUrl = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = objUrl;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(objUrl);
        });
    }
    btn.addEventListener("click", function () {
      Promise.all([
        downloadJson("/api/v1/export/postman", "postman_collection.json"),
        downloadJson("/api/v1/export/postman/environment", "dev.postman_environment.json")
      ])
        .then(function () {
          showToast(
            "postman_collection.json + dev.postman_environment.json diunduh. " +
            "Di Postman: Import → File → pilih keduanya. " +
            "Jalankan POST /auth/login dulu, token tersimpan otomatis."
          );
        })
        .catch(function (err) {
          showToast("Export gagal: " + err.message, true);
        });
    });
  })();
</script>
"""


@app.get("/api/v1/export/postman", include_in_schema=False)
async def export_postman_collection():
    from app.services.postman_export import build_postman_collection
    return JSONResponse(
        build_postman_collection(app.openapi()),
        headers={"Content-Disposition": 'attachment; filename="postman_collection.json"'},
    )


@app.get("/api/v1/export/postman/environment", include_in_schema=False)
async def export_postman_environment():
    from app.services.postman_export import build_dev_environment
    return JSONResponse(
        build_dev_environment(),
        headers={"Content-Disposition": 'attachment; filename="dev.postman_environment.json"'},
    )


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui():
    html = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - Swagger UI",
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        swagger_ui_parameters={"persistAuthorization": True},
    )
    content = html.body.decode("utf-8")
    content = content.replace("</body>", _POSTMAN_BUTTON + "</body>")
    return HTMLResponse(content)


@app.exception_handler(HTTPException)
async def _uniform_http_exception(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "success": False,
            "message": str(exc.detail),
        },
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
            graph, locations, edge_classes, source, warning = load_osm_graph_by_point(
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
            coords, total, _src, warn, _nodes = route_hierarchical(hier)
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
