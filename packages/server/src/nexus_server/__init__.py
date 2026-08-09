"""``nexus_server`` — the Nexus control plane (FastAPI server package).

Nexus is a distributed job-execution cluster. This package is the *server* half:
the single central process that owns the database, authenticates users and
agents, decides which node runs which step, and streams live state to the web
dashboard. The *agent* half (``nexus_agent``) runs on each worker node and
connects back here over a WebSocket; shared wire types and step base classes
live in ``nexus_common``, and the concrete step implementations live in
``nexus_steps``.

Sub-packages
------------
``config``
    :class:`~nexus_server.config.Settings` — the frozen, env-derived
    configuration object built once at process start.
``main``
    Application factory (:func:`~nexus_server.main.create_app`), the ASGI
    ``app`` object uvicorn imports, and the startup/shutdown ``lifespan``.
``api``
    HTTP + WebSocket surface. ``api/deps.py`` holds the shared FastAPI
    dependencies (DB session, current user); ``api/routes/*`` holds one router
    per resource (auth, nodes, pools, jobs, steps, credentials, storage,
    artifacts) plus ``api/routes/ws.py``, which owns the agent and dashboard
    WebSocket endpoints and the process-wide ``ConnectionManager``.
``db``
    SQLAlchemy async engine/session plumbing (``db/session.py``), ORM models
    (``db/models.py``), and every query/mutation helper (``db/ops.py``). Nothing
    outside ``db`` should build SQL directly.
``runner``
    :class:`~nexus_server.runner.JobRunner` — the per-job execution loop that
    picks nodes, dispatches steps to agents over the WebSocket, waits for
    completion events, and advances job state. ``runner/resume.py`` re-arms jobs
    that were mid-flight when the process last died.
``services``
    Cross-cutting services: JWT/password auth (``auth_service``), encrypted
    credential storage (``services/credentials``), and pluggable artifact
    storage backends (``services/storage``: MinIO/S3 and NAS).

Wiring
------
Everything is assembled in :func:`nexus_server.main.create_app` and its
``lifespan``: long-lived singletons (auth service, credential manager, storage
manager, job runner) are attached to ``app.state`` and read back out by route
handlers via ``request.app.state``. There is no global service locator, so tests
can construct an app with substituted state (see ``tests/conftest.py``).

AI Note: this package is intentionally import-light — it deliberately defines
and re-exports nothing. Importing ``nexus_server`` must stay free of side
effects (no DB connections, no env reads, no submodule imports) because
``tests/conftest.py`` and ``nexus_server.main`` import deep submodules
(``nexus_server.config``, ``nexus_server.db.models``, ...) in a specific order
while the hermetic test environment is still being assembled. Adding eager
re-exports here would drag ``main`` — and therefore ``Settings.from_env()``,
which raises on a missing ``JWT_SECRET`` — into every import of the package.
"""
