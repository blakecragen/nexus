"""Integration tests for the artifact routes and the job results-tarball endpoints.

SUT (primary): ``packages/server/src/nexus_server/api/routes/artifacts.py`` —
mounted at ``/api/artifacts``. That module is deliberately tiny: one read-only
``GET ""`` handler plus the ``_artifact_to_info`` projection. There is no upload
or download of artifact *bytes* anywhere in it — an ``Artifact`` row is only an
index entry pointing at a storage backend, and streaming is the storage layer's
job.

SUT (secondary): the results-tarball endpoints in
``packages/server/src/nexus_server/api/routes/jobs.py`` —
``PUT /api/jobs/{id}/results`` (agent upload, node-key auth),
``GET /api/jobs/{id}/results/download``, ``GET /api/jobs/{id}/results/manifest``
and the ``has_results`` flag on ``GET /api/jobs/{id}``. These live in
``jobs.py`` rather than ``artifacts.py`` but are the actual artifact
upload/download/manifest surface of the server, so they are covered here.
``tests/integration/test_jobs_routes.py`` already pins the two "no tarball yet"
404s; this file owns the happy paths, the node-key auth branches, the overwrite
semantics and the corrupt-archive 500.

Two axes get explicit attention
    * **ID coercion.** Every id column is ``String(36)``; binding a raw
      ``uuid.UUID`` makes aiosqlite raise ``type 'UUID' is not supported`` and
      500s the request. ``list_artifacts`` receives a ``uuid.UUID`` straight
      from FastAPI's path/query conversion, so the coercion in ``ops._sid`` /
      ``ops._sid_kwargs`` is exercised both over HTTP and by calling the ops
      functions directly with ``uuid.UUID`` objects.
    * **Filesystem state.** ``jobs.RESULTS_DIR`` is a *relative* path
      (``.nexus-results``). The autouse ``results_dir`` fixture repoints it at
      ``tmp_path`` so no test can litter the repository, and so ``has_results``
      starts False for every test.

Status-code contract exercised here
    * 200 — list (possibly empty), upload accepted, download, manifest.
    * 401 — every credential failure: a bad/absent ``X-Node-Key`` on upload, a
      missing ``Authorization`` header (``HTTPBearer(auto_error=True)`` →
      ``{"detail": "Not authenticated"}``), or a malformed bearer token
      (``get_current_user`` → ``{"detail": "Could not validate credentials"}``).
      The two bearer cases are told apart by ``detail``, not by status code —
      note that the ``AI Note`` in ``api/deps.py`` still claims ``HTTPBearer``
      answers 403, which is stale for the pinned FastAPI (0.129).
    * 404 — unknown job on the results endpoints. Never on ``/api/artifacts``:
      an unknown ``job_id`` there is an empty list by design.
    * 422 — ``job_id`` missing or not a UUID.
    * 500 — the tarball exists but is not a readable gzip.

One uncaught-exception path is documented rather than asserted as a 500: a
*truncated* gzip makes ``tarfile`` raise ``EOFError``, which is neither
``TarError`` nor ``OSError`` and therefore escapes the manifest handler's
``except``. See ``test_results_manifest_of_a_truncated_gzip_raises_eoferror``.
"""

from __future__ import annotations

import io
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from nexus_server.db import ops


# ── Fixtures / helpers ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def results_dir(tmp_path, monkeypatch):
    """Repoint ``jobs.RESULTS_DIR`` at a per-test temp directory.

    ``RESULTS_DIR`` is the module-level relative ``Path(".nexus-results")``, and
    ``_job_results_path`` dereferences it at call time — so a plain
    ``monkeypatch.setattr`` is enough to redirect upload, ``has_results``,
    download and manifest all at once.

    Autouse on purpose: without it the upload tests would write tarballs into
    the repository working directory, and every ``has_results`` assertion would
    depend on whatever a previous run left behind there.

    Returns:
        The ``Path`` now serving as ``RESULTS_DIR``. It is intentionally *not*
        created here — the upload handler's own ``mkdir(parents=True)`` is part
        of what these tests verify.
    """
    from nexus_server.api.routes import jobs as jobs_routes

    target = tmp_path / "nexus-results"
    monkeypatch.setattr(jobs_routes, "RESULTS_DIR", target)
    return target


@pytest_asyncio.fixture
async def storage_backend(db, admin_user):
    """A persisted ``StorageBackend`` row for artifacts to point at.

    ``Artifact.storage_backend_id`` is a non-nullable FK, so artifact rows need
    a backend to reference. Nothing in these tests instantiates a live client
    for it — the artifact routes never touch storage, they only echo the id back
    — so the config blob is a dummy.

    Returns:
        The persisted ``StorageBackend`` named ``artifact-store``.
    """
    credential = await ops.create_credential(
        db,
        name="artifact-store-cred",
        credential_type="minio",
        encrypted_fields=b"encrypted-blob",
        owner_id=admin_user.id,
    )
    return await ops.create_storage_backend(
        db,
        name="artifact-store",
        backend_type="minio",
        credential_id=credential.id,
        config={"bucket": "nexus"},
        is_active=True,
    )


@pytest_asyncio.fixture
async def job(db, regular_user):
    """A persisted pending job that owns the artifacts under test.

    Returns:
        A ``Job`` submitted by ``regular_user`` with a single ``run_command``
        step, matching what the ``auth_client`` identity would have created.
    """
    return await ops.create_job(
        db,
        name="artifact-job",
        submitted_by=regular_user.id,
        steps_config=[{"step": "run_command", "params": {"command": "echo hi"}}],
    )


_BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _make_artifact(db, job, storage_backend, *, filename="out.txt",
                         created_offset_s=0, **overrides):
    """Persist one ``Artifact`` row with a deterministic ``created_at``.

    ``created_at`` defaults to ``_utcnow()``, which would make ordering
    assertions depend on wall-clock resolution. Passing an explicit,
    monotonically increasing timestamp makes the oldest-first ordering
    assertion exact rather than probabilistic.

    Args:
        db: Test session.
        job: Owning ``Job``.
        storage_backend: Backend the row points at.
        filename: Display name; also used as the default ``storage_key`` suffix.
        created_offset_s: Seconds added to ``_BASE_TIME`` for this row.
        **overrides: Any other ``Artifact`` column value.

    Returns:
        The persisted ``Artifact``.
    """
    kwargs = {
        "job_id": job.id,
        "filename": filename,
        "storage_backend_id": storage_backend.id,
        "storage_key": f"jobs/{job.id}/{filename}",
        "content_type": "text/plain",
        "size_bytes": 128,
        "created_at": _BASE_TIME + timedelta(seconds=created_offset_s),
    }
    kwargs.update(overrides)
    return await ops.create_artifact(db, **kwargs)


def _tar_gz_bytes(members):
    """Build an in-memory ``.tar.gz`` payload.

    Args:
        members: Iterable of ``(name, content)`` pairs. ``content=None`` adds a
            directory member instead of a regular file, so manifest tests can
            assert on the ``is_dir`` flag.

    Returns:
        bytes: The gzipped tar archive.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                payload = content.encode()
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _upload(client, job_id, node_key, payload, *, filename="results.tar.gz"):
    """PUT a results tarball as an agent would.

    Args:
        client: Any ``TestClient``; the endpoint takes no ``CurrentUser`` so the
            client's bearer header (if any) is irrelevant.
        job_id: Target job id (str or UUID).
        node_key: Value for the ``X-Node-Key`` header, or ``None`` to omit the
            header entirely — the two are distinct branches in the handler's
            ``request.headers.get("X-Node-Key", "")`` / truthiness check.
        payload: Raw bytes to upload.
        filename: Multipart filename; the server ignores it and always writes
            ``results.tar.gz``.

    Returns:
        The ``httpx.Response``.
    """
    headers = {} if node_key is None else {"X-Node-Key": node_key}
    return client.put(
        f"/api/jobs/{job_id}/results",
        files={"file": (filename, payload, "application/gzip")},
        headers=headers,
    )


# ── GET /api/artifacts — happy path + projection ──────────────────────────


async def test_list_artifacts_returns_job_artifacts_oldest_first(
    auth_client, db, job, storage_backend
):
    """Artifacts come back ordered by ``created_at`` ascending.

    ``ops.list_artifacts_for_job`` orders by ``created_at``, and the route
    preserves that order. The UI renders the list as a chronological production
    log, so a reversal (or an unordered query relying on insertion order) would
    silently show the newest file first. Rows are seeded out of chronological
    order to prove the ORDER BY is doing the work rather than the insert order.
    """
    await _make_artifact(db, job, storage_backend, filename="third.txt", created_offset_s=30)
    await _make_artifact(db, job, storage_backend, filename="first.txt", created_offset_s=10)
    await _make_artifact(db, job, storage_backend, filename="second.txt", created_offset_s=20)

    resp = auth_client.get("/api/artifacts", params={"job_id": str(job.id)})

    assert resp.status_code == 200, resp.text
    assert [a["filename"] for a in resp.json()] == ["first.txt", "second.txt", "third.txt"]


async def test_list_artifacts_serialises_every_declared_public_field(
    auth_client, db, job, storage_backend
):
    """The response carries exactly the ``ArtifactInfo`` field set with real values.

    ``_artifact_to_info`` maps field by field rather than using
    ``from_attributes`` precisely so a new ``artifacts`` column cannot silently
    widen the API. Pinning the exact key set here is what makes that guarantee
    testable: adding a column and wiring it into the schema will fail this test
    and force a deliberate decision.
    """
    step_run = await ops.create_step_run(
        db, job_id=str(job.id), step_index=0, step_name="run_command"
    )
    artifact = await _make_artifact(
        db, job, storage_backend,
        filename="m5out.tar.gz",
        step_run_id=step_run.id,
        content_type="application/gzip",
        size_bytes=4096,
    )

    resp = auth_client.get("/api/artifacts", params={"job_id": str(job.id)})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert set(row) == {
        "id", "job_id", "step_run_id", "filename", "storage_backend_id",
        "storage_backend_name", "storage_key", "content_type", "size_bytes",
        "created_at",
    }
    assert row["id"] == str(artifact.id)
    assert row["job_id"] == str(job.id)
    assert row["step_run_id"] == str(step_run.id)
    assert row["storage_backend_id"] == str(storage_backend.id)
    assert row["storage_key"] == artifact.storage_key
    assert row["content_type"] == "application/gzip"
    assert row["size_bytes"] == 4096


async def test_list_artifacts_leaves_storage_backend_name_unset(
    auth_client, db, job, storage_backend
):
    """``storage_backend_name`` is always null — the route never joins the backend.

    Resolving the name would cost a join per row, so the field is documented as
    deliberately unset and the frontend labels the row from its cached
    ``/api/storage`` list. A future change that starts populating it must be a
    conscious one, hence this assertion.
    """
    await _make_artifact(db, job, storage_backend)

    resp = auth_client.get("/api/artifacts", params={"job_id": str(job.id)})

    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["storage_backend_name"] is None


async def test_list_artifacts_includes_rows_with_no_step_run(
    auth_client, db, job, storage_backend
):
    """An artifact attributed to the job rather than a step serialises with a null id.

    ``Artifact.step_run_id`` is nullable — the results tarball belongs to the job
    as a whole. ``ArtifactInfo.step_run_id`` must therefore stay optional; a
    non-optional declaration would make these rows raise at response time and
    take the whole list down with them.
    """
    await _make_artifact(db, job, storage_backend, step_run_id=None)

    resp = auth_client.get("/api/artifacts", params={"job_id": str(job.id)})

    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["step_run_id"] is None


def test_artifact_to_info_coerces_missing_size_bytes_to_zero():
    """A null ``size_bytes`` is published as 0 rather than failing validation.

    ``ArtifactInfo.size_bytes`` is a non-optional ``int`` while the column can
    read back as ``None`` for a row registered before its upload finished. The
    ``or 0`` in ``_artifact_to_info`` is what keeps that row from raising a
    Pydantic error and 500-ing the entire list response. Exercised through a
    duck-typed stand-in because the ``artifacts`` table itself rejects a NULL in
    that column — the projection is documented as accepting any object with the
    right attributes.
    """
    from nexus_server.api.routes.artifacts import _artifact_to_info

    info = _artifact_to_info(SimpleNamespace(
        id=uuid.uuid4(), job_id=uuid.uuid4(), step_run_id=None,
        filename="pending.bin", storage_backend_id=uuid.uuid4(),
        storage_key="k", content_type=None, size_bytes=None,
        created_at=_BASE_TIME,
    ))

    assert info.size_bytes == 0
    assert info.storage_backend_name is None


def test_artifact_to_info_preserves_a_zero_size_artifact():
    """A genuinely empty artifact stays 0 and is not confused with "unknown".

    Boundary companion to the null case: ``or 0`` collapses both ``None`` and
    ``0`` to 0, so this test documents that a legitimately empty file is
    representable and does not become an error or a sentinel.
    """
    from nexus_server.api.routes.artifacts import _artifact_to_info

    info = _artifact_to_info(SimpleNamespace(
        id=uuid.uuid4(), job_id=uuid.uuid4(), step_run_id=None,
        filename="empty.txt", storage_backend_id=uuid.uuid4(),
        storage_key="k", content_type="text/plain", size_bytes=0,
        created_at=_BASE_TIME,
    ))

    assert info.size_bytes == 0


# ── GET /api/artifacts — scoping and empty results ────────────────────────


async def test_list_artifacts_is_scoped_to_the_requested_job(
    auth_client, db, job, storage_backend, regular_user
):
    """Only the requested job's artifacts are returned; siblings are excluded.

    ``job_id`` is a required query parameter specifically so this endpoint
    cannot enumerate the whole cluster. A missing WHERE clause would leak every
    job's file list to any authenticated caller, which this test would catch.
    """
    other_job = await ops.create_job(
        db, name="other-job", submitted_by=regular_user.id, steps_config=[],
    )
    await _make_artifact(db, job, storage_backend, filename="mine.txt")
    await _make_artifact(db, other_job, storage_backend, filename="theirs.txt")

    resp = auth_client.get("/api/artifacts", params={"job_id": str(job.id)})

    assert resp.status_code == 200, resp.text
    assert [a["filename"] for a in resp.json()] == ["mine.txt"]


async def test_list_artifacts_for_job_without_artifacts_returns_empty_list(
    auth_client, job
):
    """A real job that produced nothing yields ``[]`` with a 200.

    The frontend renders "no artifacts" from an empty array; a 404 here would
    force it to special-case the difference between "job has no files" and "job
    does not exist".
    """
    resp = auth_client.get("/api/artifacts", params={"job_id": str(job.id)})

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_list_artifacts_unknown_job_returns_empty_list_not_404(auth_client):
    """An unknown ``job_id`` is also ``[]`` — the route never checks job existence.

    Documented behaviour: there is no ``ops.get_job_by_id`` call in this handler,
    so a stale job id in a bookmarked URL degrades to an empty list rather than
    an error page. Contrast with the results endpoints in ``jobs.py``, which do
    404 on an unknown job.
    """
    resp = auth_client.get("/api/artifacts", params={"job_id": str(uuid.uuid4())})

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_list_artifacts_accepts_an_uppercase_uuid_spelling(
    auth_client, db, job, storage_backend
):
    """A hex-uppercase ``job_id`` matches the lowercase id stored in the column.

    FastAPI parses the query value into a ``uuid.UUID``, and ``ops._sid`` then
    stringifies it back to the canonical lowercase hyphenated form before the
    comparison. Without that normalisation the ``String(36)`` equality test
    would be case-sensitive and this lookup would silently return nothing.
    """
    await _make_artifact(db, job, storage_backend, filename="cased.txt")

    resp = auth_client.get("/api/artifacts", params={"job_id": str(job.id).upper()})

    assert resp.status_code == 200, resp.text
    assert [a["filename"] for a in resp.json()] == ["cased.txt"]


async def test_regular_user_can_list_another_users_job_artifacts(
    auth_client, db, admin_user, storage_backend
):
    """``CurrentUser`` is authentication only — there is no per-job ownership check.

    Documents the current single-tenant posture called out in the route's own
    note: any logged-in user can enumerate any job's artifacts. If a
    ``require_pool_access``-style guard is ever added, this test is the one that
    must be rewritten, which is exactly the review signal we want.
    """
    admin_job = await ops.create_job(
        db, name="admins-job", submitted_by=admin_user.id, steps_config=[],
    )
    await _make_artifact(db, admin_job, storage_backend, filename="secret.txt")

    resp = auth_client.get("/api/artifacts", params={"job_id": str(admin_job.id)})

    assert resp.status_code == 200, resp.text
    assert [a["filename"] for a in resp.json()] == ["secret.txt"]


# ── GET /api/artifacts — auth + validation ────────────────────────────────


def test_list_artifacts_without_authorization_header_is_rejected(client, job):
    """No bearer header at all → 401 ``{"detail": "Not authenticated"}``.

    Rejected by ``HTTPBearer(auto_error=True)`` before the handler (and before
    any DB query) runs, which is why the detail differs from the
    ``get_current_user`` failure below. Asserting on the detail rather than only
    the code keeps the two distinguishable even though both are 401 — the note
    in ``api/deps.py`` predicting a 403 here no longer matches FastAPI 0.129.
    """
    resp = client.get("/api/artifacts", params={"job_id": str(job.id)})

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Not authenticated"


def test_list_artifacts_with_malformed_bearer_token_returns_401(client, job):
    """A syntactically valid but unverifiable token → 401, not 500.

    Reaches ``get_current_user``, which collapses every decode failure into one
    generic 401 so token probing cannot distinguish "expired" from "bad
    signature".
    """
    resp = client.get(
        "/api/artifacts",
        params={"job_id": str(job.id)},
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Could not validate credentials"


def test_list_artifacts_without_job_id_returns_422(auth_client):
    """Omitting the required ``job_id`` query parameter is a 422.

    ``job_id`` has no default, so FastAPI rejects the request before the handler
    runs. That is the mechanism preventing an unscoped "list every artifact"
    call, so it must stay a hard validation error rather than defaulting to
    ``None``.
    """
    resp = auth_client.get("/api/artifacts")

    assert resp.status_code == 422, resp.text
    assert any(err["loc"] == ["query", "job_id"] for err in resp.json()["detail"])


@pytest.mark.parametrize("bad_job_id", ["not-a-uuid", "", "12345", "None"])
def test_list_artifacts_rejects_non_uuid_job_id_with_422(auth_client, bad_job_id):
    """A ``job_id`` that is not a UUID is a 422 before any query is issued.

    The ``UUID`` annotation is the input filter that keeps arbitrary strings out
    of the ``String(36)`` comparison. Several spellings are checked because an
    empty string and the literal ``"None"`` are the two values a buggy frontend
    is most likely to send.
    """
    resp = auth_client.get("/api/artifacts", params={"job_id": bad_job_id})

    assert resp.status_code == 422, resp.text


# ── ops-level ID coercion probes ──────────────────────────────────────────


async def test_list_artifacts_for_job_accepts_a_raw_uuid_object(
    db, job, storage_backend
):
    """``ops.list_artifacts_for_job`` tolerates a ``uuid.UUID`` for ``job_id``.

    This is the exact shape the route hands it (FastAPI converts the query
    parameter to a ``uuid.UUID``). Every id column is ``String(36)``, so binding
    the object unwrapped raises ``sqlite3.ProgrammingError: type 'UUID' is not
    supported`` and poisons the session. ``_sid`` is the guard; this test fails
    the moment someone drops it.
    """
    await _make_artifact(db, job, storage_backend, filename="probe.txt")

    rows = await ops.list_artifacts_for_job(db, uuid.UUID(str(job.id)))

    assert [r.filename for r in rows] == ["probe.txt"]


async def test_create_and_get_artifact_accept_raw_uuid_ids(db, job, storage_backend):
    """``create_artifact`` / ``get_artifact_by_id`` coerce ``uuid.UUID`` ids.

    ``create_artifact`` funnels ``**kwargs`` through ``_sid_kwargs``, which
    stringifies every ``*_id`` key before the model constructor sees it, and
    ``get_artifact_by_id`` wraps its argument in ``_sid``. Passing UUID objects
    for ``job_id``, ``storage_backend_id`` and the primary key at once proves all
    three paths are covered, and that the written row is retrievable by either
    spelling.
    """
    artifact = await ops.create_artifact(
        db,
        job_id=uuid.UUID(str(job.id)),
        filename="uuid-probe.bin",
        storage_backend_id=uuid.UUID(str(storage_backend.id)),
        storage_key="k/uuid-probe.bin",
        size_bytes=1,
    )

    assert artifact.job_id == str(job.id)
    assert artifact.storage_backend_id == str(storage_backend.id)

    by_uuid = await ops.get_artifact_by_id(db, uuid.UUID(str(artifact.id)))
    by_str = await ops.get_artifact_by_id(db, str(artifact.id))
    assert by_uuid is not None and by_str is not None
    assert by_uuid.id == by_str.id == artifact.id


async def test_get_artifact_by_id_returns_none_for_unknown_id(db):
    """An unknown artifact id resolves to ``None`` rather than raising.

    The download/manifest callers branch on ``None`` to produce a 404, so a
    raising lookup would surface as a 500 for the common "artifact was deleted"
    case.
    """
    assert await ops.get_artifact_by_id(db, uuid.uuid4()) is None


# ── PUT /api/jobs/{id}/results — agent upload (node-key auth) ─────────────


async def test_upload_job_results_with_valid_node_key_writes_the_tarball(
    client, db, job, sample_node, results_dir
):
    """A node-authenticated PUT stores the bytes and reports the size written.

    This is the agent-side half of result collection: ``gem5_collect_results``
    tars ``m5out`` and PUTs it with the node's ``api_key``. The response's
    ``size_bytes`` is the count accumulated by the chunked write loop, so
    asserting it equals ``len(payload)`` proves no chunk was dropped, and the
    on-disk comparison proves the layout ``RESULTS_DIR/<job_id>/results.tar.gz``.
    """
    payload = _tar_gz_bytes([("m5out/stats.txt", "sim_seconds 1.0\n")])

    resp = _upload(client, job.id, sample_node.api_key, payload)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "size_bytes": len(payload)}
    stored = results_dir / str(job.id) / "results.tar.gz"
    assert stored.is_file()
    assert stored.read_bytes() == payload


async def test_upload_job_results_creates_the_results_directory(
    client, job, sample_node, results_dir
):
    """The handler creates ``RESULTS_DIR/<job_id>/`` itself on first upload.

    ``RESULTS_DIR`` does not exist on a fresh server, so the
    ``mkdir(parents=True, exist_ok=True)`` is load-bearing — without it the very
    first upload of a deployment would fail with ``FileNotFoundError`` and 500.
    """
    assert not results_dir.exists()

    resp = _upload(client, job.id, sample_node.api_key, b"payload-bytes")

    assert resp.status_code == 200, resp.text
    assert (results_dir / str(job.id)).is_dir()


async def test_upload_job_results_without_node_key_header_returns_401(
    client, job, results_dir
):
    """An absent ``X-Node-Key`` header is rejected before anything is written.

    The handler defaults the missing header to ``""`` and skips the lookup
    entirely on that falsy value, so this branch is distinct from "key present
    but unknown". Asserting the directory was not created proves the auth check
    runs before the ``mkdir``/write.
    """
    resp = _upload(client, job.id, None, b"payload")

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Invalid node key"
    assert not (results_dir / str(job.id)).exists()


async def test_upload_job_results_with_empty_node_key_returns_401(client, job):
    """An empty ``X-Node-Key`` value is rejected without querying for it.

    Boundary case between the two 401 branches: ``""`` is falsy, so
    ``get_node_by_api_key`` is never called. A refactor that dropped the
    truthiness guard would query for the empty key instead — harmless today, but
    it would authenticate any node whose ``api_key`` column was NULL-to-empty.
    """
    resp = _upload(client, job.id, "", b"payload")

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Invalid node key"


async def test_upload_job_results_with_unknown_node_key_returns_401(
    client, job, sample_node
):
    """A well-formed but unregistered node key is a 401.

    ``get_node_by_api_key`` is a plain equality lookup, so an attacker-supplied
    key must resolve to ``None`` and be refused. ``sample_node`` exists so the
    test proves the *value* was rejected, not that the table was simply empty.
    """
    resp = _upload(client, job.id, "definitely-not-a-real-node-key", b"payload")

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Invalid node key"


async def test_upload_job_results_unknown_job_returns_404(client, sample_node):
    """A valid node key uploading for a nonexistent job gets a 404.

    The job-existence check exists so a typo'd/stale job id cannot create an
    orphan directory under ``RESULTS_DIR`` that nothing will ever serve or clean
    up.
    """
    resp = _upload(client, uuid.uuid4(), sample_node.api_key, b"payload")

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Job not found"


async def test_upload_job_results_checks_node_key_before_job_existence(client):
    """An unauthenticated upload for an unknown job reports 401, not 404.

    Ordering matters for information disclosure: checking the job first would let
    an unauthenticated caller probe which job ids exist by reading the status
    code. Both failure conditions hold here, and the auth failure must win.
    """
    resp = _upload(client, uuid.uuid4(), "bogus-key", b"payload")

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Invalid node key"


async def test_upload_job_results_twice_overwrites_with_the_last_payload(
    client, job, sample_node, results_dir
):
    """Re-uploading replaces the previous tarball — last writer wins.

    Documented semantics: the file is opened ``"wb"`` at a fixed path with no
    versioning. A re-run of the collect step therefore supersedes the old
    archive rather than appending to it or 409-ing, which is what makes retrying
    a failed collection safe. The length assertion rules out an append.
    """
    first = _tar_gz_bytes([("m5out/old.txt", "old" * 100)])
    second = _tar_gz_bytes([("m5out/new.txt", "new")])

    assert _upload(client, job.id, sample_node.api_key, first).status_code == 200
    resp = _upload(client, job.id, sample_node.api_key, second)

    assert resp.status_code == 200, resp.text
    stored = results_dir / str(job.id) / "results.tar.gz"
    assert stored.read_bytes() == second
    assert stored.stat().st_size == len(second)


async def test_upload_job_results_accepts_a_multi_chunk_payload(
    client, job, sample_node, results_dir
):
    """A payload larger than the 1 MiB read size is written in full.

    The handler streams with ``while chunk := await file.read(1024 * 1024)`` so a
    multi-gigabyte ``m5out`` archive never has to fit in memory. An off-by-one in
    that loop (e.g. reading once instead of looping) would truncate every real
    upload; a payload spanning three chunks catches it.
    """
    payload = bytes(range(256)) * (1024 * 9)  # ~2.25 MiB, spans three reads
    assert len(payload) > 2 * 1024 * 1024

    resp = _upload(client, job.id, sample_node.api_key, payload)

    assert resp.status_code == 200, resp.text
    assert resp.json()["size_bytes"] == len(payload)
    assert (results_dir / str(job.id) / "results.tar.gz").read_bytes() == payload


async def test_upload_empty_file_creates_a_zero_byte_tarball(
    client, job, sample_node, results_dir
):
    """A zero-length upload succeeds and reports ``size_bytes == 0``.

    The chunk loop's first read returns ``b""``, so the ``while`` never executes
    and the file is created empty. Documents the consequence: ``has_results``
    flips to True for a file that no tar reader can open — the same state a
    connection dropped mid-upload leaves behind (see the corrupt-archive test).
    """
    resp = _upload(client, job.id, sample_node.api_key, b"")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "size_bytes": 0}
    stored = results_dir / str(job.id) / "results.tar.gz"
    assert stored.is_file()
    assert stored.stat().st_size == 0


async def test_upload_job_results_ignores_the_client_supplied_filename(
    client, job, sample_node, results_dir
):
    """The multipart filename is discarded; the server always writes ``results.tar.gz``.

    ``_job_results_path`` is the single source of truth for the layout, so an
    agent (or attacker) cannot influence the destination path through the upload
    field name — a traversal filename lands nowhere near the parent directory.
    """
    resp = _upload(
        client, job.id, sample_node.api_key, b"payload",
        filename="../../escape.tar.gz",
    )

    assert resp.status_code == 200, resp.text
    assert (results_dir / str(job.id) / "results.tar.gz").read_bytes() == b"payload"
    assert list((results_dir / str(job.id)).iterdir()) == [
        results_dir / str(job.id) / "results.tar.gz"
    ]


def test_job_results_path_resolves_uuid_and_string_identically():
    """``_job_results_path`` maps a UUID and its string form to the same file.

    Upload receives a ``uuid.UUID`` from the path converter while other callers
    pass strings from the ORM. If the two spellings disagreed, a tarball would be
    written to one directory and looked up in another — ``has_results`` False and
    a 404 download for a job that uploaded successfully.
    """
    from nexus_server.api.routes.jobs import _job_results_path

    job_id = uuid.uuid4()
    assert _job_results_path(job_id) == _job_results_path(str(job_id))
    assert _job_results_path(job_id).name == "results.tar.gz"


# ── has_results on GET /api/jobs/{id} ─────────────────────────────────────


async def test_job_detail_has_results_is_false_before_any_upload(auth_client, job):
    """``has_results`` starts False so the UI hides the results tab.

    Derived from a filesystem ``is_file()`` rather than the DB, so this also
    confirms the flag is not accidentally driven by the existence of the job's
    directory or by any ``Artifact`` row.
    """
    resp = auth_client.get(f"/api/jobs/{job.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["has_results"] is False


async def test_job_detail_has_results_flips_true_after_upload(
    auth_client, job, sample_node
):
    """Uploading a tarball makes ``has_results`` True on the job detail payload.

    This is the handshake that reveals the Download button and results tree in
    the UI: the agent PUTs the archive and the very next detail poll must report
    it. Because the flag is a ``stat()`` on ``RESULTS_DIR``, it also proves
    upload and ``has_results`` agree on the path.
    """
    assert _upload(auth_client, job.id, sample_node.api_key,
                   _tar_gz_bytes([("m5out/stats.txt", "x")])).status_code == 200

    resp = auth_client.get(f"/api/jobs/{job.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["has_results"] is True


async def test_job_detail_has_results_true_for_an_unreadable_archive(
    auth_client, job, sample_node
):
    """``has_results`` only checks existence — a corrupt tarball still reports True.

    Documents the known gap called out in the source: a truncated upload leaves a
    file behind, so the UI offers a results tree that the manifest endpoint then
    fails to build (500). Recovery is deleting the file, not clearing a DB flag.
    """
    assert _upload(auth_client, job.id, sample_node.api_key,
                   b"this is not a gzip stream").status_code == 200

    resp = auth_client.get(f"/api/jobs/{job.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["has_results"] is True


# ── GET /api/jobs/{id}/results/download ───────────────────────────────────


async def test_download_job_results_streams_the_uploaded_bytes(
    auth_client, job, sample_node
):
    """The download returns the exact bytes that were uploaded, as an attachment.

    Round-trips the upload/download pair through HTTP so a change to either half
    (chunking, media type, path derivation) is caught. The
    ``Content-Disposition`` filename embeds the job id, which is what makes
    multiple downloaded tarballs distinguishable in a browser's download folder.
    """
    payload = _tar_gz_bytes([("m5out/stats.txt", "sim_seconds 2.0\n")])
    assert _upload(auth_client, job.id, sample_node.api_key, payload).status_code == 200

    resp = auth_client.get(f"/api/jobs/{job.id}/results/download")

    assert resp.status_code == 200, resp.text
    assert resp.content == payload
    assert resp.headers["content-type"] == "application/gzip"
    assert f"job_{job.id}_results.tar.gz" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].startswith("attachment;")


def test_download_job_results_without_authorization_is_rejected(client, job):
    """The download requires a bearer token — 401 "Not authenticated" when absent.

    Unlike the upload endpoint (node key), download is a user-facing route
    guarded by ``CurrentUser``. Leaving it open would make every job's results
    world-readable to anyone who can guess a job id.
    """
    resp = client.get(f"/api/jobs/{job.id}/results/download")

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Not authenticated"


def test_download_job_results_with_invalid_token_returns_401(client, job):
    """An unverifiable token is a 401 with the generic credentials message.

    Pinned alongside the missing-header case so the two auth failure modes stay
    distinguishable by ``detail`` even though FastAPI answers both with 401.
    """
    resp = client.get(
        f"/api/jobs/{job.id}/results/download",
        headers={"Authorization": "Bearer garbage.token.value"},
    )

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Could not validate credentials"


# ── GET /api/jobs/{id}/results/manifest ───────────────────────────────────


async def test_results_manifest_lists_every_archive_member(
    auth_client, job, sample_node
):
    """The manifest enumerates each tar member with its path, size and dir flag.

    This payload is what ``ResultsTree`` turns into the file tree on the Job
    Detail page, so all three keys are contract. Directory members must report
    ``is_dir=True`` with size 0 while files report their *uncompressed* size —
    the tree indents on the former and renders a byte count from the latter.
    """
    payload = _tar_gz_bytes([
        ("m5out", None),
        ("m5out/stats.txt", "a" * 40),
        ("m5out/config.ini", "b" * 12),
    ])
    assert _upload(auth_client, job.id, sample_node.api_key, payload).status_code == 200

    resp = auth_client.get(f"/api/jobs/{job.id}/results/manifest")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"archive_bytes", "entries"}
    by_path = {e["path"]: e for e in body["entries"]}
    assert set(by_path) == {"m5out", "m5out/stats.txt", "m5out/config.ini"}
    assert by_path["m5out"]["is_dir"] is True
    assert by_path["m5out"]["size"] == 0
    assert by_path["m5out/stats.txt"]["is_dir"] is False
    assert by_path["m5out/stats.txt"]["size"] == 40
    assert by_path["m5out/config.ini"]["size"] == 12


async def test_results_manifest_reports_the_compressed_size_on_disk(
    auth_client, job, sample_node
):
    """``archive_bytes`` is the gzipped size on disk, not the sum of member sizes.

    The two differ by design — the UI shows "download is N bytes" next to
    per-file uncompressed sizes. Since the members here compress well, asserting
    ``archive_bytes < sum(member sizes)`` proves the field is a ``stat()`` on the
    file rather than a total of the entries.
    """
    payload = _tar_gz_bytes([("m5out/stats.txt", "z" * 20000)])
    assert _upload(auth_client, job.id, sample_node.api_key, payload).status_code == 200

    resp = auth_client.get(f"/api/jobs/{job.id}/results/manifest")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["archive_bytes"] == len(payload)
    assert body["archive_bytes"] < sum(e["size"] for e in body["entries"])


async def test_results_manifest_of_an_empty_archive_returns_no_entries(
    auth_client, job, sample_node
):
    """A valid but memberless tarball yields ``entries == []`` with a 200.

    Boundary between "no results" (404) and "results that contain nothing"
    (200 + empty tree). Collapsing the two would make a successful-but-empty
    collection look like a failed upload.
    """
    payload = _tar_gz_bytes([])
    assert _upload(auth_client, job.id, sample_node.api_key, payload).status_code == 200

    resp = auth_client.get(f"/api/jobs/{job.id}/results/manifest")

    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []
    assert resp.json()["archive_bytes"] == len(payload)


async def test_results_manifest_of_a_corrupt_archive_returns_500(
    auth_client, job, sample_node
):
    """A file that is not a gzipped tar produces a 500 naming the read failure.

    The realistic cause is a connection dropped mid-upload: the tarball is
    written straight to its final path with no temp-file-and-rename, so
    ``has_results`` says True while ``tarfile.open`` raises. The handler catches
    ``TarError``/``OSError`` and reports it, which is how an operator learns to
    delete the file and re-run the collect step instead of seeing a bare
    traceback.
    """
    assert _upload(auth_client, job.id, sample_node.api_key,
                   b"\x00\x01\x02 definitely not gzip").status_code == 200

    resp = auth_client.get(f"/api/jobs/{job.id}/results/manifest")

    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"].startswith("Could not read archive:")


async def test_results_manifest_of_a_truncated_gzip_raises_eoferror(
    auth_client, job, sample_node
):
    """POSSIBLE BUG documented: a truncated gzip escapes the handler's ``except``.

    The handler catches ``(tarfile.TarError, OSError)``, but a gzip stream cut
    short raises ``EOFError`` — which inherits from ``Exception``, not
    ``OSError``. So the "half-written upload" case the source's own AI Note calls
    out as the realistic corruption mode does *not* produce the friendly
    ``"Could not read archive: ..."`` 500; it propagates out of the route as an
    unhandled exception (a bare 500 with a server-side traceback in production,
    re-raised into the test here by ``TestClient``).

    This test asserts the ACTUAL behaviour so the suite stays green and so
    widening the ``except`` to include ``EOFError`` announces itself as a
    failure here.
    """
    payload = _tar_gz_bytes([("m5out/stats.txt", "q" * 5000)])
    assert _upload(auth_client, job.id, sample_node.api_key,
                   payload[: len(payload) // 2]).status_code == 200

    with pytest.raises(EOFError):
        auth_client.get(f"/api/jobs/{job.id}/results/manifest")


def test_results_manifest_unknown_job_returns_404(auth_client):
    """An unknown job id 404s with "Job not found" before the filesystem is touched.

    The job check precedes the tarball check, so the detail string distinguishes
    "no such job" from "job exists but has no results" — the two require
    different operator responses.
    """
    resp = auth_client.get(f"/api/jobs/{uuid.uuid4()}/results/manifest")

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Job not found"


def test_results_manifest_without_authorization_is_rejected(client, job):
    """The manifest requires a bearer token — 401 "Not authenticated" when absent.

    The manifest leaks the full file listing of a job's output, so it is guarded
    exactly like the download.
    """
    resp = client.get(f"/api/jobs/{job.id}/results/manifest")

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Not authenticated"
