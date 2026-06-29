"""Ensure a named Docker container exists and is running on the node.

Idempotent "create-or-attach": if a container with the given name is already
running, it's reused; if it exists but is stopped, it's started; otherwise it's
created from the image with a keep-alive command (so it stays up for later
`docker exec` steps, e.g. running gem5 inside a Linux container on macOS).

Mounts default to binding a host directory at the SAME path inside the
container, so absolute paths (gem5 binary, configs, m5out) are valid in both —
and files the container writes land back on the host for collection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class EnsureContainerParams(BaseModel):
    """Parameters for the docker_ensure_container step."""

    name: str = Field(
        "gem5_img",
        description="Container name to create or attach to.",
    )
    image: str = Field(
        "ghcr.io/gem5/ubuntu-24.04_all-dependencies:latest",
        description="Image to create the container from if it doesn't exist.",
    )
    mounts: list[str] = Field(
        default_factory=list,
        description=(
            "Paths to bind-mount. Each entry is either 'HOST:CONTAINER' or just "
            "'HOST' (mounted at the same path inside the container — recommended "
            "so absolute paths match host and container)."
        ),
        examples=[["/Users/me/Desktop/gem5"]],
    )
    workdir: str | None = Field(
        None,
        description="Default working directory inside the container.",
    )
    docker: str | None = Field(
        None,
        description="Path to the docker binary. Auto-detected when omitted.",
    )
    recreate: bool = Field(
        False,
        description="If true, remove an existing container with this name and recreate it.",
    )
    timeout: int = Field(
        600, description="Max time in seconds for image pull / container start.",
        ge=1, le=86400,
    )


# ── Step ─────────────────────────────────────────────────────────────────


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


@register("docker_ensure_container")
class EnsureContainerStep(FlowStep):
    """Create-or-attach a named Docker container, ready for `docker exec`."""

    PARAMS_SCHEMA = EnsureContainerParams
    OUTPUT_KEYS = ["container", "docker", "created", "exit_code"]
    DESCRIPTION = "Ensure a named Docker container exists and is running."

    SUPPORTED_OS = ["macos", "linux"]

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = EnsureContainerParams(**resolved)

        docker = _find_docker(validated.docker)
        if not docker:
            return {"error": "docker binary not found on node", "exit_code": -1}

        name = validated.name

        def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
            return subprocess.run(
                [docker, *args], capture_output=True, text=True, timeout=timeout,
            )

        log: list[str] = []
        created = False
        try:
            # Is a container with this name running / present?
            running = _docker(
                "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"
            ).stdout.strip()
            exists = _docker(
                "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"
            ).stdout.strip()

            if validated.recreate and exists == name:
                log.append(f"removing existing container '{name}' (recreate=true)")
                _docker("rm", "-f", name, timeout=120)
                exists = ""
                running = ""

            if running == name:
                log.append(f"container '{name}' already running — attaching")
            elif exists == name:
                log.append(f"container '{name}' exists but stopped — starting")
                r = _docker("start", name, timeout=120)
                if r.returncode != 0:
                    return {"error": f"docker start failed: {r.stderr.strip()}",
                            "exit_code": r.returncode, "_log": "\n".join(log)}
            else:
                # Build `docker run -d --name ... -v ... -w ... image <keepalive>`.
                args = ["run", "-d", "--name", name]
                for m in validated.mounts:
                    spec = m if ":" in m else f"{m}:{m}"  # same path in-container
                    args += ["-v", spec]
                if validated.workdir:
                    args += ["-w", validated.workdir]
                # Keep-alive so the container stays up for later exec steps.
                args += [validated.image, "sleep", "infinity"]
                log.append(f"creating container '{name}' from {validated.image}")
                r = _docker(*args, timeout=validated.timeout)
                if r.returncode != 0:
                    return {"error": f"docker run failed: {r.stderr.strip()}",
                            "exit_code": r.returncode, "_log": "\n".join(log)}
                created = True

            # Confirm it's up now.
            up = _docker(
                "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"
            ).stdout.strip()
            if up != name:
                return {"error": f"container '{name}' is not running after ensure",
                        "exit_code": 1, "_log": "\n".join(log)}

            return {
                "container": name,
                "docker": docker,
                "created": created,
                "exit_code": 0,
                "_command_str": f"docker ensure-container {name} ({validated.image})",
                "_log": "\n".join(log),
            }
        except subprocess.TimeoutExpired:
            return {"error": "docker command timed out", "exit_code": -1, "_log": "\n".join(log)}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}", "exit_code": -1, "_log": "\n".join(log)}

    def check(self, state: dict[str, Any]) -> StepResult:
        # startup() does the work synchronously; this is a one-shot result.
        return StepResult.SUCCESS if state.get("exit_code") == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        # Nothing long-running to cancel.
        return None
