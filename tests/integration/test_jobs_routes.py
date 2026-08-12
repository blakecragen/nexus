"""Integration tests for the job management routes.

SUT: ``packages/server/src/nexus_server/api/routes/jobs.py``.

Covers submission-time validation (unknown step, bad params, valid single-step
job with accumulated upstream context), listing + filters, detail + 404, requeue
(the exact-copy contract and its re-validation), cancel (including the
terminal-state 409), delete (terminal-only) and the plain-text log endpoint.

To keep these tests fast and deterministic we stub the runner's ``submit_job``
on ``app.state.runner`` so submitting a job never spawns the real background
``_run_job`` task (which would try to schedule onto a live agent over a
WebSocket). The route still does all of its own validation + persistence work;
we are only suppressing the fire-and-forget dispatch the route triggers at the
end. The created job therefore sits in its persisted ``pending`` state, which is
exactly the behaviour we assert on.

Division of labour with the other suites
    Execution semantics (jump/loop, on_fail, node selection, remote dispatch)
    belong to ``tests/integration/test_runner_scheduler.py``, which drives the
    runner directly. This file is strictly about the HTTP surface: status
    codes, request/response schemas, validation messages, filters, pagination
    and the terminal-state guards on cancel/delete.

Status-code contract exercised here
    * 201 — job created (validation passed, row persisted, runner notified).
    * 400 — the route's OWN step validation failed (unknown step name or bad
      step params). The message names the offending step index.
    * 422 — FastAPI/Pydantic rejected the request body before the handler ran.
      The 400 vs 422 split is meaningful: 422 means the shape was wrong, 400
      means the shape was fine but the step content was not.
    * 404 — unknown job id.
    * 409 — a state conflict: cancelling an already-terminal job, or deleting
      one that is still running. Note requeue is deliberately exempt: it copies
      a job in any state, because the copy is independent of the original.
"""

from __future__ import annotations

import uuid

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_runner_dispatch(app):
    """Replace the runner's ``submit_job`` with an async no-op.

    Without this the POST /api/jobs handler calls ``runner.submit_job`` which
    creates a real ``asyncio.create_task(self._run_job(...))`` — background work
    that depends on a live agent. We only want to test the route, not the
    distributed runner, so we neutralise the dispatch while leaving every other
    part of the handler (validation + ops.create_job) intact.

    Autouse, so EVERY test in this module is protected from spawning background
    tasks — including the read/cancel/delete tests, which never submit but
    would otherwise be vulnerable to a future refactor that dispatches from
    another handler.

    Args:
        app: The wired FastAPI app whose ``state.runner`` is patched.

    Returns:
        The recording list. Requesting this fixture by name in a test gives
        access to the captured job ids, which doubles as a "was dispatch
        reached?" probe — an empty list after a 4xx proves validation aborted
        before the runner was touched.

    Side effects:
        Permanently replaces ``app.state.runner.submit_job`` for the lifetime
        of the (function-scoped) ``app`` fixture. No restore is needed because
        a fresh app is built per test.
    """
    calls: list = []

    async def _noop_submit(db, job_id):
        """Record the dispatch instead of spawning ``_run_job``."""
        calls.append(job_id)

    app.state.runner.submit_job = _noop_submit
    return calls


async def _create_job(db, *, name="seeded-job", status="pending", steps=None,
                      submitted_by=None, pool_id=None, node_id=None,
                      priority=1, storage_target=None):
    """Persist a job directly via ops (bypassing the route) for read/cancel/delete tests.

    Going around the POST handler keeps read-path tests independent of
    submission validation, and is the only way to seed a job in a non-``pending``
    status (the route always creates ``pending``).

    Args:
        db: Test session.
        name: Job name, asserted on by listing tests.
        status: Desired starting status. Anything other than ``pending``
            requires a follow-up ``update_job`` because ``create_job`` hard-codes
            the initial state.
        steps: ``steps_config`` list; defaults to a single ``run_command``.
        submitted_by: Owner user id (pass ``regular_user.id`` so the job is
            visible to the ``auth_client`` identity).
        pool_id: Optional ``target_pool_id`` for pool-filter tests.
        node_id: Optional ``target_node_id``. Used by the requeue tests, which
            assert the pin is carried onto the copy.
        priority: Job priority; the requeue tests assert it is preserved.
        storage_target: Optional storage backend name, likewise carried over by
            requeue.

    Returns:
        The persisted ``Job``, re-fetched after a status change so the returned
        object reflects what is actually in the database.
    """
    from nexus_server.db import ops

    job = await ops.create_job(
        db,
        name=name,
        submitted_by=submitted_by,
        steps_config=steps or [{"step": "run_command", "params": {"command": "echo hi"}}],
        target_pool_id=pool_id,
        target_node_id=node_id,
        priority=priority,
        storage_target=storage_target,
    )
    if status != "pending":
        await ops.update_job(db, job.id, status=status)
        job = await ops.get_job_by_id(db, job.id)
    return job


# ── POST /api/jobs — submission + validation ───────────────────────────────


def test_submit_valid_single_step_job_returns_201(auth_client, _stub_runner_dispatch):
    """A valid run_command job is created, returns 201 + a job id, and sits pending.

    The last two assertions are the important ones: the route must hand the
    newly-created job's own id to the runner exactly once. Dispatching zero
    times would leave the job pending forever; dispatching twice would run it
    concurrently with itself.
    """
    body = {
        "name": "hello-job",
        "steps": [{"step": "run_command", "params": {"command": "echo hello"}}],
    }
    resp = auth_client.post("/api/jobs", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "hello-job"
    assert data["status"] == "pending"
    assert data["current_step"] == 0
    # Returned id must be a real UUID.
    uuid.UUID(data["id"])
    # The route handed the job to the (stubbed) runner exactly once.
    assert len(_stub_runner_dispatch) == 1
    assert str(_stub_runner_dispatch[0]) == data["id"]


def test_submit_unknown_step_name_returns_400(auth_client, _stub_runner_dispatch):
    """Referencing a step that isn't in STEP_REGISTRY is a 400 with the name echoed.

    The bad step is at index 0, so the detail must say so and must list the
    available steps (so the caller can correct the typo). No job should be
    dispatched when validation fails.

    Fail-at-submit rather than fail-at-run: without this check a typo'd step
    name would be accepted, queued, scheduled onto a node and only then fail —
    minutes later and with a far less actionable message.
    """
    body = {
        "name": "bad-step-job",
        "steps": [{"step": "definitely_not_a_real_step", "params": {}}],
    }
    resp = auth_client.post("/api/jobs", json=body)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "Step 0" in detail
    assert "unknown step" in detail
    assert "definitely_not_a_real_step" in detail
    assert "Available:" in detail
    # A failed validation must not reach the runner dispatch.
    assert _stub_runner_dispatch == []


def test_submit_unknown_step_reports_correct_index(auth_client):
    """When a later step is unknown the reported index points at that step.

    Guards against an off-by-one (or a hard-coded 0) in the validation loop's
    index reporting — with a long pipeline, pointing at the wrong step sends
    the author debugging perfectly good config. Step 0 is deliberately valid so
    only step 1 can be reported.
    """
    body = {
        "name": "bad-second-step",
        "steps": [
            {"step": "run_command", "params": {"command": "echo ok"}},
            {"step": "nope_not_real", "params": {}},
        ],
    }
    resp = auth_client.post("/api/jobs", json=body)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "Step 1" in detail
    assert "nope_not_real" in detail


def test_submit_invalid_params_returns_400(auth_client, _stub_runner_dispatch):
    """run_command requires a 'command' field; omitting it fails param validation.

    The detail echoes the failing step index + name so the caller knows which
    step is broken, and the runner must not be invoked.

    Distinct from the unknown-step case: here the step name resolves fine and
    the failure comes from the step class's own params schema. Both surface as
    400 (content problem) rather than 422 (shape problem).
    """
    body = {
        "name": "missing-command-job",
        "steps": [{"step": "run_command", "params": {}}],
    }
    resp = auth_client.post("/api/jobs", json=body)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "Step 0" in detail
    assert "run_command" in detail
    assert "validation failed" in detail
    assert _stub_runner_dispatch == []


def test_submit_multi_step_accumulates_upstream_outputs(auth_client, _stub_runner_dispatch):
    """A downstream step is validated against upstream OUTPUT_KEYS + params.

    run_command declares OUTPUT_KEYS (exit_code, stdout_path, stderr_path). Two
    run_command steps must both validate and produce a single created job.

    Regression guard for a real bug: submit-time validation used to check each
    step in isolation, so a step referencing an upstream step's output was
    rejected as missing a required input. The validator now threads accumulated
    ``OUTPUT_KEYS`` forward, which is what lets chained pipelines submit at all.
    """
    body = {
        "name": "chained-job",
        "steps": [
            {"step": "run_command", "params": {"command": "echo first"}},
            {"step": "run_command", "params": {"command": "echo second"}},
        ],
    }
    resp = auth_client.post("/api/jobs", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending"


def test_submit_empty_steps_creates_job(auth_client):
    """Edge case: no steps means the validation loop is a no-op and the job is created.

    Documents current behaviour rather than endorsing it — an empty job is
    accepted and will immediately complete. If a future change rejects empty
    pipelines, this test is the one to update deliberately.
    """
    body = {"name": "empty-job", "steps": []}
    resp = auth_client.post("/api/jobs", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "empty-job"


def test_submit_requires_authentication(client):
    """Unauthenticated submit is rejected (401).

    Job submission executes arbitrary commands on cluster nodes, so this is the
    single most security-relevant gate in the file.
    """
    body = {"name": "x", "steps": [{"step": "run_command", "params": {"command": "echo"}}]}
    resp = client.post("/api/jobs", json=body)
    assert resp.status_code == 401


def test_submit_malformed_body_returns_422(auth_client):
    """A body missing the required 'name' field fails Pydantic request validation."""
    resp = auth_client.post("/api/jobs", json={"steps": []})
    assert resp.status_code == 422


def test_submit_malformed_step_entry_returns_422(auth_client):
    """A step entry missing the required 'step' key fails request-model validation
    (422) before the route's own step-name validation runs.

    Pins the ordering of the two validation layers: schema first (422), then
    step-content checks (400). If the layers swapped, this would return 400.
    """
    resp = auth_client.post(
        "/api/jobs",
        json={"name": "x", "steps": [{"params": {"command": "echo"}}]},
    )
    assert resp.status_code == 422


def test_submit_invalid_pool_id_returns_422(auth_client):
    """A non-UUID target_pool_id fails Pydantic coercion (422).

    Rejecting at the schema boundary keeps a malformed id from reaching the DB
    layer, where a bad bind would surface as an opaque 500.
    """
    resp = auth_client.post(
        "/api/jobs",
        json={
            "name": "x",
            "steps": [],
            "target_pool_id": "not-a-uuid",
        },
    )
    assert resp.status_code == 422


# ── GET /api/jobs — list + filters ─────────────────────────────────────────


async def test_list_jobs_returns_created_jobs(auth_client, db, regular_user):
    """Listing returns previously-created jobs as JobInfo objects."""
    await _create_job(db, name="list-me", submitted_by=regular_user.id)
    resp = auth_client.get("/api/jobs")
    assert resp.status_code == 200, resp.text
    names = [j["name"] for j in resp.json()]
    assert "list-me" in names


async def test_list_jobs_status_filter(auth_client, db, regular_user):
    """The job_status query param filters by job status."""
    await _create_job(db, name="pending-one", status="pending", submitted_by=regular_user.id)
    await _create_job(db, name="done-one", status="completed", submitted_by=regular_user.id)

    resp = auth_client.get("/api/jobs", params={"job_status": "completed"})
    assert resp.status_code == 200, resp.text
    names = [j["name"] for j in resp.json()]
    assert "done-one" in names
    assert "pending-one" not in names


async def test_list_jobs_pool_filter(auth_client, db, regular_user, sample_pool):
    """The pool_id query param filters jobs by target pool.

    Exact-list equality (not membership) is used so a filter that silently
    matched everything would fail — the un-pooled job must be excluded.
    """
    await _create_job(db, name="in-pool", submitted_by=regular_user.id, pool_id=sample_pool.id)
    await _create_job(db, name="no-pool", submitted_by=regular_user.id)

    resp = auth_client.get("/api/jobs", params={"pool_id": str(sample_pool.id)})
    assert resp.status_code == 200, resp.text
    names = [j["name"] for j in resp.json()]
    assert names == ["in-pool"]


async def test_list_jobs_limit(auth_client, db, regular_user):
    """The limit query param caps the number of returned jobs."""
    for i in range(3):
        await _create_job(db, name=f"j{i}", submitted_by=regular_user.id)
    resp = auth_client.get("/api/jobs", params={"limit": 2})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2


async def test_list_jobs_newest_first(auth_client, db, regular_user):
    """ops.list_jobs orders by created_at DESC, so the most-recently created job
    comes first in the response."""
    await _create_job(db, name="older", submitted_by=regular_user.id)
    await _create_job(db, name="newer", submitted_by=regular_user.id)
    resp = auth_client.get("/api/jobs")
    assert resp.status_code == 200, resp.text
    names = [j["name"] for j in resp.json()]
    # Both present, and 'newer' precedes 'older'.
    assert names.index("newer") < names.index("older")


async def test_list_jobs_offset_paginates(auth_client, db, regular_user):
    """offset skips the first N (newest) rows. With offset=1+limit=1 the second
    newest job is returned."""
    await _create_job(db, name="o-first", submitted_by=regular_user.id)
    await _create_job(db, name="o-second", submitted_by=regular_user.id)
    await _create_job(db, name="o-third", submitted_by=regular_user.id)
    resp = auth_client.get("/api/jobs", params={"limit": 1, "offset": 1})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    # Newest is o-third (offset 0); offset 1 is o-second.
    assert data[0]["name"] == "o-second"


def test_list_jobs_requires_authentication(client):
    """Unauthenticated list is rejected (401)."""
    resp = client.get("/api/jobs")
    assert resp.status_code == 401


# ── GET /api/jobs/{id} — detail ────────────────────────────────────────────


async def test_get_job_detail(auth_client, db, regular_user):
    """JobDetail bundles the job, its (empty) step runs and the has_* flags.

    Pins the "nothing has happened yet" shape the UI renders immediately after
    submission: no step runs, no log, no results, empty context. The ``has_*``
    booleans exist so the client can hide the log/results tabs without issuing
    extra requests that would 404.
    """
    job = await _create_job(db, name="detail-job", submitted_by=regular_user.id)
    resp = auth_client.get(f"/api/jobs/{job.id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["job"]["id"] == str(job.id)
    assert data["job"]["name"] == "detail-job"
    assert data["job"]["status"] == "pending"
    assert data["job"]["current_step"] == 0
    assert data["steps"] == []
    assert data["has_log"] is False
    assert data["has_results"] is False
    assert data["context_data"] == {}


def test_get_job_detail_unknown_404(auth_client):
    """An unknown job id yields a 404."""
    resp = auth_client.get(f"/api/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Job not found"


async def test_get_job_detail_has_log_flag(auth_client, db, regular_user):
    """has_log reflects the presence of accumulated terminal log text.

    Complements the detail test above, which asserts the False case: the flag
    must actually track ``log_text`` rather than being hard-coded either way.
    """
    from nexus_server.db import ops

    job = await _create_job(db, name="logged-job", submitted_by=regular_user.id)
    await ops.append_job_log(db, job.id, "some output\n")
    resp = auth_client.get(f"/api/jobs/{job.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_log"] is True


# ── GET /api/jobs/{id}/log — plain text ────────────────────────────────────


async def test_get_job_log_returns_text(auth_client, db, regular_user):
    """The log endpoint returns the stored log_text as plain text.

    ``text/plain`` (not JSON) is deliberate — the log is streamed straight into
    a terminal view and piped to files by CLI users, so a JSON-wrapped or
    escaped body would break both.
    """
    from nexus_server.db import ops

    job = await _create_job(db, name="log-job", submitted_by=regular_user.id)
    await ops.append_job_log(db, job.id, "line one\nline two\n")
    resp = auth_client.get(f"/api/jobs/{job.id}/log")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert "line one" in resp.text
    assert "line two" in resp.text


async def test_get_job_log_empty_placeholder(auth_client, db, regular_user):
    """When no output has been captured the endpoint returns a placeholder.

    A 200 with an explanatory line beats a 404 here: the log view is opened
    while a job is still starting, and an error would look like a failure
    rather than "no output yet".
    """
    job = await _create_job(db, name="no-log-job", submitted_by=regular_user.id)
    resp = auth_client.get(f"/api/jobs/{job.id}/log")
    assert resp.status_code == 200, resp.text
    assert "No terminal output captured yet" in resp.text


async def test_get_job_log_download_disposition(auth_client, db, regular_user):
    """?download=1 sets a Content-Disposition attachment header.

    Without the attachment disposition the browser renders the log inline; the
    UI's "download log" button relies on this header (and the ``job_<id>.txt``
    filename) to save a file instead.
    """
    from nexus_server.db import ops

    job = await _create_job(db, name="dl-log-job", submitted_by=regular_user.id)
    await ops.append_job_log(db, job.id, "downloadable\n")
    resp = auth_client.get(f"/api/jobs/{job.id}/log", params={"download": "1"})
    assert resp.status_code == 200, resp.text
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert f"job_{job.id}.txt" in cd


def test_get_job_log_unknown_404(auth_client):
    """Log for an unknown job is a 404."""
    resp = auth_client.get(f"/api/jobs/{uuid.uuid4()}/log")
    assert resp.status_code == 404


# ── POST /api/jobs/{id}/cancel ─────────────────────────────────────────────


async def test_cancel_pending_job(auth_client, db, regular_user):
    """Cancelling a pending job transitions it to cancelled, stamps completed_at,
    and the change is persisted (visible on a subsequent fetch).

    The re-fetch is the point: the response body alone could be built from an
    in-memory object that was never committed. A second HTTP request uses a
    fresh session, so seeing ``cancelled`` there proves the write landed.
    """
    job = await _create_job(db, name="cancel-me", status="pending", submitted_by=regular_user.id)
    resp = auth_client.post(f"/api/jobs/{job.id}/cancel")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "cancelled"
    # runner.cancel_job stamps completed_at on cancellation.
    assert data["completed_at"] is not None
    # Persisted: a fresh detail fetch also reports cancelled.
    again = auth_client.get(f"/api/jobs/{job.id}")
    assert again.json()["job"]["status"] == "cancelled"


async def test_cancel_terminal_job_returns_409(auth_client, db, regular_user):
    """Cancelling an already-completed job is a 409 conflict.

    Terminal states are final. Letting a completed job be flipped to
    ``cancelled`` would rewrite history and confuse anyone reading the job's
    outcome or its already-uploaded artifacts.
    """
    job = await _create_job(db, name="done", status="completed", submitted_by=regular_user.id)
    resp = auth_client.post(f"/api/jobs/{job.id}/cancel")
    assert resp.status_code == 409, resp.text
    assert "terminal state" in resp.json()["detail"]


def test_cancel_unknown_404(auth_client):
    """Cancelling an unknown job is a 404."""
    resp = auth_client.post(f"/api/jobs/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404


# ── DELETE /api/jobs/{id} ──────────────────────────────────────────────────


async def test_delete_terminal_job(auth_client, db, regular_user):
    """A terminal-state job can be deleted (204) and is then gone."""
    job = await _create_job(db, name="del-me", status="completed", submitted_by=regular_user.id)
    resp = auth_client.delete(f"/api/jobs/{job.id}")
    assert resp.status_code == 204, resp.text
    # Subsequent fetch is a 404.
    assert auth_client.get(f"/api/jobs/{job.id}").status_code == 404


async def test_delete_non_terminal_job_returns_409(auth_client, db, regular_user):
    """A pending (non-terminal) job cannot be deleted — 409.

    Note this is the mirror image of cancel: delete requires a terminal state,
    cancel requires a NON-terminal one. Deleting a running job would orphan the
    background ``_run_job`` task, which would keep executing steps and then try
    to write results against a row that no longer exists.
    """
    job = await _create_job(db, name="still-running", status="running", submitted_by=regular_user.id)
    resp = auth_client.delete(f"/api/jobs/{job.id}")
    assert resp.status_code == 409, resp.text
    assert "terminal state" in resp.json()["detail"]


def test_delete_unknown_404(auth_client):
    """Deleting an unknown job is a 404."""
    resp = auth_client.delete(f"/api/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── POST /api/jobs/{id}/requeue ────────────────────────────────────────────
#
# The endpoint takes no body: it is an exact copy by design. These tests split
# into "what carries over" (plan + targeting), "what deliberately does not"
# (run state, and the submitter attribution) and "when it refuses".


async def test_requeue_creates_new_job_with_same_plan(auth_client, db, regular_user):
    """Requeue returns 201 with a NEW id whose plan matches the original's.

    The distinct id is the load-bearing assertion — an endpoint that returned
    the original job would look successful in the UI while nothing was queued.
    """
    steps = [
        {"step": "run_command", "params": {"command": "echo one"}, "on_fail": "continue",
         "target_node_id": None, "target_pool_id": None, "target_os": None},
    ]
    job = await _create_job(db, name="rerun-me", status="failed",
                            steps=steps, submitted_by=regular_user.id)

    resp = auth_client.post(f"/api/jobs/{job.id}/requeue")
    assert resp.status_code == 201, resp.text
    new = resp.json()
    assert new["id"] != str(job.id)
    assert new["name"] == "rerun-me"

    # The copy's stored plan is fetched back through the detail endpoint rather
    # than read off the ORM, so this also covers the JobDetail.steps_config wiring.
    detail = auth_client.get(f"/api/jobs/{new['id']}").json()
    assert detail["steps_config"] == steps


async def test_requeue_preserves_targeting_and_priority(
    auth_client, db, regular_user, sample_pool,
):
    """Pool/node pin, priority and storage target all carry onto the copy.

    This is the "same params" contract: a job pinned to a pool must not quietly
    become schedulable anywhere just because it was re-run.
    """
    job = await _create_job(
        db, name="pinned", status="completed", submitted_by=regular_user.id,
        pool_id=sample_pool.id, priority=7, storage_target="minio-main",
    )

    resp = auth_client.post(f"/api/jobs/{job.id}/requeue")
    assert resp.status_code == 201, resp.text
    new_id = resp.json()["id"]

    from nexus_server.db import ops
    new_job = await ops.get_job_by_id(db, new_id)
    assert str(new_job.target_pool_id) == str(sample_pool.id)
    assert new_job.priority == 7
    assert new_job.storage_target == "minio-main"


async def test_requeue_starts_clean_and_leaves_original_untouched(
    auth_client, db, regular_user,
):
    """Run state belongs to the old attempt: the copy starts pending and empty.

    Also pins the non-destructive half of the contract — requeue must never
    mutate the job it copied, or the Jobs table would appear to lose history.
    """
    from nexus_server.db import ops

    job = await _create_job(db, name="dirty", status="failed", submitted_by=regular_user.id)
    await ops.append_job_log(db, job.id, "old output\n")
    await ops.update_job(db, job.id, current_step=3, error="boom",
                         context_data={"m5out_path": "/tmp/old"})

    resp = auth_client.post(f"/api/jobs/{job.id}/requeue")
    assert resp.status_code == 201, resp.text
    new = resp.json()
    assert new["status"] == "pending"
    assert new["current_step"] == 0
    assert new["error"] is None

    new_detail = auth_client.get(f"/api/jobs/{new['id']}").json()
    assert new_detail["context_data"] == {}
    assert new_detail["has_log"] is False

    # The original still carries its own failure state.
    old_detail = auth_client.get(f"/api/jobs/{job.id}").json()
    assert old_detail["job"]["status"] == "failed"
    assert old_detail["job"]["error"] == "boom"
    assert old_detail["has_log"] is True


async def test_requeue_attributes_the_copy_to_the_caller(
    auth_client, db, admin_user, regular_user,
):
    """The re-runner owns the new job, not the original submitter.

    ``submitted_by`` answers "who caused this to run", so copying the original
    submitter would misattribute a run the caller triggered.
    """
    job = await _create_job(db, name="someone-elses", status="completed",
                            submitted_by=admin_user.id)
    resp = auth_client.post(f"/api/jobs/{job.id}/requeue")
    assert resp.status_code == 201, resp.text
    assert resp.json()["submitted_by"] == str(regular_user.id)


async def test_requeue_dispatches_the_new_job(
    auth_client, db, regular_user, _stub_runner_dispatch,
):
    """The runner is handed the NEW id — otherwise the copy would sit pending forever."""
    job = await _create_job(db, name="dispatch-me", status="completed",
                            submitted_by=regular_user.id)
    resp = auth_client.post(f"/api/jobs/{job.id}/requeue")
    assert resp.status_code == 201, resp.text
    assert [str(c) for c in _stub_runner_dispatch] == [resp.json()["id"]]


async def test_requeue_running_job_is_allowed(auth_client, db, regular_user):
    """Requeue deliberately does NOT 409 on state, unlike cancel and delete.

    The copy is an independent job sharing only a plan, so re-running something
    still in flight is well-defined. The dashboard hides the button for active
    jobs; that is a UX choice and not enforced here.
    """
    job = await _create_job(db, name="still-going", status="running",
                            submitted_by=regular_user.id)
    resp = auth_client.post(f"/api/jobs/{job.id}/requeue")
    assert resp.status_code == 201, resp.text


async def test_requeue_unknown_step_returns_400(
    auth_client, db, regular_user, _stub_runner_dispatch,
):
    """A plan naming a step this build no longer registers is rejected, not queued.

    This is the whole reason requeue re-validates instead of trusting the stored
    plan: the step registry belongs to the current server build, so a plan that
    validated at submission time can go stale under it.
    """
    job = await _create_job(
        db, name="stale-plan", status="failed", submitted_by=regular_user.id,
        steps=[{"step": "step_that_was_deleted", "params": {}}],
    )
    resp = auth_client.post(f"/api/jobs/{job.id}/requeue")
    assert resp.status_code == 400, resp.text
    assert "unknown step" in resp.json()["detail"]
    # Nothing was dispatched — validation aborted before the runner was reached.
    assert _stub_runner_dispatch == []


async def test_requeue_unreadable_plan_returns_400(auth_client, db, regular_user):
    """A stored plan that no longer parses as StepConfig is a 400, not a 500.

    Contrast ``test_get_job_detail_tolerates_unreadable_plan``: the read path
    degrades so the job stays inspectable, while this path — where the plan is
    the entire payload — refuses loudly.
    """
    job = await _create_job(
        db, name="corrupt-plan", status="failed", submitted_by=regular_user.id,
        steps=[{"params": {"command": "echo hi"}}],  # no "step" key
    )
    resp = auth_client.post(f"/api/jobs/{job.id}/requeue")
    assert resp.status_code == 400, resp.text
    assert "could not be read" in resp.json()["detail"]


def test_requeue_unknown_404(auth_client):
    """Requeueing an unknown job is a 404."""
    resp = auth_client.post(f"/api/jobs/{uuid.uuid4()}/requeue")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Job not found"


def test_requeue_requires_authentication(client):
    """Requeue is behind the user JWT like the rest of the dashboard surface."""
    resp = client.post(f"/api/jobs/{uuid.uuid4()}/requeue")
    assert resp.status_code in (401, 403)


# ── GET /api/jobs/{id} — steps_config on the detail payload ────────────────


async def test_get_job_detail_returns_steps_config(auth_client, db, regular_user):
    """The detail payload carries the submitted plan, which "Duplicate" pre-fills from.

    ``steps`` (execution records) and ``steps_config`` (the plan) are separate:
    this job has never run, so the plan is present while ``steps`` is empty.
    """
    steps = [
        {"step": "run_command", "params": {"command": "echo hi"}, "on_fail": "stop",
         "target_node_id": None, "target_pool_id": None, "target_os": None},
    ]
    job = await _create_job(db, name="has-plan", steps=steps, submitted_by=regular_user.id)
    data = auth_client.get(f"/api/jobs/{job.id}").json()
    assert data["steps_config"] == steps
    assert data["steps"] == []


async def test_get_job_detail_tolerates_unreadable_plan(auth_client, db, regular_user):
    """A plan that will not parse degrades to [] rather than breaking the detail page.

    The detail endpoint also serves the log and results tabs, which is exactly
    what you open when investigating a job whose plan went bad — a 500 here
    would hide the evidence.
    """
    job = await _create_job(
        db, name="bad-plan", submitted_by=regular_user.id,
        steps=[{"params": {"command": "echo hi"}}],  # no "step" key
    )
    resp = auth_client.get(f"/api/jobs/{job.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["steps_config"] == []


# ── GET /api/jobs/{id}/results — download + manifest (no-results path) ───────


async def test_download_results_no_results_returns_404(auth_client, db, regular_user):
    """A job with no uploaded results tarball yields a 404 on download.

    Distinguished from the unknown-job case below by the ``detail`` string —
    the client shows a different message for "this job produced nothing" versus
    "no such job", so the two 404s must stay distinguishable.
    """
    job = await _create_job(db, name="no-results", submitted_by=regular_user.id)
    resp = auth_client.get(f"/api/jobs/{job.id}/results/download")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "No results for this job"


def test_download_results_unknown_job_404(auth_client):
    """Downloading results for an unknown job is a 404 (job-not-found).

    The job existence check runs before the results check, which is why the
    detail here is "Job not found" rather than "No results for this job".
    """
    resp = auth_client.get(f"/api/jobs/{uuid.uuid4()}/results/download")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Job not found"


async def test_results_manifest_no_results_returns_404(auth_client, db, regular_user):
    """The manifest endpoint 404s when there is no tarball to inspect.

    The manifest (file tree of the results archive) is derived from the same
    stored tarball as the download, so both endpoints must agree that nothing
    is available — a manifest that returned an empty tree would render an
    empty, apparently-valid results view.
    """
    job = await _create_job(db, name="no-manifest", submitted_by=regular_user.id)
    resp = auth_client.get(f"/api/jobs/{job.id}/results/manifest")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "No results for this job"
