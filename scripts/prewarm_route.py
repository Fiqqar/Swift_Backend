"""Pre-warm cache disk untuk satu pasangan origin->dest (mis. rute antar-kota).

Gunakan untuk area di luar REGION_GRAPH_BBOX (mis. Jakarta->Surabaya) agar
server tidak melakukan cold-build saat runtime (yang bisa memutus koneksi
browser):

    python scripts/prewarm_route.py -6.17 106.83 -7.26 112.75

Cache yang dihasilkan (di cache/pathfinding/) akan dipakai otomatis oleh
_find-route saat pasangan yang sama diminta.
"""

import os
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from app.services.pathfinding.graph_loader import (  # noqa: E402
    load_graph_covering,
    region_graph_cached,
)


def main() -> None:
    args = sys.argv[1:]
    if len(args) != 4:
        print(__doc__)
        print("Pemakaian: python scripts/prewarm_route.py "
              "<lat1> <lon1> <lat2> <lon2>")
        sys.exit(2)
    try:
        lat1, lon1, lat2, lon2 = (float(v) for v in args)
    except ValueError:
        print("Koordinat harus berupa angka.", file=sys.stderr)
        sys.exit(2)

    print(f"Pre-warm rute ({lat1},{lon1}) -> ({lat2},{lon2})")
    if region_graph_cached(lat1, lon1, lat2, lon2):
        print("Sudah ada di cache disk, lewati. "
              "(Hapus file pg_v3_* di cache/pathfinding jika ingin build ulang.)")
        return

    t0 = time.perf_counter()
    pg = load_graph_covering(lat1, lon1, lat2, lon2)
    elapsed = time.perf_counter() - t0
    print(f"=== Selesai dalam {elapsed:.0f} detik ===")
    print(f"  source : {pg.source}")
    print(f"  radius : {pg.radius} m")
    print(f"  node   : {len(pg.graph)}")
    print(f"  edge   : {sum(len(v) for v in pg.graph.values())}")
    print(f"  directed: {pg.directed}")
    print("Cache disk siap. Server akan memuatnya otomatis untuk pasangan ini.")


if __name__ == "__main__":
    main()
