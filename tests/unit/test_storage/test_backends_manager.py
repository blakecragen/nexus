"""Tests for the storage subsystem: StorageManager dispatch/lifecycle, the
local-filesystem NASBackend (real, hermetic against tmp_path), and the
boto3-backed MinIOBackend (boto3 client monkeypatched so no network/server).

SUT: packages/server/src/nexus_server/services/storage/
  - manager.py      (StorageManager)
  - base.py         (StorageBackendBase / StorageRef)
  - nas_backend.py  (NASBackend)
  - minio_backend.py(MinIOBackend)
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from nexus_server.db import ops
from nexus_server.services.storage import minio_backend as minio_mod
from nexus_server.services.storage.base import StorageBackendBase, StorageRef
from nexus_server.services.storage.manager import (
    BACKEND_CLASSES,
    StorageManager,
)
from nexus_server.services.storage.minio_backend import MinIOBackend
from nexus_server.services.storage.nas_backend import NASBackend


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _make_s3_credential(db, credential_manager, owner_id) -> uuid.UUID:
    """Persist a real (encrypted) s3 credential and return its ID."""
    return await credential_manager.store(
        db,
        name=f"s3-cred-{uuid.uuid4().hex[:8]}",
        credential_type="s3",
        fields={
            "endpoint": "localhost:9000",
            "access_key": "minioadmin",
            "secret_key": "minioadmin",
        },
        owner_id=owner_id,
    )


class _FakeS3Client:
    """In-memory stand-in for a boto3 S3 client.

    Records the constructor kwargs so config dispatch can be asserted, and
    keeps a dict of stored objects so upload/download/exists round-trip.
    """

    instances: list = []

    def __init__(self, **kwargs):
        """Record the boto3 kwargs and start with empty bucket/object stores.

        Appends itself to the class-level ``instances`` list so tests can grab the
        most recently constructed client (``instances[-1]``) and assert on the config
        the backend passed to boto3.client.
        """
        self.kwargs = kwargs
        self.buckets: set[str] = set()
        self.objects: dict[str, bytes] = {}
        self.head_bucket_calls = 0
        # Records the ExtraArgs passed to the last upload_file call so the
        # backend's ContentType wiring can be asserted (the SUT builds this).
        self.last_extra_args: dict | None = None
        type(self).instances.append(self)

    def _client_error(self, code, op):
        """Build a botocore ClientError with the given code, as the real client would raise.

        Using the genuine exception type matters: the SUT catches ClientError
        specifically, so a generic Exception here would not exercise the same branch.
        """
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": code}}, op)

    def head_bucket(self, Bucket):
        """Raise a 404 ClientError unless the bucket was created; counts calls.

        The call count is asserted to prove _ensure_bucket probes exactly once.
        """
        self.head_bucket_calls += 1
        if Bucket not in self.buckets:
            raise self._client_error("404", "HeadBucket")

    def create_bucket(self, Bucket):
        """Record the bucket as existing."""
        self.buckets.add(Bucket)

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        """Copy the local file's bytes into the in-memory object store.

        Also stashes ExtraArgs so tests can assert the ContentType the backend built.
        """
        self.last_extra_args = ExtraArgs
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket, key, filename):
        """Write the stored object's bytes to the requested local path."""
        Path(filename).write_bytes(self.objects[key])

    def head_object(self, Bucket, Key):
        """Return ContentLength for a stored key, or raise a 404 ClientError."""
        if Key not in self.objects:
            raise self._client_error("404", "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        """Drop the key if present (idempotent, like real S3)."""
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        """Return a paginator over the in-memory objects, mimicking boto3's page shape."""
        objects = self.objects

        class _Paginator:
            """Minimal boto3 paginator stand-in yielding a single page."""
            def paginate(self, Bucket, Prefix=""):
                """Yield one page of sorted keys matching Prefix.

                Critically, an empty result yields a page with NO 'Contents' key at all —
                that is real boto3 behavior and the reason list_keys() must use .get().
                """
                contents = [
                    {"Key": k} for k in sorted(objects) if k.startswith(Prefix)
                ]
                # Mimic boto3: pages omit "Contents" entirely when empty.
                if contents:
                    yield {"Contents": contents}
                else:
                    yield {}

        return _Paginator()


def _install_fake_boto3(monkeypatch, *, bucket_exists=False):
    """Patch boto3.client (as used by minio_backend) to yield a fake client."""
    _FakeS3Client.instances = []

    def _factory(service, **kwargs):
        """Construct a fake client, optionally pre-creating the default bucket."""
        client = _FakeS3Client(**kwargs)
        if bucket_exists:
            # Pre-create the default bucket so _ensure_bucket is a no-op create.
            client.buckets.add("nexus-artifacts")
        return client

    monkeypatch.setattr(minio_mod.boto3, "client", _factory)


# ─────────────────────────────────────────────────────────────────────────────
# NASBackend — real local filesystem, hermetic against tmp_path
# ─────────────────────────────────────────────────────────────────────────────


def test_nas_init_rejects_missing_mount_path(tmp_path):
    """Construction must fail loudly if the mount point doesn't exist."""
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError):
        NASBackend(name="nas", mount_path=str(missing))


def test_nas_backend_type_attr(tmp_path):
    """NASBackend reports backend_type 'nas', keeps its name, and satisfies the base ABC.

    backend_type is what the manager's dispatch table and the UI badge key off.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    assert backend.backend_type == "nas"
    assert backend.name == "nas"
    assert isinstance(backend, StorageBackendBase)


async def test_nas_upload_returns_storage_ref_with_size(tmp_path):
    """upload() returns a StorageRef with key/size/content_type and creates parent dirs.

    size_bytes is recorded from the written file (the transfer-progress UI reads
    it), and nested key paths must auto-create their directories.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    src = tmp_path / "src.txt"
    src.write_bytes(b"hello nexus")

    ref = await backend.upload(src, "dir/out.txt", content_type="text/plain")

    assert isinstance(ref, StorageRef)
    assert ref.key == "dir/out.txt"
    assert ref.size_bytes == len(b"hello nexus")
    assert ref.content_type == "text/plain"
    # File physically landed under the mount root (nested dir created).
    assert (tmp_path / "dir" / "out.txt").read_bytes() == b"hello nexus"


async def test_nas_upload_download_roundtrip(tmp_path):
    """Binary content survives upload -> download byte-for-byte, into a new dest dir.

    Guards against text-mode handling that would corrupt non-UTF8 artifacts.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    src = tmp_path / "payload.bin"
    src.write_bytes(b"\x00\x01\x02payload")
    await backend.upload(src, "nested/payload.bin")

    dest = tmp_path / "downloaded" / "payload.bin"
    await backend.download("nested/payload.bin", dest)

    assert dest.read_bytes() == b"\x00\x01\x02payload"


async def test_nas_exists_and_get_size(tmp_path):
    """exists() distinguishes present from absent keys and get_size() reports the true size."""
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    src = tmp_path / "f.txt"
    src.write_bytes(b"sized")
    await backend.upload(src, "a/b.txt")

    assert await backend.exists("a/b.txt") is True
    assert await backend.exists("a/missing.txt") is False
    assert await backend.get_size("a/b.txt") == len(b"sized")


async def test_nas_get_size_missing_key_raises(tmp_path):
    """get_size stats the target path directly, so a missing key raises."""
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        await backend.get_size("never/written.txt")


async def test_nas_download_missing_key_raises(tmp_path):
    """download shutil.copy2's from the key path — missing source raises."""
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        await backend.download("ghost.txt", tmp_path / "out" / "ghost.txt")


async def test_nas_delete_is_idempotent(tmp_path):
    """delete() removes the key and is a silent no-op when called again.

    Cleanup paths may run twice (retry, or both the job runner and a reaper); a
    second delete must not raise.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    await backend.upload(src, "k.txt")
    assert await backend.exists("k.txt") is True

    await backend.delete("k.txt")
    assert await backend.exists("k.txt") is False
    # Deleting a non-existent key is a silent no-op (must not raise).
    await backend.delete("k.txt")


async def test_nas_list_keys_returns_relative_paths(tmp_path):
    """list_keys() returns mount-relative POSIX keys, honors a prefix, and returns [] for no match.

    Keys must be relative (not absolute host paths) so they are portable across
    backends — a MinIO backend given the same key must resolve the same object.
    The source file is kept outside the mount root so it can't pollute the listing.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    backend = NASBackend(name="nas", mount_path=str(mount))
    # Keep the source file OUTSIDE the mount root so it doesn't show up in list.
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    await backend.upload(src, "top.txt")
    await backend.upload(src, "sub/deep.txt")

    all_keys = await backend.list_keys()
    assert set(all_keys) == {"top.txt", "sub/deep.txt"}

    # Prefix filtering scopes to the subtree.
    sub_keys = await backend.list_keys("sub")
    assert sub_keys == ["sub/deep.txt"]

    # Unknown prefix yields an empty list, not an error.
    assert await backend.list_keys("nope") == []


async def test_nas_get_free_space_and_health(tmp_path):
    """get_free_space() reports real disk space and health_check() passes for a live mount.

    The scheduler uses free space for capacity display and health to avoid routing
    artifacts to a dead mount.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    free = await backend.get_free_space()
    assert isinstance(free, int) and free > 0
    assert await backend.health_check() is True


async def test_nas_stream_to_uses_base_default(tmp_path):
    """Cross-backend transfer via the base stream_to (download->upload)."""
    src_root = tmp_path / "src"
    dst_root = tmp_path / "dst"
    src_root.mkdir()
    dst_root.mkdir()
    source = NASBackend(name="src", mount_path=str(src_root))
    dest = NASBackend(name="dst", mount_path=str(dst_root))

    local = tmp_path / "orig.txt"
    local.write_bytes(b"stream me across")
    await source.upload(local, "obj/key.txt")

    n = await source.stream_to("obj/key.txt", dest, "copied/key.txt")

    assert n == len(b"stream me across")
    assert await dest.exists("copied/key.txt") is True
    assert (dst_root / "copied" / "key.txt").read_bytes() == b"stream me across"


# ─────────────────────────────────────────────────────────────────────────────
# MinIOBackend — boto3 client monkeypatched (no network)
# ─────────────────────────────────────────────────────────────────────────────


def test_minio_init_creates_bucket_when_missing(monkeypatch):
    """Construction auto-creates the bucket when head_bucket 404s, and forwards config verbatim.

    Auto-create is what makes a fresh MinIO deployment work without manual setup.
    The verbatim kwargs assertion catches the backend mangling endpoint/credentials.
    """
    _install_fake_boto3(monkeypatch, bucket_exists=False)
    backend = MinIOBackend(
        name="minio",
        client_config={"endpoint_url": "http://localhost:9000"},
        bucket="my-bucket",
    )
    assert backend.backend_type == "minio"
    client = _FakeS3Client.instances[-1]
    # head_bucket raised (missing) -> create_bucket ran.
    assert "my-bucket" in client.buckets
    # Constructor config forwarded to boto3.client verbatim.
    assert client.kwargs == {"endpoint_url": "http://localhost:9000"}


def test_minio_init_skips_create_when_bucket_exists(monkeypatch):
    """An existing bucket is probed exactly once and never re-created.

    Also pins the default bucket name 'nexus-artifacts' used when config omits one.
    """
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(
        name="minio",
        client_config={},
    )
    client = _FakeS3Client.instances[-1]
    # Default bucket already present, head_bucket succeeded once, no extra create.
    assert backend._bucket == "nexus-artifacts"
    assert client.head_bucket_calls == 1


async def test_minio_upload_infers_content_type(monkeypatch, tmp_path):
    """content_type is inferred from the key's extension when not supplied.

    Correct types let the browser render artifacts inline instead of downloading
    them as opaque blobs.
    """
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "report.json"
    src.write_bytes(b'{"ok": true}')

    ref = await backend.upload(src, "out/report.json")

    assert ref.key == "out/report.json"
    assert ref.size_bytes == len(b'{"ok": true}')
    # mimetypes resolves .json -> application/json.
    assert ref.content_type == "application/json"


async def test_minio_upload_download_roundtrip(monkeypatch, tmp_path):
    """Binary content survives upload -> download through the S3 path byte-for-byte."""
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "data.bin"
    src.write_bytes(b"binary-payload")
    await backend.upload(src, "k/data.bin", content_type="application/octet-stream")

    dest = tmp_path / "pulled" / "data.bin"
    await backend.download("k/data.bin", dest)

    assert dest.read_bytes() == b"binary-payload"


async def test_minio_upload_forwards_content_type_as_extra_args(monkeypatch, tmp_path):
    """The backend must pass the resolved ContentType to S3 via ExtraArgs."""
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "page.html"
    src.write_bytes(b"<html></html>")

    await backend.upload(src, "k/page.html", content_type="text/html")

    client = _FakeS3Client.instances[-1]
    assert client.last_extra_args == {"ContentType": "text/html"}


async def test_minio_upload_unknown_extension_defaults_octet_stream(monkeypatch, tmp_path):
    """When mimetypes can't guess, the backend falls back to octet-stream."""
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "blob.unknownext"
    src.write_bytes(b"raw")

    ref = await backend.upload(src, "k/blob.unknownext")

    assert ref.content_type == "application/octet-stream"
    client = _FakeS3Client.instances[-1]
    assert client.last_extra_args == {"ContentType": "application/octet-stream"}


async def test_minio_exists_and_get_size(monkeypatch, tmp_path):
    """exists() turns a 404 head_object ClientError into False; get_size() reports the real length.

    A missing key must NOT propagate the ClientError — callers treat exists() as a
    plain boolean probe.
    """
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "f.txt"
    src.write_bytes(b"twelve bytes")
    await backend.upload(src, "k/f.txt")

    assert await backend.exists("k/f.txt") is True
    # head_object raises ClientError for missing keys -> exists() returns False.
    assert await backend.exists("k/missing.txt") is False
    assert await backend.get_size("k/f.txt") == len(b"twelve bytes")


async def test_minio_delete_then_absent(monkeypatch, tmp_path):
    """delete() removes the object so a subsequent exists() is False."""
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    await backend.upload(src, "k/f.txt")
    assert await backend.exists("k/f.txt") is True

    await backend.delete("k/f.txt")
    assert await backend.exists("k/f.txt") is False


async def test_minio_list_keys_with_and_without_prefix(monkeypatch, tmp_path):
    """list_keys() paginates all keys, filters by prefix, and returns [] on no match.

    The empty case is the important one: boto3 omits 'Contents' from an empty page,
    so a direct page["Contents"] index would raise KeyError.
    """
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    await backend.upload(src, "a/one.txt")
    await backend.upload(src, "a/two.txt")
    await backend.upload(src, "b/three.txt")

    assert set(await backend.list_keys()) == {"a/one.txt", "a/two.txt", "b/three.txt"}
    assert await backend.list_keys("a/") == ["a/one.txt", "a/two.txt"]
    # Unknown prefix -> empty page (no "Contents") -> empty list, not an error.
    assert await backend.list_keys("zzz/") == []


async def test_minio_health_check_true_when_reachable(monkeypatch):
    """health_check() is True when the bucket responds."""
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    assert await backend.health_check() is True


async def test_minio_health_check_false_when_bucket_gone(monkeypatch):
    """health_check() returns False (not an exception) when the bucket disappears.

    The health poller calls this on a schedule; a raised ClientError would take out
    the poll loop instead of just marking the backend unhealthy.
    """
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    # Simulate the bucket disappearing after init.
    backend._client.buckets.discard("nexus-artifacts")
    assert await backend.health_check() is False


async def test_minio_get_free_space_is_none(monkeypatch):
    """Object stores report None free space (unknown), not 0.

    None means "not applicable" so the UI can hide the capacity bar; 0 would render
    as a full disk and could gate scheduling.
    """
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    backend = MinIOBackend(name="minio", client_config={})
    assert await backend.get_free_space() is None


# ─────────────────────────────────────────────────────────────────────────────
# StorageManager — dispatch, get_backend, get_default_backend, init_backends
# ─────────────────────────────────────────────────────────────────────────────


def test_backend_classes_registry_maps_known_types():
    """The dispatch table must cover minio, s3 (shared) and nas."""
    assert BACKEND_CLASSES["minio"] is MinIOBackend
    assert BACKEND_CLASSES["s3"] is MinIOBackend
    assert BACKEND_CLASSES["nas"] is NASBackend


async def test_create_backend_instance_nas(db, credential_manager, admin_user, tmp_path):
    """The manager builds a working NASBackend from a DB row's config.

    End-to-end for the nas branch: DB model -> mount_path -> live, healthy backend.
    """
    cred_id = await _make_s3_credential(db, credential_manager, admin_user.id)
    model = await ops.create_storage_backend(
        db,
        name="nas-1",
        backend_type="nas",
        config={"mount_path": str(tmp_path)},
        credential_id=cred_id,
    )
    mgr = StorageManager(credential_manager=credential_manager)

    backend = await mgr._create_backend_instance(db, model)

    assert isinstance(backend, NASBackend)
    assert backend.name == "nas-1"
    assert await backend.health_check() is True


async def test_create_backend_instance_minio(
    db, credential_manager, admin_user, monkeypatch
):
    """The manager decrypts the linked s3 credential and threads it into boto3.client.

    This is the join between the credential subsystem and storage: the access key,
    secret and endpoint must all arrive at the client, with region defaulting to
    us-east-1 when the credential omits it (MinIO ignores region but boto3 requires
    one).
    """
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    cred_id = await _make_s3_credential(db, credential_manager, admin_user.id)
    model = await ops.create_storage_backend(
        db,
        name="minio-1",
        backend_type="minio",
        config={"bucket": "custom-bucket"},
        credential_id=cred_id,
    )
    mgr = StorageManager(credential_manager=credential_manager)

    backend = await mgr._create_backend_instance(db, model)

    assert isinstance(backend, MinIOBackend)
    assert backend._bucket == "custom-bucket"
    # The decrypted s3 credential config was threaded through to boto3.client.
    client = _FakeS3Client.instances[-1]
    assert client.kwargs["aws_access_key_id"] == "minioadmin"
    assert client.kwargs["aws_secret_access_key"] == "minioadmin"
    assert client.kwargs["endpoint_url"] == "http://localhost:9000"
    # region defaults to us-east-1 when not supplied in the credential fields.
    assert client.kwargs["region_name"] == "us-east-1"


async def test_create_backend_instance_s3_alias_uses_minio(
    db, credential_manager, admin_user, monkeypatch
):
    """backend_type 's3' is an alias for MinIOBackend and falls back to the default bucket.

    MinIO is S3-API-compatible, so one implementation serves both types.
    """
    _install_fake_boto3(monkeypatch, bucket_exists=True)
    cred_id = await _make_s3_credential(db, credential_manager, admin_user.id)
    model = await ops.create_storage_backend(
        db,
        name="s3-1",
        backend_type="s3",
        config={},  # no bucket -> default
        credential_id=cred_id,
    )
    mgr = StorageManager(credential_manager=credential_manager)

    backend = await mgr._create_backend_instance(db, model)

    assert isinstance(backend, MinIOBackend)
    assert backend._bucket == "nexus-artifacts"


async def test_create_backend_instance_unknown_type_raises(
    db, credential_manager, admin_user
):
    """An unrecognized backend_type raises a ValueError naming the bad type.

    A row can carry any string (no DB enum), so the dispatch must fail loudly
    rather than returning None and NoneType-erroring later.
    """
    cred_id = await _make_s3_credential(db, credential_manager, admin_user.id)
    model = await ops.create_storage_backend(
        db,
        name="weird",
        backend_type="ftp",  # not in BACKEND_CLASSES
        config={},
        credential_id=cred_id,
    )
    mgr = StorageManager(credential_manager=credential_manager)

    with pytest.raises(ValueError, match="Unknown backend type: ftp"):
        await mgr._create_backend_instance(db, model)


async def test_create_backend_instance_nas_missing_mount_path_raises(
    db, credential_manager, admin_user
):
    """nas dispatch reads config['mount_path'] directly — missing key -> KeyError."""
    cred_id = await _make_s3_credential(db, credential_manager, admin_user.id)
    model = await ops.create_storage_backend(
        db,
        name="nas-bad",
        backend_type="nas",
        config={},  # no mount_path
        credential_id=cred_id,
    )
    mgr = StorageManager(credential_manager=credential_manager)

    with pytest.raises(KeyError):
        await mgr._create_backend_instance(db, model)


def test_get_backend_uninitialized_raises(credential_manager):
    """get_backend() on an id that was never initialized raises a 'not initialized' KeyError.

    Distinguishes "backend failed to load / manager not initialized" from
    "no such backend", which is the difference between a config bug and a 404.
    """
    mgr = StorageManager(credential_manager=credential_manager)
    with pytest.raises(KeyError, match="not initialized"):
        mgr.get_backend(uuid.uuid4())


async def test_init_backends_skips_inactive_and_failures(
    db, credential_manager, admin_user, tmp_path
):
    """init_backends loads only active backends and swallows per-backend errors."""
    cred_id = await _make_s3_credential(db, credential_manager, admin_user.id)

    good = await ops.create_storage_backend(
        db, name="good-nas", backend_type="nas",
        config={"mount_path": str(tmp_path)},
        credential_id=cred_id, is_active=True,
    )
    # Active but broken (mount_path points at a non-existent dir) -> init logs &
    # continues, this backend never makes it into the registry.
    broken = await ops.create_storage_backend(
        db, name="broken-nas", backend_type="nas",
        config={"mount_path": str(tmp_path / "nope")},
        credential_id=cred_id, is_active=True,
    )
    # Inactive -> skipped entirely regardless of validity.
    inactive = await ops.create_storage_backend(
        db, name="inactive-nas", backend_type="nas",
        config={"mount_path": str(tmp_path)},
        credential_id=cred_id, is_active=False,
    )

    mgr = StorageManager(credential_manager=credential_manager)
    await mgr.init_backends(db)

    # Only the good one is registered & retrievable.
    assert isinstance(mgr.get_backend(good.id), NASBackend)
    with pytest.raises(KeyError):
        mgr.get_backend(broken.id)
    with pytest.raises(KeyError):
        mgr.get_backend(inactive.id)


async def test_get_default_backend_returns_default(
    db, credential_manager, admin_user, tmp_path
):
    """get_default_backend() resolves the is_default row to its live backend instance.

    Jobs without an explicit storage_target land here, so it must return both the
    id (for the DB reference) and the usable instance.
    """
    cred_id = await _make_s3_credential(db, credential_manager, admin_user.id)
    default_model = await ops.create_storage_backend(
        db, name="default-nas", backend_type="nas",
        config={"mount_path": str(tmp_path)},
        credential_id=cred_id, is_default=True, is_active=True,
    )

    mgr = StorageManager(credential_manager=credential_manager)
    await mgr.init_backends(db)

    bid, backend = await mgr.get_default_backend(db)
    assert bid == default_model.id
    assert isinstance(backend, NASBackend)


async def test_get_default_backend_no_default_raises(db, credential_manager):
    """With no default configured, get_default_backend() raises a descriptive RuntimeError.

    Surfaces as a clear operator-facing message instead of an artifact upload
    failing with a NoneType attribute error.
    """
    mgr = StorageManager(credential_manager=credential_manager)
    with pytest.raises(RuntimeError, match="No default storage backend"):
        await mgr.get_default_backend(db)
