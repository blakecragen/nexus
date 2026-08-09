"""Server configuration from environment variables.

Defines :class:`Settings`, the single frozen configuration object for the Nexus
control plane. It is built exactly once — in
:func:`nexus_server.main.create_app`, via :meth:`Settings.from_env` — and then
stashed on ``app.state.settings`` so route handlers and the startup ``lifespan``
read the same immutable snapshot for the life of the process.

Who consumes what:

* ``database_url`` → :func:`nexus_server.db.session.init_db` (async SQLAlchemy
  engine + session factory).
* ``jwt_*`` → :class:`nexus_server.services.auth_service.AuthService` (token
  signing/verification).
* ``credential_encryption_key`` →
  :class:`nexus_server.services.credentials.manager.CredentialManager`
  (Fernet field encryption for stored secrets).
* ``cors_origins`` → the ``CORSMiddleware`` installed by ``create_app``.

Tests construct :class:`Settings` directly rather than going through the
environment (see the ``settings`` fixture in ``tests/conftest.py``), which is
why every field is a plain constructor argument with a sane default where one is
possible.

AI Note: this module deliberately uses a stdlib ``dataclass`` rather than
pydantic ``BaseSettings``. It must stay dependency-light and import-safe: it is
imported early by ``conftest.py`` and by the CLI, and the only environment
access happens inside :meth:`Settings.from_env`, never at import time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of server configuration.

    Frozen so that nothing can mutate configuration mid-flight: services capture
    these values at startup (engine URL, JWT secret, encryption key) and would
    not observe a later change anyway, so mutation would only create confusing
    drift between ``app.state.settings`` and live behaviour.

    Attributes:
        database_url: SQLAlchemy async URL handed to
            :func:`nexus_server.db.session.init_db`. Must use an async driver
            (``sqlite+aiosqlite://``, ``postgresql+asyncpg://``); a sync URL
            fails at engine creation. Defaults to a file-backed SQLite DB, which
            is the supported single-node deployment.
        redis_url: Connection URL for the Redis instance from
            ``docker-compose.yml``. AI Note: currently plumbed through config
            only — no server code reads it yet. It is reserved for future
            pub/sub fan-out of dashboard events across multiple API processes,
            which the in-process ``ConnectionManager`` cannot do today.
        minio_endpoint: ``host:port`` of the default MinIO/S3 artifact store.
            AI Note: also currently unread by the server — live storage backends
            are configured per-row in the ``storage_backends`` table and
            instantiated by :class:`~nexus_server.services.storage.manager.StorageManager`,
            so these three ``minio_*`` fields act as deployment documentation /
            bootstrap defaults rather than runtime inputs.
        minio_access_key: Access key for ``minio_endpoint``.
        minio_secret_key: Secret key for ``minio_endpoint``. Never log this.
        jwt_secret: HMAC signing secret for access and refresh tokens. Security
            critical: rotating it invalidates every issued token, and sharing it
            across environments lets tokens from one cluster authenticate
            against another. It has no default on purpose — see
            :meth:`from_env`.
        jwt_algorithm: JWT signing algorithm. Must be a symmetric HMAC variant
            because ``jwt_secret`` is a shared secret, not a keypair; switching
            to ``RS256``/``none`` here without changing ``AuthService`` would
            break verification or disable it entirely.
        jwt_access_expire_minutes: Lifetime of short-lived access tokens.
            Lowering it increases refresh traffic from the dashboard; raising it
            widens the window in which a leaked token stays usable, since there
            is no token revocation list.
        jwt_refresh_expire_days: Lifetime of refresh tokens — the effective
            "stay logged in" duration.
        cors_origins: Exact browser origins allowed to call the API. ``None`` or
            an empty list is turned into ``["*"]`` by ``create_app``, which
            allows every origin — set real origins in any deployment where the
            dashboard is not same-origin.
        credential_encryption_key: url-safe base64 32-byte Fernet key used to
            encrypt credential secrets at rest. Security critical, and *not*
            interchangeable with ``jwt_secret``. Changing it makes every
            previously stored credential permanently undecryptable — there is no
            re-wrap migration. AI Note: the empty-string default is *not* a
            working "credentials disabled" mode — ``Fernet("")`` raises
            ``ValueError``, and :func:`nexus_server.main.lifespan` builds the
            :class:`CredentialManager` unconditionally, so leaving this unset
            aborts startup rather than degrading gracefully.
        host: Bind address advertised for the API. AI Note: informational only —
            :func:`nexus_server.main.main` and the Docker/dev entrypoints pass
            ``0.0.0.0``/``8000`` to uvicorn as literals and never read these two
            fields, so changing ``HOST``/``PORT`` alone will not move the
            listening socket.
        port: TCP port advertised for the API. See the note on ``host``.
        heartbeat_timeout_seconds: How long an agent may go without a heartbeat
            before it is considered offline. AI Note: not wired into
            ``from_env`` (no env override) and not currently read by the
            heartbeat/prune path — treat as a default awaiting the pruning task.
        heartbeat_prune_interval_seconds: How often the offline sweep would run.
            Same caveat as ``heartbeat_timeout_seconds``. Must stay well below
            it, or nodes stay marked online for up to a full interval past the
            timeout.
    """

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
        """Build a :class:`Settings` from process environment variables.

        Called once by :func:`nexus_server.main.create_app` when no explicit
        settings object is supplied. Reads the environment at call time, so
        anything that must be set (``.env`` loading, container env, the test
        bootstrap in ``tests/conftest.py``) has to happen before the app is
        constructed.

        Environment variables consumed, with defaults:

        =========================== ==================================
        ``DATABASE_URL``            ``sqlite+aiosqlite:///nexus.db``
        ``REDIS_URL``               ``redis://localhost:6379``
        ``MINIO_ENDPOINT``          ``localhost:9000``
        ``MINIO_ACCESS_KEY``        ``nexus``
        ``MINIO_SECRET_KEY``        ``changeme_minio``
        ``JWT_SECRET``              *(required — no default)*
        ``CORS_ORIGINS``            ``http://localhost:3000``
        ``CREDENTIAL_ENCRYPTION_KEY`` ``""``
        ``HOST``                    ``0.0.0.0``
        ``PORT``                    ``8000``
        =========================== ==================================

        Returns:
            A frozen :class:`Settings` instance.

        Raises:
            KeyError: if ``JWT_SECRET`` is unset. AI Note: this is deliberate —
                ``os.environ[...]`` rather than ``os.getenv(...)``. A default
                signing secret would silently ship a forgeable-token
                installation, so the server refuses to start instead. Because
                ``nexus_server.main`` calls ``create_app()`` at import time, the
                failure appears as an ``ImportError``-time crash, not a clean
                startup message.
            ValueError: if ``PORT`` is not an integer.

        AI Note: ``CORS_ORIGINS`` is a comma-separated list, split and stripped
        here. It always yields at least one element — a variable that is set but
        empty produces ``[""]``, a falsy-looking but non-empty list, so the
        ``or ["*"]`` fallback in ``create_app`` does *not* fire and the effective
        allow-list becomes the single empty origin (matching nothing). Pass a
        real origin list, or omit the variable entirely to get the localhost
        default.

        AI Note: unlike ``JWT_SECRET``, ``CREDENTIAL_ENCRYPTION_KEY`` has a
        default (the empty string) — but that default is not usable: the
        credential manager constructed during ``lifespan`` calls ``Fernet("")``
        and raises ``ValueError``. Any deployment must supply a real Fernet key
        (``FieldEncryptor.generate_key()``); the tests set one in
        ``tests/conftest.py``.

        AI Note: ``jwt_algorithm``, the two ``jwt_*_expire_*`` fields and both
        ``heartbeat_*`` fields have no environment overrides — they can only be
        changed by constructing :class:`Settings` directly (as the tests do) or
        by editing the dataclass defaults.
        """
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
