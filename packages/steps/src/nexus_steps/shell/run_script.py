"""Execute an uploaded script file on a compute node.

The script must already exist on the target node's filesystem (typically
placed there by a preceding file-transfer step or pre-provisioned).  The
step makes the file executable, invokes it, and captures the exit code.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import tempfile
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class RunScriptParams(BaseModel):
    """Parameters for the run_script step."""

    script_path: str = Field(
        ...,
        description="Absolute path to the script file on the node.",
        examples=["/tmp/nexus_scripts/setup.sh"],
    )
    args: list[str] = Field(
        default_factory=list,
        description="Positional arguments passed to the script.",
    )
    working_dir: str | None = Field(
        None,
        description="Working directory. Defaults to the script's parent directory.",
    )
    timeout: int = Field(
        3600,
        description="Maximum execution time in seconds.",
        ge=1,
        le=86400,
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("run_script")
class RunScriptStep(FlowStep):
    """Execute an uploaded script file on a compute node."""

    PARAMS_SCHEMA = RunScriptParams
    OUTPUT_KEYS = ["exit_code"]
    DESCRIPTION = "Execute a script file with optional arguments."

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = RunScriptParams(**resolved)

        script = validated.script_path
        if not os.path.isfile(script):
            return {"error": f"Script not found: {script}", "exit_code": -1}

        # Ensure the script is executable.
        current_mode = os.stat(script).st_mode
        os.chmod(script, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        cwd = validated.working_dir or os.path.dirname(script)

        stdout_file = tempfile.NamedTemporaryFile(
            prefix="nexus_script_out_", suffix=".log", delete=False,
        )
        stderr_file = tempfile.NamedTemporaryFile(
            prefix="nexus_script_err_", suffix=".log", delete=False,
        )

        cmd = [script, *validated.args]
        proc = subprocess.Popen(
            cmd,
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
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        if "error" in state:
            return StepResult.FAILED

        pid = state["pid"]
        try:
            result = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            state["exit_code"] = state.get("exit_code", -1)
            return StepResult.FAILED

        if result == (0, 0):
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
