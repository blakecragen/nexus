"""Crash recovery — resume interrupted jobs on server restart.

Nexus keeps all job progress in the database (``Job.status``,
``Job.current_step``, ``Job.context_data``, plus per-step ``StepRun`` rows), so
a server restart does not have to lose in-flight work. This module runs once
during application startup: it finds every job the DB still believes is active
and hands it back to the :class:`~nexus_server.runner.runner.JobRunner`, which
re-enters its execution loop at ``Job.current_step``.

Called by ``nexus_server.main.lifespan`` right after the ``JobRunner`` is
constructed. Depends on ``db.ops.get_active_jobs`` for the candidate set and
``JobRunner.submit_job`` to restart execution.

AI Note: resumption is *step-granular, not mid-step*. A step that was already
dispatched to an agent when the server died is re-dispatched from the top —
remote steps must therefore be idempotent (or at least safe to re-run) or the
job will double-execute that step's side effects on the node.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_server.db import ops
from nexus_server.runner.runner import JobRunner

logger = logging.getLogger(__name__)


async def resume_active_jobs(db: AsyncSession, runner: JobRunner) -> int:
    """Find jobs that were active when the server last stopped and resume them.

    Queries for jobs still in a non-terminal state (``pending`` / ``queued`` /
    ``running`` — see ``ops.get_active_jobs``) and re-submits each one to the
    runner. ``JobRunner.submit_job`` only spawns a task, so this returns quickly
    and the resumed jobs continue in the background.

    Args:
        db: Session used both for the lookup and for marking un-resumable jobs
            failed. Note the resumed job tasks do *not* use this session — the
            runner opens its own session per job.
        runner: The process-wide job runner (``app.state.runner``).

    Returns:
        The number of jobs successfully handed to the runner. Jobs that raised
        during submission are excluded from the count.

    Side effects:
        Spawns one asyncio task per resumed job; writes ``status="failed"`` to
        any job whose resubmission raised.

    AI Note: a job resumes at ``Job.current_step``, which the runner only
    advances *before* executing a step. So the step that was in flight at crash
    time is re-run from scratch, not continued.
    """
    active_jobs = await ops.get_active_jobs(db)
    resumed = 0

    for job in active_jobs:
        logger.info(f"Resuming job {job.id} (name={job.name}, step={job.current_step})")
        try:
            await runner.submit_job(db, job.id)
            resumed += 1
        except Exception as e:
            # AI Note: one bad job must not abort startup — swallow, mark the
            # job failed, and keep resuming the rest. This is why the loop
            # catches broad Exception rather than letting it propagate into
            # the FastAPI lifespan (which would kill the server).
            logger.error(f"Failed to resume job {job.id}: {e}")
            await ops.update_job(db, job.id, status="failed", error=f"Resume failed: {e}")

    if resumed:
        logger.info(f"Resumed {resumed} active job(s)")
    return resumed
