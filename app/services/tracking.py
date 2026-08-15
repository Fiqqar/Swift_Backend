"""Helper Redis untuk tracking posisi kurir + state machine geofence.

Key yang dipakai (lihat docs/feature/courier_gps_tracking.md Bagian 5):
- `driver:pos:{kurir_id}`            hash posisi terbaru kurir.
- `driver:geofence:{kurir_id}:{package_id}` state "inside"/"outside" per paket.
- `driver:stops:{kurir_id}`          cache daftar stop belum terkirim.

Semua fungsi aman bila `redis is None` (log warning, tidak raise).
"""

import json
import logging
import os
import time

logger = logging.getLogger("pathfinding")

KURIR_POS_TTL = int(os.environ.get("KURIR_POS_TTL_SECONDS", "600"))
KURIR_GEOFENCE_TTL = int(os.environ.get("KURIR_GEOFENCE_TTL_SECONDS", "86400"))
KURIR_STOPS_TTL = int(os.environ.get("KURIR_STOPS_TTL_SECONDS", "300"))


def pos_key(kurir_id: int) -> str:
    return f"driver:pos:{kurir_id}"


def geofence_key(kurir_id: int, package_id: int) -> str:
    return f"driver:geofence:{kurir_id}:{package_id}"


def stops_key(kurir_id: int) -> str:
    return f"driver:stops:{kurir_id}"


async def set_kurir_position(redis, kurir_id: int, lat: float, lon: float,
                             bearing=None, speed=None, snapped=None) -> bool:
    """Simpan posisi kurir ke Redis. Kembalikan True bila tersimpan."""
    if redis is None:
        return False
    try:
        key = pos_key(kurir_id)
        mapping = {
            "lat": str(lat),
            "lon": str(lon),
            "ts": str(int(time.time())),
        }
        if bearing is not None:
            mapping["bearing"] = str(bearing)
        if speed is not None:
            mapping["speed"] = str(speed)
        if snapped is not None:
            mapping["snapped"] = f"{snapped[0]},{snapped[1]}"
        await redis.hset(key, mapping=mapping)
        await redis.expire(key, KURIR_POS_TTL)
        return True
    except Exception as exc:
        logger.warning("Set posisi kurir gagal: %s", exc)
        return False


async def get_kurir_position(redis, kurir_id: int) -> dict | None:
    if redis is None:
        return None
    try:
        raw = await redis.hgetall(pos_key(kurir_id))
    except Exception as exc:
        logger.warning("Get posisi kurir gagal: %s", exc)
        return None
    if not raw:
        return None
    try:
        return {
            "lat": float(raw["lat"]),
            "lon": float(raw["lon"]),
            "bearing": float(raw["bearing"]) if raw.get("bearing") else None,
            "speed": float(raw["speed"]) if raw.get("speed") else None,
            "ts": int(raw["ts"]) if raw.get("ts") else None,
            "snapped": raw.get("snapped"),
        }
    except (KeyError, ValueError):
        return None


async def get_kurir_position_latlon(redis, kurir_id: int) -> tuple | None:
    pos = await get_kurir_position(redis, kurir_id)
    if pos is None:
        return None
    return (pos["lat"], pos["lon"])


async def get_geofence_state(redis, kurir_id: int, package_id: int) -> str | None:
    if redis is None:
        return None
    try:
        return await redis.get(geofence_key(kurir_id, package_id))
    except Exception as exc:
        logger.warning("Get geofence state gagal: %s", exc)
        return None


async def set_geofence_state(redis, kurir_id: int, package_id: int,
                             state: str) -> None:
    if redis is None:
        return
    try:
        await redis.set(geofence_key(kurir_id, package_id), state,
                        ex=KURIR_GEOFENCE_TTL)
    except Exception as exc:
        logger.warning("Set geofence state gagal: %s", exc)


async def del_geofence_state(redis, kurir_id: int, package_id: int) -> None:
    if redis is None:
        return
    try:
        await redis.delete(geofence_key(kurir_id, package_id))
    except Exception as exc:
        logger.warning("Del geofence state gagal: %s", exc)


async def set_stops_cache(redis, kurir_id: int, stops: list) -> None:
    if redis is None:
        return
    try:
        await redis.set(stops_key(kurir_id), json.dumps(stops),
                        ex=KURIR_STOPS_TTL)
    except Exception as exc:
        logger.warning("Set stops cache gagal: %s", exc)


async def get_stops_cache(redis, kurir_id: int) -> list | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(stops_key(kurir_id))
    except Exception as exc:
        logger.warning("Get stops cache gagal: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def del_stops_cache(redis, kurir_id: int) -> None:
    if redis is None:
        return
    try:
        await redis.delete(stops_key(kurir_id))
    except Exception as exc:
        logger.warning("Del stops cache gagal: %s", exc)


def geofence_event(prev_state: str | None, inside: bool, package_id: int,
                   distance_m: float, radius_m: float) -> dict | None:
    """Kembalikan event push geofence hanya saat terjadi transisi state.

    - prev `inside` -> sekarang masih inside: tidak ada event (anti-spam).
    - prev outside/None -> inside: `geofence_enter`.
    - prev inside -> outside: `geofence_exit`.
    - prev outside/None -> outside: tidak ada event (hindari spam awal).
    """
    if inside:
        if prev_state == "inside":
            return None
        return {"type": "geofence_enter", "package_id": package_id,
                "distance_m": round(float(distance_m), 2),
                "radius_m": radius_m}
    if prev_state == "inside":
        return {"type": "geofence_exit", "package_id": package_id,
                "distance_m": round(float(distance_m), 2),
                "radius_m": radius_m}
    return None


def resolve_start_point(courier_position, redis_position, hub_origin):
    """Hirarki fallback titik awal rute (Bagian 6.1 design doc).

    1. `courier_position` (prioritas 1) -> tuple (lat, lon).
    2. `redis_position` (prioritas 2)    -> tuple (lat, lon).
    3. `hub_origin` (fallback terakhir)  -> tuple (lat, lon).
    Bila semua None, kembalikan None.
    """
    if courier_position is not None:
        return tuple(courier_position)
    if redis_position is not None:
        return tuple(redis_position)
    if hub_origin is not None:
        return tuple(hub_origin)
    return None
