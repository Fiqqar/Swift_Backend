import os
os.environ["APP_ENV"] = "development"

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Import after setting env
from app.core.logging import setup_logging, get_logger

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = get_logger("system")
    logger.info("system.startup", version="1.0.0", env=os.environ.get("APP_ENV", "development"))

    yield


app = FastAPI(lifespan=lifespan)


async def test():
    async with lifespan(app):
        print("Lifespan context completed successfully!")


if __name__ == "__main__":
    asyncio.run(test())