import os

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()
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
    import app.models.driver_report
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
            await conn.execute(
                text(
                    "ALTER TABLE paket ADD COLUMN IF NOT EXISTS service_type "
                    "VARCHAR(20) NOT NULL DEFAULT 'REGULAR'"
                )
            )
            await conn.execute(
                text(
                    "UPDATE paket SET service_type = 'EXPRESS' "
                    "WHERE service_type = 'REGULAR' "
                    "AND LOWER(jenis_pengiriman) IN "
                    "('express', 'same_day', 'same-day', 'next_day', 'next-day')"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE tracking_history ADD COLUMN IF NOT EXISTS "
                    "recipient_name VARCHAR(255)"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE tracking_history ADD COLUMN IF NOT EXISTS "
                    "latitude DOUBLE PRECISION"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE tracking_history ADD COLUMN IF NOT EXISTS "
                    "longitude DOUBLE PRECISION"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE tracking_history ADD COLUMN IF NOT EXISTS "
                    "photo_urls JSON"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE tracking_history DROP COLUMN IF EXISTS signature_url"
                )
            )


async def dispose_db() -> None:
    await engine.dispose()
