"""Database engine and session factory — SQLite via aiosqlite.

Owns the *process-wide* SQLAlchemy async engine and session factory for the
Nexus server. This module holds mutable module-level state that must be
initialised exactly once, at startup, before anything touches the database.

Lifecycle:
    1. ``nexus_server.main.lifespan()`` calls :func:`init_db` with
       ``settings.database_url`` (default ``sqlite+aiosqlite:///nexus.db``).
    2. ``lifespan`` then runs ``Base.metadata.create_all`` through
       :func:`get_engine` to materialise any missing tables.
    3. Request handlers get a session through the FastAPI dependency
       :func:`get_session`; background workers (job runner, scheduler, the
       agent WebSocket handler) build their own via :func:`get_session_factory`
       because they outlive the request scope.

Consumers should not import ``_engine``/``_session_factory`` directly — the
accessors exist so that a call before :func:`init_db` fails loudly with a clear
``RuntimeError`` instead of an ``AttributeError`` on ``None``.

AI Note: the module-level singletons mean the whole process shares one engine
and therefore one SQLite connection pool. Tests that need isolation build their
own engine/sessionmaker in ``tests/conftest.py`` rather than calling
:func:`init_db`, so calling ``init_db`` twice (e.g. spinning up a second app in
the same interpreter) silently replaces the engine for everyone — the previous
engine is never disposed.
"""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# AI Note: module-level singletons, populated only by init_db(). Every accessor
# below guards on None so "database not initialized" is an explicit error
# rather than a confusing NoneType failure deep inside a request.
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_db(database_url: str) -> None:
    """Initialize the async database engine and session factory.

    Must be called once, before any other function in this module, and before
    any ``ops.*`` call. ``nexus_server.main.lifespan()`` is the sole production
    caller.

    Args:
        database_url: SQLAlchemy async URL, e.g.
            ``sqlite+aiosqlite:///nexus.db`` or
            ``sqlite+aiosqlite:///:memory:``. The driver must be an async one
            (``aiosqlite``, ``asyncpg``, ...) — a sync URL such as
            ``sqlite:///`` raises at engine creation.

    Side effects:
        Rebinds the module-level ``_engine`` and ``_session_factory``
        singletons. Calling it a second time replaces both **without disposing
        the previous engine**, leaking its connection pool.
    """
    global _engine, _session_factory

    connect_args = {}
    if database_url.startswith("sqlite"):
        # SQLite needs check_same_thread=False for async usage
        # AI Note: aiosqlite runs the sqlite3 connection on a helper thread, so
        # the connection is legitimately used from a thread other than the one
        # that created it. Without this flag sqlite3 raises ProgrammingError.
        # It is safe here only because SQLAlchemy's pool serialises access.
        connect_args["check_same_thread"] = False

    _engine = create_async_engine(
        database_url,
        echo=False,
        connect_args=connect_args,
    )
    # AI Note: expire_on_commit=False is load-bearing. ops.* commits inside
    # nearly every function and then returns the ORM object to a caller that
    # reads attributes off it (routes serialise it, the runner reads
    # job.steps_config, etc.). With the default True those reads would trigger
    # a lazy refresh outside the await boundary and blow up with
    # MissingGreenlet. Do not "clean this up".
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a new async session for use as a FastAPI dependency.

    Used as ``db: AsyncSession = Depends(get_session)`` in the API routes: one
    session per request, closed when the request finishes.

    Yields:
        A fresh :class:`AsyncSession` bound to the process engine.

    Raises:
        RuntimeError: If :func:`init_db` has not run yet.

    AI Note: this deliberately does *not* commit or roll back on exit — the
    ``ops.*`` helpers own transaction boundaries and commit themselves. Closing
    a session with uncommitted changes discards them, which is the intended
    behaviour for a handler that raised.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    session = _session_factory()
    try:
        yield session
    finally:
        await session.close()


def get_engine():
    """Return the current engine (for Alembic migrations, table creation, etc.).

    Returns:
        The process-wide :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.

    Raises:
        RuntimeError: If :func:`init_db` has not run yet.
    """
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


def get_session_factory():
    """Return the session factory (for use outside of FastAPI dependency injection).

    Callers are the components that live outside the request lifecycle and must
    open/close their own sessions: ``runner/runner.py``, ``runner/resume.py``,
    the agent WebSocket handler, and node-side background tasks in
    ``api/routes/nodes.py``.

    Returns:
        The ``async_sessionmaker`` bound to the process engine; use it as
        ``async with session_factory() as db:``.

    Raises:
        RuntimeError: If :func:`init_db` has not run yet.

    AI Note: background tasks must NOT hold a session across long awaits (agent
    round-trips can take hours). The convention in the runner is to open a
    short-lived session per DB touch; keep it that way or SQLite will hold a
    write lock for the duration of a step.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory
