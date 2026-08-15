"""Real-time tracking posisi kurir via WebSocket.

Endpoint: `WS /api/v1/ws/driver/position`

- Autentikasi: token JWT lewat query param `token` atau header
  `Authorization: Bearer <token>` saat handshake.
- Guard: `ENABLE_LIVE_TRACKING` (bila False, koneksi ditolak).
- Pesan masuk `{type:"position", lat, lon, bearing?, speed?}`:
  - Validasi koordinat + rate limit.
  - Snapping opsional ke graf (best-effort).
  - Simpan posisi ke Redis `driver:pos:{kurir_id}`.
  - Geofence detection server-side: cek stop belum terkirim pada batch
    aktif, transisi state di Redis `driver:geofence:{kurir_id}:{package_id}`,
    push `geofence_enter`/`geofence_exit` hanya saat transisi.

Design doc: docs/feature/courier_gps_tracking.md (Bagian 4 & 6.4).
"""

import json
import logging
import os

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core.security import decode_access_token
from app.models.batch import Batch
from app.models.paket import Paket
from app.models.shipment import Shipment
from app.services.pathfinding.core_a_star import haversine_distance
from app.services.tracking import (
    del_geofence_state,
    get_geofence_state,
    get_stops_cache,
    set_geofence_state,
    set_kurir_position,
    set_stops_cache,
)

router = APIRouter(tags=["Tracking"])

logger = logging.getLogger("pathfinding")

_GEOFENCE_RADIUS_M = 30.0
_MAX_RATE_SECONDS = float(os.environ.get("KURIR_POS_MAX_RATE_SECONDS", "3"))

# Status shipment yang dianggap "belum terkirim" (masih relevan utk geofence).
_UNDELIVERED = ("assigned", "picked_up")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw in ("1", "true", "True", "TRUE", "yes", "on")


def _live_tracking_enabled() -> bool:
    return _env_bool("ENABLE_LIVE_TRACKING", False)


def _auth_kurir_id(websocket: WebSocket) -> int | None:
    token = websocket.query_params.get("token")
    if not token:
        auth = websocket.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    payload = decode_access_token(token)
    if payload is None:
        return None
    try:
        return int(payload["sub"])
    except (KeyError, ValueError):
        return None


async def _load_undelivered_stops(sessionmaker, kurir_id: int) -> list[dict]:
    if sessionmaker is None:
        return []
    try:
        async with sessionmaker() as session:
            stmt = (
                select(Paket.id, Paket.latitude, Paket.longitude,
                       Paket.service_type)
                .join(Shipment, Shipment.paket_id == Paket.id)
                .join(Batch, Batch.id == Shipment.batch_id)
                .where(Batch.kurir_id == kurir_id)
                .where(Shipment.status.in_(_UNDELIVERED))
                .where(Paket.latitude.isnot(None))
                .where(Paket.longitude.isnot(None))
            )
            rows = (await session.execute(stmt)).all()
            return [
                {"package_id": pid, "lat": float(lat), "lon": float(lon),
                 "service_type": service_type}
                for pid, lat, lon, service_type in rows
            ]
    except Exception as exc:
        logger.warning("Gagal memuat stop belum terkirim kurir %s: %s",
                       kurir_id, exc)
        return []


async def _get_stops(redis, sessionmaker, kurir_id: int) -> list[dict]:
    cached = await get_stops_cache(redis, kurir_id)
    if cached is not None:
        return cached
    stops = await _load_undelivered_stops(sessionmaker, kurir_id)
    await set_stops_cache(redis, kurir_id, stops)
    return stops


async def _try_snap(app, lat: float, lon: float):
    """Snap koordinat mentah ke edge jalan terdekat (best-effort).

    Kembalikan tuple (lat, lon) hasil snap, atau None bila graf tidak siap.
    """
    from app.services.pathfinding.graph_loader import (
        base_available,
        base_loaded,
        find_nearest_node,
        load_base_graph,
    )
    from app.services.pathfinding.snap import snap_point_to_graph
    try:
        if not (base_available() and base_loaded()):
            return None
        pg = load_base_graph()
        node_id = await run_in_threadpool(
            find_nearest_node, lat, lon, pg.locations)
        if node_id is None:
            return None
        snapped = await run_in_threadpool(
            snap_point_to_graph, pg.graph, pg.locations, lat, lon, node_id)
        return snapped
    except Exception as exc:
        logger.debug("Snapping gagal (%f, %f): %s", lat, lon, exc)
        return None


async def _process_position(websocket: WebSocket, kurir_id: int,
                            app, redis, sessionmaker, msg: dict):
    lat = msg.get("lat")
    lon = msg.get("lon")
    if lat is None or lon is None:
        await websocket.send_json({
            "type": "error", "ok": False, "detail": "lat/lon wajib diisi"})
        return
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        await websocket.send_json({
            "type": "error", "ok": False, "detail": "lat/lon tidak valid"})
        return
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Koordinat di luar rentang"})
        return

    snapped = await _try_snap(app, lat, lon)
    stored = await set_kurir_position(
        redis, kurir_id, lat, lon,
        bearing=msg.get("bearing"), speed=msg.get("speed"),
        snapped=snapped)

    await websocket.send_json({
        "type": "ack", "ok": True,
        "stored": stored,
        "warning": (None if stored else
                    "Redis tidak tersedia, posisi tidak disimpan"),
        "snapped": list(snapped) if snapped else None,
        "ts": int(__import__("time").time()),
    })

    await _geofence_check(websocket, redis, sessionmaker, kurir_id, lat, lon)


async def _geofence_check(websocket: WebSocket, redis, sessionmaker,
                          kurir_id: int, lat: float, lon: float) -> None:
    stops = await _get_stops(redis, sessionmaker, kurir_id)
    for stop in stops:
        package_id = stop["package_id"]
        dist = haversine_distance((lat, lon), (stop["lat"], stop["lon"]))
        inside = dist <= _GEOFENCE_RADIUS_M
        prev_state = await get_geofence_state(redis, kurir_id, package_id)

        from app.services.tracking import geofence_event
        event = geofence_event(
            prev_state, inside, package_id, dist, _GEOFENCE_RADIUS_M)
        if event is None:
            continue
        await set_geofence_state(
            redis, kurir_id, package_id, "inside" if inside else "outside")
        await websocket.send_json(event)


@router.get("/ws/driver/position/status",
            summary="Status live-tracking kurir",
            description=(
                "Menampilkan status konfigurasi WebSocket live-tracking "
                "`WS /api/v1/ws/driver/position`.\n\n"
                "- `enabled`: apakah `ENABLE_LIVE_TRACKING` aktif (bila False, "
                "koneksi WS ditolak kode 1008).\n"
                "- `redis_connected`: apakah Redis terhubung (bila False, "
                "posisi yang dikirim via WS tidak akan tersimpan dan `ack` "
                "berisi `stored:false` + `warning`).\n"
                "- `max_rate_seconds`: batas interval pengiriman posisi "
                "(env `KURIR_POS_MAX_RATE_SECONDS`)."))
async def tracking_status(request: Request):
    redis = getattr(request.app.state, "redis", None)
    return {
        "enabled": _live_tracking_enabled(),
        "redis_connected": redis is not None,
        "max_rate_seconds": _MAX_RATE_SECONDS,
    }


@router.websocket("/ws/driver/position")
async def driver_position_ws(websocket: WebSocket):
    if not _live_tracking_enabled():
        await websocket.close(code=1008, reason="Live tracking disabled")
        return
    await websocket.accept()

    kurir_id = _auth_kurir_id(websocket)
    if kurir_id is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return

    app = websocket.app
    redis = getattr(app.state, "redis", None)
    sessionmaker = getattr(app.state, "sessionmaker", None)

    import time
    last_rate_ts = 0.0
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                await websocket.send_json({
                    "type": "error", "ok": False, "detail": "JSON tidak valid"})
                continue
            mtype = msg.get("type")
            if mtype == "ping":
                await websocket.send_json({
                    "type": "ack", "ok": True,
                    "ts": int(time.time())})
                continue
            if mtype != "position":
                continue
            now = time.time()
            if now - last_rate_ts < _MAX_RATE_SECONDS:
                continue
            last_rate_ts = now
            await _process_position(
                websocket, kurir_id, app, redis, sessionmaker, msg)
    except WebSocketDisconnect:
        logger.info("Kurir %s terputus dari WS posisi.", kurir_id)
    except Exception as exc:
        logger.warning("WS driver error kurir %s: %s", kurir_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
