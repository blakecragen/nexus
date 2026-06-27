"""Storage management routes — backends, health, transfers."""

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
    return StorageBackendInfo(
        id=backend.id, name=backend.name, backend_type=backend.backend_type,
        config=backend.config or {}, credential_id=backend.credential_id,
        capacity_bytes=backend.capacity_bytes, is_default=backend.is_default,
        is_active=backend.is_active, priority=backend.priority,
        created_at=backend.created_at,
    )


def _transfer_to_info(t) -> TransferInfo:
    return TransferInfo(
        id=t.id, artifact_id=t.artifact_id,
        source_backend_id=t.source_backend_id, dest_backend_id=t.dest_backend_id,
        status=t.status, bytes_transferred=t.bytes_transferred,
        error=t.error, started_at=t.started_at, completed_at=t.completed_at,
    )


@router.get("/backends", response_model=list[StorageBackendInfo])
async def list_backends(db: DbSession, user: CurrentUser):
    """List all storage backends with usage info."""
    backends = await ops.list_storage_backends(db)
    return [_backend_to_info(b) for b in backends]


@router.post("/backends", response_model=StorageBackendInfo, status_code=status.HTTP_201_CREATED)
async def register_backend(body: StorageBackendCreate, db: DbSession, admin: AdminUser):
    """Register a new storage backend (admin only)."""
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
    """Update a storage backend configuration (admin only)."""
    backend = await ops.get_storage_backend_by_id(db, backend_id)
    if not backend:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backend not found")
    backend.name = body.name
    backend.backend_type = body.backend_type
    backend.config = body.config
    backend.credential_id = body.credential_id
    if body.capacity_bytes is not None:
        backend.capacity_bytes = body.capacity_bytes
    backend.is_default = body.is_default
    backend.priority = body.priority
    await db.commit()
    await db.refresh(backend)
    return _backend_to_info(backend)


@router.delete("/backends/{backend_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backend(backend_id: UUID, db: DbSession, admin: AdminUser):
    """Delete a storage backend (admin only)."""
    backend = await ops.get_storage_backend_by_id(db, backend_id)
    if not backend:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backend not found")
    await db.delete(backend)
    await db.commit()


@router.get("/backends/{backend_id}/health")
async def check_backend_health(backend_id: UUID, db: DbSession, user: CurrentUser, mgr: StorageMgr):
    """Check health/connectivity of a storage backend."""
    backend_model = await ops.get_storage_backend_by_id(db, backend_id)
    if not backend_model:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backend not found")
    try:
        instance = mgr.get_backend(backend_id)
        healthy = await instance.health_check()
        return {"backend_id": str(backend_id), "name": backend_model.name, "healthy": healthy}
    except KeyError:
        return {"backend_id": str(backend_id), "name": backend_model.name, "healthy": False, "error": "Backend not initialized"}
    except Exception as exc:
        return {"backend_id": str(backend_id), "name": backend_model.name, "healthy": False, "error": str(exc)}


@router.post("/transfer", response_model=TransferInfo, status_code=status.HTTP_201_CREATED)
async def start_transfer(body: TransferRequest, db: DbSession, user: CurrentUser, mgr: StorageMgr):
    """Initiate an artifact transfer between storage backends."""
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
    transfers = await ops.list_transfers(db)
    for t in transfers:
        if t.id == transfer_id:
            return _transfer_to_info(t)
    # Fallback — should not happen
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Transfer record not found after creation")


@router.get("/transfers", response_model=list[TransferInfo])
async def list_transfers(db: DbSession, user: CurrentUser, transfer_status: str | None = None):
    """List storage transfers, optionally filtered by status."""
    transfers = await ops.list_transfers(db, status=transfer_status)
    return [_transfer_to_info(t) for t in transfers]
