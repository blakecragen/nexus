"""Step execution engine.

Responsible for:
- Loading the step class from STEP_REGISTRY
- Resolving OS-specific parameters
- Running startup() and managing subprocess execution
- Streaming stdout/stderr back to the server via WebSocket
- Polling check() to determine the outcome
- Handling cancel() on CancelStepCommand
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from nexus_common.agent_protocol import (
    CancelStepCommand,
    ExecuteStepCommand,
    StepCompleted,
    StepFailed,
    StepLog,
    StepStarted,
)
from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import STEP_REGISTRY, get_step

import nexus_steps  # noqa: F401 — triggers @register decorators, populates STEP_REGISTRY

from nexus_agent.capability import _detect_os_type
from nexus_agent.os_adapters import get_adapter

if TYPE_CHECKING:
    from nexus_agent.connection import AgentConnection

logger = logging.getLogger("nexus.agent.executor")


class StepExecutor:
    """Manages concurrent step executions on this node."""

    def __init__(self, connection: AgentConnection) -> None:
        self._connection = connection
        self._running_steps: dict[str, _RunningStep] = {}  # key: "{job_id}:{step_index}"

    @property
    def active_count(self) -> int:
        return len(self._running_steps)

    # ── Execute ────────────────────────────────────────────────────────

    async def execute(self, cmd: ExecuteStepCommand) -> None:
        """Execute a step command from the server."""
        key = f"{cmd.job_id}:{cmd.step_index}"
        logger.info("Executing step %s/%d (%s)", cmd.job_id, cmd.step_index, cmd.step_name)

        try:
            # Load step class
            step_cls = get_step(cmd.step_name)
            step = step_cls()

            # Resolve OS-specific parameters
            os_type = _detect_os_type()
            params = step_cls.resolve_for_os(cmd.params, os_type)

            # Build context (params already resolved server-side, but we carry os_type)
            cfg = self._connection.config
            # HTTP base from the ws:// server URL (ws://host:8000/ws/agent/.. → http://host:8000)
            http_base = cfg.server_url.split("/ws/")[0].replace("ws://", "http://", 1).replace("wss://", "https://", 1)
            ctx = StepContext(
                outputs=params,
                os_type=os_type,
                node_id=cfg.node_id,
                job_id=cmd.job_id,
                server_url=http_base,
                node_api_key=cfg.api_key,
            )

            # Run startup()
            state = step.startup(params, ctx)

            # Track the running step
            running = _RunningStep(
                job_id=cmd.job_id,
                step_index=cmd.step_index,
                step=step,
                state=state,
                params=params,
            )
            self._running_steps[key] = running

            # Notify server that step has started
            await self._connection.send_message(
                StepStarted(
                    job_id=cmd.job_id,
                    step_index=cmd.step_index,
                    state=state,
                ).model_dump(mode="json"),
                critical=True,
            )

            # If the step has a "command" in state, run it as a subprocess
            if "command" in state:
                await self._run_subprocess(running)
            else:
                # Poll-based step — call check() in a loop
                await self._poll_step(running)

            # Step completed successfully. Build outputs from the step's declared
            # OUTPUT_KEYS (steps put values directly in state, not under
            # "outputs"). Fall back to an explicit "outputs" dict if present.
            outputs = state.get("outputs")
            if not isinstance(outputs, dict):
                outputs = {k: state[k] for k in step_cls.OUTPUT_KEYS if k in state}
            command, stdout, stderr, exit_code = self._capture(running)
            await self._connection.send_message(
                StepCompleted(
                    job_id=cmd.job_id,
                    step_index=cmd.step_index,
                    outputs=outputs,
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                ).model_dump(mode="json"),
                critical=True,
            )
            logger.info("Step %s/%d completed successfully", cmd.job_id, cmd.step_index)

        except asyncio.CancelledError:
            logger.info("Step %s/%d was cancelled", cmd.job_id, cmd.step_index)
            command, stdout, stderr, _ = self._capture(self._running_steps.get(key))
            await self._connection.send_message(
                StepFailed(
                    job_id=cmd.job_id,
                    step_index=cmd.step_index,
                    error="Step cancelled",
                    exit_code=None,
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                ).model_dump(mode="json"),
                critical=True,
            )
        except Exception as exc:
            logger.error("Step %s/%d failed: %s", cmd.job_id, cmd.step_index, exc, exc_info=True)
            command, stdout, stderr, exit_code = self._capture(self._running_steps.get(key))
            await self._connection.send_message(
                StepFailed(
                    job_id=cmd.job_id,
                    step_index=cmd.step_index,
                    error=str(exc),
                    exit_code=getattr(exc, "returncode", None) or exit_code,
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                ).model_dump(mode="json"),
                critical=True,
            )
        finally:
            self._running_steps.pop(key, None)

    # ── Cancel ─────────────────────────────────────────────────────────

    async def cancel(self, cmd: CancelStepCommand) -> None:
        """Cancel a running step."""
        key = f"{cmd.job_id}:{cmd.step_index}"
        running = self._running_steps.get(key)
        if running is None:
            logger.warning("Cancel requested for unknown step %s", key)
            return

        logger.info("Cancelling step %s", key)

        # Call the step's cancel method
        try:
            running.step.cancel(running.state)
        except Exception as exc:
            logger.warning("Step cancel() raised: %s", exc)

        # Kill subprocess if running
        if running.process is not None and running.process.returncode is None:
            try:
                running.process.terminate()
                # Give it 5 seconds to terminate gracefully
                try:
                    await asyncio.wait_for(running.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    running.process.kill()
            except ProcessLookupError:
                pass

        # Cancel the task
        if running.task is not None and not running.task.done():
            running.task.cancel()

    # ── Subprocess Execution ───────────────────────────────────────────

    async def _run_subprocess(self, running: _RunningStep) -> None:
        """Execute a shell command as a subprocess and stream output."""
        adapter = get_adapter()
        command = running.state["command"]
        shell_cmd = adapter.shell_command()
        work_dir = running.state.get("work_dir", adapter.temp_dir())

        # Ensure work directory exists
        os.makedirs(work_dir, exist_ok=True)

        logger.debug("Running command: %s", command)

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            executable=shell_cmd,
        )
        running.process = process
        running.task = asyncio.current_task()

        # Stream stdout and stderr concurrently
        async def stream_pipe(pipe: asyncio.StreamReader, stream_name: str) -> None:
            while True:
                line = await pipe.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                running.captured[stream_name].append(text)  # buffer for the per-job log
                await self._connection.send_message(
                    StepLog(
                        job_id=running.job_id,
                        step_index=running.step_index,
                        stream=stream_name,
                        line=text,
                        timestamp=datetime.now(timezone.utc),
                    ).model_dump(mode="json")
                )

        await asyncio.gather(
            stream_pipe(process.stdout, "stdout"),
            stream_pipe(process.stderr, "stderr"),
        )

        exit_code = await process.wait()
        running.state["exit_code"] = exit_code

        if exit_code != 0:
            raise SubprocessError(
                f"Command exited with code {exit_code}",
                returncode=exit_code,
            )

        # After subprocess completes, run check() for final validation
        result = running.step.check(running.state)
        if result == StepResult.FAILED:
            raise StepCheckFailed("Step check() returned FAILED after subprocess")

    # ── Output capture (for the per-job terminal log) ──────────────────

    _CAP_BYTES = 256 * 1024  # keep the tail of each stream, per step

    def _capture(self, running: _RunningStep | None):
        """Return (command, stdout, stderr, exit_code) for a finished step.

        Command-streaming steps buffer lines in memory; poll-based steps (the
        shipped run_command/gem5 steps) wrote to temp files — read those back.
        Each stream is truncated to the last _CAP_BYTES.
        """
        if running is None:
            return None, None, None, None
        state = running.state
        command = state.get("_command_str") or state.get("command")
        exit_code = state.get("exit_code")

        def _read(stream_name: str, path_key: str) -> str | None:
            buf = running.captured.get(stream_name)
            if buf:
                text = "\n".join(buf)
            else:
                path = state.get(path_key)
                if not path:
                    return None
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    return None
            if len(text) > self._CAP_BYTES:
                text = "…[truncated]…\n" + text[-self._CAP_BYTES:]
            return text or None

        return command, _read("stdout", "stdout_path"), _read("stderr", "stderr_path"), exit_code

    # ── Poll-based Execution ───────────────────────────────────────────

    async def _poll_step(self, running: _RunningStep) -> None:
        """Poll a step's check() method until it completes."""
        running.task = asyncio.current_task()

        while True:
            result = running.step.check(running.state)
            if result == StepResult.SUCCESS:
                return
            if result == StepResult.FAILED:
                raise StepCheckFailed("Step check() returned FAILED")
            # Still RUNNING — wait and poll again
            await asyncio.sleep(1.0)


# ── Internal Types ─────────────────────────────────────────────────────


class _RunningStep:
    """Tracks a single step execution in progress."""

    __slots__ = ("job_id", "step_index", "step", "state", "params", "process", "task", "captured")

    def __init__(
        self,
        job_id: str,
        step_index: int,
        step: FlowStep,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> None:
        self.job_id = job_id
        self.step_index = step_index
        self.step = step
        self.state = state
        self.params = params
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.captured: dict[str, list[str]] = {"stdout": [], "stderr": []}


class SubprocessError(Exception):
    """Raised when a subprocess exits with a non-zero code."""

    def __init__(self, message: str, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


class StepCheckFailed(Exception):
    """Raised when a step's check() method returns FAILED."""
