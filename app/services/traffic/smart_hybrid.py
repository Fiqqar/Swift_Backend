import asyncio
import json
import logging
import math
import os

from shapely.geometry import Point
from shapely.strtree import STRtree
from starlette.concurrency import run_in_threadpool

from app.services.cache_service import load_penalties
from app.services.pathfinding.core_a_star import haversine_distance
from app.services.traffic.matcher import snap_segment
from app.services.traffic.poller import _snap_tolerance
from app.services.traffic.provider import provider_mode, traffic_enabled
from app.services.traffic.tomtom import TomTomProvider

logger = logging.getLogger("pathfinding")

OD_KEY_PREFIX = "traffic:od:"
CORRIDOR_KEY_PREFIX = "traffic:corridor:"

# Cache index node base graph per objek graf.
_NODE_INDEX_CACHE: dict[int, tuple] = {}

_CORRIDOR_MAX_CONCURRENT = 5


def _probe_radius_km() -> float:
    try:
        return max(0.1, float(os.environ.get("TOMTOM_PROBE_RADIUS_KM", "5.0")))
    except ValueError:
        return 5.0


def _od_ttl() -> int:
    try:
        return max(30, int(os.environ.get("TRAFFIC_REDIS_TTL_SECONDS", "300")))
    except ValueError:
        return 300


def _corridor_sample_km() -> float:
    """Jarak antar titik sampel corridor (km). Default 40 km."""
    try:
        return max(5.0, float(os.environ.get("TOMTOM_CORRIDOR_SAMPLE_KM", "40")))
    except ValueError:
        return 40.0


def _corridor_max_samples() -> int:
    """Batas atas jumlah titik sampel corridor. Default 20."""
    try:
        return max(3, int(os.environ.get("TOMTOM_CORRIDOR_MAX_SAMPLES", "20")))
    except ValueError:
        return 20


def _corridor_sample_count(route_coordinates: list) -> int:
    """Jumlah titik sampel corridor berbasis jarak rute.

    Satu titik sampel per `_corridor_sample_km()` km di sepanjang rute,
    dibatasi `_corridor_max_samples()`. Minimal 3 titik agar rute pendek
    tetap ter-probe cukup padat.
    """
    total_km = 0.0
    for i in range(1, len(route_coordinates)):
        total_km += haversine_distance(
            route_coordinates[i - 1], route_coordinates[i]) / 1000.0
    n = int(math.ceil(total_km / _corridor_sample_km()))
    return max(3, min(_corridor_max_samples(), n))


def _congestion_ratio() -> float:
    try:
        return max(1.0, float(os.environ.get("TOMTOM_CONGESTION_RATIO", "1.5")))
    except ValueError:
        return 1.5


def _grid_key(lat: float, lon: float) -> str:
    """Sel grid ~500m: round lat ke 0.0045 deg (~500m), lon ikut cos.

    Satu grid = area cache; request pada grid yang sama dalam TTL tidak
    memanggil TomTom lagi.
    """
    dlat = 0.0045
    dlon = dlat / max(0.1, math.cos(math.radians(lat)))
    return "%d,%d" % (math.floor(lat / dlat), math.floor(lon / dlon))


def _node_index(graph: dict, locations: dict):
    """Index spasial node (Point) dari graf; di-cache per objek graf."""
    key = id(graph)
    cached = _NODE_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    ids = [n for n in graph if n in locations]
    if not ids:
        _NODE_INDEX_CACHE[key] = (None, [])
        return None, []
    points = [Point(locations[n][1], locations[n][0]) for n in ids]
    tree = STRtree(points)
    _NODE_INDEX_CACHE[key] = (tree, ids)
    if len(_NODE_INDEX_CACHE) > 16:
        oldest = next(iter(_NODE_INDEX_CACHE))
        _NODE_INDEX_CACHE.pop(oldest, None)
    return tree, ids


def _select_probe_points(graph: dict, locations: dict,
                         lat: float, lon: float,
                         max_points: int = 2) -> list[tuple[float, float]]:
    """Pilih s.d. max_points node arteri base-graph dalam radius O/D.

    Node diurutkan dari yang terdekat ke titik. Bila tak ada node dalam
    radius, fallback ke koordinat O/D itu sendiri (TomTom tetap dipanggil,
    snap_segment yang memutuskan apakah ada edge terpengaruh).
    """
    radius_m = _probe_radius_km() * 1000.0
    tree, ids = _node_index(graph, locations)
    if tree is None:
        return [(lat, lon)]
    coslat = math.cos(math.radians(lat))
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * max(0.1, coslat))
    buffered = Point(lon, lat).buffer(max(dlat, dlon))
    candidates = []
    for i in tree.query(buffered):
        n = ids[int(i)]
        d = haversine_distance((lat, lon), locations[n])
        if d <= radius_m:
            candidates.append((d, n))
    if not candidates:
        return [(lat, lon)]
    candidates.sort(key=lambda item: item[0])
    return [locations[n] for _, n in candidates[:max_points]]


def _sample_route_points(coords: list, n: int) -> list[tuple[float, float]]:
    """Ambil n titik sampel merata di sepanjang polyline rute (interior).

    Titik diambil pada fraksi k/(n+1) dari total panjang rute (k = 1..n),
    jadi ujung origin & destination tidak ikut disampel (sudah di-probe O/D).
    Kembalikan [] bila rute terlalu pendek (< 2 koordinat).
    """
    if not coords or len(coords) < 2:
        return []
    n = max(0, min(5, n))
    if n <= 0:
        return []
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_distance(
            (coords[i - 1][0], coords[i - 1][1]),
            (coords[i][0], coords[i][1])))
    total = cum[-1]
    if total <= 0:
        return []
    samples = []
    for k in range(1, n + 1):
        target = total * k / (n + 1)
        # cari segmen yang memuat target
        for i in range(1, len(cum)):
            if cum[i] >= target:
                seg_len = cum[i] - cum[i - 1]
                frac = (target - cum[i - 1]) / seg_len if seg_len > 0 else 0.0
                lat = coords[i - 1][0] + (coords[i][0] - coords[i - 1][0]) * frac
                lon = coords[i - 1][1] + (coords[i][1] - coords[i - 1][1]) * frac
                samples.append((lat, lon))
                break
    return samples


async def _redis_get_json(redis, key: str) -> dict | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis get %s gagal: %s", key, exc)
        return None


async def _redis_set_json(redis, key: str, data: dict, ttl: int) -> None:
    if redis is None:
        return
    try:
        await redis.set(key, json.dumps(data), ex=ttl)
    except Exception as exc:
        logger.warning("Redis set %s gagal: %s", key, exc)


async def _base_graph(app):
    """Graf rujukan untuk probe point & snap segmen (level-3 / arteri)."""
    pg = (getattr(app.state, "region_graph", None)
          or getattr(app.state, "path_graph", None))
    if pg is not None and getattr(pg, "graph", None):
        return pg
    from app.services.pathfinding.graph_loader import (
        base_available,
        load_base_graph,
    )
    if not base_available():
        return None
    try:
        return await run_in_threadpool(load_base_graph)
    except Exception as exc:
        logger.warning("[TRAFFIC] Base graph gagal dimuat: %s", exc)
        return None


async def load_cached_penalties(redis) -> dict[int, float]:
    """Agregasi semua penalti TomTom tersimpan: traffic:od:* & traffic:corridor:*.

    Setiap nilai key adalah JSON {str(edge_id): weight}. Hasilnya dipakai
    untuk overlay /traffic/map dan hitungan /traffic/status (area yang
    pernah di-request, bertahan selama TTL).
    """
    if redis is None:
        return {}
    penalties: dict[int, float] = {}
    for prefix in (OD_KEY_PREFIX, CORRIDOR_KEY_PREFIX):
        try:
            keys = await redis.keys(prefix + "*")
        except Exception as exc:
            logger.warning("[TRAFFIC] Scan key %s gagal: %s", prefix, exc)
            continue
        for key in keys:
            data = await _redis_get_json(redis, key)
            if not data:
                continue
            for edge_str, weight in data.items():
                try:
                    penalties[int(edge_str)] = float(weight)
                except (TypeError, ValueError):
                    continue
    return penalties


async def probe_corridor(app, redis,
                         route_coordinates: list) -> dict:
    """Probe TomTom di sepanjang corridor rute awal (mid-route sampling).

    1. Ambil 3-5 titik sampel merata di polyline rute (interior).
    2. Snap tiap titik ke node arteri base-graph, lalu query TomTom Flow
       (cache per grid traffic:corridor:*, TTL TRAFFIC_REDIS_TTL_SECONDS).
    3. Kembalikan {"penalties": {edge_id: weight}, "congested": bool}.
       congested = True bila ada weight > TOMTOM_CONGESTION_RATIO (1.5).

    Bobot corridor hanya berlaku di RAM request ini (tidak ditulis ke hash
    global traffic:penalties); cache Redis hanya menyimpan hasil per grid.
    """
    if not traffic_enabled() or provider_mode() != "smart_hybrid":
        return {"penalties": {}, "congested": False}
    base = await _base_graph(app)
    if base is None:
        return {"penalties": {}, "congested": False}
    graph, locations = base.graph, base.locations
    tolerance = _snap_tolerance()
    provider = TomTomProvider()
    ratio = _congestion_ratio()

    samples = _sample_route_points(
        route_coordinates, _corridor_sample_count(route_coordinates))
    penalties: dict[int, float] = {}
    filled: set[int] = set()
    congested = False

    probe_points = []
    for lat, lon in samples:
        pts = _select_probe_points(graph, locations, lat, lon, max_points=1)
        probe_points.append(pts[0])

    _corridor_sem = asyncio.Semaphore(_CORRIDOR_MAX_CONCURRENT)

    async def _probe_grid(idx: int) -> dict:
        p = probe_points[idx]
        key = CORRIDOR_KEY_PREFIX + _grid_key(*p)
        cached = await _redis_get_json(redis, key)
        if cached is not None:
            return cached
        async with _corridor_sem:
            events = await provider.fetch_points([p])
        cached = {}
        for event in events:
            for edge in snap_segment(
                    graph, locations, event.coordinates, tolerance):
                cached[str(edge)] = event.weight
        await _redis_set_json(redis, key, cached, ttl=_od_ttl())
        return cached

    grid_caches = await asyncio.gather(
        *[_probe_grid(i) for i in range(len(probe_points))])
    for cached in grid_caches:
        for edge_str, weight in cached.items():
            edge = int(edge_str)
            if edge in filled:
                continue
            filled.add(edge)
            weight = float(weight)
            penalties[edge] = weight
            if weight > ratio:
                congested = True
    return {"penalties": penalties, "congested": congested}


async def get_request_penalties(app, redis,
                                origin: tuple[float, float],
                                dest: tuple[float, float]) -> dict[int, float]:
    """Penalti lalu lintas untuk satu request find_route.

    - traffic off                     -> {} (bobot standar, tanpa API/Redis)
    - internal_only / full_tomtom     -> load_penalties(redis) (poller global)
    - smart_hybrid                    -> penalti global (internal poller) yang
      di-overlay TomTom on-demand di area O & D (cache grid, TTL).
    """
    if not traffic_enabled():
        return {}
    mode = provider_mode()
    if mode != "smart_hybrid":
        return await load_penalties(redis)

    base = await _base_graph(app)
    if base is None:
        return await load_penalties(redis)
    graph, locations = base.graph, base.locations
    tolerance = _snap_tolerance()
    provider = TomTomProvider()

    od_penalties: dict[int, float] = {}
    filled: set[int] = set()
    for lat, lon in (origin, dest):
        grid = _grid_key(lat, lon)
        key = OD_KEY_PREFIX + grid
        cached = await _redis_get_json(redis, key)
        if cached is None:
            probe_points = _select_probe_points(graph, locations, lat, lon)
            events = await provider.fetch_points(probe_points)
            cached = {}
            for event in events:
                for edge in snap_segment(
                        graph, locations, event.coordinates, tolerance):
                    cached[str(edge)] = event.weight
            await _redis_set_json(redis, key, cached, ttl=_od_ttl())
        for edge_str, weight in cached.items():
            edge = int(edge_str)
            if edge not in filled:
                filled.add(edge)
                od_penalties[edge] = float(weight)

    merged = await load_penalties(redis)
    merged.update(od_penalties)
    return merged
