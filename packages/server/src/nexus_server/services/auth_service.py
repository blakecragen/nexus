"""JWT authentication and RBAC enforcement.

Single home for every password- and token-related primitive on the server:
bcrypt hashing/verification, JWT mint/decode, and the two flows that combine
them (username+password login, refresh-token exchange).

How it fits together
--------------------
- ``nexus_server.main.lifespan`` constructs exactly one ``AuthService`` from
  ``Settings`` (jwt secret/algorithm/expiries) and parks it on
  ``app.state.auth_service``; it also calls :meth:`AuthService.hash_password`
  directly when seeding the bootstrap ``admin`` user.
- ``nexus_server.api.deps`` exposes it as the ``Auth`` dependency and implements
  ``get_current_user`` / ``require_admin`` on top of :meth:`decode_token`.
  NOTE: ``deps.get_current_user`` re-implements token decoding so it can raise
  HTTP 401s; :meth:`AuthService.get_current_user` here is the non-HTTP variant
  used outside request scope. Keep the two in sync — both must reject tokens
  whose ``type`` claim is not ``"access"``.
- ``nexus_server.api.routes.auth`` drives :meth:`authenticate` (POST /login),
  :meth:`refresh` (POST /refresh), and :meth:`hash_password` (POST /register).
- User rows are read/written through ``nexus_server.db.ops``; this module never
  touches SQLAlchemy models directly beyond attribute access.

Security invariants worth preserving
------------------------------------
- Access and refresh tokens are signed with the *same* secret and are only
  distinguished by the ``type`` claim, so every consumer MUST check ``type``
  before trusting a token; otherwise a refresh token would be accepted as an
  access token (and vice versa).
- The ``role`` claim is embedded in the access token at mint time, so a role
  change does not take effect until the current access token expires. Anything
  that must react immediately has to re-read the user row from the DB.
- Nothing here revokes tokens: there is no denylist or jti. Deactivating a user
  (``is_active = False``) blocks login/refresh/lookup but does *not* invalidate
  an already-issued, unexpired access token.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import bcrypt
import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_server.db import ops


class AuthService:
    """Handles user authentication, JWT token management, and RBAC.

    Stateless apart from its configuration (secret, algorithm, expiry windows),
    so a single instance is shared by every request. All DB access is passed in
    as an ``AsyncSession`` argument rather than held on the object.
    """

    def __init__(self, secret: str, algorithm: str = "HS256",
                 access_expire_minutes: int = 60, refresh_expire_days: int = 7):
        """Capture signing configuration for all tokens this service issues.

        Args:
            secret: HMAC signing key (``Settings.jwt_secret``). Shared by access
                and refresh tokens; rotating it invalidates every outstanding
                token, which is the only available "log everyone out" lever.
            algorithm: PyJWT algorithm name. Only symmetric HS* algorithms work
                with a plain string secret; switching to RS*/ES* would require
                passing a key object instead.
            access_expire_minutes: Lifetime of access tokens. Short-lived
                because they carry the cached ``role`` claim (see module note).
            refresh_expire_days: Lifetime of refresh tokens, i.e. how long a
                client can stay logged in without re-entering a password.
        """
        self._secret = secret
        self._algorithm = algorithm
        self._access_expire = timedelta(minutes=access_expire_minutes)
        self._refresh_expire = timedelta(days=refresh_expire_days)

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a plaintext password with bcrypt for storage in ``users.password_hash``.

        A fresh random salt is generated per call, so hashing the same password
        twice yields different strings — never compare hashes for equality, use
        :meth:`verify_password`.

        Args:
            password: Plaintext password. Bcrypt silently truncates the input at
                72 bytes, so longer passphrases gain no additional entropy.

        Returns:
            The bcrypt hash (salt included) as an ASCII ``str``, ready to store.
        """
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """Constant-time check of a plaintext password against a stored bcrypt hash.

        Args:
            password: Plaintext candidate supplied by the client.
            password_hash: Value previously produced by :meth:`hash_password`.

        Returns:
            True when the password matches.

        Raises:
            ValueError: If ``password_hash`` is not a valid bcrypt string (e.g.
                a legacy/corrupted row). Callers such as :meth:`authenticate`
                do not catch this, so it surfaces as a 500 rather than a 401 —
                deliberate, since a malformed hash is a data bug, not a failed
                login attempt.
        """
        return bcrypt.checkpw(password.encode(), password_hash.encode())

    def create_access_token(self, user_id: str, role: str) -> str:
        """Mint a short-lived access token carrying the user's id and role.

        Args:
            user_id: Stringified user UUID; becomes the ``sub`` claim. Must be a
                ``str`` (the DB stores ids as ``String(36)``) — a raw ``UUID``
                is not JSON-serialisable and would make ``jwt.encode`` fail.
            role: RBAC role ("admin" / "user") snapshotted into the token.

        Returns:
            Encoded, signed JWT.
        """
        payload = {
            "sub": user_id,
            "role": role,
            "type": "access",
            # AI Note: `exp`/`iat` are tz-aware UTC datetimes; PyJWT converts
            # them to numeric epoch claims. Using naive datetimes here would be
            # interpreted as local time and silently shift expiry by the host's
            # UTC offset (the same class of bug fixed elsewhere in this repo).
            "exp": datetime.now(timezone.utc) + self._access_expire,
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def create_refresh_token(self, user_id: str) -> str:
        """Mint a long-lived refresh token that can only be exchanged, not used
        for authorization.

        Deliberately omits the ``role`` claim: refresh tokens must never be
        accepted by authorization checks, and the role is re-read from the DB on
        every :meth:`refresh` so privilege changes propagate at refresh time.

        Args:
            user_id: Stringified user UUID; becomes the ``sub`` claim.

        Returns:
            Encoded, signed JWT with ``type == "refresh"``.
        """
        payload = {
            "sub": user_id,
            "type": "refresh",
            "exp": datetime.now(timezone.utc) + self._refresh_expire,
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def decode_token(self, token: str) -> dict:
        """Decode and validate a JWT token. Raises jwt.PyJWTError on failure.

        Verifies the signature and the ``exp`` claim (PyJWT does this by
        default) but NOT the ``type`` claim — every caller is responsible for
        asserting ``payload["type"]`` matches the context it is being used in.

        Args:
            token: Raw compact JWT string (no "Bearer " prefix).

        Returns:
            The decoded claims dict.

        Raises:
            jwt.PyJWTError: Bad signature, wrong algorithm, malformed token, or
                expired ``exp`` (``jwt.ExpiredSignatureError`` is a subclass).
        """
        return jwt.decode(token, self._secret, algorithms=[self._algorithm])

    async def authenticate(self, db: AsyncSession, username: str, password: str) -> dict | None:
        """Verify credentials and return token pair, or None on failure.

        Side effects: on success, stamps ``users.last_login_at`` (a DB write and
        commit) before returning.

        Args:
            db: Active async session; the caller owns the transaction scope.
            username: Login name, matched exactly (case-sensitive).
            password: Plaintext password to check against the stored hash.

        Returns:
            ``{"access_token", "refresh_token", "token_type"}`` on success, or
            ``None`` for unknown user / deactivated user / wrong password. The
            failure modes are intentionally indistinguishable to the caller so
            the API cannot be used to enumerate valid usernames.
        """
        user = await ops.get_user_by_username(db, username)
        if not user or not user.is_active:
            return None
        if not self.verify_password(password, user.password_hash):
            return None

        await ops.update_user(db, user.id, last_login_at=datetime.now(timezone.utc))

        return {
            "access_token": self.create_access_token(str(user.id), user.role),
            "refresh_token": self.create_refresh_token(str(user.id)),
            "token_type": "bearer",
        }

    async def refresh(self, db: AsyncSession, refresh_token: str) -> dict | None:
        """Generate new access token from a valid refresh token.

        Re-reads the user row so a deactivated account (or a changed role)
        is honoured at refresh time rather than at the next full login.

        Args:
            db: Active async session.
            refresh_token: The refresh JWT previously handed to the client.

        Returns:
            A brand-new token pair, or ``None`` if the token is invalid/expired,
            is not of ``type == "refresh"``, or the user no longer exists or is
            inactive.

        Note:
            A *new* refresh token is issued alongside the access token (sliding
            expiry), but the old one keeps working until its own ``exp`` — there
            is no rotation/reuse detection.
        """
        try:
            payload = self.decode_token(refresh_token)
            # AI Note: Security-critical. Without this check an access token
            # would be a valid refresh token, letting a leaked short-lived token
            # be traded up for a fresh long-lived pair indefinitely.
            if payload.get("type") != "refresh":
                return None
        except jwt.PyJWTError:
            return None

        # AI Note: `sub` is a stringified UUID; UUID(...) both parses it and
        # rejects garbage. A malformed `sub` raises ValueError here rather than
        # returning None — callers in routes/auth.py currently let that surface.
        user = await ops.get_user_by_id(db, UUID(payload["sub"]))
        if not user or not user.is_active:
            return None

        return {
            "access_token": self.create_access_token(str(user.id), user.role),
            "refresh_token": self.create_refresh_token(str(user.id)),
            "token_type": "bearer",
        }

    async def get_current_user(self, db: AsyncSession, token: str):
        """Extract user from access token. Returns User model or None.

        Non-HTTP counterpart of ``nexus_server.api.deps.get_current_user`` — use
        this from WebSocket handlers, background tasks, or anywhere raising
        ``HTTPException`` would be wrong.

        Args:
            db: Active async session.
            token: Raw access JWT (no "Bearer " prefix).

        Returns:
            The ``User`` ORM object, or ``None`` if the token is invalid,
            expired, not an access token, or points at a deleted user.

        Note:
            Unlike the ``deps`` version this does NOT check ``is_active``, so a
            deactivated user still resolves as long as the row exists. Callers
            that care about deactivation must check ``user.is_active``
            themselves.
        """
        try:
            payload = self.decode_token(token)
            if payload.get("type") != "access":
                return None
        except jwt.PyJWTError:
            return None

        return await ops.get_user_by_id(db, UUID(payload["sub"]))
