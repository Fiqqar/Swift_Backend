import heapq
import itertools
import os
from dataclasses import dataclass

from app.services.pathfinding.core_a_star import precompute_geo


@dataclass
class PathGraph:
    graph: dict
    locations: dict
    geo: dict
    ref_lat: float
    ref_lon: float
    landmarks: list
    landmark_dists: list
    ch: object = None
    directed: bool = False
    radius: int = 0
    source: str = "demo"
    warning: str | None = None
    bbox: tuple | None = None


def dijkstra_all(graph: dict, source) -> dict:
    dist = {source: 0.0}
    heap = [(0.0, source)]
    closed = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in closed:
            continue
        closed.add(u)
        for v, w in graph.get(u, {}).items():
            nd = d + w
            if nd < dist.get(v, float('inf')):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist


def _dist2(locations: dict, a, b) -> float:
    lat1, lon1 = locations[a]
    lat2, lon2 = locations[b]
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    return dlat * dlat + dlon * dlon


def select_landmarks(graph: dict, locations: dict, k: int = 8) -> list:
    nodes = [n for n in locations if n in graph]
    if not nodes:
        return []
    k = max(0, min(k, len(nodes)))
    if k == 0:
        return []

    seed = nodes[0]
    landmarks = [seed]
    chosen = {seed}
    best_d = {n: _dist2(locations, n, seed) for n in nodes}

    while len(landmarks) < k:
        best_node, best_val = None, -1.0
        for n in nodes:
            if n in chosen:
                continue
            d = best_d[n]
            if d > best_val:
                best_val = d
                best_node = n
        if best_node is None:
            break
        landmarks.append(best_node)
        chosen.add(best_node)
        for n in nodes:
            if n not in chosen:
                d = _dist2(locations, n, best_node)
                if d < best_d[n]:
                    best_d[n] = d

    return landmarks


_MAX_WITNESS_NODES = int(os.environ.get("OSMNX_CH_WITNESS_NODES", "2500"))
_CH_MAX_NODES = int(os.environ.get("OSMNX_CH_MAX_NODES", "80000"))


class ContractionHierarchy:
    def __init__(self):
        self.rank = {}
        self.order = []
        self.up = {}
        self.through = {}
        self.shortcuts = []

    def _expand(self, a, b):
        """Urai jalur a->b (mungkin lewat shortcut) menjadi edge asli."""
        seq = [(a, b)]
        while True:
            changed = False
            new_seq = []
            for x, y in seq:
                mid = self.through.get((x, y)) or self.through.get((y, x))
                if mid is None:
                    new_seq.append((x, y))
                else:
                    new_seq.append((x, mid))
                    new_seq.append((mid, y))
                    changed = True
            seq = new_seq
            if not changed:
                break
        out = [seq[0][0]]
        for x, y in seq:
            out.append(y)
        return out

    def query(self, start_node, goal_node, stats=None):
        if start_node == goal_node:
            return [start_node], 0.0
        up = self.up
        if start_node not in self.rank or goal_node not in self.rank:
            return None, float('inf')

        dist_f = {start_node: 0.0}
        parent_f = {}
        dist_b = {goal_node: 0.0}
        parent_b = {}
        hq_f = [(0.0, start_node)]
        hq_b = [(0.0, goal_node)]
        closed_f = set()
        closed_b = set()
        mu = float('inf')
        meet = None

        while hq_f or hq_b:
            if hq_f and (not hq_b or hq_f[0][0] <= hq_b[0][0]):
                if hq_f[0][0] > mu:
                    hq_f.clear()
                    continue
                df, u = heapq.heappop(hq_f)
                if u in closed_f:
                    continue
                closed_f.add(u)
                if stats is not None:
                    stats['visited'] = stats.get('visited', 0) + 1
                dbu = dist_b.get(u)
                if dbu is not None and df + dbu < mu:
                    mu = df + dbu
                    meet = u
                for v, w in up.get(u, {}).items():
                    if v in closed_f:
                        continue
                    nd = df + w
                    if nd < dist_f.get(v, float('inf')):
                        dist_f[v] = nd
                        parent_f[v] = u
                        heapq.heappush(hq_f, (nd, v))
                        if v in closed_b and nd + dist_b[v] < mu:
                            mu = nd + dist_b[v]
                            meet = v
            else:
                if hq_b[0][0] > mu:
                    hq_b.clear()
                    continue
                db, u = heapq.heappop(hq_b)
                if u in closed_b:
                    continue
                closed_b.add(u)
                if stats is not None:
                    stats['visited'] = stats.get('visited', 0) + 1
                dfu = dist_f.get(u)
                if dfu is not None and dfu + db < mu:
                    mu = dfu + db
                    meet = u
                for v, w in up.get(u, {}).items():
                    if v in closed_b:
                        continue
                    nd = db + w
                    if nd < dist_b.get(v, float('inf')):
                        dist_b[v] = nd
                        parent_b[v] = u
                        heapq.heappush(hq_b, (nd, v))
                        if v in closed_f and dist_f[v] + nd < mu:
                            mu = dist_f[v] + nd
                            meet = v

        if meet is None:
            return None, float('inf')

        path_fwd = []
        n = meet
        while n in parent_f:
            path_fwd.append(n)
            n = parent_f[n]
        path_fwd.append(start_node)
        path_fwd.reverse()

        path_bwd = []
        n = meet
        while n in parent_b:
            n = parent_b[n]
            path_bwd.append(n)

        path = path_fwd + path_bwd
        expanded = [path[0]]
        for a, b in zip(path, path[1:]):
            expanded.extend(self._expand(a, b)[1:])
        return expanded, mu


def _witness_found(active, u, v, exclude, max_cost, max_nodes) -> bool:
    if u == v:
        return True
    if max_cost <= 0:
        return False
    direct = active[u].get(v)
    if direct is not None and direct <= max_cost:
        return True
    dist = {u: 0.0}
    heap = [(0.0, u)]
    seen = 0
    while heap:
        d, cur = heapq.heappop(heap)
        if d > dist.get(cur, float('inf')):
            continue
        if d >= max_cost:
            continue
        seen += 1
        if seen > max_nodes:
            return False
        if cur == v:
            return d <= max_cost
        for nb, w in active[cur].items():
            if nb == exclude:
                continue
            nd = d + w
            if nd < dist.get(nb, float('inf')) and nd <= max_cost:
                dist[nb] = nd
                heapq.heappush(heap, (nd, nb))
    return False


def build_ch(graph: dict, max_witness_nodes: int | None = None) -> ContractionHierarchy:
    ch = ContractionHierarchy()
    active = {u: dict(neigh) for u, neigh in graph.items()}
    priority_cache = {}
    counter = itertools.count()
    heap = []
    budget = _MAX_WITNESS_NODES if max_witness_nodes is None else max_witness_nodes

    def priority(node):
        neighbors = list(active[node])
        deg = len(neighbors)
        short = 0
        for i in range(deg):
            u = neighbors[i]
            w_ux = active[u][node]
            for j in range(i + 1, deg):
                v = neighbors[j]
                via = w_ux + active[node][v]
                direct = active[u].get(v)
                if direct is None or direct > via:
                    short += 1
        return short - deg

    for node in active:
        p = priority(node)
        priority_cache[node] = p
        heapq.heappush(heap, (p, next(counter), node))

    rank = 0
    while heap:
        p, _, node = heapq.heappop(heap)
        if node not in active:
            continue
        p_now = priority(node)
        if p_now != priority_cache[node]:
            priority_cache[node] = p_now
            heapq.heappush(heap, (p_now, next(counter), node))
            continue

        neighbors = list(active[node])
        to_add = []
        for i in range(len(neighbors)):
            u = neighbors[i]
            if u not in active:
                continue
            for j in range(i + 1, len(neighbors)):
                v = neighbors[j]
                if v not in active:
                    continue
                via = active[u][node] + active[node][v]
                direct = active[u].get(v)
                if direct is not None and direct <= via:
                    continue
                if _witness_found(active, u, v, node, via, budget):
                    continue
                to_add.append((u, v, via))

        for u, v, via in to_add:
            active[u][v] = via
            active[v][u] = via
            ch.shortcuts.append((u, v, via))
            ch.through[(u, v)] = node
            ch.through[(v, u)] = node

        for nb in neighbors:
            if nb in active:
                del active[nb][node]
        del active[node]
        ch.rank[node] = rank
        ch.order.append(node)
        rank += 1

    up = {}

    def add_edge(a, b, w):
        if ch.rank[b] > ch.rank[a]:
            up.setdefault(a, {})[b] = w

    for u, neigh in graph.items():
        for v, w in neigh.items():
            add_edge(u, v, w)
    for u, v, w in ch.shortcuts:
        add_edge(u, v, w)
        add_edge(v, u, w)

    ch.up = up
    return ch


def is_directed(graph: dict) -> bool:
    for u, neigh in graph.items():
        for v in neigh:
            if graph.get(v, {}).get(u) is None:
                return True
    return False


def build_path_graph(graph, locations, ref_lat, ref_lon,
                     landmarks_k: int = 8, enable_ch: bool = True,
                     max_witness_nodes: int | None = None) -> PathGraph:
    geo = precompute_geo(locations)
    directed = is_directed(graph)
    landmarks = select_landmarks(graph, locations, landmarks_k)
    landmark_dists = [dijkstra_all(graph, lm) for lm in landmarks]
    ch = None
    if enable_ch and not directed and len(graph) <= _CH_MAX_NODES:
        ch = build_ch(graph, max_witness_nodes)
    return PathGraph(graph, locations, geo, ref_lat, ref_lon,
                     landmarks, landmark_dists, ch, directed)
