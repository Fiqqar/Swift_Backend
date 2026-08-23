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
import math
import os
import time

from app.core.logging import get_logger
from app.services.ai_agent import AI_REROUTE_ENABLED
from app.services.polyline import encode_polyline

logger = get_logger("navigation")


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

# Dynamic Stop Re-Ordering on Off-Route
OFF_ROUTE_REORDER_THRESHOLD_M = _env_float("OFF_ROUTE_REORDER_THRESHOLD_M", 50.0)
REORDER_MIN_DISTANCE_SAVING_PCT = _env_float("REORDER_MIN_DISTANCE_SAVING_PCT", 20.0)
REORDER_COOLDOWN_SECONDS = _env_float("REORDER_COOLDOWN_SECONDS", 60.0)
MAX_REORDERS_PER_ROUTE = int(os.environ.get("MAX_REORDERS_PER_ROUTE", "3"))
REORDER_STABILITY_WINDOW = int(os.environ.get("REORDER_STABILITY_WINDOW", "2"))

# Major Off-Route: switch active stop threshold
OFF_ROUTE_MAJOR_THRESHOLD_M = _env_float("OFF_ROUTE_MAJOR_THRESHOLD_M", 300.0)
REORDER_ACTIVE_SWITCH_DISTANCE_PCT = _env_float("REORDER_ACTIVE_SWITCH_DISTANCE_PCT", 40.0)


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

        # NEW: Navigation enhancement fields
        self.node_sequence: list[int] = []    # Current route node IDs
        self.edge_classes: dict[int, str] = {}  # Highway type per edge
        self.edge_names: dict[int, str] = {}    # Street names per edge
        self.steps: list = []                 # Pre-computed turn steps
        self.current_step_index: int = 0      # Current step in navigation
        self.traveled_distance_m: float = 0.0 # Distance traveled from route start
        self.current_package_id: int | None = None
        self.current_recipient: str | None = None
        self.total_legs: int = 1

        # Dynamic Stop Re-Ordering state
        self.reorder_cooldown_until: float = 0.0
        self.reorder_count: int = 0
        self._reorder_candidate: list[int] | None = None
        self._reorder_candidate_count: int = 0


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
        best = min(best, d2)
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

    # NEW: Calculate traveled distance and average speed
    traveled = total - remaining
    session.traveled_distance_m = traveled

    # Estimate average speed from start
    avg_speed_kmh = speed_kmh
    if session.last_position and "ts" in session.last_position:
        # Could compute from start time, but we don't have it
        avg_speed_kmh = speed_kmh

    # NEW: Determine current step and next maneuver
    from app.services.pathfinding.maneuvers import (
        find_current_step,
        get_next_maneuver,
        get_traffic_level,
    )

    current_step_idx = find_current_step(session.steps, lat, lon, traveled)
    session.current_step_index = current_step_idx
    next_maneuver = get_next_maneuver(session.steps, current_step_idx)

    # NEW: Traffic level on current edge
    traffic_level = "free"
    if (session.steps and current_step_idx < len(session.steps)
            and session.node_sequence and current_step_idx < len(session.node_sequence) - 1):
        u, v = session.node_sequence[current_step_idx], session.node_sequence[current_step_idx + 1]
        from app.services.pathfinding.core_a_star import edge_id
        eid = edge_id(u, v)
        traffic_level = get_traffic_level({}, eid)  # penalties passed in future

    # NEW: ETA timestamp
    import time
    eta_timestamp = int(time.time() + remaining_time_s)

    result = {
        "remaining_distance_m": round(remaining, 1),
        "remaining_time_s": round(remaining_time_s, 1),
        "progress_pct": round(progress_pct, 1),
        "current_speed_kmh": round(speed_kmh, 1),
        "average_speed_kmh": round(avg_speed_kmh, 1),
        "eta_timestamp": eta_timestamp,
        "traffic_level": traffic_level,
    }

    # NEW: Leg context for multi-stop
    if session.kind == "multi":
        result["leg_context"] = {
            "package_id": session.current_package_id,
            "recipient_name": session.current_recipient,
            "stop_sequence": session.leg_index + 1,
            "total_legs": session.total_legs,
        }

    # NEW: Next maneuver if available
    if next_maneuver:
        result["next_maneuver"] = next_maneuver

    return result


async def compute_reroute(app, redis, session: NavSession,
                          lat: float, lon: float,
                          traffic: bool = True,
                          extra_penalties: dict[int, float] | None = None,
                          reroute_type: str = "traffic"):
    """Hitung ulang rute dari posisi kini ke dest aktif.

    Menggunakan ulang pipeline pathfinding yang sudah ada (`_resolve_plan` +
    `_best_route`) sehingga memakai cache graf & rute. Kembalikan
    `RouteResponse` atau `None` bila gagal (area tidak tercakup / rute tak ada).

    `extra_penalties` (opsional): dict {edge_id: multiplier} yang di-merge
    (ambil max) ke penalti traffic — dipakai mis. laporan kurir (insiden jalan).
    """
    from starlette.concurrency import run_in_threadpool

    from app.api.v1.endpoints.pathfinding import (
        _best_route,
        _normalize_mode,
        _resolve_plan,
    )
    from app.schemas.pathfinding import Coordinate, RouteRequest
    from app.services.pathfinding.graph_loader import AreaNotCoveredError
    from app.services.pathfinding.maneuvers import extract_steps

    dest = session.dest
    if dest is None:
        return None
    mode = _normalize_mode(
        type("_P", (), {"mode": session.mode})())
    try:
        plan = await run_in_threadpool(
            _resolve_plan, app, lat, lon, dest[0], dest[1])
    except AreaNotCoveredError as exc:
        logger.info("nav.reroute.area_not_covered", kurir_id=session.kurir_id, error=str(exc))
        return None
    except Exception as exc:
        logger.warning("nav.reroute.plan_failed", kurir_id=session.kurir_id, error=str(exc))
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
        # Penalti berita publik (RAG) di koridor rute aktif -> dest.
        try:
            from app.services import rag_traffic
            if rag_traffic.rag_enabled():
                corridor = session.coords if (
                    session.coords and len(session.coords) >= 2
                ) else [(lat, lon), (dest[0], dest[1])]
                news_penalties = await rag_traffic.retrieve_and_evaluate_road_incidents(
                    corridor, app=app, redis=redis)
                for eid, mult in news_penalties.items():
                    traffic_penalties[eid] = max(
                        traffic_penalties.get(eid, 1.0), mult)
        except Exception as exc:
            logger.warning("[NAV] Gagal ambil penalti berita (RAG): %s", exc)
        if extra_penalties:
            for eid, mult in extra_penalties.items():
                traffic_penalties[eid] = max(
                    traffic_penalties.get(eid, 1.0), mult)
    try:
        response, node_sequence, final_penalties, _m = await _best_route(
            app, plan, redis, traffic_penalties, leg_payload, mode,
            lat, lon, dest[0], dest[1],
            need_nodes=True, skip_traffic=not traffic)
    except Exception as exc:
        logger.warning("[NAV] Reroute gagal: %s", exc)
        return None
    if response is None:
        return None

    # NEW: Populate session with navigation data for turn-by-turn
    if node_sequence:
        session.node_sequence = node_sequence
        # Get edge classes and names from the graph used
        pg = plan[1]
        if hasattr(pg, 'edge_classes'):
            session.edge_classes = pg.edge_classes or {}
        if hasattr(pg, 'edge_names'):
            session.edge_names = pg.edge_names or {}
        # Extract turn-by-turn steps
        speed_kmh = 40.0
        if session.last_position and session.last_position.get("speed"):
            try:
                speed_kmh = float(session.last_position["speed"])
            except (TypeError, ValueError):
                speed_kmh = 40.0
        if speed_kmh <= 0:
            speed_kmh = 40.0
        session.steps = extract_steps(
            node_sequence,
            response.route_coordinates,
            session.edge_classes,
            session.edge_names,
            speed_kmh,
            final_penalties if traffic else None
        )
        session.current_step_index = 0
        session.traveled_distance_m = 0.0

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

    # NEW: Reset navigation state for new leg
    session.current_package_id = leg.get("package_id")
    session.current_recipient = leg.get("recipient_name")
    session.node_sequence = []
    session.edge_classes = {}
    session.edge_names = {}
    session.steps = []
    session.current_step_index = 0
    session.traveled_distance_m = 0.0

    return {
        "leg_index": next_index,
        "package_id": leg.get("package_id"),
        "recipient_name": leg.get("recipient_name"),
        "dest": [float(dest[0]), float(dest[1])],
        "polyline": leg.get("encoded"),
    }


async def maybe_reorder_stops_on_off_route(
    app, redis, session: NavSession,
    current_lat: float, current_lon: float,
    off_route_distance_m: float,
    test_mode: bool = False
) -> bool:
    """
    Evaluate and potentially re-order remaining stops when courier goes off-route.
    
    Uses cached road distances (no fresh traffic) for speed.
    Maintains EXPRESS priority. Switches active polyline if current stop changes.
    
    Returns True if stops were re-ordered and new route sent to client.
    """
    # 1. Only for multi-leg routes with remaining stops
    if session.kind != "multi" or session.leg_index >= session.total_legs - 1:
        return False
    
    # Test mode thresholds (bypass distance checks for easy testing)
    reorder_threshold = -1.0 if test_mode else OFF_ROUTE_REORDER_THRESHOLD_M
    major_threshold = 80.0 if test_mode else OFF_ROUTE_MAJOR_THRESHOLD_M
    active_switch_pct = 20.0 if test_mode else REORDER_ACTIVE_SWITCH_DISTANCE_PCT
    min_saving_pct = 20.0 if test_mode else REORDER_MIN_DISTANCE_SAVING_PCT
    
    # 1. Only for multi-leg routes with remaining stops
    if session.kind != "multi" or session.leg_index >= session.total_legs - 1:
        return False
    
    # 2. Check off-route threshold for re-ordering (higher than warning threshold)
    if off_route_distance_m <= reorder_threshold:
        return False
    
    # 3. Cooldown check (bypass in test mode)
    now = time.time()
    if not test_mode and now < session.reorder_cooldown_until:
        logger.info(
            "[NAV] Re-order rejected: cooldown active (%.0fs remaining)",
            session.reorder_cooldown_until - now)
        return False
    
    # 4. Max re-orders per route limit
    if session.reorder_count >= MAX_REORDERS_PER_ROUTE:
        logger.info(
            "[NAV] Re-order rejected: max reorders reached (%d/%d)",
            session.reorder_count, MAX_REORDERS_PER_ROUTE)
        return False
    
    # 5. Get remaining stops from Redis snapshot
    from app.services.tracking import get_nav_route
    nav = await get_nav_route(redis, session.kurir_id)
    if not nav:
        return False
    legs = nav.get("legs", [])
    remaining_legs = legs[session.leg_index + 1:]
    if len(remaining_legs) < 2:  # Need at least 2 to reorder
        return False
    
    # 6. Prepare stops data for optimizer
    stops_coords = [(leg["dest"][0], leg["dest"][1]) for leg in remaining_legs]
    service_types = [leg.get("service_type", "REGULAR") for leg in remaining_legs]
    package_ids = [leg["package_id"] for leg in remaining_legs]
    recipient_names = [leg["recipient_name"] for leg in remaining_legs]
    
    # 7. Use cached road distance function for ordering (no fresh traffic)
    from app.api.v1.endpoints.pathfinding import _ordering_road_distance
    
    mode = session.mode
    last_mile = session.last_mile
    
    async def road_cost_fn(o, d):
        return await _ordering_road_distance(
            app, redis, mode, last_mile, o[0], o[1], d[0], d[1])
    
    # 8. Run hybrid optimizer with current position as start
    from app.services.pathfinding.delivery_optimizer import optimize_stop_order_hybrid
    
    try:
        new_order = await optimize_stop_order_hybrid(
            (current_lat, current_lon), stops_coords,
            service_types=service_types,
            road_cost_fn=road_cost_fn,
            top_k=3,
            return_to_hub=False,
        )
    except Exception as exc:
        logger.warning("[NAV] Re-order optimizer failed: %s", exc)
        return False
    
    # 9. Check if order actually changed
    current_order = list(range(len(remaining_legs)))
    if new_order == current_order:
        # Reset stability window
        session._reorder_candidate = None
        session._reorder_candidate_count = 0
        if not test_mode:
            return False
        # In test_mode: continue to force reorder to nearest stop
    
    # 10. Stability window / anti-flicker: require N consecutive same candidate (bypass in test mode)
    if not test_mode:
        if session._reorder_candidate == new_order:
            session._reorder_candidate_count += 1
        else:
            session._reorder_candidate = new_order
            session._reorder_candidate_count = 1
        
        if session._reorder_candidate_count < REORDER_STABILITY_WINDOW:
            logger.info(
                "[NAV] Re-order rejected: stability window not met (%d/%d, candidate=%s)",
                session._reorder_candidate_count, REORDER_STABILITY_WINDOW, new_order)
            return False
    else:
        # Test mode: reset stability counter, allow immediate re-order
        session._reorder_candidate = new_order
        session._reorder_candidate_count = 1
    
    # 11. Verify distance savings >= threshold (lower in test mode)
    # Compute total road distance for current order vs new order
    async def _total_distance_for_order(order_indices: list[int]) -> float:
        total = 0.0
        prev = (current_lat, current_lon)
        for idx in order_indices:
            dest_coord = stops_coords[idx]
            cost = await road_cost_fn(prev, dest_coord)
            total += cost
            prev = dest_coord
        return total
    
    try:
        old_total_dist = await _total_distance_for_order(current_order)
        new_total_dist = await _total_distance_for_order(new_order)
    except Exception as exc:
        logger.warning("[NAV] Distance comparison failed: %s", exc)
        return False
    
    # Calculate distance savings percentage
    saving_pct = ((old_total_dist - new_total_dist) / old_total_dist * 100) if old_total_dist > 0 else 0.0
    
    # 12. Determine re-order type: standard (remaining only) vs major (switch active stop)
    # Calculate distance to current active stop
    current_active_dest = session.dest
    dist_to_current_active = float('inf')
    if current_active_dest:
        from app.services.pathfinding.core_a_star import haversine_distance
        dist_to_current_active = haversine_distance(
            (current_lat, current_lon), current_active_dest)
    
    # Distance to new first stop after re-order
    new_first_stop_coord = stops_coords[new_order[0]]
    dist_to_new_first = haversine_distance(
        (current_lat, current_lon), new_first_stop_coord)
    
    # 12. Determine re-order type: standard (remaining only) vs major (switch active stop)
    # Calculate distance to current active stop
    current_active_dest = session.dest
    dist_to_current_active = float('inf')
    if current_active_dest:
        from app.services.pathfinding.core_a_star import haversine_distance
        dist_to_current_active = haversine_distance(
            (current_lat, current_lon), current_active_dest)
    
    # In test_mode: prioritize EXPRESS packages, then REGULAR
    if test_mode:
        # PRIORITY LOGIC: Check for EXPRESS packages first
        express_indices = [i for i, st in enumerate(service_types) if st == "EXPRESS"]
        regular_indices = [i for i, st in enumerate(service_types) if st != "EXPRESS"]
        
        if express_indices:
            # Find nearest EXPRESS package by haversine distance from click position
            min_dist = float('inf')
            nearest_stop_idx = -1
            for idx in express_indices:
                d = haversine_distance((current_lat, current_lon), stops_coords[idx])
                if d < min_dist:
                    min_dist = d
                    nearest_stop_idx = idx
            logger.info(
                "[NAV] Test mode: EXPRESS priority - selected nearest EXPRESS stop index=%d (pkg=%s, dist=%.0fm)",
                nearest_stop_idx, package_ids[nearest_stop_idx] if nearest_stop_idx < len(package_ids) else "N/A", min_dist)
        elif regular_indices:
            # No EXPRESS remaining, find nearest REGULAR package
            min_dist = float('inf')
            nearest_stop_idx = -1
            for idx in regular_indices:
                d = haversine_distance((current_lat, current_lon), stops_coords[idx])
                if d < min_dist:
                    min_dist = d
                    nearest_stop_idx = idx
            logger.info(
                "[NAV] Test mode: No EXPRESS remaining - selected nearest REGULAR stop index=%d (pkg=%s, dist=%.0fm)",
                nearest_stop_idx, package_ids[nearest_stop_idx] if nearest_stop_idx < len(package_ids) else "N/A", min_dist)
        else:
            nearest_stop_idx = -1
            min_dist = float('inf')
        
        # Force reorder to put nearest stop first
        if nearest_stop_idx != -1:
            new_order = [nearest_stop_idx] + [i for i in range(len(remaining_legs)) if i != nearest_stop_idx]
            logger.info("[NAV] Test mode: re-ordered new_order to put nearest first: %s", new_order)
        
        # Force major reorder to switch active leg ONLY if nearest stop differs from current active
        do_major_reorder = (nearest_stop_idx != 0)
        first_stop_changed = (nearest_stop_idx != 0)
        
        # Log test mode decision
        closer_pct = ((dist_to_current_active - min_dist) / dist_to_current_active * 100) if dist_to_current_active > 0 else 0
        logger.info(
            "[NAV] Test mode evaluation: test_mode=%s, off_route=%.0fm, nearest_stop_idx=%d, "
            "dist_to_nearest=%.0fm, dist_to_current=%.0fm, do_major=%s",
            test_mode, off_route_distance_m, nearest_stop_idx, min_dist, dist_to_current_active, do_major_reorder)
    else:
        # Normal mode: use existing logic
        major_threshold = major_threshold if test_mode else OFF_ROUTE_MAJOR_THRESHOLD_M
        active_switch_pct = active_switch_pct if test_mode else REORDER_ACTIVE_SWITCH_DISTANCE_PCT
        
        is_major_off_route = off_route_distance_m > major_threshold
        is_new_stop_significantly_closer = (
            dist_to_current_active > 0 and 
            dist_to_new_first < dist_to_current_active * (1.0 - active_switch_pct / 100.0)
        )
        
        do_major_reorder = is_major_off_route or is_new_stop_significantly_closer
        
        # Detailed debug logging with rejection/approval reasons
        closer_pct = ((dist_to_current_active - dist_to_new_first) / dist_to_current_active * 100) if dist_to_current_active > 0 else 0
        stability_info = f"{session._reorder_candidate_count}/{REORDER_STABILITY_WINDOW}" if not test_mode else "bypassed"
        cooldown_active = now < session.reorder_cooldown_until and not test_mode
        
        logger.info(
            "[NAV] Re-order evaluation: test_mode=%s, off_route=%.0fm, dist_current=%.0fm, dist_new=%.0fm, "
            "closer_pct=%.1f%%, stability=%s, cooldown_active=%s, reorder_count=%d, "
            "is_major_off_route=%s, is_closer=%.1f%% (threshold=%.1f%%), do_major=%s",
            test_mode, off_route_distance_m, dist_to_current_active, dist_to_new_first,
            closer_pct, stability_info, cooldown_active, session.reorder_count,
            is_major_off_route, closer_pct, active_switch_pct, do_major_reorder)
        
        # If major re-order, auto-find nearest stop from current position
        nearest_stop_idx = new_order[0]  # default to optimizer's first choice
        if do_major_reorder and test_mode:
            # Find nearest stop by haversine distance from current position
            min_dist = float('inf')
            for idx, coord in enumerate(stops_coords):
                d = haversine_distance((current_lat, current_lon), coord)
                if d < min_dist:
                    min_dist = d
                    nearest_stop_idx = idx
            logger.info(
                "[NAV] Test mode: auto-selected nearest stop index=%d (pkg=%s, dist=%.0fm)",
                nearest_stop_idx, package_ids[nearest_stop_idx] if nearest_stop_idx < len(package_ids) else "N/A", min_dist)
            # Override new_order to put nearest stop first
            if nearest_stop_idx != new_order[0]:
                new_order = [nearest_stop_idx] + [i for i in new_order if i != nearest_stop_idx]
                logger.info("[NAV] Test mode: re-ordered new_order to put nearest first: %s", new_order)
        
        do_major_reorder = is_major_off_route or is_new_stop_significantly_closer
    
    # Log re-order type decision
    logger.info(
        "[NAV] Re-order type: %s (off_route=%.0fm, major_threshold=%.0fm, "
        "dist_current=%.0fm, dist_new=%.0fm, closer_pct=%.1f%%)",
        "MAJOR (switch active)" if do_major_reorder else "STANDARD (remaining only)",
        off_route_distance_m, major_threshold,
        dist_to_current_active, dist_to_new_first,
        closer_pct)
    
    # 13. Apply re-order: rebuild legs in new order
    reordered_legs = []
    for new_idx, old_idx in enumerate(new_order):
        leg = remaining_legs[old_idx].copy()
        leg["stop_order"] = session.leg_index + 1 + new_idx
        reordered_legs.append(leg)
    
    # Update Redis snapshot with new leg order
    new_legs = legs[:session.leg_index + 1] + reordered_legs
    nav["legs"] = new_legs
    
    from app.services.tracking import set_nav_route
    await set_nav_route(redis, session.kurir_id, nav)
    
    # 14. Handle active stop switch (major re-order) or standard re-order
    first_stop_changed = (new_order[0] != 0)
    active_stop_switched = False
    
    if do_major_reorder and first_stop_changed:
        # MAJOR RE-ORDER: Switch active stop to the new nearest stop
        new_first_leg = reordered_legs[0]
        from app.services.polyline import decode_polyline
        
        new_coords = decode_polyline(new_first_leg.get("encoded") or "")
        if len(new_coords) >= 2:
            new_dest = new_first_leg.get("dest")
            if new_dest:
                # Store old active stop info for notification
                old_package_id = session.current_package_id
                old_recipient = session.current_recipient
                
                # Update session state for new active leg
                session.coords = new_coords
                session.dest = (float(new_dest[0]), float(new_dest[1]))
                session.leg_index = session.leg_index + 1  # Move to new first leg
                session.current_package_id = new_first_leg.get("package_id")
                session.current_recipient = new_first_leg.get("recipient_name")
                session.off_route_active = False
                session.cooldown_until = 0.0
                session.last_progress_push = 0.0
                
                # Reset navigation state for new leg
                session.node_sequence = []
                session.edge_classes = {}
                session.edge_names = {}
                session.steps = []
                session.current_step_index = 0
                session.traveled_distance_m = 0.0
                
                active_stop_switched = True
                
                logger.info(
                    "[NAV] MAJOR RE-ORDER: kurir=%s active_stop_switched %s -> %s "
                    "(off_route=%.0fm, dist_saving=%.1f%%, test_mode=%s, auto_nearest=%s)",
                    session.kurir_id, old_package_id, new_first_leg.get("package_id"),
                    off_route_distance_m, saving_pct, test_mode, nearest_stop_idx != new_order[0])
    elif first_stop_changed:
        # STANDARD RE-ORDER: Only first remaining stop changed, keep current active
        new_first_leg = reordered_legs[0]
        from app.services.polyline import decode_polyline
        
        new_coords = decode_polyline(new_first_leg.get("encoded") or "")
        if len(new_coords) >= 2:
            new_dest = new_first_leg.get("dest")
            if new_dest:
                # Update session state for new first remaining leg (NOT active yet)
                # Note: we do NOT increment leg_index here, current active stop stays
                # The new first leg will be the NEXT leg after current active completes
                logger.info(
                    "[NAV] STANDARD RE-ORDER: kurir=%s next_stop=%s saving=%.1f%%",
                    session.kurir_id, new_first_leg.get("package_id"), saving_pct)
    
    # 15. Send re-order notification to client
    if session.ws is not None:
        try:
            if active_stop_switched:
                reorder_reason = "off_route_major_switch_active" + ("_test_auto_nearest" if test_mode else "")
            else:
                reorder_reason = "off_route_closer_to_next_stop" + ("_test" if test_mode else "")
            
            # Build legs with full geometries for frontend rendering
            legs_with_geometry = []
            
            # Build all legs in new order with geometries
            all_legs_in_order = []
            if active_stop_switched:
                # Active leg is the first leg (index 0): current position -> first stop
                first_leg = reordered_legs[0]
                remaining_legs = reordered_legs[1:]
                all_legs_in_order = [first_leg] + remaining_legs
            else:
                # Standard reorder: current active stays, remaining legs reordered
                # Current active leg stays as is, then reordered remaining legs
                current_active_leg = legs[session.leg_index] if session.leg_index < len(legs) else None
                remaining_legs = reordered_legs
                if current_active_leg:
                    all_legs_in_order = [current_active_leg] + remaining_legs
                else:
                    all_legs_in_order = remaining_legs
            
            # Build legs with geometries
            # Active leg (index 0): from current position to first stop
            if all_legs_in_order:
                first_leg = all_legs_in_order[0]
                first_leg_coords = session.coords if active_stop_switched else (session.coords if session.coords else [])
                if first_leg_coords and len(first_leg_coords) >= 2:
                    legs_with_geometry.append({
                        "leg_index": 0,
                        "geometry": encode_polyline(first_leg_coords, 5),
                        "distance_km": round(sum(haversine_distance(first_leg_coords[i], first_leg_coords[i+1]) for i in range(len(first_leg_coords)-1)) / 1000.0, 2),
                        "duration_mins": 0,  # Will be calculated if needed
                    })
            
            # Remaining legs (index 1 onwards)
            for idx, leg in enumerate(all_legs_in_order[1:], start=1):
                # We need to get the geometry for this leg from the stored route
                # For now, we'll use the encoded polyline from the leg if available
                leg_geometry = leg.get("geometry") or leg.get("encoded") or ""
                legs_with_geometry.append({
                    "leg_index": idx,
                    "geometry": leg_geometry,
                    "distance_km": leg.get("distance_km", 0),
                    "duration_mins": leg.get("duration_mins", 0),
                })
            
            if active_stop_switched:
                reorder_reason = "off_route_major_switch_active" + ("_test_auto_nearest" if test_mode else "")
            else:
                reorder_reason = "off_route_closer_to_next_stop" + ("_test" if test_mode else "")
            
            await session.ws.send_json({
                "type": "stops_reordered",
                "ok": True,
                "route_id": session.route_id,
                "reorder_reason": reorder_reason,
                "new_stop_order": [
                    {
                        "package_id": leg.get("package_id"),
                        "recipient_name": leg.get("recipient_name"),
                        "service_type": leg.get("service_type", "REGULAR"),
                        "stop_order": leg.get("stop_order"),
                        "dest": leg.get("dest"),
                    }
                    for leg in reordered_legs
                ],
                "legs": legs_with_geometry,
                "active_leg_index": 0 if active_stop_switched else session.leg_index,
                "current_position": [current_lat, current_lon] if active_stop_switched else None,
                "active_leg_changed": active_stop_switched,
                "active_stop_switched": active_stop_switched,
                "switched_from": {
                    "package_id": session.current_package_id if active_stop_switched else None,
                    "recipient_name": session.current_recipient if active_stop_switched else None,
                } if active_stop_switched else None,
                "switched_to": {
                    "package_id": new_first_leg.get("package_id") if active_stop_switched else None,
                    "recipient_name": new_first_leg.get("recipient_name") if active_stop_switched else None,
                    "dest": new_first_leg.get("dest") if active_stop_switched else None,
                } if active_stop_switched else None,
                "polyline": encode_polyline(session.coords, 5) if active_stop_switched else None,
                "ts": int(time.time()),
            })
        except Exception as exc:
            logger.warning("[NAV] Failed to send reorder notification: %s", exc)
    
    # 16. Update re-order state (bypass cooldown in test mode)
    if not test_mode:
        session.reorder_cooldown_until = now + REORDER_COOLDOWN_SECONDS
    session.reorder_count += 1
    session._reorder_candidate = None
    session._reorder_candidate_count = 0
    
    return True


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
