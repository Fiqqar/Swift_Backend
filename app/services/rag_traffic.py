"""RAG Berita Publik (Google Gemini Grounding) untuk penalti rute.

Fitur:
- In-memory vector store per kota berisi berita gangguan lalu lintas
  (hasil ingestion Gemini Grounding + Google Search).
- Ingestion worker `rag_ingestion_worker` memperbarui store `RAG_NEWS_CITY`
  secara periodik (didaftarkan dari lifespan `app/main.py`).
- Kota dinamis: `_detect_city_from_coords` menentukan kota tempat kurir
  berada via reverse geocode Nominatim dari koordinat polyline rute aktif,
  lalu `_ensure_city_news` mengingest berita kota itu on-demand
  (fire-and-forget) sehingga query Grounding menyesuaikan lokasi kurir.
- `retrieve_and_evaluate_road_incidents(polyline_coords, *, app, redis)`:
  ambil berita yang relevan di dekat koridor rute, evaluasi penalti via
  Gemini, lalu snap koordinat terdampak ke edge graf -> dict
  {edge_id: multiplier} siap digabung ke penalti rute (mis. di
  `compute_reroute`).

Semua operasi opsional dan di-gate `RAG_NEWS_ENABLED` (default off).
Kegagalan apa pun bersifat non-fatal: memanggil balik ke routing biasa.
"""

import asyncio
import json
import math
import os
import re
import time

from starlette.concurrency import run_in_threadpool

from app.core.logging import get_logger
from app.services import ai_agent
from app.services.pathfinding.core_a_star import haversine_distance
from app.services.traffic.matcher import snap_segment
from app.services.traffic.poller import _snap_tolerance

logger = get_logger("rag")

# ---------------------------------------------------------------------------
# Konfigurasi (dibaca saat import, pola sama seperti ai_agent).
# ---------------------------------------------------------------------------
RAG_NEWS_ENABLED = ai_agent._env_bool("RAG_NEWS_ENABLED", False)
RAG_NEWS_CITY = os.environ.get("RAG_NEWS_CITY", "").strip()
RAG_NEWS_INGEST_INTERVAL_S = ai_agent._env_float("RAG_NEWS_INGEST_INTERVAL_S", 3600.0)
RAG_NEWS_TOP_K = max(1, ai_agent._env_int("RAG_NEWS_TOP_K", 5))
RAG_NEWS_CACHE_TTL_S = ai_agent._env_float("RAG_NEWS_CACHE_TTL_S", 1800.0)
RAG_NEWS_MAX_PENALTY = ai_agent._env_float("RAG_NEWS_MAX_PENALTY", 3.0)
RAG_NEWS_TIMEOUT_S = ai_agent._env_float("RAG_NEWS_TIMEOUT_S", 2.0)
RAG_NEWS_EMBED_MODEL = os.environ.get(
    "RAG_NEWS_EMBED_MODEL", "text-embedding-004").strip()
# 1 = deteksi kota dinamis dari koordinat rute via reverse geocode
#     (fallback ke RAG_NEWS_CITY bila gagal). Default aktif.
RAG_NEWS_DYNAMIC_CITY = ai_agent._env_bool("RAG_NEWS_DYNAMIC_CITY", True)
# TTL (detik) cache hasil reverse geocode kota per grid di Redis.
RAG_NEWS_CITY_CACHE_TTL_S = ai_agent._env_float(
    "RAG_NEWS_CITY_CACHE_TTL_S", 3600.0)

_SEVERITY_MULTIPLIER = {
    "LOW": 1.2,
    "MEDIUM": 1.8,
    "HIGH": 2.5,
    "BLOCKING": float("inf"),
}

# Cache hasil evaluasi penalti per kejadian (in-memory, TTL singkat) agar
# reroute berulang tidak memanggil Gemini setiap siklus 30 detik.
_eval_cache: dict[str, tuple] = {}


def _clamp(value, lo: float, hi: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, value))


def _k(value) -> float:
    return round(float(value), 5)


def _cosine(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _severity_multiplier(severity: str) -> float:
    return _SEVERITY_MULTIPLIER.get((severity or "MEDIUM").strip().upper(), 1.8)


def _parse_json_array(text: str) -> list:
    """Ekstrak JSON array pertama dari jawaban model (tahan code fence)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    data = None
    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                pass
    if not isinstance(data, list):
        return []
    return data


def _parse_news_items(text: str) -> list[dict]:
    """Parse jawaban ingestion menjadi item berita yang disanitasi."""
    items = []
    for idx, raw in enumerate(_parse_json_array(text)):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "")).strip()
        if not title:
            continue
        try:
            lat = float(raw["lat"])
            lng = float(raw["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            continue
        severity = str(raw.get("severity", "MEDIUM")).strip().upper()
        if severity not in _SEVERITY_MULTIPLIER:
            severity = "MEDIUM"
        items.append({
            "id": "news:%d" % idx,
            "title": title,
            "summary": ai_agent._sanitize_reason(raw.get("summary", "")),
            "lat": lat,
            "lng": lng,
            "radius_m": _clamp(raw.get("radius_m", 500), 50, 2000),
            "severity": severity,
        })
    return items


def _parse_evaluation(text: str) -> list[dict]:
    """Parse hasil evaluasi dampak menjadi dict penalti yang disanitasi."""
    out = []
    for raw in _parse_json_array(text):
        if not isinstance(raw, dict):
            continue
        try:
            lat = float(raw["lat"])
            lng = float(raw["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            continue
        out.append({
            "lat": lat,
            "lng": lng,
            "radius_m": _clamp(raw.get("radius_m", 500), 50, 2000),
            "penalty_multiplier": _clamp(
                raw.get("penalty_multiplier", 1.0),
                1.0, RAG_NEWS_MAX_PENALTY),
            "reason": ai_agent._sanitize_reason(raw.get("reason", "")),
        })
    return out


# ---------------------------------------------------------------------------
# In-memory vector store per kota (cosine similarity, singleton + lock).
# ---------------------------------------------------------------------------
class _NewsStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._docs_by_city: dict[str, list[dict]] = {}
        self._vectors_by_city: dict[str, list[list[float]]] = {}
        self._updated_by_city: dict[str, float] = {}

    def size(self) -> int:
        return sum(len(docs) for docs in self._docs_by_city.values())

    def has_city(self, city: str) -> bool:
        return bool(self._docs_by_city.get(city))

    def city_size(self, city: str) -> int:
        return len(self._docs_by_city.get(city, []))

    def last_updated(self, city: str | None = None) -> float:
        if city is None:
            return max(self._updated_by_city.values()) \
                if self._updated_by_city else 0.0
        return self._updated_by_city.get(city, 0.0)

    def clear(self) -> None:
        self._docs_by_city = {}
        self._vectors_by_city = {}
        self._updated_by_city = {}

    async def set_city(self, city: str, items: list[dict],
                       vectors: list[list[float]]) -> None:
        async with self._lock:
            self._docs_by_city[city] = list(items)
            self._vectors_by_city[city] = [list(v) for v in vectors]
            self._updated_by_city[city] = time.time()

    async def search(self, query_vec: list[float],
                     top_k: int) -> list[dict]:
        async with self._lock:
            docs: list[dict] = []
            vectors: list[list[float]] = []
            for city in self._docs_by_city:
                docs.extend(self._docs_by_city[city])
                vectors.extend(self._vectors_by_city.get(city, []))
        if not vectors:
            return []
        scored = sorted(
            ((_cosine(query_vec, v), i) for i, v in enumerate(vectors)),
            key=lambda item: item[0], reverse=True)
        return [dict(docs[i]) for _, i in scored[:max(1, top_k)]]


_store = _NewsStore()

# Tracking ingestion fire-and-forget per kota (hindari dobel & re-spam).
_last_ingest_by_city: dict[str, float] = {}
_ingest_tasks: dict[str, asyncio.Task] = {}


def reset_for_test() -> None:
    """Kosongkan store, cache evaluasi & state ingestion (untuk pengujian)."""
    _store.clear()
    _eval_cache.clear()
    for task in _ingest_tasks.values():
        task.cancel()
    _ingest_tasks.clear()
    _last_ingest_by_city.clear()


# ---------------------------------------------------------------------------
# Embedding (text-embedding-004 via google-genai).
# ---------------------------------------------------------------------------
def _embed_once(client, texts: list[str]) -> list[list[float]]:
    resp = client.models.embed_content(
        model=RAG_NEWS_EMBED_MODEL, contents=texts)
    return [list(e.values) for e in resp.embeddings]


async def _embed_texts(texts: list[str],
                       timeout_s: float | None = None) -> list[list[float]]:
    if not texts:
        return []
    if not ai_agent.GEMINI_API_KEY:
        return []
    from google import genai

    primary = genai.Client(api_key=ai_agent.GEMINI_API_KEY)
    backup = (genai.Client(api_key=ai_agent.GEMINI_API_KEY_2)
              if ai_agent.GEMINI_API_KEY_2 else None)
    timeout = timeout_s if timeout_s is not None else RAG_NEWS_TIMEOUT_S

    async def _try(client) -> list[list[float]]:
        async with ai_agent._decide_semaphore:
            return await asyncio.wait_for(
                run_in_threadpool(_embed_once, client, texts),
                timeout=timeout)

    try:
        return await _try(primary)
    except asyncio.TimeoutError:
        logger.warning("[RAG] Embedding timeout setelah %.1fs.", timeout)
        return []
    except Exception as exc:
        if ai_agent._is_rate_limit(exc) and backup is not None:
            try:
                return await _try(backup)
            except Exception as exc2:
                logger.warning("[RAG] Embedding (backup) gagal: %s", exc2)
                return []
        logger.warning("[RAG] Embedding gagal: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Ingestion Gemini Grounding (Google Search).
# ---------------------------------------------------------------------------
def _ground_search_once(client, model: str, prompt: str) -> str:
    from google.genai import types

    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.2,
    )
    contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
    resp = client.models.generate_content(model=model, contents=contents, config=config)
    parts = resp.candidates[0].content.parts if resp.candidates else []
    return "".join(p.text for p in parts if p.text)


def _build_news_prompt(city: str) -> str:
    return (
        "Cari berita publik terkini tentang gangguan lalu lintas di sekitar "
        f"{city}, Indonesia: penutupan jalan, banjir, perbaikan jalan, "
        "kecelakaan, demo, atau pohon tumbang yang berdampak pada "
        "pengendara motor/mobil.\n"
        "Untuk setiap kejadian relevan berikan perkiraan koordinat (lat, lng) "
        "lokasi terdampak.\n"
        'Jawab HANYA satu JSON array tanpa markdown dan tanpa teks lain:\n'
        '[{"title":"<judul>","summary":"<ringkasan 1-2 kalimat>",'
        '"lat":<angka>,"lng":<angka>,"radius_m":<meter 50-5000>,'
        '"severity":"LOW"|"MEDIUM"|"HIGH"|"BLOCKING"}]\n'
        "Jika tidak ada berita relevan, balas []."
    )


async def ingest_road_news(redis=None, city: str | None = None) -> int:
    """Ambil berita lalu lintas via Gemini Grounding lalu simpan ke store."""
    city = (city or RAG_NEWS_CITY or "").strip()
    if not city or not ai_agent.GEMINI_API_KEY:
        return 0
    from app.services.traffic.smart_hybrid import (
        _redis_get_json,
        _redis_set_json,
    )

    redis_key = "rag:news:" + re.sub(
        r"[^a-z0-9]+", "_", city.lower()).strip("_")
    cached = await _redis_get_json(redis, redis_key)
    items = cached.get("items") if isinstance(cached, dict) else None
    vectors = cached.get("vectors") if isinstance(cached, dict) else None
    if isinstance(items, list) and items:
        logger.info("[RAG] Pakai cache berita %s (%d item).",
                    redis_key, len(items))
    else:
        prompt = _build_news_prompt(city)
        text = None
        if ai_agent.GEMINI_API_KEY:
            from google import genai

            primary = genai.Client(api_key=ai_agent.GEMINI_API_KEY)
            backup = (genai.Client(api_key=ai_agent.GEMINI_API_KEY_2)
                      if ai_agent.GEMINI_API_KEY_2 else None)

            async def _try(client) -> str:
                async with ai_agent._decide_semaphore:
                    return await asyncio.wait_for(
                        run_in_threadpool(
                            _ground_search_once, client,
                            ai_agent.GEMINI_MODEL, prompt),
                        timeout=RAG_NEWS_TIMEOUT_S)

            try:
                text = await _try(primary)
            except asyncio.TimeoutError:
                logger.warning("[RAG] Grounding timeout setelah %.1fs.",
                               RAG_NEWS_TIMEOUT_S)
            except Exception as exc:
                if ai_agent._is_rate_limit(exc) and backup is not None:
                    try:
                        text = await _try(backup)
                    except Exception as exc2:
                        logger.warning(
                            "[RAG] Grounding (backup) gagal: %s", exc2)
                else:
                    if ai_agent._is_rate_limit(exc):
                        logger.error("[RAG] Kedua key habis (429 RESOURCE_EXHAUSTED). Fallback ke cache/empty.")
                    else:
                        logger.warning("[RAG] Grounding gagal: %s", exc)
        items = _parse_news_items(text) if text else []
        vectors = None
        if not items:
            return 0
    # Vektor cache kadaluarsa (bentuk lama) -> embed ulang & perbarui cache.
    if not isinstance(vectors, list) or len(vectors) != len(items):
        embedded = await _embed_texts(
            ["%s. %s" % (it["title"], it["summary"]) for it in items],
            timeout_s=max(RAG_NEWS_TIMEOUT_S, 15.0))
        if not embedded or len(embedded) != len(items):
            logger.warning(
                "[RAG] Embedding berita gagal; store %s tidak diperbarui.",
                city)
            return 0
        vectors = embedded
        await _redis_set_json(
            redis, redis_key, {"items": items, "vectors": vectors},
            int(RAG_NEWS_CACHE_TTL_S))
    await _store.set_city(city, items, vectors)
    return len(items)


async def rag_ingestion_worker(app, redis=None):
    """Loop periodik: perbarui store berita untuk `RAG_NEWS_CITY`."""
    if not RAG_NEWS_ENABLED:
        return
    city = (RAG_NEWS_CITY or "").strip()
    if not city:
        logger.warning(
            "[RAG] RAG_NEWS_CITY kosong; ingestion berita nonaktif.")
        return
    interval = max(60.0, RAG_NEWS_INGEST_INTERVAL_S)
    logger.info("[RAG] Ingestion berita aktif untuk kota '%s' "
                "(interval %.0fs).", city, interval)
    while True:
        try:
            count = await ingest_road_news(redis, city=city)
            if count:
                logger.info("[RAG] Store berita diperbarui: %d item.", count)
        except Exception as exc:
            logger.warning("[RAG] Ingestion berita gagal: %s", exc)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Kota dinamis (reverse geocode) & ingestion on-demand per kota.
# ---------------------------------------------------------------------------
def _static_city() -> str:
    return (RAG_NEWS_CITY or "").strip()


async def _detect_city_from_coords(coords: list, redis) -> str:
    """Tentukan kota dari koordinat polyline rute aktif.

    Reverse geocode titik pertama polyline via Nominatim (cache per grid di
    Redis). Bila nonaktif / gagal / tidak ada koordinat, fallback ke
    `RAG_NEWS_CITY` statis.
    """
    if not RAG_NEWS_DYNAMIC_CITY:
        return _static_city()
    point = None
    for p in coords or []:
        try:
            point = (float(p[0]), float(p[1]))
            break
        except (TypeError, ValueError, IndexError):
            continue
    if point is None:
        return _static_city()
    try:
        from app.services.geocode import reverse_geocode_city

        city = await reverse_geocode_city(
            redis, point[0], point[1],
            ttl=int(RAG_NEWS_CITY_CACHE_TTL_S))
        if city:
            return city
    except Exception as exc:
        logger.warning("[RAG] Reverse geocode kota gagal: %s", exc)
    return _static_city()


async def _ingest_and_mark(redis, city: str) -> None:
    """Jalankan ingestion untuk satu kota lalu catat timestamp-nya."""
    try:
        count = await ingest_road_news(redis, city=city)
        if count:
            logger.info("[RAG] Berita kota '%s' siap: %d item.", city, count)
    except Exception as exc:
        logger.warning("[RAG] Ingestion on-demand kota '%s' gagal: %s",
                       city, exc)
    finally:
        _last_ingest_by_city[city] = time.monotonic()
        _ingest_tasks.pop(city, None)


async def _ensure_city_news(redis, city: str) -> bool:
    """Pastikan store berisi berita untuk `city` (fire-and-forget).

    Return True bila data kota sudah tersedia di store (siap dipakai).
    Bila belum, spawn task ingestion dan return False — siklus reroute
    berikutnya akan memakai berita yang baru.
    """
    if not city:
        return False
    interval = max(60.0, RAG_NEWS_INGEST_INTERVAL_S)

    # Throttle check berlaku untuk SEMUA kota — bukan hanya yang punya berita.
    # Sebelumnya, kota tanpa berita tidak pernah masuk cooldown → spam Gemini.
    last = _last_ingest_by_city.get(city, 0.0)
    if time.monotonic() - last < interval:
        return _store.has_city(city)

    existing = _ingest_tasks.get(city)
    if existing is not None and not existing.done():
        return False
    task = asyncio.create_task(_ingest_and_mark(redis, city))
    _ingest_tasks[city] = task
    return False


# ---------------------------------------------------------------------------
# Retrieval + evaluasi dampak berita untuk koridor rute.
# ---------------------------------------------------------------------------
def _corridor_query_text(coords: list) -> str:
    lats = [p[0] for p in coords]
    lons = [p[1] for p in coords]
    clat = sum(lats) / len(lats)
    clon = sum(lons) / len(lons)
    span_km = max(
        0.5,
        haversine_distance((lats[0], lons[0]), (lats[-1], lons[-1])) / 1000.0,
    )
    return (
        "gangguan lalu lintas di sekitar (%.4f,%.4f) radius ~%.0f km: "
        "penutupan jalan, banjir, perbaikan jalan, kecelakaan"
        % (clat, clon, span_km)
    )


async def _retrieve_relevant(coords: list) -> list[dict]:
    """Embed query koridor, cari top-K, lalu filter jarak ke polyline."""
    vectors = await _embed_texts([_corridor_query_text(coords)])
    if not vectors:
        return []
    query_vec = vectors[0]
    candidates = await _store.search(query_vec, RAG_NEWS_TOP_K)
    if not candidates:
        return []
    from app.services.navigation import point_to_polyline_distance_m

    relevant = []
    for item in candidates:
        dist = point_to_polyline_distance_m(item["lat"], item["lng"], coords)
        if dist <= item["radius_m"]:
            relevant.append(item)
    return relevant


def _build_eval_prompt(coords: list, items: list[dict]) -> str:
    corridor = _corridor_query_text(coords)
    incidents = [{
        "title": it["title"],
        "summary": it["summary"],
        "lat": it["lat"],
        "lng": it["lng"],
        "severity": it["severity"],
    } for it in items]
    incidents_json = json.dumps(incidents, ensure_ascii=False)
    return (
        "Kamu mengevaluasi dampak berita gangguan lalu lintas terhadap rute "
        "pengiriman.\n"
        "Koridor rute: %s\n"
        "Berita (JSON):\n%s\n"
        "Untuk setiap berita yang benar-benar memengaruhi lalu lintas jalan "
        "di koridor rute, tentukan koordinat & radius dampak:\n"
        '- "lat","lng": koordinat titik terdampak.\n'
        '- "radius_m": radius dampak dalam meter (50-5000).\n'
        '- "penalty_multiplier": >= 1.0; default 1.0 bila tidak berdampak; '
        ">= 2.0 untuk jalan tertutup/banjir parah.\n"
        '- "reason": alasan singkat.\n'
        'Jawab HANYA satu JSON array tanpa markdown:\n'
        '[{"lat":..,"lng":..,"radius_m":..,"penalty_multiplier":..,'
        '"reason":".."}]'
    ) % (corridor, incidents_json)


async def _evaluate_penalties(coords: list, items: list[dict]) -> list[dict]:
    """Evaluasi penalti per kejadian via Gemini (fallback ke severity)."""
    result = []
    pending = []
    now = time.monotonic()
    for item in items:
        cached = _eval_cache.get(item["id"])
        if cached is not None and cached[0] > now:
            result.append(cached[1])
        else:
            pending.append(item)
    if not pending:
        return result

    evaluated = []
    if ai_agent.GEMINI_API_KEY:
        prompt = _build_eval_prompt(coords, pending)
        try:
            from google import genai

            primary = genai.Client(api_key=ai_agent.GEMINI_API_KEY)
            backup = (genai.Client(api_key=ai_agent.GEMINI_API_KEY_2)
                      if ai_agent.GEMINI_API_KEY_2 else None)

            async def _try(client) -> str:
                async with ai_agent._decide_semaphore:
                    return await asyncio.wait_for(
                        run_in_threadpool(
                            _ground_search_once, client,
                            ai_agent.GEMINI_MODEL, prompt),
                        timeout=RAG_NEWS_TIMEOUT_S)

            try:
                text = await _try(primary)
            except asyncio.TimeoutError:
                logger.warning(
                    "[RAG] Evaluasi timeout; fallback severity.")
                text = None
            except Exception as exc:
                if ai_agent._is_rate_limit(exc) and backup is not None:
                    try:
                        text = await _try(backup)
                    except Exception as exc2:
                        logger.warning(
                            "[RAG] Evaluasi (backup) gagal: %s", exc2)
                        text = None
                else:
                    logger.warning(
                        "[RAG] Evaluasi gagal; fallback severity: %s", exc)
                    text = None
            evaluated = _parse_evaluation(text) if text else []
        except Exception as exc:
            logger.warning(
                "[RAG] Evaluasi gagal; fallback severity: %s", exc)

    by_pos = {(_k(ev["lat"]), _k(ev["lng"])): ev for ev in evaluated}
    expiry = now + RAG_NEWS_CACHE_TTL_S
    for item in pending:
        key = (_k(item["lat"]), _k(item["lng"]))
        ev = by_pos.get(key)
        if ev is None:
            ev = {
                "lat": item["lat"],
                "lng": item["lng"],
                "radius_m": item["radius_m"],
                "penalty_multiplier": _severity_multiplier(item["severity"]),
                "reason": item["title"],
            }
        _eval_cache[item["id"]] = (expiry, ev)
        result.append(ev)

    if len(_eval_cache) > 256:
        stale = [k for k, (exp, _v) in _eval_cache.items() if exp <= now]
        for k in stale:
            _eval_cache.pop(k, None)
    return result


# ---------------------------------------------------------------------------
# Snap koordinat terdampak ke edge graf.
# ---------------------------------------------------------------------------
def _snap_incident_edges(graph: dict, locations: dict,
                         lat: float, lon: float, radius_m: float,
                         tolerance_m: float) -> set[int]:
    """Cari edge di sekitar titik (lat,lon) via dua segmen silang + snap."""
    half = max(50.0, radius_m) / 2.0
    coslat = math.cos(math.radians(lat)) or 0.1
    dlat = half / 111320.0
    dlon = half / (111320.0 * coslat)
    hits: set[int] = set()
    for angle in (0.0, 90.0):
        rad = math.radians(angle)
        a = (lat + dlat * math.cos(rad), lon + dlon * math.sin(rad))
        b = (lat - dlat * math.cos(rad), lon - dlon * math.sin(rad))
        hits.update(snap_segment(graph, locations, [a, b], tolerance_m))
    return hits


async def _reference_graph(app):
    pg = (getattr(app.state, "region_graph", None)
          or getattr(app.state, "path_graph", None))
    if pg is not None and getattr(pg, "graph", None):
        return pg.graph, pg.locations
    from app.services.pathfinding.graph_loader import (
        base_available,
        load_base_graph,
    )
    if not base_available():
        return None, None
    try:
        base = await asyncio.to_thread(load_base_graph)
        if getattr(base, "graph", None):
            return base.graph, base.locations
    except Exception as exc:
        logger.warning("[RAG] Base graph gagal dimuat: %s", exc)
    return None, None


async def _snap_incidents_to_edges(evaluated: list[dict],
                                   app) -> dict[int, float]:
    if not evaluated:
        return {}
    graph, locations = await _reference_graph(app)
    if graph is None or not locations:
        logger.info(
            "[RAG] Tidak ada graf untuk snap; penalti berita diabaikan.")
        return {}
    tol = _snap_tolerance()
    tolerance = tol if tol is not None else 15.0
    penalties: dict[int, float] = {}
    for ev in evaluated:
        multiplier = float(ev["penalty_multiplier"])
        if ev.get("closure") or multiplier == float("inf"):
            multiplier = float("inf")
        else:
            multiplier = min(max(multiplier, 1.0), RAG_NEWS_MAX_PENALTY)
        if multiplier <= 1.0:
            continue
        edges = _snap_incident_edges(
            graph, locations, ev["lat"], ev["lng"],
            ev["radius_m"], tolerance)
        for eid in edges:
            penalties[eid] = max(penalties.get(eid, 1.0), multiplier)
    return penalties


async def retrieve_and_evaluate_road_incidents(
        polyline_coords, *, app=None, redis=None) -> dict[int, float]:
    """Cari berita relevan di koridor rute lalu snap penaltinya ke edge graf.

    Menentukan kota secara dinamis dari koordinat rute (reverse geocode) dan
    memastikan berita kota itu tersedia via ingestion on-demand
    (fire-and-forget). Kembalikan dict {edge_id: multiplier}. Kosong bila RAG
    nonaktif / store kosong / tidak ada berita relevan / terjadi kegagalan
    (fallback aman).
    """
    if not RAG_NEWS_ENABLED or not ai_agent.GEMINI_API_KEY:
        return {}
    coords = list(polyline_coords or [])
    if not coords or len(coords) < 2:
        return {}
    try:
        city = await _detect_city_from_coords(coords, redis)
        await _ensure_city_news(redis, city)
    except Exception as exc:
        logger.warning("[RAG] Siapkan berita kota gagal: %s", exc)
    if _store.size() == 0:
        return {}
    try:
        relevant = await _retrieve_relevant(coords)
        if not relevant:
            return {}
        evaluated = await _evaluate_penalties(coords, relevant)
        return await _snap_incidents_to_edges(evaluated, app)
    except Exception as exc:
        logger.warning("[RAG] Evaluasi penalti berita gagal: %s", exc)
        return {}


def rag_enabled() -> bool:
    return RAG_NEWS_ENABLED and bool(ai_agent.GEMINI_API_KEY)
