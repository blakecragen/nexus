"""Crash recovery — resume interrupted jobs on server restart."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_server.db import ops
from nexus_server.runner.runner import JobRunner

logger = logging.getLogger(__name__)


async def resume_active_jobs(db: AsyncSession, runner: JobRunner) -> int:
    """Find jobs that were active when the server last stopped and resume them.

    Returns the number of jobs resumed.
    """
    active_jobs = await ops.get_active_jobs(db)
    resumed = 0

    for job in active_jobs:
        logger.info(f"Resuming job {job.id} (name={job.name}, step={job.current_step})")
        try:
            await runner.submit_job(db, job.id)
            resumed += 1
        except Exception as e:
            logger.error(f"Failed to resume job {job.id}: {e}")
            await ops.update_job(db, job.id, status="failed", error=f"Resume failed: {e}")

    if resumed:
        logger.info(f"Resumed {resumed} active job(s)")
    return resumed
