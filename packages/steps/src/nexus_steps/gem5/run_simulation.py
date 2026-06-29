"""Run a gem5 simulation on a compute node.

Runs gem5 either directly on the node or, when ``container`` is set, inside a
Docker container via ``docker exec`` (the standard way to run a Linux gem5 build
on macOS). For the container path, ``working_dir`` must be a host path that is
bind-mounted at the SAME path inside the container (see docker_ensure_container)
so the gem5 binary, config, and the m5out stats directory resolve identically on
host and container — and the produced stats.txt lands back on the host.

stdout/stderr and the m5out stats directory are captured; ``LARGE_OUTPUT = True``
signals the storage manager to prefer high-capacity backends for the artifacts.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import uuid
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


def _find_docker(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    found = shutil.which("docker")
    if found:
        return found
    for cand in (
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
        os.path.expanduser("~/.docker/bin/docker"),
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ):
        if os.path.exists(cand):
            return cand
    return None


# ── Params ───────────────────────────────────────────────────────────────


class RunSimulationParams(BaseModel):
    """Parameters for the gem5_run_simulation step."""

    gem5_binary: str | None = Field(
        None,
        description=(
            "Path to the gem5 binary. Auto-selected per OS when omitted. With "
            "`container`, this is the path INSIDE the container (= host path if "
            "mounted at the same location)."
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
        description="Working directory for the gem5 process (required for `container`).",
    )
    container: str | None = Field(
        None,
        description=(
            "If set, run gem5 via `docker exec` in this container instead of "
            "directly on the node (use docker_ensure_container first)."
        ),
    )
    docker: str | None = Field(
        None,
        description="Path to the docker binary (auto-detected when omitted).",
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
    OUTPUT_KEYS = ["exit_code", "stats_artifact_id", "m5out_path", "container"]
    DESCRIPTION = "Run a gem5 simulation and collect results."

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

        if validated.container:
            # Run inside a Docker container. All gem5 paths (binary, config,
            # working_dir, m5out) are CONTAINER paths — they may not exist on the
            # host, so the host process must not touch them (no mkdtemp(dir=cwd),
            # no cwd=). `docker exec -w` sets the working dir inside the container.
            docker = _find_docker(validated.docker)
            if not docker:
                return {"error": "docker binary not found on node", "exit_code": -1}
            if not validated.working_dir:
                return {"error": "working_dir is required when using `container`", "exit_code": -1}
            cwd = validated.working_dir
            # A unique m5out dir, relative to working_dir, created INSIDE the
            # container. Random suffix without host filesystem access.
            m5out_dir = f"{cwd.rstrip('/')}/m5out_nexus_{uuid.uuid4().hex[:8]}"
            mk = subprocess.run(
                [docker, "exec", validated.container, "mkdir", "-p", m5out_dir],
                capture_output=True, text=True, timeout=30,
            )
            if mk.returncode != 0:
                return {"error": f"could not create m5out in container: {mk.stderr.strip()}",
                        "exit_code": mk.returncode}
            inner = [binary, f"--outdir={m5out_dir}", validated.config_script, *validated.script_args]
            cmd = [docker, "exec", "-w", cwd, validated.container, *inner]
            host_cwd = None  # docker client runs from anywhere
        else:
            # Run gem5 directly on the node.
            cwd = validated.working_dir or os.getcwd()
            m5out_dir = tempfile.mkdtemp(prefix="nexus_m5out_")
            cmd = [
                binary,
                f"--outdir={m5out_dir}",
                validated.config_script,
                *validated.script_args,
            ]
            host_cwd = cwd

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
            cwd=host_cwd,
            start_new_session=True,
        )

        stdout_file.close()
        stderr_file.close()

        return {
            "pid": proc.pid,
            "m5out_path": m5out_dir,
            "container": validated.container,
            "docker": _find_docker(validated.docker) if validated.container else None,
            "stdout_path": stdout_file.name,
            "stderr_path": stderr_file.name,
            "timeout": validated.timeout,
            "collect_stats": validated.collect_stats,
            "stats_artifact_id": None,
            "_command_str": " ".join(cmd),
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
            container = state.get("container")
            if container:
                docker = state.get("docker") or "docker"
                found = subprocess.run(
                    [docker, "exec", container, "test", "-f", stats_path],
                    capture_output=True, text=True, timeout=30,
                ).returncode == 0
            else:
                found = os.path.isfile(stats_path)
            if found:
                # Placeholder until the storage manager uploads + returns an ID.
                state["stats_artifact_id"] = stats_path

        return StepResult.SUCCESS if exit_status == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        pid = state.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
