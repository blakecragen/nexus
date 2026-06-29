"""Nexus server — FastAPI application entry point."""

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

import nexus_steps  # noqa: F401 — triggers @register decorators, populates STEP_REGISTRY

logger = logging.getLogger("nexus")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    settings: Settings = app.state.settings

    # ── Database ──
    init_db(settings.database_url)
    async with get_engine().begin() as conn:
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
    runner = JobRunner(ws_manager=ws.manager, credential_manager=credential_manager)
    app.state.runner = runner

    # Initialize storage backends from DB (best-effort; backends may not exist yet)
    session_factory = get_session_factory()
    async with session_factory() as db:
        try:
            await storage_manager.init_backends(db)
        except Exception as exc:
            logger.warning("Storage backend init deferred: %s", exc)

        # ── Default admin user ──
        admin = await ops.get_user_by_username(db, "admin")
        if admin is None:
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
            resumed = await resume_active_jobs(db, runner)
            if resumed:
                logger.info("Resumed %d active job(s) on startup", resumed)
        except Exception as exc:
            logger.warning("Job resume on startup failed: %s", exc)

    logger.info("Nexus server started")
    yield
    logger.info("Nexus server shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factory function to build the FastAPI application."""
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ──
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
app = create_app()


def main() -> None:
    """CLI entry point."""
    uvicorn.run("nexus_server.main:app", host="0.0.0.0", port=8000, log_level="info", reload=True)


if __name__ == "__main__":
    main()
