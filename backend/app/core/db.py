from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def create_missing_indexes(conn) -> None:
    """create_all skips tables that exist, so it never adds an index to them."""
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(conn, checkfirst=True)


async def init_db() -> None:
    # TODO: replace with Alembic migrations once the schema stabilises.
    from app import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        # api and collector start together; their check-then-create would otherwise race.
        await conn.execute(text("SELECT pg_advisory_xact_lock(727401)"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(create_missing_indexes)
