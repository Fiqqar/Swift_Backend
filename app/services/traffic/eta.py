import logging
import math
import os
from datetime import datetime, timedelta, timezone

from app.services.pathfinding.core_a_star import haversine_distance
from app.services.traffic.matcher import snap_segment
from app.services.traffic.poller import _snap_tolerance

logger = logging.getLogger("pathfinding")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "").strip() in ("1", "true", "True", "TRUE", "yes", "on") \
        if os.environ.get(name, "").strip() else default


def eta_config() -> dict:
    """Konfigurasi ETA dari env (lihat docs/feature/eta.md)."""
    default_speed = _env_float("DEFAULT_SPEED_KMH", 40.0)
    mode_speed = _env_float("MODE_AVG_SPEED_KMH", default_speed)
    if mode_speed <= 0:
        mode_speed = default_speed
    return {
        "custom": _env_bool("ENABLE_CUSTOM_ETA", False),
        "service_minutes": max(0.0, _env_float("SERVICE_TIME_MINUTES", 3.0)),
        "mode_speed_kmh": mode_speed,
        "turn_angle_deg": max(0.0, _env_float("TURN_ANGLE_DEG", 30.0)),
        "turn_penalty_s": max(0.0, _env_float("TURN_PENALTY_SECONDS", 5.0)),
    }


def _turn_count(coords: list) -> int:
    """Jumlah belokan tajam (sudut antar segmen > ambang derajat)."""
    if not coords or len(coords) < 3:
        return 0
    angle_deg = eta_config()["turn_angle_deg"]
    if angle_deg <= 0:
        return 0
    count = 0
    # heading segmen dalam derajat (0 = timur, berlawanan jarum jam)
    prev_bearing = None
    for i in range(len(coords) - 1):
        lat1, lon1 = coords[i][0], coords[i][1]
        lat2, lon2 = coords[i + 1][0], coords[i + 1][1]
        dlon = math.radians(lon2 - lon1)
        x = math.sin(dlon) * math.cos(math.radians(lat2))
        y = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
             - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2))
             * math.cos(dlon))
        bearing = math.degrees(math.atan2(x, y)) % 360.0
        if prev_bearing is not None:
            diff = abs(bearing - prev_bearing)
            if diff > 180.0:
                diff = 360.0 - diff
            if diff > angle_deg:
                count += 1
        prev_bearing = bearing
    return count


def compute_eta(graph: dict, locations: dict,
                coords: list, penalties: dict | None = None) -> float | None:
    """Estimasi waktu tempuh (detik) sesuai docs/feature/eta.md.

    - ENABLE_CUSTOM_ETA=False (default): pure travel time = jarak / kecepatan
      standar jalan (DEFAULT_SPEED_KMH).
    - ENABLE_CUSTOM_ETA=True:
        ETA = (jarak / kecepatan rata-rata moda) * traffic_multiplier
              + SERVICE_TIME_MINUTES*60 + belokan_tajam * TURN_PENALTY_SECONDS

    traffic_multiplier diambil dari rata-rata penalty (>1) pada edge yang
    dilalui rute (proyeksi polyline rute ke graf). Tanpa data traffic -> 1.0.
    Kembalikan None bila tidak dapat dihitung (jarak/kecepatan tak valid).
    """
    if not coords or len(coords) < 2:
        return None
    cfg = eta_config()
    speed_kmh = cfg["mode_speed_kmh"] if cfg["custom"] \
        else _env_float("DEFAULT_SPEED_KMH", 40.0)
    if speed_kmh <= 0:
        return None

    total = 0.0
    for i in range(len(coords) - 1):
        total += haversine_distance(
            (coords[i][0], coords[i][1]), (coords[i + 1][0], coords[i + 1][1]))

    base_s = total / (speed_kmh / 3.6)
    if not cfg["custom"]:
        return base_s

    multiplier = 1.0
    if penalties and graph is not None and locations is not None:
        tolerance = _snap_tolerance()
        try:
            affected = snap_segment(graph, locations, coords, tolerance)
        except Exception as exc:
            logger.warning("[ETA] snap_segment gagal: %s", exc)
            affected = []
        weights = [penalties[e] for e in affected
                   if e in penalties and penalties[e] > 1.0]
        if weights:
            multiplier = sum(weights) / len(weights)

    eta = base_s * multiplier
    eta += cfg["service_minutes"] * 60.0
    eta += _turn_count(coords) * cfg["turn_penalty_s"]
    return eta


def estimated_arrival(eta_seconds: float | None,
                      now: datetime | None = None) -> datetime | None:
    """Perkiraan waktu tiba (UTC, timezone-aware) = now + eta_seconds.

    Kembalikan None bila eta_seconds tidak valid (kosong / bukan angka).
    """
    if eta_seconds is None:
        return None
    base = now if now is not None else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base + timedelta(seconds=eta_seconds)
