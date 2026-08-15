import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


def _database_url() -> str:
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit
    return "postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}".format(
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", "postgres"),
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        name=os.environ.get("DB_NAME", "test2"),
    )


class Base(DeclarativeBase):
    pass


engine = create_async_engine(_database_url(), echo=False)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    import app.models.paket
    import app.models.route_history
    import app.models.kurir
    import app.models.hub
    import app.models.batch
    import app.models.shipment
    import app.models.tracking_history
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "postgresql":
            await conn.execute(
                text(
                    "ALTER TABLE paket ADD COLUMN IF NOT EXISTS ongkir "
                    "DOUBLE PRECISION NOT NULL DEFAULT 0"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE paket ADD COLUMN IF NOT EXISTS latitude "
                    "DOUBLE PRECISION"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE paket ADD COLUMN IF NOT EXISTS longitude "
                    "DOUBLE PRECISION"
                )
            )


async def dispose_db() -> None:
    await engine.dispose()
