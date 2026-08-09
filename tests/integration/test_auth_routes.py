"""Integration tests for the authentication routes.

SUT: ``packages/server/src/nexus_server/api/routes/auth.py`` exercised through a
real ``TestClient`` against the in-memory app. These tests cover the full
login / refresh / register / me surface — happy paths, auth failures, RBAC
edges, and JWT claim/structure verification — using the shared conftest
fixtures.

Where a test asserts on token *contents* it decodes the JWT with the same
secret the app is configured with (``test-jwt-secret-not-for-production`` from
conftest) so we prove the route actually minted a real, signed token with the
expected claims — not just any truthy string.

Security properties pinned here (do not weaken without a deliberate decision):
  * Login failures are indistinguishable — unknown user and wrong password
    return the identical status and ``detail``, so the endpoint cannot be used
    to enumerate accounts.
  * Deactivated users cannot authenticate even with correct credentials.
  * Access and refresh tokens are NOT interchangeable: ``/me`` rejects a refresh
    token and ``/refresh`` rejects an access token (the ``type`` claim).
  * Refresh tokens carry no ``role`` claim, so a stale refresh token cannot
    replay a role the user has since lost.
  * Signature, expiry and subject-existence are each independently enforced.
  * ``register`` is admin-only and the RBAC gate runs before any user is
    created, so a rejected request leaves no partial state.
  * No response body ever contains ``password`` or ``password_hash``.

Nothing is stubbed: real routes, real ``AuthService``, real bcrypt hashing and
the real in-memory DB. Forged/expired tokens are constructed locally with
``jwt.encode`` because the app itself will never mint them.

Fixture note
    ``client``, ``admin_client`` and ``auth_client`` are the SAME TestClient
    object with different default headers. Tests that need a specific identity
    per request pass an explicit ``Authorization`` header instead of switching
    fixtures.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

# The JWT secret/algorithm the app is wired with in conftest.
# AI Note: duplicated deliberately rather than imported from conftest — these
# tests verify the app really signs with the expected secret, so reading the
# value back out of the same object under test would make the assertion
# circular. If conftest's ``auth_service`` fixture changes its secret, this
# constant must be updated by hand and the decode assertions will catch it.
_SECRET = "test-jwt-secret-not-for-production"
_ALG = "HS256"


def _decode(token: str) -> dict:
    """Decode a token the app handed us, validating the signature.

    Args:
        token: An encoded JWT string returned by a route under test.

    Returns:
        The decoded claims dict.

    Raises:
        jwt.PyJWTError: If the signature is invalid or the token has expired —
            which is itself a meaningful failure for these tests (it means the
            app minted something we cannot verify).
    """
    return jwt.decode(token, _SECRET, algorithms=[_ALG])


# ── POST /api/auth/login ──────────────────────────────────────────────────────


def test_login_admin_returns_token_pair(client, admin_user):
    """Valid admin credentials yield a 200 with an access + refresh token pair."""
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"
    # The two tokens are distinct JWTs (access vs refresh).
    assert body["access_token"] != body["refresh_token"]


def test_login_admin_access_token_has_expected_claims(client, admin_user):
    """The minted access token is a real signed JWT carrying sub/role/type=access.

    The refresh half asserts a deliberate omission: refresh tokens must NOT
    carry a ``role`` claim. If they did, a long-lived refresh token could keep
    replaying a privilege that has since been revoked, because ``/refresh``
    would copy the stale claim into the new access token instead of re-reading
    the user's current role.
    """
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert resp.status_code == 200
    body = resp.json()

    access = _decode(body["access_token"])
    assert access["sub"] == str(admin_user.id)
    assert access["role"] == "admin"
    assert access["type"] == "access"
    # Expiry is in the future relative to issue time.
    assert access["exp"] > access["iat"]

    refresh = _decode(body["refresh_token"])
    assert refresh["sub"] == str(admin_user.id)
    assert refresh["type"] == "refresh"
    # Refresh tokens carry no role claim (only access tokens do).
    assert "role" not in refresh


def test_login_regular_user_returns_token_pair(client, regular_user):
    """A non-admin user can also log in; the access token carries role=user."""
    resp = client.post("/api/auth/login", json={"username": "alice", "password": "userpass"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"

    access = _decode(body["access_token"])
    assert access["sub"] == str(regular_user.id)
    assert access["role"] == "user"
    assert access["type"] == "access"


def test_login_access_token_authenticates_me(client, regular_user):
    """The access token from login actually works as a bearer credential for /me."""
    login = client.post("/api/auth/login", json={"username": "alice", "password": "userpass"})
    access = login.json()["access_token"]

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


def test_login_wrong_password_is_401(client, admin_user):
    """A correct username with the wrong password is rejected as 401."""
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrongpass"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


def test_login_unknown_user_is_401(client):
    """An unknown username is rejected as 401 (no user enumeration leak)."""
    resp = client.post("/api/auth/login", json={"username": "nobody", "password": "whatever"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


def test_login_unknown_and_bad_password_share_detail(client, admin_user):
    """Unknown-user and wrong-password failures are indistinguishable to a caller.

    Anti-enumeration guarantee: if the two paths ever diverged (different status
    or a "no such user" detail), an attacker could probe which usernames exist
    before ever attempting a password. Both halves are asserted in one test so a
    change to only one branch cannot slip through.
    """
    unknown = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    bad_pw = client.post("/api/auth/login", json={"username": "admin", "password": "x"})

    assert unknown.status_code == bad_pw.status_code == 401
    assert unknown.json()["detail"] == bad_pw.json()["detail"] == "Invalid credentials"


async def test_login_inactive_user_is_401(client, db):
    """An existing but deactivated user cannot authenticate (returns 401).

    Deactivation is the offboarding mechanism, so it must block login outright
    rather than merely hiding the account from listings. The user is created
    with a known-good password first so the failure can only come from the
    ``is_active`` gate.
    """
    from nexus_server.db import ops
    from nexus_server.services.auth_service import AuthService

    # Create the user via the same factory the app reads from, then deactivate.
    user = await ops.create_user(
        db,
        username="dormant",
        password_hash=AuthService.hash_password("dormantpass"),
        role="user",
    )
    await ops.update_user(db, user.id, is_active=False)

    # Sanity: credentials are otherwise correct, so a 401 here proves the
    # is_active gate fired (not a password mismatch).
    resp = client.post(
        "/api/auth/login", json={"username": "dormant", "password": "dormantpass"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


def test_login_missing_field_is_422(client):
    """A malformed body (missing password) fails request validation with 422."""
    resp = client.post("/api/auth/login", json={"username": "admin"})

    assert resp.status_code == 422


def test_login_empty_body_is_422(client):
    """An entirely empty JSON body fails validation (both fields required)."""
    resp = client.post("/api/auth/login", json={})

    assert resp.status_code == 422


# ── POST /api/auth/refresh ────────────────────────────────────────────────────


def test_refresh_valid_token_returns_new_pair(client, admin_user):
    """A valid refresh token is exchanged for a fresh access + refresh pair."""
    login = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    refresh_token = login.json()["refresh_token"]

    resp = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"


def test_refresh_new_access_token_is_usable_and_correct(client, admin_user):
    """The refreshed access token is a real access JWT and authenticates /me."""
    login = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    refresh_token = login.json()["refresh_token"]

    resp = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    new_access = resp.json()["access_token"]

    claims = _decode(new_access)
    assert claims["sub"] == str(admin_user.id)
    assert claims["role"] == "admin"
    assert claims["type"] == "access"

    # And it works as a credential end-to-end.
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200
    assert me.json()["username"] == "admin"


def test_refresh_with_access_token_is_401(client, admin_user):
    """An access token is not a refresh token — refresh must reject it as 401.

    Both token kinds are signed with the same secret, so only the ``type``
    claim distinguishes them. Dropping that check would let a short-lived
    access token be traded for an indefinitely renewable session.
    """
    login = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    access_token = login.json()["access_token"]

    resp = client.post("/api/auth/refresh", json={"refresh_token": access_token})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid refresh token"


def test_refresh_with_garbage_is_401(client):
    """An undecodable token string is rejected as 401."""
    resp = client.post("/api/auth/refresh", json={"refresh_token": "not-a-real-jwt"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid refresh token"


def test_refresh_token_signed_with_wrong_secret_is_401(client, admin_user):
    """A well-formed refresh JWT signed with a foreign secret fails signature → 401.

    Proves the app verifies the HMAC rather than merely decoding the payload.
    Every other claim (sub, type, exp) is deliberately valid so signature
    verification is the only thing that can reject it.
    """
    forged = jwt.encode(
        {
            "sub": str(admin_user.id),
            "type": "refresh",
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
            "iat": datetime.now(timezone.utc),
        },
        # AI Note: the wrong secret is padded to a realistic length on purpose —
        # a too-short key could be rejected by the library for its own reasons,
        # which would make this test pass without exercising signature checking.
        "the-wrong-secret-but-long-enough-for-hs256-sha256",
        algorithm=_ALG,
    )

    resp = client.post("/api/auth/refresh", json={"refresh_token": forged})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid refresh token"


def test_refresh_missing_field_is_422(client):
    """A refresh body without the refresh_token field fails validation."""
    resp = client.post("/api/auth/refresh", json={})

    assert resp.status_code == 422


# ── POST /api/auth/register ───────────────────────────────────────────────────


def test_register_as_admin_creates_user(admin_client):
    """An admin can register a new user and gets back a 201 with the user info.

    The negative half — that neither ``password`` nor ``password_hash`` appears
    in the response — is the part worth protecting: the create response is built
    from the ORM object, so adding a field to ``UserInfo`` could leak the hash.
    """
    resp = admin_client.post(
        "/api/auth/register",
        json={"username": "bob", "password": "bobpass", "email": "bob@example.com", "role": "user"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "bob"
    assert body["email"] == "bob@example.com"
    assert body["role"] == "user"
    assert body["is_active"] is True
    assert body["id"]
    # No secret material is ever echoed back to the caller.
    assert "password" not in body
    assert "password_hash" not in body


def test_register_defaults_role_to_user_and_email_to_null(admin_client):
    """Omitting role/email uses the schema defaults (role='user', email=None)."""
    resp = admin_client.post(
        "/api/auth/register",
        json={"username": "dave", "password": "davepass"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "user"
    assert body["email"] is None


def test_register_admin_role_creates_admin(admin_client):
    """Registering with role='admin' yields an admin whose token works as admin.

    The follow-up registration is the real assertion: it proves the stored role
    is honoured by the RBAC dependency at request time, not merely echoed back
    in the create response.
    """
    resp = admin_client.post(
        "/api/auth/register",
        json={"username": "eve", "password": "evepass", "role": "admin"},
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "admin"

    # The newly minted admin can log in and is recognised as an admin via RBAC:
    # registering yet another user (an admin-only action) succeeds.
    # AI Note: the per-request ``headers=`` override below is what makes this
    # work — ``admin_client`` already carries the seed admin's bearer token, and
    # passing an explicit header for this one call swaps in Eve's identity
    # without mutating the shared client for the rest of the test.
    login = admin_client.post(
        "/api/auth/login", json={"username": "eve", "password": "evepass"}
    )
    eve_access = login.json()["access_token"]
    second = admin_client.post(
        "/api/auth/register",
        json={"username": "frank", "password": "frankpass"},
        headers={"Authorization": f"Bearer {eve_access}"},
    )
    assert second.status_code == 201


def test_registered_user_can_then_log_in(admin_client):
    """A user created via register can authenticate with the given password."""
    admin_client.post(
        "/api/auth/register",
        json={"username": "carol", "password": "carolpass", "role": "user"},
    )
    login = admin_client.post(
        "/api/auth/login", json={"username": "carol", "password": "carolpass"}
    )

    assert login.status_code == 200
    assert login.json()["access_token"]
    # Wrong password for the freshly created user is still rejected.
    bad = admin_client.post(
        "/api/auth/login", json={"username": "carol", "password": "nope"}
    )
    assert bad.status_code == 401


def test_register_as_regular_user_is_403(auth_client):
    """A non-admin caller is forbidden from registering users (RBAC gate)."""
    resp = auth_client.post(
        "/api/auth/register",
        json={"username": "mallory", "password": "x", "role": "user"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin access required"


def test_register_regular_user_cannot_create_even_valid_admin(auth_client):
    """RBAC is enforced before any user creation — a 403 means no user persists.

    Privilege-escalation guard: a non-admin asking for ``role='admin'`` must be
    stopped *before* the INSERT. Asserting only the 403 would still pass if the
    handler created the user and then rejected the response, so the follow-up
    login proves nothing was written.
    """
    resp = auth_client.post(
        "/api/auth/register",
        json={"username": "sneaky", "password": "x", "role": "admin"},
    )
    assert resp.status_code == 403

    # The would-be user was never created: logging in as them fails with 401.
    login = auth_client.post(
        "/api/auth/login", json={"username": "sneaky", "password": "x"}
    )
    assert login.status_code == 401


def test_register_unauthenticated_is_401(client):
    """An unauthenticated caller cannot reach register (no bearer credentials)."""
    resp = client.post(
        "/api/auth/register",
        json={"username": "anon", "password": "x", "role": "user"},
    )

    # HTTPBearer with no Authorization header → 401 Not authenticated.
    assert resp.status_code == 401


def test_register_duplicate_username_is_409(admin_client, regular_user):
    """Registering an already-taken username is a 409 conflict."""
    resp = admin_client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "newpass", "role": "user"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Username already taken"


def test_register_then_duplicate_same_request_is_409(admin_client):
    """Registering the same username twice in a row: first 201, second 409."""
    payload = {"username": "grace", "password": "gracepass"}
    first = admin_client.post("/api/auth/register", json=payload)
    second = admin_client.post("/api/auth/register", json=payload)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "Username already taken"


def test_register_missing_password_is_422(admin_client):
    """A register body missing the required password fails validation."""
    resp = admin_client.post("/api/auth/register", json={"username": "noPass"})

    assert resp.status_code == 422


# ── GET /api/auth/me ──────────────────────────────────────────────────────────


def test_me_as_admin_returns_admin(admin_client, admin_user):
    """``/me`` returns the authenticated admin's own profile."""
    resp = admin_client.get("/api/auth/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert body["email"] == "admin@example.com"
    assert body["id"] == str(admin_user.id)
    assert body["is_active"] is True
    # /me never leaks the password hash.
    assert "password_hash" not in body


def test_me_as_regular_user_returns_self(auth_client, regular_user):
    """``/me`` returns the authenticated regular user's own profile."""
    resp = auth_client.get("/api/auth/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "alice"
    assert body["role"] == "user"
    assert body["email"] == "alice@example.com"
    assert body["id"] == str(regular_user.id)


def test_me_unauthenticated_is_401(client):
    """No Authorization header → HTTPBearer rejects with 401."""
    resp = client.get("/api/auth/me")

    assert resp.status_code == 401


def test_me_with_garbage_token_is_401(client):
    """A malformed bearer token cannot be decoded → 401."""
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert resp.status_code == 401


def test_me_rejects_refresh_token(client, admin_user):
    """A refresh token is not an access token — /me must reject it (type check)."""
    login = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    refresh_token = login.json()["refresh_token"]

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh_token}"})

    assert resp.status_code == 401


def test_me_rejects_expired_access_token(client, admin_user):
    """An access token past its expiry is rejected by /me (signature valid, exp past).

    Isolates expiry from every other failure mode: the token is signed with the
    real ``_SECRET`` and names a real user, so only the past ``exp`` can cause
    the 401. Guards against a decode call that forgets ``verify_exp``.
    """
    expired = jwt.encode(
        {
            "sub": str(admin_user.id),
            "role": "admin",
            "type": "access",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
            "iat": datetime.now(timezone.utc) - timedelta(minutes=61),
        },
        _SECRET,
        algorithm=_ALG,
    )

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


def test_me_rejects_token_for_unknown_user(client):
    """A perfectly-signed access token whose sub does not exist is rejected.

    Covers the deleted-user case: possession of a valid signature is not enough,
    the dependency must still resolve ``sub`` against the database. Without this
    lookup a token issued to a since-deleted account would keep working until
    it expired.
    """
    import uuid

    ghost = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "admin",
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=60),
            "iat": datetime.now(timezone.utc),
        },
        _SECRET,
        algorithm=_ALG,
    )

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {ghost}"})
    assert resp.status_code == 401
