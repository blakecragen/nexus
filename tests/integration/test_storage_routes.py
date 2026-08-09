"""Integration tests for the storage management routes.

SUT: ``packages/server/src/nexus_server/api/routes/storage.py`` — mounted at
``/api/storage``. These tests exercise the hermetic code paths only: listing /
creating / updating / deleting backends, the per-backend health check, and the
transfer flow. We never touch a real MinIO/NAS object store — the backend
*instances* that the StorageManager would normally build from boto3 / a NAS
mount are replaced with in-memory fakes, and the only piece of network/IO
behaviour we stub is ``StorageBackendBase.health_check`` / ``.stream_to``.

Stubs (true external boundaries only):
  * ``_FakeBackend.health_check`` — would otherwise open a real MinIO/NAS conn.
  * ``_FakeBackend.stream_to``   — would otherwise copy real bytes between stores.
Everything else (routes, ops, DB, schema validation) is the real SUT.

The backend-instance registry (essential background for these tests)
    ``StorageBackend`` rows in the DB are *configuration*. The live objects that
    actually talk to MinIO/NAS live in ``StorageManager._backends``, a dict
    populated at startup by ``init_backends``. Because the ``app`` fixture skips
    the production lifespan, that dict starts empty — so any test exercising
    health or transfer must inject a fake instance itself. That is what
    ``_register_fake`` / ``_register_fake_both`` do.

    The KEY TYPE of that dict is where the bugs live. ``init_backends`` stores
    instances under the ``str`` id, while ``check_backend_health`` looks them up
    with the path-converted ``UUID``. The two never match. Which key a helper
    uses is therefore a deliberate choice per test:
      * ``_register_fake``      → UUID key: works around the mismatch to test
                                  the health logic itself.
      * ``_register_fake_both`` → both keys: the transfer flow looks the source
                                  up by a str (from the ORM) and the dest by a
                                  UUID (from the request).
      * raw ``str`` key         → used by exactly one test, which documents the
                                  real production keying and the latent bug.

Known source bugs (all the same family)
    Three route paths pass a raw ``uuid.UUID`` into a ``String(36)`` column and
    the SQLite driver refuses to bind it: ``register_backend``,
    ``update_backend`` and the ``create_transfer`` call inside
    ``StorageManager.transfer_artifact``. Affected tests are
    ``xfail(strict=True)`` with the offending function named; strict mode makes
    a future fix announce itself as an unexpected pass.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from nexus_server.db import ops


# ── Helpers / fixtures ────────────────────────────────────────────────────


class _FakeBackend:
    """Stand-in for a real StorageBackendBase instance.

    The route layer only ever calls ``health_check`` (health endpoint) and the
    manager only calls ``stream_to`` / ``delete`` during a transfer. We make
    those configurable so each test drives the behaviour it needs without any
    real network IO.

    Deliberately does NOT subclass ``StorageBackendBase`` — duck typing keeps
    the fake from inheriting real constructor requirements (boto3 clients, NAS
    mount paths) that would defeat the point.

    Attributes:
        name: Label used in health responses.
        deleted_keys: Records ``delete`` calls, so a test can assert the source
            object was (or was not) removed after a move-style transfer.
    """

    def __init__(self, name: str, *, healthy: bool = True, health_exc: Exception | None = None,
                 stream_bytes: int = 0):
        """Configure the fake's responses.

        Args:
            name: Backend display name.
            healthy: What ``health_check`` returns when it does not raise.
            health_exc: If set, ``health_check`` raises this instead —
                simulating an unreachable store (the route must catch it and
                report the message rather than 500).
            stream_bytes: Byte count ``stream_to`` reports, which the transfer
                record stores as ``bytes_transferred``.
        """
        self.name = name
        self._healthy = healthy
        self._health_exc = health_exc
        self._stream_bytes = stream_bytes
        self.deleted_keys: list[str] = []

    async def health_check(self) -> bool:
        """Report health, or raise the configured exception.

        Returns:
            The configured ``healthy`` flag.

        Raises:
            Exception: Whatever ``health_exc`` was constructed with.
        """
        if self._health_exc is not None:
            raise self._health_exc
        return self._healthy

    async def stream_to(self, src_key, dest_backend, dest_key) -> int:
        """Pretend to copy an object to another backend.

        Args:
            src_key: Source object key (ignored).
            dest_backend: Destination backend instance (ignored).
            dest_key: Destination object key (ignored).

        Returns:
            The configured byte count, standing in for bytes actually copied.
        """
        return self._stream_bytes

    async def delete(self, key: str) -> None:
        """Record a delete instead of removing a real object.

        Args:
            key: Object key the manager asked to remove.
        """
        self.deleted_keys.append(key)


@pytest_asyncio.fixture
async def credential(db, admin_user):
    """A persisted credential that backends can reference.

    Every ``StorageBackend`` has a ``credential_id`` FK, so backend tests need
    one to exist. The encrypted blob is a dummy — nothing here decrypts it,
    because the real backend instances are replaced by ``_FakeBackend``.

    Returns:
        The persisted ``Credential`` owned by ``admin_user``.
    """
    return await ops.create_credential(
        db,
        name="minio-cred",
        credential_type="minio",
        encrypted_fields=b"encrypted-blob",
        owner_id=admin_user.id,
    )


async def _make_backend(db, credential, *, name="primary", backend_type="minio",
                        is_default=False, priority=10, config=None):
    """Persist a StorageBackend row via the real ops layer.

    ``credential_id`` is coerced to ``str`` here because the SQLite driver can't
    bind a raw ``UUID`` (see the register-backend xfail for the route-level bug).

    Seeding through ops rather than the POST route is what lets the read/health/
    delete tests run green while the register route's UUID bug is still open.

    Args:
        db: Test session.
        credential: Credential the backend references.
        name: Backend name (unique per test; also drives sort-order tests).
        backend_type: ``minio`` or ``nas``.
        is_default: Marks this as the default upload target.
        priority: Failover ordering — lower sorts first.
        config: Backend-specific settings; defaults to a bucket config.

    Returns:
        The persisted ``StorageBackend``.
    """
    return await ops.create_storage_backend(
        db,
        name=name,
        backend_type=backend_type,
        config=config or {"bucket": "nexus-artifacts"},
        credential_id=str(credential.id),
        is_default=is_default,
        priority=priority,
    )


def _register_fake(app, backend, fake):
    """Register a fake backend instance under the key the route looks up.

    The health/transfer routes call ``mgr.get_backend(backend_id)`` with the
    path-converted ``UUID`` object, so we key the live manager's ``_backends``
    map by that same ``UUID`` to exercise the initialized path.

    Args:
        app: The FastAPI app holding the live ``StorageManager``.
        backend: The persisted backend row whose id becomes the key.
        fake: The ``_FakeBackend`` instance to install.

    Side effects:
        Mutates ``app.state.storage_manager._backends`` (a private attribute)
        for the remainder of the test.
    """
    # AI Note: the UUID key is a TEST WORKAROUND, not what production does.
    # init_backends keys by str(id) (manager.py L48) while the route looks up by
    # UUID, so real deployments always miss. Using the UUID key here lets these
    # tests exercise the health-reporting logic instead of stopping at the
    # keying bug — which is documented separately by
    # test_health_check_str_keyed_instance_reports_not_initialized.
    app.state.storage_manager._backends[uuid.UUID(str(backend.id))] = fake


# ── GET /backends (list) ───────────────────────────────────────────────────


async def test_list_backends_empty(auth_client):
    """With no backends configured the list endpoint returns an empty array."""
    resp = auth_client.get("/api/storage/backends")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_backends_returns_persisted_backends(auth_client, db, credential):
    """Persisted backends are serialised into StorageBackendInfo objects.

    Doubles as the full-shape contract test for ``StorageBackendInfo``: every
    field the Storage admin page renders is asserted once here, including the
    two easily-confused defaults — ``capacity_bytes`` stays ``None`` when never
    configured, while ``used_bytes`` defaults to ``0`` in the schema.
    """
    b1 = await _make_backend(db, credential, name="zeta", priority=20)
    b2 = await _make_backend(db, credential, name="alpha", priority=5)

    resp = auth_client.get("/api/storage/backends")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # ops orders by (priority, name) — alpha(5) before zeta(20).
    assert [b["name"] for b in body] == ["alpha", "zeta"]
    ids = {b["id"] for b in body}
    assert {str(b1.id), str(b2.id)} == ids
    # Full StorageBackendInfo shape check on the first (alpha) row.
    alpha = body[0]
    assert alpha["id"] == str(b2.id)
    assert alpha["backend_type"] == "minio"
    assert alpha["credential_id"] == str(credential.id)
    assert alpha["config"] == {"bucket": "nexus-artifacts"}
    assert alpha["priority"] == 5
    assert alpha["is_active"] is True
    assert alpha["is_default"] is False
    # capacity_bytes was never set → None; used_bytes defaults to 0 in the schema.
    assert alpha["capacity_bytes"] is None
    assert alpha["used_bytes"] == 0
    assert "created_at" in alpha and alpha["created_at"] is not None


async def test_list_backends_requires_auth(client):
    """The list endpoint is gated behind authentication."""
    resp = client.get("/api/storage/backends")
    assert resp.status_code in (401, 403)


# ── POST /backends (create) ────────────────────────────────────────────────


async def test_register_backend_creates_row(admin_client, credential):
    """Admin can register a backend referencing an existing credential.

    XFAIL — SOURCE BUG: ``register_backend`` passes ``body.credential_id`` (a
    ``UUID`` object from the parsed schema) straight into
    ``ops.create_storage_backend``, which stores it on the model unchanged. The
    SQLite driver cannot bind a raw ``UUID`` → ``ProgrammingError: type 'UUID'
    is not supported``. The route should coerce ``credential_id`` to ``str``
    (the credentials route already does ``str(...)`` for its UUID fields). Same
    class of bug as the documented UUID/SQLite issue.
    """
    payload = {
        "name": "new-backend",
        "backend_type": "minio",
        "config": {"bucket": "my-bucket"},
        "credential_id": str(credential.id),
        "capacity_bytes": 1024,
        "is_default": True,
        "priority": 3,
    }
    resp = admin_client.post("/api/storage/backends", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "new-backend"
    assert body["backend_type"] == "minio"
    assert body["config"] == {"bucket": "my-bucket"}
    assert body["credential_id"] == str(credential.id)
    assert body["capacity_bytes"] == 1024
    assert body["is_default"] is True
    assert body["priority"] == 3
    assert body["is_active"] is True
    # It should now be listable.
    listing = admin_client.get("/api/storage/backends").json()
    assert any(b["id"] == body["id"] for b in listing)


async def test_register_backend_unknown_credential_400(admin_client, db):
    """Registering against a non-existent credential is a 400 and writes nothing.

    Referential-integrity guard at the route layer: a backend row pointing at a
    missing credential could never be instantiated, so it would sit in the
    admin UI looking configured while every upload through it failed. Passes
    today (unlike the happy path) because the credential check runs before the
    raw-UUID bind that breaks ``register_backend``.
    """
    payload = {
        "name": "orphan-backend",
        "backend_type": "minio",
        "config": {},
        "credential_id": str(uuid.uuid4()),
    }
    resp = admin_client.post("/api/storage/backends", json=payload)
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"].lower()
    # The credential check happens before any write → no backend row persisted.
    assert await ops.list_storage_backends(db) == []


async def test_register_backend_requires_admin(auth_client, db, credential):
    """A non-admin user cannot register a backend and no row is written.

    The empty-table assertion proves the RBAC dependency rejects the request
    before the handler body runs, rather than after a partial write.
    """
    payload = {
        "name": "forbidden-backend",
        "backend_type": "minio",
        "config": {},
        "credential_id": str(credential.id),
    }
    resp = auth_client.post("/api/storage/backends", json=payload)
    assert resp.status_code == 403
    assert await ops.list_storage_backends(db) == []


async def test_register_backend_unauthenticated_rejected(client, credential):
    """An unauthenticated request to create a backend is rejected (401/403)."""
    payload = {
        "name": "anon-backend",
        "backend_type": "minio",
        "config": {},
        "credential_id": str(credential.id),
    }
    resp = client.post("/api/storage/backends", json=payload)
    assert resp.status_code in (401, 403)


async def test_register_backend_validation_error_422(admin_client):
    """Missing required fields is a schema validation (422) error."""
    resp = admin_client.post("/api/storage/backends", json={"name": "incomplete"})
    assert resp.status_code == 422


# ── PUT /backends/{id} (update) ─────────────────────────────────────────────


async def test_update_backend_not_found_404(admin_client, credential):
    """Updating a non-existent backend is a 404 (checked before any write)."""
    payload = {
        "name": "ghost",
        "backend_type": "minio",
        "config": {},
        "credential_id": str(credential.id),
    }
    resp = admin_client.put(f"/api/storage/backends/{uuid.uuid4()}", json=payload)
    assert resp.status_code == 404


async def test_update_backend_requires_admin(auth_client, db, credential):
    """A non-admin user cannot update a backend (403 before any lookup/write)."""
    backend = await _make_backend(db, credential, name="locked")
    payload = {
        "name": "renamed",
        "backend_type": "minio",
        "config": {},
        "credential_id": str(credential.id),
    }
    resp = auth_client.put(f"/api/storage/backends/{backend.id}", json=payload)
    assert resp.status_code == 403


async def test_update_backend_persists_changes(admin_client, db, credential):
    """Admin can update a backend's fields and the changes round-trip.

    XFAIL — SOURCE BUG: ``update_backend`` writes ``body.credential_id`` (a
    ``UUID`` object from the parsed schema) straight onto the model and commits.
    The ``credential_id`` column is ``String(36)`` and the SQLite driver cannot
    bind a raw ``UUID`` → ``ProgrammingError``. The route should ``str(...)`` it
    like the credentials route does.
    """
    backend = await _make_backend(db, credential, name="before", priority=10)
    payload = {
        "name": "after",
        "backend_type": "nas",
        "config": {"mount_path": "/mnt/store"},
        "credential_id": str(credential.id),
        "capacity_bytes": 2048,
        "is_default": True,
        "priority": 1,
    }
    resp = admin_client.put(f"/api/storage/backends/{backend.id}", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(backend.id)
    assert body["name"] == "after"
    assert body["backend_type"] == "nas"
    assert body["config"] == {"mount_path": "/mnt/store"}
    assert body["capacity_bytes"] == 2048
    assert body["is_default"] is True
    assert body["priority"] == 1
    # Change must be visible via the listing too.
    listing = admin_client.get("/api/storage/backends").json()
    names = {b["id"]: b["name"] for b in listing}
    assert names[str(backend.id)] == "after"


# ── DELETE /backends/{id} ──────────────────────────────────────────────────


async def test_delete_backend_removes_row(admin_client, db, credential):
    """Deleting an existing backend returns 204 and removes it from listing.

    ``resp.content == b""`` is asserted because a 204 must carry no body — some
    clients (and strict proxies) reject a 204 with content.
    """
    backend = await _make_backend(db, credential, name="to-delete")
    resp = admin_client.delete(f"/api/storage/backends/{backend.id}")
    assert resp.status_code == 204
    assert resp.content == b""

    listing = admin_client.get("/api/storage/backends").json()
    assert all(b["id"] != str(backend.id) for b in listing)


async def test_delete_backend_not_found_404(admin_client):
    """Deleting a non-existent backend is a 404."""
    resp = admin_client.delete(f"/api/storage/backends/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_delete_backend_requires_admin(auth_client, db, credential):
    """A non-admin user cannot delete a backend."""
    backend = await _make_backend(db, credential, name="protected")
    resp = auth_client.delete(f"/api/storage/backends/{backend.id}")
    assert resp.status_code == 403


# ── GET /backends/{id}/health ──────────────────────────────────────────────


async def test_health_check_healthy(auth_client, app, db, credential):
    """A registered, healthy backend reports healthy=True.

    Uses the UUID-key workaround (``_register_fake``) so the keying mismatch
    documented below does not mask the health-reporting logic itself.
    """
    backend = await _make_backend(db, credential, name="healthy-be")
    # Register a fake instance in the live StorageManager (avoids real MinIO).
    fake = _FakeBackend("healthy-be", healthy=True)
    _register_fake(app, backend, fake)

    resp = auth_client.get(f"/api/storage/backends/{backend.id}/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is True
    assert body["name"] == "healthy-be"
    assert body["backend_id"] == str(backend.id)


async def test_health_check_unhealthy(auth_client, app, db, credential):
    """A backend whose health_check returns False reports healthy=False.

    Separates "the store answered and said it is unwell" from the raise case
    (which carries an ``error``) and from the not-initialized case.
    """
    backend = await _make_backend(db, credential, name="down-be")
    _register_fake(app, backend, _FakeBackend("down-be", healthy=False))

    resp = auth_client.get(f"/api/storage/backends/{backend.id}/health")
    assert resp.status_code == 200
    assert resp.json()["healthy"] is False


async def test_health_check_not_initialized(auth_client, db, credential):
    """A backend row that has no live instance reports the not-initialized error.

    Configuration exists but no live client was built (the usual cause: the
    backend failed to initialise at startup). Returning 200 with
    ``healthy=False`` rather than an HTTP error is deliberate — the admin page
    renders a red status dot per backend, and an error status would break the
    whole listing.
    """
    backend = await _make_backend(db, credential, name="uninit-be")
    # Deliberately do NOT register a fake instance → manager raises KeyError.
    resp = auth_client.get(f"/api/storage/backends/{backend.id}/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is False
    assert body["error"] == "Backend not initialized"
    assert body["name"] == "uninit-be"
    assert body["backend_id"] == str(backend.id)


async def test_health_check_instance_raises(auth_client, app, db, credential):
    """If the backend health check throws, the error string is surfaced.

    An unreachable store is operational information, not a server fault, so the
    exception message is passed through in ``error`` with a 200. Distinguishing
    "connection refused" from "Backend not initialized" is what tells an
    operator whether to fix the network or the configuration.
    """
    backend = await _make_backend(db, credential, name="boom-be")
    fake = _FakeBackend("boom-be", health_exc=RuntimeError("connection refused"))
    _register_fake(app, backend, fake)

    resp = auth_client.get(f"/api/storage/backends/{backend.id}/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is False
    assert "connection refused" in body["error"]


async def test_health_check_backend_not_found_404(auth_client):
    """Health check on an unknown backend id is a 404."""
    resp = auth_client.get(f"/api/storage/backends/{uuid.uuid4()}/health")
    assert resp.status_code == 404


async def test_health_check_str_keyed_instance_reports_not_initialized(
    auth_client, app, db, credential
):
    """LATENT SOURCE BUG: an instance keyed exactly as init_backends keys it
    (by the str id) is NOT found by the route's UUID lookup.

    ``StorageManager.init_backends`` stores instances under ``backend_model.id``
    (a ``str`` — see manager.py L48), but ``check_backend_health`` calls
    ``mgr.get_backend(backend_id)`` with the path-converted ``UUID``. A
    str-keyed dict never matches a ``UUID`` key, so ``get_backend`` raises
    ``KeyError`` and health *always* reports "Backend not initialized" in
    production even for a fully-initialised backend. This test documents that
    real keying mismatch (it does NOT use the UUID-key workaround the healthy
    tests rely on). It passes today because the route swallows the KeyError, but
    the reported ``healthy=False`` is the latent bug's user-visible symptom.

    AI Note: this test PASSES on purpose while describing broken behaviour —
    it is a characterization test. Do not "fix" it by switching to
    ``_register_fake`` (the UUID-key workaround); that would delete the only
    executable record of the production keying mismatch. When the manager is
    corrected to use one consistent key type, this test's expectations flip to
    ``healthy=True`` and the docstring should be rewritten accordingly.
    """
    backend = await _make_backend(db, credential, name="strkey-be")
    # Key under the str id — exactly how init_backends registers real instances.
    app.state.storage_manager._backends[str(backend.id)] = _FakeBackend(
        "strkey-be", healthy=True
    )

    resp = auth_client.get(f"/api/storage/backends/{backend.id}/health")
    assert resp.status_code == 200
    body = resp.json()
    # Despite a healthy instance being registered, the UUID lookup misses it.
    assert body["healthy"] is False
    assert body["error"] == "Backend not initialized"
    assert body["name"] == "strkey-be"


async def test_health_check_requires_auth(client):
    """The health endpoint is gated behind authentication."""
    resp = client.get(f"/api/storage/backends/{uuid.uuid4()}/health")
    assert resp.status_code in (401, 403)


# ── POST /transfer + GET /transfers ────────────────────────────────────────


def _register_fake_both(app, backend, fake):
    """Register a fake under both str and UUID keys.

    The transfer route looks up the *source* backend by ``artifact.storage_backend_id``
    (a ``str`` from the ORM) and the *destination* by the path/body ``UUID``. Keying
    both forms means a single fake satisfies whichever lookup the manager performs.

    Args:
        app: The FastAPI app holding the live ``StorageManager``.
        backend: The persisted backend row whose id supplies both keys.
        fake: The ``_FakeBackend`` instance to install under both keys.

    Side effects:
        Adds TWO entries to ``_backends`` pointing at the same object.
    """
    # AI Note: the dual-keying is itself evidence of the underlying design
    # problem — the registry is indexed inconsistently across call sites. Once
    # the manager settles on one key type, this helper collapses into
    # _register_fake.
    app.state.storage_manager._backends[str(backend.id)] = fake
    app.state.storage_manager._backends[uuid.UUID(str(backend.id))] = fake


async def _make_artifact_on(db, backend, user):
    """Persist a job + an artifact that lives on the given backend.

    A transfer needs a real artifact to move, and an artifact needs a parent
    job, so both are created here.

    Args:
        db: Test session.
        backend: The backend the artifact currently lives on (becomes the
            transfer's implicit source).
        user: Submitter of the owning job.

    Returns:
        The persisted ``Artifact``.

    Side effects:
        INSERTs one job row and one artifact row. ``storage_backend_id`` is
        stored as ``str`` — this is what makes the source lookup a str-keyed
        one during transfer.
    """
    job = await ops.create_job(
        db,
        name="transfer-job",
        submitted_by=user.id,
        steps_config=[],
    )
    return await ops.create_artifact(
        db,
        job_id=job.id,
        filename="result.tar.gz",
        storage_backend_id=str(backend.id),
        storage_key="result.tar.gz",
        size_bytes=4242,
    )


# AI Note: the transfer-write tests below used to be xfail(strict) for the
# UUID/SQLite bind bug — ops.create_transfer wrote raw ``UUID`` objects onto
# String(36) columns, and StorageManager.transfer_artifact did the same on
# ``artifact.storage_backend_id``. Both are fixed (ops._sid_kwargs coerces every
# ``*_id`` kwarg; the manager str()s the destination id), so these now guard the
# fixed behaviour directly. Keep them unmarked — a regression must fail loudly.


async def test_start_transfer_success(auth_client, app, db, credential, regular_user):
    """A transfer between two registered backends completes and is returned.

    The end-to-end artifact-migration path: read from source, stream to
    destination, record a completed ``StorageTransfer`` with the byte count.
    ``bytes_transferred == 4242`` must come from what ``stream_to`` reported,
    not from the artifact's recorded ``size_bytes`` — they are equal here only
    because the fake was configured to match.
    """
    src = await _make_backend(db, credential, name="src-be", priority=1)
    dest = await _make_backend(db, credential, name="dest-be", priority=2)
    artifact = await _make_artifact_on(db, src, regular_user)

    # Register fake instances; stub the actual byte copy.
    _register_fake_both(app, src, _FakeBackend("src-be", stream_bytes=4242))
    _register_fake_both(app, dest, _FakeBackend("dest-be"))

    payload = {"artifact_id": str(artifact.id), "dest_backend_id": str(dest.id)}
    resp = auth_client.post("/api/storage/transfer", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["artifact_id"] == str(artifact.id)
    assert body["source_backend_id"] == str(src.id)
    assert body["dest_backend_id"] == str(dest.id)
    assert body["status"] == "completed"
    assert body["bytes_transferred"] == 4242
    assert body["error"] is None


async def test_start_transfer_unknown_artifact_404(auth_client):
    """Transferring a non-existent artifact surfaces the manager KeyError as 404.

    Passes today (unmarked) because the artifact lookup happens before the
    ``create_transfer`` call that trips the raw-UUID bind bug.
    """
    payload = {"artifact_id": str(uuid.uuid4()), "dest_backend_id": str(uuid.uuid4())}
    resp = auth_client.post("/api/storage/transfer", json=payload)
    assert resp.status_code == 404


async def test_start_transfer_dest_not_initialized_500(auth_client, app, db, credential, regular_user):
    """An uninitialised destination backend should surface as a 404 (KeyError).

    Intended behaviour: transfer_artifact calls get_backend(dest) inside its
    try-block, which raises KeyError → the route maps it to 404. In practice the
    same create_transfer raw-UUID bind bug fires *before* the dest lookup, so the
    request 500s instead. Marked xfail with the shared transfer bug.
    """
    src = await _make_backend(db, credential, name="src2-be", priority=1)
    dest = await _make_backend(db, credential, name="dest2-be", priority=2)
    artifact = await _make_artifact_on(db, src, regular_user)

    # Register only the source; destination instance intentionally absent.
    _register_fake_both(app, src, _FakeBackend("src2-be", stream_bytes=10))

    payload = {"artifact_id": str(artifact.id), "dest_backend_id": str(dest.id)}
    resp = auth_client.post("/api/storage/transfer", json=payload)
    # Intended (once the source bug is fixed): get_backend KeyError → 404.
    assert resp.status_code == 404


async def test_list_transfers_after_transfer(auth_client, app, db, credential, regular_user):
    """A completed transfer shows up in the transfers listing.

    Confirms the transfer record is persisted, not just echoed in the POST
    response — the Storage page's activity list reads from this endpoint.
    """
    src = await _make_backend(db, credential, name="src3-be", priority=1)
    dest = await _make_backend(db, credential, name="dest3-be", priority=2)
    artifact = await _make_artifact_on(db, src, regular_user)
    _register_fake_both(app, src, _FakeBackend("src3-be", stream_bytes=99))
    _register_fake_both(app, dest, _FakeBackend("dest3-be"))

    auth_client.post(
        "/api/storage/transfer",
        json={"artifact_id": str(artifact.id), "dest_backend_id": str(dest.id)},
    )

    resp = auth_client.get("/api/storage/transfers")
    assert resp.status_code == 200
    transfers = resp.json()
    assert len(transfers) == 1
    assert transfers[0]["artifact_id"] == str(artifact.id)
    assert transfers[0]["status"] == "completed"


async def test_list_transfers_status_filter(auth_client, app, db, credential, regular_user):
    """The status query param filters the transfers listing.

    Both a matching and a non-matching filter are exercised so a filter that is
    silently ignored (returning everything) fails on the ``pending`` case.
    """
    src = await _make_backend(db, credential, name="src4-be", priority=1)
    dest = await _make_backend(db, credential, name="dest4-be", priority=2)
    artifact = await _make_artifact_on(db, src, regular_user)
    _register_fake_both(app, src, _FakeBackend("src4-be", stream_bytes=1))
    _register_fake_both(app, dest, _FakeBackend("dest4-be"))
    auth_client.post(
        "/api/storage/transfer",
        json={"artifact_id": str(artifact.id), "dest_backend_id": str(dest.id)},
    )

    # Matching filter returns the completed transfer.
    completed = auth_client.get("/api/storage/transfers?transfer_status=completed")
    assert completed.status_code == 200
    assert len(completed.json()) == 1

    # Non-matching filter returns nothing.
    pending = auth_client.get("/api/storage/transfers?transfer_status=pending")
    assert pending.status_code == 200
    assert pending.json() == []


async def test_list_transfers_empty(auth_client):
    """No transfers → empty list, with or without a status filter.

    An empty table must yield a well-formed ``[]`` rather than null or an
    error, both with and without a filter — the UI renders the array directly.
    """
    resp = auth_client.get("/api/storage/transfers")
    assert resp.status_code == 200
    assert resp.json() == []
    # A status filter on an empty table is still a well-formed empty list.
    filtered = auth_client.get("/api/storage/transfers?transfer_status=completed")
    assert filtered.status_code == 200
    assert filtered.json() == []


async def test_list_transfers_requires_auth(client):
    """Listing transfers requires authentication."""
    resp = client.get("/api/storage/transfers")
    assert resp.status_code in (401, 403)
