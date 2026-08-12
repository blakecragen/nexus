"""Job management routes — list, submit, detail, requeue, cancel, delete, results.

Mounted at ``/api/jobs`` by ``nexus_server.main.create_app()``. This is the
busiest router in the server and the seam between three very different callers:

* **The dashboard** (``frontend/src/pages/Jobs.tsx``, ``JobBuilder.tsx``,
  ``JobDetail.tsx`` via ``frontend/src/api/client.ts``) — authenticates with a
  user JWT and drives list / submit / detail / requeue / cancel / delete plus
  the log and results views.
* **The job runner** (``nexus_server.runner.JobRunner``, injected as the
  ``Runner`` dependency) — this module never executes a step itself. It persists
  the job row and hands the id to the runner, which schedules steps onto agents
  over the WebSocket and writes progress back into the same tables.
* **Agents** (``nexus_agent``) — one endpoint here, ``PUT /{job_id}/results``,
  is called by the agent rather than a browser and therefore authenticates with
  a node API key instead of a JWT.

Two separate output channels exist per job and are easy to confuse:

``log_text``
    A single aggregated text column on the ``jobs`` row, appended to as steps
    report stdout/stderr through the WebSocket handler. Served as plain text by
    ``GET /{job_id}/log``.
``results.tar.gz``
    An opaque tarball a step chose to collect (today: ``nexus_steps.gem5``'s
    ``collect_results``) and uploaded straight to the server's local disk under
    ``RESULTS_DIR``. Served by the ``/{job_id}/results*`` endpoints. This
    deliberately bypasses the pluggable storage-backend machinery and the
    ``artifacts`` table used by ``routes/artifacts.py``.

Neighbours: ``nexus_common.steps.registry.STEP_REGISTRY`` and
``nexus_common.steps.base`` (submission-time validation),
``nexus_server.db.ops`` (persistence), ``nexus_server.api.deps`` (DI).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse

from nexus_common.models.schemas import (
    JobDetail, JobInfo, JobSubmit, StepConfig, StepRunInfo,
)
from nexus_common.steps.registry import STEP_REGISTRY
from nexus_server.api.deps import CurrentUser, DbSession, Runner
from nexus_server.db import ops

router = APIRouter()

# Per-job result artifacts uploaded by agents (no external storage backend needed).
#
# AI Note: this is a *relative* path, so results land under whatever directory
# the server process was started in. Restarting the server from a different cwd
# (or running it in a container without a volume mounted here) makes previously
# uploaded tarballs invisible — has_results flips to False and the download
# endpoint 404s even though the job row is untouched. Keep it aligned with the
# working directory used in docker-compose.yml / dev.sh.
RESULTS_DIR = Path(".nexus-results")


def _job_results_path(job_id) -> Path:
    """Return the on-disk location of a job's uploaded results tarball.

    Single source of truth for the layout ``.nexus-results/<job_id>/results.tar.gz``
    — upload, existence check, download and manifest all route through here so
    the four stay in sync.

    Args:
        job_id: Job identifier. Accepts a ``UUID`` or a string; it is
            ``str()``-ified either way, and ``UUID`` stringification is the
            canonical lowercase hyphenated form, so both spellings resolve to the
            same directory.

    Returns:
        Path: The tarball path. Nothing is created or verified here; callers do
        their own ``mkdir``/``is_file`` checks.
    """
    return RESULTS_DIR / str(job_id) / "results.tar.gz"


def _job_to_info(job) -> JobInfo:
    """Project a ``Job`` ORM row onto the public ``JobInfo`` schema.

    Field-by-field on purpose: it keeps internal columns (``steps_config``,
    ``context_data``, ``log_text``, ``storage_target``) out of list responses,
    which matters because ``list_jobs`` can return 50 rows at a time and
    ``steps_config`` is unbounded JSON.

    Args:
        job: A ``nexus_server.db.models.Job`` row (untyped to keep the ORM out of
            the route layer's signatures).

    Returns:
        JobInfo: Summary view. The timestamps are declared as ``UTCDateTime`` on
        the schema, which normalises the naive values SQLite hands back into
        tz-aware UTC — without that the frontend renders negative relative times.
    """
    return JobInfo(
        id=job.id, name=job.name, submitted_by=job.submitted_by,
        target_pool_id=job.target_pool_id, target_node_id=job.target_node_id,
        priority=job.priority, status=job.status, current_step=job.current_step,
        error=job.error, created_at=job.created_at,
        started_at=job.started_at, completed_at=job.completed_at,
    )


def _step_run_to_info(sr) -> StepRunInfo:
    """Project a ``StepRun`` ORM row onto the public ``StepRunInfo`` schema.

    Args:
        sr: A ``nexus_server.db.models.StepRun`` row.

    Returns:
        StepRunInfo: Per-attempt view of one step.

    Notes:
        ``sr.state`` — the agent's opaque in-flight bookkeeping for long-running
        steps — is intentionally *not* copied. Only the declared
        ``input_params``/``output_params`` are public.

        A single ``step_index`` can appear more than once in a job's step-run
        list: the runner creates a fresh row per attempt so that loop constructs
        produce one row per iteration. Consumers must not assume
        ``step_index`` is unique.
    """
    return StepRunInfo(
        id=sr.id, job_id=sr.job_id, step_index=sr.step_index,
        step_name=sr.step_name, status=sr.status, node_id=sr.node_id,
        input_params=sr.input_params, output_params=sr.output_params,
        error=sr.error, started_at=sr.started_at, finished_at=sr.finished_at,
    )


def _parse_steps_config(raw) -> list[StepConfig]:
    """Parse a ``Job.steps_config`` JSON column into typed step configs, leniently.

    Args:
        raw: The column value — normally a list of dicts produced by
            ``StepConfig.model_dump()``. ``None`` and non-list values are
            tolerated.

    Returns:
        list[StepConfig]: The parsed plan, or ``[]`` if it does not parse.

    AI Note: swallowing the parse error is deliberate and is the *read* path's
    policy only — see the call site in ``get_job``. Callers that act on the plan
    (``requeue_job``) must parse it themselves and report the failure, not reuse
    this.
    """
    if not isinstance(raw, list):
        return []
    try:
        return [StepConfig(**s) for s in raw]
    except (TypeError, ValueError):
        # ValueError covers pydantic's ValidationError; TypeError covers a
        # non-mapping entry (e.g. a bare string) reaching the ** unpack.
        return []


def _validate_steps(steps: list[StepConfig]) -> None:
    """Validate a whole job plan the way submission does, or raise 400.

    Shared by ``submit_job`` (validating what the caller just sent) and
    ``requeue_job`` (re-validating a plan that was stored possibly months ago).
    Fail-fast and side-effect free, so a rejected plan leaves no database row
    behind in either caller.

    Each step is checked twice over: that its ``step`` names something in
    ``STEP_REGISTRY``, and that its ``params`` satisfy the step class's own
    ``validate_params`` against a context accumulated from everything upstream.
    That context is what lets a step reference an output an earlier step has not
    produced yet (``m5out_path`` and friends) without being rejected.

    Args:
        steps: The ordered plan. Index into this list is what the error messages
            report, so callers must pass it in submission order.

    Returns:
        None. The only signal is the exception.

    Raises:
        HTTPException: 400 naming the offending step index and the reason —
            either an unknown step name (with the available names listed) or the
            joined validation errors.

    AI Note: this mirrors, at submission time, the context the runner will build
    at execution time. If a step type's ``OUTPUT_KEYS`` and the keys it actually
    returns ever diverge, a job will validate here and then fail at runtime with
    a missing-parameter error — that pairing is load-bearing.
    """
    # AI Note: imported lazily inside the function rather than at module scope.
    # Importing nexus_common.steps.base at import time would pull the step
    # machinery into every module that touches the routes package; keeping it
    # local also keeps the STEP_REGISTRY import above as the only hard
    # dependency on the step system.
    from nexus_common.steps.base import StepContext

    val_ctx = StepContext()
    for i, step_cfg in enumerate(steps):
        if step_cfg.step not in STEP_REGISTRY:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Step {i} references unknown step '{step_cfg.step}'. "
                       f"Available: {sorted(STEP_REGISTRY.keys())}",
            )

        # Run parameter validation on each step against the accumulated context.
        step_cls = STEP_REGISTRY[step_cfg.step]
        errors = step_cls.validate_params(step_cfg.params, val_ctx)
        if errors:
            detail = "; ".join(str(e) for e in errors)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Step {i} ('{step_cfg.step}') validation failed: {detail}",
            )

        # This step's outputs become available to later steps.
        #
        # AI Note: values are seeded as None because only *presence* of the key
        # matters to the "context-satisfiable" input rules — the real value is
        # unknown until the step runs. Pass-3 type validation in
        # validate_params() resolves the context, so a None placeholder must
        # never be the only source for a field a downstream step type-checks.
        for k in getattr(step_cls, "OUTPUT_KEYS", []):
            val_ctx.outputs[k] = None
        # AI Note: explicit params are merged in after the None placeholders so a
        # concretely-supplied value wins over an upstream step's placeholder for
        # the same key. Order here is deliberate; swapping these two statements
        # would blank out real values.
        val_ctx.outputs.update(step_cfg.params)


@router.get("", response_model=list[JobInfo])
async def list_jobs(
    db: DbSession,
    user: CurrentUser,
    job_status: str | None = None,
    pool_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """List jobs with optional filtering and pagination.

    Serves ``GET /api/jobs``. Backs the dashboard's job table and the
    auto-refreshing "recent jobs" widget.

    Args:
        db: Async session, injected.
        user: Authenticated caller, injected.
        job_status: Optional exact-match filter on ``Job.status`` (``pending``,
            ``running``, ``completed``, ``failed``, ``cancelled``). Named
            ``job_status`` rather than ``status`` because the module-level
            ``status`` import from FastAPI would shadow it; the query parameter
            on the wire is therefore literally ``?job_status=``.
        pool_id: Optional filter on ``Job.target_pool_id``. Matches only jobs
            explicitly pinned to that pool at submission — a job that merely
            *ran* on a node in the pool is not returned.
        limit: Page size.
        offset: Page offset.

    Returns:
        list[JobInfo]: Newest first (``created_at DESC``, applied in
        ``ops.list_jobs``). Note the ordering is by creation time, so paging
        while new jobs are being submitted can shift rows across pages.
    """
    # AI Note (authorization): results are not scoped to the caller — every
    # authenticated user sees every job. Same single-tenant assumption as the
    # rest of this router.
    #
    # AI Note: `limit` is passed through unclamped, so a client can request an
    # arbitrarily large page (e.g. ?limit=1000000) and force a full table scan
    # plus serialisation of every job. Add an upper bound before exposing this
    # beyond a trusted network.
    jobs = await ops.list_jobs(
        db, status=job_status, pool_id=pool_id,
        limit=limit, offset=offset,
    )
    return [_job_to_info(j) for j in jobs]


@router.post("", response_model=JobInfo, status_code=status.HTTP_201_CREATED)
async def submit_job(body: JobSubmit, db: DbSession, user: CurrentUser, runner: Runner):
    """Submit a new job with step validation.

    Validates the whole step list up front, persists the job, then hands it to
    the runner. Validation is fail-fast and happens *before* any row is written,
    so a rejected submission leaves no trace in the database.

    Args:
        body: Job name, ordered step configs, optional pool/node pinning,
            priority and storage target override.
        db: Async session, injected.
        user: Authenticated submitter; recorded as ``Job.submitted_by``.
        runner: ``JobRunner`` singleton, injected from ``app.state``.

    Returns:
        JobInfo: The persisted job, with ``status="pending"``. Execution has been
        scheduled but has almost certainly not started — poll
        ``GET /api/jobs/{id}`` or listen on the dashboard WebSocket for progress.

    Raises:
        HTTPException: 400 if any step names an unregistered step type, or if any
            step's params fail ``validate_params`` against the accumulated
            context. The detail names the offending step index and the reason.

    Side effects:
        Inserts and commits a ``jobs`` row, then spawns a background asyncio task
        via ``runner.submit_job``. No ``step_runs`` rows are created here.
    """
    # Validate each step, accumulating a context of what upstream steps will
    # provide (their declared OUTPUT_KEYS + any explicit params) so that
    # context-satisfiable fields downstream (e.g. m5out_path) validate correctly.
    _validate_steps(body.steps)

    # Persist job. Step runs are created lazily by the runner on each
    # iteration so loops produce one row per attempt at the same step_index.
    steps_config = [s.model_dump() for s in body.steps]
    job = await ops.create_job(
        db, name=body.name, submitted_by=user.id,
        steps_config=steps_config,
        target_pool_id=body.target_pool_id, target_node_id=body.target_node_id,
        priority=body.priority, storage_target=body.storage_target,
    )

    # Hand the job to the runner for asynchronous execution.
    #
    # AI Note: submit_job() only creates an asyncio task and returns — it does
    # not await execution, which is what keeps this request fast. The task owns
    # its own DB session, so it must not reuse `db` (this request's session is
    # closed when the response is sent).
    await runner.submit_job(db, job.id)

    return _job_to_info(job)


@router.get("/{job_id}", response_model=JobDetail)
async def get_job(job_id: UUID, db: DbSession, user: CurrentUser):
    """Get job detail including step runs and context data.

    Serves ``GET /api/jobs/{job_id}`` — the payload behind the Job Detail page.
    Polled while a job is running, so it stays to three cheap queries/stats.

    Args:
        job_id: Job to fetch.
        db: Async session, injected.
        user: Authenticated caller, injected.

    Returns:
        JobDetail: The job summary, every step run ordered by ``step_index``
        (with repeats for loop iterations), the submitted plan as
        ``steps_config``, the accumulated ``context_data``, and two booleans the
        UI uses to decide whether to render the log tab and the results
        tree/download button.

    Raises:
        HTTPException: 404 if no such job.
    """
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    step_runs = await ops.get_step_runs_for_job(db, job_id)
    return JobDetail(
        job=_job_to_info(job),
        steps=[_step_run_to_info(sr) for sr in step_runs],
        # The stored plan, handed back so the dashboard's "Duplicate" can
        # pre-fill the Job Builder without a second round trip.
        #
        # AI Note: degrades to [] rather than raising if the stored plan does not
        # parse against the current StepConfig. This endpoint is polled every 3s
        # by the detail page and serves the log/results tabs too — a job with a
        # hand-edited or schema-drifted plan must stay *inspectable*, which is
        # exactly when you need to read its log. The frontend hides Duplicate on
        # an empty list. POST /{id}/requeue makes the opposite trade and reports
        # the parse failure as a 400, because there the plan is the whole point.
        steps_config=_parse_steps_config(job.steps_config),
        context_data=job.context_data or {},
        has_log=bool(job.log_text),
        # AI Note: has_results is derived from the filesystem, not the DB, so it
        # is only as trustworthy as RESULTS_DIR (see the constant's note). It is
        # also a blocking stat() inside an async handler — cheap for a local
        # disk, but do not move RESULTS_DIR onto a network mount without moving
        # this into a thread.
        has_results=_job_results_path(job_id).is_file(),
    )


@router.put("/{job_id}/results")
async def upload_job_results(job_id: UUID, request: Request, db: DbSession, file: UploadFile):
    """Agent uploads a job's result tarball. Authenticated by node api_key
    (header X-Node-Key), since this is called by the agent, not a logged-in user.

    Called from ``nexus_steps.gem5.collect_results``, which tars the job's
    ``m5out`` directory and PUTs it here using the ``server_url`` / ``job_id`` /
    ``node_api_key`` triple carried on the ``StepContext``. Uploading the same
    job twice overwrites the previous tarball — last writer wins.

    Args:
        job_id: Job the results belong to; determines the destination directory.
        request: Raw request, used only to read the ``X-Node-Key`` header. This
            endpoint cannot use the ``CurrentUser`` dependency because agents
            hold no user JWT.
        db: Async session, injected. Used for the node-key lookup and the job
            existence check only; nothing is written to the database here.
        file: Multipart upload field named ``file`` — the gzipped tarball.

    Returns:
        dict: ``{"ok": True, "size_bytes": <bytes written>}``.

    Raises:
        HTTPException: 401 if the ``X-Node-Key`` header is absent or matches no
            registered node; 404 if the job does not exist.

    Side effects:
        Creates ``RESULTS_DIR/<job_id>/`` and writes ``results.tar.gz`` into it.
    """
    # AI Note (security): the node key only proves the caller is *some*
    # registered node — there is no check that this node was ever scheduled to
    # run this job. Any node holding a valid key can therefore overwrite any
    # job's results tarball. Acceptable inside a trusted cluster; tighten by
    # cross-checking node.id against the job's step_runs if that changes.
    node_key = request.headers.get("X-Node-Key", "")
    node = await ops.get_node_by_api_key(db, node_key) if node_key else None
    if not node:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node key")
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    dest = _job_results_path(job_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    # AI Note: streamed in 1 MiB chunks rather than file.read() with no argument
    # so that a multi-gigabyte m5out tarball never has to fit in memory. The
    # write itself is blocking I/O on the event loop; that is tolerated because
    # the destination is local disk.
    #
    # AI Note: the tarball is written straight to its final path with no temp
    # file and no rename, so a connection dropped mid-upload leaves a truncated
    # results.tar.gz behind. has_results/get_job then report results exist while
    # the manifest endpoint fails to open the archive (500). Deleting the file
    # and re-running the collect step is the recovery.
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            size += len(chunk)
    return {"ok": True, "size_bytes": size}


@router.get("/{job_id}/results/download")
async def download_job_results(job_id: UUID, db: DbSession, user: CurrentUser):
    """Download a job's collected results tarball.

    Serves ``GET /api/jobs/{job_id}/results/download``, the target of the
    Download button on the Job Detail page.

    Args:
        job_id: Job whose tarball to stream.
        db: Async session, injected — used only to confirm the job exists so a
            deleted job yields a clear 404 rather than a bare missing file.
        user: Any authenticated user, injected.

    Returns:
        FileResponse: The gzipped tarball, sent as an attachment named
        ``job_<id>_results.tar.gz``.

    Raises:
        HTTPException: 404 if the job is unknown, or if it exists but no
            tarball has been uploaded for it.
    """
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    path = _job_results_path(job_id)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No results for this job")
    return FileResponse(
        path, media_type="application/gzip", filename=f"job_{job_id}_results.tar.gz",
    )


@router.get("/{job_id}/results/manifest")
async def get_job_results_manifest(job_id: UUID, db: DbSession, user: CurrentUser):
    """List the files inside a job's results tarball (for the file-tree view).

    Returns {archive_bytes, entries: [{path, size, is_dir}]}.

    Serves ``GET /api/jobs/{job_id}/results/manifest``, consumed by the
    ``ResultsTree`` component on the Job Detail page. Only metadata is read —
    member *contents* are never extracted, so nothing is written to disk and
    tarball path-traversal ("zip slip") is not a concern here.

    Args:
        job_id: Job whose archive to inspect.
        db: Async session, injected — job existence check only.
        user: Any authenticated user, injected.

    Returns:
        dict: ``archive_bytes`` (compressed size on disk) plus one ``entries``
        item per tar member, each with the member's ``path`` as recorded in the
        archive, its uncompressed ``size`` and an ``is_dir`` flag.

    Raises:
        HTTPException: 404 if the job or its tarball is missing; 500 if the file
            exists but cannot be read as a gzipped tar (truncated upload,
            corrupt gzip stream).
    """
    # AI Note: tarfile is imported inside the handler so the module import stays
    # cheap — the manifest view is rarely hit compared with the rest of this
    # router.
    import tarfile

    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    path = _job_results_path(job_id)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No results for this job")
    try:
        # AI Note: getmembers() walks and buffers the entire member index. For a
        # tarball with a very large number of small files this both blocks the
        # event loop and holds every member in memory; acceptable for m5out-sized
        # archives, but revisit before pointing this at arbitrary user tarballs.
        with tarfile.open(path, "r:gz") as tar:
            entries = [
                {"path": m.name, "size": m.size, "is_dir": m.isdir()}
                for m in tar.getmembers()
            ]
    except (tarfile.TarError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Could not read archive: {exc}")
    return {"archive_bytes": path.stat().st_size, "entries": entries}


@router.get("/{job_id}/log")
async def get_job_log(job_id: UUID, db: DbSession, user: CurrentUser, download: bool = False):
    """Return the aggregated per-job terminal log as plain text.

    With ?download=1 the response is sent as an attachment .txt file.

    The log lives in the ``jobs.log_text`` column and is appended to by the
    WebSocket handler as agents report each step's stdout/stderr — it is not
    read from any file on the node. Responses are ``text/plain``, not JSON, so
    the frontend can render or save the body verbatim.

    Args:
        job_id: Job whose log to return.
        db: Async session, injected.
        user: Any authenticated user, injected.
        download: When truthy, adds a ``Content-Disposition: attachment`` header
            so browsers save rather than display the log.

    Returns:
        PlainTextResponse: The captured log, or a placeholder line when the job
        has produced no output yet. The placeholder means a 200 with a non-empty
        body does *not* imply real output — use ``JobDetail.has_log`` to tell
        the difference.

    Raises:
        HTTPException: 404 if no such job.
    """
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    body = job.log_text or "No terminal output captured yet.\n"
    headers = (
        {"Content-Disposition": f'attachment; filename="job_{job_id}.txt"'}
        if download else None
    )
    return PlainTextResponse(body, headers=headers)


@router.post("/{job_id}/cancel", response_model=JobInfo)
async def cancel_job(job_id: UUID, db: DbSession, user: CurrentUser, runner: Runner):
    """Cancel a pending or running job.

    Serves ``POST /api/jobs/{job_id}/cancel``. Delegates the actual work to
    ``JobRunner.cancel_job``, which cancels the in-process asyncio task (if this
    server instance still owns one) and marks the row ``cancelled`` with a
    ``completed_at`` timestamp.

    Args:
        job_id: Job to cancel.
        db: Async session, injected.
        user: Authenticated caller, injected.
        runner: ``JobRunner`` singleton, injected.

    Returns:
        JobInfo: The job re-read after cancellation, so the caller sees the
        updated status rather than the pre-cancel snapshot.

    Raises:
        HTTPException: 404 if no such job; 409 if it has already reached a
            terminal state (``completed``/``failed``/``cancelled``), which keeps
            cancel idempotent-but-loud instead of silently rewriting a finished
            job's outcome.

    Side effects:
        Cancels a background task and commits a status update.
    """
    # AI Note (authorization): no ownership check — any authenticated user can
    # cancel any job, including one submitted by someone else.
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    # AI Note: terminal-state names are duplicated here and in delete_job below
    # as string literals rather than being read from JobStatus. If a new terminal
    # status is ever added to the enum, both tuples must be updated too.
    if job.status in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is already in terminal state: {job.status}",
        )
    # AI Note: cancellation is best-effort with respect to the agent. The runner
    # cancels its local task and flips the DB row immediately, but a step already
    # dispatched over the WebSocket keeps executing on the node until it finishes
    # on its own — the job is reported cancelled before the remote work stops.
    await runner.cancel_job(db, job_id)
    job = await ops.get_job_by_id(db, job_id)
    return _job_to_info(job)


@router.post("/{job_id}/requeue", response_model=JobInfo, status_code=status.HTTP_201_CREATED)
async def requeue_job(job_id: UUID, db: DbSession, user: CurrentUser, runner: Runner):
    """Re-submit a job's plan verbatim as a brand new job.

    Serves ``POST /api/jobs/{job_id}/requeue`` — the dashboard's "Re-run" button
    on the Jobs table and the Job Detail header. Takes no request body: the
    whole point is an exact copy, so there is nothing to configure.

    Copied from the original: ``name`` (no "(retry N)" suffix — attempts are
    told apart by ``created_at``), ``steps_config``, ``target_pool_id``,
    ``target_node_id``, ``priority`` and ``storage_target``.

    Args:
        job_id: Job whose plan to re-submit. Left completely untouched.
        db: Async session, injected.
        user: Authenticated caller. Recorded as the new job's ``submitted_by``.
        runner: ``JobRunner`` singleton, injected.

    Returns:
        JobInfo: The *new* job, 201, ``status="pending"``. The caller gets a new
        id — this never returns the original.

    Raises:
        HTTPException: 404 if no such job; 400 if the stored plan no longer
            parses or no longer validates (a step type that has since been
            renamed or removed, or whose ``PARAMS_SCHEMA`` tightened).

    Side effects:
        Inserts and commits a new ``jobs`` row, then spawns a background
        asyncio task via ``runner.submit_job``.

    AI Note: nothing but the plan and its targeting carries over. ``context_data``,
    ``log_text``, ``current_step``, ``error`` and the results tarball under
    ``RESULTS_DIR`` all describe the *old* attempt; the new job starts clean and
    re-resolves its own ``${...}`` references from scratch. That is why the plan
    (``steps_config``, which keeps placeholders intact) is the right source and
    ``StepRun.input_params`` (already resolved against the old run's context) is
    not.

    AI Note: unlike ``cancel_job`` and ``delete_job``, this deliberately does NOT
    409 on job state — requeueing a still-``running`` job is well-defined,
    because the result is an independent job that shares nothing but its plan.
    The dashboard only offers the button on terminal jobs; that is a UX choice,
    not a rule this endpoint enforces.

    AI Note (authorization): no ownership check — any authenticated user can
    requeue any job, and the copy is attributed to *them*, not to the original
    submitter. Same single-tenant assumption as the rest of this router.
    """
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Strict parse, unlike the lenient one in get_job: acting on a plan we cannot
    # read would queue a job that is certain to fail at dispatch time.
    try:
        steps = [StepConfig(**s) for s in (job.steps_config or [])]
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Stored job plan could not be read: {exc}",
        )

    # Re-validate rather than trusting the stored plan. The registry is a
    # property of the *current* server build, so a plan that validated at
    # submission can be stale — better a readable 400 here than a job queued
    # into a guaranteed runtime failure.
    _validate_steps(steps)

    new_job = await ops.create_job(
        db, name=job.name, submitted_by=user.id,
        steps_config=[s.model_dump(mode="json") for s in steps],
        target_pool_id=job.target_pool_id, target_node_id=job.target_node_id,
        priority=job.priority, storage_target=job.storage_target,
    )

    await runner.submit_job(db, new_job.id)

    return _job_to_info(new_job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: UUID, db: DbSession, user: CurrentUser):
    """Delete a job (must be in terminal state).

    Serves ``DELETE /api/jobs/{job_id}``. Refusing to delete a live job is what
    stops the runner from continuing to write step results against a row that no
    longer exists.

    Args:
        job_id: Job to delete.
        db: Async session, injected.
        user: Authenticated caller, injected.

    Returns:
        None: 204 No Content on success.

    Raises:
        HTTPException: 404 if no such job; 409 if the job is still ``pending`` or
            ``running``. Cancel it first.

    Side effects:
        Deletes and commits the ``jobs`` row.
    """
    # AI Note (authorization): no ownership check — any authenticated user can
    # delete any terminal job.
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status not in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Can only delete jobs in terminal state",
        )
    # AI Note: two things deliberately (or at least knowingly) survive this
    # delete. (1) The job's results tarball under RESULTS_DIR is never removed,
    # so deleted jobs leak disk until that directory is pruned by hand.
    # (2) The Job relationships declare no cascade, so related step_runs and
    # artifacts rows are orphaned rather than deleted; on SQLite (no FK
    # enforcement by default) this succeeds silently, but on a backend with
    # foreign keys enforced it would raise an IntegrityError instead of 204.
    await db.delete(job)
    await db.commit()
