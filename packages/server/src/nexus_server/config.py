"""Server configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_access_expire_minutes: int = 60
    jwt_refresh_expire_days: int = 7
    cors_origins: list[str] | None = None
    credential_encryption_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    heartbeat_timeout_seconds: int = 30
    heartbeat_prune_interval_seconds: int = 10

    @classmethod
    def from_env(cls) -> Settings:
        cors = os.getenv("CORS_ORIGINS", "http://localhost:3000")
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///nexus.db"),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379"),
            minio_endpoint=os.getenv("MINIO_ENDPOINT", "localhost:9000"),
            minio_access_key=os.getenv("MINIO_ACCESS_KEY", "nexus"),
            minio_secret_key=os.getenv("MINIO_SECRET_KEY", "changeme_minio"),
            jwt_secret=os.environ["JWT_SECRET"],
            cors_origins=[o.strip() for o in cors.split(",")],
            credential_encryption_key=os.getenv("CREDENTIAL_ENCRYPTION_KEY", ""),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )
