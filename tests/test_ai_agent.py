import asyncio
import time

import pytest

from app.services import ai_agent
from app.services.ai_agent import (
    ToolContext,
    build_reroute_context,
    decide_reroute,
)
from app.services.navigation import NavSession

ROUTE = [(-6.8048, 110.8385), (-6.8052, 110.8390), (-6.8100, 110.8500)]


def _session():
    session = NavSession(kurir_id=7)
    session.coords = ROUTE
    session.dest = ROUTE[-1]
    session.mode = "motorcycle"
    session.last_position = {"lat": ROUTE[0][0], "lon": ROUTE[0][1],
                             "speed": 10}
    return session


async def _run_decision(monkeypatch, fake_agent_loop):
    monkeypatch.setattr(ai_agent, "AI_REROUTE_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ai_agent, "_agent_loop", fake_agent_loop)
    session = _session()
    context = build_reroute_context(
        session, ROUTE[0][0], ROUTE[0][1], hint="traffic")
    return await decide_reroute(
        context, "traffic", app=object(), redis=object(), session=session)


def test_parse_decision_valid_apply():
    decision = ai_agent._parse_decision('{"action": "apply", "reason": "x"}')
    assert decision["action"] == "apply"
    assert decision["reason"] == "x"


def test_parse_decision_strips_code_fence():
    decision = ai_agent._parse_decision(
        '```json\n{"action":"ignore","reason":"noise"}\n```')
    assert decision["action"] == "ignore"


def test_parse_decision_invalid_returns_none():
    assert ai_agent._parse_decision("tidak ada json") is None


def test_parse_decision_normalizes_unknown_action():
    decision = ai_agent._parse_decision('{"action": "explode", "reason": "?"}')
    assert decision["action"] == "defer"


def test_decide_reroute_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(ai_agent, "AI_REROUTE_ENABLED", False)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    session = _session()
    result = asyncio.run(decide_reroute(
        {}, "traffic", app=object(), redis=object(), session=session))
    assert result is None


def test_decide_reroute_no_api_key_returns_none(monkeypatch):
    monkeypatch.setattr(ai_agent, "AI_REROUTE_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "")
    session = _session()
    result = asyncio.run(decide_reroute(
        {}, "traffic", app=object(), redis=object(), session=session))
    assert result is None


def test_decide_reroute_apply(monkeypatch):
    async def fake_agent_loop(ctx, context, hint):
        return {"action": "apply", "reason": "macet parah"}

    decision = asyncio.run(_run_decision(monkeypatch, fake_agent_loop))
    assert decision is not None
    assert decision["action"] == "apply"
    assert decision["reason"] == "macet parah"


def test_decide_reroute_ignore(monkeypatch):
    async def fake_agent_loop(ctx, context, hint):
        return {"action": "ignore", "reason": "noise GPS"}

    decision = asyncio.run(_run_decision(monkeypatch, fake_agent_loop))
    assert decision is not None
    assert decision["action"] == "ignore"


def test_decide_reroute_error_falls_back_to_none(monkeypatch):
    async def fake_agent_loop(ctx, context, hint):
        raise RuntimeError("Gemini API down")

    decision = asyncio.run(_run_decision(monkeypatch, fake_agent_loop))
    assert decision is None


def test_decide_reroute_timeout_falls_back_to_none(monkeypatch):
    async def fake_agent_loop(ctx, context, hint):
        await asyncio.sleep(10.0)
        return {"action": "apply", "reason": "terlambat"}

    monkeypatch.setattr(ai_agent, "GEMINI_REROUTE_TIMEOUT_S", 0.05)
    decision = asyncio.run(_run_decision(monkeypatch, fake_agent_loop))
    assert decision is None


def test_decide_reroute_no_session_returns_none(monkeypatch):
    monkeypatch.setattr(ai_agent, "AI_REROUTE_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    result = asyncio.run(decide_reroute(
        {}, "traffic", app=object(), redis=object(), session=None))
    assert result is None


def test_dispatch_unknown_tool_returns_error():
    ctx = ToolContext(app=object(), redis=object(), session=_session())

    async def run():
        return await ai_agent._dispatch_tool(ctx, "unknown_tool", {})

    result = asyncio.run(run())
    assert result.get("error")


def test_apply_reroute_tool_returns_commit():
    ctx = ToolContext(app=object(), redis=object(), session=_session())

    async def run():
        return await ai_agent._tool_apply_reroute(ctx, {"reason": "yakin"})

    result = asyncio.run(run())
    assert result["ok"] is True
    assert result["action"] == "apply"


def test_tool_get_remaining_progress():
    ctx = ToolContext(app=object(), redis=object(), session=_session())

    async def run():
        return await ai_agent._tool_get_remaining_progress(
            ctx, {"lat": ROUTE[0][0], "lng": ROUTE[0][1]})

    result = asyncio.run(run())
    assert result["remaining_distance_m"] > 0
    assert 0 <= result["progress_pct"] <= 100


def test_build_reroute_context_serializable():
    session = _session()
    context = build_reroute_context(
        session, ROUTE[0][0], ROUTE[0][1], hint="traffic",
        extra={"candidate": {"eta_s": 120}})
    import json
    json.dumps(context)  # harus aman di-serialize (tanpa objek runtime)
    assert context["hint"] == "traffic"
    assert context["remaining"] is not None
    assert context["candidate"]["eta_s"] == 120


class _RateLimitError(Exception):
    def __init__(self):
        super().__init__("429 RESOURCE_EXHAUSTED")
        self.code = 429


class _OtherError(Exception):
    def __init__(self):
        super().__init__("500 internal")
        self.code = 500


class _FakeModels:
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def generate_content(self, *args, **kwargs):
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeClient:
    def __init__(self, results):
        self.models = _FakeModels(results)


def test_is_rate_limit_detects_429():
    e = _RateLimitError()
    assert ai_agent._is_rate_limit(e)
    e2 = _OtherError()
    e2.code = 42903
    assert ai_agent._is_rate_limit(e2)
    assert not ai_agent._is_rate_limit(_OtherError())


def test_is_rate_limit_detects_via_message():
    assert ai_agent._is_rate_limit(RuntimeError("quota RESOURCE_EXHAUSTED"))
    assert ai_agent._is_rate_limit(RuntimeError("http 429 too many requests"))
    assert not ai_agent._is_rate_limit(RuntimeError("random failure"))


def test_call_model_uses_backup_on_rate_limit():
    primary = _FakeClient([_RateLimitError()])
    backup = _FakeClient(["ok-backup"])

    async def run():
        return await ai_agent._call_model(primary, backup, "m", [], None)

    result = asyncio.run(run())
    assert result == "ok-backup"
    assert primary.models.calls == 1
    assert backup.models.calls == 1


def test_call_model_propagates_non_rate_limit_without_backup():
    primary = _FakeClient([_OtherError()])
    backup = _FakeClient(["x"])

    async def run():
        with pytest.raises(_OtherError):
            await ai_agent._call_model(primary, backup, "m", [], None)

    asyncio.run(run())
    assert backup.models.calls == 0


def test_call_model_primary_success_skips_backup():
    primary = _FakeClient(["ok"])
    backup = _FakeClient(["x"])

    async def run():
        return await ai_agent._call_model(primary, backup, "m", [], None)

    result = asyncio.run(run())
    assert result == "ok"
    assert backup.models.calls == 0


def test_decide_reroute_uses_backup_key_on_rate_limit(monkeypatch):
    from google.genai import types

    monkeypatch.setattr(ai_agent, "AI_REROUTE_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "primary")
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY_2", "backup")

    def fake_response(text):
        content = types.Content(role="model",
                                parts=[types.Part(text=text)])
        cand = type("C", (), {"content": content})()
        return type("R", (), {"candidates": [cand]})()

    primary = _FakeClient([_RateLimitError()])
    backup = _FakeClient([
        fake_response('{"action": "apply", "reason": "via backup key"}')])
    monkeypatch.setattr(ai_agent, "_make_clients",
                        lambda: (primary, backup))

    session = _session()
    context = build_reroute_context(
        session, ROUTE[0][0], ROUTE[0][1], hint="traffic")
    decision = asyncio.run(decide_reroute(
        context, "traffic", app=object(), redis=object(), session=session))

    assert decision is not None
    assert decision["action"] == "apply"
    assert decision["reason"] == "via backup key"
    assert primary.models.calls == 1
    assert backup.models.calls == 1


def _enable_ai(monkeypatch):
    monkeypatch.setattr(ai_agent, "AI_REROUTE_ENABLED", True)
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "primary")
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY_2", "backup")


def _traffic_context():
    session = _session()
    return build_reroute_context(
        session, ROUTE[0][0], ROUTE[0][1], hint="traffic"), session


def test_semaphore_released_after_call(monkeypatch):
    from google.genai import types

    _enable_ai(monkeypatch)

    def fake_response(text):
        content = types.Content(role="model",
                                parts=[types.Part(text=text)])
        cand = type("C", (), {"content": content})()
        return type("R", (), {"candidates": [cand]})()

    payload = fake_response('{"action": "apply", "reason": "ok"}')
    monkeypatch.setattr(
        ai_agent, "_make_clients",
        lambda: (_FakeClient([payload]), _FakeClient([payload])))

    context, session = _traffic_context()
    before = ai_agent._decide_semaphore._value
    decision = asyncio.run(decide_reroute(
        context, "traffic", app=object(), redis=object(), session=session))
    assert decision is not None
    assert ai_agent._decide_semaphore._value == before


def test_breaker_opens_after_repeated_failures(monkeypatch):
    _enable_ai(monkeypatch)
    monkeypatch.setattr(ai_agent, "_BREAKER_FAILURE_THRESHOLD", 2)
    ai_agent._reset_breaker()
    failing = lambda: (_FakeClient([_RateLimitError()]),
                       _FakeClient([_RateLimitError()]))
    monkeypatch.setattr(ai_agent, "_make_clients", failing)

    context, session = _traffic_context()

    async def run():
        return await decide_reroute(
            context, "traffic", app=object(), redis=object(), session=session)

    assert asyncio.run(run()) is None
    assert asyncio.run(run()) is None
    assert ai_agent._breaker_open()

    called = []

    async def boom(ctx, context_, hint):
        called.append(hint)
        return None

    monkeypatch.setattr(ai_agent, "_agent_loop", boom)
    assert asyncio.run(run()) is None
    assert called == []
    ai_agent._reset_breaker()


def test_breaker_success_resets_failures(monkeypatch):
    monkeypatch.setattr(ai_agent, "_BREAKER_FAILURE_THRESHOLD", 3)
    ai_agent._reset_breaker()
    ai_agent._breaker_record_failure()
    ai_agent._breaker_record_failure()
    assert ai_agent._breaker_failures == 2
    assert not ai_agent._breaker_open()
    ai_agent._breaker_record_success()
    assert ai_agent._breaker_failures == 0
    ai_agent._reset_breaker()


def test_breaker_closes_after_reset_window(monkeypatch):
    monkeypatch.setattr(ai_agent, "_BREAKER_FAILURE_THRESHOLD", 2)
    ai_agent._reset_breaker()
    ai_agent._breaker_record_failure()
    ai_agent._breaker_record_failure()
    assert ai_agent._breaker_open()
    ai_agent._breaker_open_until = time.monotonic() - 1.0
    assert not ai_agent._breaker_open()
    assert ai_agent._breaker_failures == 0
    ai_agent._reset_breaker()


def test_sanitize_reason_strips_control_and_truncates():
    out = ai_agent._sanitize_reason("x" * 600)
    assert len(out) == ai_agent._MAX_REASON_LEN
    out2 = ai_agent._sanitize_reason("good\nline\0hidden\x07")
    assert "\x00" not in out2
    assert "\x07" not in out2
    assert "good\nline" in out2
    assert ai_agent._sanitize_reason(123) == "123"


def test_parse_decision_sanitizes_reason():
    decision = ai_agent._parse_decision(
        '{"action": "apply", "reason": "ok\\u0000bad\\u0007end\\nnext"}')
    assert decision["reason"] == "okbadend\nnext"


def test_navigation_status_includes_ai_block():
    from app.api.v1.endpoints.navigation import navigation_status

    class _FakeState:
        redis = object()

    class _FakeRequest:
        app = type("App", (), {"state": _FakeState()})()

    status = asyncio.run(navigation_status(_FakeRequest()))
    ai = status["ai"]
    assert "enabled" in ai
    assert "backup_key_available" in ai
    assert "circuit_breaker_open" in ai
    assert "concurrent_slots" in ai
    assert "timeout_s" in ai
