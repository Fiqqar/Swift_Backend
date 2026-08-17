import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.driver_report import DriverReport
from app.schemas.driver_report import DriverReportCreate
from app.services import ai_agent
from app.services import internal_report_agent
from app.services import rag_traffic
from app.services.navigation import NavSession
from app.services.pathfinding.core_a_star import edge_id

ROUTE = [(-6.8048, 110.8385), (-6.8052, 110.8390), (-6.8100, 110.8500)]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(internal_report_agent, "INTERNAL_REPORT_AGENT_ENABLED",
                        False)
    monkeypatch.setattr(internal_report_agent, "REPORT_SEVERITY_MIN", "HIGH")
    monkeypatch.setattr(internal_report_agent, "REPORT_IMPACT_RADIUS_M", 500.0)
    yield


class _FakeReport:
    def __init__(self, rid, text, lat=None, lng=None):
        self.id = rid
        self.text = text
        self.latitude = lat
        self.longitude = lng
        self.status = "pending"
        self.severity = None
        self.radius_m = None
        self.processed_at = None


class _FakePart:
    def __init__(self, text):
        self.text = text


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, text):
        self.content = _FakeContent([_FakePart(text)])


class _FakeResponse:
    def __init__(self, text):
        self.candidates = [_FakeCandidate(text)]


class _FakeRouteResponse:
    route_coordinates = list(ROUTE)
    estimated_time_seconds = 600.0


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class _FakeRegistry:
    def __init__(self, sessions):
        self._sessions = sessions

    async def all(self):
        return list(self._sessions)


class _FakeApp:
    state = SimpleNamespace()


# ---------------------------------------------------------------------------
# Parser klasifikasi
# ---------------------------------------------------------------------------
def test_parse_classification_json():
    out = internal_report_agent._parse_classification(
        '{"severity":"HIGH","lat":-6.8,"lng":110.8,"radius_m":800,'
        '"reason":"Jalan tertutup"}')
    assert out["severity"] == "HIGH"
    assert out["lat"] == -6.8
    assert out["lng"] == 110.8
    assert out["radius_m"] == 800.0
    assert out["reason"]


def test_parse_classification_markdown_fence():
    out = internal_report_agent._parse_classification(
        '```json\n{"severity":"BLOCKING","lat":-6.8,"lng":110.8,'
        '"radius_m":5000,"reason":"Banjir"}\n```')
    assert out["severity"] == "BLOCKING"
    assert out["radius_m"] == 5000.0


def test_parse_classification_with_noise():
    out = internal_report_agent._parse_classification(
        'ok berikut jawabannya: {"severity":"MEDIUM","radius_m":150,'
        '"reason":"Macet"}')
    assert out["severity"] == "MEDIUM"
    assert out["radius_m"] == 150.0
    assert "lat" not in out


def test_parse_classification_garbage():
    assert internal_report_agent._parse_classification("nope") is None
    assert internal_report_agent._parse_classification("") is None
    assert internal_report_agent._parse_classification("[1,2,3]") is None


def test_parse_classification_sanitizes_severity_and_radius():
    out = internal_report_agent._parse_classification(
        '{"severity":"URGENT","radius_m":9999999,"reason":"x"}')
    assert out["severity"] == "MEDIUM"
    assert out["radius_m"] == 5000.0


# ---------------------------------------------------------------------------
# Gate severity
# ---------------------------------------------------------------------------
def test_meets_min_severity():
    assert internal_report_agent._meets_min_severity("HIGH")
    assert internal_report_agent._meets_min_severity("BLOCKING")
    assert not internal_report_agent._meets_min_severity("MEDIUM")
    assert not internal_report_agent._meets_min_severity("LOW")
    assert not internal_report_agent._meets_min_severity("unknown")


def test_severity_index():
    assert internal_report_agent._severity_index("LOW") == 1
    assert internal_report_agent._severity_index("HIGH") == 3
    assert internal_report_agent._severity_index("BLOCKING") == 4
    assert internal_report_agent._severity_index("") == 0


# ---------------------------------------------------------------------------
# _classify_report (Gemini di-mock)
# ---------------------------------------------------------------------------
def test_classify_report_ok(monkeypatch):
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ai_agent, "_make_clients", lambda: (object(), None))

    async def _fake_call_model(primary, backup, model, contents, config):
        assert model == ai_agent.GEMINI_MODEL
        assert config.temperature == 0.2
        return _FakeResponse(
            '{"severity":"HIGH","lat":-6.8,"lng":110.8,"radius_m":300,'
            '"reason":"Pohon tumbang"}')

    monkeypatch.setattr(ai_agent, "_call_model", _fake_call_model)
    report = _FakeReport(1, "Pohon tumbang di Jl. Sunan Muria")

    async def _run():
        return await internal_report_agent._classify_report(report)

    out = asyncio.run(_run())
    assert out is not None
    assert out["severity"] == "HIGH"
    assert out["lat"] == -6.8


def test_classify_report_skips_without_key(monkeypatch):
    monkeypatch.setattr(ai_agent, "GEMINI_API_KEY", "")
    report = _FakeReport(1, "Apa saja")

    async def _run():
        return await internal_report_agent._classify_report(report)

    assert asyncio.run(_run()) is None


# ---------------------------------------------------------------------------
# _handle_report
# ---------------------------------------------------------------------------
async def _run_handle(report, session=None, snap_penalties=None,
                      classify=None, monkeypatch=None, resolve=None,
                      reroute=None):
    registry = _FakeRegistry([session]) if session else _FakeRegistry([])
    app = _FakeApp()
    app.state.nav_registry = registry
    if classify is not None:
        async def _classify(report, redis=None):
            return classify
        monkeypatch.setattr(internal_report_agent, "_classify_report",
                            _classify)
    if resolve is not None:
        async def _resolve(report, classification, redis=None):
            return resolve
        monkeypatch.setattr(internal_report_agent, "_resolve_report_coords",
                            _resolve)
    if snap_penalties is not None:
        async def _snap(evaluated, app):
            return snap_penalties
        monkeypatch.setattr(rag_traffic, "_snap_incidents_to_edges", _snap)
    if reroute is not None:
        monkeypatch.setattr(internal_report_agent, "compute_reroute", reroute)
    return await internal_report_agent._handle_report(report, app)


def test_handle_report_high_matched(monkeypatch):
    ws = _FakeWS()
    session = NavSession(kurir_id=7, ws=ws)
    session.route_id = 11
    session.coords = list(ROUTE)
    session.dest = ROUTE[-1]
    session.last_position = {"lat": ROUTE[0][0], "lon": ROUTE[0][1]}

    async def _reroute(app, redis, session, lat, lon,
                       traffic=True, extra_penalties=None):
        assert extra_penalties == {edge_id(1, 2): 2.5}
        return _FakeRouteResponse()

    report = _FakeReport(5, "Pohon tumbang", lat=ROUTE[1][0], lng=ROUTE[1][1])
    out = asyncio.run(_run_handle(
        report,
        session=session,
        snap_penalties={edge_id(1, 2): 2.5},
        classify={"severity": "HIGH", "lat": ROUTE[1][0], "lng": ROUTE[1][1],
                  "radius_m": 300, "reason": "x"},
        monkeypatch=monkeypatch,
        reroute=_reroute))

    assert out["status"] == "processed"
    assert out["affected"] == 1
    assert report.status == "processed"
    assert report.severity == "HIGH"
    assert report.processed_at is not None
    assert len(ws.sent) == 1
    event = ws.sent[0]
    assert event["type"] in ("auto_rerouted", "reroute_available")
    assert event["source"] == "driver_report"
    assert event["route_id"] == 11
    assert event["reason"].startswith("Laporan kurir #5:")
    assert session.coords == list(ROUTE)


def test_handle_report_low_severity_ignored(monkeypatch):
    called = {}

    async def _reroute(app, redis, session, lat, lon,
                       traffic=True, extra_penalties=None):
        called["n"] = True
        return _FakeRouteResponse()

    report = _FakeReport(6, "Macet ringan", lat=ROUTE[0][0], lng=ROUTE[0][1])
    session = NavSession(kurir_id=7)
    session.coords = list(ROUTE)
    session.dest = ROUTE[-1]
    session.last_position = {"lat": ROUTE[0][0], "lon": ROUTE[0][1]}

    out = asyncio.run(_run_handle(
        report,
        session=session,
        classify={"severity": "MEDIUM", "radius_m": 200, "reason": "x"},
        monkeypatch=monkeypatch,
        reroute=_reroute))

    assert out["status"] == "ignored"
    assert report.status == "ignored"
    assert report.severity == "MEDIUM"
    assert "n" not in called


def test_handle_report_no_coords_ignored(monkeypatch):
    called = {}

    async def _reroute(app, redis, session, lat, lon,
                       traffic=True, extra_penalties=None):
        called["n"] = True
        return _FakeRouteResponse()

    report = _FakeReport(7, "Kebakaran di pasar")
    out = asyncio.run(_run_handle(
        report,
        classify={"severity": "HIGH", "radius_m": 500, "reason": "x"},
        resolve=None,
        monkeypatch=monkeypatch,
        reroute=_reroute))

    assert out["status"] == "ignored"
    assert out.get("reason") == "no_coords"
    assert "n" not in called


def test_handle_report_no_snap_penalties(monkeypatch):
    async def _reroute(app, redis, session, lat, lon,
                       traffic=True, extra_penalties=None):
        raise AssertionError("reroute tidak boleh dipanggil")

    report = _FakeReport(8, "Genangan kecil", lat=ROUTE[1][0],
                         lng=ROUTE[1][1])
    session = NavSession(kurir_id=9)
    session.coords = list(ROUTE)
    session.dest = ROUTE[-1]
    session.last_position = {"lat": ROUTE[0][0], "lon": ROUTE[0][1]}

    out = asyncio.run(_run_handle(
        report,
        session=session,
        snap_penalties={},
        classify={"severity": "HIGH", "lat": ROUTE[1][0], "lng": ROUTE[1][1],
                  "radius_m": 300, "reason": "x"},
        monkeypatch=monkeypatch,
        reroute=_reroute))

    assert out["status"] == "processed"
    assert out["affected"] == 0


def test_handle_report_no_matching_session(monkeypatch):
    async def _reroute(app, redis, session, lat, lon,
                       traffic=True, extra_penalties=None):
        raise AssertionError("reroute tidak boleh dipanggil")

    report = _FakeReport(9, "Jalan amblas", lat=ROUTE[0][0], lng=ROUTE[0][1])
    out = asyncio.run(_run_handle(
        report,
        classify={"severity": "HIGH", "lat": ROUTE[0][0], "lng": ROUTE[0][1],
                  "radius_m": 300, "reason": "x"},
        snap_penalties={edge_id(1, 2): 2.5},
        monkeypatch=monkeypatch,
        reroute=_reroute))

    assert out["status"] == "processed"
    assert out["affected"] == 0


# ---------------------------------------------------------------------------
# process_pending_reports & loop
# ---------------------------------------------------------------------------
class _FakeSession:
    def __init__(self, reports):
        self._reports = reports
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(
            all=lambda: list(self._reports)))

    async def commit(self):
        self.commits += 1


class _FakeSessionMaker:
    def __init__(self, reports):
        self._reports = reports

    def __call__(self):
        return _FakeSession(self._reports)


def test_process_pending_reports(monkeypatch):
    reports = [_FakeReport(i, "Laporan %d" % i) for i in (1, 2, 3)]
    maker = _FakeSessionMaker(reports)
    app = _FakeApp()
    app.state.sessionmaker = maker
    app.state.nav_registry = _FakeRegistry([])
    seen = []

    async def _handle(report, app, redis=None, registry=None):
        seen.append(report.id)
        report.status = "processed"
        return {"id": report.id, "status": "processed"}

    monkeypatch.setattr(internal_report_agent, "_handle_report", _handle)

    async def _run():
        return await internal_report_agent.process_pending_reports(app)

    n = asyncio.run(_run())
    assert n == 3
    assert seen == [1, 2, 3]
    assert all(r.status == "processed" for r in reports)


def test_agent_loop_disabled_returns():
    async def _run():
        return await internal_report_agent.internal_report_agent(_FakeApp())

    assert asyncio.run(_run()) is None


# ---------------------------------------------------------------------------
# Endpoint helper (_token_kurir_id)
# ---------------------------------------------------------------------------
def test_token_kurir_id():
    from app.api.v1.endpoints.reports import _token_kurir_id
    from app.core.security import create_access_token

    token = create_access_token(123, "kurir")
    assert _token_kurir_id(
        SimpleNamespace(headers={"Authorization": f"Bearer {token}"})) == 123
    assert _token_kurir_id(SimpleNamespace(headers={})) is None
    assert _token_kurir_id(SimpleNamespace(
        headers={"Authorization": "Bearer bogus"})) is None
    assert _token_kurir_id(SimpleNamespace(
        headers={"Authorization": "Basic abc"})) is None


# ---------------------------------------------------------------------------
# Model & schema
# ---------------------------------------------------------------------------
def test_driver_report_model_defaults():
    r = DriverReport(kurir_id=1, text="Banjir")
    assert r.kurir_id == 1
    assert r.text == "Banjir"
    assert r.latitude is None
    assert r.severity is None
    assert DriverReport.__table__.c.status.default.arg == "pending"


def test_driver_report_create_validation():
    payload = DriverReportCreate(
        text="Jalan tertutup", latitude=-6.8, longitude=110.8)
    assert payload.latitude == -6.8
    with pytest.raises(ValidationError):
        DriverReportCreate(text="")