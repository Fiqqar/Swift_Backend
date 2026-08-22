"""Structured logging with structlog — JSON in production, text in development."""

import os
from typing import Any

import structlog
from structlog.stdlib import LoggerFactory, BoundLogger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Context variable for correlation ID propagation across async calls
import contextvars

_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def get_correlation_id() -> str | None:
    """Get current correlation ID from context."""
    return _correlation_id_var.get()


def set_correlation_id(cid: str | None) -> None:
    """Set correlation ID in context."""
    _correlation_id_var.set(cid)


def generate_correlation_id() -> str:
    """Generate a new correlation ID."""
    import uuid
    return uuid.uuid4().hex[:16]


def ws_correlation_id(websocket: Any) -> str:
    """
    Extract or generate correlation ID for a WebSocket connection.

    Tries to read a correlation ID from the client via the
    ``x-correlation-id`` header or ``cid`` query parameter.
    If neither is present, generates a new ID and sets it in the
    logging context.

    Returns the correlation ID string for use by the caller.
    """
    # Try to read from client-supplied headers or query params
    cid = websocket.headers.get("x-correlation-id") or websocket.query_params.get("cid")
    if cid:
        cid = cid.strip()
    if not cid:
        cid = generate_correlation_id()
    set_correlation_id(cid)
    return cid


class PiiFilter:
    """Round GPS coordinates in log records to 3 decimals (~100m)."""

    COORD_KEYS = {
        "lat",
        "lon",
        "latitude",
        "longitude",
        "origin",
        "destination",
        "current",
        "target",
    }

    def __call__(self, logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        for key, value in list(event_dict.items()):
            if key.lower() in self.COORD_KEYS:
                if isinstance(value, (int, float)):
                    event_dict[key] = round(float(value), 3)
                elif isinstance(value, (list, tuple)) and len(value) == 2:
                    try:
                        event_dict[key] = [round(float(v), 3) for v in value]
                    except (TypeError, ValueError):
                        pass
                elif isinstance(value, dict):
                    try:
                        rounded = {}
                        for k, v in value.items():
                            if isinstance(v, (int, float)):
                                rounded[k] = round(float(v), 3)
                            else:
                                rounded[k] = v
                        event_dict[key] = rounded
                    except (TypeError, ValueError):
                        pass
        return event_dict


def add_correlation_id(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Inject correlation ID into event dict."""
    cid = get_correlation_id()
    event_dict["correlation_id"] = cid if cid else "-"
    return event_dict

class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    Assigns a correlation ID to every incoming HTTP request and exposes it
    via the correlation ID contextvar for the duration of the request.
    Reuses a client-supplied 'X-Correlation-ID' header if present, otherwise
    generates a new one. The ID is echoed back in the response headers.
    """

    async def dispatch(self, request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or generate_correlation_id()
        token = _correlation_id_var.set(cid)
        try:
            response: Response = await call_next(request)
        finally:
            _correlation_id_var.reset(token)
        response.headers["X-Correlation-ID"] = cid
        return response

structlog_processors: list[Any] = [
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.stdlib.add_log_level,
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
    add_correlation_id,
    PiiFilter(),
    structlog.dev.ConsoleRenderer(),
]


def get_renderer() -> Any:
    """Return JSON renderer (production) or Console renderer (development)."""
    is_production = os.environ.get("APP_ENV", "development").lower() == "production"
    if is_production:
        structlog_processors.append(structlog.processors.JSONRenderer())
        return structlog.processors.JSONRenderer()
    else:
        # ConsoleRenderer already in processors list for development
        return structlog.dev.ConsoleRenderer()


structlog.configure(
    processors=structlog_processors,
    logger_factory=LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structlog-bound logger for the given name.

    Returns a BoundLogger that accepts **kwargs in log methods,
    e.g. logger.info("system.startup", version="1.0.0", env="development").
    """
    return structlog.get_logger(name)


def setup_logging() -> None:
    """Configure application-wide structlog and standard library logging interception."""

    is_production = os.environ.get("APP_ENV", "development").lower() == "production"
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    # Configure structlog-enabled loggers for pathfinding components
    for component, env_key in [("", ""), ("pathfinding", "LOG_PATHFINDING")]:
        prefix = env_key.upper() if env_key else "PATHFINDING"
        level = os.environ.get(env_key, log_level).upper()
        logger_name = f"pathfinding.{component}" if component else "pathfinding"
        struct_logger = structlog.get_logger(logger_name)

    # Intercept uvicorn/starlette standard library logs
    import logging as logging_mod

    for uvicorn_logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access", "starlette"]:
        uvicorn_logger = logging_mod.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = False
        # Configure structlog to handle these loggers
        structlog.get_logger(uvicorn_logger_name)

    # Log the configuration
    logger = structlog.get_logger("app")
    logger.info(
        "logging.configured",
        extra={"format": "json" if is_production else "text", "level": log_level},
    )