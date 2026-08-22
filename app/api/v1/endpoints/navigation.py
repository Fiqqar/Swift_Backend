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
import os
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger, ws_correlation_id
from app.core.metrics import (
    nav_active_sessions,
    nav_off_route_warnings_total,
    nav_reroute_duration_seconds,
    nav_reroutes_total,
    nav_ws_connections_total,
)
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

logger = get_logger("navigation")

_NAV_POS_MAX_RATE_SECONDS = float(
    os.environ.get("NAV_POS_MAX_RATE_SECONDS", "3"))

_NAV_TURN_NOTIFY_DISTANCE_M = float(
    os.environ.get("NAV_TURN_NOTIFY_DISTANCE_M", "150"))


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
        logger.warning("[NAV] _apply_route: legs kosong (route_id=%s)", nav.get("route_id"))
        return False
    leg_index = max(0, min(leg_index, len(legs) - 1))
    leg = legs[leg_index]
    from app.services.polyline import decode_polyline
    coords = decode_polyline(leg.get("encoded") or "")
    if len(coords) < 2:
        logger.warning("[NAV] _apply_route: coords < 2 (leg_index=%s, encoded_len=%s)",
                       leg_index, len(leg.get("encoded") or ""))
        return False
    dest = leg.get("dest")
    if dest is None:
        logger.warning("[NAV] _apply_route: dest is None (leg_index=%s)", leg_index)
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

    # NEW: Leg context for multi-stop
    session.current_package_id = leg.get("package_id")
    session.current_recipient = leg.get("recipient_name")
    session.total_legs = len(legs)

    # Reset navigation state for new leg
    session.traveled_distance_m = 0.0
    session.current_step_index = 0
    session.node_sequence = []
    session.edge_classes = {}
    session.edge_names = {}
    session.steps = []

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
    if nav is None:
        logger.warning("[NAV] Kurir %s: snapshot rute tidak ditemukan di Redis (route_id=%s, key=driver:nav:%s)",
                       session.kurir_id, route_id, session.kurir_id)
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Route snapshot tidak ditemukan. Hitung ulang rute via API terlebih dahulu."})
        return
    if nav.get("route_id") != route_id:
        logger.warning("[NAV] Kurir %s: route_id mismatch (client=%s, redis=%s)",
                       session.kurir_id, route_id, nav.get("route_id"))
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Route snapshot tidak cocok. Hitung ulang rute via API terlebih dahulu."})
        return
    if not await _apply_route(session, nav, leg_index):
        logger.warning("[NAV] Kurir %s: _apply_route gagal (leg_index=%s, legs=%s)",
                       session.kurir_id, leg_index, len(nav.get("legs") or []))
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": "Polyline leg tidak valid pada snapshot."})
        return
    await websocket.send_json({
        "type": "ack", "ok": True,
        "route_id": session.route_id,
        "leg_index": session.leg_index,
        "kind": session.kind,
        "total_distance_m": session.total_distance_m,
        "total_eta_s": session.total_eta_s,
        "total_legs": session.total_legs,
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

    # Validasi current_route_id agar client tidak kirim posisi untuk rute yang salah
    current_route_id = msg.get("current_route_id")
    if current_route_id is not None:
        try:
            current_route_id = int(current_route_id)
        except (TypeError, ValueError):
            current_route_id = None
    if current_route_id is not None and current_route_id != session.route_id:
        await websocket.send_json({
            "type": "error", "ok": False,
            "detail": f"current_route_id tidak cocok: client={current_route_id}, server={session.route_id}"})
        logger.warning("[NAV] Kurir %s kirim current_route_id mismatch: client=%s, server=%s",
                       session.kurir_id, current_route_id, session.route_id)
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

            # NEW: Send turn_by_turn if approaching a maneuver
            next_maneuver = prog.get("next_maneuver")
            if next_maneuver and next_maneuver.get("distance_m", 0) <= _NAV_TURN_NOTIFY_DISTANCE_M:
                await websocket.send_json({
                    "type": "turn_by_turn", "ok": True,
                    "route_id": session.route_id,
                    "leg_index": session.leg_index,
                    "maneuver": next_maneuver,
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
        nav_off_route_warnings_total.inc()
        logger.warning("nav.off_route.warning", kurir_id=session.kurir_id, distance_m=round(dist, 2), threshold_m=OFF_ROUTE_THRESHOLD_M)
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
                    logger.info("nav.reroute.ai_ignored", kurir_id=session.kurir_id, reason=decision.get("reason"))
                    return
                if action == "apply":
                    session.off_route_active = True
                    session.cooldown_until = now + REROUTE_COOLDOWN_SECONDS
                    if not was_active:
                        await _send_off_route_warning()
                    old_prog = remaining_progress(session, lat, lon)
                    reroute_start = time.time()
                    response = await compute_reroute(
                        app, redis, session, lat, lon, traffic=True)
                    if response is not None:
                        session.coords = list(response.route_coordinates)
                        new_eta_s = response.estimated_time_seconds or 0.0
                        reroute_duration = time.time() - reroute_start
                        nav_reroute_duration_seconds.labels(type="off_route").observe(reroute_duration)
                        nav_reroutes_total.labels(type="off_route", applied=str(AUTO_REROUTE).lower()).inc()
                        await websocket.send_json({
                            "type": "auto_rerouted" if AUTO_REROUTE
                            else "reroute_available",
                            "ok": True,
                            "route_id": session.route_id,
                            "leg_index": session.leg_index,
                            "polyline": encode_polyline(
                                response.route_coordinates, 5),
                            "saving_s": round(
                                (old_prog["remaining_time_s"] - new_eta_s)
                                if old_prog else 0.0, 1),
                            "eta_s": round(new_eta_s, 1),
                            "applied": AUTO_REROUTE,
                            "reason": "off_route",
                            "steps": session.steps,
                        })
                    return
                session.off_route_active = True
                session.cooldown_until = now + REROUTE_COOLDOWN_SECONDS
                if not was_active:
                    await _send_off_route_warning()
                logger.info("nav.reroute.ai_deferred", kurir_id=session.kurir_id, reason=decision.get("reason"))
                return
        if not session.off_route_active:
            session.off_route_active = True
            await _send_off_route_warning()
        if now >= session.cooldown_until:
            session.cooldown_until = now + REROUTE_COOLDOWN_SECONDS
            old_prog = remaining_progress(session, lat, lon)
            reroute_start = time.time()
            response = await compute_reroute(
                app, redis, session, lat, lon, traffic=True)
            if response is not None:
                session.coords = list(response.route_coordinates)
                new_eta_s = response.estimated_time_seconds or 0.0
                reroute_duration = time.time() - reroute_start
                nav_reroute_duration_seconds.labels(type="off_route").observe(reroute_duration)
                nav_reroutes_total.labels(type="off_route", applied=str(AUTO_REROUTE).lower()).inc()
                await websocket.send_json({
                    "type": "auto_rerouted" if AUTO_REROUTE
                    else "reroute_available",
                    "ok": True,
                    "route_id": session.route_id,
                    "leg_index": session.leg_index,
                    "polyline": encode_polyline(
                        response.route_coordinates, 5),
                    "saving_s": round(
                        (old_prog["remaining_time_s"] - new_eta_s)
                        if old_prog else 0.0, 1),
                    "eta_s": round(new_eta_s, 1),
                    "applied": AUTO_REROUTE,
                    "reason": "off_route",
                    "steps": session.steps,
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
        logger.info("nav.complete", kurir_id=session.kurir_id, route_id=session.route_id)
        await websocket.send_json({
            "type": "route_complete", "ok": True,
            "route_id": session.route_id, "ts": int(time.time())})
        return
    logger.info("nav.leg.completed", kurir_id=session.kurir_id, leg_index=result["leg_index"], package_id=result.get("package_id"), recipient=result.get("recipient_name"))
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
        "total_distance_m": session.total_distance_m,
        "total_eta_s": session.total_eta_s,
        "total_legs": session.total_legs,
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
                "(env `NAV_POS_MAX_RATE_SECONDS`).\n"
                "- `turn_notify_distance_m`: jarak notifikasi belok "
                "(env `NAV_TURN_NOTIFY_DISTANCE_M`)."))
async def navigation_status(request: Request):
    redis = getattr(request.app.state, "redis", None)
    registry = getattr(request.app.state, "nav_registry", None)
    active_sessions = 0
    if registry is not None:
        try:
            active_sessions = len(await registry.all())
        except Exception:
            pass
    return {
        "enabled": _live_navigation_enabled(),
        "redis_connected": redis is not None,
        "active_sessions": active_sessions,
        "off_route_threshold_m": OFF_ROUTE_THRESHOLD_M,
        "reroute_cooldown_s": REROUTE_COOLDOWN_SECONDS,
        "auto_reroute": AUTO_REROUTE,
        "max_rate_seconds": _NAV_POS_MAX_RATE_SECONDS,
        "turn_notify_distance_m": _NAV_TURN_NOTIFY_DISTANCE_M,
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

    # Set correlation ID for this WS connection
    cid = ws_correlation_id(websocket)
    
    kurir_id = _auth_kurir_id(websocket)
    if kurir_id is None:
        nav_ws_connections_total.labels(status="error").inc()
        await websocket.close(code=4401, reason="Unauthorized")
        return

    app = websocket.app
    redis = getattr(app.state, "redis", None)
    registry = getattr(app.state, "nav_registry", None)
    if registry is None:
        nav_ws_connections_total.labels(status="error").inc()
        await websocket.close(code=1011, reason="Navigation unavailable")
        return

    session = NavSession(kurir_id=kurir_id, ws=websocket)
    await registry.set(session)
    
    nav_active_sessions.inc()
    nav_ws_connections_total.labels(status="connected").inc()
    
    logger.info("nav.ws.connected", kurir_id=kurir_id, correlation_id=cid)
    
    connect_time = time.time()
    messages_rx = 0
    last_rate_ts = 0.0
    try:
        while True:
            raw = await websocket.receive_text()
            messages_rx += 1
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
        duration = time.time() - connect_time
        nav_ws_connections_total.labels(status="disconnected").inc()
        logger.info("nav.ws.disconnected", kurir_id=kurir_id, duration_s=round(duration, 2), messages_rx=messages_rx, correlation_id=cid)
    except Exception as exc:
        nav_ws_connections_total.labels(status="error").inc()
        logger.warning("nav.ws.error", kurir_id=kurir_id, error=str(exc), correlation_id=cid)
    finally:
        nav_active_sessions.dec()
        await registry.remove(kurir_id)
        try:
            await websocket.close()
        except Exception:
            pass
