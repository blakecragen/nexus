"""Extended regression tests for the storage subsystem — error branches, key
safety, and the transfer bookkeeping that ``test_backends_manager.py`` does not
reach.

Complements (does not repeat) ``tests/unit/test_storage/test_backends_manager.py``,
which covers the happy paths of NASBackend/MinIOBackend and the manager's
dispatch/lifecycle. This module concentrates on:

  * **Key safety (security regression guards)** — ``NASBackend._full_path``
    confines every key to the mount root, so ``../`` keys, absolute keys and
    symlinks that point out of the share are rejected with ``ValueError``
    instead of operating on the host filesystem. Each guard asserts both the
    raise *and* the absence of the side effect (no file created/read/deleted
    outside the root), contrasted with ``MinIOBackend``, where the same payloads
    remain inert literal object keys.
  * **Error/IO branches** — missing files, ``ClientError`` propagation vs.
    swallowing, permission and ENOSPC failures, disappearing mounts.
  * **``StorageManager.transfer_artifact``** — the DB bookkeeping around a copy
    (transfer row status transitions, the artifact repoint) including the
    UUID-vs-``String(36)`` bind hazard that made the repoint crash aiosqlite,
    now covered on both the ``*_id`` and the ``*_by`` columns that
    ``ops._sid_kwargs`` coerces.

SUT: packages/server/src/nexus_server/services/storage/
  - manager.py      (StorageManager.upload_artifact/download_artifact/transfer_artifact)
  - base.py         (StorageBackendBase.stream_to)
  - nas_backend.py  (NASBackend)
  - minio_backend.py(MinIOBackend)

The boto3 client is always monkeypatched (``_install_stub_boto3``) so no test
touches the network; every filesystem test is rooted in ``tmp_path``.
"""

from __future__ import annotations

import errno
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy.exc
from botocore.exceptions import ClientError

from nexus_server.db import ops
from nexus_server.services.storage import minio_backend as minio_mod
from nexus_server.services.storage import nas_backend as nas_mod
from nexus_server.services.storage.manager import StorageManager
from nexus_server.services.storage.minio_backend import MinIOBackend
from nexus_server.services.storage.nas_backend import NASBackend


# ── Helpers ──────────────────────────────────────────────────────────────────


def _client_error(code: str, op: str) -> ClientError:
    """Build a genuine botocore ``ClientError`` with the given error code.

    The real exception type matters: the SUT catches ``ClientError``
    specifically, so a bare ``Exception`` would not exercise the same branch.
    """
    return ClientError({"Error": {"Code": code}}, op)


class _StubS3Client:
    """In-memory boto3 S3 client stand-in with per-operation error injection.

    Richer than the fake in ``test_backends_manager.py``: any operation can be
    made to raise (``fail["head_object"] = ...``), individual keys can be marked
    403/AccessDenied, and ``page_size`` forces the paginator to emit multiple
    pages so ``list_keys`` pagination is genuinely exercised.
    """

    instances: list = []

    def __init__(self, **kwargs):
        """Record the boto3 kwargs and start with empty bucket/object stores."""
        self.kwargs = kwargs
        self.buckets: set[str] = set()
        self.objects: dict[str, bytes] = {}
        #: remote_key -> the ExtraArgs dict the backend passed for that upload.
        self.extra_args: dict[str, dict | None] = {}
        #: (Filename, Bucket, Key) for every upload_file call, in order.
        self.upload_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[str] = []
        self.head_bucket_calls = 0
        self.create_bucket_calls = 0
        #: Objects per paginator page; small values force multi-page listings.
        self.page_size = 1000
        #: Operation name -> exception to raise instead of running it.
        self.fail: dict[str, BaseException] = {}
        #: Keys that answer head_object with 403 even though they exist.
        self.access_denied_keys: set[str] = set()
        type(self).instances.append(self)

    def _maybe_fail(self, op: str) -> None:
        """Raise the injected exception for ``op``, if one was configured."""
        exc = self.fail.get(op)
        if exc is not None:
            raise exc

    def head_bucket(self, Bucket):
        """Count the probe, honour injected failures, then 404 on a missing bucket."""
        self.head_bucket_calls += 1
        self._maybe_fail("head_bucket")
        if Bucket not in self.buckets:
            raise _client_error("404", "HeadBucket")

    def create_bucket(self, Bucket):
        """Record the bucket as existing (or raise, if creation was made to fail)."""
        self.create_bucket_calls += 1
        self._maybe_fail("create_bucket")
        self.buckets.add(Bucket)

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        """Copy the local file's bytes into the in-memory object store."""
        self._maybe_fail("upload_file")
        self.upload_calls.append((filename, bucket, key))
        self.extra_args[key] = ExtraArgs
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket, key, filename):
        """Write a stored object to ``filename``, or raise 404 like real S3."""
        self._maybe_fail("download_file")
        if key not in self.objects:
            raise _client_error("404", "GetObject")
        Path(filename).write_bytes(self.objects[key])

    def head_object(self, Bucket, Key):
        """Return ContentLength, or raise 403 for denied keys / 404 for missing ones."""
        self._maybe_fail("head_object")
        if Key in self.access_denied_keys:
            raise _client_error("403", "HeadObject")
        if Key not in self.objects:
            raise _client_error("404", "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        """Drop the key if present (idempotent, like real S3)."""
        self._maybe_fail("delete_object")
        self.delete_calls.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        """Return a paginator that chunks the in-memory keys by ``page_size``."""
        client = self

        class _Paginator:
            """Paginator stand-in yielding boto3-shaped pages."""

            def paginate(self, Bucket, Prefix=""):
                """Yield ``page_size``-sized pages of matching keys.

                An empty result yields ONE page with no ``Contents`` key at all,
                which is what real boto3 does and why ``list_keys`` must use
                ``.get()``.
                """
                client._maybe_fail("paginate")
                keys = [k for k in sorted(client.objects) if k.startswith(Prefix)]
                if not keys:
                    yield {}
                    return
                for start in range(0, len(keys), client.page_size):
                    chunk = keys[start : start + client.page_size]
                    yield {"Contents": [{"Key": k} for k in chunk]}

        return _Paginator()


def _install_stub_boto3(monkeypatch, *, bucket_exists=True, configure=None):
    """Patch ``boto3.client`` (as imported by minio_backend) to yield a stub.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        bucket_exists: Pre-create ``nexus-artifacts`` so ``_ensure_bucket`` is a
            no-op probe.
        configure: Optional callback applied to each stub BEFORE the backend's
            constructor uses it — the only way to inject a failure that has to
            happen during ``MinIOBackend.__init__``.
    """
    _StubS3Client.instances = []

    def _factory(service, **kwargs):
        """Construct a stub client, pre-seeding and configuring it."""
        client = _StubS3Client(**kwargs)
        if bucket_exists:
            client.buckets.add("nexus-artifacts")
        if configure is not None:
            configure(client)
        return client

    monkeypatch.setattr(minio_mod.boto3, "client", _factory)


async def _make_s3_cred(db, credential_manager, owner_id) -> str:
    """Persist a real (encrypted) s3 credential and return its ID."""
    return await credential_manager.store(
        db,
        name=f"ext-s3-cred-{uuid.uuid4().hex[:8]}",
        credential_type="s3",
        fields={
            "endpoint": "localhost:9000",
            "access_key": "minioadmin",
            "secret_key": "minioadmin",
        },
        owner_id=owner_id,
    )


async def _make_transfer_env(
    db, credential_manager, admin_user, tmp_path, *,
    payload: bytes = b"transfer me",
    key: str = "k/payload.bin",
):
    """Seed two initialized NAS backends, a job, and one artifact on the source.

    Returns a namespace with the manager, both ``storage_backends`` rows, both
    mount roots, and the artifact id — the common ground state for the
    ``transfer_artifact`` tests.
    """
    src_root = tmp_path / "src-mount"
    dst_root = tmp_path / "dst-mount"
    src_root.mkdir()
    dst_root.mkdir()
    cred_id = await _make_s3_cred(db, credential_manager, admin_user.id)
    src_model = await ops.create_storage_backend(
        db, name="src-nas", backend_type="nas",
        config={"mount_path": str(src_root)},
        credential_id=cred_id, is_active=True,
    )
    dst_model = await ops.create_storage_backend(
        db, name="dst-nas", backend_type="nas",
        config={"mount_path": str(dst_root)},
        credential_id=cred_id, is_active=True,
    )
    mgr = StorageManager(credential_manager=credential_manager)
    await mgr.init_backends(db)
    job = await ops.create_job(
        db, name="artifact-job", submitted_by=admin_user.id, steps_config=[],
    )
    local = tmp_path / "local-payload.bin"
    local.write_bytes(payload)
    artifact_id = await mgr.upload_artifact(
        db, local_path=local, remote_key=key, job_id=job.id,
        backend_id=src_model.id,
    )
    return SimpleNamespace(
        mgr=mgr, src=src_model, dst=dst_model, src_root=src_root,
        dst_root=dst_root, job=job, artifact_id=artifact_id, key=key,
        payload=payload, cred_id=cred_id, local=local,
    )


# ─────────────────────────────────────────────────────────────────────────────
# NASBackend — key safety (SECURITY): _full_path confines every key to the root
#
# These are regression guards for a fixed path-traversal hole. Each one asserts
# BOTH halves of the contract: the ValueError is raised, AND the side effect the
# old code performed (a file created / read / deleted outside the mount) did not
# happen. Only the second half actually proves the hole is closed.
# ─────────────────────────────────────────────────────────────────────────────

#: Substring of the ``ValueError`` message ``NASBackend._full_path`` raises.
ESCAPE_MSG = "escapes the backend root"


def test_nas_full_path_rejects_traversal_and_absolute_keys(tmp_path):
    """_full_path() resolves the key and refuses anything outside the mount root.

    The single chokepoint every other NAS method funnels through, so this test
    is the root of the containment contract. Two payload families are rejected:

    * ``..`` segments, including ones hidden mid-key (``"a/../../out.txt"``),
      because ``resolve()`` collapses them before the comparison.
    * absolute keys, because ``Path.__truediv__`` treats an absolute right
      operand as a *full replacement* for the mount root rather than a child.

    The second half of the test pins that the guard is a containment check and
    not a blanket ban on ``.``/``..``: a key that normalises back to somewhere
    under the root is still accepted, and the root itself is legal (``list_keys``
    relies on that for its empty prefix).
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    backend = NASBackend(name="nas", mount_path=str(mount))

    for key in ("../escaped.txt", "..", "../../etc/passwd", "a/../../out.txt"):
        with pytest.raises(ValueError, match=ESCAPE_MSG):
            backend._full_path(key)

    with pytest.raises(ValueError, match=ESCAPE_MSG):
        backend._full_path("/etc/passwd")
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        backend._full_path(str(tmp_path / "absolute-escape.txt"))

    # Keys that normalise back under the root are still honoured.
    assert backend._full_path("a/../b.txt") == mount / "b.txt"
    assert backend._full_path("k/./x.txt") == mount / "k" / "x.txt"
    assert backend._full_path("plain.txt") == mount / "plain.txt"
    # The root itself is inside the root (the `candidate != self._root` arm).
    assert backend._full_path(".") == mount


async def test_nas_upload_with_parent_traversal_key_is_rejected(tmp_path):
    """upload() with a '../' key raises ValueError and writes nothing anywhere.

    SECURITY regression guard for the arbitrary-file-write half of the hole: the
    old code copied the bytes to ``tmp_path/escaped.txt``, a sibling of the mount
    root, and returned a StorageRef that still echoed the traversal key so
    nothing downstream could tell the write had escaped.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    backend = NASBackend(name="nas", mount_path=str(mount))
    src = tmp_path / "src.txt"
    src.write_bytes(b"escaping payload")

    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.upload(src, "../escaped.txt")

    # The side effect that used to happen: no file outside, none inside either.
    assert not (tmp_path / "escaped.txt").exists()
    assert list(mount.iterdir()) == []


async def test_nas_upload_with_absolute_key_is_rejected(tmp_path):
    """An absolute remote_key is rejected instead of discarding the mount root.

    SECURITY regression guard for the second traversal family: a key of
    '/tmp/x' used to write to '/tmp/x' rather than under the share, because the
    ``self._root / key`` join silently dropped the root.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    outside_target = tmp_path / "absolute-escape.txt"
    backend = NASBackend(name="nas", mount_path=str(mount))
    src = tmp_path / "src.txt"
    src.write_bytes(b"absolute payload")

    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.upload(src, str(outside_target))

    assert not outside_target.exists()
    assert list(mount.iterdir()) == []


async def test_nas_download_with_traversal_key_is_rejected(tmp_path):
    """download() with a '../' key raises before reading the outside file.

    SECURITY regression guard for arbitrary file read: the destination file is
    never even created, so no bytes from outside the share reach the caller.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP SECRET")
    backend = NASBackend(name="nas", mount_path=str(mount))

    out = tmp_path / "pulled" / "secret.txt"
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.download("../secret.txt", out)

    # Nothing was exfiltrated: no destination file, not even its parent dir.
    assert not out.exists()
    assert not out.parent.exists()
    assert secret.read_bytes() == b"TOP SECRET"


async def test_nas_exists_and_get_size_reject_traversal_key(tmp_path):
    """exists()/get_size() raise on a '../' key instead of probing the host.

    SECURITY regression guard for existence disclosure — the metadata calls were
    a free oracle for "does this host file exist / how big is it". The guard
    fires *before* the stat, so a caller cannot distinguish a present outside
    file from an absent one. In-root probing is unaffected: a missing key still
    returns a plain ``False`` rather than raising.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"12345")
    backend = NASBackend(name="nas", mount_path=str(mount))

    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.exists("../outside.txt")
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.get_size("../outside.txt")
    # Same rejection whether or not the outside path exists -> no oracle.
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.exists("../not-there.txt")

    # The in-mount probe semantics are untouched.
    assert await backend.exists("not-there.txt") is False


async def test_nas_delete_with_traversal_key_is_rejected(tmp_path):
    """delete() with a '../' key raises and leaves the outside file untouched.

    SECURITY regression guard for arbitrary file deletion: the ``is_file()``
    guard only ever stopped directories, so before the fix this call unlinked
    a file outside the share.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"delete me")
    backend = NASBackend(name="nas", mount_path=str(mount))

    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.delete("../victim.txt")

    assert victim.read_bytes() == b"delete me"


async def test_nas_list_keys_with_parent_prefix_is_rejected(tmp_path):
    """list_keys('..') raises instead of enumerating outside the share.

    SECURITY regression guard for directory-listing disclosure. ``relative_to``
    is purely lexical and validates nothing, so the containment check on the
    *prefix* is what stops the walk from ever starting outside the root. In-mount
    listing still works and still yields root-relative keys.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "inside.txt").write_bytes(b"in")
    (tmp_path / "outside.txt").write_bytes(b"out")
    backend = NASBackend(name="nas", mount_path=str(mount))

    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.list_keys("..")

    # Only the share's own contents are ever enumerable.
    assert await backend.list_keys() == ["inside.txt"]


async def test_nas_symlink_escaping_the_root_is_rejected_by_every_op(tmp_path):
    """A symlink pointing out of the share is rejected on read, stat and delete.

    SECURITY regression guard for the sneakiest of the three escapes: the key
    ``"innocent.txt"`` contains no ``..`` and is not absolute, so only
    ``resolve()``-then-compare catches it. Anyone who can write to the NAS mount
    (a lab user, another host on the share) could otherwise turn a perfectly
    normal-looking key into a read of an arbitrary host file.

    Both the link and its target survive the rejected ``delete``.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    target = tmp_path / "linked-secret.txt"
    target.write_bytes(b"LINKED SECRET")
    link = mount / "innocent.txt"
    link.symlink_to(target)
    backend = NASBackend(name="nas", mount_path=str(mount))

    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.exists("innocent.txt")
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.get_size("innocent.txt")
    out = tmp_path / "out.txt"
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.download("innocent.txt", out)
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.delete("innocent.txt")

    assert not out.exists()  # nothing exfiltrated
    assert target.read_bytes() == b"LINKED SECRET"  # nothing destroyed
    assert link.is_symlink()  # the link itself was not touched either


async def test_nas_symlink_inside_the_root_is_still_served(tmp_path):
    """A symlink whose target is also inside the share keeps working normally.

    The containment check is about *where the key lands*, not about symlinks as
    such — so an in-share link is an ordinary key. Pinning this stops a future
    "just reject every symlink" tightening from breaking legitimate shares that
    use links for layout.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    target = mount / "real" / "payload.bin"
    target.parent.mkdir()
    target.write_bytes(b"in-share bytes")
    (mount / "alias.bin").symlink_to(target)
    backend = NASBackend(name="nas", mount_path=str(mount))

    assert await backend.exists("alias.bin") is True
    assert await backend.get_size("alias.bin") == len(b"in-share bytes")
    out = tmp_path / "out.bin"
    await backend.download("alias.bin", out)
    assert out.read_bytes() == b"in-share bytes"


async def test_nas_list_keys_lists_an_escaping_symlink_it_cannot_read(tmp_path):
    """A symlink out of the share is LISTED but not readable — listing is lexical.

    The honest, slightly lopsided consequence of where the guard lives:

    * ``list_keys`` walks with ``rglob`` and keys the results with a purely
      lexical ``relative_to(self._root)``. ``is_file()`` follows the link, so an
      escaping symlink-to-file is indistinguishable from a real file and shows
      up in the listing.
    * Every *access* to that key goes through ``_full_path`` and is rejected.

    So the key exists as far as the listing is concerned and 404s (well,
    ``ValueError``) on use. That leaks a filename, never file contents. A
    symlinked *directory* leaks nothing at all: ``rglob`` does not recurse into
    directory symlinks, and — unlike before the fix — ``exists()`` on a path
    *through* it is now rejected too, so its contents are neither listable nor
    reachable.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "real.txt").write_bytes(b"real")
    file_target = tmp_path / "file-target.txt"
    file_target.write_bytes(b"target")
    (mount / "link.txt").symlink_to(file_target)
    dir_target = tmp_path / "dir-target"
    dir_target.mkdir()
    (dir_target / "hidden.txt").write_bytes(b"hidden")
    (mount / "linkdir").symlink_to(dir_target, target_is_directory=True)
    backend = NASBackend(name="nas", mount_path=str(mount))

    keys = await backend.list_keys()

    # The escaping file link is listed; the symlinked directory contributes none.
    assert set(keys) == {"real.txt", "link.txt"}
    # ...but the listed key cannot actually be read.
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.exists("link.txt")
    # ...and the symlinked directory's contents are no longer reachable either.
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.exists("linkdir/hidden.txt")
    # Only the genuinely in-share key round-trips.
    assert await backend.exists("real.txt") is True


async def test_nas_delete_in_root_symlink_unlinks_the_target_not_the_link(tmp_path):
    """delete() resolves through an in-share symlink, so the TARGET is unlinked.

    Consequence of ``_full_path`` now returning a ``resolve()``d path: ``delete``
    operates on the link's target, leaving the (now dangling) link behind. That
    is safe — the containment check guarantees the target is inside the share —
    but it is asymmetric with the pre-fix behaviour, which unlinked the link and
    kept the target, so it is pinned deliberately rather than assumed.

    The escaping-symlink case (where resolve-then-delete WOULD destroy data
    outside the share) is covered by
    ``test_nas_symlink_escaping_the_root_is_rejected_by_every_op``.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    target = mount / "keepme.txt"
    target.write_bytes(b"keep")
    link = mount / "link.txt"
    link.symlink_to(target)
    backend = NASBackend(name="nas", mount_path=str(mount))

    await backend.delete("link.txt")

    assert not target.exists()  # the resolved target is what got unlinked
    assert link.is_symlink()  # the link survives, now dangling
    assert await backend.exists("link.txt") is False  # and reads as absent
    # A dangling link is not a file, so it drops out of the listing too.
    assert await backend.list_keys() == []


async def test_nas_exists_broken_symlink_absent_inside_root_rejected_outside(tmp_path):
    """A dangling in-share symlink is 'absent'; one pointing outside is rejected.

    ``resolve()`` is non-strict, so it happily normalises a link to a
    non-existent path and the containment check then decides:

    * target inside the root -> allowed, ``is_file()`` False, i.e. the
      object-store "dangling means absent" semantics are preserved.
    * target outside the root -> ``ValueError``, because a broken link is just as
      much an escape primitive as a working one (the target could appear later).
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "dangling-inside.txt").symlink_to(mount / "never-existed.txt")
    (mount / "dangling-outside.txt").symlink_to(tmp_path / "never-existed.txt")
    backend = NASBackend(name="nas", mount_path=str(mount))

    assert await backend.exists("dangling-inside.txt") is False
    with pytest.raises(ValueError, match=ESCAPE_MSG):
        await backend.exists("dangling-outside.txt")


async def test_nas_exists_false_for_directory_key(tmp_path):
    """A key that names a directory is 'absent' — exists() means "is a regular file"."""
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    (tmp_path / "adir").mkdir()

    assert await backend.exists("adir") is False


async def test_nas_delete_directory_key_is_silent_noop(tmp_path):
    """delete() ignores a key that names a directory instead of raising.

    The ``is_file()`` guard means a mistaken directory key is silently dropped —
    the caller gets no signal that nothing was deleted.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    d = tmp_path / "adir"
    d.mkdir()
    (d / "child.txt").write_bytes(b"child")

    await backend.delete("adir")

    assert d.is_dir()
    assert (d / "child.txt").exists()


async def test_nas_get_size_on_directory_key_returns_stat_size(tmp_path):
    """get_size() stats whatever the key names, so a directory returns its inode size.

    Documents that get_size does NOT validate file-ness (unlike exists()) — it
    returns a meaningless-but-non-raising number for a directory key.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    (tmp_path / "adir").mkdir()

    size = await backend.get_size("adir")

    assert isinstance(size, int)
    assert size >= 0


# ─────────────────────────────────────────────────────────────────────────────
# NASBackend — error / IO branches and boundaries
# ─────────────────────────────────────────────────────────────────────────────


async def test_nas_upload_missing_source_raises_file_not_found(tmp_path):
    """upload() surfaces a missing local source as FileNotFoundError.

    The upload half of the "no artifact row without bytes" invariant: the
    manager must not get a StorageRef for a file that was never copied.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))

    with pytest.raises(FileNotFoundError):
        await backend.upload(tmp_path / "no-such-file.txt", "k/out.txt")

    assert not (tmp_path / "k").joinpath("out.txt").exists()


async def test_nas_upload_directory_source_raises_is_a_directory(tmp_path):
    """Passing a directory as the local source raises IsADirectoryError, not silence."""
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    srcdir = tmp_path / "srcdir"
    srcdir.mkdir()

    with pytest.raises(IsADirectoryError):
        await backend.upload(srcdir, "k/out.txt")


async def test_nas_upload_permission_error_propagates(tmp_path, monkeypatch):
    """A PermissionError from the copy is not swallowed by upload().

    A read-only share must fail the upload loudly so the caller never records an
    artifact row for bytes that were rejected by the filesystem.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    src = tmp_path / "src.txt"
    src.write_bytes(b"payload")

    def _denied(*args, **kwargs):
        """Stand in for a copy onto a read-only mount."""
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(nas_mod.shutil, "copy2", _denied)

    with pytest.raises(PermissionError):
        await backend.upload(src, "k/out.txt")


async def test_nas_download_io_error_propagates(tmp_path, monkeypatch):
    """An ENOSPC OSError during download propagates instead of yielding a truncated file.

    ``download`` has no try/except, so a full local disk must reach the caller
    (the transfer path relies on this to mark the transfer failed).
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    src = tmp_path / "src.txt"
    src.write_bytes(b"payload")
    await backend.upload(src, "k/out.txt")

    def _no_space(*args, **kwargs):
        """Stand in for a full destination filesystem."""
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(nas_mod.shutil, "copy2", _no_space)

    with pytest.raises(OSError) as excinfo:
        await backend.download("k/out.txt", tmp_path / "dest" / "out.txt")
    assert excinfo.value.errno == errno.ENOSPC


async def test_nas_upload_overwrites_existing_key_and_reports_new_size(tmp_path):
    """Re-uploading a key overwrites silently and the StorageRef reports the NEW size.

    size_bytes is read back from the destination, so a shrinking overwrite must
    not leave a stale larger size on the artifact row.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 100)
    small = tmp_path / "small.txt"
    small.write_bytes(b"y")

    await backend.upload(big, "k/obj.bin")
    ref = await backend.upload(small, "k/obj.bin")

    assert ref.size_bytes == 1
    assert (tmp_path / "k" / "obj.bin").read_bytes() == b"y"


async def test_nas_upload_without_content_type_keeps_none(tmp_path):
    """NAS never guesses a MIME type — an omitted content_type stays None.

    Deliberate divergence from MinIOBackend (which guesses from the filename);
    the artifact row therefore stores NULL content_type for NAS uploads.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    src = tmp_path / "report.json"
    src.write_bytes(b"{}")

    ref = await backend.upload(src, "k/report.json")

    assert ref.content_type is None


async def test_nas_zero_byte_upload_is_stored_and_visible(tmp_path):
    """A 0-byte artifact round-trips: size 0, exists() True, and it lists.

    Boundary case — empty step output must not be mistaken for "no artifact".
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    backend = NASBackend(name="nas", mount_path=str(mount))
    src = tmp_path / "empty.bin"
    src.write_bytes(b"")

    ref = await backend.upload(src, "k/empty.bin")

    assert ref.size_bytes == 0
    assert await backend.exists("k/empty.bin") is True
    assert await backend.get_size("k/empty.bin") == 0
    assert await backend.list_keys() == ["k/empty.bin"]


async def test_nas_list_keys_prefix_is_a_directory_not_a_string_prefix(tmp_path):
    """list_keys('logs') matches logs/ only — NOT logs2/, unlike the S3 backend.

    Pins the documented semantic divergence between the two backends: the same
    prefix argument means "subdirectory" here and "literal key prefix" on S3.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    backend = NASBackend(name="nas", mount_path=str(mount))
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    await backend.upload(src, "logs/a.txt")
    await backend.upload(src, "logs2/b.txt")

    assert await backend.list_keys("logs") == ["logs/a.txt"]
    assert set(await backend.list_keys()) == {"logs/a.txt", "logs2/b.txt"}


async def test_nas_list_keys_empty_mount_returns_empty_list(tmp_path):
    """An empty share lists as [] rather than raising or returning the root."""
    mount = tmp_path / "mount"
    mount.mkdir()
    backend = NASBackend(name="nas", mount_path=str(mount))

    assert await backend.list_keys() == []


async def test_nas_list_keys_omits_directories(tmp_path):
    """Only regular files become keys; intermediate directories are skipped."""
    mount = tmp_path / "mount"
    mount.mkdir()
    backend = NASBackend(name="nas", mount_path=str(mount))
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    await backend.upload(src, "a/b/c/deep.txt")
    (mount / "empty-dir").mkdir()

    assert await backend.list_keys() == ["a/b/c/deep.txt"]


async def test_nas_get_free_space_matches_disk_usage_free(tmp_path):
    """get_free_space() reports disk_usage().free, not total or used.

    Pins which of the three figures is returned — reporting ``total`` would make
    a full share look infinitely writable in the dashboard.
    """
    backend = NASBackend(name="nas", mount_path=str(tmp_path))
    expected = shutil.disk_usage(str(tmp_path))

    free = await backend.get_free_space()

    assert free == expected.free
    assert free != expected.total or expected.used == 0


async def test_nas_get_free_space_raises_when_mount_disappears(tmp_path):
    """get_free_space() does NOT swallow a vanished mount — it raises OSError.

    Contrast with health_check(): callers polling capacity must guard this call
    themselves when a share can go offline.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    backend = NASBackend(name="nas", mount_path=str(mount))
    mount.rmdir()

    with pytest.raises(OSError):
        await backend.get_free_space()


async def test_nas_health_check_false_when_mount_removed(tmp_path):
    """health_check() flips to False (never raises) once the mount root is gone.

    The health poller calls this on a schedule; an exception would take out the
    loop instead of marking one backend unhealthy.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    backend = NASBackend(name="nas", mount_path=str(mount))
    assert await backend.health_check() is True

    mount.rmdir()

    assert await backend.health_check() is False


async def test_nas_health_check_false_when_mount_path_is_a_file(tmp_path):
    """Construction accepts a FILE as mount_path, but health_check() reports False.

    The constructor only checks ``exists()``, so the is_dir() half of the health
    check is the only thing that catches this misconfiguration.
    """
    file_mount = tmp_path / "not-a-dir.txt"
    file_mount.write_bytes(b"x")

    backend = NASBackend(name="nas", mount_path=str(file_mount))

    assert await backend.health_check() is False


def test_nas_client_config_is_accepted_and_ignored(tmp_path):
    """A client_config is accepted for signature parity and never stored.

    NAS access is governed by the OS mount, so the credential must not turn into
    hidden state on the instance.
    """
    backend = NASBackend(
        name="nas", mount_path=str(tmp_path), client_config={"secret": "unused"},
    )

    assert not hasattr(backend, "_client_config")
    assert backend._root == tmp_path


# ─────────────────────────────────────────────────────────────────────────────
# MinIOBackend — error branches, pagination, key handling (boto3 stubbed)
# ─────────────────────────────────────────────────────────────────────────────


def test_minio_init_creates_bucket_after_403_access_denied(monkeypatch):
    """A 403 on head_bucket also triggers create_bucket, per the broad except.

    Documents that ``_ensure_bucket`` cannot distinguish "missing" from
    "forbidden": an under-privileged key produces a create attempt whose error
    is what the operator actually sees.
    """
    _install_stub_boto3(
        monkeypatch,
        bucket_exists=True,
        configure=lambda c: c.fail.__setitem__(
            "head_bucket", _client_error("403", "HeadBucket"),
        ),
    )

    MinIOBackend(name="minio", client_config={})

    client = _StubS3Client.instances[-1]
    assert client.head_bucket_calls == 1
    assert client.create_bucket_calls == 1


def test_minio_init_propagates_client_error_when_create_bucket_fails(monkeypatch):
    """A failing create_bucket aborts construction so the backend is never registered.

    ``init_backends`` relies on this: a bucket it cannot create must surface as a
    skipped backend at startup, not a half-built instance.
    """
    def _configure(client):
        """Make the bucket look missing AND uncreatable."""
        client.fail["head_bucket"] = _client_error("404", "HeadBucket")
        client.fail["create_bucket"] = _client_error("AccessDenied", "CreateBucket")

    _install_stub_boto3(monkeypatch, bucket_exists=False, configure=_configure)

    with pytest.raises(ClientError):
        MinIOBackend(name="minio", client_config={}, bucket="denied-bucket")


async def test_minio_download_missing_key_propagates_client_error(monkeypatch, tmp_path):
    """download() lets a 404 ClientError through (no normalisation to FileNotFoundError).

    The interface deliberately keeps backend-specific errors; the transfer path
    turns whatever is raised into a failed transfer row.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})

    with pytest.raises(ClientError):
        await backend.download("k/ghost.bin", tmp_path / "out" / "ghost.bin")


async def test_minio_get_size_missing_key_propagates_client_error(monkeypatch):
    """get_size() does NOT swallow the 404 that exists() converts to False.

    The asymmetry is intentional and load-bearing: exists() is a probe, get_size()
    is an assertion that the object is there.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})

    assert await backend.exists("k/ghost.bin") is False
    with pytest.raises(ClientError):
        await backend.get_size("k/ghost.bin")


async def test_minio_exists_false_on_access_denied(monkeypatch, tmp_path):
    """A 403 makes exists() return False even though the object IS there.

    Pins the documented ambiguity — a permissions regression looks exactly like
    a missing artifact, so exists() must never be used as an integrity check.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "f.txt"
    src.write_bytes(b"present")
    await backend.upload(src, "k/f.txt")
    backend._client.access_denied_keys.add("k/f.txt")

    assert await backend.exists("k/f.txt") is False
    # The bytes are still in the store — only the HEAD is denied.
    assert backend._client.objects["k/f.txt"] == b"present"


async def test_minio_upload_propagates_client_error_and_stores_nothing(monkeypatch, tmp_path):
    """A failed upload raises and leaves no object behind (no partial success).

    The manager creates the artifact row only after upload returns, so this is
    what keeps a failed upload from being indexed.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    backend._client.fail["upload_file"] = _client_error("AccessDenied", "PutObject")
    src = tmp_path / "f.txt"
    src.write_bytes(b"payload")

    with pytest.raises(ClientError):
        await backend.upload(src, "k/f.txt")

    assert backend._client.objects == {}


async def test_minio_delete_propagates_client_error(monkeypatch):
    """delete() has no error handling — a failing DeleteObject reaches the caller.

    Matters for ``transfer_artifact(delete_source=True)``, which relies on the
    exception to mark the transfer failed rather than silently orphaning bytes.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    backend._client.fail["delete_object"] = _client_error("AccessDenied", "DeleteObject")

    with pytest.raises(ClientError):
        await backend.delete("k/whatever.bin")


async def test_minio_delete_missing_key_is_idempotent_noop(monkeypatch):
    """Deleting a key that was never stored succeeds, matching S3 semantics.

    Cleanup paths can run twice (retry, or a reaper plus the runner), so the
    second delete must not raise.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})

    await backend.delete("k/never-existed.bin")
    await backend.delete("k/never-existed.bin")

    assert backend._client.delete_calls == [
        "k/never-existed.bin", "k/never-existed.bin",
    ]


async def test_minio_upload_explicit_content_type_overrides_guess(monkeypatch, tmp_path):
    """An explicit content_type wins over what mimetypes would have guessed."""
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "data.json"
    src.write_bytes(b"{}")

    ref = await backend.upload(src, "k/data.json", content_type="text/plain")

    assert ref.content_type == "text/plain"
    assert backend._client.extra_args["k/data.json"] == {"ContentType": "text/plain"}


async def test_minio_upload_empty_content_type_falls_back_to_guess(monkeypatch, tmp_path):
    """An empty-string content_type is falsy, so the type is guessed instead.

    Boundary between "caller supplied nothing" and "caller supplied a blank" —
    both must end up with a real MIME type on the object.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "table.csv"
    src.write_bytes(b"a,b\n1,2\n")

    ref = await backend.upload(src, "k/table.csv", content_type="")

    assert ref.content_type == "text/csv"


async def test_minio_upload_guesses_from_local_filename_not_remote_key(monkeypatch, tmp_path):
    """The MIME guess reads the LOCAL path, so an extension-less source yields octet-stream.

    Pins a real consequence: any caller that stages bytes in a temp file (the
    default ``stream_to`` does exactly that) loses the content type even when the
    remote key clearly says '.json'.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "tmpstaged"  # no extension, like a NamedTemporaryFile
    src.write_bytes(b'{"ok": true}')

    ref = await backend.upload(src, "k/report.json")

    assert ref.content_type == "application/octet-stream"


async def test_minio_upload_zero_byte_object_round_trips(monkeypatch, tmp_path):
    """A 0-byte object uploads, reports size 0, exists, and downloads as empty."""
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "empty.bin"
    src.write_bytes(b"")

    ref = await backend.upload(src, "k/empty.bin")

    assert ref.size_bytes == 0
    assert await backend.exists("k/empty.bin") is True
    assert await backend.get_size("k/empty.bin") == 0
    out = tmp_path / "pulled" / "empty.bin"
    await backend.download("k/empty.bin", out)
    assert out.read_bytes() == b""


async def test_minio_list_keys_spans_multiple_pages(monkeypatch, tmp_path):
    """list_keys() concatenates every paginator page, not just the first.

    Regression guard for the >1000-object case: dropping the loop would silently
    truncate the listing (and any GC built on it).
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    expected = []
    for i in range(5):
        key = f"k/obj-{i}.bin"
        await backend.upload(src, key)
        expected.append(key)
    backend._client.page_size = 2  # forces 3 pages

    keys = await backend.list_keys()

    assert sorted(keys) == sorted(expected)


async def test_minio_list_keys_prefix_is_a_literal_string_prefix(monkeypatch, tmp_path):
    """S3 prefixes are literal, so 'logs' also matches 'logs2/...' (unlike NAS).

    The mirror image of the NAS prefix test — pinning both sides makes the
    divergence explicit for anyone writing backend-agnostic cleanup code.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    await backend.upload(src, "logs/a.txt")
    await backend.upload(src, "logs2/b.txt")

    assert set(await backend.list_keys("logs")) == {"logs/a.txt", "logs2/b.txt"}
    assert await backend.list_keys("logs/") == ["logs/a.txt"]


async def test_minio_list_keys_propagates_client_error(monkeypatch):
    """A failing paginate reaches the caller instead of returning a partial list.

    A silently empty listing would look like "no artifacts" to callers.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    backend._client.fail["paginate"] = _client_error("AccessDenied", "ListObjectsV2")

    with pytest.raises(ClientError):
        await backend.list_keys()


async def test_minio_health_check_false_on_non_client_error(monkeypatch):
    """health_check() swallows ANY exception, not just ClientError.

    A DNS/connection failure raises something other than ClientError, and the
    poller still needs a boolean.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    backend._client.fail["head_bucket"] = ConnectionError("endpoint unreachable")

    assert await backend.health_check() is False


async def test_minio_traversal_key_is_an_inert_literal_object_key(monkeypatch, tmp_path):
    """'../' in a key is passed to S3 verbatim — an object name, not a path escape.

    The deliberate security contrast with NASBackend: the exact payload that
    ``NASBackend._full_path`` now rejects with ``ValueError`` is a harmless
    literal key here, because S3 has no filesystem to escape from. That is why
    the containment check lives in the NAS backend rather than in shared
    key-handling code — hoisting it would break existing (legal) S3 keys and
    would be pure overhead for object stores.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    src = tmp_path / "f.txt"
    src.write_bytes(b"escape attempt")

    ref = await backend.upload(src, "../../etc/passwd")

    assert ref.key == "../../etc/passwd"
    assert backend._client.upload_calls[-1][2] == "../../etc/passwd"
    assert backend._client.objects["../../etc/passwd"] == b"escape attempt"
    assert await backend.exists("../../etc/passwd") is True


async def test_minio_get_free_space_none_even_when_unhealthy(monkeypatch):
    """get_free_space() is unconditionally None — it never probes the endpoint.

    Guards against a future implementation that would make the capacity call
    fail (or block) when the bucket is unreachable.
    """
    _install_stub_boto3(monkeypatch)
    backend = MinIOBackend(name="minio", client_config={})
    backend._client.fail["head_bucket"] = ConnectionError("down")

    assert await backend.get_free_space() is None


# ─────────────────────────────────────────────────────────────────────────────
# StorageManager.upload_artifact / download_artifact — id typing
# ─────────────────────────────────────────────────────────────────────────────


async def test_upload_artifact_with_uuid_job_id_persists_a_string_id(
    db, credential_manager, admin_user, tmp_path,
):
    """A UUID-typed job_id is coerced to str(36) on the artifact row.

    Regression for the UUID/SQLite bind class: a raw ``uuid.UUID`` bound to a
    ``String(36)`` column raises "type 'UUID' is not supported" in aiosqlite and
    poisons the session. ``ops._sid_kwargs`` absorbs it for every ``*_id`` key.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)
    src = tmp_path / "second.txt"
    src.write_bytes(b"second artifact")

    artifact_id = await env.mgr.upload_artifact(
        db, local_path=src, remote_key="k/second.txt",
        job_id=uuid.UUID(str(env.job.id)),
        uploaded_by=str(admin_user.id),
        backend_id=env.src.id,
    )

    db.expunge_all()
    row = await ops.get_artifact_by_id(db, artifact_id)
    assert isinstance(row.job_id, str)
    assert row.job_id == str(env.job.id)
    assert row.uploaded_by == str(admin_user.id)
    assert row.filename == "second.txt"
    assert row.size_bytes == len(b"second artifact")


async def test_upload_artifact_uuid_uploaded_by_is_coerced_and_persisted(
    db, credential_manager, admin_user, tmp_path,
):
    """A UUID ``uploaded_by`` is coerced to str(36) — the ``_by`` suffix is covered.

    Regression guard for the second half of the ``ops._sid_kwargs`` fix. It
    originally matched only keys ending in ``_id``, which silently missed
    ``artifacts.uploaded_by`` — a ``String(36)`` FK to ``users.id`` whose name has
    no ``_id`` suffix — so a ``uuid.UUID`` reached the aiosqlite bind and raised
    "type 'UUID' is not supported". That crash landed *after* the bytes were
    already on the backend, orphaning them with no artifact row, so the assertions
    below check both the persisted string id and that the row exists at all.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)
    src = tmp_path / "audited.txt"
    src.write_bytes(b"audited bytes")

    artifact_id = await env.mgr.upload_artifact(
        db, local_path=src, remote_key="k/audited.txt",
        job_id=env.job.id,
        uploaded_by=uuid.UUID(str(admin_user.id)),
        backend_id=env.src.id,
    )

    db.expunge_all()
    row = await ops.get_artifact_by_id(db, artifact_id)
    assert isinstance(row.uploaded_by, str)
    assert row.uploaded_by == str(admin_user.id)
    # The bytes are no longer orphaned: object and row both exist and agree.
    assert (env.src_root / "k" / "audited.txt").read_bytes() == b"audited bytes"
    assert row.size_bytes == len(b"audited bytes")
    listed = await ops.list_artifacts_for_job(db, env.job.id)
    assert {a.id for a in listed} == {env.artifact_id, artifact_id}


async def test_upload_artifact_uuid_backend_id_is_not_found_in_registry(
    db, credential_manager, admin_user, tmp_path,
):
    """A UUID-typed backend_id misses the str-keyed registry and raises KeyError.

    Pins CURRENT behaviour (see the ``get_backend`` docstring): the registry is
    keyed by ``storage_backends.id``, a plain str, and no coercion happens on
    lookup — so a caller that hands over a ``uuid.UUID`` gets
    "not initialized" for a perfectly live backend.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)
    src = tmp_path / "third.txt"
    src.write_bytes(b"third")

    with pytest.raises(KeyError, match="not initialized"):
        await env.mgr.upload_artifact(
            db, local_path=src, remote_key="k/third.txt", job_id=env.job.id,
            backend_id=uuid.UUID(str(env.src.id)),
        )

    # Nothing was written and no artifact row was created for the failed call.
    assert not (env.src_root / "k" / "third.txt").exists()


async def test_download_artifact_with_uuid_artifact_id_writes_file(
    db, credential_manager, admin_user, tmp_path,
):
    """download_artifact() accepts a UUID artifact_id and writes the stored bytes.

    The lookup goes through ``ops.get_artifact_by_id`` -> ``_sid``, so the UUID
    must not reach the SQLite bind layer.
    """
    env = await _make_transfer_env(
        db, credential_manager, admin_user, tmp_path, payload=b"downloadable",
    )
    out = tmp_path / "out" / "payload.bin"

    await env.mgr.download_artifact(db, uuid.UUID(str(env.artifact_id)), out)

    assert out.read_bytes() == b"downloadable"


async def test_download_artifact_unknown_id_raises_keyerror(
    db, credential_manager, admin_user, tmp_path,
):
    """An unknown artifact id raises KeyError('... not found'), which routes map to 404."""
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)

    with pytest.raises(KeyError, match="not found"):
        await env.mgr.download_artifact(db, uuid.uuid4(), tmp_path / "nope.bin")


# ─────────────────────────────────────────────────────────────────────────────
# StorageManager.transfer_artifact — UUID bind fix + transfer bookkeeping
# ─────────────────────────────────────────────────────────────────────────────


async def test_transfer_artifact_with_uuid_typed_ids_completes_end_to_end(
    db, credential_manager, admin_user, tmp_path,
):
    """A UUID-typed dest_backend_id/artifact_id transfer completes and repoints the row.

    This is the regression for the just-fixed bind bug: the final
    ``artifact.storage_backend_id = str(dest_backend_id)`` used to assign the raw
    ``uuid.UUID``, so the UPDATE blew up with "type 'UUID' is not supported",
    wedging the transfer. Asserts the persisted value is the STRING id (re-read
    from the DB after ``expunge_all``) and that the transfer row is 'completed'.

    The destination is also aliased into the registry under its UUID key because
    ``get_backend`` does not coerce ids — see
    ``test_transfer_artifact_uuid_dest_backend_id_not_in_registry_fails``.
    """
    env = await _make_transfer_env(
        db, credential_manager, admin_user, tmp_path, payload=b"uuid payload",
    )
    dest_uuid = uuid.UUID(str(env.dst.id))
    env.mgr._backends[dest_uuid] = env.mgr.get_backend(env.dst.id)

    transfer_id = await env.mgr.transfer_artifact(
        db,
        artifact_id=uuid.UUID(str(env.artifact_id)),
        dest_backend_id=dest_uuid,
        requested_by=str(admin_user.id),
    )

    db.expunge_all()
    row = await ops.get_artifact_by_id(db, env.artifact_id)
    assert isinstance(row.storage_backend_id, str)
    assert row.storage_backend_id == str(env.dst.id)
    # Key is never rewritten by a transfer.
    assert row.storage_key == env.key

    transfers = await ops.list_transfers(db)
    assert [t.id for t in transfers] == [transfer_id]
    assert transfers[0].status == "completed"
    assert transfers[0].bytes_transferred == len(b"uuid payload")
    assert transfers[0].dest_backend_id == str(env.dst.id)
    assert transfers[0].requested_by == str(admin_user.id)
    # The bytes really are on the destination mount.
    assert (env.dst_root / env.key).read_bytes() == b"uuid payload"


async def test_transfer_artifact_uuid_requested_by_is_coerced_and_completes(
    db, credential_manager, admin_user, tmp_path,
):
    """A UUID ``requested_by`` is coerced to str(36) and the transfer completes.

    Companion regression guard to
    ``test_upload_artifact_uuid_uploaded_by_is_coerced_and_persisted`` for the
    other ``String(36)`` FK the old ``_id``-only ``_sid_kwargs`` missed:
    ``storage_transfers.requested_by``. ``ops.create_transfer`` runs OUTSIDE
    ``transfer_artifact``'s try/except, so the old bind error produced no
    transfer row at all — the caller just got a DB error and no copy was
    attempted. Now the row is created, the copy runs, and the audit column holds
    the string form of the id.
    """
    env = await _make_transfer_env(
        db, credential_manager, admin_user, tmp_path, payload=b"requested payload",
    )

    transfer_id = await env.mgr.transfer_artifact(
        db, artifact_id=env.artifact_id, dest_backend_id=env.dst.id,
        requested_by=uuid.UUID(str(admin_user.id)),
    )

    transfers = await ops.list_transfers(db)
    assert [t.id for t in transfers] == [transfer_id]
    assert isinstance(transfers[0].requested_by, str)
    assert transfers[0].requested_by == str(admin_user.id)
    assert transfers[0].status == "completed"
    assert transfers[0].bytes_transferred == len(b"requested payload")
    # The copy really happened, and the artifact was repointed.
    assert (env.dst_root / env.key).read_bytes() == b"requested payload"
    db.expunge_all()
    row = await ops.get_artifact_by_id(db, env.artifact_id)
    assert row.storage_backend_id == str(env.dst.id)


async def test_transfer_artifact_uuid_dest_leaves_session_usable(
    db, credential_manager, admin_user, tmp_path,
):
    """After a UUID-typed transfer the session still commits — no PendingRollbackError.

    The old failure mode was not just a 500: the bad bind left the session in a
    rolled-back-pending state, so every later write in the same request also
    failed. Writing another row afterwards proves the session survived.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)
    dest_uuid = uuid.UUID(str(env.dst.id))
    env.mgr._backends[dest_uuid] = env.mgr.get_backend(env.dst.id)

    await env.mgr.transfer_artifact(
        db, artifact_id=env.artifact_id, dest_backend_id=dest_uuid,
    )

    later = await ops.create_job(
        db, name="after-transfer", submitted_by=admin_user.id, steps_config=[],
    )
    assert await ops.get_job_by_id(db, later.id) is not None


async def test_artifact_storage_backend_id_rejects_a_raw_uuid_bind(
    db, session_factory, credential_manager, admin_user, tmp_path,
):
    """Assigning a raw uuid.UUID to artifacts.storage_backend_id fails on commit.

    Proves the ``str()`` in ``transfer_artifact`` is load-bearing rather than
    cosmetic: the column is ``String(36)`` and aiosqlite refuses to bind a UUID.
    Run on a throw-away session so the poisoned transaction cannot affect the
    other assertions in the suite.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)

    async with session_factory() as other:
        row = await ops.get_artifact_by_id(other, env.artifact_id)
        row.storage_backend_id = uuid.UUID(str(env.dst.id))  # deliberately raw
        with pytest.raises(sqlalchemy.exc.StatementError) as excinfo:
            await other.commit()
        assert "UUID" in str(excinfo.value)
        await other.rollback()


async def test_transfer_artifact_uuid_dest_backend_id_not_in_registry_fails(
    db, credential_manager, admin_user, tmp_path,
):
    """A UUID dest_backend_id that is not aliased raises KeyError and fails the transfer.

    POSSIBLE BUG pinned as current behaviour: ``get_backend`` looks the id up in
    a str-keyed dict without coercing, while ``TransferRequest.dest_backend_id``
    is declared ``UUID`` — so the HTTP path hands over a ``uuid.UUID`` and gets
    "Backend <id> not initialized" (404) for a live backend. The transfer row is
    still created and correctly marked 'failed', and the artifact is NOT
    repointed.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)
    dest_uuid = uuid.UUID(str(env.dst.id))

    with pytest.raises(KeyError, match="not initialized"):
        await env.mgr.transfer_artifact(
            db, artifact_id=env.artifact_id, dest_backend_id=dest_uuid,
        )

    transfers = await ops.list_transfers(db)
    assert len(transfers) == 1
    assert transfers[0].status == "failed"
    assert "not initialized" in transfers[0].error
    db.expunge_all()
    row = await ops.get_artifact_by_id(db, env.artifact_id)
    assert row.storage_backend_id == str(env.src.id)
    assert not (env.dst_root / env.key).exists()


async def test_transfer_artifact_string_dest_copies_and_leaves_source(
    db, credential_manager, admin_user, tmp_path,
):
    """The default (copy) transfer leaves the source object in place.

    delete_source=False must be a pure copy: both backends hold the bytes and
    only the artifact's pointer moves.
    """
    env = await _make_transfer_env(
        db, credential_manager, admin_user, tmp_path, payload=b"copy me",
    )

    await env.mgr.transfer_artifact(
        db, artifact_id=env.artifact_id, dest_backend_id=env.dst.id,
    )

    assert (env.src_root / env.key).read_bytes() == b"copy me"
    assert (env.dst_root / env.key).read_bytes() == b"copy me"
    db.expunge_all()
    row = await ops.get_artifact_by_id(db, env.artifact_id)
    assert row.storage_backend_id == str(env.dst.id)


async def test_transfer_artifact_delete_source_removes_source_object(
    db, credential_manager, admin_user, tmp_path,
):
    """delete_source=True turns the copy into a move: source gone, dest present."""
    env = await _make_transfer_env(
        db, credential_manager, admin_user, tmp_path, payload=b"move me",
    )

    await env.mgr.transfer_artifact(
        db, artifact_id=env.artifact_id, dest_backend_id=env.dst.id,
        delete_source=True,
    )

    assert not (env.src_root / env.key).exists()
    assert (env.dst_root / env.key).read_bytes() == b"move me"
    db.expunge_all()
    row = await ops.get_artifact_by_id(db, env.artifact_id)
    assert row.storage_backend_id == str(env.dst.id)


async def test_transfer_artifact_unknown_artifact_creates_no_transfer_row(
    db, credential_manager, admin_user, tmp_path,
):
    """An unknown artifact id raises before any transfer row is created.

    The lookup happens first on purpose — a 404 must not litter the transfers
    table with rows that can never complete.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)

    with pytest.raises(KeyError, match="not found"):
        await env.mgr.transfer_artifact(
            db, artifact_id=uuid.uuid4(), dest_backend_id=env.dst.id,
        )

    assert await ops.list_transfers(db) == []


async def test_transfer_artifact_missing_source_object_marks_transfer_failed(
    db, credential_manager, admin_user, tmp_path,
):
    """If the bytes are gone from the source, the transfer is recorded failed and re-raised.

    Rows and objects are not kept in sync, so a manually deleted object must
    produce a failed transfer (with the error text) rather than a "completed"
    one, and the artifact must keep pointing at the source.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)
    (env.src_root / env.key).unlink()  # bytes vanish behind the artifact row

    with pytest.raises(FileNotFoundError):
        await env.mgr.transfer_artifact(
            db, artifact_id=env.artifact_id, dest_backend_id=env.dst.id,
        )

    transfers = await ops.list_transfers(db)
    assert len(transfers) == 1
    assert transfers[0].status == "failed"
    assert transfers[0].error
    assert transfers[0].bytes_transferred == 0
    db.expunge_all()
    row = await ops.get_artifact_by_id(db, env.artifact_id)
    assert row.storage_backend_id == str(env.src.id)


async def test_transfer_artifact_same_backend_with_delete_source_destroys_object(
    db, credential_manager, admin_user, tmp_path,
):
    """POSSIBLE BUG pinned: source == dest + delete_source deletes the only copy.

    Nothing rejects a transfer whose destination is the artifact's current
    backend. ``stream_to`` copies the object onto itself, the row is marked
    'completed', and THEN ``delete_source`` unlinks that same key — so the
    artifact row survives pointing at bytes that no longer exist, and the
    transfer reports success.
    """
    env = await _make_transfer_env(
        db, credential_manager, admin_user, tmp_path, payload=b"self destruct",
    )

    transfer_id = await env.mgr.transfer_artifact(
        db, artifact_id=env.artifact_id, dest_backend_id=env.src.id,
        delete_source=True,
    )

    assert not (env.src_root / env.key).exists()  # data loss
    transfers = await ops.list_transfers(db)
    assert [t.id for t in transfers] == [transfer_id]
    assert transfers[0].status == "completed"
    db.expunge_all()
    row = await ops.get_artifact_by_id(db, env.artifact_id)
    assert row.storage_backend_id == str(env.src.id)
    assert await env.mgr.get_backend(env.src.id).exists(env.key) is False


async def test_transfer_artifact_same_backend_without_delete_source_is_a_noop(
    db, credential_manager, admin_user, tmp_path,
):
    """A same-backend copy is accepted and leaves the object intact.

    Boundary companion to the delete_source case: the overwrite-with-itself path
    must not truncate or lose the object.
    """
    env = await _make_transfer_env(
        db, credential_manager, admin_user, tmp_path, payload=b"idempotent",
    )

    await env.mgr.transfer_artifact(
        db, artifact_id=env.artifact_id, dest_backend_id=env.src.id,
    )

    assert (env.src_root / env.key).read_bytes() == b"idempotent"
    transfers = await ops.list_transfers(db)
    assert transfers[0].status == "completed"
    assert transfers[0].source_backend_id == transfers[0].dest_backend_id


async def test_transfer_artifact_zero_byte_artifact_records_zero_bytes(
    db, credential_manager, admin_user, tmp_path,
):
    """A 0-byte artifact transfers successfully with bytes_transferred == 0.

    Boundary: 0 must read as "completed, nothing to copy" and not be mistaken
    for a failure by anything inspecting bytes_transferred.
    """
    env = await _make_transfer_env(
        db, credential_manager, admin_user, tmp_path, payload=b"",
    )

    await env.mgr.transfer_artifact(
        db, artifact_id=env.artifact_id, dest_backend_id=env.dst.id,
    )

    transfers = await ops.list_transfers(db)
    assert transfers[0].status == "completed"
    assert transfers[0].bytes_transferred == 0
    assert (env.dst_root / env.key).read_bytes() == b""


async def test_transfer_artifact_nas_to_minio_uses_generic_content_type(
    db, credential_manager, admin_user, tmp_path, monkeypatch,
):
    """A cross-backend transfer lands the bytes on S3 but with octet-stream metadata.

    Covers the NAS -> MinIO path end to end, and pins a side effect of the
    default ``stream_to``: it stages the object in an extension-less temp file,
    so MinIOBackend guesses the type from THAT name and the destination object
    gets 'application/octet-stream' even though the key ends in '.json' and the
    artifact row still says 'application/json'.
    """
    _install_stub_boto3(monkeypatch)
    src_root = tmp_path / "nas-mount"
    src_root.mkdir()
    cred_id = await _make_s3_cred(db, credential_manager, admin_user.id)
    nas_model = await ops.create_storage_backend(
        db, name="nas-src", backend_type="nas",
        config={"mount_path": str(src_root)}, credential_id=cred_id,
        is_active=True,
    )
    minio_model = await ops.create_storage_backend(
        db, name="minio-dst", backend_type="minio",
        config={"bucket": "nexus-artifacts"}, credential_id=cred_id,
        is_active=True,
    )
    mgr = StorageManager(credential_manager=credential_manager)
    await mgr.init_backends(db)
    job = await ops.create_job(
        db, name="x-backend", submitted_by=admin_user.id, steps_config=[],
    )
    local = tmp_path / "report.json"
    local.write_bytes(b'{"ok": true}')
    artifact_id = await mgr.upload_artifact(
        db, local_path=local, remote_key="k/report.json", job_id=job.id,
        backend_id=nas_model.id, content_type="application/json",
    )

    await mgr.transfer_artifact(
        db, artifact_id=artifact_id, dest_backend_id=minio_model.id,
    )

    stub = mgr.get_backend(minio_model.id)._client
    assert stub.objects["k/report.json"] == b'{"ok": true}'
    assert stub.extra_args["k/report.json"] == {
        "ContentType": "application/octet-stream",
    }
    db.expunge_all()
    row = await ops.get_artifact_by_id(db, artifact_id)
    assert row.storage_backend_id == str(minio_model.id)
    # The row's content_type is untouched by the transfer, so it now disagrees
    # with the object metadata on the destination.
    assert row.content_type == "application/json"


async def test_transfer_artifact_dest_failure_marks_failed_and_keeps_source(
    db, credential_manager, admin_user, tmp_path, monkeypatch,
):
    """An error while writing to the destination fails the transfer, source untouched.

    The upload half of the copy is where a full/denied destination shows up; the
    artifact must keep resolving to the source backend afterwards.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)
    dest_backend = env.mgr.get_backend(env.dst.id)

    async def _explode(local_path, remote_key, content_type=None):
        """Stand in for a destination that rejects the write."""
        raise PermissionError(errno.EACCES, "destination read-only")

    monkeypatch.setattr(dest_backend, "upload", _explode)

    with pytest.raises(PermissionError):
        await env.mgr.transfer_artifact(
            db, artifact_id=env.artifact_id, dest_backend_id=env.dst.id,
            delete_source=True,
        )

    transfers = await ops.list_transfers(db)
    assert transfers[0].status == "failed"
    # delete_source never ran, so the original is still there.
    assert (env.src_root / env.key).read_bytes() == env.payload
    db.expunge_all()
    row = await ops.get_artifact_by_id(db, env.artifact_id)
    assert row.storage_backend_id == str(env.src.id)


async def test_transfer_artifact_delete_source_failure_reports_failed_after_copy(
    db, credential_manager, admin_user, tmp_path, monkeypatch,
):
    """A failing delete_source rewrites the row from 'completed' back to 'failed'.

    Pins the documented ordering oddity: the copy already succeeded and the
    bytes are on the destination, yet the transfer is reported as failed and the
    artifact is never repointed — so the row still resolves to the source.
    """
    env = await _make_transfer_env(db, credential_manager, admin_user, tmp_path)
    source_backend = env.mgr.get_backend(env.src.id)

    async def _explode(remote_key):
        """Stand in for a source that refuses the delete."""
        raise PermissionError(errno.EACCES, "source read-only")

    monkeypatch.setattr(source_backend, "delete", _explode)

    with pytest.raises(PermissionError):
        await env.mgr.transfer_artifact(
            db, artifact_id=env.artifact_id, dest_backend_id=env.dst.id,
            delete_source=True,
        )

    transfers = await ops.list_transfers(db)
    assert transfers[0].status == "failed"
    # The copy DID happen before the failure.
    assert (env.dst_root / env.key).read_bytes() == env.payload
    db.expunge_all()
    row = await ops.get_artifact_by_id(db, env.artifact_id)
    assert row.storage_backend_id == str(env.src.id)
