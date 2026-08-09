"""Authentication routes — login, refresh, register, current user.

Mounted at ``/api/auth`` by ``nexus_server.main.create_app()``. This is the only
router that is reachable without a bearer token: ``/login`` and ``/refresh`` are
how a client *obtains* one. Every other endpoint in the API (and this module's
own ``/register`` and ``/me``) resolves ``CurrentUser``/``AdminUser`` from
``nexus_server.api.deps``, which rejects the request with 401/403 before the
handler body runs.

Division of labour:
    * This module — HTTP shape only: request bodies, status codes, uniqueness
      checks, ORM-row → ``UserInfo`` projection.
    * ``nexus_server.services.auth_service.AuthService`` — all the actual
      security work: bcrypt hashing/verification, JWT signing and decoding,
      access/refresh expiry windows, ``last_login_at`` bookkeeping. Injected as
      the ``Auth`` dependency, constructed once at startup from
      ``nexus_server.config`` settings.
    * ``nexus_server.db.ops`` — user reads and writes.

Token model (see ``AuthService``): ``/login`` and ``/refresh`` both return a
*pair* of tokens. The access token carries ``{sub, role, type: "access"}`` and is
short-lived (settings-driven, 60 min by default); the refresh token carries
``{sub, type: "refresh"}`` and lives for days. ``deps.get_current_user``
explicitly rejects a refresh token presented as a bearer credential by checking
the ``type`` claim, so the two are not interchangeable.

Frontend counterpart: ``frontend/src/api/client.ts`` (token storage) and
``frontend/src/stores/index.ts`` (``useAuthStore``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from pydantic import BaseModel

from nexus_common.models.schemas import LoginRequest, TokenResponse, UserInfo
from nexus_server.api.deps import AdminUser, Auth, CurrentUser, DbSession
from nexus_server.db import ops
from nexus_server.services.auth_service import AuthService

router = APIRouter()


class RefreshRequest(BaseModel):
    """Body of ``POST /api/auth/refresh``.

    Attributes:
        refresh_token: The refresh JWT previously handed out by ``/login`` or a
            prior ``/refresh``. It travels in the JSON body rather than the
            ``Authorization`` header on purpose — the header slot is reserved
            for access tokens, and ``deps.get_current_user`` would reject a
            refresh token there because of its ``type`` claim.
    """

    refresh_token: str


class RegisterRequest(BaseModel):
    """Body of ``POST /api/auth/register`` (admin-only user creation).

    Attributes:
        username: Login name. Must be unique; the handler checks and returns 409.
        password: Plaintext password, hashed with bcrypt before it is stored.
            Never persisted or logged in the clear.
        email: Optional contact address; purely informational today.
        role: RBAC role, validated downstream against
            ``nexus_common.models.enums.UserRole`` when the created row is
            serialised into ``UserInfo``. Defaults to the least-privileged
            ``"user"`` so that omitting the field can never mint an admin.
    """

    username: str
    password: str
    email: str | None = None
    role: str = "user"


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, auth: Auth, db: DbSession):
    """Authenticate with username/password and receive JWT tokens.

    Args:
        body: Username and plaintext password.
        auth: ``AuthService`` singleton, injected.
        db: Async session, injected.

    Returns:
        TokenResponse: A fresh access/refresh token pair.

    Raises:
        HTTPException: 401 if the user does not exist, is deactivated, or the
            password does not match.
    """
    # AI Note: AuthService.authenticate() collapses "no such user", "inactive
    # user" and "wrong password" into a single None. Keep the 401 detail generic
    # — distinguishing them here would turn this endpoint into a username oracle.
    result = await auth.authenticate(db, body.username, body.password)
    if result is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return result


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, auth: Auth, db: DbSession):
    """Exchange a valid refresh token for a new token pair.

    Args:
        body: Wrapper carrying the refresh token.
        auth: ``AuthService`` singleton, injected.
        db: Async session, injected.

    Returns:
        TokenResponse: A new access token *and* a new refresh token. Clients
        must replace both; the old refresh token is not revoked but will expire.

    Raises:
        HTTPException: 401 if the token is malformed, expired, signed with a
            different secret, is an access token rather than a refresh token, or
            belongs to a user who has since been deleted or deactivated.
    """
    # AI Note: No refresh-token denylist exists — a leaked refresh token stays
    # usable until its own expiry (settings.refresh_expire_days). The only
    # server-side revocation lever today is deactivating the user, which
    # AuthService.refresh() re-checks on every call.
    result = await auth.refresh(db, body.refresh_token)
    if result is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    return result


@router.post("/register", response_model=UserInfo, status_code=status.HTTP_201_CREATED)
async def register_user(body: RegisterRequest, auth: Auth, db: DbSession, admin: AdminUser):
    """Create a new user (admin only).

    This is not self-service signup: the ``AdminUser`` dependency rejects
    non-admin callers with 403 before the handler runs, so accounts can only be
    minted by an existing admin. The very first admin is seeded at startup by
    ``nexus_server.main``, not through this endpoint.

    Args:
        body: New user's username, password, optional email and role.
        auth: ``AuthService`` singleton, injected. Present only to satisfy the
            dependency graph; hashing below uses the ``AuthService`` static
            method directly.
        db: Async session, injected.
        admin: Enforces the admin role; the value itself is unused.

    Returns:
        UserInfo: The created user, without the password hash.

    Raises:
        HTTPException: 409 if the username is already taken.

    Side effects:
        Inserts and commits a row in the ``users`` table.
    """
    existing = await ops.get_user_by_username(db, body.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    # AI Note: This check-then-insert is not atomic. Two concurrent registrations
    # of the same username can both pass the check; the loser is caught only by
    # the DB's unique constraint on users.username, surfacing as a 500 rather
    # than the 409 above. Rare enough (admin-only endpoint) to be left as-is.
    password_hash = AuthService.hash_password(body.password)
    user = await ops.create_user(
        db, username=body.username, password_hash=password_hash,
        email=body.email, role=body.role,
    )
    # AI Note: Fields are copied explicitly rather than via from_attributes so
    # that user.password_hash can never be serialised into the response.
    return UserInfo(
        id=user.id, username=user.username, email=user.email,
        role=user.role, is_active=user.is_active,
    )


@router.get("/me", response_model=UserInfo)
async def get_me(user: CurrentUser):
    """Return the current authenticated user's info.

    Used by the frontend on boot to validate a stored access token and to learn
    the caller's role (which drives admin-only UI). Takes no database session:
    ``deps.get_current_user`` has already loaded and freshness-checked the row.

    Args:
        user: The authenticated ``User``, injected from the bearer token.

    Returns:
        UserInfo: Identity and role of the caller, without the password hash.

    Raises:
        HTTPException: 401 (raised by the dependency, not here) when the bearer
            token is missing, expired, of the wrong type, or the user is
            inactive.
    """
    return UserInfo(
        id=user.id, username=user.username, email=user.email,
        role=user.role, is_active=user.is_active,
    )
