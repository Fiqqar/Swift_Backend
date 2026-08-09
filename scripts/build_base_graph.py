"""Pre-build graf dasar (level 3: motorway/trunk/primary) seluruh Jawa.

Graf ini adalah lapisan tengah hierarchical routing: semua rute antar-kota
di Jawa lewat sini. Dibangun SEKALI offline (scan PBF 895 MB ~19 mnt) lalu
dimuat lazy ke RAM saat request jarak jauh pertama.

    python scripts/build_base_graph.py

Output: <OSMNX_DISK_CACHE>/base_v3_<pbf_id>_l3.pkl (dipakai otomatis oleh
app.services.pathfinding.graph_loader.load_base_graph).
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from app.services.pathfinding.core_a_star import haversine_distance  # noqa: E402
from app.services.pathfinding.graph_loader import (  # noqa: E402
    _DISK_CACHE_DIR,
    _LEVEL_HIGHWAYS,
    load_graph_from_pbf,
)
from app.services.pathfinding.pbf_registry import (  # noqa: E402
    discover_pbfs,
)
from app.services.pathfinding.preprocess import build_path_graph  # noqa: E402

logger = logging.getLogger("build_base_graph")

_LEVEL = int(os.environ.get("BASE_GRAPH_LEVEL", "3"))
_PBF_KEY = os.environ.get("SPLIT_PBF_KEY", "java").strip().lower()


def _pick_pbf():
    for entry in discover_pbfs():
        if _PBF_KEY in entry.stem.lower():
            return entry
    raise SystemExit(
        f"Tidak ada PBF berisi '{_PBF_KEY}' di PBF_DIR. "
        "Cek file di data/pbf.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    pbf = _pick_pbf()
    out_path = os.path.join(
        _DISK_CACHE_DIR, "base_v3_%s_l%d.pkl" % (pbf.pbf_id, _LEVEL))

    if os.path.exists(out_path):
        print("Base graph sudah ada: %s" % out_path)
        print("Hapus file tsb jika ingin build ulang.")
        os._exit(0)

    os.makedirs(_DISK_CACHE_DIR, exist_ok=True)
    bbox = pbf.bbox
    minlon, minlat, maxlon, maxlat = bbox
    ref_lat = (minlat + maxlat) / 2.0
    ref_lon = (minlon + maxlon) / 2.0

    print("Build base graph level=%d dari %s (bbox=%s)..." % (
        _LEVEL, pbf.basename, bbox))
    t0 = time.perf_counter()
    graph, locations, edge_classes = load_graph_from_pbf(
        pbf.path, bbox=bbox, highway_filter=_LEVEL_HIGHWAYS[_LEVEL])
    t_scan = time.perf_counter()
    print("  scan PBF selesai dalam %.0f detik "
          "(node=%d, edge=%d)" % (
              t_scan - t0, len(graph),
              sum(len(v) for v in graph.values())))

    pg = build_path_graph(graph, locations, ref_lat, ref_lon,
                          landmarks_k=0, enable_ch=False,
                          edge_classes=edge_classes)
    t_build = time.perf_counter()
    print("  build PathGraph selesai dalam %.0f detik" % (t_build - t_scan))

    corner = (maxlat, maxlon)
    radius = int(haversine_distance((ref_lat, ref_lon), corner)) + 1000
    pg.radius = radius
    pg.source = "base:%s:l%d" % (pbf.stem, _LEVEL)
    pg.warning = None
    pg.bbox = bbox

    tmp = out_path + ".tmp"
    import pickle
    with open(tmp, "wb") as fh:
        pickle.dump(pg, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out_path)
    print("=== Selesai dalam %.0f detik ===" % (time.perf_counter() - t0))
    print("  node    : %d" % len(pg.graph))
    print("  edge    : %d" % sum(len(v) for v in pg.graph.values()))
    print("  directed: %s" % pg.directed)
    print("  pickle  : %.1f MB" % (os.path.getsize(out_path) / 1048576.0))
    print("  path    : %s" % out_path)

    # Validasi kecil: rute antar-kota contoh di graf base.
    from app.services.pathfinding.core_engine import route as engine_route
    from app.services.pathfinding.graph_loader import find_nearest_node
    start = find_nearest_node(-6.8048, 110.8385, pg.locations)   # Kudus
    goal = find_nearest_node(-6.1751, 106.8650, pg.locations)    # Jakarta
    if start is None or goal is None:
        print("  (validasi rute dilewati: titik contoh tidak di graf)")
        os._exit(0)
    t1 = time.perf_counter()
    node_path, total = engine_route(pg, start, goal)
    print("  rute contoh Kudus->Jakarta: %d node, %.0f m, %.2f detik" % (
        len(node_path) if node_path else 0, total,
        time.perf_counter() - t1))
    os._exit(0)


if __name__ == "__main__":
    main()
