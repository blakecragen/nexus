"""Storage management routes — backends, health, transfers.

Role in the system
------------------
Mounted at ``/api/storage``. Nexus stores job artifacts (result tarballs, logs,
gem5 ``m5out`` bundles) on pluggable *storage backends* — MinIO/S3, Google
Drive, a NAS mount, etc. This module is the control plane for those backends
and for moving artifacts between them.

Two halves:

- **Backend registry** (``/backends*``): CRUD over ``StorageBackend`` rows plus
  a liveness probe. All mutations are admin-only; reads are open to any
  authenticated user.
- **Transfers** (``/transfer``, ``/transfers``): copy an existing artifact from
  its current backend to another, optionally deleting the source.

Neighbouring modules
--------------------
- ``nexus_server.services.storage.manager.StorageManager`` (injected as
  ``StorageMgr``) holds the *initialized client instances* keyed by backend ID
  and performs the actual byte movement.
- ``nexus_server.services.credentials.manager.CredentialManager`` supplies the
  decrypted secrets a backend needs; every backend row references a
  ``credential_id``.
- ``nexus_server.db.ops`` owns the rows; ``routes/artifacts.py`` serves the
  downloads.
- Frontend ``frontend/src/pages/Storage.tsx`` is the primary consumer.

AI Note: the DB row and the live client instance are two different things and
they can disagree. ``StorageManager.init_backends()`` runs once at startup, so
a backend registered or edited afterwards has a row but no initialized
instance until the server restarts. That split is why several handlers here
catch ``KeyError`` from ``mgr.get_backend()`` and report "Backend not
initialized" rather than 404.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from nexus_common.models.schemas import (
    StorageBackendCreate,
    StorageBackendInfo,
    TransferInfo,
    TransferRequest,
)
from nexus_server.api.deps import AdminUser, CurrentUser, DbSession, StorageMgr
from nexus_server.db import ops

router = APIRouter()


def _backend_to_info(backend) -> StorageBackendInfo:
    """Project a ``StorageBackend`` ORM row to the public API shape.

    Args:
        backend: A ``StorageBackend`` ORM instance.

    Returns:
        StorageBackendInfo: The serializable view of the backend.

    AI Note: ``config`` is echoed back verbatim (defaulted to ``{}`` when
    NULL). It is expected to hold only non-secret connection settings —
    endpoint URL, bucket, region — because real secrets live in the referenced
    ``Credential`` row. Anything secret written into ``config`` by a caller
    WILL be readable by any authenticated user via ``GET /backends``.

    AI Note: ``StorageBackendInfo.used_bytes`` is never set here, so it always
    serializes as its default of 0. Current usage is not tracked; the UI's
    capacity bar is therefore always empty.
    """
    return StorageBackendInfo(
        id=backend.id, name=backend.name, backend_type=backend.backend_type,
        config=backend.config or {}, credential_id=backend.credential_id,
        capacity_bytes=backend.capacity_bytes, is_default=backend.is_default,
        is_active=backend.is_active, priority=backend.priority,
        created_at=backend.created_at,
    )


def _transfer_to_info(t) -> TransferInfo:
    """Project a ``StorageTransfer`` ORM row to the public API shape.

    Args:
        t: A ``StorageTransfer`` ORM instance.

    Returns:
        TransferInfo: The serializable view, including progress
        (``bytes_transferred``), terminal ``status``, and ``error`` text when
        the copy failed.
    """
    return TransferInfo(
        id=t.id, artifact_id=t.artifact_id,
        source_backend_id=t.source_backend_id, dest_backend_id=t.dest_backend_id,
        status=t.status, bytes_transferred=t.bytes_transferred,
        error=t.error, started_at=t.started_at, completed_at=t.completed_at,
    )


@router.get("/backends", response_model=list[StorageBackendInfo])
async def list_backends(db: DbSession, user: CurrentUser):
    """List all storage backends with usage info.

    Args:
        db: Request-scoped DB session.
        user: Any authenticated user.

    Returns:
        list[StorageBackendInfo]: Every registered backend row, initialized or
        not. A backend appearing here does NOT imply the manager holds a
        working client for it — use ``GET /backends/{id}/health`` for that.
    """
    backends = await ops.list_storage_backends(db)
    return [_backend_to_info(b) for b in backends]


@router.post("/backends", response_model=StorageBackendInfo, status_code=status.HTTP_201_CREATED)
async def register_backend(body: StorageBackendCreate, db: DbSession, admin: AdminUser):
    """Register a new storage backend (admin only).

    Persists the backend definition. Does not connect to it or validate the
    credential's contents beyond checking that the credential row exists.

    Args:
        body: Backend name, type (``"minio"`` / ``"s3"`` / ``"gdrive"`` /
            ``"nas"``), type-specific ``config``, referenced ``credential_id``,
            optional ``capacity_bytes``, ``is_default`` and ``priority``.
        db: Request-scoped DB session (a row is committed).
        admin: Enforces admin role.

    Returns:
        StorageBackendInfo: The newly created backend.

    Raises:
        HTTPException: 400 if ``credential_id`` does not resolve to an existing
            credential.

    Note:
        A newly registered backend is not usable for uploads or transfers until
        the server restarts and initializes a client for it.
    """
    # AI Note: 400 (not 404) is used for the missing credential because the
    # resource being addressed — the backend collection — does exist; it is the
    # submitted body that is bad. Clients distinguish this from "backend not
    # found" on other routes.
    #
    # AI Note: the new backend is NOT initialized in the StorageManager, so
    # uploads/transfers targeting it raise KeyError (surfaced as 404 or
    # "Backend not initialized") until init_backends() runs again at startup.
    #
    # AI Note: is_default=True is not enforced to be exclusive here, so two
    # backends can both claim default. ops.get_default_storage_backend then
    # returns whichever the query orders first.
    # Verify credential exists
    cred = await ops.get_credential_by_id(db, body.credential_id)
    if not cred:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Credential {body.credential_id} not found",
        )
    backend = await ops.create_storage_backend(
        db,
        name=body.name, backend_type=body.backend_type,
        config=body.config, credential_id=body.credential_id,
        capacity_bytes=body.capacity_bytes, is_default=body.is_default,
        priority=body.priority,
    )
    return _backend_to_info(backend)


@router.put("/backends/{backend_id}", response_model=StorageBackendInfo)
async def update_backend(backend_id: UUID, body: StorageBackendCreate, db: DbSession, admin: AdminUser):
    """Update a storage backend configuration (admin only).

    Args:
        backend_id: Backend UUID from the path.
        body: Full replacement values. Reuses ``StorageBackendCreate``, so this
            is a PUT in the true sense — every field except
            ``capacity_bytes`` is overwritten unconditionally.
        db: Request-scoped DB session (row is mutated and committed).
        admin: Enforces admin role.

    Returns:
        StorageBackendInfo: The updated backend.

    Raises:
        HTTPException: 404 if the backend does not exist.

    Note:
        Configuration changes take effect for new connections only after the
        server restarts.
    """
    # AI Note: unlike register_backend, this does NOT verify that the new
    # credential_id refers to an existing credential. Pointing a backend at a
    # deleted credential fails later, at connect time, as a health-check error
    # rather than a 400 here.
    #
    # AI Note: edits apply to the DB row only. The already-initialized client
    # instance in StorageManager keeps using the OLD config until the server
    # restarts, so a config fix will not appear to take effect immediately.
    backend = await ops.get_storage_backend_by_id(db, backend_id)
    if not backend:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backend not found")
    backend.name = body.name
    backend.backend_type = body.backend_type
    backend.config = body.config
    # AI Note: str() is load-bearing. body.credential_id is typed UUID | None,
    # but the column is String(36) — assigning the raw UUID object made
    # aiosqlite raise "type 'UUID' is not supported" on commit, 500ing this
    # endpoint. Mirrors the _sid() coercion ops.py applies on the insert path.
    backend.credential_id = str(body.credential_id) if body.credential_id is not None else None
    # AI Note: capacity is the one field guarded by a None check — omitting it
    # preserves the existing value instead of clearing it. Every other field
    # here is a hard overwrite.
    if body.capacity_bytes is not None:
        backend.capacity_bytes = body.capacity_bytes
    backend.is_default = body.is_default
    backend.priority = body.priority
    await db.commit()
    await db.refresh(backend)
    return _backend_to_info(backend)


@router.delete("/backends/{backend_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backend(backend_id: UUID, db: DbSession, admin: AdminUser):
    """Delete a storage backend (admin only).

    Args:
        backend_id: Backend UUID from the path.
        db: Request-scoped DB session (row is deleted and committed).
        admin: Enforces admin role.

    Raises:
        HTTPException: 404 if the backend does not exist.

    Note:
        Artifacts stored on this backend are not migrated first and become
        unreachable through Nexus. Transfer them elsewhere before deleting.
    """
    # AI Note: destructive and unguarded — no check that the backend still
    # holds artifacts. The remote bytes remain, but Nexus can no longer resolve
    # a client to fetch them.
    backend = await ops.get_storage_backend_by_id(db, backend_id)
    if not backend:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backend not found")
    await db.delete(backend)
    await db.commit()


@router.get("/backends/{backend_id}/health")
async def check_backend_health(backend_id: UUID, db: DbSession, user: CurrentUser, mgr: StorageMgr):
    """Check health/connectivity of a storage backend.

    Performs a live probe against the backend's remote service (network I/O),
    so this can be slow if the endpoint is unreachable.

    Args:
        backend_id: Backend UUID from the path.
        db: Request-scoped DB session (used only to resolve the row's name).
        user: Any authenticated user.
        mgr: The storage manager holding initialized client instances.

    Returns:
        dict: ``{"backend_id", "name", "healthy"}`` and, when unhealthy, an
        ``"error"`` string.

    Raises:
        HTTPException: 404 only if no backend *row* exists.

    Note:
        An unreachable backend is reported as HTTP 200 with ``healthy: false``,
        not as an error status.
    """
    # AI Note: the 200-with-healthy:false convention is intentional — the UI
    # polls this for a status dot and needs to distinguish "no such backend"
    # (404) from "backend is down" (200 + healthy:false).
    backend_model = await ops.get_storage_backend_by_id(db, backend_id)
    if not backend_model:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backend not found")
    try:
        instance = mgr.get_backend(backend_id)
        healthy = await instance.health_check()
        return {"backend_id": str(backend_id), "name": backend_model.name, "healthy": healthy}
    except KeyError:
        # AI Note: distinct from a generic failure — the row exists but
        # StorageManager.init_backends() never built a client for it. Almost
        # always means the backend was registered after server startup.
        return {"backend_id": str(backend_id), "name": backend_model.name, "healthy": False, "error": "Backend not initialized"}
    except Exception as exc:
        # AI Note: deliberately broad. Backend SDKs (boto3, google-api,
        # filesystem) raise wildly different exception types, and a health
        # probe must never propagate a 500 — the caller only wants a boolean.
        # The trade-off is that genuine bugs in health_check() are reported as
        # "unhealthy" rather than crashing loudly.
        return {"backend_id": str(backend_id), "name": backend_model.name, "healthy": False, "error": str(exc)}


@router.post("/transfer", response_model=TransferInfo, status_code=status.HTTP_201_CREATED)
async def start_transfer(body: TransferRequest, db: DbSession, user: CurrentUser, mgr: StorageMgr):
    """Initiate an artifact transfer between storage backends.

    Despite the name, ``StorageManager.transfer_artifact`` runs the copy
    *inline* and only returns once it has completed or failed — so this request
    blocks for the duration of the byte transfer.

    Args:
        body: ``artifact_id``, ``dest_backend_id``, and ``delete_source``
            (when True the artifact is removed from its original backend after
            a successful copy).
        db: Request-scoped DB session (transfer row created and updated).
        user: Any authenticated user; recorded as ``requested_by``.
        mgr: Storage manager that performs the stream-to-stream copy.

    Returns:
        TransferInfo: The already-terminal transfer record.

    Raises:
        HTTPException: 404 if the artifact or a backend is unknown
            (``KeyError`` from the manager); 400 if the manager rejects the
            request (``RuntimeError``, e.g. no default backend configured);
            500 if the transfer record cannot be found after creation.

    Note:
        The returned record is already in a terminal state — the copy runs
        inline, so large artifacts hold the connection open for its duration
        and a client timeout does not cancel it.
    """
    # AI Note: the route name and 201 Created are misleading — there is no
    # background task and no polling. By the time this returns, `status` is
    # already "completed" or "failed".
    #
    # AI Note: KeyError is overloaded by the manager — it means both "artifact
    # row not found" and "backend not initialized in this process". Both
    # surface as 404, so a 404 here does not prove the destination backend is
    # unregistered; it may just need a server restart.
    try:
        transfer_id = await mgr.transfer_artifact(
            db, artifact_id=body.artifact_id, dest_backend_id=body.dest_backend_id,
            requested_by=user.id, delete_source=body.delete_source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Fetch the completed transfer record
    # AI Note: linear scan over ALL transfers because ops has no
    # get_transfer_by_id. Cost grows with transfer history; replace with a
    # direct lookup if this table ever gets large.
    transfers = await ops.list_transfers(db)
    for t in transfers:
        if t.id == transfer_id:
            return _transfer_to_info(t)
    # Fallback — should not happen
    # AI Note: unreachable unless the row was deleted concurrently or the id
    # types stopped comparing equal (UUID vs str). If this 500 ever fires, look
    # at `_sid()` normalization in db/ops.py first.
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Transfer record not found after creation")


@router.get("/transfers", response_model=list[TransferInfo])
async def list_transfers(db: DbSession, user: CurrentUser, transfer_status: str | None = None):
    """List storage transfers, optionally filtered by status.

    Args:
        db: Request-scoped DB session.
        user: Any authenticated user — transfers are not scoped to their
            requester, so everyone sees the full history.
        transfer_status: Filter by ``pending`` / ``in_progress`` /
            ``completed`` / ``failed``. Named ``transfer_status`` rather than
            ``status`` because ``status`` is bound to the imported FastAPI
            status-code module in this file.

    Returns:
        list[TransferInfo]: Matching transfers, newest first
        (``started_at DESC``, NULLs last).
    """
    # AI Note: unpaginated. The result set grows without bound as transfers
    # accumulate; add a limit before running this at scale.
    transfers = await ops.list_transfers(db, status=transfer_status)
    return [_transfer_to_info(t) for t in transfers]
