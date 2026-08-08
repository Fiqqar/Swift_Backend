"""Split PBF Jawa menjadi grid tile kecil (sekali, offline).

Tile dipakai oleh lapisan 'local' pada hierarchical routing: graf gang
(level 1, semua _DRIVE_HIGHWAYS) di sekitar origin/tujuan dibangun dengan
memindai hanya file tile penutup area, bukan seluruh PBF 854 MB (14-19 mnt).

    python scripts/split_tiles.py

Dua pass (tanpa lokasi-index temp di disk):
  Pass 1: baca hanya WAY -> kumpulkan node id jalan.
  Pass 2: baca NODE+WAY -> tulis node jalan + way ke tile penutup.

Output: <TILES_DIR>/<stem>/tile_rRRR_cCCC.osm.pbf + <TILES_DIR>/manifest.json
Pengaturan grid disimpan di manifest dan dipakai graph_loader.
Sesuaikan via env: TILES_DIR, TILE_DEG, TILE_BUFFER_DEG.
"""

import json
import logging
import math
import os
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

import osmium  # noqa: E402

from app.services.pathfinding.graph_loader import (  # noqa: E402
    _DRIVE_HIGHWAYS,
)
from app.services.pathfinding.pbf_registry import (  # noqa: E402
    discover_pbfs,
)

logger = logging.getLogger("split_tiles")

_TILES_DIR = os.environ.get("TILES_DIR", os.path.join("data", "tiles"))
_TILE_DEG = float(os.environ.get("TILE_DEG", "0.2"))
_TILE_BUFFER_DEG = float(os.environ.get("TILE_BUFFER_DEG", "0.01"))
_PBF_KEY = os.environ.get("SPLIT_PBF_KEY", "java").strip().lower()


def _pick_pbf():
    for entry in discover_pbfs():
        if _PBF_KEY in entry.stem.lower():
            return entry
    raise SystemExit(
        f"Tidak ada PBF berisi '{_PBF_KEY}' di PBF_DIR. "
        "Cek file di data/pbf atau set SPLIT_PBF_KEY.")


class _NodeView:
    __slots__ = ("id", "location")

    def __init__(self, nid, location):
        self.id = nid
        self.location = location


class _WayCollector(osmium.SimpleHandler):
    """Pass 1: kumpulkan node id dari semua road-way."""

    def __init__(self):
        super().__init__()
        self.road_nodes = set()
        self.road_ways = 0

    def way(self, w):
        tags = dict(w.tags)
        if tags.get("highway") not in _DRIVE_HIGHWAYS:
            return
        if tags.get("area") == "yes":
            return
        self.road_ways += 1
        for nd in w.nodes:
            self.road_nodes.add(nd.ref)
        if self.road_ways % 200000 == 0:
            logger.info("  pass1: %d way, %d node", 
                        self.road_ways, len(self.road_nodes))


class _TileWriter(osmium.SimpleHandler):
    """Pass 2: tulis road-way + node-nya ke tile penutup."""

    def __init__(self, road_nodes, grid, pool):
        super().__init__()
        self.road_nodes = road_nodes
        self.grid = grid
        self.pool = pool
        self.coords = {}
        self.writers = {}
        self.nodes_written = 0
        self.ways_written = 0

    def node(self, n):
        if n.id in self.road_nodes:
            self.coords[n.id] = (n.location.lat, n.location.lon)

    def _writer(self, row, col):
        key = (row, col)
        w = self.writers.get(key)
        if w is None:
            out_dir = self.grid["out_dir"]
            os.makedirs(out_dir, exist_ok=True)
            header = osmium.io.Header()
            lat0 = self.grid["origin_lat"] + row * self.grid["deg"]
            lon0 = self.grid["origin_lon"] + col * self.grid["deg"]
            header.add_box(osmium.osm.Box(
                osmium.osm.Location(lon0, lat0),
                osmium.osm.Location(lon0 + self.grid["deg"],
                                    lat0 + self.grid["deg"])))
            w = osmium.SimpleWriter(
                os.path.join(out_dir, "tile_r%03d_c%03d.osm.pbf" % (row, col)),
                bufsz=1024 * 1024, header=header, overwrite=True,
                thread_pool=self.pool)
            self.writers[key] = w
        return w

    def _row_col(self, lat, lon):
        r = int((lat - self.grid["origin_lat"]) // self.grid["deg"])
        c = int((lon - self.grid["origin_lon"]) // self.grid["deg"])
        return r, c

    def _in_tile(self, latlon, row, col):
        deg = self.grid["deg"]
        buf = self.grid["buffer_deg"]
        lat0 = self.grid["origin_lat"] + row * deg - buf
        lon0 = self.grid["origin_lon"] + col * deg - buf
        lat, lon = latlon
        return (lat0 <= lat <= lat0 + deg + 2 * buf
                and lon0 <= lon <= lon0 + deg + 2 * buf)

    def way(self, w):
        tags = dict(w.tags)
        if tags.get("highway") not in _DRIVE_HIGHWAYS:
            return
        if tags.get("area") == "yes":
            return
        refs = []
        minlat = minlon = float("inf")
        maxlat = maxlon = -float("inf")
        coords = self.coords
        for nd in w.nodes:
            latlon = coords.get(nd.ref)
            if latlon is None:
                continue
            refs.append((nd.ref, latlon))
            lat, lon = latlon
            minlat = min(minlat, lat)
            maxlat = max(maxlat, lat)
            minlon = min(minlon, lon)
            maxlon = max(maxlon, lon)
        if not refs:
            return
        buf = self.grid["buffer_deg"]
        r0, c0 = self._row_col(minlat - buf, minlon - buf)
        r1, c1 = self._row_col(maxlat + buf, maxlon + buf)
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                in_tile = [(rid, ll) for rid, ll in refs
                           if self._in_tile(ll, row, col)]
                if not in_tile:
                    continue
                wtr = self._writer(row, col)
                for rid, ll in in_tile:
                    wtr.add_node(
                        _NodeView(rid, osmium.osm.Location(ll[1], ll[0])))
                    self.nodes_written += 1
                wtr.add_way(w)
                self.ways_written += 1
        if self.ways_written % 50000 == 0:
            logger.info("  pass2: %d way ditulis (%d node)",
                        self.ways_written, self.nodes_written)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    pbf = _pick_pbf()
    bbox = pbf.bbox
    minlon, minlat, maxlon, maxlat = bbox

    origin_lat = math.floor((minlat - _TILE_BUFFER_DEG) / _TILE_DEG) * _TILE_DEG
    origin_lon = math.floor((minlon - _TILE_BUFFER_DEG) / _TILE_DEG) * _TILE_DEG

    out_dir = os.path.join(_TILES_DIR, pbf.stem)
    grid = {
        "pbf": pbf.basename,
        "pbf_id": pbf.pbf_id,
        "stem": pbf.stem,
        "deg": _TILE_DEG,
        "buffer_deg": _TILE_BUFFER_DEG,
        "origin_lat": origin_lat,
        "origin_lon": origin_lon,
        "bbox": list(bbox),
        "out_dir": out_dir,
    }
    manifest_path = os.path.join(_TILES_DIR, "manifest.json")
    os.makedirs(_TILES_DIR, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in grid.items() if k != "out_dir"},
                  fh, ensure_ascii=False, indent=2)

    pool = osmium.io.ThreadPool()

    logger.info("Split %s -> %s (deg=%.3f, buffer=%.3f)",
                pbf.basename, out_dir, _TILE_DEG, _TILE_BUFFER_DEG)
    t0 = time.perf_counter()

    logger.info("Pass 1: kumpulkan node jalan...")
    reader = osmium.io.Reader(pbf.path, types=osmium.osm.WAY, thread_pool=pool)
    collector = _WayCollector()
    osmium.apply(reader, collector)
    reader.close()
    t_pass1 = time.perf_counter()
    logger.info("  pass1 selesai: %d way, %d node (%.0f detik)",
                collector.road_ways, len(collector.road_nodes),
                t_pass1 - t0)

    logger.info("Pass 2: tulis tile...")
    reader = osmium.io.Reader(
        pbf.path, types=osmium.osm.NODE | osmium.osm.WAY, thread_pool=pool)
    writer = _TileWriter(collector.road_nodes, grid, pool)
    osmium.apply(reader, writer)
    reader.close()
    for w in writer.writers.values():
        w.close()
    t_pass2 = time.perf_counter()
    logger.info("  pass2 selesai dalam %.0f detik", t_pass2 - t_pass1)

    total_bytes = 0
    n_tiles = 0
    for name in os.listdir(out_dir):
        n_tiles += 1
        total_bytes += os.path.getsize(os.path.join(out_dir, name))
    logger.info("=== Split selesai dalam %.0f detik ===", t_pass2 - t0)
    logger.info("  tile  : %d file (%.1f MB) di %s",
                n_tiles, total_bytes / 1048576.0, out_dir)
    logger.info("  way   : %d ditulis, node: %d",
                writer.ways_written, writer.nodes_written)
    logger.info("  manifest: %s", manifest_path)
    os._exit(0)


if __name__ == "__main__":
    main()
