"""FastAPI dependency injection for authentication, DB sessions, and services.

Role in the system
------------------
This is the single wiring point between FastAPI's request lifecycle and the
long-lived objects the Nexus server owns. Every HTTP route module under
``nexus_server.api.routes`` imports its dependencies from here rather than
reaching into ``request.app.state`` or the DB session factory itself, so
authentication rules and service lookup exist in exactly one place.

Three kinds of dependency live here:

1. **Database** — :func:`get_db` yields a per-request :class:`AsyncSession`
   from ``nexus_server.db.session.get_session``.
2. **Services** — thin accessors that pull singletons off ``app.state``.
   Those singletons (``auth_service``, ``credential_manager``,
   ``storage_manager``, ``runner``) are constructed once during the
   ``lifespan`` startup hook in ``nexus_server.main``. If a dependency here
   raises ``AttributeError``, it means startup never ran (a common symptom in
   tests that build a bare ``FastAPI()`` instead of using ``create_app()``).
3. **Authentication / authorization** — :func:`get_current_user`,
   :func:`require_admin` and :func:`require_pool_access` translate a bearer
   JWT into a :class:`~nexus_server.db.models.User` and enforce role/pool
   rules.

Neighbouring modules
--------------------
- Upstream: ``nexus_server.main.create_app`` mounts the routers and populates
  ``app.state``; ``nexus_server.services.auth_service.AuthService`` issues and
  decodes the tokens validated here.
- Downstream: ``routes/nodes.py``, ``routes/pools.py``, ``routes/jobs.py``,
  ``routes/steps.py``, ``routes/storage.py``, ``routes/credentials.py`` and
  ``routes/artifacts.py`` consume the ``Annotated`` shortcuts at the bottom of
  this file.

AI Note: the agent and dashboard WebSocket endpoints in ``routes/ws.py``
deliberately do NOT use anything from this module — agents authenticate with a
per-node ``api_key`` query parameter instead of a JWT, because the browser/CLI
WebSocket clients cannot set an ``Authorization`` header.
"""

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

# AI Note: HTTPBearer() defaults to auto_error=True, so a missing or malformed
# Authorization header is rejected by FastAPI with 403 *before* get_current_user
# runs. That is why get_current_user only ever sees a syntactically valid token
# and only raises 401 for semantic failures (bad signature, wrong token type,
# unknown/inactive user). Flipping auto_error would change those status codes
# and break clients that branch on 401 vs 403.
_bearer_scheme = HTTPBearer()


# ── Database ──────────────────────────────────────────────────────────────


async def get_db() -> AsyncSession:
    """Yield an async database session scoped to a single request.

    Delegates to :func:`nexus_server.db.session.get_session`, which closes the
    session when the request finishes. Routes should treat the yielded session
    as request-local state: it is never shared across tasks.

    Yields:
        AsyncSession: An open SQLAlchemy async session.

    Raises:
        RuntimeError: If ``init_db()`` has not been called yet (raised by the
            underlying ``get_session``).

    AI Note: this session lives only for the duration of the request. Long
    background work (SSH provisioning, job polling) must open its OWN session
    via ``get_session_factory()`` — see ``routes/nodes.py::_provision_and_poll``
    for why: writes made by other tasks are invisible to an already-loaded
    identity map on this session.
    """
    async for session in get_session():
        yield session


# ── Services (from app.state, populated at startup) ─────────────────────


def get_auth_service(request: Request) -> AuthService:
    """Return the process-wide :class:`AuthService` built during app startup.

    Args:
        request: The incoming request, used only to reach ``app.state``.

    Returns:
        AuthService: Singleton that hashes passwords and encodes/decodes JWTs.

    Raises:
        AttributeError: If the app's ``lifespan`` startup never ran.
    """
    return request.app.state.auth_service


def get_credential_manager(request: Request) -> CredentialManager:
    """Return the process-wide :class:`CredentialManager`.

    The manager holds the Fernet encryption key used to seal/unseal stored
    credentials, so it must be a singleton — constructing a new one with a
    different key would make existing rows undecryptable.

    Args:
        request: The incoming request, used only to reach ``app.state``.

    Returns:
        CredentialManager: Singleton credential encryption/lookup service.

    Raises:
        AttributeError: If the app's ``lifespan`` startup never ran.
    """
    return request.app.state.credential_manager


def get_storage_manager(request: Request) -> StorageManager:
    """Return the process-wide :class:`StorageManager`.

    The manager caches *initialized* backend client instances keyed by backend
    ID (populated by ``init_backends()`` at startup). A backend row that exists
    in the DB but was added after startup will not be in that cache, which is
    why ``routes/storage.py`` treats ``KeyError`` from ``get_backend()`` as
    "not initialized" rather than "not found".

    Args:
        request: The incoming request, used only to reach ``app.state``.

    Returns:
        StorageManager: Singleton artifact upload/download/transfer service.

    Raises:
        AttributeError: If the app's ``lifespan`` startup never ran.
    """
    return request.app.state.storage_manager


def get_runner(request: Request) -> JobRunner:
    """Return the process-wide :class:`JobRunner`.

    The runner owns in-memory job state (``asyncio`` tasks, per-step completion
    events and results), so there must be exactly one per process. Routes use
    it to submit and cancel jobs; ``routes/ws.py`` reaches the same instance
    via ``ws.app.state.runner`` to deliver step completion callbacks.

    Args:
        request: The incoming request, used only to reach ``app.state``.

    Returns:
        JobRunner: Singleton job orchestrator.

    Raises:
        AttributeError: If the app's ``lifespan`` startup never ran.
    """
    return request.app.state.runner


# ── Authentication ────────────────────────────────────────────────────────


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Decode JWT from Authorization header and return the User model.

    This is the primary authentication gate for every non-WebSocket route. It
    performs three checks in order: the signature must verify, the token must
    be an *access* token (not a refresh token), and the referenced user must
    still exist and be active.

    Args:
        credentials: Bearer credentials extracted by ``HTTPBearer``.
        auth: Auth service used to verify the signature and read the claims.
        db: Request-scoped session used to load the user row.

    Returns:
        User: The authenticated, active user.

    Raises:
        HTTPException: 401 if the token is expired/invalid, is a refresh token
            rather than an access token, lacks a ``sub`` claim, or resolves to
            a user that no longer exists or has ``is_active=False``.
    """
    token = credentials.credentials
    try:
        payload = auth.decode_token(token)
        # AI Note: security-sensitive. Refresh tokens are signed with the same
        # secret as access tokens, so without this `type` check a long-lived
        # refresh token would be accepted as an API credential everywhere.
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
        user_id = payload["sub"]  # already a string from JWT
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        # AI Note: all decode failures collapse into one generic 401 message on
        # purpose — distinguishing "expired" from "bad signature" from "unknown
        # user" would leak information to an attacker probing tokens.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        ) from exc

    # AI Note: `user_id` is the raw string from the JWT `sub` claim, not a UUID
    # object. That is intentional — IDs are stored as TEXT in SQLite and
    # `ops.get_user_by_id` normalizes via `_sid()`, so a string works directly.
    user = await ops.get_user_by_id(db, user_id)
    # Deactivating a user takes effect immediately on the next request even
    # though their previously issued token is still cryptographically valid.
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Require the authenticated user to have admin role.

    Layered on top of :func:`get_current_user`, so an unauthenticated caller
    still gets 401 rather than 403. Used by every destructive or
    infrastructure-mutating route (node registration/provisioning/deletion,
    storage backend CRUD, credential management).

    Args:
        user: The already-authenticated user.

    Returns:
        User: The same user, once confirmed to be an admin.

    Raises:
        HTTPException: 403 if ``user.role`` is anything other than ``"admin"``.

    AI Note: role is a plain string column, not an enum — a typo such as
    ``"Admin"`` silently denies access rather than failing loudly.
    """
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def require_pool_access(pool_id: UUID):
    """Return a dependency that checks the current user has access to the given pool.

    This is a *dependency factory*: it is called at import/route-definition
    time with a fixed pool ID and returns the async callable FastAPI will run
    per request. Access is granted if the user is an admin, or if any group
    they belong to has been granted access to the pool (see
    ``ops.check_user_pool_access``).

    Args:
        pool_id: The pool the generated dependency will guard.

    Returns:
        Callable: An async FastAPI dependency returning the authorized user.

    AI Note: because ``pool_id`` is bound when the factory is called, this only
    works for a pool known statically — it cannot read a path parameter. That
    is why no route currently uses it; pool authorization is enforced ad hoc in
    the job routes instead. Keep it in mind before assuming pool ACLs are
    applied uniformly across the API.
    """

    async def _check(
        user: Annotated[User, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        """Per-request pool authorization check.

        Args:
            user: The authenticated user.
            db: Request-scoped session used for the group/pool ACL query.

        Returns:
            User: The user, once pool access is confirmed.

        Raises:
            HTTPException: 403 if the user has no group granting access to the
                captured ``pool_id``.
        """
        has_access = await ops.check_user_pool_access(db, user.id, pool_id)
        if not has_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this pool",
            )
        return user

    return _check


# ── Annotated shortcuts ──────────────────────────────────────────────────
#
# AI Note: these aliases are the public surface of this module — route modules
# import them and annotate handler parameters (e.g. ``db: DbSession``) instead
# of repeating ``Annotated[..., Depends(...)]``. Two consequences worth knowing
# before editing:
#   * Swapping the dependency behind an alias silently changes the auth posture
#     of every route that uses it. Changing ``CurrentUser`` to ``AdminUser``
#     here would lock out non-admins across the whole API.
#   * FastAPI resolves each distinct dependency callable once per request and
#     caches the result, so a handler taking both ``CurrentUser`` and
#     ``AdminUser`` decodes the JWT and loads the user only once.

# Request-scoped SQLAlchemy session.
DbSession = Annotated[AsyncSession, Depends(get_db)]
# Authenticated, active user (401 otherwise).
CurrentUser = Annotated[User, Depends(get_current_user)]
# Authenticated user with role == "admin" (403 otherwise).
AdminUser = Annotated[User, Depends(require_admin)]
# JWT/password service singleton.
Auth = Annotated[AuthService, Depends(get_auth_service)]
# Credential encryption/lookup singleton.
CredMgr = Annotated[CredentialManager, Depends(get_credential_manager)]
# Artifact storage backend singleton.
StorageMgr = Annotated[StorageManager, Depends(get_storage_manager)]
# Job orchestration singleton.
Runner = Annotated[JobRunner, Depends(get_runner)]
