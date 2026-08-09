"""Smoke tests — validate the test infrastructure itself.

These verify that conftest fixtures wire up correctly: packages import, the
in-memory DB works, the FastAPI app boots, and auth flows through TestClient.
If these fail, every other backend test is suspect — fix the harness first.

Role in the suite
    This module is deliberately the cheapest, broadest signal in the backend
    test suite. It asserts only on the *plumbing* provided by
    ``tests/conftest.py`` (sys.path bootstrapping, the in-memory SQLite engine,
    ``create_app`` wiring, the ``get_db`` dependency override and the
    pre-authenticated client fixtures). It intentionally does NOT assert on
    business behaviour — that belongs in ``tests/unit`` and
    ``tests/integration``.

Triage tip
    A failure here almost always means a harness/config regression (a moved
    package ``src`` dir, a changed ``create_app`` signature, a new required
    setting, a renamed auth route) rather than a product bug. Fix conftest
    first; re-run the rest of the suite afterwards.
"""

from __future__ import annotations


async def test_packages_importable():
    """The four workspace packages import cleanly under the test interpreter.

    Guards the ``sys.path`` bootstrap in conftest: the editable installs in
    ``.venv`` are unreliable/offline, so conftest injects each
    ``packages/*/src`` directory manually. If someone moves or renames a
    package directory, every other test would fail with an opaque
    ``ModuleNotFoundError`` deep inside a fixture — this test localises that
    failure to one obvious place.
    """
    import nexus_common  # noqa: F401
    import nexus_server  # noqa: F401
    import nexus_steps  # noqa: F401


async def test_step_registry_populated():
    """Importing ``nexus_steps`` populates the global STEP_REGISTRY.

    ``STEP_REGISTRY`` is filled as a side effect of the ``@register`` decorators
    running at import time, and conftest imports ``nexus_steps`` for exactly
    that reason. Job submission validation, the scheduler and the agent all
    resolve step classes through this registry, so an empty registry silently
    turns every step name into "unknown step". Asserting a known built-in
    (``run_command``) is present catches a broken/reordered package __init__.
    """
    from nexus_common.steps.registry import STEP_REGISTRY

    assert "run_command" in STEP_REGISTRY
    assert len(STEP_REGISTRY) > 0


async def test_db_session_works(db):
    """The ``db`` fixture yields a usable AsyncSession against in-memory SQLite.

    Executing a trivial ``SELECT 1`` proves the engine, the StaticPool (which
    keeps the single ``:memory:`` database alive across connections) and the
    async driver are all functional before any test relies on real tables.
    """
    from sqlalchemy import text

    result = await db.execute(text("SELECT 1"))
    assert result.scalar_one() == 1


async def test_create_and_fetch_user(db):
    """A user can be written and read back through the real repository layer.

    Exercises ``Base.metadata.create_all`` (the tables actually exist),
    ``ops.create_user`` (INSERT + commit) and ``ops.get_user_by_id`` (SELECT)
    in one round trip. Nearly every fixture in the suite builds on user
    creation, so a regression in the model/ops pair shows up here first.
    """
    from nexus_server.db import ops
    from nexus_server.services.auth_service import AuthService

    user = await ops.create_user(
        db, username="smoke", password_hash=AuthService.hash_password("x"), role="user"
    )
    fetched = await ops.get_user_by_id(db, user.id)
    assert fetched is not None
    assert fetched.username == "smoke"


def test_app_health_via_unauthed_route(client):
    """The FastAPI app boots and routes reach real handler logic.

    A 401 (rather than 404 or 500) is the meaningful assertion: 404 would mean
    the auth router was never mounted by ``create_app``, and 500 would mean the
    handler blew up before it could evaluate the credentials — e.g. the
    ``get_db`` dependency override in conftest is not wired to the test engine.
    """
    # /api/auth/login exists and rejects empty/bad creds with 401 (not 404/500).
    resp = client.post("/api/auth/login", json={"username": "nope", "password": "nope"})
    assert resp.status_code == 401


def test_admin_client_authenticated(admin_client):
    """The ``admin_client`` fixture really is authenticated as the admin user.

    Proves the full token chain: ``admin_user`` was persisted, ``auth_service``
    minted a token with the same secret the app validates against, and the
    fixture attached it as an ``Authorization: Bearer`` header. Every admin-only
    route test depends on this chain holding.
    """
    resp = admin_client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"


def test_auth_client_is_regular_user(auth_client):
    """``auth_client`` authenticates as a NON-admin user.

    The RBAC tests elsewhere assert 403s using this fixture, so if it ever
    started returning an admin token those tests would pass for the wrong
    reason. This pins the role claim explicitly.
    """
    resp = auth_client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["role"] == "user"


def test_unauthenticated_me_is_rejected(client):
    """The bare ``client`` fixture carries no credentials.

    Confirms the negative control for every "requires auth" assertion in the
    suite. Both 401 and 403 are accepted because the exact code depends on
    whether FastAPI's ``HTTPBearer`` is configured with ``auto_error`` and
    whether a header is absent versus malformed.
    """
    resp = client.get("/api/auth/me")
    assert resp.status_code in (401, 403)
