import logging
import os
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger("pathfinding")


@dataclass
class TrafficEvent:
    coordinates: list[tuple[float, float]] = field(default_factory=list)
    multiplier: float = 1.0
    closure: bool = False
    provider: str | None = None

    @property
    def weight(self) -> float:
        if self.closure:
            return float("inf")
        return self.multiplier


class TrafficProvider(Protocol):
    name: str

    async def fetch(self) -> list[TrafficEvent]:
        ...


def traffic_enabled() -> bool:
    return os.environ.get("ENABLE_REALTIME_TRAFFIC", "0").strip() in (
        "1", "true", "True", "TRUE", "yes", "on")


def provider_mode() -> str:
    mode = os.environ.get("TRAFFIC_PROVIDER_MODE", "smart_hybrid").strip().lower()
    if mode not in ("smart_hybrid", "internal_only", "full_tomtom"):
        logger.warning(
            "[TRAFFIC] TRAFFIC_PROVIDER_MODE tak dikenal: %r, pakai "
            "smart_hybrid.", mode)
        return "smart_hybrid"
    return mode


def get_providers() -> list[TrafficProvider]:
    from app.services.traffic.internal import InternalIncidentProvider
    from app.services.traffic.tomtom import TomTomProvider

    mode = provider_mode()
    if mode == "full_tomtom":
        return [TomTomProvider(), InternalIncidentProvider()]
    return [InternalIncidentProvider()]
