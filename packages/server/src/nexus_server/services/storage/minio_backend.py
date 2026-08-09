"""MinIO (S3-compatible) storage backend implementation.

Concrete :class:`~nexus_server.services.storage.base.StorageBackendBase` built
on boto3's S3 client. Registered under both the ``"minio"`` and ``"s3"`` keys in
``storage.manager.BACKEND_CLASSES``; the two are the same code path, they only
differ in the endpoint/credentials supplied by the credential strategy.

Instances are created by ``StorageManager._create_backend_instance``, which
passes the already-decrypted boto3 kwargs as ``client_config``. Nothing here
reads settings or secrets on its own.

Caveat that applies to the whole module: every method is declared ``async`` to
satisfy the ABC, but boto3 is synchronous, so each call blocks the event loop
for its full duration. Large uploads/downloads will stall other requests. Fixing
that means wrapping the boto3 calls in ``asyncio.to_thread`` — a behavioural
change, not done here.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from nexus_server.services.storage.base import StorageBackendBase, StorageRef


class MinIOBackend(StorageBackendBase):
    """S3-compatible storage backend using boto3.

    Works with MinIO, AWS S3, Backblaze B2, Wasabi, etc.

    One instance owns one bucket. The boto3 client is thread-safe for the
    operations used here and is reused for the process lifetime.
    """

    backend_type = "minio"

    def __init__(self, name: str, client_config: dict, bucket: str = "nexus-artifacts"):
        """Build the boto3 client and make sure the target bucket exists.

        Args:
            name: Human-readable backend name from the DB row (used in logs and
                the dashboard).
            client_config: Kwargs splatted straight into ``boto3.client("s3",
                ...)`` — typically ``endpoint_url``, ``aws_access_key_id``,
                ``aws_secret_access_key``, ``region_name``. Produced by the
                credential strategy, so key names must match boto3's exactly or
                this raises ``TypeError``.
            bucket: Bucket to store artifacts in. Created on first use if
                absent.

        Raises:
            botocore.exceptions.ClientError: Bucket creation failed (e.g. bad
                credentials, or the name is taken by another account).
            Exception: Connection errors if the endpoint is unreachable.

        Side effects:
            Performs network I/O during construction (``head_bucket`` and
            possibly ``create_bucket``), so a bad endpoint fails at server
            startup rather than at first upload. ``StorageManager.init_backends``
            catches that and skips the backend.
        """
        self.name = name
        self._bucket = bucket
        self._client = boto3.client("s3", **client_config)
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """Create the configured bucket if a HEAD on it fails.

        Raises:
            botocore.exceptions.ClientError: If ``create_bucket`` itself fails.

        Note:
            The ``except ClientError`` is broader than "404 no such bucket" — a
            403 (bucket exists but this key cannot see it) also lands here and
            triggers a create attempt, which then fails with a clearer error.
            On real AWS, ``create_bucket`` without a ``CreateBucketConfiguration``
            only works in us-east-1; MinIO accepts it anywhere, which is the
            common case for this deployment.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    async def upload(self, local_path: Path, remote_key: str, content_type: str | None = None) -> StorageRef:
        """Upload a local file to ``remote_key``, guessing the MIME type if needed.

        Args:
            local_path: Existing file to upload. ``upload_file`` handles
                multipart chunking for large files automatically.
            remote_key: S3 object key; overwrites any existing object.
            content_type: MIME override. When omitted it is guessed from the
                filename, falling back to ``application/octet-stream`` — this
                matters because browsers use it when the artifact is served
                back.

        Returns:
            :class:`StorageRef` with the key, the local file's size, and the
            effective content type.

        Note:
            ``size_bytes`` is read from the *local* file after the upload, not
            from S3, so it is the intended size rather than a verified one.
        """
        if not content_type:
            content_type, _ = mimetypes.guess_type(str(local_path))
            content_type = content_type or "application/octet-stream"

        extra_args = {"ContentType": content_type}
        self._client.upload_file(str(local_path), self._bucket, remote_key, ExtraArgs=extra_args)
        size = local_path.stat().st_size
        return StorageRef(key=remote_key, size_bytes=size, content_type=content_type)

    async def download(self, remote_key: str, local_path: Path) -> None:
        """Fetch an object to ``local_path``, creating parent directories.

        Args:
            remote_key: Object key to fetch.
            local_path: Destination file; overwritten if present.

        Raises:
            botocore.exceptions.ClientError: Key does not exist or access denied.
        """
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self._bucket, remote_key, str(local_path))

    async def delete(self, remote_key: str) -> None:
        """Delete an object.

        Args:
            remote_key: Object key to remove.

        Note:
            S3 ``delete_object`` succeeds even when the key does not exist, which
            gives the idempotent delete the base class expects.
        """
        self._client.delete_object(Bucket=self._bucket, Key=remote_key)

    async def exists(self, remote_key: str) -> bool:
        """Return True when the key is present in the bucket.

        Note:
            Any ``ClientError`` maps to False, so a 403 (permissions problem) is
            indistinguishable from a 404 (genuinely missing).
        """
        try:
            self._client.head_object(Bucket=self._bucket, Key=remote_key)
            return True
        except ClientError:
            return False

    async def get_size(self, remote_key: str) -> int:
        """Return the object's size in bytes as reported by S3.

        Raises:
            botocore.exceptions.ClientError: Key missing or inaccessible —
                unlike :meth:`exists`, the error is not swallowed.
        """
        resp = self._client.head_object(Bucket=self._bucket, Key=remote_key)
        return resp["ContentLength"]

    async def list_keys(self, prefix: str = "") -> list[str]:
        """List every object key under ``prefix``.

        Uses a paginator so buckets with more than 1000 objects are fully
        enumerated rather than truncated.

        Args:
            prefix: Key prefix; empty string lists the whole bucket.

        Returns:
            All matching keys. Unbounded — the entire listing is materialised in
            memory, so pass a selective prefix for large buckets.
        """
        keys = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            # AI Note: `Contents` is absent (not empty) on a page with no
            # matches, hence the .get default rather than direct indexing.
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    async def get_free_space(self) -> int | None:
        """Always ``None`` — the S3 API exposes no capacity/quota figure.

        Returns:
            ``None``. Callers must treat this as "unknown", not "zero"; the
            dashboard hides the capacity bar for such backends.
        """
        return None  # MinIO doesn't expose this via S3 API

    async def health_check(self) -> bool:
        """Return True when the bucket is reachable with the current credentials.

        Returns:
            True on a successful ``head_bucket``, False on ANY exception
            (network, auth, bucket removed). Never raises, as the base-class
            contract requires.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except Exception:
            return False
