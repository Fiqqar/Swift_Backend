"""Real-time navigation & auto-rerouting via WebSocket.

Endpoint: `WS /api/v1/ws/navigation`

- Autentikasi: token JWT lewat query param `token` atau header
  `Authorization: Bearer <token>` saat handshake.
- Guard: `ENABLE_LIVE_NAVIGATION` (bila False, koneksi ditolak kode 1008).
- Satu koneksi per kurir (session registry in-memory).

Pesan masuk:
- `start_navigation` `{type, route_id, leg_index?}` — aktifkan rute snapshot
  dari Redis `driver:nav:{kurir_id}` (diisi endpoint find-route /
  find-optimized-delivery-route).
- `location_update` `{type, lat, lng, bearing?, speed?, current_route_id}` —
  simpan posisi, kirim `route_progress`, deteksi off-route, dan (bila lewat
  cooldown) auto-reroute.
- `complete_leg` / `pod_submitted` `{type}` — kurir menyelesaikan satu
  pengiriman; navigasi pindah otomatis ke leg berikutnya (multi-leg).
- `ping` `{type}` — balas `ack`.

Pesan keluar: `ack`, `error`, `route_progress`, `off_route_warning`,
`reroute_available`, `auto_rerouted`, `route_complete`. Semua geometri memakai
`encode_polyline(..., 5)`.
"""

import json
import logging
import os
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from app.core.security import decode_access_token
from app.services import ai_agent
from app.services.navigation import (
    AI_REROUTE_ENABLED,
    AUTO_REROUTE,
    NAV_PROGRESS_MIN_INTERVAL_SECONDS,
    OFF_ROUTE_THRESHOLD_M,
    REROUTE_COOLDOWN_SECONDS,
    NavSession,
    advance_leg,
    compute_reroute,
    point_to_polyline_distance_m,
    remaining_progress,
)
from app.services.polyline import encode_polyline
from app.services.tracking import (
    clear_nav_route,
    get_nav_route,
    set_kurir_position,
)

router = APIRouter(tags=["Navigation"])

logger = logging.getLogger("pathfinding")

_NAV_POS_MAX_RATE_SECONDS = float(
    os.environ.get("NAV_POS_MAX_RATE_SECONDS", "3"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw in ("1", "true", "True", "TRUE", "yes", "on")


def _live_navigation_enabled() -> bool:
    return _env_bool("ENABLE_LIVE_NAVIGATION", False)


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


async def _apply_route(session: NavSession, nav: dict, leg_index: int) -> bool:
    """Set polyline aktif + dest dari snapshot nav (single/multi)."""
    legs = nav.get("legs") or []
    if not legs:
        return False
    leg_index = max(0, min(leg_index, len(legs) - 1))
    leg = legs[leg_index]
    from app.services.polyline import decode_polyline
    coords = decode_polyline(leg.get("encoded") or "")
    if len(coords) < 2:
        return False
    dest = leg.get("dest")
    if dest is None:
        return False
    session.route_id = nav.get("route_id")
    session.kind = nav.get("kind", "single")
    session.leg_index = leg_index
    session.coords = coords
    session.dest = (float(dest[0]), float(dest[1]))
    session.mode = nav.get("mode", "motorcycle")
    session.last_mile = bool(nav.get("last_mile", True))
    session.total_distance_m = float(nav.get("total_distance_m", 0.0) or 0.0)
    session.total_eta_s = float(nav.get("total_eta_s", 0.0) or 0.0)
    session.off_route_active = False
    return True


async def _handle_start_navigation(websocket: WebSocket, session: NavSession,
                                   redis, msg: dict) -> None:
    route_id = msg.get("route_id")
    if route_id is None:
        await websocket.send_json({
            "type": "error", "ok": False, "detail": "route_id wajib diisi"})
        return
    leg_index = int(msg.get("leg_index", 0) or 0)
    nav = await get_nav_route(redis, session.kurir_id)
    if nav is None or nav.get("route_id") != route_id:
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Route snapshot tidak ditemukan / tidak cocok. "
                      "Hitung ulang rute via API terlebih dahulu."})
        return
    if not await _apply_route(session, nav, leg_index):
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Polyline leg tidak valid pada snapshot."})
        return
    await websocket.send_json({
        "type": "ack", "ok": True,
        "route_id": session.route_id,
        "leg_index": session.leg_index,
        "kind": session.kind,
        "off_route_threshold_m": OFF_ROUTE_THRESHOLD_M,
        "polyline": encode_polyline(session.coords, 5),
        "ts": int(time.time()),
    })


async def _handle_location_update(websocket: WebSocket, app,
                                  session: NavSession, redis, msg: dict) -> None:
    lat = msg.get("lat")
    lon = msg.get("lng", msg.get("lon"))
    if lat is None or lon is None:
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "lat/lng wajib diisi"})
        return
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        await websocket.send_json({
            "type": "error", "ok": False, "detail": "lat/lng tidak valid"})
        return
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Koordinat di luar rentang"})
        return

    await set_kurir_position(
        redis, session.kurir_id, lat, lon,
        bearing=msg.get("bearing"), speed=msg.get("speed"))
    session.last_position = {"lat": lat, "lon": lon,
                             "speed": msg.get("speed"),
                             "ts": time.time()}

    now = time.time()
    if now - session.last_progress_push >= NAV_PROGRESS_MIN_INTERVAL_SECONDS:
        session.last_progress_push = now
        prog = remaining_progress(session, lat, lon)
        if prog is not None:
            await websocket.send_json({
                "type": "route_progress", "ok": True,
                "route_id": session.route_id,
                "leg_index": session.leg_index,
                **prog,
            })

    if session.dest is None or len(session.coords) < 2:
        return
    dist = point_to_polyline_distance_m(lat, lon, session.coords)
    now = time.time()

    async def _send_off_route_warning() -> None:
        await websocket.send_json({
            "type": "off_route_warning", "ok": True,
            "route_id": session.route_id,
            "distance_m": round(dist, 2),
            "threshold_m": OFF_ROUTE_THRESHOLD_M,
            "ts": int(time.time()),
        })

    if dist > OFF_ROUTE_THRESHOLD_M:
        was_active = session.off_route_active
        if AI_REROUTE_ENABLED and now >= session.cooldown_until:
            from app.services.ai_agent import (
                build_reroute_context,
                decide_reroute,
            )
            decision = await decide_reroute(
                build_reroute_context(
                    session, lat, lon, hint="off_route",
                    extra={
                        "off_route_distance_m": round(dist, 2),
                        "threshold_m": OFF_ROUTE_THRESHOLD_M,
                    }),
                "off_route",
                app=app,
                redis=redis,
                session=session,
            )
            if decision is not None:
                action = decision.get("action")
                if action == "ignore":
                    session.off_route_active = False
                    session.cooldown_until = now + REROUTE_COOLDOWN_SECONDS
                    logger.info(
                        "[NAV] AI abaikan off-route kurir %s (%s).",
                        session.kurir_id, decision.get("reason"))
                    return
                if action == "apply":
                    session.off_route_active = True
                    session.cooldown_until = now + REROUTE_COOLDOWN_SECONDS
                    if not was_active:
                        await _send_off_route_warning()
                    old_prog = remaining_progress(session, lat, lon)
                    response = await compute_reroute(
                        app, redis, session, lat, lon, traffic=True)
                    if response is not None:
                        session.coords = list(response.route_coordinates)
                        new_eta_s = response.estimated_time_seconds or 0.0
                        await websocket.send_json({
                            "type": "auto_rerouted" if AUTO_REROUTE
                            else "reroute_available",
                            "ok": True,
                            "route_id": session.route_id,
                            "polyline": encode_polyline(
                                response.route_coordinates, 5),
                            "saving_s": round(
                                (old_prog["remaining_time_s"] - new_eta_s)
                                if old_prog else 0.0, 1),
                            "eta_s": round(new_eta_s, 1),
                            "applied": AUTO_REROUTE,
                            "reason": "off_route",
                        })
                    return
                session.off_route_active = True
                session.cooldown_until = now + REROUTE_COOLDOWN_SECONDS
                if not was_active:
                    await _send_off_route_warning()
                logger.info(
                    "[NAV] AI tunda reroute kurir %s (%s).",
                    session.kurir_id, decision.get("reason"))
                return
        if not session.off_route_active:
            session.off_route_active = True
            await _send_off_route_warning()
        if now >= session.cooldown_until:
            session.cooldown_until = now + REROUTE_COOLDOWN_SECONDS
            old_prog = remaining_progress(session, lat, lon)
            response = await compute_reroute(
                app, redis, session, lat, lon, traffic=True)
            if response is not None:
                session.coords = list(response.route_coordinates)
                new_eta_s = response.estimated_time_seconds or 0.0
                await websocket.send_json({
                    "type": "auto_rerouted" if AUTO_REROUTE
                    else "reroute_available",
                    "ok": True,
                    "route_id": session.route_id,
                    "polyline": encode_polyline(
                        response.route_coordinates, 5),
                    "saving_s": round(
                        (old_prog["remaining_time_s"] - new_eta_s)
                        if old_prog else 0.0, 1),
                    "eta_s": round(new_eta_s, 1),
                    "applied": AUTO_REROUTE,
                    "reason": "off_route",
                })
    else:
        session.off_route_active = False


async def _handle_complete_leg(websocket: WebSocket, session: NavSession,
                               redis) -> None:
    """Kurir menyelesaikan satu pengiriman (POD) → pindah ke leg berikutnya."""
    if session.route_id is None:
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Tidak ada rute aktif untuk diselesaikan."})
        return
    nav = await get_nav_route(redis, session.kurir_id)
    if nav is None:
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Snapshot rute tidak ditemukan di Redis."})
        return
    result = advance_leg(session, nav)
    if result is None:
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Snapshot rute tidak cocok / leg tidak valid."})
        return
    if result.get("done"):
        await clear_nav_route(redis, session.kurir_id)
        await websocket.send_json({
            "type": "route_complete", "ok": True,
            "route_id": session.route_id, "ts": int(time.time())})
        return
    await websocket.send_json({
        "type": "ack", "ok": True,
        "action": "complete_leg",
        "route_id": session.route_id,
        "leg_index": result["leg_index"],
        "kind": session.kind,
        "package_id": result.get("package_id"),
        "recipient_name": result.get("recipient_name"),
        "dest": result.get("dest"),
        "polyline": result.get("polyline"),
        "off_route_threshold_m": OFF_ROUTE_THRESHOLD_M,
        "ts": int(time.time()),
    })


@router.get("/ws/navigation/status",
            summary="Status real-time navigation",
            description=(
                "Menampilkan status konfigurasi WebSocket real-time navigation "
                "`WS /api/v1/ws/navigation`.\n\n"
                "- `enabled`: apakah `ENABLE_LIVE_NAVIGATION` aktif (bila False, "
                "koneksi WS ditolak kode 1008).\n"
                "- `off_route_threshold_m`: ambang off-route "
                "(env `OFF_ROUTE_THRESHOLD_M`).\n"
                "- `reroute_cooldown_s`: cooldown auto-reroute "
                "(env `REROUTE_COOLDOWN_SECONDS`).\n"
                "- `auto_reroute`: apakah rute baru langsung diterapkan "
                "(env `AUTO_REROUTE`).\n"
                "- `max_rate_seconds`: batas interval pengiriman posisi "
                "(env `NAV_POS_MAX_RATE_SECONDS`)."))
async def navigation_status(request: Request):
    redis = getattr(request.app.state, "redis", None)
    return {
        "enabled": _live_navigation_enabled(),
        "redis_connected": redis is not None,
        "off_route_threshold_m": OFF_ROUTE_THRESHOLD_M,
        "reroute_cooldown_s": REROUTE_COOLDOWN_SECONDS,
        "auto_reroute": AUTO_REROUTE,
        "max_rate_seconds": _NAV_POS_MAX_RATE_SECONDS,
        "ai": {
            "enabled": bool(ai_agent.AI_REROUTE_ENABLED
                            and ai_agent.GEMINI_API_KEY),
            "backup_key_available": bool(ai_agent.GEMINI_API_KEY_2),
            "circuit_breaker_open": ai_agent._breaker_open(),
            "concurrent_slots": ai_agent._MAX_CONCURRENT,
            "timeout_s": ai_agent.GEMINI_REROUTE_TIMEOUT_S,
        },
    }


@router.websocket("/ws/navigation")
async def navigation_ws(websocket: WebSocket):
    if not _live_navigation_enabled():
        await websocket.close(code=1008, reason="Live navigation disabled")
        return
    await websocket.accept()

    kurir_id = _auth_kurir_id(websocket)
    if kurir_id is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return

    app = websocket.app
    redis = getattr(app.state, "redis", None)
    registry = getattr(app.state, "nav_registry", None)
    if registry is None:
        await websocket.close(code=1011, reason="Navigation unavailable")
        return

    session = NavSession(kurir_id=kurir_id, ws=websocket)
    await registry.set(session)
    logger.info("Kurir %s terhubung ke WS navigation.", kurir_id)

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
                    "type": "ack", "ok": True, "ts": int(time.time())})
                continue
            if mtype == "start_navigation":
                await _handle_start_navigation(
                    websocket, session, redis, msg)
                continue
            if mtype in ("complete_leg", "pod_submitted"):
                await _handle_complete_leg(websocket, session, redis)
                continue
            if mtype != "location_update":
                continue
            now = time.time()
            if now - last_rate_ts < _NAV_POS_MAX_RATE_SECONDS:
                continue
            last_rate_ts = now
            await _handle_location_update(
                websocket, app, session, redis, msg)
    except WebSocketDisconnect:
        logger.info("Kurir %s terputus dari WS navigation.", kurir_id)
    except Exception as exc:
        logger.warning("WS navigation error kurir %s: %s", kurir_id, exc)
    finally:
        await registry.remove(kurir_id)
        try:
            await websocket.close()
        except Exception:
            pass
