import asyncio
import logging
import os

from app.services.cache_service import (
    PENALTIES_KEY,
    clear_penalties,
)
from app.services.traffic.matcher import snap_segment
from app.services.traffic.provider import get_providers, traffic_enabled

logger = logging.getLogger("pathfinding")

# Prioritas provider saat dua provider melaporkan edge yang sama:
# tomtom (indeks kecil) menang atas internal. Edge yang sudah diisi provider
# berprioritas lebih tinggi tidak ditimpa.
_PROVIDER_PRIORITY = {"tomtom": 0, "internal": 1}


def _poll_interval() -> float:
    try:
        return max(5.0, float(os.environ.get("TRAFFIC_POLL_INTERVAL", "60")))
    except ValueError:
        return 60.0


def _snap_tolerance() -> float | None:
    raw = os.environ.get("TRAFFIC_SNAP_TOLERANCE_M", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _reference_graph(app):
    """Pilih graf untuk proyeksi segmen -> edge_id.

    Prioritas: region graph -> path graph (warmup) -> base graph. Graf ini
    hanya dipakai sebagai "geometri rujukan"; penalti tersimpan per edge_id
    node OSM sehingga berlaku juga pada graf lokal tile saat routing.
    """
    pg = (getattr(app.state, "region_graph", None)
          or getattr(app.state, "path_graph", None))
    if pg is not None and getattr(pg, "graph", None):
        return pg.graph, pg.locations
    from app.services.pathfinding.graph_loader import (
        base_available,
        load_base_graph,
    )
    if base_available():
        base = load_base_graph()
        if getattr(base, "graph", None):
            return base.graph, base.locations
    return None, None


async def _collect_events(providers) -> list:
    events = []
    for provider in providers:
        try:
            fetched = await provider.fetch()
            if fetched:
                events.extend(fetched)
        except Exception as exc:
            logger.warning("[TRAFFIC] Provider %s gagal: %s", provider.name, exc)
    return events


async def poll_once(app, redis) -> int:
    """Satu siklus pengambilan traffic; kembalikan jumlah penalti tersimpan."""
    graph, locations = _reference_graph(app)
    if graph is None or locations is None:
        logger.warning(
            "[TRAFFIC] Tidak ada graf rujukan, penalti dikosongkan.")
        await clear_penalties(redis)
        return 0

    tolerance = _snap_tolerance()
    providers = get_providers()
    events = await _collect_events(providers)

    weights: dict[int, float] = {}
    filled: set[int] = set()
    for event in events:
        prio = _PROVIDER_PRIORITY.get(getattr(event, "provider", None), 99)
        for edge in snap_segment(graph, locations, event.coordinates, tolerance):
            if edge in filled:
                continue
            filled.add(edge)
            weights[edge] = event.weight

    if weights:
        await redis.hset(PENALTIES_KEY, mapping={str(k): str(v) for k, v in weights.items()})
    else:
        await clear_penalties(redis)
    logger.info("[TRAFFIC] Poll selesai: %d event -> %d edge penalti.",
                len(events), len(weights))
    return len(weights)


async def traffic_poller(app, redis) -> None:
    """Loop background: ambil data traffic periodik lalu simpan ke Redis."""
    interval = _poll_interval()
    logger.info("[TRAFFIC] Poller dimulai (interval %.0fs).", interval)
    while True:
        try:
            await poll_once(app, redis)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[TRAFFIC] Siklus poll gagal: %s", exc)
        await asyncio.sleep(interval)


def traffic_poller_running(app) -> bool:
    task = getattr(app.state, "traffic_task", None)
    return task is not None and not task.done()


def should_start_poller() -> bool:
    return traffic_enabled()
