"""MinIO (S3-compatible) storage backend implementation."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from nexus_server.services.storage.base import StorageBackendBase, StorageRef


class MinIOBackend(StorageBackendBase):
    """S3-compatible storage backend using boto3.

    Works with MinIO, AWS S3, Backblaze B2, Wasabi, etc.
    """

    backend_type = "minio"

    def __init__(self, name: str, client_config: dict, bucket: str = "nexus-artifacts"):
        self.name = name
        self._bucket = bucket
        self._client = boto3.client("s3", **client_config)
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    async def upload(self, local_path: Path, remote_key: str, content_type: str | None = None) -> StorageRef:
        if not content_type:
            content_type, _ = mimetypes.guess_type(str(local_path))
            content_type = content_type or "application/octet-stream"

        extra_args = {"ContentType": content_type}
        self._client.upload_file(str(local_path), self._bucket, remote_key, ExtraArgs=extra_args)
        size = local_path.stat().st_size
        return StorageRef(key=remote_key, size_bytes=size, content_type=content_type)

    async def download(self, remote_key: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self._bucket, remote_key, str(local_path))

    async def delete(self, remote_key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=remote_key)

    async def exists(self, remote_key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=remote_key)
            return True
        except ClientError:
            return False

    async def get_size(self, remote_key: str) -> int:
        resp = self._client.head_object(Bucket=self._bucket, Key=remote_key)
        return resp["ContentLength"]

    async def list_keys(self, prefix: str = "") -> list[str]:
        keys = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    async def get_free_space(self) -> int | None:
        return None  # MinIO doesn't expose this via S3 API

    async def health_check(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except Exception:
            return False
