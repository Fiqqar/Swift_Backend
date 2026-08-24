"""AI Agent (Google Gemini) untuk keputusan real-time rerouting.

Agent bertindak sebagai *decision-maker* (orkestrator) di atas pipeline
pathfinding eksisting (A*/Rust/TomTom) yang TIDAK diubah. Ia memutuskan apakah
sebuah reroute (traffic / off-route) layak dilakukan dengan Function Calling.

Kontrak:
- `decide_reroute(context, decision_hint, *, app, redis, session)`
  mengembalikan dict keputusan:
    {"action": "apply"|"ignore"|"defer", "reason": "...", ...}
  atau `None` sebagai sinyal *fallback* ke logika deterministik eksisting
  (AI nonaktif / tanpa `GEMINI_API_KEY` / error / timeout).
- Semua tool membungkus fungsi eksisting dan bersifat *pure* (tidak memutasi
  session / tidak mengirim event WS); penerapan rute tetap dilakukan oleh
  pemanggil (navigation_worker / WebSocket handler).
"""

import asyncio
import json
import os
import re
import time

from starlette.concurrency import run_in_threadpool

from app.core.logging import get_logger

logger = get_logger("agent")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw in ("1", "true", "True", "TRUE", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
AI_REROUTE_ENABLED = _env_bool("AI_REROUTE_ENABLED", False)
GEMINI_REROUTE_TIMEOUT_S = _env_float("GEMINI_REROUTE_TIMEOUT_S", 15.0)

_MAX_TOOL_ROUNDS = 6
_CONGESTION_RATIO = 1.5
_ALT_MAX = 3

# Batas konkuransi panggilan AI (semaphore global).
_MAX_CONCURRENT = max(1, _env_int("GEMINI_MAX_CONCURRENT", 3))
# Circuit breaker: matikan AI sementara bila ada N kegagalan beruntun.
_BREAKER_FAILURE_THRESHOLD = max(2, _env_int("GEMINI_BREAKER_THRESHOLD", 3))
_BREAKER_RESET_S = _env_float("GEMINI_BREAKER_RESET_S", 30.0)

_decide_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
_breaker_failures = 0
_breaker_open_until = 0.0

_MAX_REASON_LEN = 500


def _sanitize_reason(value: str) -> str:
    """Bersihkan alasan keputusan agar aman masuk DB / payload WS."""
    if not isinstance(value, str):
        value = str(value)
    value = "".join(ch for ch in value if ch >= " " or ch in "\n\t")
    return value.strip()[:_MAX_REASON_LEN]


def _breaker_open() -> bool:
    """True bila circuit breaker sedang terbuka (skip AI sementara)."""
    global _breaker_failures, _breaker_open_until
    if _breaker_open_until and time.monotonic() >= _breaker_open_until:
        _breaker_open_until = 0.0
        _breaker_failures = 0
    return _breaker_open_until > 0.0


def _breaker_record_failure() -> None:
    global _breaker_failures, _breaker_open_until
    _breaker_failures += 1
    if _breaker_failures >= _BREAKER_FAILURE_THRESHOLD:
        _breaker_open_until = time.monotonic() + _BREAKER_RESET_S
        logger.warning(
            "[AI] Circuit breaker terbuka selama %.0fs "
            "(%d kegagalan beruntun).", _BREAKER_RESET_S, _breaker_failures)


def _breaker_record_success() -> None:
    global _breaker_failures
    if _breaker_failures:
        _breaker_failures = 0


def _reset_breaker() -> None:
    """Reset state circuit breaker (untuk pengujian)."""
    global _breaker_failures, _breaker_open_until
    _breaker_failures = 0
    _breaker_open_until = 0.0


def _system_prompt(hint: str) -> str:
    if hint == "off_route":
        return (
            "Kamu adalah AI navigator pengiriman yang memutuskan apakah kurir "
            "benar-benar menyimpang dari rute (off-route) atau hanya noise GPS. "
            "Konteks JSON disediakan (posisi, sisa jarak/waktu, jarak ke polyline "
            "aktif). Gunakan tool untuk menggali info tambahan bila perlu.\n"
            "Aturan:\n"
            "- action='apply' bila kurir benar-benar keluar dari rute dan ada rute "
            "perbaikan yang wajar.\n"
            "- action='ignore' bila jarak melenceng kecil / posisi masih mendekati "
            "polyline / kecepatan menunjukkan kurir masih bergerak di sepanjang "
            "rute (noise GPS, jalan memutar wajar).\n"
            "- action='defer' bila ragu (mis. butuh info lebih lanjut).\n"
            "Balas HANYA satu JSON tanpa markdown:\n"
            '{"action": "apply"|"ignore"|"defer", "reason": "<alasan singkat>"}'
        )
    return (
        "Kamu adalah AI navigator pengiriman yang memutuskan apakah auto-reroute "
        "akibat kemacetan layak diterapkan. Konteks JSON disediakan (posisi kurir, "
        "sisa jarak/waktu rute aktif, kandidat rute baru dengan estimasi ETA & "
        "penghematan detik). Gunakan tool untuk menggali info tambahan bila perlu "
        "(mis. penalti traffic, alternatif rute).\n"
        "Aturan:\n"
        "- action='apply' bila rute kandidat memberikan penghematan waktu yang "
        "bermakna (>= ~120 detik) dan rute tetap layak.\n"
        "- action='ignore' bila penghematan tidak berarti atau rute baru tidak "
        "lebih baik.\n"
        "- action='defer' bila ragu (mis. data traffic belum cukup).\n"
        "Balas HANYA satu JSON tanpa markdown:\n"
        '{"action": "apply"|"ignore"|"defer", "reason": "<alasan singkat>"}'
    )


class ToolContext:
    """Runtime object yang dibutuhkan tool (bukan bagian payload ke LLM)."""

    def __init__(self, app, redis, session):
        self.app = app
        self.redis = redis
        self.session = session


def _normalize_mode(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    return val if val in ("motorcycle", "car", "truck") else "car"


def build_reroute_context(session, lat: float, lon: float, *,
                          hint: str, extra: dict | None = None) -> dict:
    """Bangun konteks JSON yang dikirim ke LLM (tanpa objek runtime)."""
    from app.services.navigation import remaining_progress

    context = {
        "hint": hint,
        "kurir_id": session.kurir_id,
        "mode": session.mode,
        "destination": ({"lat": session.dest[0], "lng": session.dest[1]}
                        if session.dest is not None else None),
        "last_position": {"lat": lat, "lng": lon},
        "remaining": remaining_progress(session, lat, lon),
        "off_route_active": session.off_route_active,
    }
    if extra:
        context.update(extra)
    return context


def _parse_decision(text: str) -> dict | None:
    """Parse jawaban model menjadi dict keputusan yang disanitasi."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    data = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                pass
    if data is None:
        logger.warning("[AI] Jawaban model tidak dapat di-parse: %r", text[:120])
        return None
    action = str(data.get("action", "defer")).strip().lower()
    if action not in ("apply", "ignore", "defer"):
        action = "defer"
    out = {
        "action": action,
        "reason": _sanitize_reason(data.get("reason", "")),
    }
    for key in ("polyline", "eta_s", "saving_s", "route_id"):
        if key in data:
            out[key] = data[key]
    return out


# ---------------------------------------------------------------------------
# Tools (Function Calling) — membungkus fungsi eksisting, semua pure.
# ---------------------------------------------------------------------------

async def _tool_get_remaining_progress(ctx: ToolContext, args: dict) -> dict:
    from app.services.navigation import remaining_progress

    lat = float(args.get("lat"))
    lon = float(args.get("lng"))

    # Save state sebelum memanggil remaining_progress (yang memiliki side
    # effect: menulis traveled_distance_m, current_step_index, dan
    # _last_nearest_idx). AI bisa panggil dengan lat/lng eksploratif —
    # tanpa restore, state kurir asli jadi rusak.
    saved_traveled = ctx.session.traveled_distance_m
    saved_step_idx = ctx.session.current_step_index
    saved_nearest_idx = ctx.session._last_nearest_idx
    try:
        prog = remaining_progress(ctx.session, lat, lon)
    finally:
        ctx.session.traveled_distance_m = saved_traveled
        ctx.session.current_step_index = saved_step_idx
        ctx.session._last_nearest_idx = saved_nearest_idx

    if prog is None:
        return {"error": "belum ada rute aktif yang valid"}
    return prog


async def _tool_compute_reroute(ctx: ToolContext, args: dict) -> dict:
    from app.services.navigation import compute_reroute
    from app.services.polyline import encode_polyline

    session = ctx.session
    lat = float(args.get("lat"))
    lon = float(args.get("lng"))
    dest_lat = float(args.get("dest_lat", session.dest[0]))
    dest_lon = float(args.get("dest_lng", session.dest[1]))
    traffic = bool(args.get("traffic", True))

    original_mode = session.mode
    if args.get("mode"):
        session.mode = _normalize_mode(args.get("mode"))
    try:
        async with session.lock:
            response = await compute_reroute(
                ctx.app, ctx.redis, session, lat, lon, traffic=traffic)
    finally:
        session.mode = original_mode
    if response is None:
        return {"route_found": False}
    return {
        "route_found": True,
        "eta_s": round(response.estimated_time_seconds or 0.0, 1),
        "distance_m": round(response.total_distance_meters or 0.0, 1),
        "coords_count": len(response.route_coordinates),
        "polyline": encode_polyline(response.route_coordinates, 5),
    }


async def _tool_get_traffic_penalties(ctx: ToolContext, args: dict) -> dict:
    from app.services.traffic.smart_hybrid import get_request_penalties

    lat1 = float(args.get("lat1"))
    lon1 = float(args.get("lng1"))
    lat2 = float(args.get("lat2", args.get("dest_lat")))
    lon2 = float(args.get("lng2", args.get("dest_lng")))
    if lat2 is None or lon2 is None:
        dest = ctx.session.dest
        if dest is None:
            return {"error": "destinasi tidak tersedia"}
        lat2, lon2 = dest[0], dest[1]
    penalties = await get_request_penalties(
        ctx.app, ctx.redis, (lat1, lon1), (lat2, lon2))
    multipliers = [v for v in penalties.values() if v != float("inf")]
    return {
        "penalty_count": len(penalties),
        "max_multiplier": round(max(multipliers), 2) if multipliers else 0.0,
        "blocked_edges": sum(1 for v in penalties.values() if v == float("inf")),
        "congested": bool(multipliers and max(multipliers) >= _CONGESTION_RATIO),
    }


async def _tool_get_route_options(ctx: ToolContext, args: dict) -> dict:
    from app.api.v1.endpoints.pathfinding import (
        _best_route,
        _compute_route,
        _resolve_plan,
    )
    from app.schemas.pathfinding import Coordinate, RouteRequest
    from app.services.pathfinding.route_options import (
        bump_penalties,
        max_overlap,
        route_edges,
    )
    from app.services.traffic.smart_hybrid import get_request_penalties

    lat = float(args.get("lat"))
    lon = float(args.get("lng"))
    dest_lat = float(args.get("dest_lat"))
    dest_lon = float(args.get("dest_lng"))
    mode = _normalize_mode(args.get("mode") or ctx.session.mode)

    try:
        plan = await run_in_threadpool(
            _resolve_plan, ctx.app, lat, lon, dest_lat, dest_lon)
    except Exception as exc:
        return {"error": "plan tidak tersedia: %s" % exc}

    payload = RouteRequest(
        origin=Coordinate(latitude=lat, longitude=lon),
        destination=Coordinate(latitude=dest_lat, longitude=dest_lon),
        mode=mode,
        last_mile_precision=ctx.session.last_mile,
    )
    try:
        traffic_penalties = await get_request_penalties(
            ctx.app, ctx.redis, (lat, lon), (dest_lat, dest_lon))
        best, node_sequence, final_penalties, mode = await _best_route(
            ctx.app, plan, ctx.redis, traffic_penalties, payload, mode,
            lat, lon, dest_lat, dest_lon, need_nodes=True)
    except Exception as exc:
        return {"error": "perhitungan rute gagal: %s" % exc}
    if best is None or node_sequence is None:
        return {"options": []}

    options = [{
        "index": 1,
        "is_best": True,
        "eta_s": round(best.estimated_time_seconds or 0.0, 1),
        "distance_m": round(best.total_distance_meters or 0.0, 1),
    }]
    accepted_seqs = [node_sequence]
    used_edge_sets = [route_edges(node_sequence)]

    for route_id in range(2, _ALT_MAX + 1):
        alt_penalties = bump_penalties(final_penalties, used_edge_sets, 8.0)
        try:
            resp, ns = await _compute_route(
                ctx.app, plan, ctx.redis, alt_penalties,
                lat, lon, dest_lat, dest_lon,
                ctx.session.last_mile, mode, need_nodes=True)
        except Exception:
            break
        if resp is None or ns is None:
            break
        if any(max_overlap(ns, other) >= 0.8 for other in accepted_seqs):
            break
        options.append({
            "index": route_id,
            "is_best": False,
            "eta_s": round(resp.estimated_time_seconds or 0.0, 1),
            "distance_m": round(resp.total_distance_meters or 0.0, 1),
        })
        accepted_seqs.append(ns)
        used_edge_sets.append(route_edges(ns))
    return {"options": options}


async def _tool_apply_reroute(ctx: ToolContext, args: dict) -> dict:
    """Tool komitment: menyatakan model memutuskan untuk menerapkan reroute."""
    return {
        "ok": True,
        "action": "apply",
        "reason": str(args.get("reason", "") or "").strip(),
    }


_TOOL_IMPL = {
    "get_remaining_progress": _tool_get_remaining_progress,
    "compute_reroute": _tool_compute_reroute,
    "get_traffic_penalties": _tool_get_traffic_penalties,
    "get_route_options": _tool_get_route_options,
    "apply_reroute": _tool_apply_reroute,
}


_TOOL_DEFS = [
    {
        "name": "get_remaining_progress",
        "description": (
            "Ambil sisa jarak (meter), sisa waktu (detik), dan persen progres "
            "dari posisi (lat,lng) ke ujung polyline rute aktif."),
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude posisi"},
                "lng": {"type": "number", "description": "Longitude posisi"},
            },
            "required": ["lat", "lng"],
        },
    },
    {
        "name": "compute_reroute",
        "description": (
            "Hitung ulang rute terbaik dari posisi (lat,lng) ke destinasi "
            "(dest_lat,dest_lng) memakai pipeline pathfinding eksisting. "
            "Mengembalikan ETA, jarak, dan polyline (precision 5)."),
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude asal"},
                "lng": {"type": "number", "description": "Longitude asal"},
                "dest_lat": {"type": "number", "description": "Latitude tujuan"},
                "dest_lng": {"type": "number", "description": "Longitude tujuan"},
                "mode": {
                    "type": "string",
                    "enum": ["motorcycle", "car", "truck"],
                    "description": "Mode kendaraan (default: mode sesi)",
                },
                "traffic": {
                    "type": "boolean",
                    "description": "Pakai penalti traffic (default true)",
                },
            },
            "required": ["lat", "lng"],
        },
    },
    {
        "name": "get_traffic_penalties",
        "description": (
            "Ambil ringkasan penalti traffic antara dua titik: jumlah edge "
            "terdampak, multiplier maksimum, jumlah edge tertutup, dan flag "
            "macet (congested)."),
        "parameters": {
            "type": "object",
            "properties": {
                "lat1": {"type": "number", "description": "Latitude titik 1"},
                "lng1": {"type": "number", "description": "Longitude titik 1"},
                "lat2": {"type": "number", "description": "Latitude titik 2"},
                "lng2": {"type": "number", "description": "Longitude titik 2"},
            },
            "required": ["lat1", "lng1", "lat2", "lng2"],
        },
    },
    {
        "name": "get_route_options",
        "description": (
            "Hitung beberapa alternatif rute dari (lat,lng) ke (dest_lat,"
            "dest_lng) beserta ETA & jaraknya (opsi 1 = rute terbaik)."),
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude asal"},
                "lng": {"type": "number", "description": "Longitude asal"},
                "dest_lat": {"type": "number", "description": "Latitude tujuan"},
                "dest_lng": {"type": "number", "description": "Longitude tujuan"},
                "mode": {
                    "type": "string",
                    "enum": ["motorcycle", "car", "truck"],
                    "description": "Mode kendaraan (default: mode sesi)",
                },
            },
            "required": ["lat", "lng", "dest_lat", "dest_lng"],
        },
    },
    {
        "name": "apply_reroute",
        "description": (
            "Nyatakan komitmen untuk menerapkan reroute. Panggil tool ini bila "
            "Anda memutuskan action='apply'."),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Alasan menerapkan"},
            },
            "required": [],
        },
    },
]


async def _dispatch_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    impl = _TOOL_IMPL.get(name)
    if impl is None:
        return {"error": "tool tidak dikenal: %s" % name}
    try:
        return await impl(ctx, args)
    except Exception as exc:
        logger.warning("[AI] Tool %s gagal: %s", name, exc)
        return {"error": "tool error: %s" % exc}


def _is_rate_limit(exc: Exception) -> bool:
    """Deteksi rate-limit / quota (HTTP 429 / RESOURCE_EXHAUSTED)."""
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    if code in (429, 42903):
        return True
    message = str(exc)
    return "429" in message or "RESOURCE_EXHAUSTED" in message


def _generate_once(client, model: str, contents, config) -> object:
    return client.models.generate_content(model=model, contents=contents, config=config)


async def _call_model(primary, backup, model: str, contents, config) -> object:
    """Panggil Gemini; bila key primer kena rate-limit, pakai key cadangan.
    Jika keduanya habis (429 RESOURCE_EXHAUSTED), kembalikan None untuk fallback deterministik."""
    try:
        return await run_in_threadpool(
            _generate_once, primary, model, contents, config)
    except Exception as exc:
        if _is_rate_limit(exc) and backup is not None:
            logger.warning("[AI] Rate limit key 1, memakai key cadangan.")
            return await run_in_threadpool(
                _generate_once, backup, model, contents, config)
        # 429 RESOURCE_EXHAUSTED (quota habis) -> fallback deterministik
        if _is_rate_limit(exc) and backup is None:
            logger.error("[AI] Kedua key habis (429 RESOURCE_EXHAUSTED). Fallback ke logika deterministik.")
            return None
        raise


def _make_clients():
    from google import genai

    primary = genai.Client(api_key=GEMINI_API_KEY)
    backup = (genai.Client(api_key=GEMINI_API_KEY_2)
              if GEMINI_API_KEY_2 else None)
    return primary, backup


async def _agent_loop(ctx: ToolContext, context: dict, hint: str) -> dict | None:
    from google.genai import types

    primary, backup = _make_clients()
    config = types.GenerateContentConfig(
        system_instruction=_system_prompt(hint),
        tools=[types.Tool(function_declarations=_TOOL_DEFS)],
        temperature=0.2,
    )
    contents = [types.Content(
        role="user",
        parts=[types.Part(text=json.dumps(context, ensure_ascii=False))],
    )]

    for _ in range(_MAX_TOOL_ROUNDS):
        response = await _call_model(
            primary, backup, GEMINI_MODEL, contents, config)
        if not response.candidates or not response.candidates[0].content.parts:
            return None
        content = response.candidates[0].content
        parts = content.parts

        function_calls = [p.function_call for p in parts if p.function_call]
        if function_calls:
            contents.append(content)
            responses = []
            for fc in function_calls:
                result = await _dispatch_tool(
                    ctx, fc.name, dict(fc.args or {}))
                responses.append(types.Part(function_response=types.FunctionResponse(
                    name=fc.name, response=result)))
            contents.append(types.Content(role="user", parts=responses))
            continue

        texts = [p.text for p in parts if p.text]
        if texts:
            return _parse_decision("".join(texts))
        return None
    return None


async def decide_reroute(context: dict, decision_hint: str, *,
                          app=None, redis=None, session=None) -> dict | None:
    """Putuskan apakah reroute layak. Kembalikan None sebagai sinyal fallback.

    Fallback dipicu bila:
    - `AI_REROUTE_ENABLED` = False, atau
    - `GEMINI_API_KEY` kosong, atau
    - circuit breaker sedang terbuka (kegagalan beruntun), atau
    - error / timeout (batas `GEMINI_REROUTE_TIMEOUT_S`, default 15.0s), atau
    - slot semaphore konkuransi penuh melebihi timeout.
    - quota AI habis (429 RESOURCE_EXHAUSTED) pada kedua key.
    """
    if not AI_REROUTE_ENABLED or not GEMINI_API_KEY:
        return None
    if session is None:
        return None
    if _breaker_open():
        logger.warning("[AI] Circuit breaker terbuka; skip AI reroute.")
        return None
    ctx = ToolContext(app=app, redis=redis, session=session)
    try:
        async with _decide_semaphore:
            result = await asyncio.wait_for(
                _agent_loop(ctx, context, decision_hint),
                timeout=GEMINI_REROUTE_TIMEOUT_S)
        if result is not None:
            _breaker_record_success()
        return result
    except asyncio.TimeoutError:
        _breaker_record_failure()
        logger.warning(
            "[AI] decide_reroute(%s) timeout setelah %.1fs.",
            decision_hint, GEMINI_REROUTE_TIMEOUT_S)
        return None
    except Exception as exc:
        _breaker_record_failure()
        logger.warning("[AI] decide_reroute(%s) error: %s", decision_hint, exc)
        return None