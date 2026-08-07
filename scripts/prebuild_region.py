"""Pre-build graf regional (jalan utama) ke cache disk secara offline.

Jalankan SEKALI sebelum menjalankan server agar rute jarak kota menengah
tidak memicu scan file PBF raksasa (bisa 5-10 menit) di runtime:

    python scripts/prebuild_region.py

Graf yang dihasilkan disimpan ke cache/pathfinding/pg_v3_*.pkl dan akan
dipakai ulang oleh app saat startup (lifespan) serta oleh /find-route.
Ubah wilayah lewat env REGION_GRAPH_BBOX, atau override nilai default di sini.
"""

import os
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from app.services.pathfinding.graph_loader import (  # noqa: E402
    DEFAULT_REGION_BBOX,
    REGION_LEVEL,
    load_graph_covering,
    region_graph_cached,
)


def _bbox() -> tuple:
    raw = os.environ.get("REGION_GRAPH_BBOX", "").strip()
    if raw:
        parts = [c.strip() for c in raw.split(",")]
        if len(parts) == 4:
            try:
                return tuple(float(c) for c in parts)
            except ValueError:
                pass
    return DEFAULT_REGION_BBOX


def main() -> None:
    lat1, lon1, lat2, lon2 = _bbox()
    print(f"Pre-build graf regional {lat1},{lon1} -> {lat2},{lon2} "
          f"(level={REGION_LEVEL})")

    if region_graph_cached(lat1, lon1, lat2, lon2, REGION_LEVEL):
        print("Sudah ada di cache disk, lewati. (Hapus file pg_v3_*_l2_* "
              "di cache/pathfinding jika ingin build ulang.)")
        return

    t0 = time.perf_counter()
    pg = load_graph_covering(lat1, lon1, lat2, lon2, REGION_LEVEL)
    elapsed = time.perf_counter() - t0
    print(f"=== Selesai dalam {elapsed:.0f} detik ===")
    print(f"  source : {pg.source}")
    print(f"  radius : {pg.radius} m")
    print(f"  node   : {len(pg.graph)}")
    print(f"  edge   : {sum(len(v) for v in pg.graph.values())}")
    print(f"  directed: {pg.directed}")
    print("Cache disk siap. Jalankan server untuk memuat graf ini.")

    # Cek kecil: jalankan rute contoh dalam graf untuk validasi.
    from app.services.pathfinding.core_engine import route as engine_route
    from app.services.pathfinding.graph_loader import find_nearest_node

    start = find_nearest_node(-6.8048, 110.8385, pg.locations)
    goal = find_nearest_node(-6.9667, 110.4167, pg.locations)
    if start is None or goal is None:
        print("  (validasi rute dilewati: titik contoh tidak ada di graf)")
        return
    t1 = time.perf_counter()
    node_path, total = engine_route(pg, start, goal)
    print(f"  rute contoh Kudus->Semarang: "
          f"{len(node_path) if node_path else 0} node, "
          f"{total:.0f} m, {time.perf_counter() - t1:.2f} detik")


if __name__ == "__main__":
    main()
