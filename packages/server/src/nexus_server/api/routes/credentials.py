"""Credential management routes — CRUD, test, type listing."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from nexus_common.models.schemas import CredentialCreate, CredentialInfo, CredentialTypeInfo
from nexus_server.api.deps import CredMgr, CurrentUser, DbSession
from nexus_server.db import ops

router = APIRouter()


def _cred_to_info(cred) -> CredentialInfo:
    return CredentialInfo(
        id=cred.id, name=cred.name, credential_type=cred.credential_type,
        description=cred.description, is_shared=cred.is_shared,
        owner_id=cred.owner_id, created_at=cred.created_at,
        updated_at=cred.updated_at,
    )


@router.get("/types", response_model=list[CredentialTypeInfo])
async def list_credential_types(mgr: CredMgr, user: CurrentUser):
    """List all supported credential types and their required fields."""
    return [CredentialTypeInfo(**t) for t in mgr.list_types()]


@router.get("", response_model=list[CredentialInfo])
async def list_credentials(db: DbSession, user: CurrentUser):
    """List all credentials (no secrets returned)."""
    creds = await ops.list_credentials(db)
    return [_cred_to_info(c) for c in creds]


@router.post("", response_model=CredentialInfo, status_code=status.HTTP_201_CREATED)
async def create_credential(body: CredentialCreate, db: DbSession, user: CurrentUser, mgr: CredMgr):
    """Store a new credential (fields are encrypted at rest)."""
    try:
        cred_id = await mgr.store(
            db, name=body.name, credential_type=body.credential_type.value,
            fields=body.fields, owner_id=user.id, is_shared=body.is_shared,
            allowed_groups=[str(g) for g in body.allowed_groups],
            description=body.description,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    cred = await ops.get_credential_by_id(db, cred_id)
    return _cred_to_info(cred)


@router.put("/{cred_id}", response_model=CredentialInfo)
async def update_credential(cred_id: UUID, body: CredentialCreate, db: DbSession, user: CurrentUser, mgr: CredMgr):
    """Update credential fields (re-encrypted)."""
    cred = await ops.get_credential_by_id(db, cred_id)
    if not cred:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")

    try:
        await mgr.update_fields(db, cred_id, body.fields)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    # Update non-encrypted metadata
    await ops.update_credential(
        db, cred_id,
        name=body.name, description=body.description,
        is_shared=body.is_shared,
        allowed_groups=[str(g) for g in body.allowed_groups],
    )
    cred = await ops.get_credential_by_id(db, cred_id)
    return _cred_to_info(cred)


@router.delete("/{cred_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(cred_id: UUID, db: DbSession, user: CurrentUser):
    """Delete a credential."""
    deleted = await ops.delete_credential(db, cred_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")


@router.post("/{cred_id}/test")
async def test_credential(cred_id: UUID, db: DbSession, user: CurrentUser, mgr: CredMgr):
    """Test that a stored credential can connect to its target."""
    try:
        success = await mgr.test(db, cred_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {"success": success}
