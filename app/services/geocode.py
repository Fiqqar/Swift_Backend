import asyncio
import hashlib
import logging
import math
import os
import re

import httpx

logger = logging.getLogger("pathfinding")

NOMINATIM_URL = os.environ.get(
    "NOMINATIM_URL", "https://nominatim.openstreetmap.org"
).rstrip("/")
GEOCODE_TTL = int(os.environ.get("GEOCODE_TTL", "2592000"))
GEOCODE_TIMEOUT = float(os.environ.get("GEOCODE_TIMEOUT", "8.0"))
GEOCODE_RATE_DELAY = float(os.environ.get("GEOCODE_RATE_DELAY", "1.0"))
GEOCODE_USER_AGENT = os.environ.get(
    "GEOCODE_USER_AGENT", "Test2Pathfinding/1.0 (multi-stop delivery)"
)

_geocode_semaphore = asyncio.Semaphore(1)

_HOUSE_NO = re.compile(r"^(?:no\.?|nomor)?\s*\d+[a-zA-Z]?(?:\s*[/-]\s*\d+[a-zA-Z]?)?", re.I)


def _cache_key(alamat: str) -> str:
    digest = hashlib.sha1(alamat.strip().lower().encode("utf-8"))
    return f"geocode:{digest.hexdigest()[:32]}"


def _query_variants(alamat: str) -> list[str]:
    """Turunkan beberapa kandidat query dari alamat lengkap (paling spesifik
    ke paling umum) agar Nominatim punya peluang mencocokkan."""
    variants = [alamat.strip()]
    stripped = _HOUSE_NO.sub("", alamat.strip()).strip(" ,-")
    if stripped and stripped not in variants:
        variants.append(stripped)

    parts = [p.strip(" ,") for p in alamat.split(",") if p.strip(" ,")]
    if len(parts) > 2:
        # lepas nomor jalan; coba kecamatan + kota
        joined = ", ".join(parts[1:])
        if joined not in variants:
            variants.append(joined)
    if len(parts) > 1:
        joined = ", ".join(parts[-2:])
        if joined not in variants:
            variants.append(joined)
    if parts:
        city = parts[-1].strip(" ,")
        if city and city not in variants:
            variants.append(city)
    return variants


async def _from_cache(redis, key: str) -> tuple[float, float] | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
    except Exception as exc:
        logger.warning("Geocode cache read gagal: %s", exc)
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        lat, _, lon = raw.partition(",")
        return float(lat), float(lon)
    except (ValueError, AttributeError):
        return None


async def _to_cache(redis, key: str, lat: float, lon: float) -> None:
    if redis is None:
        return
    try:
        await redis.set(key, f"{lat},{lon}", ex=GEOCODE_TTL)
    except Exception as exc:
        logger.warning("Geocode cache write gagal: %s", exc)


async def _nominatim(alamat: str) -> tuple[float, float] | None:
    async with _geocode_semaphore:
        try:
            async with httpx.AsyncClient(
                timeout=GEOCODE_TIMEOUT,
                headers={"User-Agent": GEOCODE_USER_AGENT},
                follow_redirects=True,
            ) as client:
                resp = await client.get(
                    f"{NOMINATIM_URL}/search",
                    params={"q": alamat, "format": "json", "limit": 1},
                )
            if resp.status_code != 200:
                logger.warning(
                    "Nominatim %d untuk '%s'", resp.status_code, alamat)
                return None
            results = resp.json()
            if not results:
                return None
            return float(results[0]["lat"]), float(results[0]["lon"])
        except Exception as exc:
            logger.warning("Nominatim gagal untuk '%s': %s", alamat, exc)
            return None
        finally:
            if GEOCODE_RATE_DELAY > 0:
                await asyncio.sleep(GEOCODE_RATE_DELAY)


def _grid_key(lat: float, lon: float) -> str:
    dlat = 0.0045
    dlon = dlat / max(0.1, math.cos(math.radians(lat)))
    return "%d,%d" % (math.floor(lat / dlat), math.floor(lon / dlon))


async def _reverse_cache_get(redis, key: str) -> str | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
    except Exception as exc:
        logger.warning("Reverse geocode cache read gagal: %s", exc)
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    return raw or None


async def _reverse_cache_set(redis, key: str, city: str, ttl: int) -> None:
    if redis is None:
        return
    try:
        await redis.set(key, city, ex=ttl)
    except Exception as exc:
        logger.warning("Reverse geocode cache write gagal: %s", exc)


def _extract_city_from_reverse(data) -> str | None:
    """Ekstrak nama kota dari respons Nominatim /reverse."""
    if not isinstance(data, dict):
        return None
    address = data.get("address") or {}
    for key in ("city", "town", "village", "county"):
        value = str(address.get(key, "")).strip()
        if value:
            return value
    display = str(data.get("display_name", "")).strip()
    if display:
        return display.split(",")[0].strip() or None
    return None


async def _nominatim_reverse(lat: float, lon: float) -> str | None:
    """Reverse geocode via Nominatim, kembalikan nama kota (atau None)."""
    async with _geocode_semaphore:
        try:
            async with httpx.AsyncClient(
                timeout=GEOCODE_TIMEOUT,
                headers={"User-Agent": GEOCODE_USER_AGENT},
                follow_redirects=True,
            ) as client:
                resp = await client.get(
                    f"{NOMINATIM_URL}/reverse",
                    params={"lat": lat, "lon": lon, "format": "jsonv2"},
                )
            if resp.status_code != 200:
                logger.warning(
                    "Nominatim reverse %d untuk (%.5f,%.5f)",
                    resp.status_code, lat, lon)
                return None
            data = resp.json()
        except Exception as exc:
            logger.warning(
                "Nominatim reverse gagal untuk (%.5f,%.5f): %s",
                lat, lon, exc)
            return None
        finally:
            if GEOCODE_RATE_DELAY > 0:
                await asyncio.sleep(GEOCODE_RATE_DELAY)
    return _extract_city_from_reverse(data)


async def reverse_geocode_city(redis, lat: float, lon: float,
                               ttl: int | None = None) -> str | None:
    """Ubah koordinat menjadi nama kota via Nominatim (cache per grid).

    Kembalikan nama kota (city/town/village/county) atau None bila gagal.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    key = "reverse:" + _grid_key(lat_f, lon_f)
    cached = await _reverse_cache_get(redis, key)
    if cached:
        return cached
    city = await _nominatim_reverse(lat_f, lon_f)
    if city:
        await _reverse_cache_set(
            redis, key, city, int(ttl) if ttl else int(GEOCODE_TTL))
    return city


async def geocode_address(redis, alamat: str) -> tuple[float, float] | None:
    """Mengubah alamat jadi (lat, lon) via Nominatim dengan cache Redis.

    Mencoba beberapa variasi query (dari paling spesifik ke umum) dan
    menyimpan hasil pertama yang cocok ke Redis.
    """
    if not alamat or not alamat.strip():
        return None
    key = _cache_key(alamat)
    cached = await _from_cache(redis, key)
    if cached is not None:
        return cached
    for variant in _query_variants(alamat):
        result = await _nominatim(variant)
        if result is not None:
            await _to_cache(redis, key, result[0], result[1])
            return result
    return None
