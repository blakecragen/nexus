"""Authentication routes — login, refresh, register, current user."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from pydantic import BaseModel

from nexus_common.models.schemas import LoginRequest, TokenResponse, UserInfo
from nexus_server.api.deps import AdminUser, Auth, CurrentUser, DbSession
from nexus_server.db import ops
from nexus_server.services.auth_service import AuthService

router = APIRouter()


class RefreshRequest(BaseModel):
    refresh_token: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str | None = None
    role: str = "user"


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, auth: Auth, db: DbSession):
    """Authenticate with username/password and receive JWT tokens."""
    result = await auth.authenticate(db, body.username, body.password)
    if result is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return result


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, auth: Auth, db: DbSession):
    """Exchange a valid refresh token for a new token pair."""
    result = await auth.refresh(db, body.refresh_token)
    if result is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    return result


@router.post("/register", response_model=UserInfo, status_code=status.HTTP_201_CREATED)
async def register_user(body: RegisterRequest, auth: Auth, db: DbSession, admin: AdminUser):
    """Create a new user (admin only)."""
    existing = await ops.get_user_by_username(db, body.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    password_hash = AuthService.hash_password(body.password)
    user = await ops.create_user(
        db, username=body.username, password_hash=password_hash,
        email=body.email, role=body.role,
    )
    return UserInfo(
        id=user.id, username=user.username, email=user.email,
        role=user.role, is_active=user.is_active,
    )


@router.get("/me", response_model=UserInfo)
async def get_me(user: CurrentUser):
    """Return the current authenticated user's info."""
    return UserInfo(
        id=user.id, username=user.username, email=user.email,
        role=user.role, is_active=user.is_active,
    )
