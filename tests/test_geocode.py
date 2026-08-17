import asyncio

from app.services import geocode


class _FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value


def test_extract_city_prefers_city_key():
    data = {"address": {"city": "Kudus", "town": "Kudus Kota",
                        "county": "Kudus Regency",
                        "display_name": "Kudus, Jawa Tengah"}}
    assert geocode._extract_city_from_reverse(data) == "Kudus"


def test_extract_city_falls_to_town_and_county():
    assert geocode._extract_city_from_reverse(
        {"address": {"town": "Jepara", "county": "Jepara Regency"}}) == "Jepara"
    assert geocode._extract_city_from_reverse(
        {"address": {"county": "Pati Regency"}}) == "Pati Regency"


def test_extract_city_uses_display_name():
    data = {"address": {},
            "display_name": "Desa X, Kabupaten Y, Jawa Tengah"}
    assert geocode._extract_city_from_reverse(data) == "Desa X"


def test_extract_city_none_on_invalid():
    assert geocode._extract_city_from_reverse(None) is None
    assert geocode._extract_city_from_reverse({}) is None
    assert geocode._extract_city_from_reverse(
        {"address": {}, "display_name": ""}) is None


def test_grid_key():
    key = geocode._grid_key(-6.8048, 110.8385)
    assert key.count(",") == 1
    assert key == geocode._grid_key(-6.80481, 110.83851)
    assert key != geocode._grid_key(-6.9, 110.8)


def test_reverse_geocode_city_uses_reverse_and_caches(monkeypatch):
    async def fake_reverse(lat, lon):
        return "Kudus"

    monkeypatch.setattr(geocode, "_nominatim_reverse", fake_reverse)
    redis = _FakeRedis()
    city = asyncio.run(geocode.reverse_geocode_city(
        redis, -6.8048, 110.8385, ttl=100))
    assert city == "Kudus"
    assert redis.data
    assert next(iter(redis.data.values())) == "Kudus"


def test_reverse_geocode_city_reads_cache(monkeypatch):
    async def fake_reverse(lat, lon):
        raise AssertionError("reverse tak boleh dipanggil bila ada cache")

    monkeypatch.setattr(geocode, "_nominatim_reverse", fake_reverse)
    redis = _FakeRedis()
    key = "reverse:" + geocode._grid_key(-6.8048, 110.8385)
    redis.data[key] = "Semarang"
    city = asyncio.run(geocode.reverse_geocode_city(redis, -6.8048, 110.8385))
    assert city == "Semarang"


def test_reverse_geocode_city_failure_returns_none(monkeypatch):
    async def fake_reverse(lat, lon):
        return None

    monkeypatch.setattr(geocode, "_nominatim_reverse", fake_reverse)
    redis = _FakeRedis()
    city = asyncio.run(geocode.reverse_geocode_city(redis, -6.8048, 110.8385))
    assert city is None
    assert not redis.data


def test_reverse_geocode_city_invalid_coords():
    assert asyncio.run(geocode.reverse_geocode_city(
        None, "not-a-number", 110.8)) is None
