"""Async job runner — orchestrates step execution across distributed agents.

Adapted from HVE-Automation-Worker's runner with distributed execution model:
steps are dispatched to remote agents via WebSocket rather than running locally.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_common.agent_protocol import ExecuteStepCommand
from nexus_common.steps.base import StepContext
from nexus_common.steps.registry import get_step
from nexus_server.db import ops
from nexus_server.db.models import Job
from nexus_server.runner.scheduler import find_node_for_step

logger = logging.getLogger(__name__)


def _format_log_block(idx: int, step_name: str, node_label: str, status: str, result: dict) -> str:
    """Render one step's command + output as a terminal-log block."""
    lines = [f"===== [step {idx}] {step_name} on {node_label} ====="]
    command = result.get("command")
    if command:
        lines.append(f"$ {command}")
    stdout = (result.get("stdout") or "").rstrip("\n")
    stderr = (result.get("stderr") or "").rstrip("\n")
    if stdout:
        lines.append(stdout)
    if stderr:
        lines.append("--- stderr ---")
        lines.append(stderr)
    if status != "success":
        err = result.get("error")
        if err and not stderr:
            lines.append(f"error: {err}")
    ec = result.get("exit_code")
    ec_part = f"exit code: {ec}  " if ec is not None else ""
    lines.append(f"[{ec_part}status: {status}]")
    return "\n".join(lines) + "\n\n"


class JobRunner:
    """Manages the lifecycle of job execution.

    For each running job, advances through its steps sequentially:
    1. Find a suitable node for the step
    2. Dispatch the step to the agent via WebSocket
    3. Wait for completion (agent sends step.completed / step.failed)
    4. Merge outputs into job context
    5. Advance to next step or complete the job
    """

    def __init__(self, ws_manager, credential_manager=None):
        self._ws = ws_manager  # WebSocket connection manager
        self._cred_manager = credential_manager
        self._active_jobs: dict[UUID, asyncio.Task] = {}
        self._step_events: dict[str, asyncio.Event] = {}  # job_id:step_idx -> event
        self._step_results: dict[str, dict] = {}  # job_id:step_idx -> result

    async def submit_job(self, db: AsyncSession, job_id: UUID) -> None:
        """Start processing a job asynchronously."""
        job = await ops.get_job_by_id(db, job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        task = asyncio.create_task(self._run_job(job_id))
        self._active_jobs[job_id] = task

    async def cancel_job(self, db: AsyncSession, job_id: UUID) -> None:
        """Cancel a running job."""
        task = self._active_jobs.get(job_id)
        if task and not task.done():
            task.cancel()
        await ops.update_job(db, job_id, status="cancelled",
                             completed_at=datetime.now(timezone.utc))

    def on_step_completed(self, job_id: str, step_index: int, outputs: dict,
                          command: str | None = None, stdout: str | None = None,
                          stderr: str | None = None, exit_code: int | None = None) -> None:
        """Called by WebSocket handler when agent reports step completion."""
        key = f"{job_id}:{step_index}"
        self._step_results[key] = {
            "status": "success", "outputs": outputs,
            "command": command, "stdout": stdout, "stderr": stderr, "exit_code": exit_code,
        }
        event = self._step_events.get(key)
        if event:
            event.set()

    def on_step_failed(self, job_id: str, step_index: int, error: str,
                       command: str | None = None, stdout: str | None = None,
                       stderr: str | None = None, exit_code: int | None = None) -> None:
        """Called by WebSocket handler when agent reports step failure."""
        key = f"{job_id}:{step_index}"
        self._step_results[key] = {
            "status": "failed", "error": error,
            "command": command, "stdout": stdout, "stderr": stderr, "exit_code": exit_code,
        }
        event = self._step_events.get(key)
        if event:
            event.set()

    async def _run_job(self, job_id: UUID) -> None:
        """Main job execution loop."""
        from nexus_server.db.session import get_session_factory

        session_factory = get_session_factory()
        async with session_factory() as db:
            try:
                job = await ops.get_job_by_id(db, job_id)
                if not job:
                    return

                await ops.update_job(db, job_id, status="running",
                                     started_at=datetime.now(timezone.utc))

                steps_config = job.steps_config
                context = StepContext(outputs=job.context_data or {})
                idx = job.current_step

                while idx < len(steps_config):
                    step_cfg = steps_config[idx]
                    step_name = step_cfg["step"]
                    step_params = step_cfg.get("params", {})
                    on_fail = step_cfg.get("on_fail", "stop")

                    step_cls = get_step(step_name)

                    # Update job progress
                    await ops.update_job(db, job_id, current_step=idx)

                    # Create step run record
                    step_run = await ops.create_step_run(
                        db, job_id=job_id, step_index=idx, step_name=step_name,
                        input_params=step_params,
                    )

                    if not step_cls.REQUIRES_NODE:
                        # Control-plane step — execute locally
                        result = await self._execute_local_step(
                            db, step_cls, step_params, context, step_run.id,
                        )
                    else:
                        # Remote step — dispatch to agent. Step-level targets
                        # override job-level; this lets one job hit multiple
                        # gem5 hosts on different OSes.
                        step_target_node = step_cfg.get("target_node_id") or job.target_node_id
                        step_target_pool = step_cfg.get("target_pool_id") or job.target_pool_id
                        step_target_os = step_cfg.get("target_os")
                        result = await self._execute_remote_step(
                            db, job, step_cls, step_name, step_params, context,
                            step_run.id, idx,
                            target_node_id=step_target_node,
                            target_pool_id=step_target_pool,
                            target_os=step_target_os,
                        )

                    # Append this step's command + output to the per-job log
                    # (committed incrementally so a crash leaves a partial log).
                    node_label = result.get("node_label", "control-plane")
                    await ops.append_job_log(
                        db, job_id,
                        _format_log_block(idx, step_name, node_label, result["status"], result),
                    )

                    if result["status"] == "success":
                        outputs = result.get("outputs", {})
                        context.outputs.update(outputs)
                        # A successful step clears the prior failure flag so a
                        # later jump(on="fail") doesn't fire on a stale signal.
                        context.outputs.pop("_last_failed", None)
                        await ops.update_step_run(
                            db, step_run.id, status="success",
                            output_params=outputs,
                            finished_at=datetime.now(timezone.utc),
                        )
                        await ops.update_job(db, job_id, context_data=context.outputs)

                        # Check for jump directive
                        jump_target = result.get("jump_target")
                        if jump_target is not None:
                            idx = jump_target
                            continue
                    else:
                        error = result.get("error", "Step failed")
                        await ops.update_step_run(
                            db, step_run.id, status="failed", error=error,
                            finished_at=datetime.now(timezone.utc),
                        )
                        if on_fail == "stop":
                            await ops.update_job(
                                db, job_id, status="failed", error=error,
                                completed_at=datetime.now(timezone.utc),
                            )
                            return
                        # on_fail="continue": flag the failure for downstream
                        # conditional steps (e.g. jump on="fail") and persist.
                        context.outputs["_last_failed"] = True
                        await ops.update_job(db, job_id, context_data=context.outputs)

                    idx += 1

                # All steps completed
                await ops.update_job(
                    db, job_id, status="completed",
                    completed_at=datetime.now(timezone.utc),
                )

            except asyncio.CancelledError:
                logger.info(f"Job {job_id} cancelled")
            except Exception as e:
                logger.exception(f"Job {job_id} failed with error")
                async with session_factory() as db2:
                    await ops.update_job(
                        db2, job_id, status="failed", error=str(e),
                        completed_at=datetime.now(timezone.utc),
                    )
            finally:
                self._active_jobs.pop(job_id, None)

    async def _execute_local_step(
        self, db: AsyncSession, step_cls, params: dict,
        context: StepContext, step_run_id: UUID,
    ) -> dict:
        """Execute a control-plane step locally (e.g., sleep, jump)."""
        step = step_cls()
        try:
            await ops.update_step_run(
                db, step_run_id, status="running",
                started_at=datetime.now(timezone.utc),
            )
            resolved = context.resolve(params)
            state = step.startup(resolved, context)

            # Save state for crash recovery
            await ops.update_step_run(db, step_run_id, state=state)

            # Poll until complete
            from nexus_common.models.enums import StepResult
            while True:
                result = step.check(state)
                if result == StepResult.SUCCESS:
                    outputs = {k: state.get(k) for k in step_cls.OUTPUT_KEYS if k in state}
                    jump_target = state.get("__jump_target")
                    ret = {"status": "success", "outputs": outputs}
                    if jump_target is not None:
                        ret["jump_target"] = jump_target
                    return ret
                elif result == StepResult.FAILED:
                    return {"status": "failed", "error": state.get("error", "Step failed")}
                await asyncio.sleep(1)

        except Exception as e:
            return {"status": "failed", "error": str(e)}

    async def _execute_remote_step(
        self, db: AsyncSession, job: Job, step_cls, step_name: str,
        params: dict, context: StepContext, step_run_id: UUID, step_index: int,
        target_node_id=None, target_pool_id=None, target_os: str | None = None,
    ) -> dict:
        """Dispatch a step to a remote agent and wait for completion.

        Step-level target_* overrides take precedence over the job-level
        targets stored on the Job row; the caller is responsible for that
        precedence.
        """
        # Find a suitable node honoring per-step targeting overrides.
        node = await find_node_for_step(
            db, step_name,
            target_pool_id=target_pool_id,
            target_node_id=target_node_id,
            target_os=target_os,
        )
        if not node:
            target_desc = []
            if target_os:
                target_desc.append(f"os={target_os}")
            if target_node_id:
                target_desc.append(f"node={target_node_id}")
            if target_pool_id:
                target_desc.append(f"pool={target_pool_id}")
            qualifier = f" ({', '.join(target_desc)})" if target_desc else ""
            return {
                "status": "failed",
                "error": f"No available node for step '{step_name}'{qualifier}",
            }

        await ops.update_step_run(
            db, step_run_id, status="running", node_id=node.id,
            started_at=datetime.now(timezone.utc),
        )

        # Resolve OS-specific params
        resolved = context.resolve(params)
        resolved = step_cls.resolve_for_os(resolved, node.os_type)

        # Resolve credential if needed
        cred_config = None
        cred_name = resolved.pop("credential_name", None)
        if cred_name and self._cred_manager:
            try:
                cred_config = await self._cred_manager.get_by_name(db, cred_name)
            except KeyError:
                return {"status": "failed", "error": f"Credential '{cred_name}' not found"}

        # Send execution command to agent
        command = ExecuteStepCommand(
            job_id=str(job.id),
            step_index=step_index,
            step_name=step_name,
            params=resolved,
            credential_config=cred_config,
        )

        # Set up event for completion notification
        key = f"{job.id}:{step_index}"
        self._step_events[key] = asyncio.Event()

        try:
            # send_to_agent → ws.send_json, which JSON-encodes once. Pass a dict
            # (not model_dump_json()'s string) or the agent receives a quoted
            # string and `data.get(...)` blows up.
            sent = await self._ws.send_to_agent(str(node.id), command.model_dump(mode="json"))
            if not sent:
                return {"status": "failed", "error": f"Agent for node {node.hostname} not connected"}

            # Wait for agent to report completion
            await asyncio.wait_for(self._step_events[key].wait(), timeout=7200)

            result = self._step_results.pop(key, {"status": "failed", "error": "No result received"})
            result["node_label"] = node.hostname or str(node.id)
            return result

        except asyncio.TimeoutError:
            return {"status": "failed", "error": "Step execution timed out (2h)"}
        finally:
            self._step_events.pop(key, None)
            self._step_results.pop(key, None)
