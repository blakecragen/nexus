"""OS-aware shell command execution step.

Executes a shell command on a compute node, routing stdout and stderr to
temporary files so large outputs don't bloat the job state.  OS_VARIANTS
select the default shell binary per platform.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class RunCommandParams(BaseModel):
    """Parameters for the run_command step."""

    command: str = Field(
        ...,
        description="Shell command string to execute.",
        examples=["echo hello", "ls -la /tmp"],
    )
    working_dir: str | None = Field(
        None,
        description="Working directory for the command. Defaults to the agent's cwd.",
    )
    timeout: int = Field(
        3600,
        description="Maximum execution time in seconds.",
        ge=1,
        le=86400,
    )
    shell: str | None = Field(
        None,
        description=(
            "Path to the shell binary. Auto-selected per OS when omitted."
        ),
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("run_command")
class RunCommandStep(FlowStep):
    """Execute a shell command on a compute node."""

    PARAMS_SCHEMA = RunCommandParams
    OUTPUT_KEYS = ["exit_code", "stdout_path", "stderr_path"]
    DESCRIPTION = "Run an OS-aware shell command on a compute node."

    OS_VARIANTS = {
        "macos": {"shell": "/bin/zsh"},
        "linux": {"shell": "/bin/bash"},
        "windows": {"shell": "powershell.exe"},
    }

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = RunCommandParams(**resolved)

        shell = validated.shell or "/bin/sh"
        cwd = validated.working_dir or os.getcwd()

        stdout_file = tempfile.NamedTemporaryFile(
            prefix="nexus_stdout_", suffix=".log", delete=False,
        )
        stderr_file = tempfile.NamedTemporaryFile(
            prefix="nexus_stderr_", suffix=".log", delete=False,
        )

        proc = subprocess.Popen(
            [shell, "-c", validated.command],
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            start_new_session=True,
        )

        stdout_file.close()
        stderr_file.close()

        return {
            "pid": proc.pid,
            "stdout_path": stdout_file.name,
            "stderr_path": stderr_file.name,
            "timeout": validated.timeout,
            "_command_str": f"{shell} -c {validated.command!r}",
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        pid = state["pid"]
        try:
            result = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # Process already reaped -- treat as completed.
            state["exit_code"] = state.get("exit_code", -1)
            return StepResult.FAILED

        if result == (0, 0):
            # Still running.
            return StepResult.RUNNING

        exit_status = os.WEXITSTATUS(result[1]) if os.WIFEXITED(result[1]) else -1
        state["exit_code"] = exit_status
        return StepResult.SUCCESS if exit_status == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        pid = state.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
