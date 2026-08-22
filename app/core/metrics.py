"""Prometheus metrics for observability."""

import os

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

# Global registry
REGISTRY = CollectorRegistry()

# Feature flag
METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "1") == "1"

if not METRICS_ENABLED:
    # No-op metrics
    class _NoOp:
        def labels(self, *args, **kwargs):
            return self
        def inc(self, *args, **kwargs):
            pass
        def dec(self, *args, **kwargs):
            pass
        def set(self, *args, **kwargs):
            pass
        def observe(self, *args, **kwargs):
            pass

    class _NoOpHistogram:
        def labels(self, *args, **kwargs):
            return self
        def observe(self, *args, **kwargs):
            pass
        def time(self):
            class _Timer:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
            return _Timer()

    Counter = Gauge = Histogram = _NoOp  # type: ignore


# ──────────────────────────────────────────────────────────────────────────
# HTTP Metrics
# ──────────────────────────────────────────────────────────────────────────
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


# ──────────────────────────────────────────────────────────────────────────
# Navigation WebSocket Metrics
# ──────────────────────────────────────────────────────────────────────────
nav_active_sessions = Gauge(
    "nav_active_sessions",
    "Active navigation WebSocket sessions",
    registry=REGISTRY,
)

nav_ws_connections_total = Counter(
    "nav_ws_connections_total",
    "Total WebSocket connections",
    ["status"],  # connected, disconnected, error
    registry=REGISTRY,
)

nav_reroutes_total = Counter(
    "nav_reroutes_total",
    "Reroutes triggered",
    ["type", "applied"],  # type: off_route, traffic, ai; applied: true, false
    registry=REGISTRY,
)

nav_reroute_duration_seconds = Histogram(
    "nav_reroute_duration_seconds",
    "Reroute calculation time in seconds",
    ["type"],
    registry=REGISTRY,
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)

nav_progress_interval_seconds = Histogram(
    "nav_progress_interval_seconds",
    "Interval between route_progress messages",
    registry=REGISTRY,
    buckets=(1, 2, 3, 5, 10, 30, 60),
)

nav_off_route_warnings_total = Counter(
    "nav_off_route_warnings_total",
    "Off-route warnings sent",
    registry=REGISTRY,
)

nav_turn_notifications_total = Counter(
    "nav_turn_notifications_total",
    "Turn-by-turn notifications sent",
    registry=REGISTRY,
)


# ──────────────────────────────────────────────────────────────────────────
# Pathfinding Metrics
# ──────────────────────────────────────────────────────────────────────────
pf_route_calc_duration_seconds = Histogram(
    "pf_route_calc_duration_seconds",
    "Route calculation time in seconds",
    ["mode", "source"],
    registry=REGISTRY,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

pf_graph_load_duration_seconds = Histogram(
    "pf_graph_load_duration_seconds",
    "Graph load time in seconds",
    ["source", "level"],
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

pf_graph_cache_hits_total = Counter(
    "pf_graph_cache_hits_total",
    "Graph cache hits",
    ["source"],
    registry=REGISTRY,
)

pf_route_requests_total = Counter(
    "pf_route_requests_total",
    "Route requests",
    ["mode", "status"],  # status: success, not_found, error, fallback
    registry=REGISTRY,
)

pf_a_star_nodes_visited = Histogram(
    "pf_a_star_nodes_visited",
    "A* nodes visited",
    ["mode"],
    registry=REGISTRY,
    buckets=(10, 50, 100, 500, 1000, 5000, 10000, 50000),
)


# ──────────────────────────────────────────────────────────────────────────
# Traffic Metrics
# ──────────────────────────────────────────────────────────────────────────
traffic_poller_duration_seconds = Histogram(
    "traffic_poller_duration_seconds",
    "Traffic poller cycle duration in seconds",
    registry=REGISTRY,
    buckets=(0.5, 1, 2, 5, 10, 30, 60),
)

traffic_tomtom_request_duration_seconds = Histogram(
    "traffic_tomtom_request_duration_seconds",
    "TomTom API request latency in seconds",
    ["endpoint"],
    registry=REGISTRY,
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

traffic_tomtom_congested_edges = Gauge(
    "traffic_tomtom_congested_edges",
    "Number of congested edges from TomTom",
    ["city"],
    registry=REGISTRY,
)

traffic_penalty_edges_total = Counter(
    "traffic_penalty_edges_total",
    "Edges with traffic penalties applied",
    ["source"],  # tomtom, internal, rag
    registry=REGISTRY,
)

traffic_eta_calculation_duration_seconds = Histogram(
    "traffic_eta_calculation_duration_seconds",
    "ETA calculation time in seconds",
    registry=REGISTRY,
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
)


# ──────────────────────────────────────────────────────────────────────────
# RAG (News) Metrics
# ──────────────────────────────────────────────────────────────────────────
rag_ingestion_duration_seconds = Histogram(
    "rag_ingestion_duration_seconds",
    "News ingestion time in seconds",
    ["city"],
    registry=REGISTRY,
    buckets=(1, 5, 10, 30, 60, 120, 300),
)

rag_ingestion_news_count = Histogram(
    "rag_ingestion_news_count",
    "Number of news items fetched per ingestion",
    ["city"],
    registry=REGISTRY,
    buckets=(1, 5, 10, 25, 50, 100),
)

rag_city_detection_duration_seconds = Histogram(
    "rag_city_detection_duration_seconds",
    "City detection time in seconds",
    ["method"],
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

rag_evaluation_duration_seconds = Histogram(
    "rag_evaluation_duration_seconds",
    "Incident evaluation time in seconds",
    ["city"],
    registry=REGISTRY,
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)

rag_incidents_evaluated_total = Counter(
    "rag_incidents_evaluated_total",
    "Incidents evaluated by RAG",
    ["severity"],
    registry=REGISTRY,
)

rag_penalties_generated_total = Counter(
    "rag_penalties_generated_total",
    "Penalties generated from RAG evaluation",
    registry=REGISTRY,
)


# ──────────────────────────────────────────────────────────────────────────
# Agent (Reroute Decisions) Metrics
# ──────────────────────────────────────────────────────────────────────────
agent_decision_duration_seconds = Histogram(
    "agent_decision_duration_seconds",
    "Agent decision time in seconds",
    ["hint", "action"],  # hint: off_route, traffic; action: apply, ignore, defer
    registry=REGISTRY,
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

agent_tool_calls_total = Counter(
    "agent_tool_calls_total",
    "Agent tool calls",
    ["tool", "status"],
    registry=REGISTRY,
)

agent_circuit_breaker_state = Gauge(
    "agent_circuit_breaker_state",
    "Circuit breaker state (1=open, 0=closed)",
    registry=REGISTRY,
)

agent_fallback_total = Counter(
    "agent_fallback_total",
    "Agent fallback to deterministic logic",
    ["reason"],
    registry=REGISTRY,
)


# ──────────────────────────────────────────────────────────────────────────
# Internal Reports Metrics
# ──────────────────────────────────────────────────────────────────────────
reports_received_total = Counter(
    "reports_received_total",
    "Driver reports received",
    registry=REGISTRY,
)

reports_classified_total = Counter(
    "reports_classified_total",
    "Reports classified",
    ["severity"],
    registry=REGISTRY,
)

reports_reroutes_total = Counter(
    "reports_reroutes_total",
    "Reroutes triggered from reports",
    ["applied"],
    registry=REGISTRY,
)


# ──────────────────────────────────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────────────────────────────────
def metrics_endpoint() -> Response:
    """Prometheus metrics endpoint handler."""
    return Response(
        content=generate_latest(REGISTRY),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def record_http_request(method: str, path: str, status: int, duration: float) -> None:
    """Record HTTP request metrics."""
    if not METRICS_ENABLED:
        return
    # Normalize path for cardinality control
    norm_path = _normalize_path(path)
    http_requests_total.labels(method=method, path=norm_path, status=str(status)).inc()
    http_request_duration_seconds.labels(method=method, path=norm_path).observe(duration)


def _normalize_path(path: str) -> str:
    """Normalize path to reduce cardinality."""
    # Replace UUIDs and IDs with placeholders
    import re
    path = re.sub(r"/[0-9a-f-]{36}", "/:uuid", path)
    path = re.sub(r"/\d+", "/:id", path)
    return path