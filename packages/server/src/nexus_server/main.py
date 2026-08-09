"""Nexus server — FastAPI application entry point.

This module is the composition root for the Nexus control plane. It owns three
things and nothing else:

1. :func:`create_app` — builds the ``FastAPI`` instance: CORS, and one router
   per resource mounted under ``/api/*`` plus the WebSocket router at the root.
2. :func:`lifespan` — the startup/shutdown hook that initializes the database
   and constructs the long-lived singletons (auth service, credential manager,
   storage manager, job runner), parking each on ``app.state``.
3. ``app`` / :func:`main` — the module-level ASGI object uvicorn imports
   (``uvicorn nexus_server.main:app``, used by ``Dockerfile.api`` and
   ``dev.sh``) and a small CLI wrapper around it.

How it fits together
--------------------
Route handlers never construct services; they pull them off
``request.app.state`` (via ``nexus_server.api.deps``). The one piece of shared
mutable state that is *not* created here is the WebSocket
``ConnectionManager``: it is a module-level singleton in
``nexus_server.api.routes.ws`` and is handed to the
:class:`~nexus_server.runner.JobRunner` below, so the runner and the socket
handler agree on which agents are reachable.

Control flow for a job: HTTP ``POST /api/jobs`` → ``routes/jobs.py`` →
``app.state.runner.submit_job`` → :class:`JobRunner` picks a node and sends an
``execute_step`` frame over the agent WebSocket → the agent replies
``step.completed`` / ``step.failed`` → ``routes/ws.py`` notifies the runner →
the runner advances the job.

AI Note: importing this module has side effects — it imports ``nexus_steps``
(populating the global step registry) and calls ``create_app()`` at module
scope, which reads the environment via ``Settings.from_env()`` and therefore
raises if ``JWT_SECRET`` is unset. Tests must set that env var *before*
importing anything that reaches this module (``tests/conftest.py`` does).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nexus_server.config import Settings
from nexus_server.db import ops
from nexus_server.db.models import Base
from nexus_server.db.session import get_engine, get_session_factory, init_db
from nexus_server.runner import JobRunner, resume_active_jobs
from nexus_server.services.auth_service import AuthService
from nexus_server.services.credentials.manager import CredentialManager
from nexus_server.services.storage.manager import StorageManager

from nexus_server.api.routes import auth, credentials, jobs, nodes, pools, steps, storage, ws
from nexus_server.api.routes import artifacts

# AI Note: side-effecting import. Each ``nexus_steps`` submodule applies the
# ``@register`` decorator to its FlowStep subclass, filling
# ``nexus_common.steps.registry.STEP_REGISTRY``. Without this import
# ``GET /api/steps`` returns an empty list and every job submission fails
# validation with "unknown step type". The ``noqa: F401`` keeps linters from
# "helpfully" deleting it.
import nexus_steps  # noqa: F401 — triggers @register decorators, populates STEP_REGISTRY

logger = logging.getLogger("nexus")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle.

    Runs once per process, before the first request is served (everything above
    the ``yield``) and once during graceful shutdown (below it). Responsible for
    creating every long-lived singleton and attaching it to ``app.state``, which
    is how route handlers reach them.

    Startup performs, in order:

    1. Initialize the async engine/session factory from ``settings.database_url``
       and issue ``CREATE TABLE IF NOT EXISTS`` for every ORM model.
    2. Construct :class:`AuthService`, :class:`CredentialManager` and
       :class:`StorageManager` (the storage manager needs the credential manager
       to decrypt backend secrets).
    3. Construct the :class:`JobRunner`, wired to the WebSocket connection
       manager.
    4. Load active storage backends from the DB (best effort).
    5. Seed a default ``admin`` user if the users table has none.
    6. Resume jobs left in an active state by a previous crash/restart.

    Args:
        app: The FastAPI application. ``app.state.settings`` must already be
            populated — :func:`create_app` does this before handing the app to
            uvicorn.

    Yields:
        Control back to the ASGI server for the lifetime of the process.

    Side effects:
        Creates/migrates database tables, may INSERT a default admin user,
        opens connections to configured storage backends, and can spawn
        background job-execution tasks via ``resume_active_jobs``.

    AI Note: ordering matters here. ``init_db`` must precede any ``ops.*`` call
    (they use the module-level session factory), the credential manager must
    exist before the storage manager (backend secrets are decrypted through it),
    and the runner must exist before ``resume_active_jobs`` can hand jobs to it.

    AI Note: tests bypass this function entirely — ``tests/conftest.py`` swaps in
    a no-op lifespan and populates ``app.state`` itself — specifically to avoid
    the on-disk DB, the admin seed, and job resumption. If you add a new
    singleton here, add it to that fixture too or the tests will see a missing
    ``app.state`` attribute.
    """
    settings: Settings = app.state.settings

    # ── Database ──
    init_db(settings.database_url)
    async with get_engine().begin() as conn:
        # AI Note: schema is created directly from the ORM metadata rather than
        # by running Alembic. ``create_all`` only ever ADDS missing tables — it
        # will not alter an existing table — so a column added to db/models.py
        # silently does nothing against an already-created nexus.db. Column
        # changes require a real migration or a wiped database.
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")

    # ── Services ──
    auth_service = AuthService(
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        access_expire_minutes=settings.jwt_access_expire_minutes,
        refresh_expire_days=settings.jwt_refresh_expire_days,
    )
    app.state.auth_service = auth_service

    credential_manager = CredentialManager(encryption_key=settings.credential_encryption_key)
    app.state.credential_manager = credential_manager

    storage_manager = StorageManager(credential_manager=credential_manager)
    app.state.storage_manager = storage_manager

    # ── Job Runner ──
    # The runner dispatches steps to agents over WebSocket and is notified
    # of step.completed / step.failed by the WS handler in routes/ws.py.
    # AI Note: ``ws.manager`` is the module-level singleton ConnectionManager,
    # passed by reference on purpose. Both sides must share one instance —
    # constructing a second manager would leave the runner dispatching into a
    # connection table that no live socket ever writes back to, and every remote
    # step would block until its timeout.
    runner = JobRunner(ws_manager=ws.manager, credential_manager=credential_manager)
    app.state.runner = runner

    # Initialize storage backends from DB (best-effort; backends may not exist yet)
    session_factory = get_session_factory()
    async with session_factory() as db:
        try:
            # AI Note: swallowing the exception is deliberate — a misconfigured
            # or unreachable MinIO/NAS backend must not prevent the control
            # plane from booting. Artifact upload will fail later with a clearer,
            # per-request error instead.
            await storage_manager.init_backends(db)
        except Exception as exc:
            logger.warning("Storage backend init deferred: %s", exc)

        # ── Default admin user ──
        admin = await ops.get_user_by_username(db, "admin")
        if admin is None:
            # AI Note: bootstrap credential. The password falls back to the
            # literal "admin" when NEXUS_ADMIN_PASSWORD is unset, which is a
            # known first-run convenience, not an oversight — the log line below
            # tells the operator to change it. Any deployment reachable off
            # localhost must set the env var. This branch only runs when no user
            # named "admin" exists, so it is idempotent across restarts and will
            # never overwrite a rotated password.
            import os
            admin_pass = os.getenv("NEXUS_ADMIN_PASSWORD", "admin")
            password_hash = AuthService.hash_password(admin_pass)
            await ops.create_user(db, username="admin", password_hash=password_hash, role="admin")
            logger.info(
                "Default admin user 'admin' created. Set NEXUS_ADMIN_PASSWORD to choose the "
                "password; change it after first login."
            )

        # ── Resume active jobs interrupted by the prior shutdown ──
        try:
            # AI Note: crash recovery. Jobs still marked queued/running in the DB
            # are re-submitted to the runner, which restarts them from
            # ``job.current_step`` — already-completed steps are not re-run, but a
            # step that was in flight when the process died *is* re-executed, so
            # steps should be idempotent. Failures are logged rather than raised:
            # one un-resumable job must not block server startup.
            resumed = await resume_active_jobs(db, runner)
            if resumed:
                logger.info("Resumed %d active job(s) on startup", resumed)
        except Exception as exc:
            logger.warning("Job resume on startup failed: %s", exc)

    logger.info("Nexus server started")
    yield
    # AI Note: there is no teardown below the yield beyond this log line —
    # in-flight ``JobRunner`` asyncio tasks are not awaited or cancelled, and
    # storage backend clients are not closed. Recovery relies on
    # ``resume_active_jobs`` at the next startup rather than on a clean drain.
    logger.info("Nexus server shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factory function to build the FastAPI application.

    Assembles the app with CORS middleware and all routers, and records the
    settings on ``app.state`` so :func:`lifespan` (and every request handler) can
    reach them. No I/O happens here — the database and services are only touched
    once the ASGI server runs the lifespan.

    Args:
        settings: Configuration to use. When ``None``, one is read from the
            environment via :meth:`Settings.from_env`, which raises if
            ``JWT_SECRET`` is unset. Tests pass an explicit instance to stay
            hermetic.

    Returns:
        A configured :class:`FastAPI` app, not yet started.

    AI Note: ``ws.router`` is the one router mounted without a prefix — its
    paths (``/ws/agent``, ``/ws/dashboard``) are absolute and must stay outside
    ``/api`` for the frontend's WebSocket client and the agent's connection URL.

    AI Note: tests reuse this factory rather than hand-building an app so that
    middleware and routing match production exactly; only ``app.state`` and the
    lifespan are substituted.
    """
    if settings is None:
        settings = Settings.from_env()

    app = FastAPI(
        title="Nexus",
        description="Cross-platform compute orchestration server",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # ── CORS ──
    # AI Note: the ``or ["*"]`` fallback allows every origin. Combined with
    # ``allow_credentials=True`` below, Starlette echoes the request's Origin
    # back on credentialed requests, so this is genuinely permissive rather than
    # self-disabling — do not rely on it as a safe default. In practice
    # ``Settings.from_env`` always supplies a non-empty list, so this branch is
    # only reachable when Settings is constructed directly.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ──
    # One router per resource; the ``prefix``/``tags`` pairs below are the public
    # API contract consumed by frontend/src/api/client.ts and by the CLI, so
    # renaming a prefix is a breaking change for both.
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(nodes.router, prefix="/api/nodes", tags=["nodes"])
    app.include_router(pools.router, prefix="/api/pools", tags=["pools"])
    app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
    app.include_router(steps.router, prefix="/api/steps", tags=["steps"])
    app.include_router(credentials.router, prefix="/api/credentials", tags=["credentials"])
    app.include_router(storage.router, prefix="/api/storage", tags=["storage"])
    app.include_router(artifacts.router, prefix="/api/artifacts", tags=["artifacts"])
    app.include_router(ws.router, tags=["websocket"])

    return app


# Module-level app instance for uvicorn (uvicorn nexus_server.main:app)
# AI Note: constructed at import time, so merely importing ``nexus_server.main``
# calls ``Settings.from_env()`` and can raise ``KeyError: 'JWT_SECRET'``. This is
# what ``Dockerfile.api`` and ``dev.sh`` load by string reference; tests import
# ``create_app`` instead and build their own instance, but the import still
# evaluates this line — hence the env bootstrap at the top of
# ``tests/conftest.py``.
app = create_app()


def main() -> None:
    """CLI entry point.

    Runs the module-level ``app`` under uvicorn. Passing the app as an import
    string (rather than the object) is required for ``reload=True`` to work,
    since the reloader re-imports the module in a fresh subprocess.

    AI Note: host, port and ``log_level`` are hard-coded literals here and
    deliberately ignore ``settings.host`` / ``settings.port``. ``reload=True``
    also makes this a development-only path — production uses the uvicorn
    command line in ``Dockerfile.api``, which sets its own host/port.
    """
    uvicorn.run("nexus_server.main:app", host="0.0.0.0", port=8000, log_level="info", reload=True)


if __name__ == "__main__":
    main()
