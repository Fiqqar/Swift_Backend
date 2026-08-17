import asyncio

import pytest

from app.services import ai_agent
from app.services import rag_traffic
from app.services.pathfinding.core_a_star import edge_id

ROUTE = [(-6.8048, 110.8385), (-6.8052, 110.8390), (-6.8100, 110.8500)]


@pytest.fixture(autouse=True)
def _reset_rag():
    rag_traffic.reset_for_test()
    yield
    rag_traffic.reset_for_test()


def _populate(items, vectors=None):
    if vectors is None:
        vectors = [[1.0, 0.0]] * len(items)
    asyncio.run(rag_traffic._store.replace(items, vectors))


# ---------------------------------------------------------------------------
# Helper murni
# ---------------------------------------------------------------------------
def test_cosine():
    assert rag_traffic._cosine([1, 0], [1, 0]) == 1.0
    assert abs(rag_traffic._cosine([1, 0], [0, 1])) < 1e-9
    assert rag_traffic._cosine([], [1, 0]) == 0.0


def test_severity_multiplier():
    assert rag_traffic._severity_multiplier("LOW") == 1.2
    assert rag_traffic._severity_multiplier("MEDIUM") == 1.8
    assert rag_traffic._severity_multiplier("HIGH") == 2.5
    assert rag_traffic._severity_multiplier("BLOCKING") == float("inf")
    assert rag_traffic._severity_multiplier("unknown") == 1.8


def test_parse_news_items_valid():
    text = (
        '[{"title":"Banjir","summary":"genangan","lat":-6.8,"lng":110.84,'
        '"radius_m":300,"severity":"HIGH"},'
        '{"title":"B","lat":-7.0,"lng":110.0,"radius_m":10,"severity":"X"}]'
    )
    items = rag_traffic._parse_news_items(text)
    assert len(items) == 2
    assert items[0]["severity"] == "HIGH"
    assert items[0]["radius_m"] == 300
    assert items[1]["severity"] == "MEDIUM"
    assert items[1]["radius_m"] == 50


def test_parse_news_items_code_fence_and_empty():
    assert rag_traffic._parse_news_items('```json\n[]\n```') == []
    assert rag_traffic._parse_news_items("tidak ada berita") == []


def test_parse_news_items_skips_missing_coords():
    assert rag_traffic._parse_news_items(
        '[{"title":"NoCoords","summary":"x"}]') == []


def test_parse_evaluation_valid():
    text = (
        '[{"lat":-6.8,"lng":110.84,"radius_m":250,'
        '"penalty_multiplier":2.0,"reason":"banjir"}]'
    )
    ev = rag_traffic._parse_evaluation(text)
    assert ev[0]["penalty_multiplier"] == 2.0
    assert ev[0]["radius_m"] == 250
    assert ev[0]["reason"] == "banjir"


def test_parse_evaluation_invalid():
    assert rag_traffic._parse_evaluation("tidak ada") == []
    assert rag_traffic._parse_evaluation('[{"lat":"x","lng":1}]') == []


# ---------------------------------------------------------------------------
# In-memory vector store
# ---------------------------------------------------------------------------
def test_store_replace_and_search():
    store = rag_traffic._NewsStore()
    asyncio.run(store.replace([{"id": "a"}, {"id": "b"}],
                              [[1, 0], [0, 1]]))
    hits = asyncio.run(store.search([1, 0], 2))
    assert [h["id"] for h in hits] == ["a", "b"]
    assert store.size() == 2
    assert store.last_updated() > 0
    store.clear()
    assert store.size() == 0


def test_store_search_empty():
    store = rag_traffic._NewsStore()
    assert asyncio.run(store.search([1, 0], 5)) == []


# ---------------------------------------------------------------------------
# Gate & guard
# ---------------------------------------------------------------------------
def test_rag_enabled_requires_flag_and_key(monkeypatch):
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", False)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    assert rag_traffic.rag_enabled() is False
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "")
    assert rag_traffic.rag_enabled() is False
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    assert rag_traffic.rag_enabled() is True


def test_ingestion_worker_disabled_returns(monkeypatch):
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", False)
    result = asyncio.run(rag_traffic.rag_ingestion_worker(object()))
    assert result is None


def test_ingestion_worker_no_city_returns(monkeypatch):
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", True)
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_CITY", "")
    result = asyncio.run(rag_traffic.rag_ingestion_worker(object()))
    assert result is None


# ---------------------------------------------------------------------------
# Ingestion (mocked grounding + embedding)
# ---------------------------------------------------------------------------
def test_ingest_road_news_grounding(monkeypatch):
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY_2", "")
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_CITY", "Kudus")

    def fake_ground(client, model, prompt):
        return (
            '[{"title":"Banjir Kudus","summary":"genangan","lat":-6.8,'
            '"lng":110.84,"radius_m":300,"severity":"HIGH"}]'
        )

    async def fake_embed(texts, timeout_s=None):
        return [[1.0, 0.0]] * len(texts)

    monkeypatch.setattr(rag_traffic, "_ground_search_once", fake_ground)
    monkeypatch.setattr(rag_traffic, "_embed_texts", fake_embed)

    count = asyncio.run(rag_traffic.ingest_road_news(redis=None))
    assert count == 1
    assert rag_traffic._store.size() == 1
    assert rag_traffic._store.last_updated() > 0


def test_ingest_road_news_no_city_returns_zero(monkeypatch):
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_CITY", "")
    assert asyncio.run(rag_traffic.ingest_road_news()) == 0


def test_ingest_road_news_no_key_returns_zero(monkeypatch):
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "")
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_CITY", "Kudus")
    assert asyncio.run(rag_traffic.ingest_road_news()) == 0


# ---------------------------------------------------------------------------
# Retrieval (proximity filter)
# ---------------------------------------------------------------------------
def test_retrieve_relevant_filters_by_distance(monkeypatch):
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    near = {"id": "near", "title": "Macet", "summary": "s",
            "lat": ROUTE[1][0], "lng": ROUTE[1][1],
            "radius_m": 200, "severity": "HIGH"}
    far = {"id": "far", "title": "Jauh", "summary": "s",
           "lat": -6.7900, "lng": 110.8200,
           "radius_m": 100, "severity": "LOW"}
    _populate([near, far])

    async def fake_embed(texts, timeout_s=None):
        return [[1.0, 0.0]]

    monkeypatch.setattr(rag_traffic, "_embed_texts", fake_embed)

    result = asyncio.run(rag_traffic._retrieve_relevant(ROUTE))
    ids = [item["id"] for item in result]
    assert "near" in ids
    assert "far" not in ids


# ---------------------------------------------------------------------------
# Evaluasi penalti
# ---------------------------------------------------------------------------
def test_evaluate_penalties_severity_fallback_without_key(monkeypatch):
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "")
    items = [{"id": "n1", "title": "Banjir", "summary": "s",
              "lat": -6.8, "lng": 110.84, "radius_m": 300,
              "severity": "HIGH"}]
    result = asyncio.run(rag_traffic._evaluate_penalties(ROUTE, items))
    assert result[0]["penalty_multiplier"] == 2.5
    assert result[0]["reason"] == "Banjir"


def test_evaluate_penalties_uses_gemini(monkeypatch):
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY_2", "")
    monkeypatch.setattr(ai_agent, "GEMINI_MODEL", "gemini-2.5-flash")

    def fake_ground(client, model, prompt):
        return (
            '[{"lat":-6.8,"lng":110.84,"radius_m":250,'
            '"penalty_multiplier":2.0,"reason":"banjir"}]'
        )

    monkeypatch.setattr(rag_traffic, "_ground_search_once", fake_ground)

    items = [{"id": "n1", "title": "Banjir", "summary": "s",
              "lat": -6.8, "lng": 110.84, "radius_m": 300,
              "severity": "HIGH"}]
    result = asyncio.run(rag_traffic._evaluate_penalties(ROUTE, items))
    assert result[0]["penalty_multiplier"] == 2.0
    assert result[0]["radius_m"] == 250
    assert result[0]["reason"] == "banjir"


def test_evaluate_penalties_caches_per_item(monkeypatch):
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "")
    calls = []

    original = rag_traffic._severity_multiplier

    def counting_multiplier(severity):
        calls.append(severity)
        return original(severity)

    monkeypatch.setattr(rag_traffic, "_severity_multiplier",
                        counting_multiplier)
    items = [{"id": "n1", "title": "Banjir", "summary": "s",
              "lat": -6.8, "lng": 110.84, "radius_m": 300,
              "severity": "HIGH"}]
    asyncio.run(rag_traffic._evaluate_penalties(ROUTE, items))
    asyncio.run(rag_traffic._evaluate_penalties(ROUTE, items))
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# End-to-end: retrieve -> evaluate -> snap ke edge graf
# ---------------------------------------------------------------------------
def test_retrieve_and_evaluate_snaps_to_edges(monkeypatch):
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY_2", "")

    item = {"id": "n1", "title": "Banjir", "summary": "s",
            "lat": ROUTE[1][0], "lng": ROUTE[1][1],
            "radius_m": 300, "severity": "HIGH"}
    _populate([item])

    async def fake_embed(texts, timeout_s=None):
        return [[1.0, 0.0]]

    async def fake_eval(coords, items):
        return [{"lat": ROUTE[1][0], "lng": ROUTE[1][1],
                 "radius_m": 300, "penalty_multiplier": 2.5,
                 "reason": "banjir"}]

    graph = {0: [1], 1: [0]}
    locations = {0: ROUTE[0], 1: ROUTE[1]}

    async def fake_reference(app):
        return graph, locations

    monkeypatch.setattr(rag_traffic, "_embed_texts", fake_embed)
    monkeypatch.setattr(rag_traffic, "_evaluate_penalties", fake_eval)
    monkeypatch.setattr(rag_traffic, "_reference_graph", fake_reference)

    result = asyncio.run(rag_traffic.retrieve_and_evaluate_road_incidents(
        ROUTE, app=object()))
    assert edge_id(0, 1) in result
    assert edge_id(1, 0) in result
    assert result[edge_id(0, 1)] == 2.5
    assert result[edge_id(1, 0)] == 2.5


def test_retrieve_and_evaluate_skips_closure(monkeypatch):
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")

    item = {"id": "n1", "title": "Jalan Tertutup", "summary": "s",
            "lat": ROUTE[1][0], "lng": ROUTE[1][1],
            "radius_m": 300, "severity": "BLOCKING"}
    _populate([item])

    async def fake_embed(texts, timeout_s=None):
        return [[1.0, 0.0]]

    async def fake_eval(coords, items):
        return [{"lat": ROUTE[1][0], "lng": ROUTE[1][1],
                 "radius_m": 300,
                 "penalty_multiplier": float("inf"), "reason": "tutup"}]

    graph = {0: [1], 1: [0]}
    locations = {0: ROUTE[0], 1: ROUTE[1]}

    async def fake_reference(app):
        return graph, locations

    monkeypatch.setattr(rag_traffic, "_embed_texts", fake_embed)
    monkeypatch.setattr(rag_traffic, "_evaluate_penalties", fake_eval)
    monkeypatch.setattr(rag_traffic, "_reference_graph", fake_reference)

    result = asyncio.run(rag_traffic.retrieve_and_evaluate_road_incidents(
        ROUTE, app=object()))
    assert edge_id(0, 1) in result
    assert result[edge_id(0, 1)] == float("inf")


# ---------------------------------------------------------------------------
# Guard end-to-end
# ---------------------------------------------------------------------------
def test_retrieve_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", False)
    assert asyncio.run(
        rag_traffic.retrieve_and_evaluate_road_incidents(ROUTE)) == {}


def test_retrieve_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "")
    assert asyncio.run(
        rag_traffic.retrieve_and_evaluate_road_incidents(ROUTE)) == {}


def test_retrieve_empty_store_returns_empty(monkeypatch):
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    assert asyncio.run(
        rag_traffic.retrieve_and_evaluate_road_incidents(ROUTE)) == {}


def test_retrieve_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(rag_traffic, "RAG_NEWS_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    _populate([{"id": "n1", "title": "X", "summary": "s",
                "lat": ROUTE[1][0], "lng": ROUTE[1][1],
                "radius_m": 300, "severity": "HIGH"}])

    async def boom(texts, timeout_s=None):
        raise RuntimeError("embedding down")

    monkeypatch.setattr(rag_traffic, "_embed_texts", boom)
    assert asyncio.run(
        rag_traffic.retrieve_and_evaluate_road_incidents(ROUTE)) == {}
