"""FastAPI dependency injection for authentication, DB sessions, and services."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_server.db import ops
from nexus_server.db.models import User
from nexus_server.db.session import get_session
from nexus_server.runner import JobRunner
from nexus_server.services.auth_service import AuthService
from nexus_server.services.credentials.manager import CredentialManager
from nexus_server.services.storage.manager import StorageManager

_bearer_scheme = HTTPBearer()


# ── Database ──────────────────────────────────────────────────────────────


async def get_db() -> AsyncSession:
    """Yield an async database session."""
    async for session in get_session():
        yield session


# ── Services (from app.state, populated at startup) ─────────────────────


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def get_credential_manager(request: Request) -> CredentialManager:
    return request.app.state.credential_manager


def get_storage_manager(request: Request) -> StorageManager:
    return request.app.state.storage_manager


def get_runner(request: Request) -> JobRunner:
    return request.app.state.runner


# ── Authentication ────────────────────────────────────────────────────────


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Decode JWT from Authorization header and return the User model."""
    token = credentials.credentials
    try:
        payload = auth.decode_token(token)
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
        user_id = payload["sub"]  # already a string from JWT
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        ) from exc

    user = await ops.get_user_by_id(db, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Require the authenticated user to have admin role."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def require_pool_access(pool_id: UUID):
    """Return a dependency that checks the current user has access to the given pool."""

    async def _check(
        user: Annotated[User, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        has_access = await ops.check_user_pool_access(db, user.id, pool_id)
        if not has_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this pool",
            )
        return user

    return _check


# ── Annotated shortcuts ──────────────────────────────────────────────────

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
Auth = Annotated[AuthService, Depends(get_auth_service)]
CredMgr = Annotated[CredentialManager, Depends(get_credential_manager)]
StorageMgr = Annotated[StorageManager, Depends(get_storage_manager)]
Runner = Annotated[JobRunner, Depends(get_runner)]
