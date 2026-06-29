"""Artifact routes — list artifacts produced by a job."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from nexus_common.models.schemas import ArtifactInfo
from nexus_server.api.deps import CurrentUser, DbSession
from nexus_server.db import ops

router = APIRouter()


def _artifact_to_info(a) -> ArtifactInfo:
    return ArtifactInfo(
        id=a.id, job_id=a.job_id, step_run_id=a.step_run_id,
        filename=a.filename, storage_backend_id=a.storage_backend_id,
        storage_key=a.storage_key, content_type=a.content_type,
        size_bytes=a.size_bytes or 0, created_at=a.created_at,
    )


@router.get("", response_model=list[ArtifactInfo])
async def list_artifacts(db: DbSession, user: CurrentUser, job_id: UUID):
    """List artifacts produced by a job."""
    artifacts = await ops.list_artifacts_for_job(db, job_id)
    return [_artifact_to_info(a) for a in artifacts]
