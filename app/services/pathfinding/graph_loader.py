import json
import logging
import math
import os
import pickle
import threading
from time import perf_counter

from app.services.pathfinding.core_a_star import haversine_distance
from app.services.pathfinding.preprocess import (
    PathGraph,
    build_path_graph,
)
from app.services.pathfinding.pbf_registry import (
    PbfEntry,
    discover_pbfs,
    pbfs_available,
    select_pbf,
)

logger = logging.getLogger("pathfinding")


class AreaNotCoveredError(ValueError):
    """Area yang diminta tidak tercakup oleh file PBF lokal."""


def _setup_perf_logging():
    """Pastikan log [PERF] (level INFO) muncul ke stderr."""
    log = logging.getLogger("pathfinding")
    log.setLevel(logging.INFO)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [pathfinding] %(message)s"))
        log.addHandler(handler)
        log.propagate = False


_setup_perf_logging()


def _perf(label: str, start: float) -> None:
    logger.info("[PERF] %s: %.1f ms", label, (perf_counter() - start) * 1000.0)


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
_USE_LOCAL_PBF = os.environ.get("USE_LOCAL_PBF", "1") == "1"
_GRAPH_CACHE_SIZE = int(os.environ.get("OSMNX_GRAPH_CACHE_SIZE", "32"))
_PG_CACHE_SIZE = int(os.environ.get("OSMNX_PG_CACHE_SIZE", "8"))
_RADIUS_BUCKET = int(os.environ.get("OSMNX_RADIUS_BUCKET", "5000"))
_DRIVE_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "service", "living_street", "road",
}
_PG_VERSION = 3

_LEVEL_DIST_KM_1 = 4.0
_LEVEL_DIST_KM_2 = 100.0
_PAD_MEDIUM_DEG = 0.05
_PAD_LONG_DEG = 0.10

_LEVEL_HIGHWAYS = {
    1: None,
    2: _DRIVE_HIGHWAYS - {"service", "residential", "living_street", "unclassified"},
    3: {"motorway", "motorway_link", "trunk", "trunk_link",
        "primary", "primary_link"},
}

DEFAULT_REGION_BBOX = (-7.10, 110.25, -6.40, 111.20)
# Level filter jalan utk graf regional (2 = semua jalan layak kendaraan minus
# jalan kecil). Level 3 (tol/trunk/primary saja) hanya dipakai rute jarak jauh.
REGION_LEVEL = int(os.environ.get("OSMNX_REGION_LEVEL", "2"))

# --- Hierarchical routing (lapisan local tile + base graph) ---
# Local: graf gang (level 1) di sekitar origin/tujuan, dibangun dari TILE
# (hasil scripts/split_tiles.py) agar tidak scan PBF raksasa per request.
_TILES_ENABLED = os.environ.get("TILES_ENABLED", "1") == "1"
_TILES_DIR = os.environ.get("TILES_DIR", os.path.join("data", "tiles"))
_TILE_DEG = float(os.environ.get("TILE_DEG", "0.2"))
_TILE_BUFFER_DEG = float(os.environ.get("TILE_BUFFER_DEG", "0.01"))
# Base: graf jalan utama seluruh Jawa (level 3), dibangun sekali offline,
# dimuat lazy ke RAM pada request jarak jauh pertama.
_BASE_GRAPH_ENABLED = os.environ.get("BASE_GRAPH_ENABLED", "1") == "1"
_BASE_LEVEL = int(os.environ.get("BASE_GRAPH_LEVEL", "3"))
_LOCAL_RADIUS = int(os.environ.get("LOCAL_RADIUS", "3000"))
_LOCAL_RADIUS_MAX = int(os.environ.get("LOCAL_RADIUS_MAX", "10000"))
_HIERARCHICAL_MIN_M = float(os.environ.get("HIERARCHICAL_MIN_KM", "25")) * 1000.0


class _LRUDict(dict):
    """dict berbatas yang membuang entri terlama saat melampaui maxsize."""

    def __init__(self, maxsize: int):
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value):
        if key in self:
            super().__delitem__(key)
        super().__setitem__(key, value)
        if len(self) > self._maxsize:
            oldest = next(iter(self))
            super().__delitem__(oldest)


def pbf_available() -> bool:
    """True bila PBF lokal diaktifkan dan ada file di disk."""
    return bool(_USE_LOCAL_PBF and pbfs_available())


def _level_for_distance(dist_meters: float) -> int:
    dist_km = dist_meters / 1000.0
    if dist_km <= _LEVEL_DIST_KM_1:
        return 1
    if dist_km <= _LEVEL_DIST_KM_2:
        return 2
    return 3


def _rect_bbox(lat1: float, lon1: float,
               lat2: float, lon2: float, pad_deg: float) -> tuple:
    dlat = pad_deg
    dlon = pad_deg / max(0.1, math.cos(math.radians((lat1 + lat2) / 2.0)))
    return (min(lon1, lon2) - dlon, min(lat1, lat2) - dlat,
            max(lon1, lon2) + dlon, max(lat1, lat2) + dlat)


def _pbf_adaptive(lat: float, lon: float, dist_meters: int,
                  origin: tuple | None = None,
                  dest: tuple | None = None) -> tuple:
    """Tentukan (level, bbox) untuk jalur PBF adaptif.

    Level 1 (<=4 km): bbox persegi di sekitar pusat (rect=None).
    Level 2/3 (>4 km): bbox persegi panjang origin->dest + padding.
    Bila origin/dest tidak diketahui, level diambil dari radius saja.
    """
    if origin is not None and dest is not None:
        dist_m = haversine_distance(origin, dest)
        level = _level_for_distance(dist_m)
        if level >= 2:
            pad = _PAD_MEDIUM_DEG if level == 2 else _PAD_LONG_DEG
            rect = _rect_bbox(origin[0], origin[1], dest[0], dest[1], pad)
        else:
            rect = None
    else:
        level = _level_for_distance(dist_meters)
        rect = None
    return level, rect


def _select_pbf(origin: tuple | None = None,
                dest: tuple | None = None,
                lat: float | None = None,
                lon: float | None = None) -> PbfEntry | None:
    """Pilih PBF lokal yang mencakup area, atau None bila nonaktif/tak ada."""
    if not _USE_LOCAL_PBF:
        return None
    if origin is not None and dest is not None:
        return select_pbf(origin[0], origin[1], dest[0], dest[1])
    if lat is not None and lon is not None:
        return select_pbf(lat, lon, lat, lon)
    return None


def _pbf_load_plan(lat: float, lon: float, dist_meters: int,
                   origin: tuple | None = None,
                   dest: tuple | None = None,
                   pbf: PbfEntry | None = None,
                   level: int | None = None) -> tuple:
    """Kembalikan (level, rect, tag). Tag dipakai untuk cache key."""
    if level is None:
        level, rect = _pbf_adaptive(lat, lon, dist_meters, origin, dest)
    elif origin is not None and dest is not None:
        pad = _PAD_MEDIUM_DEG if level == 2 else _PAD_LONG_DEG
        rect = _rect_bbox(origin[0], origin[1], dest[0], dest[1], pad)
    else:
        rect = None
    tag = ""
    if pbf is not None:
        tag = _adaptive_tag(level, rect, pbf.pbf_id)
    return level, rect, tag


_GRAPH_CACHE = _LRUDict(_GRAPH_CACHE_SIZE)
_LOCATIONS_CACHE = _LRUDict(_GRAPH_CACHE_SIZE)
_SOURCE_CACHE = _LRUDict(_GRAPH_CACHE_SIZE)
_WARNING_CACHE = _LRUDict(_GRAPH_CACHE_SIZE)
_PG_CACHE = _LRUDict(_PG_CACHE_SIZE)
_CACHE_LOCK = threading.RLock()


def auto_radius(origin_lat: float, origin_lon: float,
                dest_lat: float, dest_lon: float) -> int:
    distance = haversine_distance((origin_lat, origin_lon), (dest_lat, dest_lon))
    radius = int(max(_BASE_RADIUS, distance * _RADIUS_MARGIN + _RADIUS_PADDING))
    return min(radius, _MAX_RADIUS)


def _quantize_radius(dist_meters: int) -> int:
    """Bulatkan radius ke atas ke kelipatan _RADIUS_BUCKET agar banyak
    permintaan dengan panjang rute mirip memakai cache graf yang sama."""
    bucket = max(100, _RADIUS_BUCKET)
    q = max(_BASE_RADIUS, dist_meters)
    if q % bucket:
        q = q + (bucket - q % bucket)
    return min(q, _MAX_RADIUS)


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


def _cache_key(lat: float, lon: float, dist_meters: int,
               tag: str = "") -> tuple:
    base = (_snap(lat), _snap(lon), int(dist_meters))
    if tag:
        return base + (tag,)
    return base


def _adaptive_tag(level: int, rect: tuple | None, pbf_id: str) -> str:
    if rect is None:
        return f"{pbf_id}_l{level}"
    minlon, minlat, maxlon, maxlat = rect
    return ("{pbf}_l{level}_{minlon:.5f}_{minlat:.5f}_{maxlon:.5f}_{maxlat:.5f}"
            .format(pbf=pbf_id, level=level, minlon=_snap(minlon),
                    minlat=_snap(minlat), maxlon=_snap(maxlon),
                    maxlat=_snap(maxlat)))


def _cache_file(prefix: str, key: tuple) -> str:
    lat, lon, radius = key[0], key[1], key[2]
    tag = key[3] if len(key) > 3 else ""
    name = f"{prefix}_{lat:.6f}_{lon:.6f}_{radius}.pkl"
    if tag:
        name = f"{prefix}_{tag}_{lat:.6f}_{lon:.6f}_{radius}.pkl"
    return os.path.join(_DISK_CACHE_DIR, name)


def _disk_path(key: tuple) -> str:
    return _cache_file(f"g_v{_PG_VERSION}", key)


def _pg_disk_path(key: tuple) -> str:
    return _cache_file(f"pg_v{_PG_VERSION}", key)


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
                            dist_meters: int = 3000,
                            origin: tuple | None = None,
                            dest: tuple | None = None,
                            pbf: PbfEntry | None = None,
                            level: int | None = None):
    if pbf is None:
        pbf = _select_pbf(origin, dest, lat, lon)
    level, rect, tag = _pbf_load_plan(
        lat, lon, dist_meters, origin, dest, pbf, level=level)
    key = _cache_key(lat, lon, dist_meters, tag)
    lat, lon = key[0], key[1]

    t_cache = perf_counter()
    with _CACHE_LOCK:
        if key in _GRAPH_CACHE:
            _perf("Disk Cache Check", t_cache)
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
            _perf("Disk Cache Check", t_cache)
            return (data["graph"], data["locations"],
                    data["source"], data["warning"])
        except Exception as exc:
            logger.warning("Cache disk tidak terbaca: %s", exc)

    _perf("Disk Cache Check", t_cache)
    graph, locations, source, warning = _load_raw(
        lat, lon, dist_meters, origin, dest, level, rect, pbf)

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


def _pbf_bbox(lat: float, lon: float, dist_meters: int) -> tuple:
    margin = 1.05
    dlat = (dist_meters * margin) / 111320.0
    dlon = (dist_meters * margin) / (
        111320.0 * max(0.1, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def load_graph_from_pbf(pbf_path: str,
                        bbox: tuple | None = None,
                        highway_filter: set | None = None):
    """Baca network dari file .osm.pbf via osmium (pyosmium).

    bbox: (minlon, minlat, maxlon, maxlat) untuk memotong area saat baca.
    highway_filter: set tipe jalan yang diizinkan; None = semua _DRIVE_HIGHWAYS.
    """
    import osmium

    allowed = _DRIVE_HIGHWAYS if highway_filter is None else highway_filter

    class _RefCollector(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.needed = set()

        def way(self, w):
            tags = dict(w.tags)
            if tags.get("highway") not in allowed:
                return
            if tags.get("area") == "yes":
                return
            for nd in w.nodes:
                self.needed.add(nd.ref)

    reader = osmium.io.Reader(pbf_path, types=osmium.osm.WAY)
    collector = _RefCollector()
    osmium.apply(reader, collector)
    reader.close()
    needed = collector.needed

    class _RoadNetHandler(osmium.SimpleHandler):
        def __init__(self, bbox, needed):
            super().__init__()
            self.bbox = bbox
            self.needed = needed
            self.nodes = {}
            self.ways = []

        def node(self, n):
            if n.id not in self.needed:
                return
            if not n.location.valid():
                return
            lat, lon = n.location.lat, n.location.lon
            if self.bbox is not None:
                minlon, minlat, maxlon, maxlat = self.bbox
                if not (minlon <= lon <= maxlon and minlat <= lat <= maxlat):
                    return
            self.nodes[n.id] = (lat, lon)

        def way(self, w):
            tags = dict(w.tags)
            if tags.get("highway") not in allowed:
                return
            if tags.get("area") == "yes":
                return
            self.ways.append(([nd.ref for nd in w.nodes], tags))

    handler = _RoadNetHandler(bbox, needed)
    reader = osmium.io.Reader(pbf_path)
    osmium.apply(reader, handler)
    reader.close()

    graph = {}
    for refs, tags in handler.ways:
        oneway = str(tags.get("oneway", "")).strip().lower()
        reversed_edge = oneway == "-1"
        one_way = oneway in ("yes", "true", "1", "-1")
        for i in range(len(refs) - 1):
            a, b = refs[i], refs[i + 1]
            if a == b or a not in handler.nodes or b not in handler.nodes:
                continue
            if reversed_edge:
                a, b = b, a
            length = haversine_distance(handler.nodes[a], handler.nodes[b])
            graph.setdefault(a, {})
            graph.setdefault(b, {})
            graph[a][b] = length
            if not one_way:
                graph[b][a] = length

    node_ids = set(graph)
    for neighbors in graph.values():
        node_ids.update(neighbors)
    locations = {nid: handler.nodes[nid]
                 for nid in node_ids if nid in handler.nodes}
    return graph, locations


def _load_raw(lat: float, lon: float, dist_meters: int,
              origin: tuple | None = None,
              dest: tuple | None = None,
              level: int | None = None,
              rect: tuple | None = None,
              pbf: PbfEntry | None = None):
    if level is None or rect is None:
        level, rect = _pbf_adaptive(lat, lon, dist_meters, origin, dest)

    if pbf is not None:
        try:
            bbox = rect if rect is not None else _pbf_bbox(lat, lon, dist_meters)
            t_pbf = perf_counter()
            graph, locations = load_graph_from_pbf(
                pbf.path, bbox, _LEVEL_HIGHWAYS[level])
            _perf("PBF Parse (Cold Start)", t_pbf)
            if graph and locations:
                return graph, locations, f"pbf:{pbf.stem}", None
            raise AreaNotCoveredError(
                f"PBF {pbf.basename} tidak memiliki data jalan "
                "di area yang diminta")
        except AreaNotCoveredError:
            raise
        except Exception as exc:
            logger.warning("Gagal memuat PBF: %s", exc)
            raise AreaNotCoveredError(
                f"Gagal memuat PBF ({type(exc).__name__}): {exc}") from exc

    if _USE_LOCAL_PBF and pbfs_available():
        raise AreaNotCoveredError("Area di luar cakupan file PBF lokal")

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
    warnings = [w for w in (warning, ) if w]
    return graph, locations, "demo", "; ".join(warnings)


def load_path_graph(lat: float, lon: float, dist_meters: int = 3000,
                    use_ch: bool | None = None,
                    origin: tuple | None = None,
                    dest: tuple | None = None,
                    pbf: PbfEntry | None = None,
                    level: int | None = None) -> PathGraph:
    if pbf is None:
        pbf = _select_pbf(origin, dest, lat, lon)
    level, rect, tag = _pbf_load_plan(
        lat, lon, dist_meters, origin, dest, pbf, level=level)
    key = _cache_key(lat, lon, dist_meters, tag)
    lat, lon = key[0], key[1]

    with _CACHE_LOCK:
        if key in _PG_CACHE:
            return _PG_CACHE[key]

    path = _pg_disk_path(key)
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                pg = pickle.load(fh)
            if getattr(pg, "bbox", None) is None:
                pg.bbox = rect
            with _CACHE_LOCK:
                _PG_CACHE[key] = pg
            return pg
        except Exception as exc:
            logger.warning("Cache PathGraph tidak terbaca: %s", exc)

    graph, locations, source, warning = load_osm_graph_by_point(
        lat, lon, dist_meters, origin, dest, pbf, level=level)
    enable_ch = _ENABLE_CH if use_ch is None else use_ch
    t_build = perf_counter()
    pg = build_path_graph(graph, locations, lat, lon, enable_ch=enable_ch)
    _perf("PathGraph Build", t_build)
    pg.radius = int(dist_meters)
    pg.source = source
    pg.warning = warning
    pg.bbox = rect

    with _CACHE_LOCK:
        _PG_CACHE[key] = pg
    _save_disk(path, pg)
    return pg


def _point_inside(pg: PathGraph, lat: float, lon: float) -> bool:
    return haversine_distance((pg.ref_lat, pg.ref_lon),
                              (lat, lon)) <= pg.radius + _GRID_EXTRA


def load_graph_covering(lat1: float, lon1: float,
                        lat2: float, lon2: float,
                        level: int | None = None) -> PathGraph:
    pbf = _select_pbf((lat1, lon1), (lat2, lon2))
    if pbf is None and _USE_LOCAL_PBF and pbfs_available():
        raise AreaNotCoveredError("Area di luar cakupan file PBF lokal")

    dist = haversine_distance((lat1, lon1), (lat2, lon2))
    mid_lat = (lat1 + lat2) / 2.0
    mid_lon = (lon1 + lon2) / 2.0
    need = int(dist / 2 * _RADIUS_MARGIN + _RADIUS_PADDING)
    need = min(_MAX_RADIUS, max(_BASE_RADIUS, need))
    need = _quantize_radius(need)

    origin = (lat1, lon1)
    dest = (lat2, lon2)

    if need > _BASE_RADIUS and dist / 2 <= _BASE_RADIUS:
        pg = load_path_graph(mid_lat, mid_lon, _BASE_RADIUS,
                             origin=origin, dest=dest, pbf=pbf, level=level)
        if _point_inside(pg, lat1, lon1) and _point_inside(pg, lat2, lon2):
            return pg
    return load_path_graph(mid_lat, mid_lon, need,
                           origin=origin, dest=dest, pbf=pbf, level=level)


def region_graph_cached(lat1: float, lon1: float,
                        lat2: float, lon2: float,
                        level: int | None = None) -> bool:
    """True bila graf wilayah (level+rect+radius) sudah ada di cache disk.

    Digunakan saat startup agar server tidak melakukan cold-build (scan PBF
    raksasa) secara sinkron; cukup cek keberadaan pickle disk yang sama
    dengan yang dipakai load_graph_covering.
    """
    pbf = _select_pbf((lat1, lon1), (lat2, lon2))
    dist = haversine_distance((lat1, lon1), (lat2, lon2))
    mid_lat = (lat1 + lat2) / 2.0
    mid_lon = (lon1 + lon2) / 2.0
    need = int(dist / 2 * _RADIUS_MARGIN + _RADIUS_PADDING)
    need = _quantize_radius(min(_MAX_RADIUS, max(_BASE_RADIUS, need)))
    level, rect, tag = _pbf_load_plan(
        mid_lat, mid_lon, need, (lat1, lon1), (lat2, lon2), pbf, level=level)
    key = _cache_key(mid_lat, mid_lon, need, tag)
    return os.path.exists(_pg_disk_path(key))


# ---------------------------------------------------------------------------
# Hierarchical routing: lapisan LOCAL (tile) dan BASE (graf utama seluruh Jawa)
# ---------------------------------------------------------------------------

_tiles_manifest_cache = None
_tiles_manifest_ready = False


def _tiles_manifest() -> dict | None:
    """Baca manifest grid tile (dari scripts/split_tiles.py). None = tidak ada."""
    global _tiles_manifest_cache, _tiles_manifest_ready
    if _tiles_manifest_ready:
        return _tiles_manifest_cache
    _tiles_manifest_cache = None
    _tiles_manifest_ready = True
    if not _TILES_ENABLED:
        return None
    path = os.path.join(_TILES_DIR, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            m = json.load(fh)
        if not m.get("pbf_id") or not m.get("stem"):
            return None
        # Tile basi bila PBF sumbernya berubah (pbf_id menyandikan nama+mtime).
        current = None
        for entry in discover_pbfs():
            if entry.stem == m.get("stem"):
                current = entry
                break
        if current is not None and current.pbf_id != m.get("pbf_id"):
            logger.warning(
                "Tile basi untuk %s (pbf_id berubah). Jalankan "
                "scripts/split_tiles.py ulang.", m.get("stem"))
            return None
        _tiles_manifest_cache = m
    except Exception as exc:
        logger.debug("Manifest tile tidak terbaca: %s", exc)
    return _tiles_manifest_cache


def tiles_enabled() -> bool:
    return _TILES_ENABLED and _tiles_manifest() is not None


def _tile_files_for_point(lat: float, lon: float, radius: int) -> list:
    """Daftar file tile yang menutup area (radius) di sekitar titik."""
    m = _tiles_manifest()
    if m is None:
        return []
    deg = m["deg"]
    buf = m["buffer_deg"]
    olat = m["origin_lat"]
    olon = m["origin_lon"]
    minlon, minlat, maxlon, maxlat = _pbf_bbox(lat, lon, radius)
    minlat -= buf
    minlon -= buf
    maxlat += buf
    maxlon += buf
    r0 = int((minlat - olat) // deg)
    r1 = int((maxlat - olat) // deg)
    c0 = int((minlon - olon) // deg)
    c1 = int((maxlon - olon) // deg)
    base = os.path.join(_TILES_DIR, m["stem"])
    out = []
    for row in range(r0, r1 + 1):
        for col in range(c0, c1 + 1):
            p = os.path.join(
                base, "tile_r%03d_c%03d.osm.pbf" % (row, col))
            if os.path.exists(p):
                out.append(p)
    return out


def _tile_tag(level: int) -> str:
    m = _tiles_manifest()
    stem = m["stem"] if m else "tile"
    return f"tile_{stem}_l{level}"


def _load_local_raw(lat: float, lon: float, radius: int, level: int = 1):
    """Baca graf jalan (level 1 = semua _DRIVE_HIGHWAYS) dari tile terdekat."""
    tiles = _tile_files_for_point(lat, lon, radius)
    if not tiles:
        raise AreaNotCoveredError(
            "Tidak ada tile lokal untuk area ini. Jalankan "
            "scripts/split_tiles.py terlebih dahulu.")
    box = _pbf_bbox(lat, lon, radius)
    graph = {}
    locations = {}
    t_tile = perf_counter()
    for path in tiles:
        g, loc = load_graph_from_pbf(
            path, bbox=box, highway_filter=_LEVEL_HIGHWAYS[level])
        graph.update(g)
        locations.update(loc)
    _perf("Tile Scan", t_tile)
    if not graph:
        raise AreaNotCoveredError(
            "Tidak ada data jalan di tile sekitar titik yang diminta.")
    m = _tiles_manifest()
    stem = m["stem"] if m else "tile"
    return graph, locations, f"tile:{stem}:l{level}", None


def load_local_graph_point(lat: float, lon: float,
                           radius: int = _LOCAL_RADIUS,
                           level: int = 1) -> PathGraph:
    """PathGraph lokal (gang) dari tile di sekitar titik, dicache disk+RAM."""
    if not _TILES_ENABLED:
        raise AreaNotCoveredError(
            "Tile lokal dinonaktifkan (TILES_ENABLED=0).")
    tag = _tile_tag(level)
    key = _cache_key(lat, lon, radius, tag)
    lat, lon = key[0], key[1]

    with _CACHE_LOCK:
        if key in _PG_CACHE:
            return _PG_CACHE[key]

    path = _pg_disk_path(key)
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                pg = pickle.load(fh)
            if getattr(pg, "bbox", None) is None:
                pg.bbox = _pbf_bbox(lat, lon, radius)
            with _CACHE_LOCK:
                _PG_CACHE[key] = pg
            return pg
        except Exception as exc:
            logger.warning("Cache PathGraph lokal tidak terbaca: %s", exc)

    graph, locations, source, warning = _load_local_raw(lat, lon, radius, level)
    t_build = perf_counter()
    pg = build_path_graph(graph, locations, lat, lon, enable_ch=_ENABLE_CH)
    _perf("PathGraph Local Build", t_build)
    pg.radius = int(radius)
    pg.source = source
    pg.warning = warning
    pg.bbox = _pbf_bbox(lat, lon, radius)

    with _CACHE_LOCK:
        _PG_CACHE[key] = pg
    _save_disk(path, pg)
    return pg


# --- Base graph (jalan utama seluruh Jawa, level 3) ---

_base_lock = threading.Lock()
_base_pg: PathGraph | None = None


def base_pickle_path() -> str | None:
    """Lokasi pickle base graph bila ada (cocok dgn PBF aktif)."""
    suffix = "_l%d.pkl" % _BASE_LEVEL
    prefix = "base_v3_"
    try:
        names = [n for n in os.listdir(_DISK_CACHE_DIR)
                 if n.startswith(prefix) and n.endswith(suffix)]
    except OSError:
        return None
    if not names:
        return None
    ids = {e.pbf_id for e in discover_pbfs()}
    for name in names:
        if name[len(prefix):-len(suffix)] in ids:
            return os.path.join(_DISK_CACHE_DIR, name)
    names.sort()
    return os.path.join(_DISK_CACHE_DIR, names[-1])


def base_available() -> bool:
    return _BASE_GRAPH_ENABLED and base_pickle_path() is not None


def hierarchical_available(lat1: float, lon1: float,
                           lat2: float, lon2: float) -> bool:
    """True bila lapisan hierarchical (tile+base) siap utk pasangan O->D."""
    if not tiles_enabled() or not base_available():
        return False
    m = _tiles_manifest()
    if not m:
        return False
    entry = None
    for e in discover_pbfs():
        if e.pbf_id == m["pbf_id"]:
            entry = e
            break
    if entry is None:
        return False
    return (entry.contains(lat1, lon1) and entry.contains(lat2, lon2))


def base_loaded() -> bool:
    return _base_pg is not None


def base_bbox() -> tuple | None:
    return getattr(_base_pg, "bbox", None)


def load_base_graph() -> PathGraph:
    """Muat base graph (lazy, thread-safe). Dipanggil dari threadpool."""
    global _base_pg
    if _base_pg is not None:
        return _base_pg
    with _base_lock:
        if _base_pg is not None:
            return _base_pg
        if not _BASE_GRAPH_ENABLED:
            raise AreaNotCoveredError(
                "Base graph dinonaktifkan (BASE_GRAPH_ENABLED=0).")
        path = base_pickle_path()
        if path is None:
            raise AreaNotCoveredError(
                "Base graph belum tersedia. Jalankan "
                "scripts/build_base_graph.py sekali, lalu kirim hasilnya "
                "bersama data/pbf ke server.")
        t_load = perf_counter()
        with open(path, "rb") as fh:
            pg = pickle.load(fh)
        _perf("Base Graph Load", t_load)
        _base_pg = pg
        logger.info("[PERF] Base graph dimuat dari %s", path)
        return pg


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
