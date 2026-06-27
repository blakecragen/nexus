"""Storage backend ABC — all storage backends implement this interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StorageRef:
    """Reference to a stored object."""
    key: str
    size_bytes: int
    content_type: str | None = None


class StorageBackendBase(ABC):
    """Pluggable storage backend interface.

    Each implementation handles one storage type (MinIO, Google Drive, NAS, etc.).
    Backends never manage credentials directly — they receive client config
    from the CredentialManager.
    """

    name: str
    backend_type: str

    @abstractmethod
    async def upload(self, local_path: Path, remote_key: str, content_type: str | None = None) -> StorageRef:
        """Upload a file to the storage backend."""

    @abstractmethod
    async def download(self, remote_key: str, local_path: Path) -> None:
        """Download a file from the storage backend."""

    @abstractmethod
    async def delete(self, remote_key: str) -> None:
        """Delete a file from the storage backend."""

    @abstractmethod
    async def exists(self, remote_key: str) -> bool:
        """Check if a key exists in the backend."""

    @abstractmethod
    async def get_size(self, remote_key: str) -> int:
        """Get the size of a stored object in bytes."""

    @abstractmethod
    async def list_keys(self, prefix: str = "") -> list[str]:
        """List object keys with the given prefix."""

    @abstractmethod
    async def get_free_space(self) -> int | None:
        """Return available space in bytes, or None if unknown."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the backend is reachable and operational."""

    async def stream_to(
        self, remote_key: str, dest_backend: StorageBackendBase, dest_key: str,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> int:
        """Stream data from this backend to another without loading into memory.

        Default implementation downloads to a temp file then uploads.
        Backends can override for more efficient cross-backend transfers.
        Returns bytes transferred.
        """
        import tempfile

        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            tmp_path = Path(tmp.name)
            await self.download(remote_key, tmp_path)
            size = tmp_path.stat().st_size
            await dest_backend.upload(tmp_path, dest_key)
            return size
