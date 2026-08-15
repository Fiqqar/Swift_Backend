import heapq
import math
import threading


def haversine_distance(coord1: tuple, coord2: tuple) -> float:
    R = 6371000
    lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
    lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])

    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def _closest_point_on_segment(p: tuple, a: tuple, b: tuple) -> tuple:
    coslat = math.cos(math.radians((a[0] + b[0]) / 2.0))
    ax, ay = a[1] * coslat, a[0]
    bx, by = b[1] * coslat, b[0]
    px, py = p[1] * coslat, p[0]
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return a
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    cx = ax + t * dx
    cy = ay + t * dy
    return (cy, cx / coslat)


def _snap_endpoint(graph: dict, locations: dict, lat: float, lon: float,
                   node_id: int) -> tuple:
    best = locations[node_id]
    best_d = haversine_distance((lat, lon), best)
    for nbr in graph.get(node_id, {}):
        proj = _closest_point_on_segment(
            (lat, lon), locations[node_id], locations[nbr])
        d = haversine_distance((lat, lon), proj)
        if d < best_d:
            best_d = d
            best = proj
    return best


def edge_id(u: int, v: int) -> int:
    a = u if u >= 0 else 2 * (-u) - 1
    b = v if v >= 0 else 2 * (-v) - 1
    return (a + b) * (a + b + 1) // 2 + b


def precompute_geo(locations: dict) -> dict:
    out = {}
    for node_id, (lat, lon) in locations.items():
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        out[node_id] = (lat_r, lon_r, math.sin(lat_r), math.cos(lat_r))
    return out


def _haversine_from_geo(g1: tuple, g2: tuple) -> float:
    R = 6371000.0
    lat1, lon1, _s1, c1 = g1
    lat2, lon2, _s2, c2 = g2
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (math.sin(dlat / 2) ** 2
         + c1 * c2 * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _astar(graph: dict, geo: dict, heuristic, start_node: int,
           goal_node: int, stats: dict | None = None,
           penalties: dict | None = None):
    if start_node not in graph or goal_node not in graph:
        return None, float('inf')
    if start_node == goal_node:
        return [start_node], 0.0

    open_set = []
    g_score = {start_node: 0.0}
    came_from = {}
    best = float('inf')

    heapq.heappush(open_set, (heuristic(start_node), 0.0, start_node))

    while open_set:
        _, gc, current = heapq.heappop(open_set)
        if gc >= best:
            break
        if gc > g_score[current]:
            continue
        if current == goal_node:
            best = g_score[current]
            continue
        if stats is not None:
            stats['visited'] = stats.get('visited', 0) + 1
        for neighbor, weight in graph[current].items():
            if penalties:
                weight = weight * penalties.get(
                    edge_id(current, neighbor), 1.0)
            tentative = gc + weight
            if tentative < g_score.get(neighbor, float('inf')):
                came_from[neighbor] = current
                g_score[neighbor] = tentative
                heapq.heappush(
                    open_set, (tentative + heuristic(neighbor), tentative,
                               neighbor))

    if best == float('inf'):
        return None, float('inf')
    path = [goal_node]
    current = goal_node
    while current in came_from:
        current = came_from[current]
        path.append(current)
    return path[::-1], best


def run_a_star(graph: dict, locations: dict, start_node: int,
               goal_node: int, geo: dict | None = None,
               stats: dict | None = None,
               penalties: dict | None = None):
    if start_node not in graph or goal_node not in graph:
        return None, float('inf')
    if geo is None:
        geo = precompute_geo(locations)

    def heuristic(node):
        return _haversine_from_geo(geo[node], geo[goal_node])

    return _astar(graph, geo, heuristic, start_node, goal_node, stats,
                  penalties)


def _alt_heuristic(geo: dict, landmark_dists: list,
                   node: int, goal: int) -> float:
    h = _haversine_from_geo(geo[node], geo[goal])
    for dist in landmark_dists:
        dg = dist.get(goal)
        dn = dist.get(node)
        if dg is not None and dn is not None:
            diff = dg - dn
            if diff > h:
                h = diff
    return h


def run_alt_a_star(graph: dict, geo: dict, landmark_dists: list,
                   start_node: int, goal_node: int,
                   stats: dict | None = None):
    if start_node not in graph or goal_node not in graph:
        return None, float('inf')
    return _astar(
        graph, geo,
        lambda n: _alt_heuristic(geo, landmark_dists, n, goal_node),
        start_node, goal_node, stats)


def run_bidirectional_alt(graph: dict, geo: dict, landmark_dists: list,
                          start_node: int, goal_node: int,
                          stats: dict | None = None):
    if start_node not in graph or goal_node not in graph:
        return None, float('inf')
    if start_node == goal_node:
        return [start_node], 0.0

    def hf(node):
        return _alt_heuristic(geo, landmark_dists, node, goal_node)

    def hb(node):
        return _alt_heuristic(geo, landmark_dists, node, start_node)

    fwd_g = {start_node: 0.0}
    bwd_g = {goal_node: 0.0}
    fwd_parent = {}
    bwd_parent = {}
    fwd_open = [(hf(start_node), 0.0, start_node)]
    bwd_open = [(hb(goal_node), 0.0, goal_node)]
    mu = float('inf')
    meet = None

    while fwd_open or bwd_open:
        if fwd_open and (not bwd_open or fwd_open[0][0] <= bwd_open[0][0]):
            if fwd_open[0][0] > mu:
                fwd_open.clear()
                continue
            _, fg, u = heapq.heappop(fwd_open)
            if fg > fwd_g[u]:
                continue
            if stats is not None:
                stats['visited'] = stats.get('visited', 0) + 1
            bgu = bwd_g.get(u)
            if bgu is not None and fwd_g[u] + bgu < mu:
                mu = fwd_g[u] + bgu
                meet = u
            for v, w in graph[u].items():
                nd = fwd_g[u] + w
                if nd < fwd_g.get(v, float('inf')):
                    fwd_g[v] = nd
                    fwd_parent[v] = u
                    heapq.heappush(fwd_open, (nd + hf(v), nd, v))
        else:
            if bwd_open[0][0] > mu:
                bwd_open.clear()
                continue
            _, bg, u = heapq.heappop(bwd_open)
            if bg > bwd_g[u]:
                continue
            if stats is not None:
                stats['visited'] = stats.get('visited', 0) + 1
            fgu = fwd_g.get(u)
            if fgu is not None and fgu + bwd_g[u] < mu:
                mu = fgu + bwd_g[u]
                meet = u
            for v, w in graph[u].items():
                nd = bwd_g[u] + w
                if nd < bwd_g.get(v, float('inf')):
                    bwd_g[v] = nd
                    bwd_parent[v] = u
                    heapq.heappush(bwd_open, (nd + hb(v), nd, v))

    if meet is None:
        return None, float('inf')

    path_fwd = []
    n = meet
    while n in fwd_parent:
        path_fwd.append(n)
        n = fwd_parent[n]
    path_fwd.append(start_node)
    path_fwd.reverse()

    path_bwd = []
    n = meet
    while n in bwd_parent:
        n = bwd_parent[n]
        path_bwd.append(n)

    return path_fwd + path_bwd, mu


_ALT_LOCK = threading.RLock()


def _ensure_alt(pg) -> None:
    if getattr(pg, "_alt_k", 0) <= 0 or pg.landmark_dists:
        return
    with _ALT_LOCK:
        if pg.landmark_dists:
            return
        from app.services.pathfinding.preprocess import (
            dijkstra_all,
            select_landmarks,
        )
        landmarks = select_landmarks(pg.graph, pg.locations, pg._alt_k)
        pg.landmarks = landmarks
        pg.landmark_dists = [dijkstra_all(pg.graph, lm) for lm in landmarks]


def shortest_path(pg, start_node: int, goal_node: int,
                  penalties: dict | None = None):
    if penalties:
        return run_a_star(pg.graph, pg.locations, start_node, goal_node,
                          pg.geo, penalties=penalties)
    if pg.ch is not None:
        path, cost = pg.ch.query(start_node, goal_node)
        if path is not None:
            return path, cost
    _ensure_alt(pg)
    if pg.landmark_dists and not pg.directed:
        path, cost = run_bidirectional_alt(
            pg.graph, pg.geo, pg.landmark_dists, start_node, goal_node)
        if path is not None:
            return path, cost
    if pg.landmark_dists:
        path, cost = run_alt_a_star(
            pg.graph, pg.geo, pg.landmark_dists, start_node, goal_node)
        if path is not None:
            return path, cost
    return run_a_star(pg.graph, pg.locations, start_node, goal_node,
                      pg.geo)
