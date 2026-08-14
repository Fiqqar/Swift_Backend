"""Benchmark perbandingan algoritma pencarian rute.

Menjalankan pencarian rute pada graf sintetis (grid + jitter)
menggunakan beberapa metode dan membandingkan waktu, jumlah node yang
ditelusuri, serta kesamaan jarak terhadap A* dasar.

Jalankan:
    python scripts/benchmark.py
"""

import os
import heapq
import random
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from app.services.pathfinding.core_a_star import (
    _ensure_alt,
    haversine_distance,
    run_a_star,
    run_alt_a_star,
    run_bidirectional_alt,
)
from app.services.pathfinding.preprocess import build_path_graph


def build_synthetic_grid(rows: int, cols: int, seed: int = 1):
    rng = random.Random(seed)
    lat0, lon0 = -6.1754, 106.8272
    lat_span, lon_span = 0.02, 0.02
    locations = {}
    graph = {}

    def edge_weight(a, b):
        # bobot >= jarak great-circle agar heuristik haversine tetap admissible
        base = haversine_distance(a, b)
        return base * (1.0 + 0.5 * rng.random())

    for r in range(rows):
        for c in range(cols):
            nid = r * cols + c
            lat = lat0 + (r / (rows - 1) - 0.5) * lat_span
            lon = lon0 + (c / (cols - 1) - 0.5) * lon_span
            locations[nid] = (lat, lon)
            graph[nid] = {}

    for r in range(rows):
        for c in range(cols):
            nid = r * cols + c
            if c + 1 < cols:
                other = nid + 1
                w = edge_weight(locations[nid], locations[other])
                graph[nid][other] = w
                graph[other][nid] = w
            if r + 1 < rows:
                other = nid + cols
                w = edge_weight(locations[nid], locations[other])
                graph[nid][other] = w
                graph[other][nid] = w

    return graph, locations


def is_valid_path(graph, path):
    if path is None:
        return False
    for a, b in zip(path, path[1:]):
        if b not in graph[a]:
            return False
    return True


def dijkstra_baseline(graph, start, goal, stats=None):
    dist = {start: 0.0}
    heap = [(0.0, start)]
    closed = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in closed:
            continue
        closed.add(u)
        if stats is not None:
            stats['visited'] = stats.get('visited', 0) + 1
        if u == goal:
            return [goal], d
        for v, w in graph[u].items():
            nd = d + w
            if nd < dist.get(v, float('inf')):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return None, float('inf')


def benchmark(rows: int, cols: int, pairs: int = 60, seed: int = 7):
    graph, locations = build_synthetic_grid(rows, cols, seed)
    nodes = list(graph)

    print(f"\n=== Graf {rows}x{cols} ({len(nodes)} node, "
          f"{pairs} pasangan OD) ===")

    t0 = time.perf_counter()
    pg = build_path_graph(graph, locations, -6.1754, 106.8272)
    preprocess_time = time.perf_counter() - t0
    print(f"Preprocessing (CH): {preprocess_time * 1000:.0f} ms")

    t0 = time.perf_counter()
    _ensure_alt(pg)
    alt_time = time.perf_counter() - t0
    print(f"ALT lazy materialisasi: {alt_time * 1000:.0f} ms")

    rng = random.Random(seed)
    od_pairs = []
    for _ in range(pairs):
        s = rng.choice(nodes)
        g = rng.choice(nodes)
        if s != g:
            od_pairs.append((s, g))

    methods = {
        "dijkstra": lambda s, g, st: dijkstra_baseline(
            graph, s, g, stats=st),
        "a_star_fast": lambda s, g, st: run_a_star(
            graph, locations, s, g, stats=st),
        "alt": lambda s, g, st: run_alt_a_star(
            graph, pg.geo, pg.landmark_dists, s, g, stats=st),
        "bidir_alt": lambda s, g, st: run_bidirectional_alt(
            graph, pg.geo, pg.landmark_dists, s, g, stats=st),
        "ch": lambda s, g, st: pg.ch.query(s, g, stats=st),
    }

    baseline = {}
    results = {}
    print(f"{'metode':<12} {'waktu_total':>10} {'waktu/rute':>10} "
          f"{'visited/rute':>12} {'mismatch':>9}")

    for name, fn in methods.items():
        total_t = 0.0
        total_vis = 0
        mismatch = 0
        cost_map = {}
        for s, g in od_pairs:
            stats = {}
            t0 = time.perf_counter()
            path, cost = fn(s, g, stats)
            total_t += time.perf_counter() - t0
            total_vis += stats.get('visited', 0)
            cost_map[(s, g)] = cost
            if name == "dijkstra":
                baseline[(s, g)] = cost
            elif baseline.get((s, g)) is not None:
                if abs(baseline[(s, g)] - cost) > 1e-3:
                    mismatch += 1
            if not is_valid_path(graph, path):
                mismatch += 1
        results[name] = total_t
        n_routes = len(od_pairs)
        print(f"{name:<12} {total_t * 1000:>9.0f}ms "
              f"{total_t / n_routes * 1000:>9.1f}ms "
              f"{total_vis / n_routes:>12.0f} {mismatch:>9}")

    base = results.get('dijkstra', 1e-9)
    for name in ('a_star_fast', 'alt', 'bidir_alt', 'ch'):
        if name in results and results[name] > 0:
            print(f"  {name} vs dijkstra: "
                  f"{base / results[name]:.1f}x lebih cepat")


def smoke_demo():
    print("\n=== Smoke test: demo grid (tanpa jaringan) ===")
    from app.services.pathfinding.graph_loader import _build_demo_grid
    from app.services.pathfinding.preprocess import build_path_graph
    from app.services.pathfinding.core_a_star import shortest_path

    graph, locations = _build_demo_grid(-6.1754, 106.8272, 3000)
    pg = build_path_graph(graph, locations, -6.1754, 106.8272)
    path, cost = shortest_path(pg, 0, 35)
    print(f"Demo grid 0 -> 35: path={len(path)} node, "
          f"jarak={cost:.2f} m, valid={is_valid_path(graph, path)}")


if __name__ == "__main__":
    smoke_demo()
    benchmark(20, 20, pairs=60)
    benchmark(40, 40, pairs=60)
    benchmark(60, 60, pairs=40)
    sys.exit(0)
