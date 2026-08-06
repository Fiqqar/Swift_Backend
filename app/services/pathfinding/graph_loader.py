import logging
import math
import os
import pickle
import threading

from app.services.pathfinding.core_a_star import haversine_distance
from app.services.pathfinding.preprocess import (
    PathGraph,
    build_path_graph,
)

logger = logging.getLogger("pathfinding")

_PROBE_TIMEOUT = 5

_OSMNX_TIMEOUT = 25
_BASE_RADIUS = 3000
_MAX_RADIUS = int(os.environ.get("OSMNX_MAX_RADIUS", 50000))
_RADIUS_MARGIN = 1.25
_RADIUS_PADDING = 500
_GRID = float(os.environ.get("OSMNX_CACHE_GRID", "0.005"))
_GRID_EXTRA = _GRID * 111320.0
_DISK_CACHE_DIR = os.environ.get(
    "OSMNX_DISK_CACHE", os.path.join("cache", "pathfinding"))
_ENABLE_CH = os.environ.get("OSMNX_ENABLE_CH", "1") == "1"
_PG_VERSION = 2

_GRAPH_CACHE = {}
_LOCATIONS_CACHE = {}
_SOURCE_CACHE = {}
_WARNING_CACHE = {}
_PG_CACHE = {}
_CACHE_LOCK = threading.RLock()


def auto_radius(origin_lat: float, origin_lon: float,
                dest_lat: float, dest_lon: float) -> int:
    distance = haversine_distance((origin_lat, origin_lon), (dest_lat, dest_lon))
    radius = int(max(_BASE_RADIUS, distance * _RADIUS_MARGIN + _RADIUS_PADDING))
    return min(radius, _MAX_RADIUS)


def radius_timeout(dist_meters: int) -> int:
    timeout = int(dist_meters // 1000) + 20
    return max(_OSMNX_TIMEOUT, min(timeout, 120))


def _try_import_osmnx():
    try:
        import osmnx as ox
        _configure_osmnx(ox, _OSMNX_TIMEOUT)
        return ox, None
    except Exception as exc:
        logger.warning("osmnx tidak dapat dimuat: %s", exc)
        return None, f"Gagal memuat osmnx ({type(exc).__name__}): {exc}"


def _configure_osmnx(ox, timeout: int):
    ox.settings.requests_timeout = timeout
    overpass_url = os.environ.get("OSMNX_OVERPASS_URL")
    if overpass_url:
        ox.settings.overpass_url = overpass_url.rstrip("/")


def _snap(value: float) -> float:
    return round(value / _GRID) * _GRID


def _cache_key(lat: float, lon: float, dist_meters: int) -> tuple:
    return (_snap(lat), _snap(lon), int(dist_meters))


def _disk_path(key: tuple) -> str:
    lat, lon, radius = key
    return os.path.join(_DISK_CACHE_DIR, f"g_{lat:.6f}_{lon:.6f}_{radius}.pkl")


def _pg_disk_path(key: tuple) -> str:
    lat, lon, radius = key
    return os.path.join(_DISK_CACHE_DIR,
                        f"pg_v{_PG_VERSION}_{lat:.6f}_{lon:.6f}_{radius}.pkl")


def _save_disk(path: str, data) -> None:
    try:
        os.makedirs(_DISK_CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("Gagal menyimpan cache disk: %s", exc)


def load_osm_graph_by_point(lat: float, lon: float,
                            dist_meters: int = 3000):
    key = _cache_key(lat, lon, dist_meters)
    lat, lon = key[0], key[1]

    with _CACHE_LOCK:
        if key in _GRAPH_CACHE:
            return (_GRAPH_CACHE[key], _LOCATIONS_CACHE[key],
                    _SOURCE_CACHE[key], _WARNING_CACHE[key])

    path = _disk_path(key)
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                data = pickle.load(fh)
            with _CACHE_LOCK:
                _GRAPH_CACHE[key] = data["graph"]
                _LOCATIONS_CACHE[key] = data["locations"]
                _SOURCE_CACHE[key] = data["source"]
                _WARNING_CACHE[key] = data["warning"]
            return (data["graph"], data["locations"],
                    data["source"], data["warning"])
        except Exception as exc:
            logger.warning("Cache disk tidak terbaca: %s", exc)

    graph, locations, source, warning = _load_raw(lat, lon, dist_meters)

    with _CACHE_LOCK:
        _GRAPH_CACHE[key] = graph
        _LOCATIONS_CACHE[key] = locations
        _SOURCE_CACHE[key] = source
        _WARNING_CACHE[key] = warning
    _save_disk(path, {"graph": graph, "locations": locations,
                      "source": source, "warning": warning})
    return graph, locations, source, warning


def _overpass_reachable(url: str, timeout: float = _PROBE_TIMEOUT) -> bool:
    try:
        import httpx
    except ImportError:
        return True
    try:
        response = httpx.get(
            url.rstrip("/") + "/status",
            timeout=timeout,
            headers={"User-Agent": "Test2Pathfinding/1.0 (probe)"},
        )
        return response.status_code == 200
    except Exception:
        return False


def _friendly_osm_error(exc: Exception) -> str:
    if isinstance(exc, UnboundLocalError):
        return "Gagal mengunduh peta OSM: server Overpass tidak merespons"
    try:
        import requests
        if isinstance(exc, requests.exceptions.RequestException):
            return ("Gagal mengunduh peta OSM: "
                    "server Overpass tidak terjangkau atau tidak merespons")
    except ImportError:
        pass
    err_type = type(exc).__name__
    if err_type in ("Timeout", "ReadTimeout", "ConnectTimeout", "ConnectionError"):
        return ("Gagal mengunduh peta OSM: "
                "server Overpass tidak terjangkau atau tidak merespons")
    return f"Gagal mengunduh peta OSM ({err_type}): {exc}"


def _load_raw(lat: float, lon: float, dist_meters: int):
    ox, import_warning = _try_import_osmnx()
    if ox is not None:
        if not _overpass_reachable(ox.settings.overpass_url):
            warning = "Server OSM (Overpass) tidak terjangkau - cek koneksi internet"
        else:
            try:
                graph, locations = _load_osm_graph(ox, lat, lon, dist_meters)
                return graph, locations, "osm", None
            except Exception as exc:
                warning = _friendly_osm_error(exc)
    else:
        warning = import_warning or "osmnx tidak terpasang di lingkungan ini"

    graph, locations = _build_demo_grid(lat, lon, dist_meters)
    return graph, locations, "demo", warning


def load_path_graph(lat: float, lon: float, dist_meters: int = 3000,
                    use_ch: bool | None = None) -> PathGraph:
    key = _cache_key(lat, lon, dist_meters)
    lat, lon = key[0], key[1]

    with _CACHE_LOCK:
        if key in _PG_CACHE:
            return _PG_CACHE[key]

    path = _pg_disk_path(key)
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                pg = pickle.load(fh)
            with _CACHE_LOCK:
                _PG_CACHE[key] = pg
            return pg
        except Exception as exc:
            logger.warning("Cache PathGraph tidak terbaca: %s", exc)

    graph, locations, source, warning = load_osm_graph_by_point(
        lat, lon, dist_meters)
    enable_ch = _ENABLE_CH if use_ch is None else use_ch
    pg = build_path_graph(graph, locations, lat, lon, enable_ch=enable_ch)
    pg.radius = int(dist_meters)
    pg.source = source
    pg.warning = warning

    with _CACHE_LOCK:
        _PG_CACHE[key] = pg
    _save_disk(path, pg)
    return pg


def _point_inside(pg: PathGraph, lat: float, lon: float) -> bool:
    return haversine_distance((pg.ref_lat, pg.ref_lon),
                              (lat, lon)) <= pg.radius + _GRID_EXTRA


def load_graph_covering(lat1: float, lon1: float,
                        lat2: float, lon2: float) -> PathGraph:
    dist = haversine_distance((lat1, lon1), (lat2, lon2))
    mid_lat = (lat1 + lat2) / 2.0
    mid_lon = (lon1 + lon2) / 2.0
    need = int(dist / 2 * _RADIUS_MARGIN + _RADIUS_PADDING)
    need = min(_MAX_RADIUS, max(_BASE_RADIUS, need))

    if need > _BASE_RADIUS and dist / 2 <= _BASE_RADIUS:
        pg = load_path_graph(mid_lat, mid_lon, _BASE_RADIUS)
        if _point_inside(pg, lat1, lon1) and _point_inside(pg, lat2, lon2):
            return pg
    return load_path_graph(mid_lat, mid_lon, need)


def _load_osm_graph(ox, lat: float, lon: float, dist_meters: int):
    _configure_osmnx(ox, radius_timeout(dist_meters))
    G = ox.graph_from_point((lat, lon), dist=dist_meters, network_type="drive")

    locations = {node_id: (data['y'], data['x']) for node_id, data in G.nodes(data=True)}
    graph = {node_id: {} for node_id in G.nodes}

    for u, v, data in G.edges(data=True):
        length = data.get('length', 1.0)
        graph[u][v] = length

        if not data.get('oneway', False):
            graph[v][u] = length

    return graph, locations


def _build_demo_grid(lat: float, lon: float, dist_meters: int,
                     rows: int = 6, cols: int = 6):
    locations = {}
    graph = {}

    lat_span = dist_meters / 111320.0
    lon_span = dist_meters / (111320.0 * max(0.1, math.cos(math.radians(lat))))

    def node_id(r, c):
        return r * cols + c

    for r in range(rows):
        lat_frac = (r / (rows - 1)) * 2 - 1
        for c in range(cols):
            lon_frac = (c / (cols - 1)) * 2 - 1
            nid = node_id(r, c)
            locations[nid] = (lat + lat_frac * lat_span, lon + lon_frac * lon_span)
            graph[nid] = {}

    for r in range(rows):
        for c in range(cols):
            nid = node_id(r, c)
            if c + 1 < cols:
                other = node_id(r, c + 1)
                d = haversine_distance(locations[nid], locations[other])
                graph[nid][other] = d
                graph[other][nid] = d
            if r + 1 < rows:
                other = node_id(r + 1, c)
                d = haversine_distance(locations[nid], locations[other])
                graph[nid][other] = d
                graph[other][nid] = d

    return graph, locations


def find_nearest_node(lat: float, lon: float, locations: dict) -> int | None:
    nearest_node = None
    min_dist = float('inf')

    for node_id, coord in locations.items():
        dist = haversine_distance((lat, lon), coord)
        if dist < min_dist:
            min_dist = dist
            nearest_node = node_id

    return nearest_node
