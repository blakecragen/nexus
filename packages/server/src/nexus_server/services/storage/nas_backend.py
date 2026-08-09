"""NAS storage backend — reads/writes to a mounted filesystem.

Concrete :class:`~nexus_server.services.storage.base.StorageBackendBase` that
treats an already-mounted directory (NFS/SMB share, external disk, plain local
path) as an object store: the ``remote_key`` becomes a relative path underneath
the mount root.

Registered under ``"nas"`` in ``storage.manager.BACKEND_CLASSES`` and
constructed by ``StorageManager._create_backend_instance`` from the DB row's
``config["mount_path"]``. Unlike the S3 backend it needs no credentials — the
mount itself is the trust boundary, so whoever runs the server process has
whatever access the OS grants to that path.

Two caveats that apply module-wide:

- Mounting is out of scope. If the share is unmounted at runtime, reads/writes
  fail (or, worse for an autofs-style mount, silently hit the empty local
  directory that the mount point shadows). :meth:`NASBackend.health_check` only
  verifies the directory exists, not that it is actually mounted.
- Every method is ``async`` for interface parity but performs blocking
  ``shutil``/``pathlib`` I/O, so a large copy stalls the event loop.
"""

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
        """Validate the mount point up front.

        Args:
            name: Human-readable backend name from the DB row.
            mount_path: Absolute path where the share is mounted. Must already
                exist — this class never creates or mounts it.
            client_config: Accepted for signature parity with the other backends
                (the manager builds every backend from a credential) but
                deliberately unused: filesystem access is governed by the OS,
                not by a credential.

        Raises:
            FileNotFoundError: The mount path does not exist. Failing here means
                a missing mount is reported at server startup by
                ``StorageManager.init_backends`` instead of corrupting artifacts
                later by writing into a non-existent share.
        """
        self.name = name
        # AI Note: resolved once here so it is a stable, symlink-free base for
        # both the containment check in _full_path and the lexical
        # relative_to() in list_keys. If these two used different bases (one
        # resolved, one not) a symlinked mount root would make every
        # containment check fail or every relative_to() raise.
        self._root = Path(mount_path).resolve()
        if not self._root.exists():
            raise FileNotFoundError(f"NAS mount path does not exist: {mount_path}")

    def _full_path(self, key: str) -> Path:
        """Resolve a storage key to an absolute path, refusing to leave the root.

        Args:
            key: Backend-independent storage key, ``/``-separated.

        Returns:
            The resolved absolute path, guaranteed to be the root itself or a
            descendant of it.

        Raises:
            ValueError: The key resolves outside the mount root.

        AI Note: this guard is security-critical, and it is the ONLY thing
        standing between a caller-chosen key and the rest of the filesystem.
        Three distinct escapes are blocked:

        - ``"../../etc/passwd"`` — ``..`` segments, collapsed by ``resolve()``.
        - ``"/etc/passwd"`` — an absolute key, because ``Path``'s ``/`` operator
          treats it as a *full replacement* for the left operand rather than a
          child, silently discarding the mount root.
        - a symlink planted inside the share that points outside it, because
          ``resolve()`` follows links before the comparison.

        Without it a caller-controlled key gave arbitrary write (``upload``),
        read (``download``), delete (``delete`` unlinks the target), and
        directory disclosure (``list_keys("..")`` enumerated outside the share,
        since ``relative_to`` is purely lexical and does not validate).

        Failing loudly with ``ValueError`` is deliberate: the alternative —
        silently clamping the key back under the root — would let two different
        keys collide on one file and corrupt artifacts. Note the same payloads
        are legitimate *literal* object keys on S3/MinIO, so this belongs here
        in the filesystem backend, not in shared code.
        """
        candidate = (self._root / key).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError(
                f"storage key escapes the backend root: {key!r}"
            )
        return candidate

    async def upload(self, local_path: Path, remote_key: str, content_type: str | None = None) -> StorageRef:
        """Copy a local file into the share, creating intermediate directories.

        Args:
            local_path: Source file on the server's filesystem.
            remote_key: Relative destination path under the mount root.
                Overwrites an existing file without warning.
            content_type: Passed straight through to the returned
                :class:`StorageRef`. Unlike the S3 backend this does NOT guess a
                MIME type, so the value can legitimately be ``None`` and gets
                stored that way on the artifact row.

        Returns:
            :class:`StorageRef` whose ``size_bytes`` is read back from the
            *destination* file, so it reflects what actually landed on the share.

        Note:
            ``copy2`` preserves mtime/mode metadata. It is not atomic — an
            interrupted copy leaves a partial file at the destination key with
            no marker distinguishing it from a complete one.
        """
        dest = self._full_path(remote_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_path), str(dest))
        size = dest.stat().st_size
        return StorageRef(key=remote_key, size_bytes=size, content_type=content_type)

    async def download(self, remote_key: str, local_path: Path) -> None:
        """Copy a stored file out of the share to ``local_path``.

        Args:
            remote_key: Key to read.
            local_path: Destination file; parents are created, an existing file
                is overwritten.

        Raises:
            FileNotFoundError: The key does not exist on the share (or the share
                is not mounted).
        """
        src = self._full_path(remote_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(local_path))

    async def delete(self, remote_key: str) -> None:
        """Remove a stored file; a no-op if it is already gone.

        The ``is_file()`` guard provides the idempotent-delete behaviour the base
        class expects, and also means a key that happens to name a *directory*
        is silently ignored rather than raising — empty parent directories are
        never cleaned up, so the share accumulates empty trees over time.
        """
        path = self._full_path(remote_key)
        if path.is_file():
            path.unlink()

    async def exists(self, remote_key: str) -> bool:
        """Return True when the key resolves to a regular file.

        Directories and broken symlinks count as absent, matching the object
        store semantics the interface promises.
        """
        return self._full_path(remote_key).is_file()

    async def get_size(self, remote_key: str) -> int:
        """Return the stored file's size in bytes.

        Raises:
            FileNotFoundError: Key does not exist.
        """
        return self._full_path(remote_key).stat().st_size

    async def list_keys(self, prefix: str = "") -> list[str]:
        """List every file under ``prefix``, as keys relative to the mount root.

        Args:
            prefix: A *directory* path relative to the root, not a string
                prefix. This differs from the S3 backend, where ``prefix`` is a
                literal key prefix — ``"logs"`` matches ``logs/a.txt`` here but
                would also match ``logs2/a.txt`` on S3.

        Returns:
            Relative paths of all regular files found, recursively. Empty list
            when the prefix directory does not exist (rather than an error, so
            listing an unused namespace is harmless). Directories themselves are
            omitted.

        Note:
            ``rglob("*")`` walks the whole subtree eagerly and follows into every
            directory; on a large share this is slow and allocates the full
            listing in memory.
        """
        search_root = self._full_path(prefix) if prefix else self._root
        if not search_root.exists():
            return []
        keys = []
        for path in search_root.rglob("*"):
            if path.is_file():
                # Keys are always relative to the mount root, never to the
                # prefix, so the result can be fed straight back to other
                # methods on this backend.
                keys.append(str(path.relative_to(self._root)))
        return keys

    async def get_free_space(self) -> int | None:
        """Return bytes free on the filesystem holding the mount root.

        Returns:
            The ``free`` figure from ``shutil.disk_usage`` — space available to
            the current user, which on some filesystems is less than raw
            unallocated space (reserved blocks).

        Raises:
            OSError: If the mount root has disappeared. Unlike
                :meth:`health_check` this does not swallow the failure, so
                callers should guard it when a share may be offline.
        """
        usage = shutil.disk_usage(str(self._root))
        return usage.free

    async def health_check(self) -> bool:
        """Return True when the mount root is present and is a directory.

        Note:
            This is a liveness check for the *path*, not the mount. If the share
            is unmounted but the mount point directory still exists locally,
            this returns True while every read fails — the classic silent-failure
            mode for NFS/SMB backends. A stronger check would stat a known
            sentinel file inside the share.
        """
        return self._root.exists() and self._root.is_dir()
