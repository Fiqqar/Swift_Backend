import logging

logger = logging.getLogger("pathfinding.vehicle")

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
    "secondary", "secondary_link", "tertiary", "tertiary_link",
}

_ALLOWED_BY_MODE = {
    "motorcycle": _MOTORCYCLE,
    "car": _CAR,
    "truck": _TRUCK,
}


def _pg_edge_classes(pg) -> dict:
    return getattr(pg, "edge_classes", None) or {}


def _log_stale_graph(pg, logger) -> None:
    try:
        if _pg_edge_classes(pg):
            return
        source = getattr(pg, "source", "?")
        if logger.isEnabledFor(logging.WARNING):
            logger.warning(
                "[vehicle] graf tanpa edge_classes (mode tidak tersaring), "
                "source=%s. Rebuild dengan scripts/build_base_graph.py.",
                source)
    except Exception:
        pass


def blocked_penalties_for(plan, mode: str) -> dict:
    allowed = _ALLOWED_BY_MODE.get(mode, _CAR)

    if plan[0] == "hierarchical":
        graphs = [plan[1].local_origin, plan[1].local_dest, plan[1].base]
    else:
        graphs = [plan[1]]

    blocked = {}
    for pg in graphs:
        edge_classes = _pg_edge_classes(pg)
        if not edge_classes:
            _log_stale_graph(pg, logger)
            continue
        for eid, cls in edge_classes.items():
            if cls not in allowed:
                blocked[eid] = float("inf")
    return blocked
