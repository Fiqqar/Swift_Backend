import asyncio
import logging
import os

import httpx

from app.services.traffic.provider import TrafficEvent

logger = logging.getLogger("pathfinding")

_TIMEOUT = 8.0
_MAX_CONCURRENCY = 5


class TomTomProvider:
    """Integrasi data lalu lintas TomTom (Traffic Flow Segment Data API).

    API bersifat point-based: satu request per titik probe (lat,lon), dan
    respons mengembalikan segmen jalan terdekat dengan kecepatan arus saat
    ini vs free-flow. Multiplier dihitung sebagai freeFlowSpeed/currentSpeed
    (clamp 1..10); roadClosure=true berarti penutupan jalan total (infinity).

    Diaktifkan dengan TOMTOM_KEY dan daftar titik probe di
    TOMTOM_PROBE_POINTS (format "lat,lon|lat,lon|..."). Tanpa keduanya,
    fetch() mengembalikan [] sehingga aman di-off secara default.
    """

    name = "tomtom"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("TOMTOM_KEY", "").strip() or None
        self.base_url = os.environ.get(
            "TOMTOM_BASE_URL", "https://api.tomtom.com").rstrip("/")
        self.zoom = _int_env("TOMTOM_ZOOM", 10)
        self.confidence_min = _float_env("TOMTOM_CONFIDENCE_MIN", 0.5)
        self.max_requests = int(_int_env("TOMTOM_MAX_REQUESTS", 0))
        self.timeout = _float_env("TOMTOM_TIMEOUT", _TIMEOUT)
        self.probe_points = _parse_probe_points(
            os.environ.get("TOMTOM_PROBE_POINTS", "").strip())

    async def fetch(self) -> list[TrafficEvent]:
        if not self.api_key:
            return []
        if not self.probe_points:
            logger.warning(
                "[TRAFFIC] TOMTOM_KEY terisi tapi TOMTOM_PROBE_POINTS kosong, "
                "tidak ada segmen diambil.")
            return []
        points = self.probe_points
        if self.max_requests > 0:
            points = points[: self.max_requests]
        return await self.fetch_points(points)

    async def fetch_points(
            self, points: list[tuple[float, float]]) -> list[TrafficEvent]:
        """Query TomTom untuk daftar titik probe tertentu (on-demand).

        Dipakai mode smart_hybrid: panggil hanya utk titik probe di sekitar
        Origin & Destination. Mengembalikan [] bila tanpa key.
        """
        if not self.api_key:
            return []
        if not points:
            return []
        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            tasks = [
                self._fetch_point(client, semaphore, lat, lon)
                for lat, lon in points
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        events: list[TrafficEvent] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("[TRAFFIC] TomTom request gagal: %s", result)
                continue
            if result is not None:
                events.append(result)
        return events

    async def _fetch_point(self, client, semaphore, lat: float, lon: float):
        async with semaphore:
            url = (
                f"{self.base_url}/traffic/services/4/flowSegmentData/absolute/"
                f"{self.zoom}/json"
            )
            params = {
                "point": f"{lat},{lon}",
                "unit": "KMPH",
                "key": self.api_key,
            }
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return self._parse_segment(response.json())
            except Exception as exc:
                logger.warning(
                    "[TRAFFIC] TomTom point (%.5f,%.5f) gagal: %s",
                    lat, lon, exc)
                return None

    def _parse_segment(self, data) -> TrafficEvent | None:
        """Terjemahkan satu respons flowSegmentData menjadi TrafficEvent.

        Kembalikan None bila data tidak valid atau confidence di bawah ambang.
        """
        try:
            flow = data.get("flowSegmentData") or {}
            coordinates = (flow.get("coordinates") or {}).get("coordinate") or []
            points = [
                (float(c["latitude"]), float(c["longitude"]))
                for c in coordinates
                if "latitude" in c and "longitude" in c
            ]
            if len(points) < 2:
                return None
            confidence = float(flow.get("confidence", 1.0))
            if confidence < self.confidence_min:
                return None
            closure = bool(flow.get("roadClosure", False))
            if closure:
                return TrafficEvent(
                    coordinates=points, multiplier=1.0,
                    closure=True, provider=self.name)
            current = float(flow.get("currentSpeed", 0) or 0)
            free_flow = float(flow.get("freeFlowSpeed", 0) or 0)
            if current <= 0 or free_flow <= 0:
                return None
            multiplier = free_flow / max(current, 1.0)
            multiplier = max(1.0, min(10.0, multiplier))
            return TrafficEvent(
                coordinates=points, multiplier=multiplier,
                closure=False, provider=self.name)
        except (TypeError, ValueError, KeyError):
            return None


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _parse_probe_points(raw: str) -> list[tuple[float, float]]:
    points = []
    for part in raw.split("|"):
        part = part.strip()
        if not part:
            continue
        coords = [c.strip() for c in part.split(",")]
        try:
            if len(coords) == 2:
                points.append((float(coords[0]), float(coords[1])))
        except ValueError:
            logger.warning("[TRAFFIC] TOMTOM_PROBE_POINTS tak valid: %s", part)
    return points
