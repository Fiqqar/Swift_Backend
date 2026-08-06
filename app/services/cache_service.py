import hashlib
import json
import logging

logger = logging.getLogger("pathfinding")

ROUTE_TTL = 300
PENALTIES_KEY = "traffic:penalties"


def graph_scope(pg) -> str:
    return f"{pg.ref_lat:.6f}_{pg.ref_lon:.6f}_{pg.radius}"


def _penalty_signature(penalties: dict) -> str:
    if not penalties:
        return "0"
    digest = hashlib.sha1()
    for edge_id in sorted(penalties):
        digest.update(f"{edge_id}:{penalties[edge_id]};".encode())
    return digest.hexdigest()[:16]


def route_key(scope: str, start_node: int, goal_node: int, penalties: dict) -> str:
    return f"route:{scope}:{start_node}:{goal_node}:{_penalty_signature(penalties)}"


async def get_route(redis, key: str) -> dict | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Cache get gagal: %s", exc)
        return None


async def set_route(redis, key: str, data: dict, ttl: int = ROUTE_TTL) -> None:
    if redis is None:
        return
    try:
        await redis.set(key, json.dumps(data), ex=ttl)
    except Exception as exc:
        logger.warning("Cache set gagal: %s", exc)


async def load_penalties(redis) -> dict[int, float]:
    if redis is None:
        return {}
    try:
        raw = await redis.hgetall(PENALTIES_KEY)
        return {int(k): float(v) for k, v in raw.items()}
    except Exception as exc:
        logger.warning("Load penalti gagal: %s", exc)
        return {}


async def store_penalty(redis, edge_id: int, multiplier: float) -> None:
    if redis is None:
        raise ConnectionError("Redis tidak tersedia")
    await redis.hset(PENALTIES_KEY, str(edge_id), str(multiplier))


async def clear_penalties(redis) -> None:
    if redis is None:
        return
    try:
        await redis.delete(PENALTIES_KEY)
    except Exception as exc:
        logger.warning("Hapus penalti gagal: %s", exc)
