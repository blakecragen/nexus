"""Unit tests for AuthService (JWT auth + password hashing + RBAC token flow).

SUT: packages/server/src/nexus_server/services/auth_service.py

These exercise real bcrypt hashing and real PyJWT encode/decode round-trips, plus
the async authenticate/refresh flows against the in-memory SQLite DB via ops.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from nexus_server.db import ops


# ── Password hashing ─────────────────────────────────────────────────────────


def test_hash_password_is_not_plaintext_and_salts():
    """Hash differs from the input and is non-deterministic (salted)."""
    from nexus_server.services.auth_service import AuthService

    h1 = AuthService.hash_password("hunter2")
    h2 = AuthService.hash_password("hunter2")
    assert h1 != "hunter2"
    assert h1 != h2  # distinct salts


def test_verify_password_correct():
    """verify_password accepts the password that produced the hash.

    The bcrypt salt is embedded in the hash, so verification must re-derive it
    from the stored value rather than needing the original salt.
    """
    from nexus_server.services.auth_service import AuthService

    h = AuthService.hash_password("correct horse")
    assert AuthService.verify_password("correct horse", h) is True


def test_verify_password_wrong():
    """verify_password rejects a different password against the same hash.

    A constant-True verify would make every login succeed — this is the single
    most security-critical assertion in the auth suite.
    """
    from nexus_server.services.auth_service import AuthService

    h = AuthService.hash_password("correct horse")
    assert AuthService.verify_password("battery staple", h) is False


# ── Access token payload ─────────────────────────────────────────────────────


def test_create_access_token_payload(auth_service):
    """An access token carries sub, role, type='access', and iat/exp claims.

    ``role`` is stamped into the access token so RBAC dependencies can authorize a
    request without a DB round trip; ``type`` is what stops a refresh token from
    being accepted as a bearer credential.
    """
    token = auth_service.create_access_token("user-123", "admin")
    payload = auth_service.decode_token(token)

    assert payload["sub"] == "user-123"
    assert payload["role"] == "admin"
    assert payload["type"] == "access"
    assert "exp" in payload and "iat" in payload
    # exp is after iat (access expiry is positive)
    assert payload["exp"] > payload["iat"]


def test_create_refresh_token_payload(auth_service):
    """A refresh token carries sub and type='refresh' but deliberately NO role.

    Omitting role forces refresh() to re-read the user's current role from the DB,
    so a demotion takes effect on the next refresh instead of persisting for the
    full 7-day refresh lifetime.
    """
    token = auth_service.create_refresh_token("user-456")
    payload = auth_service.decode_token(token)

    assert payload["sub"] == "user-456"
    assert payload["type"] == "refresh"
    # refresh tokens carry no role claim
    assert "role" not in payload
    assert payload["exp"] > payload["iat"]


def test_access_and_refresh_have_distinct_expiries(auth_service):
    """Refresh (7d) lives far longer than access (60m), with the right absolute spans."""
    access = auth_service.decode_token(auth_service.create_access_token("u", "user"))
    refresh = auth_service.decode_token(auth_service.create_refresh_token("u"))
    assert refresh["exp"] - refresh["iat"] > access["exp"] - access["iat"]
    # default access expiry is 60 minutes; refresh is 7 days (allow 5s clock slack)
    assert abs((access["exp"] - access["iat"]) - 60 * 60) <= 5
    assert abs((refresh["exp"] - refresh["iat"]) - 7 * 24 * 60 * 60) <= 5


def test_custom_expiries_are_honored():
    """Constructor expiry overrides flow through to the encoded claims."""
    from nexus_server.services.auth_service import AuthService

    svc = AuthService(
        secret="x" * 32, access_expire_minutes=5, refresh_expire_days=1
    )
    access = svc.decode_token(svc.create_access_token("u", "user"))
    refresh = svc.decode_token(svc.create_refresh_token("u"))
    assert abs((access["exp"] - access["iat"]) - 5 * 60) <= 5
    assert abs((refresh["exp"] - refresh["iat"]) - 24 * 60 * 60) <= 5


# ── decode_token round-trip + failures ───────────────────────────────────────


def test_decode_token_round_trip(auth_service):
    """decode_token returns the claims of a token this service just issued."""
    token = auth_service.create_access_token("abc", "user")
    assert auth_service.decode_token(token)["sub"] == "abc"


def test_decode_token_garbage_raises(auth_service):
    """A non-JWT string raises PyJWTError rather than returning None/{}.

    Callers rely on the exception to distinguish "malformed" from "valid but
    unauthorized"; a silent empty payload would be treated as an anonymous-but-
    valid session by naive callers.
    """
    with pytest.raises(jwt.PyJWTError):
        auth_service.decode_token("not.a.jwt")


def test_decode_token_tampered_signature_raises(auth_service):
    """Flipping a character in the signature invalidates the token.

    AI Note: the mutation targets the FIRST character of the signature, not the
    last. This test was flaky when it flipped the final character: in base64url
    the trailing character of a segment can carry insignificant padding bits, so
    for some signatures several distinct final characters decode to the SAME
    byte string and the token still verified — the test then failed with
    "DID NOT RAISE" depending on the randomly generated signature. Every one of
    the first character's 6 bits is significant, so changing it always perturbs
    byte 0 of the decoded signature.
    """
    token = auth_service.create_access_token("abc", "user")
    head, body, sig = token.split(".")
    # mutate the signature segment at a position with no padding ambiguity
    bad_char = "A" if sig[0] != "A" else "B"
    tampered = f"{head}.{body}.{bad_char}{sig[1:]}"
    with pytest.raises(jwt.InvalidSignatureError):
        auth_service.decode_token(tampered)


def test_decode_token_wrong_secret_raises(auth_service):
    """A token signed with a different secret must not validate."""
    from nexus_server.services.auth_service import AuthService

    # use a full-length secret so PyJWT doesn't emit an InsecureKeyLength warning
    other = AuthService(secret="a-totally-different-secret-32bytes!!")
    foreign = other.create_access_token("abc", "user")
    with pytest.raises(jwt.InvalidSignatureError):
        auth_service.decode_token(foreign)


def test_decode_token_expired_raises(auth_service):
    """An access token whose exp is in the past is rejected by decode_token."""
    from nexus_server.services.auth_service import AuthService

    svc = AuthService(secret="y" * 32, access_expire_minutes=-1)
    expired = svc.create_access_token("abc", "user")
    # same secret, so the failure is specifically expiry (not signature)
    with pytest.raises(jwt.ExpiredSignatureError):
        svc.decode_token(expired)


# ── access vs refresh type discrimination ────────────────────────────────────


def test_token_type_discrimination(auth_service):
    """Access and refresh tokens are distinguishable by their 'type' claim.

    This claim is the only thing separating the two token classes — they are signed
    with the same secret and algorithm.
    """
    access = auth_service.decode_token(auth_service.create_access_token("u", "user"))
    refresh = auth_service.decode_token(auth_service.create_refresh_token("u"))
    assert access["type"] == "access"
    assert refresh["type"] == "refresh"
    assert access["type"] != refresh["type"]


# ── authenticate() ───────────────────────────────────────────────────────────


async def test_authenticate_success_returns_token_pair(db, auth_service):
    """A valid username/password yields a bearer access+refresh pair bound to the user.

    The access token must carry the user's real UUID and role; a mismatch here
    would authorize the wrong account.
    """
    user = await ops.create_user(
        db,
        username="bob",
        password_hash=auth_service.hash_password("s3cret"),
        email="bob@example.com",
        role="user",
    )

    result = await auth_service.authenticate(db, "bob", "s3cret")

    assert result is not None
    assert result["token_type"] == "bearer"
    # access token carries this user's id + role
    access_payload = auth_service.decode_token(result["access_token"])
    assert access_payload["sub"] == str(user.id)
    assert access_payload["role"] == "user"
    assert access_payload["type"] == "access"
    # refresh token is genuinely a refresh token
    refresh_payload = auth_service.decode_token(result["refresh_token"])
    assert refresh_payload["type"] == "refresh"
    assert refresh_payload["sub"] == str(user.id)


async def test_authenticate_updates_last_login_at(db, auth_service):
    """A successful login stamps last_login_at to the current UTC time (DB write).

    Bounded on both sides so a hardcoded/stale timestamp would fail. The tz-naive
    normalization mirrors SQLite returning naive datetimes.
    """
    user = await ops.create_user(
        db,
        username="carol",
        password_hash=auth_service.hash_password("pw"),
        email="carol@example.com",
        role="user",
    )
    assert user.last_login_at is None

    before = datetime.now(timezone.utc)
    await auth_service.authenticate(db, "carol", "pw")
    after = datetime.now(timezone.utc)

    refreshed = await ops.get_user_by_id(db, user.id)
    assert refreshed.last_login_at is not None
    stamp = refreshed.last_login_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    # last_login_at was stamped to "now" — bounded on both sides (allow slack)
    assert before - timedelta(seconds=2) <= stamp <= after + timedelta(seconds=2)


async def test_authenticate_unknown_user_returns_none(db, auth_service):
    """An unknown username returns None instead of raising.

    The route turns None into a generic 401; raising here would leak user existence
    via a differing error shape.
    """
    assert await auth_service.authenticate(db, "nobody", "whatever") is None


async def test_authenticate_wrong_password_returns_none(db, auth_service):
    """A wrong password returns None AND leaves last_login_at untouched.

    Stamping last_login_at on a failed attempt would corrupt the audit trail and
    let an attacker prove an account exists by watching the field change.
    """
    user = await ops.create_user(
        db,
        username="dave",
        password_hash=auth_service.hash_password("right"),
        email="dave@example.com",
        role="user",
    )
    assert await auth_service.authenticate(db, "dave", "wrong") is None
    # a failed login must NOT stamp last_login_at
    refreshed = await ops.get_user_by_id(db, user.id)
    assert refreshed.last_login_at is None


async def test_authenticate_inactive_user_returns_none(db, auth_service):
    """A deactivated user cannot log in even with the correct password.

    is_active is the offboarding switch; if this check regressed, disabled accounts
    would keep working.
    """
    user = await ops.create_user(
        db,
        username="eve",
        password_hash=auth_service.hash_password("pw"),
        email="eve@example.com",
        role="user",
    )
    await ops.update_user(db, user.id, is_active=False)

    assert await auth_service.authenticate(db, "eve", "pw") is None


# ── refresh() ────────────────────────────────────────────────────────────────


async def test_refresh_success_returns_new_token_pair(db, auth_service):
    """A valid refresh token mints a new pair with the role re-read from the DB.

    Role freshness is the point of the refresh flow (see the no-role-claim test):
    the new access token must reflect the user's role *now*.
    """
    user = await ops.create_user(
        db,
        username="frank",
        password_hash=auth_service.hash_password("pw"),
        email="frank@example.com",
        role="admin",
    )
    refresh_token = auth_service.create_refresh_token(str(user.id))

    result = await auth_service.refresh(db, refresh_token)

    assert result is not None
    assert result["token_type"] == "bearer"
    access_payload = auth_service.decode_token(result["access_token"])
    assert access_payload["sub"] == str(user.id)
    # role is re-read from the user record on refresh
    assert access_payload["role"] == "admin"
    assert access_payload["type"] == "access"
    assert auth_service.decode_token(result["refresh_token"])["type"] == "refresh"


async def test_refresh_with_access_token_returns_none(db, auth_service):
    """Passing an access token where a refresh token is expected is rejected."""
    user = await ops.create_user(
        db,
        username="grace",
        password_hash=auth_service.hash_password("pw"),
        email="grace@example.com",
        role="user",
    )
    access_token = auth_service.create_access_token(str(user.id), "user")
    assert await auth_service.refresh(db, access_token) is None


async def test_refresh_invalid_token_returns_none(db, auth_service):
    """A malformed refresh token returns None rather than propagating a JWT error.

    The refresh route maps None to 401; an uncaught PyJWTError would surface as a
    500.
    """
    assert await auth_service.refresh(db, "garbage.token.value") is None


async def test_refresh_inactive_user_returns_none(db, auth_service):
    """Deactivating a user invalidates their outstanding refresh tokens.

    Without this, a user disabled mid-session could keep minting fresh access
    tokens for up to the refresh lifetime.
    """
    user = await ops.create_user(
        db,
        username="heidi",
        password_hash=auth_service.hash_password("pw"),
        email="heidi@example.com",
        role="user",
    )
    refresh_token = auth_service.create_refresh_token(str(user.id))
    await ops.update_user(db, user.id, is_active=False)

    assert await auth_service.refresh(db, refresh_token) is None


async def test_refresh_unknown_user_returns_none(db, auth_service):
    """A well-formed refresh token for a non-existent user yields None."""
    import uuid

    refresh_token = auth_service.create_refresh_token(str(uuid.uuid4()))
    assert await auth_service.refresh(db, refresh_token) is None


async def test_refresh_token_missing_type_claim_returns_none(db, auth_service):
    """A valid-signature token lacking a 'type' claim is not accepted as refresh."""
    import uuid

    user = await ops.create_user(
        db,
        username="ivan",
        password_hash=auth_service.hash_password("pw"),
        email="ivan@example.com",
        role="user",
    )
    # hand-craft a token that is signed correctly but has no "type" claim
    payload = {"sub": str(user.id)}
    typeless = jwt.encode(payload, auth_service._secret, algorithm="HS256")
    assert await auth_service.refresh(db, typeless) is None
    # sanity: the user genuinely exists, so the None is due to the missing type
    assert await ops.get_user_by_id(db, user.id) is not None


# ── get_current_user() ───────────────────────────────────────────────────────


async def test_get_current_user_success(db, auth_service):
    """A valid access token resolves to the corresponding User row.

    This is the dependency behind every authenticated route, so the resolved row
    must match the token's ``sub``.
    """
    user = await ops.create_user(
        db,
        username="judy",
        password_hash=auth_service.hash_password("pw"),
        email="judy@example.com",
        role="admin",
    )
    access = auth_service.create_access_token(str(user.id), "admin")

    resolved = await auth_service.get_current_user(db, access)
    assert resolved is not None
    assert str(resolved.id) == str(user.id)
    assert resolved.username == "judy"


async def test_get_current_user_rejects_refresh_token(db, auth_service):
    """A refresh token must not resolve a user via get_current_user (type guard)."""
    user = await ops.create_user(
        db,
        username="mallory",
        password_hash=auth_service.hash_password("pw"),
        email="mallory@example.com",
        role="user",
    )
    refresh = auth_service.create_refresh_token(str(user.id))
    assert await auth_service.get_current_user(db, refresh) is None


async def test_get_current_user_invalid_token_returns_none(db, auth_service):
    """A malformed bearer token resolves to None (route turns it into 401)."""
    assert await auth_service.get_current_user(db, "not.a.jwt") is None
