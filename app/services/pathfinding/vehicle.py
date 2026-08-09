"""Vehicle transport mode: blokir edge yang tidak diizinkan per mode.

Mode kendaraan (motorcycle/car/truck) memetakan tag highway OSM
(`edge_classes` pada PathGraph) ke set kelas jalan yang diizinkan. Edge
yang tidak diizinkan diberi penalty inf sehingga routing engine
menghindarinya. Lihat docs/feature/verhicle_transport.md.
"""

_MOTORCYCLE = {
    "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link",
    "unclassified", "residential", "service", "living_street", "road",
}

_CAR = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "service", "living_street", "road",
}

_TRUCK = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link",
}

_ALLOWED_BY_MODE = {
    "motorcycle": _MOTORCYCLE,
    "car": _CAR,
    "truck": _TRUCK,
}


def _pg_edge_classes(pg) -> dict:
    return getattr(pg, "edge_classes", None) or {}


def blocked_penalties_for(plan, mode: str) -> dict:
    """Kembalikan {edge_id: inf} untuk edge yang dilarang mode `mode`.

    `plan` adalah hasil `_resolve_plan`: ("graph", PathGraph) atau
    ("hierarchical", HierarchicalResult). Mode tak dikenal diperlakukan
    sebagai "car" (tanpa blokir). Edge tanpa info kelas jalan tidak
    diblokir agar graf tetap tersambung.
    """
    allowed = _ALLOWED_BY_MODE.get(mode, _CAR)

    if plan[0] == "hierarchical":
        graphs = [plan[1].local_origin, plan[1].local_dest, plan[1].base]
    else:
        graphs = [plan[1]]

    blocked = {}
    for pg in graphs:
        edge_classes = _pg_edge_classes(pg)
        if not edge_classes:
            continue
        for eid, cls in edge_classes.items():
            if cls not in allowed:
                blocked[eid] = float("inf")
    return blocked
