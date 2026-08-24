import os

import redis.asyncio as aioredis


def _redis_url() -> str:
    explicit = os.environ.get("REDIS_URL")
    if explicit:
        return explicit
    host = os.environ.get("REDIS_HOST", "localhost")
    port = os.environ.get("REDIS_PORT", "6379")
    password = os.getenv("REDIS_PASSWORD", "")

    auth = f":{password}@" if password else ""

    return f"redis://{host}:{port}"


def get_redis() -> aioredis.Redis:
    return aioredis.from_url(
        _redis_url(),
        decode_responses=True,
        protocol=2,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


async def close_redis(client) -> None:
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        pass
