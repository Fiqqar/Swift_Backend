from app.services.pathfinding.core_a_star import shortest_path
from app.services.pathfinding.connectivity import reaches

try:
    import _rust_engine
    RUST_AVAILABLE = True
except ImportError:
    _rust_engine = None
    RUST_AVAILABLE = False


def _rust_graph_for(pg):
    rg = getattr(pg, "_rust_graph", None)
    if rg is None:
        rg = _rust_engine.RustGraph(pg.graph, True)  # type: ignore
        pg._rust_graph = rg
    return rg


def route(pg, start_node: int, goal_node: int,
          penalties: dict | None = None):
    """Find shortest path.

    Bila Rust engine tersedia, skip reachability pre-check Python murni —
    Rust menangani "no path" secara native dan lebih cepat daripada
    BFS/DFS pre-check di `connectivity.py`.

    Returns (path, cost) or (None, inf) if no path exists.
    """
    blocked_edges = set()
    if penalties:
        blocked_edges = {eid for eid, mult in penalties.items() if mult == float("inf")}

    # Pre-check reachability HANYA saat fallback ke Python A* murni.
    # Saat Rust tersedia, biarkan engine yang menentukan "no path" —
    # menghindari double traversal (BFS + A*) pada graf besar/dense.
    if not RUST_AVAILABLE:
        if not reaches(pg.graph, start_node, goal_node, blocked_edges):
            return None, float('inf')

    if RUST_AVAILABLE:
        try:
            path, cost = _rust_graph_for(pg).route(
                start_node, goal_node, penalties)
            if path:
                return path, cost
        except Exception:
            pass
    return shortest_path(pg, start_node, goal_node, penalties)
