"""Storage Manager — orchestrates artifact storage across multiple backends.

The single stateful object that turns ``storage_backends`` DB rows into live
backend instances and keeps them in an in-process registry keyed by backend id.
Everything above it (``api/routes/storage.py``, job/artifact code) works with
backend *ids* and artifact *ids*; only this module knows about concrete backend
classes.

Responsibilities beyond raw I/O: it also owns the DB bookkeeping that must
accompany each operation — creating ``artifacts`` rows on upload and
``storage_transfers`` rows (plus their status transitions) on transfer. Backends
themselves are DB-unaware.

Lifecycle
---------
``nexus_server.main.lifespan`` constructs one instance with the shared
``CredentialManager`` and calls :meth:`StorageManager.init_backends` once at
startup (best-effort — startup continues if no backends are configured yet).
The instance is exposed to routes as the ``StorageMgr`` dependency.

IMPORTANT: the registry is populated only by ``init_backends`` at startup, so a
backend created or edited through the API afterwards is NOT picked up until the
server restarts. :meth:`get_backend` raises ``KeyError`` for such a backend, and
``routes/storage.py`` translates that into "Backend not initialized".
"""

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

#: Maps ``storage_backends.backend_type`` values to backend classes.
#:
#: AI Note: this table is currently only used as a *validity check* — the actual
#: instantiation happens in the explicit if/elif chain in
#: ``_create_backend_instance`` because each backend takes different constructor
#: kwargs. Adding an entry here without adding a branch there yields
#: "Unsupported backend type" at runtime, so both must be updated together.
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

    Holds a process-wide, in-memory registry (``{backend_id: instance}``) that
    is shared by all requests. Instances are cheap handles (a boto3 client, a
    mount path), but they are created once and never refreshed, so rotating a
    credential requires a server restart to take effect.
    """

    def __init__(self, credential_manager: CredentialManager):
        """Wire up the manager; no I/O happens here.

        Args:
            credential_manager: Shared ``CredentialManager`` used to decrypt each
                backend's credential into a client config at init time. Held by
                reference so credential strategy registration done later is
                still visible.
        """
        self._cred_manager = credential_manager
        self._backends: dict[UUID, StorageBackendBase] = {}

    async def init_backends(self, db: AsyncSession) -> None:
        """Load and initialize all active storage backends from DB.

        Called once from application startup. Inactive rows
        (``is_active = False``) are skipped, and a backend that fails to
        construct — bad credential, unreachable NAS mount, unknown type — is
        logged and skipped rather than aborting startup, so one broken backend
        cannot take the whole server down.

        Args:
            db: Async session used to read the ``storage_backends`` rows and the
                associated credentials.

        Side effects:
            Populates ``self._backends``; constructing a backend may perform
            network I/O (``MinIOBackend`` creates its bucket if missing) and
            filesystem checks (``NASBackend`` validates the mount path exists).

        Note:
            Not idempotent-safe against concurrent calls, and re-running it adds
            to the existing registry rather than replacing it. It is only ever
            invoked once, from ``lifespan``.
        """
        backends = await ops.list_storage_backends(db)
        for backend_model in backends:
            if not backend_model.is_active:
                continue
            try:
                instance = await self._create_backend_instance(db, backend_model)
                self._backends[backend_model.id] = instance
                logger.info(f"Initialized storage backend: {backend_model.name}")
            except Exception as e:
                # AI Note: broad catch is intentional — a misconfigured backend
                # must not prevent the server from booting. The failure is only
                # visible in the log and via the health-check endpoint.
                logger.error(f"Failed to initialize backend {backend_model.name}: {e}")

    async def _create_backend_instance(self, db: AsyncSession, backend_model) -> StorageBackendBase:
        """Create a backend instance from DB model.

        Args:
            db: Async session, used only to resolve the credential.
            backend_model: A ``StorageBackend`` ORM row. ``config`` is a free-form
                JSON blob whose expected keys depend on ``backend_type``
                (``bucket`` for minio/s3, ``mount_path`` for nas).

        Returns:
            A ready-to-use backend instance.

        Raises:
            ValueError: Unknown/unsupported ``backend_type``.
            KeyError: ``config["mount_path"]`` missing for a NAS backend, or the
                referenced credential does not exist (from ``CredentialManager``).
            Exception: Whatever the backend constructor raises — e.g.
                ``FileNotFoundError`` for a NAS mount that is not present, or a
                botocore error while ensuring the bucket exists.

        Note:
            The credential is decrypted on every call, so plaintext secrets exist
            only as a transient dict handed straight to the backend constructor.
        """
        cred_config = await self._cred_manager.get(db, backend_model.credential_id)
        backend_cls = BACKEND_CLASSES.get(backend_model.backend_type)
        if not backend_cls:
            raise ValueError(f"Unknown backend type: {backend_model.backend_type}")

        config = backend_model.config or {}
        if backend_model.backend_type in ("minio", "s3"):
            # AI Note: cred_config is splatted into boto3.client() by the
            # backend, so the credential strategy must emit exactly boto3's
            # kwarg names (endpoint_url, aws_access_key_id, ...). A stray key
            # here surfaces as a confusing boto3 TypeError at startup.
            return MinIOBackend(
                name=backend_model.name,
                client_config=cred_config,
                bucket=config.get("bucket", "nexus-artifacts"),
            )
        elif backend_model.backend_type == "nas":
            # AI Note: direct indexing (not .get) — a NAS backend without a
            # mount_path is unrecoverable, so fail loudly at init rather than
            # silently writing artifacts to a wrong/relative location.
            return NASBackend(
                name=backend_model.name,
                mount_path=config["mount_path"],
            )
        else:
            # Reachable only if BACKEND_CLASSES gains a type without a matching
            # branch above — see the note on BACKEND_CLASSES.
            raise ValueError(f"Unsupported backend type: {backend_model.backend_type}")

    def get_backend(self, backend_id: UUID) -> StorageBackendBase:
        """Get an initialized backend instance by ID.

        Args:
            backend_id: Primary key of the ``storage_backends`` row. In practice
                a ``str`` (the DB stores ids as ``String(36)``) — the registry
                is keyed by whatever ``backend_model.id`` returned, so lookups
                must use the same representation, not a converted ``UUID``.

        Returns:
            The live backend instance.

        Raises:
            KeyError: The backend was inactive at startup, failed to initialize,
                or was created after the server booted. ``routes/storage.py``
                catches this and reports "Backend not initialized".
        """
        if backend_id not in self._backends:
            raise KeyError(f"Backend {backend_id} not initialized")
        return self._backends[backend_id]

    async def get_default_backend(self, db: AsyncSession) -> tuple[UUID, StorageBackendBase]:
        """Get the default storage backend.

        Resolves the row flagged ``is_default AND is_active`` and returns its
        live instance. Used by :meth:`upload_artifact` when the caller does not
        pin a backend.

        Args:
            db: Async session.

        Returns:
            ``(backend_id, backend_instance)``.

        Raises:
            RuntimeError: No active default backend row exists.
            KeyError: A default row exists but its instance was never
                initialized (see :meth:`get_backend`).
        """
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
        """Upload a file and create an artifact record. Returns artifact ID.

        Args:
            db: Async session; the ``artifacts`` row is committed by
                ``ops.create_artifact``.
            local_path: File on the server's filesystem to upload. Its
                ``.name`` is stored as the artifact's display filename.
            remote_key: Backend key to write under. Must be unique per artifact
                — backends overwrite silently, and transfers reuse this key on
                the destination.
            job_id: Owning job; artifacts are listed per job in the UI.
            step_run_id: Optional owning step run, for per-step attribution.
            uploaded_by: Optional user id for audit purposes.
            backend_id: Pin a specific backend. Defaults to the configured
                default backend.
            content_type: MIME override; otherwise backend-guessed.

        Returns:
            The new artifact's id.

        Side effects:
            Writes to the storage backend (network or filesystem I/O) and then
            inserts + commits an ``artifacts`` row.

        Note:
            Not atomic — if the DB insert fails after a successful upload, the
            object is left orphaned in the backend with no artifact row
            referencing it. There is no reconciliation/GC pass today.
        """
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
        """Download an artifact to a local path.

        Args:
            db: Async session, used to look up where the artifact lives.
            artifact_id: Artifact row id.
            local_path: Destination on the server's filesystem; parent
                directories are created by the backend and an existing file is
                overwritten.

        Raises:
            KeyError: No such artifact row, or its backend is not initialized.
            Exception: Backend-specific error if the object is missing from the
                backend even though the row exists (possible after a manual
                deletion, since nothing keeps the two in sync).
        """
        artifact = await ops.get_artifact_by_id(db, artifact_id)
        if not artifact:
            raise KeyError(f"Artifact {artifact_id} not found")

        backend = self.get_backend(artifact.storage_backend_id)
        await backend.download(artifact.storage_key, local_path)

    async def transfer_artifact(
        self, db: AsyncSession, artifact_id: UUID, dest_backend_id: UUID,
        requested_by: UUID | None = None, delete_source: bool = False,
    ) -> UUID:
        """Start transferring an artifact between backends. Returns transfer ID.

        Despite the name this runs the transfer to completion *inline* — the
        awaiting HTTP request (``POST /api/storage/transfer``) blocks for the
        whole copy. The ``storage_transfers`` row exists so progress/outcome is
        queryable, and so the work can later be moved to a background task
        without changing the API shape.

        Args:
            db: Async session; several intermediate commits happen through
                ``ops.create_transfer`` / ``ops.update_transfer``.
            artifact_id: Artifact to move.
            dest_backend_id: Target backend; must be initialized.
            requested_by: Optional user id recorded on the transfer row.
            delete_source: Remove the object from the source backend after a
                successful copy, turning the copy into a move.

        Returns:
            The ``storage_transfers`` row id (already in a terminal state).

        Raises:
            KeyError: Unknown artifact, or source/destination backend not
                initialized.
            Exception: Re-raised after the transfer row is marked ``failed``, so
                the caller sees the error *and* the failure is recorded.

        Side effects:
            Creates a transfer row, flips it ``pending`` -> ``in_progress`` ->
            ``completed``/``failed``, copies the object (via
            ``StorageBackendBase.stream_to``, which stages the whole object in
            the system temp dir), optionally deletes the source object, and
            repoints ``artifacts.storage_backend_id`` at the destination.

        Note:
            Ordering is deliberate: the source object is deleted BEFORE the
            artifact row is repointed. A crash in that window leaves the row
            pointing at a backend where the object no longer exists.
            ``bytes_transferred`` and the artifact repoint are also committed
            separately, so the operation is not atomic overall.
        """
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

            # AI Note: the destination key is the SAME as the source key, so a
            # transfer never rewrites keys. That keeps `artifacts.storage_key`
            # valid after the repoint below — but it also means transferring
            # into a backend that already holds that key overwrites it.
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
            # AI Note: mutating the ORM object directly (it is still attached to
            # this session) rather than going through ops.* — the explicit
            # commit below is what persists it. Dropping that commit would
            # silently leave the artifact pointing at the old backend.
            # AI Note: str() is load-bearing — dest_backend_id arrives as a
            # uuid.UUID from the route, but storage_backend_id is String(36).
            # Binding the raw UUID made aiosqlite raise "type 'UUID' is not
            # supported" on this UPDATE, which poisoned the session and left the
            # transfer wedged. Same bug class as ops._sid().
            artifact.storage_backend_id = str(dest_backend_id)
            await db.commit()

        except Exception as e:
            # AI Note: the transfer row is marked failed and the exception is
            # re-raised so the HTTP layer can turn it into a 4xx/5xx. Note the
            # row may already say "completed" if the failure happened during
            # delete_source or the final commit — the status is then corrected
            # back to "failed" even though the copy itself did succeed.
            await ops.update_transfer(db, transfer.id, status="failed", error=str(e))
            raise

        return transfer.id
