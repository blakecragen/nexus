"""Database engine and session factory — SQLite via aiosqlite."""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_db(database_url: str) -> None:
    """Initialize the async database engine and session factory."""
    global _engine, _session_factory

    connect_args = {}
    if database_url.startswith("sqlite"):
        # SQLite needs check_same_thread=False for async usage
        connect_args["check_same_thread"] = False

    _engine = create_async_engine(
        database_url,
        echo=False,
        connect_args=connect_args,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a new async session for use as a FastAPI dependency."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    session = _session_factory()
    try:
        yield session
    finally:
        await session.close()


def get_engine():
    """Return the current engine (for Alembic migrations, table creation, etc.)."""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


def get_session_factory():
    """Return the session factory (for use outside of FastAPI dependency injection)."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory
