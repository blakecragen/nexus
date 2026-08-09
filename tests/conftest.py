"""Root pytest configuration and shared fixtures for the Nexus backend test suite.

This module does three jobs:

1. **Make the workspace packages importable.** The editable installs in ``.venv``
   are not reliably on ``sys.path`` (and the box is offline, so we can't
   ``pip install -e`` to repair them). We inject each ``packages/*/src`` directory
   onto ``sys.path`` directly — the same directories the editable ``.pth`` files
   point at.

2. **Provide a hermetic environment.** ``JWT_SECRET`` and a Fernet
   ``CREDENTIAL_ENCRYPTION_KEY`` are required by the server config / credential
   manager; we set deterministic test values before anything imports the app.

3. **Expose reusable fixtures** — an in-memory SQLite engine, a per-test
   ``AsyncSession``, a fully wired FastAPI app with its DB dependency overridden,
   a ``TestClient``, and authenticated client variants for admin and regular
   users. Domain fixtures (users, nodes, pools, jobs, credentials, storage) build
   on these.

Everything here is import-safe and side-effect-free at module load beyond the
path / env bootstrapping, which must happen first.

Fixture dependency graph (what builds on what)::

    engine ──> session_factory ──> db ──> admin_user / regular_user /
      │              │                      sample_pool / sample_node
      │              │
      └──────────────┴──> app ──> client ──> admin_client / auth_client
                            ▲                      ▲
             settings ──────┤        admin_token / user_token
        auth_service ───────┤              ▲
   credential_manager ──────┘        auth_service

Consumers: ``tests/test_smoke.py``, ``tests/unit/**`` (mostly ``settings`` /
``encryptor`` / ``credential_manager`` / ``step_context``) and
``tests/integration/**`` (mostly ``db`` / ``app`` / the client fixtures).

Conventions worth knowing before you add a fixture here:
  * ``pytest_asyncio`` runs in ``asyncio_mode = "auto"`` (see pyproject), so a
    bare ``async def test_...`` is collected without a marker.
  * Async fixtures MUST use ``@pytest_asyncio.fixture``; a plain
    ``@pytest.fixture`` on an async generator yields the generator object.
  * ``admin_client`` and ``auth_client`` mutate the SAME underlying
    ``client`` object. Requesting both in one test makes the last-applied
    Authorization header win — pick one per test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── 1. Make workspace packages importable ────────────────────────────────────
# Must run before any ``nexus_*`` import below.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_SRC_DIRS = [
    _REPO_ROOT / "packages" / "common" / "src",
    _REPO_ROOT / "packages" / "server" / "src",
    _REPO_ROOT / "packages" / "steps" / "src",
    _REPO_ROOT / "packages" / "agent" / "src",
    _REPO_ROOT / "packages" / "cli" / "src",
]
for _src in _PACKAGE_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)

# ── 2. Hermetic environment ──────────────────────────────────────────────────
# A valid 32-byte url-safe base64 Fernet key (deterministic for tests).
# AI Note: ``setdefault`` (not assignment) is deliberate — a developer or CI job
# may export real-ish values to reproduce an environment-specific failure, and
# these defaults must not clobber that. They also have to be in place BEFORE
# ``nexus_server.config`` is imported below, because Settings reads the process
# environment at class-definition/instantiation time.
TEST_FERNET_KEY = "tEsT_KeY_dQw4w9WgXcQ12345678901234567890abc="
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", TEST_FERNET_KEY)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# These imports are safe now that sys.path is set up.
import nexus_steps  # noqa: E402,F401  — populates STEP_REGISTRY via @register
from nexus_common.steps.base import StepContext  # noqa: E402
from nexus_server.config import Settings  # noqa: E402
from nexus_server.db.models import Base  # noqa: E402
from nexus_server.services.auth_service import AuthService  # noqa: E402
from nexus_server.services.credentials.encryption import FieldEncryptor  # noqa: E402
from nexus_server.services.credentials.manager import CredentialManager  # noqa: E402


# Generate a fresh, definitely-valid Fernet key in case the literal above ever
# drifts from Fernet's exact format. We try the literal first so tests are
# deterministic, then fall back to a generated one.
def _valid_fernet_key() -> str:
    """Return a Fernet key that is guaranteed to construct a ``FieldEncryptor``.

    The literal ``TEST_FERNET_KEY`` is tried first so encrypted blobs are
    byte-stable across runs (handy when eyeballing DB rows while debugging). If
    Fernet ever rejects that literal — e.g. the constant is edited and no longer
    decodes as 32 url-safe base64 bytes — we fall back to a freshly generated
    key so the suite degrades to "non-deterministic but working" instead of
    erroring during collection.

    Returns:
        A url-safe base64 Fernet key string.

    Side effects:
        On the fallback path, overwrites ``CREDENTIAL_ENCRYPTION_KEY`` in
        ``os.environ`` so anything reading the env (rather than the returned
        value) stays consistent with the fixtures.
    """
    try:
        FieldEncryptor(TEST_FERNET_KEY)
        return TEST_FERNET_KEY
    except Exception:
        key = FieldEncryptor.generate_key()
        os.environ["CREDENTIAL_ENCRYPTION_KEY"] = key
        return key


# AI Note: module-level, not a fixture. Both the ``settings`` fixture and the
# ``encryptor`` / ``credential_manager`` fixtures must agree on ONE key or a
# credential stored via one and read via the other fails to decrypt.
FERNET_KEY = _valid_fernet_key()


# ── Settings ─────────────────────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    """A Settings instance pointed at an in-memory DB with test secrets.

    Consumed by the ``app`` fixture (passed to ``create_app``) and by unit tests
    that need a realistic config object without reading a ``.env`` file. Every
    field is supplied explicitly so the fixture is insensitive to whatever
    happens to be in the ambient environment.

    Returns:
        A ``Settings`` whose ``database_url`` is ``:memory:`` and whose secrets
        match the ones the ``auth_service`` / ``encryptor`` fixtures use.
    """
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6379",
        minio_endpoint="localhost:9000",
        minio_access_key="test",
        minio_secret_key="test",
        jwt_secret="test-jwt-secret-not-for-production",
        credential_encryption_key=FERNET_KEY,
        cors_origins=["http://localhost:3000"],
    )


# ── Database ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine():
    """A fresh in-memory SQLite engine with all tables created.

    A StaticPool keeps the single in-memory DB alive across connections within
    the test, which is required because ``:memory:`` is per-connection otherwise.

    Scope is per-test (function), so every test gets a pristine schema with no
    rows — tests never need to clean up after themselves and can safely assert
    on absolute counts / "the list is empty".

    Yields:
        An ``AsyncEngine`` with ``Base.metadata`` fully created.

    Side effects:
        Creates every table in ``nexus_server.db.models``; disposes the engine
        (dropping the whole in-memory DB) on teardown.
    """
    from sqlalchemy.pool import StaticPool

    # AI Note: both connect args are load-bearing for the in-memory setup.
    #   * StaticPool → one shared connection, so the schema created below is
    #     visible to the session_factory / dependency-override sessions. With
    #     the default pool each checkout would get its OWN empty ``:memory:`` DB
    #     and every query would fail with "no such table".
    #   * check_same_thread=False → TestClient runs the ASGI app on a worker
    #     thread while the test body runs on the main thread; sqlite3 otherwise
    #     refuses cross-thread use of that shared connection.
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the in-memory engine.

    Args:
        engine: The per-test in-memory ``AsyncEngine``.

    Returns:
        An ``async_sessionmaker`` used both by the ``db`` fixture and by the
        ``get_db`` dependency override installed in ``app``.
    """
    # AI Note: expire_on_commit=False is essential here. With the default
    # (True), every ORM object a test holds would be expired after the route
    # under test commits, and touching an attribute afterwards would trigger a
    # lazy refresh — illegal on an async session and surfacing as
    # MissingGreenlet. It also lets tests keep using e.g. ``sample_node.id``
    # after the app has committed on another session.
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncSession:
    """A per-test AsyncSession. Rolled back / closed on teardown.

    Use this to seed rows directly through ``nexus_server.db.ops`` and to assert
    on persisted state after driving the app.

    Yields:
        An open ``AsyncSession``.

    Side effects:
        Anything committed through this session is visible to the app (they
        share one in-memory database via StaticPool). The session itself is
        closed on teardown; the data disappears when ``engine`` is disposed.
    """
    # AI Note: this session is a DIFFERENT session from the ones the app's
    # get_db override yields per request. They share a connection, so reading
    # app-written rows through ``db`` may require a fresh query (ops.get_*)
    # rather than trusting an identity-map hit on a stale object.
    async with session_factory() as session:
        yield session


# ── Services ─────────────────────────────────────────────────────────────────


@pytest.fixture
def auth_service() -> AuthService:
    """AuthService with the test JWT secret and short, predictable expiries.

    The same instance is installed on ``app.state.auth_service`` by the ``app``
    fixture, so tokens minted here validate inside the app and vice versa.

    Returns:
        An ``AuthService`` signing HS256 tokens with the conftest secret.
    """
    # AI Note: the secret literal must stay in sync with the one hard-coded in
    # tests/integration/test_auth_routes.py (``_SECRET``), which decodes tokens
    # the app hands back to verify their claims.
    return AuthService(
        secret="test-jwt-secret-not-for-production",
        algorithm="HS256",
        access_expire_minutes=60,
        refresh_expire_days=7,
    )


@pytest.fixture
def encryptor() -> FieldEncryptor:
    """FieldEncryptor with the test Fernet key.

    Returns:
        A ``FieldEncryptor`` that can decrypt anything the ``credential_manager``
        fixture encrypted (they share ``FERNET_KEY``).
    """
    return FieldEncryptor(FERNET_KEY)


@pytest.fixture
def credential_manager() -> CredentialManager:
    """CredentialManager with the test Fernet key and all real strategies.

    No strategy is stubbed here, so field validation and encryption behave
    exactly as in production. Tests that must not make outbound calls
    monkeypatch the specific boundary (typically ``manager.test``) rather than
    replacing the manager.

    Returns:
        A ``CredentialManager`` — the same object the ``app`` fixture installs
        on ``app.state.credential_manager``, which is why monkeypatching this
        instance in a test affects the running app.
    """
    return CredentialManager(encryption_key=FERNET_KEY)


# ── FastAPI app + clients ────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def app(engine, session_factory, settings, auth_service, credential_manager):
    """A fully wired FastAPI app backed by the in-memory DB.

    We build the app via ``create_app`` (so routers/middleware match production)
    but bypass the production ``lifespan`` — which would create its own on-disk
    DB engine, default admin user, and storage manager. Instead we populate
    ``app.state`` and override the ``get_db`` dependency to use our test session.

    What is real vs. replaced:
      * Real: every router, middleware, dependency, Pydantic schema, the
        ``AuthService``, the ``CredentialManager`` (with all strategies) and the
        ``JobRunner`` object itself.
      * Replaced: the DB (in-memory), the lifespan (no-op), and — implicitly —
        anything lifespan would have bootstrapped (default admin user, storage
        backend instances). ``StorageManager`` is constructed but its
        ``_backends`` map starts empty; storage tests inject fakes into it.

    Args:
        engine: In-memory engine, also patched onto the ``db.session`` module.
        session_factory: Used by the ``get_db`` override.
        settings: Passed to ``create_app``.
        auth_service: Installed on ``app.state``.
        credential_manager: Installed on ``app.state``.

    Yields:
        A configured ``FastAPI`` instance ready for ``TestClient``.

    Side effects:
        Mutates module-level globals ``nexus_server.db.session._engine`` and
        ``._session_factory``, and installs a dependency override that is
        cleared on teardown.
    """
    from nexus_server.api import deps
    from nexus_server.db import session as db_session
    from nexus_server.main import create_app
    from nexus_server.runner import JobRunner
    from nexus_server.api.routes import ws as ws_module
    from nexus_server.services.storage.manager import StorageManager

    # Point the module-level engine/factory used by get_session() at our test DB,
    # so any code path that bypasses the dependency override still hits memory.
    # AI Note: this is NOT redundant with the dependency override below. Code
    # that opens its own session — most importantly ``JobRunner._run_job``,
    # which runs in a background task with no request scope — calls
    # ``get_session_factory()`` directly. Without this patch those paths would
    # lazily create a real on-disk engine from DATABASE_URL and the runner tests
    # would assert against a different database than the one they seeded.
    # These are process-global and are intentionally left pointing at the (now
    # disposed) test engine after teardown; the next ``app`` fixture overwrites
    # them.
    db_session._engine = engine
    db_session._session_factory = session_factory

    app = create_app(settings=settings)

    # Populate app.state the way lifespan would, minus the on-disk side effects.
    app.state.auth_service = auth_service
    app.state.credential_manager = credential_manager
    storage_manager = StorageManager(credential_manager=credential_manager)
    app.state.storage_manager = storage_manager
    # AI Note: the runner is given the REAL ws manager singleton. That is safe
    # because no agent ever connects in tests, so ``send_to_agent`` reports the
    # node as disconnected. Runner tests that care about dispatch construct
    # their own JobRunner with a FakeWsManager instead of using this one.
    app.state.runner = JobRunner(
        ws_manager=ws_module.manager, credential_manager=credential_manager
    )

    # Override get_db to yield sessions from our test factory.
    async def _override_get_db():
        """Request-scoped session replacement for ``deps.get_db``.

        Yields a session from the test factory so route handlers read/write the
        same in-memory DB the test seeds through the ``db`` fixture.
        """
        async with session_factory() as s:
            yield s

    app.dependency_overrides[deps.get_db] = _override_get_db

    # Run the app without triggering the real lifespan (tables already created).
    # AI Note: assigning ``router.lifespan_context`` (rather than passing a
    # lifespan to create_app) is what makes ``with TestClient(app)`` skip the
    # production startup. If a future change moves required setup INTO lifespan,
    # it must also be mirrored into this fixture or tests will fail with
    # AttributeError on app.state.
    app.router.lifespan_context = _noop_lifespan
    yield app
    app.dependency_overrides.clear()


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _noop_lifespan(app):
    """Replacement lifespan: do nothing (test fixtures own setup/teardown).

    Args:
        app: The FastAPI instance (accepted to match the lifespan protocol;
            unused).

    Yields:
        ``None`` — startup and shutdown are both no-ops.
    """
    yield


@pytest.fixture
def client(app):
    """A synchronous TestClient. Unauthenticated by default.

    Args:
        app: The wired FastAPI app.

    Yields:
        A ``TestClient`` context-managed so the (no-op) lifespan runs and the
        underlying transport is closed afterwards.
    """
    # AI Note: imported lazily so that merely collecting this module does not
    # require starlette's test dependencies, and so the import happens after the
    # sys.path bootstrap at the top of the file.
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


# ── Domain fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def admin_user(db):
    """A persisted admin user (password: 'adminpass').

    Args:
        db: Test session; the row is committed by ``ops.create_user`` and is
            therefore visible to the app.

    Returns:
        The persisted ``User`` ORM object with ``role='admin'``.

    Side effects:
        INSERTs one row into ``users``. Auth-route tests log in as this user
        with the literal password ``adminpass``, so changing it here breaks
        ``tests/integration/test_auth_routes.py``.
    """
    # AI Note: imported inside the fixture rather than at module scope so the
    # sys.path bootstrap at the top of this file has definitely run first.
    from nexus_server.db import ops

    return await ops.create_user(
        db,
        username="admin",
        password_hash=AuthService.hash_password("adminpass"),
        email="admin@example.com",
        role="admin",
    )


@pytest_asyncio.fixture
async def regular_user(db):
    """A persisted non-admin user (password: 'userpass').

    The counterpart to ``admin_user``, used as the "should be forbidden" actor
    in RBAC tests and as ``submitted_by`` for job fixtures.

    Returns:
        The persisted ``User`` ORM object with ``role='user'`` and username
        ``alice`` (asserted on by name in the auth tests).
    """
    from nexus_server.db import ops

    return await ops.create_user(
        db,
        username="alice",
        password_hash=AuthService.hash_password("userpass"),
        email="alice@example.com",
        role="user",
    )


@pytest.fixture
def admin_token(admin_user, auth_service) -> str:
    """A valid access token for the admin user.

    Returns:
        A signed HS256 access JWT carrying ``sub`` (the user id as a string)
        and ``role='admin'``.
    """
    return auth_service.create_access_token(str(admin_user.id), admin_user.role)


@pytest.fixture
def user_token(regular_user, auth_service) -> str:
    """A valid access token for the regular user.

    Returns:
        A signed HS256 access JWT with ``role='user'``.
    """
    return auth_service.create_access_token(str(regular_user.id), regular_user.role)


@pytest.fixture
def admin_client(client, admin_token):
    """TestClient pre-authenticated as admin via Authorization header.

    Returns:
        The shared ``TestClient`` with a default admin bearer header.
    """
    # AI Note: this MUTATES the shared ``client`` object rather than returning a
    # new one, so ``admin_client`` and ``auth_client`` are the same instance.
    # Requesting both fixtures in a single test silently gives every request the
    # last-applied identity. Use one per test (see the note in
    # test_pools_routes.test_delete_pool_as_regular_user_forbidden).
    client.headers.update({"Authorization": f"Bearer {admin_token}"})
    return client


@pytest.fixture
def auth_client(client, user_token):
    """TestClient pre-authenticated as a regular user.

    Returns:
        The shared ``TestClient`` with a default non-admin bearer header. See
        the mutation caveat on ``admin_client``.
    """
    client.headers.update({"Authorization": f"Bearer {user_token}"})
    return client


@pytest.fixture
def step_context() -> StepContext:
    """An empty StepContext for step-level unit tests.

    Returns:
        A fresh ``StepContext`` with no accumulated outputs — the state a job
        starts in before its first step runs.
    """
    return StepContext()


@pytest_asyncio.fixture
async def sample_pool(db, admin_user):
    """A persisted pool owned by the admin user.

    Returns:
        A ``Pool`` named ``test-pool`` with no member nodes.

    Side effects:
        INSERTs one row into ``pools`` (and pulls in ``admin_user``, so a test
        requesting only this fixture still gets an admin user in the DB).
    """
    from nexus_server.db import ops

    return await ops.create_pool(db, name="test-pool", created_by=admin_user.id)


@pytest_asyncio.fixture
async def sample_node(db):
    """A persisted online node.

    A linux/x86_64 agent host in the ``online`` state — i.e. eligible for
    scheduling — with every descriptive field populated so node-serialisation
    assertions have real values to check.

    Returns:
        A ``Node`` with hostname ``node-1.test``, 8 cores and 16 GiB RAM.

    Side effects:
        INSERTs one row into ``nodes``. ``ops.create_node`` also generates an
        ``api_key``; route tests assert that key is never serialised to clients.
    """
    from nexus_server.db import ops

    # AI Note: status="online" matters — the scheduler only considers nodes in
    # {online, busy}, and the nodes reconnect test relies on this node already
    # being online so the post-provision poll loop succeeds on its first pass.
    return await ops.create_node(
        db,
        hostname="node-1.test",
        display_name="Test Node 1",
        os_type="linux",
        os_version="Ubuntu 22.04",
        arch="x86_64",
        cpu_model="Test CPU",
        cpu_cores=8,
        ram_mb=16384,
        agent_version="0.1.0",
        ip_address="10.0.0.1",
        status="online",
    )
