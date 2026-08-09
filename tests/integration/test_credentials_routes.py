"""Integration tests for the credential management routes.

SUT: ``packages/server/src/nexus_server/api/routes/credentials.py`` mounted at
``/api/credentials``. These exercise the real route handlers wired through the
real ``CredentialManager`` (with all real strategies) and the in-memory DB.

We never hit a real network: the one route that could (``POST .../test``)
dispatches to ``strategy.test_connection`` via ``manager.test`` — we monkeypatch
the shared manager's ``test`` method (a true external boundary) so no live
GitHub/S3 call is made. Everything else uses real validation, encryption, and
persistence.

Why the monkeypatching works
    ``conftest``'s ``credential_manager`` fixture is the SAME object the ``app``
    fixture installs at ``app.state.credential_manager``. Patching a method on
    the fixture therefore changes the manager the running route uses — no
    dependency override needed.

Secret-handling invariants asserted here (the point of this file)
    * Secrets are encrypted at rest and decrypt back to exactly what was POSTed
      (``test_create_persists_decryptable_fields`` — proves the route is not a
      metadata-only echo).
    * No response body — create or list — ever contains the raw secret, a
      ``fields`` key, or the ``encrypted_fields`` blob.
    * Every endpoint requires authentication.

Validation layering (mirrors the jobs routes)
    422 covers BOTH a bad enum value (Pydantic rejects an unknown
    ``credential_type`` before the manager is reached) and a strategy-level
    missing required field (the manager raises ``ValueError``, which the route
    maps to 422). 404 is reserved for an unknown credential id.

xfail policy
    Two DELETE tests are ``xfail(strict=True)`` against a real source bug in
    ``ops.delete_credential``. When the source is fixed, strict mode turns the
    now-passing tests into failures — that is the cue to remove the markers.
"""

from __future__ import annotations

import uuid

import pytest

from nexus_common.models.enums import CredentialType


# ── GET /api/credentials/types ───────────────────────────────────────────────


def test_list_types_returns_all_registered_strategies(auth_client):
    """The types endpoint lists every registered strategy with its metadata.

    Set-equality against the ``CredentialType`` enum catches drift in either
    direction: a new enum member added without a strategy (the UI would offer a
    type that cannot be created) or a strategy registered under a name the enum
    does not know. The length check additionally rules out duplicate entries.
    """
    resp = auth_client.get("/api/credentials/types")
    assert resp.status_code == 200

    body = resp.json()
    types = {t["credential_type"] for t in body}
    # Every CredentialType enum value has a backing strategy.
    assert types == {t.value for t in CredentialType}
    # One metadata entry per enum value, no duplicates.
    assert len(body) == len(CredentialType)


def test_list_types_exposes_required_and_optional_fields(auth_client):
    """Per-type metadata includes the strategy's required/optional fields.

    This metadata drives the dynamic credential form in the UI, so the exact
    field names and their required/optional split are a client-facing contract:
    dropping a required field from the list would render a form that always
    fails validation on submit.
    """
    resp = auth_client.get("/api/credentials/types")
    assert resp.status_code == 200
    by_type = {t["credential_type"]: t for t in resp.json()}

    git_pat = by_type["git_pat"]
    assert git_pat["required_fields"] == ["token"]
    assert git_pat["optional_fields"] == ["username"]
    assert "Personal Access Token" in git_pat["description"]

    s3 = by_type["s3"]
    assert s3["required_fields"] == ["endpoint", "access_key", "secret_key"]
    # AI Note: optional_fields is compared as a SET (unlike required_fields,
    # compared as an ordered list) because form ordering only matters for the
    # required inputs; do not "normalise" these two into the same style.
    assert set(s3["optional_fields"]) == {"region", "use_ssl"}


def test_list_types_requires_authentication(client):
    """Unauthenticated requests are rejected (bearer scheme enforced)."""
    resp = client.get("/api/credentials/types")
    assert resp.status_code in (401, 403)


# ── POST /api/credentials (create) ───────────────────────────────────────────


def test_create_git_pat_credential_succeeds(auth_client):
    """A git_pat credential with its required field is created (201)."""
    resp = auth_client.post(
        "/api/credentials",
        json={
            "name": "my-gh-token",
            "credential_type": "git_pat",
            "fields": {"token": "ghp_secret_value"},
            "description": "GitHub PAT",
        },
    )
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["name"] == "my-gh-token"
    assert body["credential_type"] == "git_pat"
    assert body["description"] == "GitHub PAT"
    assert body["is_shared"] is False
    # A real UUID id was assigned and an owner recorded.
    uuid.UUID(body["id"])
    uuid.UUID(body["owner_id"])


def test_create_basic_credential_succeeds(auth_client):
    """A basic (username/password) credential is created (201)."""
    resp = auth_client.post(
        "/api/credentials",
        json={
            "name": "svc-account",
            "credential_type": "basic",
            "fields": {"username": "svc", "password": "hunter2"},
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["credential_type"] == "basic"


def test_create_s3_credential_succeeds(auth_client):
    """An s3 credential with all required fields is created (201)."""
    resp = auth_client.post(
        "/api/credentials",
        json={
            "name": "minio-cred",
            "credential_type": "s3",
            "fields": {
                "endpoint": "minio.local:9000",
                "access_key": "AKIA",
                "secret_key": "shhh",
                "region": "us-west-2",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["credential_type"] == "s3"


def test_create_shared_credential_records_is_shared(auth_client):
    """is_shared=True is honoured and reflected back in the response."""
    resp = auth_client.post(
        "/api/credentials",
        json={
            "name": "shared-cred",
            "credential_type": "basic",
            "fields": {"username": "u", "password": "p"},
            "is_shared": True,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_shared"] is True


async def test_create_persists_decryptable_fields(auth_client, credential_manager, db):
    """A created credential is really encrypted-at-rest and decrypts back.

    Proves the route -> manager.store -> encryption path is real (not a
    metadata-only echo): we read the stored secret back through the manager's
    decrypt+strategy path and confirm it matches what we POSTed.

    The ``token_type`` assertion is the subtle one: it was never sent by the
    client. The git_pat strategy injects it while building the stored config,
    so seeing it on the way out proves the decrypt path returns the
    strategy-processed config rather than a raw echo of the request body.
    """
    created = auth_client.post(
        "/api/credentials",
        json={
            "name": "roundtrip-cred",
            "credential_type": "git_pat",
            "fields": {"token": "ghp_roundtrip", "username": "octocat"},
        },
    ).json()

    config = await credential_manager.get(db, created["id"])
    assert config["token"] == "ghp_roundtrip"
    assert config["username"] == "octocat"
    assert config["token_type"] == "pat"


def test_create_missing_required_field_other_strategy_is_422(auth_client):
    """A different strategy (smb) also rejects a missing required field (422)."""
    resp = auth_client.post(
        "/api/credentials",
        json={
            "name": "bad-smb",
            "credential_type": "smb",
            "fields": {"username": "u"},  # missing "password"
        },
    )
    assert resp.status_code == 422, resp.text
    assert "password" in resp.text


def test_create_response_never_leaks_secret_fields(auth_client):
    """The create response carries metadata only — no raw/encrypted secrets.

    Scans the RAW response text (not just the parsed keys) so a secret nested
    anywhere in the payload is caught, however it got there. Credentials are
    write-only by design: once stored, a secret is only ever decrypted
    server-side for use by a step, never returned to a client.
    """
    resp = auth_client.post(
        "/api/credentials",
        json={
            "name": "leak-check",
            "credential_type": "git_pat",
            "fields": {"token": "ghp_super_secret"},
        },
    )
    assert resp.status_code == 201
    serialized = resp.text
    assert "ghp_super_secret" not in serialized
    assert "fields" not in resp.json()
    assert "encrypted_fields" not in resp.json()


def test_create_missing_required_field_is_422(auth_client):
    """Omitting a strategy-required field surfaces as 422 (ValueError path)."""
    resp = auth_client.post(
        "/api/credentials",
        json={
            "name": "bad-pat",
            "credential_type": "git_pat",
            "fields": {},  # missing "token"
        },
    )
    assert resp.status_code == 422, resp.text
    assert "token" in resp.text


def test_create_unknown_credential_type_is_422(auth_client):
    """An unknown credential_type is rejected at schema validation (422).

    ``credential_type`` is a ``CredentialType`` enum on ``CredentialCreate``, so
    a bogus value never reaches the manager's KeyError path — Pydantic rejects
    it first with a 422.
    """
    resp = auth_client.post(
        "/api/credentials",
        json={
            "name": "weird",
            "credential_type": "not_a_real_type",
            "fields": {"token": "x"},
        },
    )
    assert resp.status_code == 422


def test_create_requires_authentication(client):
    """Creating a credential without auth is rejected."""
    resp = client.post(
        "/api/credentials",
        json={
            "name": "no-auth",
            "credential_type": "basic",
            "fields": {"username": "a", "password": "b"},
        },
    )
    assert resp.status_code in (401, 403)


# ── GET /api/credentials (list) ──────────────────────────────────────────────


def test_list_credentials_returns_created_entries(auth_client):
    """Listing returns previously created credentials by metadata."""
    auth_client.post(
        "/api/credentials",
        json={
            "name": "listed-cred",
            "credential_type": "basic",
            "fields": {"username": "u", "password": "p"},
        },
    )
    resp = auth_client.get("/api/credentials")
    assert resp.status_code == 200

    names = {c["name"] for c in resp.json()}
    assert "listed-cred" in names


def test_list_credentials_does_not_leak_secrets(auth_client):
    """The list response exposes metadata only — secrets stay server-side.

    The list is the higher-exposure surface (it returns every credential the
    caller can see, not just one they just created), so the same raw-text scan
    is applied. The positive subset check guards the opposite failure: an
    over-zealous serializer that strips so much the UI can no longer identify
    or attribute a credential.
    """
    auth_client.post(
        "/api/credentials",
        json={
            "name": "secret-holder",
            "credential_type": "git_pat",
            "fields": {"token": "ghp_do_not_leak_me"},
        },
    )
    resp = auth_client.get("/api/credentials")
    assert resp.status_code == 200
    assert "ghp_do_not_leak_me" not in resp.text
    for entry in resp.json():
        assert "fields" not in entry
        assert "encrypted_fields" not in entry
        # Sanity: the metadata that SHOULD be present is present.
        assert {"id", "name", "credential_type", "is_shared", "owner_id"} <= set(entry)


def test_list_credentials_requires_authentication(client):
    """Listing without auth is rejected."""
    resp = client.get("/api/credentials")
    assert resp.status_code in (401, 403)


# ── DELETE /api/credentials/{id} ─────────────────────────────────────────────


def test_delete_credential_removes_it(auth_client):
    """Deleting a credential returns 204 and removes it from the listing.

    Secret lifecycle matters: a credential the user believes they revoked must
    actually be gone. Currently blocked by the raw-UUID bind bug in
    ``ops.delete_credential`` — see the xfail reason above.
    """
    created = auth_client.post(
        "/api/credentials",
        json={
            "name": "to-delete",
            "credential_type": "basic",
            "fields": {"username": "u", "password": "p"},
        },
    ).json()
    cred_id = created["id"]

    resp = auth_client.delete(f"/api/credentials/{cred_id}")
    assert resp.status_code == 204

    listed = auth_client.get("/api/credentials").json()
    assert cred_id not in {c["id"] for c in listed}


def test_delete_missing_credential_is_404(auth_client):
    """Deleting a non-existent credential id returns 404.

    Even the "nothing to do" path currently 500s, which is what makes this bug
    unambiguous: the crash happens at id binding, before any row is looked up.
    """
    resp = auth_client.delete(f"/api/credentials/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_requires_authentication(client):
    """Deleting without auth is rejected."""
    resp = client.delete(f"/api/credentials/{uuid.uuid4()}")
    assert resp.status_code in (401, 403)


# ── POST /api/credentials/{id}/test ──────────────────────────────────────────


def test_test_credential_success_shape(auth_client, credential_manager, monkeypatch):
    """A passing connection test returns ``{"success": True}``.

    We monkeypatch the shared manager's ``test`` (the network boundary, which
    would otherwise call GitHub/S3) to return True. The app uses this exact
    manager instance via ``app.state.credential_manager``.

    The assertion INSIDE the stub is doing real work: it verifies the route
    forwards the path parameter unchanged to the manager. A route that passed
    the wrong id would still return ``{"success": True}`` and look fine.
    """
    created = auth_client.post(
        "/api/credentials",
        json={
            "name": "test-ok",
            "credential_type": "git_pat",
            "fields": {"token": "ghp_value"},
        },
    ).json()
    cred_id = created["id"]

    async def _fake_test(db, credential_id):
        """Stub for ``manager.test``; asserts the route forwarded the right id."""
        # Exercises that the route forwards the path param to the manager.
        assert str(credential_id) == cred_id
        return True

    # AI Note: setattr on the fixture instance (not the class) — the app holds
    # this very object on app.state, so the patch reaches the running route.
    # monkeypatch restores the real method at teardown.
    monkeypatch.setattr(credential_manager, "test", _fake_test)

    resp = auth_client.post(f"/api/credentials/{cred_id}/test")
    assert resp.status_code == 200
    assert resp.json() == {"success": True}


def test_test_credential_failure_shape(auth_client, credential_manager, monkeypatch):
    """A failing connection test (returns False) yields ``{"success": False}``.

    A failed credential check is a normal outcome, not an HTTP error — the UI
    renders it inline next to the credential, so the status stays 200.
    """
    created = auth_client.post(
        "/api/credentials",
        json={
            "name": "test-fail",
            "credential_type": "git_pat",
            "fields": {"token": "ghp_value"},
        },
    ).json()
    cred_id = created["id"]

    async def _fake_test(db, credential_id):
        """Stub ``manager.test`` reporting a clean negative result."""
        return False

    monkeypatch.setattr(credential_manager, "test", _fake_test)

    resp = auth_client.post(f"/api/credentials/{cred_id}/test")
    assert resp.status_code == 200
    assert resp.json() == {"success": False}


def test_test_credential_exception_returns_error_payload(
    auth_client, credential_manager, monkeypatch
):
    """A raised (non-KeyError) exception is caught and reported in the body.

    The handler swallows generic exceptions and returns
    ``{"success": False, "error": <msg>}`` with a 200 status.

    Deliberate design: a connection test hitting an unreachable host is
    diagnostic information for the operator, not a server fault. Returning 500
    would make the UI show a generic error banner instead of the actual reason
    ("connection refused"). Note the exception message IS surfaced to the
    caller — strategies must not put secret material in exception text.
    """
    created = auth_client.post(
        "/api/credentials",
        json={
            "name": "test-boom",
            "credential_type": "git_pat",
            "fields": {"token": "ghp_value"},
        },
    ).json()
    cred_id = created["id"]

    async def _boom(db, credential_id):
        """Stub ``manager.test`` raising the way a live connection failure would."""
        raise RuntimeError("connection refused")

    monkeypatch.setattr(credential_manager, "test", _boom)

    resp = auth_client.post(f"/api/credentials/{cred_id}/test")
    assert resp.status_code == 200
    assert resp.json() == {"success": False, "error": "connection refused"}


def test_test_missing_credential_is_404(auth_client):
    """Testing an unknown credential id surfaces the manager KeyError as 404.

    No monkeypatch here — the real manager raises KeyError for a missing id,
    which the route maps to 404.

    Safe to run unstubbed: the manager raises while *loading* the credential,
    so no strategy is ever constructed and no outbound connection is attempted.
    This also pins that KeyError is handled distinctly from the generic
    exception path above (404, not a 200 error payload).
    """
    resp = auth_client.post(f"/api/credentials/{uuid.uuid4()}/test")
    assert resp.status_code == 404


def test_test_requires_authentication(client):
    """Testing without auth is rejected."""
    resp = client.post(f"/api/credentials/{uuid.uuid4()}/test")
    assert resp.status_code in (401, 403)
