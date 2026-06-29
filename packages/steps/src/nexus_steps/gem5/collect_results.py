"""Collect a gem5 m5out directory and upload it to the server as a downloadable
per-job result artifact.

Chained after ``gem5_run_simulation``; ``m5out_path`` is auto-resolved from the
upstream step's context. Works whether gem5 ran directly on the node or inside a
Docker container (``container`` is likewise resolved from context):

  - container mode: tar m5out INSIDE the container, ``docker cp`` it to the host,
    then upload to the server.
  - direct mode: tar the host m5out directory, then upload.

Upload is a PUT to ``/api/jobs/{job_id}/results`` authenticated by the node's
api_key — no external storage backend required. LARGE_OUTPUT is set because
m5out can be large.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import (
    ContextSatisfiableRule,
    FlowStep,
    InputRule,
    StepContext,
)
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


class CollectResultsParams(BaseModel):
    """Parameters for the gem5_collect_results step."""

    m5out_path: str | None = Field(
        None,
        description="Path to the m5out directory. Auto-resolved from upstream gem5_run_simulation.",
    )
    container: str | None = Field(
        None,
        description="Docker container the m5out lives in. Auto-resolved from upstream if gem5 ran in a container.",
    )
    docker: str | None = Field(
        None, description="Path to the docker binary (auto-detected when omitted).",
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("gem5_collect_results")
class CollectResultsStep(FlowStep):
    """Package gem5 m5out results and upload them to the server."""

    PARAMS_SCHEMA = CollectResultsParams
    OUTPUT_KEYS = ["results_size_bytes", "results_url"]
    DESCRIPTION = "Collect gem5 m5out results and store them as a downloadable artifact."

    SUPPORTED_OS = ["macos", "linux"]
    LARGE_OUTPUT = True

    # ── Validation ──

    @classmethod
    def input_rules(cls) -> list[InputRule]:
        return [
            ContextSatisfiableRule(
                "m5out_path",
                context_key="m5out_path",
                description="Resolved from upstream gem5_run_simulation if omitted.",
            ),
        ]

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = CollectResultsParams(**resolved)

        m5out = validated.m5out_path
        if not m5out:
            return {"error": "m5out_path not provided and not resolvable from context"}

        tar_path = tempfile.NamedTemporaryFile(
            prefix="nexus_m5out_", suffix=".tar.gz", delete=False,
        ).name

        try:
            if validated.container:
                # Tar INSIDE the container (m5out is a container path), stream the
                # archive to the host file, so we never touch the path on the host.
                docker = _find_docker(validated.docker)
                if not docker:
                    return {"error": "docker binary not found on node"}
                parent = os.path.dirname(m5out.rstrip("/")) or "/"
                base = os.path.basename(m5out.rstrip("/"))
                with open(tar_path, "wb") as out:
                    proc = subprocess.run(
                        [docker, "exec", validated.container, "tar", "-czf", "-",
                         "-C", parent, base],
                        stdout=out, stderr=subprocess.PIPE, timeout=600,
                    )
                if proc.returncode != 0:
                    return {"error": f"tar in container failed: {proc.stderr.decode()[:300]}"}
            else:
                if not os.path.isdir(m5out):
                    return {"error": f"m5out directory not found on host: {m5out}"}
                import tarfile
                with tarfile.open(tar_path, "w:gz") as tar:
                    tar.add(m5out, arcname=os.path.basename(m5out))

            size = os.path.getsize(tar_path)

            # Upload to the server (PUT /api/jobs/{job_id}/results), authed by node key.
            if not (ctx.server_url and ctx.job_id and ctx.node_api_key):
                return {"error": "missing server callback info (server_url/job_id/node_api_key)"}
            import httpx
            url = f"{ctx.server_url}/api/jobs/{ctx.job_id}/results"
            with open(tar_path, "rb") as fh:
                r = httpx.put(
                    url,
                    headers={"X-Node-Key": ctx.node_api_key},
                    files={"file": ("results.tar.gz", fh, "application/gzip")},
                    timeout=600,
                )
            if r.status_code >= 300:
                return {"error": f"upload failed: HTTP {r.status_code} {r.text[:200]}"}

            return {
                "results_size_bytes": size,
                "results_url": f"{ctx.server_url}/api/jobs/{ctx.job_id}/results/download",
                "done": True,
                "_command_str": f"collect+upload m5out ({size} bytes) -> {url}",
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                os.unlink(tar_path)
            except OSError:
                pass

    def check(self, state: dict[str, Any]) -> StepResult:
        if "error" in state:
            return StepResult.FAILED
        return StepResult.SUCCESS if state.get("done") else StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        # Collection is synchronous in startup(); nothing to cancel.
        pass
