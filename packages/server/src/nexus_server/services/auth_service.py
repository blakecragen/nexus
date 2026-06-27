"""JWT authentication and RBAC enforcement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import bcrypt
import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_server.db import ops


class AuthService:
    """Handles user authentication, JWT token management, and RBAC."""

    def __init__(self, secret: str, algorithm: str = "HS256",
                 access_expire_minutes: int = 60, refresh_expire_days: int = 7):
        self._secret = secret
        self._algorithm = algorithm
        self._access_expire = timedelta(minutes=access_expire_minutes)
        self._refresh_expire = timedelta(days=refresh_expire_days)

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        return bcrypt.checkpw(password.encode(), password_hash.encode())

    def create_access_token(self, user_id: str, role: str) -> str:
        payload = {
            "sub": user_id,
            "role": role,
            "type": "access",
            "exp": datetime.now(timezone.utc) + self._access_expire,
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def create_refresh_token(self, user_id: str) -> str:
        payload = {
            "sub": user_id,
            "type": "refresh",
            "exp": datetime.now(timezone.utc) + self._refresh_expire,
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def decode_token(self, token: str) -> dict:
        """Decode and validate a JWT token. Raises jwt.PyJWTError on failure."""
        return jwt.decode(token, self._secret, algorithms=[self._algorithm])

    async def authenticate(self, db: AsyncSession, username: str, password: str) -> dict | None:
        """Verify credentials and return token pair, or None on failure."""
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
        """Generate new access token from a valid refresh token."""
        try:
            payload = self.decode_token(refresh_token)
            if payload.get("type") != "refresh":
                return None
        except jwt.PyJWTError:
            return None

        user = await ops.get_user_by_id(db, UUID(payload["sub"]))
        if not user or not user.is_active:
            return None

        return {
            "access_token": self.create_access_token(str(user.id), user.role),
            "refresh_token": self.create_refresh_token(str(user.id)),
            "token_type": "bearer",
        }

    async def get_current_user(self, db: AsyncSession, token: str):
        """Extract user from access token. Returns User model or None."""
        try:
            payload = self.decode_token(token)
            if payload.get("type") != "access":
                return None
        except jwt.PyJWTError:
            return None

        return await ops.get_user_by_id(db, UUID(payload["sub"]))
