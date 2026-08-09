"""Persistence layer for the Nexus server.

This package owns everything that touches the database and is deliberately
split into three layers so the rest of the server never has to know which
engine or ORM is in use:

- ``models.py``   — SQLAlchemy declarative ORM models (the schema itself).
- ``session.py``  — process-wide async engine + session factory lifecycle.
- ``ops.py``      — the repository / public API. **Everything outside this
  package should import from ``nexus_server.db.ops`` only.**
- ``migrations/`` — reserved for Alembic revisions (see that package's
  docstring; schema is currently created via ``Base.metadata.create_all``).

Typical wiring:
    ``main.lifespan()`` calls ``session.init_db(settings.database_url)`` once at
    startup, then ``Base.metadata.create_all`` to materialise tables. FastAPI
    routes receive an ``AsyncSession`` via the ``session.get_session``
    dependency; long-lived background workers (``runner/runner.py``,
    ``runner/scheduler.py``, the WebSocket handlers in ``api/routes/ws.py``)
    build their own sessions from ``session.get_session_factory()`` because
    they outlive any single request.

AI Note: this package intentionally exports nothing at the top level. Keeping
``__init__`` free of imports avoids a circular-import hazard — ``ops`` imports
``models``, and several service modules import ``ops`` during module import.
Add re-exports here only if you are certain no cycle is introduced.
"""
