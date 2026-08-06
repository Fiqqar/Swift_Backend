from app.services.pathfinding.core_a_star import shortest_path

try:
    import _rust_engine
    RUST_AVAILABLE = True
except ImportError:
    _rust_engine = None
    RUST_AVAILABLE = False


def _rust_graph_for(pg):
    rg = getattr(pg, "_rust_graph", None)
    if rg is None:
        rg = _rust_engine.RustGraph(pg.graph, True)
        pg._rust_graph = rg
    return rg


def route(pg, start_node: int, goal_node: int,
          penalties: dict | None = None):
    if RUST_AVAILABLE:
        try:
            path, cost = _rust_graph_for(pg).route(
                start_node, goal_node, penalties)
            if path:
                return path, cost
        except Exception:
            pass
    return shortest_path(pg, start_node, goal_node, penalties)
