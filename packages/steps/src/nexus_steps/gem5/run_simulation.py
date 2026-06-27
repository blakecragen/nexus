"""Run a gem5 simulation on a compute node.

Requires the ``gem5`` capability.  OS_VARIANTS point to typical gem5 binary
locations per platform.  stdout/stderr and the m5out stats directory are
captured; ``LARGE_OUTPUT = True`` signals the storage manager to prefer
high-capacity backends for the resulting artifacts.
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


class RunSimulationParams(BaseModel):
    """Parameters for the gem5_run_simulation step."""

    gem5_binary: str | None = Field(
        None,
        description=(
            "Path to the gem5 binary. Auto-selected per OS when omitted."
        ),
    )
    config_script: str = Field(
        ...,
        description="Path to the gem5 Python configuration script.",
        examples=["configs/example/se.py"],
    )
    script_args: list[str] = Field(
        default_factory=list,
        description="Additional arguments passed to the config script.",
    )
    working_dir: str | None = Field(
        None,
        description="Working directory for the gem5 process.",
    )
    timeout: int = Field(
        7200,
        description="Maximum simulation time in seconds.",
        ge=1,
        le=604800,
    )
    collect_stats: bool = Field(
        True,
        description="Whether to collect the m5out/stats.txt artifact on completion.",
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("gem5_run_simulation")
class RunSimulationStep(FlowStep):
    """Run a gem5 architectural simulation."""

    PARAMS_SCHEMA = RunSimulationParams
    OUTPUT_KEYS = ["exit_code", "stats_artifact_id", "m5out_path"]
    DESCRIPTION = "Run a gem5 simulation and collect results."

    REQUIRED_CAPABILITIES = ["gem5"]
    SUPPORTED_OS = ["macos", "linux"]
    LARGE_OUTPUT = True

    OS_VARIANTS = {
        "macos": {"gem5_binary": "/opt/gem5/build/ARM/gem5.opt"},
        "linux": {"gem5_binary": "/usr/local/bin/gem5.opt"},
    }

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = RunSimulationParams(**resolved)

        binary = validated.gem5_binary or "/usr/local/bin/gem5.opt"
        cwd = validated.working_dir or os.getcwd()

        # Create a dedicated m5out directory for this run.
        m5out_dir = tempfile.mkdtemp(prefix="nexus_m5out_")

        cmd = [
            binary,
            f"--outdir={m5out_dir}",
            validated.config_script,
            *validated.script_args,
        ]

        stdout_file = tempfile.NamedTemporaryFile(
            prefix="nexus_gem5_out_", suffix=".log", delete=False,
        )
        stderr_file = tempfile.NamedTemporaryFile(
            prefix="nexus_gem5_err_", suffix=".log", delete=False,
        )

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
            "m5out_path": m5out_dir,
            "stdout_path": stdout_file.name,
            "stderr_path": stderr_file.name,
            "timeout": validated.timeout,
            "collect_stats": validated.collect_stats,
            "stats_artifact_id": None,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
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

        # If stats collection is requested, check that stats.txt exists.
        if state.get("collect_stats"):
            stats_path = os.path.join(state["m5out_path"], "stats.txt")
            if os.path.isfile(stats_path):
                # In a real implementation the storage manager would upload
                # this file and return an artifact ID.  We store the path
                # as a placeholder.
                state["stats_artifact_id"] = stats_path

        return StepResult.SUCCESS if exit_status == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        pid = state.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
