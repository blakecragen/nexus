"""NAS storage backend — reads/writes to a mounted filesystem."""

from __future__ import annotations

import shutil
from pathlib import Path

from nexus_server.services.storage.base import StorageBackendBase, StorageRef


class NASBackend(StorageBackendBase):
    """Network-attached storage backend using a local mount point.

    Expects the NAS to be mounted at a known path (e.g., /mnt/nas-lab).
    Files are stored as: {mount_path}/{remote_key}
    """

    backend_type = "nas"

    def __init__(self, name: str, mount_path: str, client_config: dict | None = None):
        self.name = name
        self._root = Path(mount_path)
        if not self._root.exists():
            raise FileNotFoundError(f"NAS mount path does not exist: {mount_path}")

    def _full_path(self, key: str) -> Path:
        return self._root / key

    async def upload(self, local_path: Path, remote_key: str, content_type: str | None = None) -> StorageRef:
        dest = self._full_path(remote_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_path), str(dest))
        size = dest.stat().st_size
        return StorageRef(key=remote_key, size_bytes=size, content_type=content_type)

    async def download(self, remote_key: str, local_path: Path) -> None:
        src = self._full_path(remote_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(local_path))

    async def delete(self, remote_key: str) -> None:
        path = self._full_path(remote_key)
        if path.is_file():
            path.unlink()

    async def exists(self, remote_key: str) -> bool:
        return self._full_path(remote_key).is_file()

    async def get_size(self, remote_key: str) -> int:
        return self._full_path(remote_key).stat().st_size

    async def list_keys(self, prefix: str = "") -> list[str]:
        search_root = self._full_path(prefix) if prefix else self._root
        if not search_root.exists():
            return []
        keys = []
        for path in search_root.rglob("*"):
            if path.is_file():
                keys.append(str(path.relative_to(self._root)))
        return keys

    async def get_free_space(self) -> int | None:
        usage = shutil.disk_usage(str(self._root))
        return usage.free

    async def health_check(self) -> bool:
        return self._root.exists() and self._root.is_dir()
