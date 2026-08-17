"""Real-time navigation & auto-rerouting logic (per-kurir).

Fondasi:
- `NavSession`: state aktif satu kurir (rute aktif, cooldown, posisi terakhir).
- `NavRegistry`: kumpulan sesi in-memory (1 koneksi/kurir), dipegang
  `app.state.nav_registry`.
- `navigation_worker`: background task yang mengevaluasi ulang traffic tiap
  `TRAFFIC_REROUTE_INTERVAL_SECONDS` untuk semua sesi aktif.

Event WS didefinisikan di `app/api/v1/endpoints/navigation.py`; module ini
hanya berisi logika murni agar mudah diuji.
"""

import asyncio
import logging
import math
import os
import time

from app.services.ai_agent import AI_REROUTE_ENABLED

logger = logging.getLogger("pathfinding")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw in ("1", "true", "True", "TRUE", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


OFF_ROUTE_THRESHOLD_M = _env_float("OFF_ROUTE_THRESHOLD_M", 40.0)
REROUTE_COOLDOWN_SECONDS = _env_float("REROUTE_COOLDOWN_SECONDS", 30.0)
TRAFFIC_REROUTE_INTERVAL_SECONDS = _env_float(
    "TRAFFIC_REROUTE_INTERVAL_SECONDS", 30.0)
TRAFFIC_REROUTE_MIN_SAVING_SECONDS = _env_float(
    "TRAFFIC_REROUTE_MIN_SAVING_SECONDS", 120.0)
AUTO_REROUTE = _env_bool("AUTO_REROUTE", True)
NAV_PROGRESS_MIN_INTERVAL_SECONDS = _env_float(
    "NAV_PROGRESS_MIN_INTERVAL_SECONDS", 3.0)


class NavSession:
    """State real-time navigation untuk satu kurir (in-memory)."""

    def __init__(self, kurir_id: int, ws=None):
        self.kurir_id = kurir_id
        self.ws = ws
        self.route_id: int | None = None
        self.kind: str | None = None          # "single" | "multi"
        self.leg_index: int = 0
        self.coords: list = []                # polyline aktif (decoded, lat,lng)
        self.dest: tuple | None = None
        self.mode: str = "motorcycle"
        self.last_mile: bool = True
        self.total_distance_m: float = 0.0
        self.total_eta_s: float = 0.0
        self.last_position: dict | None = None
        self.off_route_active: bool = False
        self.cooldown_until: float = 0.0
        self.last_traffic_eval: float = 0.0
        self.last_progress_push: float = 0.0
        self.lock = asyncio.Lock()


class NavRegistry:
    """Registry sesi navigation per kurir (1 koneksi/kurir)."""

    def __init__(self):
        self._sessions: dict[int, NavSession] = {}
        self._lock = asyncio.Lock()

    async def get(self, kurir_id: int) -> NavSession | None:
        async with self._lock:
            return self._sessions.get(kurir_id)

    async def set(self, session: NavSession) -> None:
        async with self._lock:
            self._sessions[session.kurir_id] = session

    async def remove(self, kurir_id: int) -> None:
        async with self._lock:
            self._sessions.pop(kurir_id, None)

    async def all(self) -> list[NavSession]:
        async with self._lock:
            return list(self._sessions.values())


def point_to_polyline_distance_m(lat: float, lon: float,
                                 coords: list) -> float:
    """Jarak terdekat titik (lat,lon) ke polyline rute (meter).

    Proyeksi planar ke tiap segmen memakai skala cos(lat) — akurat untuk
    segmen pendek (perkotaan).
    """
    if not coords or len(coords) < 2:
        return float("inf")
    coslat = math.cos(math.radians(lat))
    m_per_deg = 111320.0
    px = lon * coslat * m_per_deg
    py = lat * m_per_deg
    best = float("inf")
    for i in range(len(coords) - 1):
        ax, ay = coords[i][1] * coslat * m_per_deg, coords[i][0] * m_per_deg
        bx, by = coords[i + 1][1] * coslat * m_per_deg, coords[i + 1][0] * m_per_deg
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        if length2 <= 0:
            d2 = (px - ax) ** 2 + (py - ay) ** 2
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
            qx, qy = ax + t * dx, ay + t * dy
            d2 = (px - qx) ** 2 + (py - qy) ** 2
        if d2 < best:
            best = d2
    return math.sqrt(best)


def _haversine(a, b) -> float:
    from app.services.pathfinding.core_a_star import haversine_distance
    return haversine_distance(a, b)


def remaining_progress(session: NavSession, lat: float,
                       lon: float) -> dict | None:
    """Sisa jarak/waktu + persen progres dari posisi ke ujung polyline aktif."""
    coords = session.coords
    if not coords or len(coords) < 2:
        return None
    total = 0.0
    for i in range(len(coords) - 1):
        total += _haversine(coords[i], coords[i + 1])
    if total <= 0:
        return None

    nearest = 0
    nearest_d = float("inf")
    for i, c in enumerate(coords):
        d = _haversine((lat, lon), c)
        if d < nearest_d:
            nearest_d = d
            nearest = i
    remaining = nearest_d
    for i in range(nearest, len(coords) - 1):
        remaining += _haversine(coords[i], coords[i + 1])

    progress_pct = max(0.0, min(100.0, (1.0 - remaining / total) * 100.0))
    speed_kmh = 40.0
    if session.last_position and session.last_position.get("speed"):
        try:
            speed_kmh = float(session.last_position["speed"])
        except (TypeError, ValueError):
            speed_kmh = 40.0
    if speed_kmh <= 0:
        speed_kmh = 40.0
    remaining_time_s = remaining / (speed_kmh / 3.6)
    return {
        "remaining_distance_m": round(remaining, 1),
        "remaining_time_s": round(remaining_time_s, 1),
        "progress_pct": round(progress_pct, 1),
    }


async def compute_reroute(app, redis, session: NavSession,
                          lat: float, lon: float,
                          traffic: bool = True):
    """Hitung ulang rute dari posisi kini ke dest aktif.

    Menggunakan ulang pipeline pathfinding yang sudah ada (`_resolve_plan` +
    `_best_route`) sehingga memakai cache graf & rute. Kembalikan
    `RouteResponse` atau `None` bila gagal (area tidak tercakup / rute tak ada).
    """
    from app.api.v1.endpoints.pathfinding import (
        _best_route,
        _normalize_mode,
        _resolve_plan,
    )
    from app.schemas.pathfinding import Coordinate, RouteRequest
    from app.services.pathfinding.graph_loader import AreaNotCoveredError
    from starlette.concurrency import run_in_threadpool

    dest = session.dest
    if dest is None:
        return None
    mode = _normalize_mode(
        type("_P", (), {"mode": session.mode})())
    try:
        plan = await run_in_threadpool(
            _resolve_plan, app, lat, lon, dest[0], dest[1])
    except AreaNotCoveredError as exc:
        logger.info("[NAV] Reroute area tak tercakup: %s", exc)
        return None
    except Exception as exc:
        logger.warning("[NAV] Gagal resolve plan saat reroute: %s", exc)
        return None

    leg_payload = RouteRequest(
        origin=Coordinate(latitude=lat, longitude=lon),
        destination=Coordinate(latitude=dest[0], longitude=dest[1]),
        mode=mode,
        last_mile_precision=session.last_mile,
        dynamic_rerouting=True,
    )
    traffic_penalties = {}
    if traffic:
        try:
            from app.services.traffic.smart_hybrid import get_request_penalties
            traffic_penalties = await get_request_penalties(
                app, redis, (lat, lon), dest)
        except Exception as exc:
            logger.warning("[NAV] Gagal ambil penalti traffic: %s", exc)
        # Penalti berita publik (RAG) di koridor posisi-kini -> dest.
        try:
            from app.services import rag_traffic
            if rag_traffic.rag_enabled():
                news_penalties = await rag_traffic.retrieve_and_evaluate_road_incidents(
                    [(lat, lon), (dest[0], dest[1])], app=app)
                for eid, mult in news_penalties.items():
                    traffic_penalties[eid] = max(
                        traffic_penalties.get(eid, 1.0), mult)
        except Exception as exc:
            logger.warning("[NAV] Gagal ambil penalti berita (RAG): %s", exc)
    try:
        response, _node_sequence, _final_penalties, _m = await _best_route(
            app, plan, redis, traffic_penalties, leg_payload, mode,
            lat, lon, dest[0], dest[1],
            need_nodes=True, skip_traffic=not traffic)
    except Exception as exc:
        logger.warning("[NAV] Reroute gagal: %s", exc)
        return None
    if response is None:
        return None
    return response


def advance_leg(session: NavSession, nav: dict) -> dict | None:
    """Pindahkan navigasi ke leg berikutnya dari snapshot rute multi-leg.

    Dipanggil saat kurir menyelesaikan satu pengiriman (POD) via WS
    `complete_leg` / `pod_submitted`. Mengubah state session (polyline aktif,
    dest, leg_index) ke leg berikutnya.

    Kembalikan:
    - `{"done": True}` bila semua leg selesai (atau rute single-leg).
    - dict `{"leg_index", "package_id", "recipient_name", "dest", "polyline"}`
      untuk leg baru.
    - `None` bila snapshot tidak cocok (`route_id`) / leg tidak valid.
    """
    if nav.get("route_id") != session.route_id:
        return None
    legs = nav.get("legs") or []
    if not legs:
        return None
    if session.kind != "multi":
        return {"done": True}
    next_index = session.leg_index + 1
    if next_index >= len(legs):
        return {"done": True}
    leg = legs[next_index]

    from app.services.polyline import decode_polyline

    coords = decode_polyline(leg.get("encoded") or "")
    if len(coords) < 2:
        return None
    dest = leg.get("dest")
    if dest is None:
        return None
    session.leg_index = next_index
    session.coords = coords
    session.dest = (float(dest[0]), float(dest[1]))
    session.off_route_active = False
    session.cooldown_until = 0.0
    session.last_progress_push = 0.0
    return {
        "leg_index": next_index,
        "package_id": leg.get("package_id"),
        "recipient_name": leg.get("recipient_name"),
        "dest": [float(dest[0]), float(dest[1])],
        "polyline": leg.get("encoded"),
    }


def _should_traffic_reroute(session: NavSession, new_eta_s: float) -> bool:
    """Reroute traffic hanya bila rute baru lebih cepat >= minimum saving."""
    remaining = remaining_progress(
        session,
        session.last_position.get("lat") if session.last_position else 0.0,
        session.last_position.get("lon") if session.last_position else 0.0,
    )
    if remaining is None:
        return False
    cur = remaining["remaining_time_s"]
    return (cur - new_eta_s) >= TRAFFIC_REROUTE_MIN_SAVING_SECONDS


async def navigation_worker(app) -> None:
    """Background: evaluasi ulang traffic tiap interval untuk sesi aktif.

    Ikut pola `traffic_poller` (app/services/traffic/poller.py). Cooldown
    `REROUTE_COOLDOWN_SECONDS` mencegah route flickering.
    """
    logger.info("[NAV] Worker dimulai (interval %.0fs).",
                TRAFFIC_REROUTE_INTERVAL_SECONDS)
    while True:
        try:
            registry = getattr(app.state, "nav_registry", None)
            if registry is not None:
                for session in await registry.all():
                    await _evaluate_session(app, session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[NAV] Siklus worker gagal: %s", exc)
        await asyncio.sleep(TRAFFIC_REROUTE_INTERVAL_SECONDS)


async def _evaluate_session(app, session: NavSession) -> None:
    from app.services.polyline import encode_polyline

    now = time.time()
    if session.route_id is None or session.dest is None:
        return
    if now < session.cooldown_until:
        return
    if now - session.last_traffic_eval < TRAFFIC_REROUTE_INTERVAL_SECONDS:
        return
    session.last_traffic_eval = now
    if not session.last_position:
        return
    pos = session.last_position
    response = await compute_reroute(
        app, getattr(app.state, "redis", None), session,
        float(pos["lat"]), float(pos["lon"]), traffic=True)
    if response is None:
        return
    new_eta_s = response.estimated_time_seconds or 0.0
    old_prog = remaining_progress(
        session, float(pos["lat"]), float(pos["lon"]))

    reason = None
    if AI_REROUTE_ENABLED:
        from app.services.ai_agent import (
            build_reroute_context,
            decide_reroute,
        )
        decision = await decide_reroute(
            build_reroute_context(
                session, float(pos["lat"]), float(pos["lon"]),
                hint="traffic",
                extra={
                    "candidate": {
                        "eta_s": new_eta_s,
                        "saving_s": round(
                            (old_prog["remaining_time_s"] - new_eta_s)
                            if old_prog else 0.0, 1),
                    },
                }),
            "traffic",
            app=app,
            redis=getattr(app.state, "redis", None),
            session=session,
        )
        if decision is not None:
            if decision.get("action") != "apply":
                logger.info(
                    "[NAV] AI tolak reroute traffic kurir %s (%s).",
                    session.kurir_id, decision.get("reason"))
                return
            reason = decision.get("reason") or "ai_decision"
        elif not _should_traffic_reroute(session, new_eta_s):
            return
    elif not _should_traffic_reroute(session, new_eta_s):
        return

    session.cooldown_until = now + REROUTE_COOLDOWN_SECONDS
    session.coords = list(response.route_coordinates)
    session.dest = (session.dest[0], session.dest[1])
    event = {
        "type": "auto_rerouted" if AUTO_REROUTE else "reroute_available",
        "route_id": session.route_id,
        "polyline": encode_polyline(response.route_coordinates, 5),
        "saving_s": round(
            (old_prog["remaining_time_s"] - new_eta_s)
            if old_prog else 0.0, 1),
        "eta_s": round(new_eta_s, 1),
        "applied": AUTO_REROUTE,
    }
    if reason:
        event["reason"] = reason
    if session.ws is not None:
        try:
            await session.ws.send_json(event)
            logger.info(
                "[NAV] Kurir %s %s (hemat %.0fs, cooldown %.0fs).",
                session.kurir_id, event["type"], event["saving_s"],
                REROUTE_COOLDOWN_SECONDS)
        except Exception as exc:
            logger.warning("[NAV] Kirim event kurir %s gagal: %s",
                           session.kurir_id, exc)
