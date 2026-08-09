"""Artifact routes — list artifacts produced by a job.

Mounted at ``/api/artifacts`` by ``nexus_server.main.create_app()``.

An ``Artifact`` row is the server-side *index entry* for a file that a step
produced and pushed into a configured storage backend (local disk, S3, SMB, …
see ``nexus_server.services.storage``). The row records where the bytes live
(``storage_backend_id`` + ``storage_key``) and what they are (filename, content
type, size); the bytes themselves are never stored in the database, and this
module never streams them. Downloading is the storage layer's job
(``/api/storage/...``).

Do not confuse artifacts with the *job results tarball* handled in
``routes/jobs.py`` (``/api/jobs/{job_id}/results``). That path is a separate,
simpler mechanism: agents ``PUT`` a single ``results.tar.gz`` straight onto the
server's local ``.nexus-results/`` directory with no storage backend and no
``Artifact`` row involved.

Neighbours:
    * ``nexus_server.db.ops.list_artifacts_for_job`` — the only query used here.
    * ``nexus_common.models.schemas.ArtifactInfo`` — the wire shape.
    * ``nexus_server.api.deps`` — session/auth dependencies.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from nexus_common.models.schemas import ArtifactInfo
from nexus_server.api.deps import CurrentUser, DbSession
from nexus_server.db import ops

router = APIRouter()


def _artifact_to_info(a) -> ArtifactInfo:
    """Project an ``Artifact`` ORM row onto the public ``ArtifactInfo`` schema.

    The mapping is written out field by field rather than using Pydantic's
    ``from_attributes`` so that adding a column to the ``artifacts`` table can
    never silently widen the API response.

    Args:
        a: A ``nexus_server.db.models.Artifact`` row. Untyped to avoid importing
            the ORM models into the route layer; only attribute access is used,
            so any object with the same fields works (handy in tests).

    Returns:
        ArtifactInfo: Serialisable view of the artifact. ``storage_backend_name``
        is deliberately left unset (it defaults to ``None``) — resolving it would
        require a join per row, and the frontend already has the backend list
        from ``/api/storage`` to look the name up client-side.

    Notes:
        ``size_bytes`` is coerced with ``or 0`` because the column is nullable in
        practice for artifacts registered before their upload finished, while the
        schema declares a non-optional ``int``. Without the coercion those rows
        would raise a Pydantic validation error at response time.
    """
    return ArtifactInfo(
        id=a.id, job_id=a.job_id, step_run_id=a.step_run_id,
        filename=a.filename, storage_backend_id=a.storage_backend_id,
        storage_key=a.storage_key, content_type=a.content_type,
        size_bytes=a.size_bytes or 0, created_at=a.created_at,
    )


@router.get("", response_model=list[ArtifactInfo])
async def list_artifacts(db: DbSession, user: CurrentUser, job_id: UUID):
    """List artifacts produced by a job.

    Serves ``GET /api/artifacts?job_id=<uuid>``. ``job_id`` is a *required*
    query parameter (it has no default), so there is intentionally no way to
    enumerate every artifact in the cluster through this endpoint.

    Args:
        db: Async session, injected.
        user: Authenticated caller, injected.
        job_id: Job whose artifacts to return.

    Returns:
        list[ArtifactInfo]: Artifacts ordered oldest-first by ``created_at``
        (ordering comes from ``ops.list_artifacts_for_job``). An unknown or
        artifact-less ``job_id`` yields an empty list rather than a 404 — the
        frontend relies on that to render "no artifacts" without special-casing.
    """
    # AI Note (authorization): CurrentUser is authentication only. There is no
    # per-job or per-pool ownership check, so any logged-in user can enumerate
    # any job's artifacts. Intentional for this single-tenant cluster; add a
    # deps.require_pool_access-style guard if that assumption changes.
    artifacts = await ops.list_artifacts_for_job(db, job_id)
    return [_artifact_to_info(a) for a in artifacts]
