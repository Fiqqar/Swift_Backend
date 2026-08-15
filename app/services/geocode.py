import asyncio
import hashlib
import logging
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
