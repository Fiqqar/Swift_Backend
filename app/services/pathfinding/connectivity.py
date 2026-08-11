from app.services.pathfinding.core_a_star import edge_id
from app.services.pathfinding.snap import k_nearest_nodes

_BLOCKED = float("inf")


def _neighbors(graph: dict, u, blocked_edge_ids: set) -> list:
    if not blocked_edge_ids:
        return list(graph.get(u, ()))
    out = []
    for v in graph.get(u, ()):
        if edge_id(u, v) not in blocked_edge_ids:
            out.append(v)
    return out


def reachable(graph: dict, source, blocked_edge_ids: set | None = None) -> set:
    if source not in graph:
        return set()
    blocked = blocked_edge_ids or set()
    seen = {source}
    stack = [source]
    while stack:
        u = stack.pop()
        for v in _neighbors(graph, u, blocked):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return seen


def reaches(graph: dict, source, target,
            blocked_edge_ids: set | None = None) -> bool:
    if source not in graph or target not in graph:
        return False
    if source == target:
        return True
    blocked = blocked_edge_ids or set()
    seen = {source}
    stack = [source]
    while stack:
        u = stack.pop()
        for v in _neighbors(graph, u, blocked):
            if v == target:
                return True
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return False


def _reach_any_target(graph: dict, source, targets: set,
                      blocked_edge_ids: set | None = None) -> bool:
    if not targets:
        return False
    if len(targets) == 1:
        return reaches(graph, source, next(iter(targets)),
                       blocked_edge_ids)
    return bool(reachable(graph, source, blocked_edge_ids) & targets)


def resolve_goal(graph: dict, locations: dict, start_node, dest_coord: tuple,
                 goal_node, blocked_edge_ids: set | None = None,
                 k: int = 8, max_dist: float = 800.0):
    blocked = blocked_edge_ids or set()
    if goal_node in graph and reaches(graph, start_node, goal_node, blocked):
        return goal_node, None, False
    for nid, dist in k_nearest_nodes(dest_coord[0], dest_coord[1],
                                     locations, k=k, max_dist=max_dist):
        if nid == goal_node:
            continue
        if reaches(graph, start_node, nid, blocked):
            return nid, dist, True
    return None, None, True


def nearest_node_reaching(graph: dict, locations: dict, targets: set,
                          coord: tuple, blocked_edge_ids: set | None = None,
                          k: int = 8, max_dist: float = 800.0):
    if not targets:
        return None, None
    blocked = blocked_edge_ids or set()
    for nid, dist in k_nearest_nodes(coord[0], coord[1], locations,
                                     k=k, max_dist=max_dist):
        if nid not in graph:
            continue
        if _reach_any_target(graph, nid, targets, blocked):
            return nid, dist
    return None, None
