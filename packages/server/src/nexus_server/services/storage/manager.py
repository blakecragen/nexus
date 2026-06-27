"""Storage Manager — orchestrates artifact storage across multiple backends."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_server.db import ops
from nexus_server.services.credentials.manager import CredentialManager
from nexus_server.services.storage.base import StorageBackendBase
from nexus_server.services.storage.minio_backend import MinIOBackend
from nexus_server.services.storage.nas_backend import NASBackend

logger = logging.getLogger(__name__)

BACKEND_CLASSES: dict[str, type] = {
    "minio": MinIOBackend,
    "s3": MinIOBackend,  # Generic S3 uses the same boto3 backend
    "nas": NASBackend,
}


class StorageManager:
    """Manages multiple storage backends and routes artifacts between them.

    Responsibilities:
    - Initialize backend instances from DB config + credential manager
    - Route uploads to appropriate backend based on size/policy
    - Transfer artifacts between backends
    - Track where each artifact lives
    """

    def __init__(self, credential_manager: CredentialManager):
        self._cred_manager = credential_manager
        self._backends: dict[UUID, StorageBackendBase] = {}

    async def init_backends(self, db: AsyncSession) -> None:
        """Load and initialize all active storage backends from DB."""
        backends = await ops.list_storage_backends(db)
        for backend_model in backends:
            if not backend_model.is_active:
                continue
            try:
                instance = await self._create_backend_instance(db, backend_model)
                self._backends[backend_model.id] = instance
                logger.info(f"Initialized storage backend: {backend_model.name}")
            except Exception as e:
                logger.error(f"Failed to initialize backend {backend_model.name}: {e}")

    async def _create_backend_instance(self, db: AsyncSession, backend_model) -> StorageBackendBase:
        """Create a backend instance from DB model."""
        cred_config = await self._cred_manager.get(db, backend_model.credential_id)
        backend_cls = BACKEND_CLASSES.get(backend_model.backend_type)
        if not backend_cls:
            raise ValueError(f"Unknown backend type: {backend_model.backend_type}")

        config = backend_model.config or {}
        if backend_model.backend_type in ("minio", "s3"):
            return MinIOBackend(
                name=backend_model.name,
                client_config=cred_config,
                bucket=config.get("bucket", "nexus-artifacts"),
            )
        elif backend_model.backend_type == "nas":
            return NASBackend(
                name=backend_model.name,
                mount_path=config["mount_path"],
            )
        else:
            raise ValueError(f"Unsupported backend type: {backend_model.backend_type}")

    def get_backend(self, backend_id: UUID) -> StorageBackendBase:
        """Get an initialized backend instance by ID."""
        if backend_id not in self._backends:
            raise KeyError(f"Backend {backend_id} not initialized")
        return self._backends[backend_id]

    async def get_default_backend(self, db: AsyncSession) -> tuple[UUID, StorageBackendBase]:
        """Get the default storage backend."""
        backend_model = await ops.get_default_storage_backend(db)
        if not backend_model:
            raise RuntimeError("No default storage backend configured")
        return backend_model.id, self.get_backend(backend_model.id)

    async def upload_artifact(
        self, db: AsyncSession, local_path: Path, remote_key: str,
        job_id: UUID, step_run_id: UUID | None = None,
        uploaded_by: UUID | None = None,
        backend_id: UUID | None = None,
        content_type: str | None = None,
    ) -> UUID:
        """Upload a file and create an artifact record. Returns artifact ID."""
        if backend_id:
            bid = backend_id
            backend = self.get_backend(bid)
        else:
            bid, backend = await self.get_default_backend(db)

        ref = await backend.upload(local_path, remote_key, content_type)

        artifact = await ops.create_artifact(
            db,
            job_id=job_id,
            step_run_id=step_run_id,
            filename=local_path.name,
            storage_backend_id=bid,
            storage_key=ref.key,
            content_type=ref.content_type,
            size_bytes=ref.size_bytes,
            uploaded_by=uploaded_by,
        )
        return artifact.id

    async def download_artifact(self, db: AsyncSession, artifact_id: UUID, local_path: Path) -> None:
        """Download an artifact to a local path."""
        artifact = await ops.get_artifact_by_id(db, artifact_id)
        if not artifact:
            raise KeyError(f"Artifact {artifact_id} not found")

        backend = self.get_backend(artifact.storage_backend_id)
        await backend.download(artifact.storage_key, local_path)

    async def transfer_artifact(
        self, db: AsyncSession, artifact_id: UUID, dest_backend_id: UUID,
        requested_by: UUID | None = None, delete_source: bool = False,
    ) -> UUID:
        """Start transferring an artifact between backends. Returns transfer ID."""
        artifact = await ops.get_artifact_by_id(db, artifact_id)
        if not artifact:
            raise KeyError(f"Artifact {artifact_id} not found")

        transfer = await ops.create_transfer(
            db,
            artifact_id=artifact_id,
            source_backend_id=artifact.storage_backend_id,
            dest_backend_id=dest_backend_id,
            requested_by=requested_by,
        )

        # Execute transfer (could be made async/background)
        try:
            await ops.update_transfer(db, transfer.id, status="in_progress")
            source = self.get_backend(artifact.storage_backend_id)
            dest = self.get_backend(dest_backend_id)

            bytes_transferred = await source.stream_to(
                artifact.storage_key, dest, artifact.storage_key,
            )

            # Update artifact to point to new backend
            await ops.update_transfer(
                db, transfer.id,
                status="completed",
                bytes_transferred=bytes_transferred,
            )

            if delete_source:
                await source.delete(artifact.storage_key)

            # Update artifact record to point to destination
            artifact.storage_backend_id = dest_backend_id
            await db.commit()

        except Exception as e:
            await ops.update_transfer(db, transfer.id, status="failed", error=str(e))
            raise

        return transfer.id
