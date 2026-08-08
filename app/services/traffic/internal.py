import json
import logging
import os

import httpx

from app.services.traffic.provider import TrafficEvent

logger = logging.getLogger("pathfinding")

TIMEOUT = 8.0


class InternalIncidentProvider:
    """Sumber data traffic internal (Incident Service).

    Provider mengambil daftar insiden/segmen macet dari endpoint HTTP
    internal (TRAFFIC_INTERNAL_URL) dengan format JSON:

        {
          "incidents": [
            {"points": [[lat, lon], ...], "multiplier": 2.5},
            {"points": [[lat, lon], ...], "closure": true}
          ]
        }

    Bila URL tidak dikonfigurasi atau tidak terjangkau, fetch() mengembalikan
    [] sehingga integrasi aman di-off secara default.
    """

    name = "internal"

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("TRAFFIC_INTERNAL_URL", "").strip() or None

    async def fetch(self) -> list[TrafficEvent]:
        if not self.url:
            return []
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("[TRAFFIC] Internal incident fetch gagal: %s", exc)
            return []
        return self._parse(data)

    def _parse(self, data) -> list[TrafficEvent]:
        events: list[TrafficEvent] = []
        for item in data.get("incidents", []) if isinstance(data, dict) else data:
            try:
                points = [
                    (float(p[0]), float(p[1]))
                    for p in item.get("points", [])
                ]
                if len(points) < 2:
                    continue
                events.append(TrafficEvent(
                    coordinates=points,
                    multiplier=float(item.get("multiplier", 1.0)),
                    closure=bool(item.get("closure", False)),
                    provider=self.name,
                ))
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning("[TRAFFIC] Item internal tidak valid %s: %s",
                               json.dumps(item, ensure_ascii=False), exc)
        return events
