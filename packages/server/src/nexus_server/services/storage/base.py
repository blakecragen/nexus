"""Storage backend ABC — all storage backends implement this interface.

Defines the contract that ``StorageManager`` programs against, so the rest of
the server never learns whether an artifact lives in S3/MinIO, on a NAS mount,
or somewhere added later. Two exports:

- :class:`StorageRef` — the small value object an upload returns.
- :class:`StorageBackendBase` — the abstract interface (eight abstract methods
  plus a concrete :meth:`~StorageBackendBase.stream_to` default).

Implementations live alongside this module (``minio_backend``, ``nas_backend``)
and are constructed by ``storage.manager.StorageManager._create_backend_instance``.

Interface conventions every implementation must honour
------------------------------------------------------
- ``remote_key`` is an opaque, backend-independent, ``/``-separated path-like
  string. The same key is reused verbatim when transferring between backends,
  so backends must not rewrite or namespace it.
- All methods are declared ``async`` for a uniform call site, but the shipped
  implementations do blocking I/O inside them (boto3, ``shutil``). They
  therefore occupy the event loop for the duration of a transfer. Treat that as
  a known limitation, not as a guarantee of concurrency.
- Credentials are never read from config here; the manager passes in an
  already-decrypted client config obtained from ``CredentialManager``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StorageRef:
    """Reference to a stored object.

    Returned by :meth:`StorageBackendBase.upload` and consumed by
    ``StorageManager.upload_artifact``, which copies these three fields into the
    ``artifacts`` DB row.

    Attributes:
        key: The ``remote_key`` the object was written under. Backends echo the
            requested key back rather than inventing one, so callers can rely on
            it matching what they asked for.
        size_bytes: Size of the stored object, measured after the write.
        content_type: MIME type, if one was supplied or could be guessed.
            ``None`` when the backend does not track content types (the NAS
            backend only passes through what the caller gave it).
    """
    key: str
    size_bytes: int
    content_type: str | None = None


class StorageBackendBase(ABC):
    """Pluggable storage backend interface.

    Each implementation handles one storage type (MinIO, Google Drive, NAS, etc.).
    Backends never manage credentials directly — they receive client config
    from the CredentialManager.

    Subclasses must set ``backend_type`` as a class attribute (it is the key
    used in ``manager.BACKEND_CLASSES``) and assign ``name`` in ``__init__``
    (the human-readable name from the ``storage_backends`` DB row). Neither is
    enforced by the ABC — only the ``@abstractmethod``\\ s are.
    """

    #: Human-readable instance name, copied from the ``storage_backends.name``
    #: DB row by the manager. Assigned in each subclass's ``__init__``.
    name: str
    #: Class-level identifier matching the ``storage_backends.backend_type``
    #: column and the keys of ``manager.BACKEND_CLASSES``. Set by subclasses.
    backend_type: str

    @abstractmethod
    async def upload(self, local_path: Path, remote_key: str, content_type: str | None = None) -> StorageRef:
        """Upload a file to the storage backend.

        Args:
            local_path: Existing file on the server's filesystem to copy up.
            remote_key: Destination key. Overwrites silently if it already
                exists — no implementation performs an existence check first.
            content_type: MIME type override. When omitted, backends may guess
                (S3 does, NAS does not).

        Returns:
            A :class:`StorageRef` describing the stored object.
        """

    @abstractmethod
    async def download(self, remote_key: str, local_path: Path) -> None:
        """Download a file from the storage backend.

        Implementations create ``local_path.parent`` as needed and overwrite an
        existing destination file.

        Args:
            remote_key: Key to fetch.
            local_path: Destination path on the server's filesystem.

        Raises:
            Exception: Backend-specific (``ClientError`` for S3,
                ``FileNotFoundError`` for NAS) when the key does not exist —
                this interface deliberately does not normalise it.
        """

    @abstractmethod
    async def delete(self, remote_key: str) -> None:
        """Delete a file from the storage backend.

        Expected to be idempotent: deleting a missing key is a no-op rather than
        an error, which is what makes the ``delete_source`` path of a transfer
        safe to retry.
        """

    @abstractmethod
    async def exists(self, remote_key: str) -> bool:
        """Check if a key exists in the backend.

        Returns False rather than raising for a missing key. Note this is a
        best-effort probe, not a lock — the object can disappear between this
        call and a subsequent download.
        """

    @abstractmethod
    async def get_size(self, remote_key: str) -> int:
        """Get the size of a stored object in bytes.

        Raises:
            Exception: Backend-specific error if the key does not exist; unlike
                :meth:`exists` this does not swallow the failure.
        """

    @abstractmethod
    async def list_keys(self, prefix: str = "") -> list[str]:
        """List object keys with the given prefix.

        Args:
            prefix: Key prefix filter; empty string lists everything.

        Returns:
            Keys in the backend's natural order. Unbounded — a large bucket or
            share returns the entire listing in memory, so callers should pass a
            selective prefix.
        """

    @abstractmethod
    async def get_free_space(self) -> int | None:
        """Return available space in bytes, or None if unknown.

        ``None`` is a legitimate answer, not an error: object stores expose no
        capacity figure. The dashboard renders capacity only when this is set.
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the backend is reachable and operational.

        Must never raise — implementations swallow their own errors and return
        False, because ``api/routes/storage.py`` surfaces the result as a status
        flag rather than an HTTP error.
        """

    async def stream_to(
        self, remote_key: str, dest_backend: StorageBackendBase, dest_key: str,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> int:
        """Stream data from this backend to another without loading into memory.

        Default implementation downloads to a temp file then uploads.
        Backends can override for more efficient cross-backend transfers.
        Returns bytes transferred.

        Args:
            remote_key: Source key in *this* backend.
            dest_backend: Target backend instance (may be the same class).
            dest_key: Key to write on the destination. ``StorageManager``
                currently passes the source key unchanged.
            chunk_size: Intended chunk size for overriding implementations.
                The default implementation does not use it — it is part of the
                signature so subclasses that do genuine chunked streaming share
                one call shape.

        Returns:
            Number of bytes transferred, as measured on the local temp copy.

        Note:
            "without loading into memory" refers to RAM only — the default path
            still needs enough free space in the system temp directory for the
            entire object, and does two full copies (down, then up). Overriding
            is worthwhile for same-provider transfers (e.g. S3 server-side copy).
        """
        # AI Note: imported lazily so the ABC stays importable in minimal
        # environments and to keep module import cost off the hot path.
        import tempfile

        # AI Note: delete=True means the temp file is removed when the `with`
        # block exits, including on exception — no cleanup handler needed. The
        # file stays open for the whole transfer, so the backends must be able
        # to write to / read from a path that already has an open handle. That
        # is fine on POSIX but would break on Windows.
        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            tmp_path = Path(tmp.name)
            await self.download(remote_key, tmp_path)
            size = tmp_path.stat().st_size
            await dest_backend.upload(tmp_path, dest_key)
            return size
