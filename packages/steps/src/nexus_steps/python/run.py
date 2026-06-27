"""Run Python code on a compute node.

Accepts either inline ``code`` (a string evaluated by the chosen
interpreter) or a pre-existing ``script_path`` on the node's filesystem.
``OS_VARIANTS`` picks a sensible default interpreter per platform; users
can override via the ``interpreter`` parameter.

Like the shell ``run_command`` step, stdout and stderr are routed to
temporary files so large outputs don't bloat the persisted state, and the
exit code is exposed via ``OUTPUT_KEYS`` for downstream conditional
control flow.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from typing import Any

from pydantic import BaseModel, Field, model_validator

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import (
    AtLeastOneRule,
    FlowStep,
    InputRule,
    StepContext,
)
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class RunPythonParams(BaseModel):
    """Parameters for the run_python step."""

    code: str | None = Field(
        None,
        description="Inline Python source to execute. Mutually inclusive with script_path.",
        examples=["import sys; print(sys.version)"],
    )
    script_path: str | None = Field(
        None,
        description="Absolute path to a .py file already on the node.",
        examples=["/tmp/nexus_scripts/run.py"],
    )
    args: list[str] = Field(
        default_factory=list,
        description="Positional arguments passed to the script.",
    )
    working_dir: str | None = Field(
        None,
        description="Working directory. Defaults to the agent's cwd.",
    )
    timeout: int = Field(
        3600,
        description="Maximum execution time in seconds.",
        ge=1,
        le=86400,
    )
    interpreter: str | None = Field(
        None,
        description=(
            "Path to the Python interpreter. Auto-selected per OS when omitted."
        ),
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables for the subprocess (merged into os.environ).",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "RunPythonParams":
        if bool(self.code) == bool(self.script_path):
            raise ValueError("provide exactly one of 'code' or 'script_path'")
        return self


# ── Step ─────────────────────────────────────────────────────────────────


@register("run_python")
class RunPythonStep(FlowStep):
    """Run Python code on a compute node (inline or from a script file)."""

    PARAMS_SCHEMA = RunPythonParams
    OUTPUT_KEYS = ["exit_code", "stdout_path", "stderr_path"]
    DESCRIPTION = "Run Python (inline code or a script) on a compute node."

    OS_VARIANTS = {
        "macos": {"interpreter": "/usr/bin/python3"},
        "linux": {"interpreter": "/usr/bin/python3"},
        "windows": {"interpreter": "python.exe"},
    }

    @classmethod
    def input_rules(cls) -> list[InputRule]:
        # Either code OR script_path satisfies the source requirement; the
        # exclusivity (not-both) is enforced by the model_validator above.
        return [AtLeastOneRule(["code", "script_path"], "Python source")]

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = RunPythonParams(**resolved)

        interpreter = validated.interpreter or "python3"
        cwd = validated.working_dir or os.getcwd()

        # Resolve the source: inline code is materialized to a tempfile so
        # the subprocess invocation looks identical to the script_path path.
        cleanup_source: str | None = None
        if validated.code is not None:
            src = tempfile.NamedTemporaryFile(
                prefix="nexus_pycode_", suffix=".py", delete=False, mode="w",
            )
            src.write(validated.code)
            src.close()
            source_path = src.name
            cleanup_source = source_path
        else:
            source_path = validated.script_path
            if not os.path.isfile(source_path):
                return {"error": f"Script not found: {source_path}", "exit_code": -1}

        stdout_file = tempfile.NamedTemporaryFile(
            prefix="nexus_python_out_", suffix=".log", delete=False,
        )
        stderr_file = tempfile.NamedTemporaryFile(
            prefix="nexus_python_err_", suffix=".log", delete=False,
        )

        env = os.environ.copy()
        env.update(validated.env)
        # Unbuffered output so the dashboard sees stdout in real time when
        # the executor switches to its streaming path in the future.
        env.setdefault("PYTHONUNBUFFERED", "1")

        cmd = [interpreter, source_path, *validated.args]
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

        stdout_file.close()
        stderr_file.close()

        return {
            "pid": proc.pid,
            "source_path": source_path,
            "cleanup_source": cleanup_source,
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

        # Best-effort cleanup of the materialized inline-code tempfile.
        cleanup = state.get("cleanup_source")
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass

        return StepResult.SUCCESS if exit_status == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        pid = state.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
