"""Registry of local .osm.pbf files.

Setiap file PBF regional (java, sumatera, sulawesi, dst) dideteksi otomatis
dari direktori PBF_DIR (default: data/pbf). Bounding box wilayah dibaca dari
header file PBF via pyosmium (murah), dipakai untuk memilih PBF yang tepat
per-request dan untuk identitas cache (pbf_id).

pbf_id menyandikan nama file + mtime + ukuran sehingga mengganti/memperbarui
file PBF otomatis membatalkan cache lama yang tersimpan di disk.
"""

import logging
import math
import os
import threading
from dataclasses import dataclass

logger = logging.getLogger("pathfinding")

_DEFAULT_PBF_DIR = os.path.join("data", "pbf")
_ENV_PBF_DIR = os.environ.get("PBF_DIR", "").strip()
_ENV_PBF_FILE = os.environ.get("OSM_PBF_FILE_PATH", "").strip()


@dataclass(frozen=True)
class PbfEntry:
    path: str
    basename: str
    stem: str
    size: int
    mtime_ns: int
    bbox: tuple
    pbf_id: str

    @property
    def minlon(self) -> float:
        return self.bbox[0]

    @property
    def minlat(self) -> float:
        return self.bbox[1]

    @property
    def maxlon(self) -> float:
        return self.bbox[2]

    @property
    def maxlat(self) -> float:
        return self.bbox[3]

    def contains(self, lat: float, lon: float) -> bool:
        return (self.minlon <= lon <= self.maxlon
                and self.minlat <= lat <= self.maxlat)

    @property
    def area_deg2(self) -> float:
        return (self.maxlon - self.minlon) * (self.maxlat - self.minlat)


def pbf_dir() -> str:
    if _ENV_PBF_DIR:
        return _ENV_PBF_DIR
    if _ENV_PBF_FILE:
        return os.path.dirname(_ENV_PBF_FILE) or "."
    return _DEFAULT_PBF_DIR


def _read_header_box(path: str) -> tuple | None:
    """Baca bbox (minlon, minlat, maxlon, maxlat) dari header file PBF."""
    from osmium import apply as osmium_apply
    from osmium import io as osmium_io

    try:
        reader = osmium_io.Reader(path)
        try:
            box = reader.header().box()
        finally:
            reader.close()
        if box is not None and box.valid():
            bl, tr = box.bottom_left, box.top_right
            return (bl.lon, bl.lat, tr.lon, tr.lat)
    except Exception as exc:
        logger.warning("Gagal membaca header PBF %s: %s", path, exc)
    return None


def _scan_bbox(path: str) -> tuple | None:
    """Fallback: hitung bbox dengan memindai seluruh node (lambat)."""
    import osmium
    from osmium import io as osmium_io

    class _MinMax(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.minlon = 180.0
            self.minlat = 90.0
            self.maxlon = -180.0
            self.maxlat = -90.0

        def node(self, n):
            if n.location.valid():
                self.minlon = min(self.minlon, n.location.lon)
                self.maxlon = max(self.maxlon, n.location.lon)
                self.minlat = min(self.minlat, n.location.lat)
                self.maxlat = max(self.maxlat, n.location.lat)

    try:
        handler = _MinMax()
        osmium.apply(osmium_io.Reader(path), handler)
        return (handler.minlon, handler.minlat, handler.maxlon, handler.maxlat)
    except Exception as exc:
        logger.warning("Gagal memindai bbox PBF %s: %s", path, exc)
    return None


def _file_signature(pbf_dir_path: str) -> tuple:
    """Tuple (name, size, mtime_ns) semua *.osm.pbf di direktori."""
    try:
        entries = []
        for name in os.listdir(pbf_dir_path):
            if not name.endswith(".osm.pbf"):
                continue
            st = os.stat(os.path.join(pbf_dir_path, name))
            entries.append((name, st.st_size, st.st_mtime_ns))
        return tuple(sorted(entries))
    except OSError:
        return ()


_registry_lock = threading.RLock()
_registry_cache = None
_registry_signature = None


def discover_pbfs() -> list:
    """Daftar PbfEntry terurut. Re-index hanya bila daftar file berubah."""
    global _registry_cache, _registry_signature
    directory = pbf_dir()
    sig = _file_signature(directory)

    with _registry_lock:
        if _registry_cache is not None and sig == _registry_signature:
            return list(_registry_cache)
        if not sig:
            logger.info("Tidak ada file *.osm.pbf di %s", directory)
            _registry_cache = []
            _registry_signature = sig
            return []

        entries = []
        for name, size, mtime_ns in sig:
            path = os.path.join(directory, name)
            bbox = _read_header_box(path)
            if bbox is None:
                bbox = _scan_bbox(path)
            if bbox is None:
                logger.warning("Lewati PBF tanpa bbox: %s", name)
                continue
            stem = name[: -len(".osm.pbf")] if name.endswith(".osm.pbf") else name
            pbf_id = f"{stem}__{mtime_ns}_{size}"
            entries.append(PbfEntry(
                path=path, basename=name, stem=stem, size=size,
                mtime_ns=mtime_ns, bbox=bbox, pbf_id=pbf_id,
            ))
        entries.sort(key=lambda e: e.basename)
        _registry_cache = entries
        _registry_signature = sig
        logger.info("PBF registry: %d file terdeteksi di %s",
                    len(entries), directory)
        return list(entries)


def refresh_registry() -> list:
    """Paksa re-index registry (mis. setelah file PBF baru ditambahkan)."""
    global _registry_cache, _registry_signature
    with _registry_lock:
        _registry_cache = None
        _registry_signature = None
    return discover_pbfs()


def select_pbf(lat1: float, lon1: float,
               lat2: float, lon2: float) -> PbfEntry | None:
    mid_lat = (lat1 + lat2) / 2.0
    mid_lon = (lon1 + lon2) / 2.0
    best = None
    best_score = None
    for entry in discover_pbfs():
        if not (entry.contains(lat1, lon1) and entry.contains(lat2, lon2)):
            continue
        margins = []
        for lat, lon in ((lat1, lon1), (lat2, lon2)):
            m = min(lon - entry.minlon, entry.maxlon - lon,
                    lat - entry.minlat, entry.maxlat - lat)
            margins.append(m)
        margin = min(margins)
        d = math.sqrt(
            (mid_lat - (entry.minlat + entry.maxlat) / 2.0) ** 2
            + (mid_lon - (entry.minlon + entry.maxlon) / 2.0) ** 2)
        score = (margin, -d, -entry.area_deg2)
        if best_score is None or score > best_score:
            best = entry
            best_score = score
    return best


def pbfs_available() -> bool:
    return bool(discover_pbfs())
