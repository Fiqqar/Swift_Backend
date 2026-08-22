"""Centralized logging infrastructure with hybrid JSON/text formatting."""

import contextvars
import json
import logging
import logging.config
import os
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Context variable for correlation ID propagation across async calls
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
    return uuid.uuid4().hex[:16]


class CorrelationIdFilter(logging.Filter):
    """Inject correlation ID into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        cid = get_correlation_id()
        if cid:
            record.correlation_id = cid
        else:
            record.correlation_id = "-"
        return True


class PiiFilter(logging.Filter):
    """Round GPS coordinates in log records to 3 decimals (~100m)."""

    COORD_KEYS = {"lat", "lon", "latitude", "longitude", "origin", "destination", "current", "target"}

    def filter(self, record: logging.LogRecord) -> bool:
        # Round any coordinate-like fields in the record's __dict__
        for key, value in list(record.__dict__.items()):
            if key.lower() in self.COORD_KEYS:
                if isinstance(value, (int, float)):
                    record.__dict__[key] = round(float(value), 3)
                elif isinstance(value, (list, tuple)) and len(value) == 2:
                    try:
                        record.__dict__[key] = [round(float(v), 3) for v in value]
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
                        record.__dict__[key] = rounded
                    except (TypeError, ValueError):
                        pass
        return True


class HybridFormatter(logging.Formatter):
    """Format logs as JSON in production, colored text in development."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.is_production = os.environ.get("APP_ENV", "development").lower() == "production"
        self._color_map = {
            logging.DEBUG: "\033[36m",    # Cyan
            logging.INFO: "\033[32m",     # Green
            logging.WARNING: "\033[33m",  # Yellow
            logging.ERROR: "\033[31m",    # Red
            logging.CRITICAL: "\033[35m", # Magenta
        }
        self._reset = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        # Base fields
        ts = self._format_timestamp(record.created)
        base = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }

        # Extract extra fields (everything not in standard LogRecord attributes)
        standard_attrs = {
            "name", "msg", "args", "created", "filename", "funcName", "levelname",
            "levelno", "lineno", "module", "msecs", "message", "pathname", "process", "processName", "relativeCreated", "thread",
            "threadName", "exc_info", "exc_text", "stack_info", "getMessage",
            "event", "correlation_id"
        }
        extra = {k: v for k, v in record.__dict__.items() if k not in standard_attrs}
        if extra:
            base["fields"] = extra

        if self.is_production:
            return json.dumps(base, ensure_ascii=False, separators=(",", ":"))
        else:
            return self._format_text(base, record)

    def _format_timestamp(self, created: float) -> str:
        """Format timestamp with milliseconds."""
        from datetime import datetime
        dt = datetime.fromtimestamp(created)
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond / 1000):03d}Z"

    def _format_text(self, base: dict[str, Any], record: logging.LogRecord) -> str:
        color = self._color_map.get(record.levelno, "")
        level = f"{color}{record.levelname:<8}{self._reset}"
        logger = f"\033[90m{record.name}{self._reset}"
        event = base["event"]
        cid = base["correlation_id"]

        parts = [f"{base['ts']} {level} {logger} [{cid}] {event}"]

        if "fields" in base:
            for k, v in base["fields"].items():
                parts.append(f"  \033[90m{k}={v}{self._reset}")

        if record.exc_info:
            parts.append(self.formatException(record.exc_info))

        return "\n".join(parts)


def _get_log_levels() -> dict[str, int]:
    """Get log levels from environment with component overrides."""
    global_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    levels = {"": global_level}

    # Component-specific overrides
    prefix = "LOG_LEVEL_"
    for key, value in os.environ.items():
        if key.startswith(prefix):
            component = key[len(prefix):].lower()
            levels[component] = value.upper()

    return levels


def setup_logging() -> None:
    """Configure application-wide logging."""
    log_levels = _get_log_levels()
    is_production = os.environ.get("APP_ENV", "development").lower() == "production"

    # Determine format
    formatter_class = "app.core.logging.HybridFormatter"

    # Build logging config dict
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "hybrid": {
                "()": formatter_class,
            }
        },
        "filters": {
            "correlation_id": {
                "()": "app.core.logging.CorrelationIdFilter",
            },
            "pii": {
                "()": "app.core.logging.PiiFilter",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "hybrid",
                "filters": ["correlation_id", "pii"],
            }
        },
        "loggers": {},
        "root": {
            "level": log_levels.get("", "INFO"),
            "handlers": ["console"],
        }
    }

    # Configure component loggers
    for component, level in log_levels.items():
        if component:
            logger_name = f"pathfinding.{component}"
        else:
            logger_name = "pathfinding"

        config["loggers"][logger_name] = {
            "level": level,
            "handlers": ["console"],
            "propagate": False,
        }

    # Also configure app logger for main.py startup logs
    config["loggers"]["app"] = {
        "level": log_levels.get("", "INFO"),
        "handlers": ["console"],
        "propagate": False,
    }

    logging.config.dictConfig(config)

    # Log the configuration
    logger = logging.getLogger("app")
    logger.info("logging.configured", extra={"format": "json" if is_production else "text", "levels": log_levels})


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Extract/generate correlation ID for each HTTP request."""

    async def dispatch(self, request: Request, call_next):
        # Extract from header or generate
        cid = request.headers.get("x-request-id") or request.headers.get("x-trace-id")
        if not cid:
            cid = generate_correlation_id()

        # Set in context
        token = _correlation_id_var.set(cid)

        # Add to response headers
        response: Response = await call_next(request)
        response.headers["x-request-id"] = cid

        # Reset context
        _correlation_id_var.reset(token)

        return response


def ws_correlation_id(websocket) -> str:
    """Extract/generate correlation ID for WebSocket connection."""
    # Try to get from query params or headers
    cid = websocket.query_params.get("trace_id") or websocket.headers.get("x-trace-id")
    if not cid:
        cid = generate_correlation_id()
    set_correlation_id(cid)
    return cid


# Convenience function for getting component loggers
def get_logger(name: str) -> logging.Logger:
    """Get a logger for a pathfinding component."""
    return logging.getLogger(f"pathfinding.{name}")