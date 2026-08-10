"""Multi-route options & incident extraction (docs/feature/route_options.md).

Helper murni (tanpa I/O) yang dipakai endpoint /find-route-options:

- bump_penalties : penalti alternatif dengan menaikkan bobot edge rute
  yang sudah dipilih (penalty-bump) agar rute 2/3 mengambil jalur berbeda.
- max_overlap    : IoU node-set dua rute untuk dedupe (skip bila hampir sama).
- route_summary  : ringkasan jalan utama dari kelas highway OSM dominan.
- route_incidents: roadClosure / kemacetan (delay > ambang) di sepanjang rute.

Edge_id berbasis Cantor pairing node OSM sehingga konsisten lintas graf
(base/region/tile) — lihat app/services/traffic/matcher.py.
"""

import logging
import math

from app.services.pathfinding.core_a_star import edge_id, haversine_distance

logger = logging.getLogger("pathfinding")

_CLASS_LABELS = {
    "motorway": "Jalan Tol",
    "motorway_link": "Jalan Tol",
    "trunk": "Arteri Utama",
    "trunk_link": "Arteri Utama",
    "primary": "Jalan Nasional",
    "primary_link": "Jalan Nasional",
    "secondary": "Jalan Provinsi",
    "secondary_link": "Jalan Provinsi",
    "tertiary": "Jalan Kabupaten",
    "tertiary_link": "Jalan Kabupaten",
    "unclassified": "Jalan Lokal",
    "residential": "Jalan Lokal",
    "service": "Jalan Lokal",
    "living_street": "Jalan Lokal",
    "road": "Jalan Lokal",
}

_MIN_CLASS_SHARE = 0.15


def route_edges(node_sequence: list) -> set[int]:
    """edge_id untuk tiap pasangan node berurutan pada rute."""
    if not node_sequence or len(node_sequence) < 2:
        return set()
    return {
        edge_id(node_sequence[i], node_sequence[i + 1])
        for i in range(len(node_sequence) - 1)
    }


def _is_inf(value) -> bool:
    return isinstance(value, float) and math.isinf(value)


def bump_penalties(base: dict | None,
                   used_edge_sets: list[set],
                   bump: float = 8.0) -> dict:
    """Salin `base` lalu naikkan bobot semua edge rute yang sudah terpilih.

    Edge dengan penalty inf (blokir mode kendaraan / road closure) DIJAGA inf
    agar tidak membatalkan pembatasan; edge lain diset ke max(nilai lama, bump).
    Kembalikan dict baru (tidak memodifikasi `base`).
    """
    out = dict(base) if base else {}
    for edges in used_edge_sets:
        for e in edges:
            cur = out.get(e)
            if _is_inf(cur):
                continue
            out[e] = max(cur, bump) if cur is not None else bump
    return out


def max_overlap(ns_a: list, ns_b: list) -> float:
    """IoU (min-normalized) himpunan node dua rute; 1.0 = identik."""
    if not ns_a or not ns_b:
        return 0.0
    sa, sb = set(ns_a), set(ns_b)
    inter = len(sa & sb)
    return inter / max(1, min(len(sa), len(sb)))


def plan_graphs(plan) -> list:
    """Graf penyusun plan: 3 graf utk hierarchical, 1 utk graph biasa."""
    if plan[0] == "hierarchical":
        return [plan[1].local_origin, plan[1].local_dest, plan[1].base]
    return [plan[1]]


def merge_edge_classes(graphs: list) -> dict:
    """Gabung edge_classes seluruh graf plan menjadi satu dict edge_id->kelas."""
    merged: dict = {}
    for pg in graphs:
        ec = getattr(pg, "edge_classes", None) or {}
        merged.update(ec)
    return merged


def route_summary(node_sequence: list,
                  route_coords: list,
                  edge_classes: dict) -> str:
    """Ringkasan jalan utama: label kelas highway dominan (dibobot jarak).

    Kelas dengan share >= 15% dari panjang rute ditampilkan, urut terbesar,
    dipisah ' + '. Tanpa data kelas -> 'Rute utama'.
    """
    if not node_sequence or len(node_sequence) < 2 or not edge_classes:
        return "Rute utama"
    share: dict[str, float] = {}
    total = 0.0
    for i in range(len(node_sequence) - 1):
        eid = edge_id(node_sequence[i], node_sequence[i + 1])
        cls = edge_classes.get(eid)
        if cls is None:
            continue
        length = 0.0
        if i < len(route_coords) - 1:
            length = haversine_distance(route_coords[i], route_coords[i + 1])
        if length <= 0:
            length = 1.0
        label = _CLASS_LABELS.get(cls, cls.replace("_", " ").title())
        share[label] = share.get(label, 0.0) + length
        total += length
    if total <= 0:
        return "Rute utama"
    ranked = sorted(share.items(), key=lambda item: -item[1])
    parts = [
        label for label, length in ranked if length / total >= _MIN_CLASS_SHARE
    ]
    if not parts:
        return ranked[0][0] if ranked else "Rute utama"
    return " + ".join(parts)


def _midpoint(coords: list) -> tuple[float, float]:
    if not coords:
        return (0.0, 0.0)
    return coords[len(coords) // 2]


def route_incidents(node_sequence: list,
                    route_coords: list,
                    penalties: dict | None,
                    speed_kmh: float,
                    delay_minutes: float = 3.0) -> list[dict]:
    """Insiden di sepanjang polyline rute.

    - penalty == inf              -> road_closure (penutupan jalan).
    - penalty > 1 dgn tundaan     -> congestion, bila tundaan > delay_minutes.
      Tundaan edge = (panjang/(speed/3.6)) * (multiplier - 1).

    Segmen edge terdampak yang berdekatan digabung jadi satu insiden.
    Kembalikan list dict siap untuk schema RouteIncident.
    """
    if not node_sequence or len(node_sequence) < 2 or not penalties:
        return []
    if speed_kmh <= 0:
        speed_kmh = 40.0
    threshold_s = delay_minutes * 60.0
    incidents: list[dict] = []
    cur = None

    def flush():
        nonlocal cur
        if cur is None:
            return
        incidents.append({
            "type": cur["type"],
            "location": _midpoint(cur["coordinates"]),
            "coordinates": list(cur["coordinates"]),
            "multiplier": cur["multiplier"],
            "delay_minutes": round(cur["delay"], 1),
            "description": cur["description"],
            "provider": None,
        })
        cur = None

    for i in range(len(node_sequence) - 1):
        eid = edge_id(node_sequence[i], node_sequence[i + 1])
        mult = penalties.get(eid)
        p0, p1 = route_coords[i], route_coords[i + 1]
        if mult is None or mult <= 1.0:
            flush()
            continue
        length = haversine_distance(p0, p1) if i < len(route_coords) - 1 else 0.0
        if _is_inf(mult):
            if cur is not None and cur["type"] == "road_closure":
                cur["coordinates"].append(p1)
                cur["multiplier"] = max(cur["multiplier"], float("inf"))
            else:
                flush()
                cur = {
                    "type": "road_closure",
                    "coordinates": [p0, p1],
                    "multiplier": float("inf"),
                    "delay": 0.0,
                    "description": "Penutupan jalan (road closure) di sepanjang rute",
                }
            continue
        base_time = length / (speed_kmh / 3.6)
        delay = base_time * (mult - 1.0)
        if delay < threshold_s:
            flush()
            continue
        if cur is not None and cur["type"] == "congestion":
            cur["coordinates"].append(p1)
            cur["multiplier"] = max(cur["multiplier"], mult)
            cur["delay"] += delay
        else:
            flush()
            cur = {
                "type": "congestion",
                "coordinates": [p0, p1],
                "multiplier": mult,
                "delay": delay,
                "description": None,
            }
    flush()

    for inc in incidents:
        if inc["type"] == "congestion":
            inc["description"] = (
                "Kemacetan, tundaan +%.0f mnt (multiplier %.1fx)" % (
                    inc["delay_minutes"], inc["multiplier"]))
    return incidents
