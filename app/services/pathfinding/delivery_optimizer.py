"""Optimisasi urutan stop pengantaran (TSP heuristic) untuk last-mile.

Menggunakan jarak haversine (straight-line) untuk membangun matriks jarak dan
menentukan urutan kunjungan, lalu menandai paket EXPRESS agar cenderung diantar
lebih dulu lewat penalti biaya. Rute sungguhan per-leg dihitung di endpoint
pathfinding (bukan di sini) sehingga request pertama tidak perlu N^2 panggilan
engine.
"""

import math

from app.services.pathfinding.core_a_star import haversine_distance

EXPRESS_DISCOUNT_DEFAULT = 0.6


def haversine_matrix(points: list[tuple[float, float]]) -> list[list[float]]:
    n = len(points)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                matrix[i][j] = haversine_distance(points[i], points[j])
    return matrix


def _nearest_neighbor(matrix: list[list[float]], start: int = 0) -> list[int]:
    n = len(matrix)
    visited = {start}
    route = [start]
    cur = start
    while len(route) < n:
        nxt = min(
            (j for j in range(n) if j not in visited),
            key=lambda j: matrix[cur][j],
        )
        route.append(nxt)
        visited.add(nxt)
        cur = nxt
    return route


def _tour_cost(matrix: list[list[float]], route: list[int],
               return_to_start: bool = True) -> float:
    total = 0.0
    for i in range(len(route) - 1):
        total += matrix[route[i]][route[i + 1]]
    if return_to_start and len(route) > 1:
        total += matrix[route[-1]][route[0]]
    return total


def _two_opt(matrix: list[list[float]], route: list[int],
             return_to_start: bool = True) -> list[int]:
    improved = True
    n = len(route)
    best = list(route)
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 2, n + 1):
                if not return_to_start and j == n:
                    continue
                candidate = list(best)
                candidate[i:j] = reversed(candidate[i:j])
                if _tour_cost(matrix, candidate, return_to_start) < \
                        _tour_cost(matrix, best, return_to_start) - 1e-12:
                    best = candidate
                    improved = True
    return best


def optimize_stop_order(
    hub: tuple[float, float],
    deliveries: list[tuple[float, float]],
    service_types: list[str] | None = None,
    express_discount: float = EXPRESS_DISCOUNT_DEFAULT,
    return_to_hub: bool = False,
) -> list[int]:
    """Urutkan indeks pengantaran (0-based) paling efisien.

    - `hub`: koordinat titik awal (Drop Point/Hub).
    - `deliveries`: koordinat (lat, lon) tiap alamat penerima.
    - `service_types`: opsional; berisi 'EXPRESS'/'REGULAR' per delivery.
      Biaya MENUJU stop EXPRESS dikali `express_discount` (<1) supaya paket
      cepat cenderung diantar lebih dulu.
    - `return_to_hub`: bila True, rute dianggap tour tertutup kembali ke hub
      saat mengevaluasi perbaikan 2-opt (urutan yang dikembalikan tetap hanya
      indeks delivery).
    """
    if not deliveries:
        return []
    points = [hub] + list(deliveries)
    matrix = haversine_matrix(points)

    if service_types and express_discount < 1.0:
        for i in range(len(points)):
            for j in range(1, len(points)):
                if service_types[j - 1] == "EXPRESS":
                    matrix[i][j] *= express_discount

    route = _nearest_neighbor(matrix, 0)
    route = _two_opt(matrix, route, return_to_start=return_to_hub)
    return [idx - 1 for idx in route if idx != 0]


def ensure_express_first(
    order: list[int],
    service_types: list[str],
    express: str = "EXPRESS",
) -> list[int]:
    """Jamin semua EXPRESS berada di depan REGULAR (stabil, tanpa ubah yang
    sudah berurutan). Helper untuk test/pesan deterministik bila diinginkan."""
    expr = [i for i in order if service_types[i] == express]
    reg = [i for i in order if service_types[i] != express]
    return expr + reg
