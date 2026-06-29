"""Job management routes — list, submit, detail, cancel, delete, results."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse

from nexus_common.models.schemas import JobDetail, JobInfo, JobSubmit, StepRunInfo
from nexus_common.steps.registry import STEP_REGISTRY
from nexus_server.api.deps import CurrentUser, DbSession, Runner
from nexus_server.db import ops

router = APIRouter()

# Per-job result artifacts uploaded by agents (no external storage backend needed).
RESULTS_DIR = Path(".nexus-results")


def _job_results_path(job_id) -> Path:
    return RESULTS_DIR / str(job_id) / "results.tar.gz"


def _job_to_info(job) -> JobInfo:
    return JobInfo(
        id=job.id, name=job.name, submitted_by=job.submitted_by,
        target_pool_id=job.target_pool_id, target_node_id=job.target_node_id,
        priority=job.priority, status=job.status, current_step=job.current_step,
        error=job.error, created_at=job.created_at,
        started_at=job.started_at, completed_at=job.completed_at,
    )


def _step_run_to_info(sr) -> StepRunInfo:
    return StepRunInfo(
        id=sr.id, job_id=sr.job_id, step_index=sr.step_index,
        step_name=sr.step_name, status=sr.status, node_id=sr.node_id,
        input_params=sr.input_params, output_params=sr.output_params,
        error=sr.error, started_at=sr.started_at, finished_at=sr.finished_at,
    )


@router.get("", response_model=list[JobInfo])
async def list_jobs(
    db: DbSession,
    user: CurrentUser,
    job_status: str | None = None,
    pool_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """List jobs with optional filtering and pagination."""
    jobs = await ops.list_jobs(
        db, status=job_status, pool_id=pool_id,
        limit=limit, offset=offset,
    )
    return [_job_to_info(j) for j in jobs]


@router.post("", response_model=JobInfo, status_code=status.HTTP_201_CREATED)
async def submit_job(body: JobSubmit, db: DbSession, user: CurrentUser, runner: Runner):
    """Submit a new job with step validation."""
    # Validate that all step names are registered
    for i, step_cfg in enumerate(body.steps):
        if step_cfg.step not in STEP_REGISTRY:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Step {i} references unknown step '{step_cfg.step}'. "
                       f"Available: {sorted(STEP_REGISTRY.keys())}",
            )

        # Run parameter validation on each step
        step_cls = STEP_REGISTRY[step_cfg.step]
        errors = step_cls.validate_params(step_cfg.params)
        if errors:
            detail = "; ".join(str(e) for e in errors)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Step {i} ('{step_cfg.step}') validation failed: {detail}",
            )

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
    await runner.submit_job(db, job.id)

    return _job_to_info(job)


@router.get("/{job_id}", response_model=JobDetail)
async def get_job(job_id: UUID, db: DbSession, user: CurrentUser):
    """Get job detail including step runs and context data."""
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    step_runs = await ops.get_step_runs_for_job(db, job_id)
    return JobDetail(
        job=_job_to_info(job),
        steps=[_step_run_to_info(sr) for sr in step_runs],
        context_data=job.context_data or {},
        has_log=bool(job.log_text),
        has_results=_job_results_path(job_id).is_file(),
    )


@router.put("/{job_id}/results")
async def upload_job_results(job_id: UUID, request: Request, db: DbSession, file: UploadFile):
    """Agent uploads a job's result tarball. Authenticated by node api_key
    (header X-Node-Key), since this is called by the agent, not a logged-in user."""
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
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            size += len(chunk)
    return {"ok": True, "size_bytes": size}


@router.get("/{job_id}/results/download")
async def download_job_results(job_id: UUID, db: DbSession, user: CurrentUser):
    """Download a job's collected results tarball."""
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    path = _job_results_path(job_id)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No results for this job")
    return FileResponse(
        path, media_type="application/gzip", filename=f"job_{job_id}_results.tar.gz",
    )


@router.get("/{job_id}/log")
async def get_job_log(job_id: UUID, db: DbSession, user: CurrentUser, download: bool = False):
    """Return the aggregated per-job terminal log as plain text.

    With ?download=1 the response is sent as an attachment .txt file.
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
    """Cancel a pending or running job."""
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is already in terminal state: {job.status}",
        )
    await runner.cancel_job(db, job_id)
    job = await ops.get_job_by_id(db, job_id)
    return _job_to_info(job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: UUID, db: DbSession, user: CurrentUser):
    """Delete a job (must be in terminal state)."""
    job = await ops.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status not in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Can only delete jobs in terminal state",
        )
    await db.delete(job)
    await db.commit()
