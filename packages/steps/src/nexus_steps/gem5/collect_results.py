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

Where this fits
---------------
Registered as ``"gem5_collect_results"``. It is the terminal step of a gem5
pipeline and the only step in this package that talks back to the server over
HTTP rather than over the agent's WebSocket. The server endpoint it targets
backs the Job Detail "Download" button and the results file-tree view.

The callback contract
---------------------
``StepContext.job_id``, ``server_url`` and ``node_api_key`` are populated by the
agent's :class:`~nexus_agent.executor.StepExecutor` (which derives the HTTP base
by rewriting its ``ws://``/``wss://`` server URL). If the agent were ever to stop
setting them, this step fails cleanly with "missing server callback info" rather
than uploading nowhere.

AI Note: the module docstring above says container mode uses ``docker cp``. The
implementation actually streams ``tar -czf -`` over the exec's stdout, which is
equivalent in effect; treat the code as authoritative.
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
    """Locate the ``docker`` CLI on this node.

    Args:
        explicit: A caller-supplied path, returned as-is without validation.

    Returns:
        Path to a docker binary, or ``None`` if none was found.

    AI Note: the PATH lookup alone is insufficient because agents typically run
    as a LaunchAgent/systemd unit with a minimal PATH that omits
    ``/usr/local/bin`` and ``/opt/homebrew/bin``; the hardcoded candidates are
    the workaround.

    AI Note: duplicated verbatim in ``nexus_steps.docker.ensure_container`` and
    ``nexus_steps.gem5.run_simulation``. Keep all three in sync.
    """
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
    """Parameters for the gem5_collect_results step.

    Every field is optional because the intended usage is to supply nothing at
    all and let ``ctx.resolve()`` pull ``m5out_path``, ``container`` and
    ``docker`` from the upstream ``gem5_run_simulation`` outputs. The
    ``description`` text is user-facing via ``to_schema()`` → ``/api/steps``.
    """

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
    """Package gem5 m5out results and upload them to the server.

    Fully synchronous: tar + HTTP upload both complete inside ``startup()``, so
    a multi-gigabyte m5out blocks the agent's step coroutine for the duration
    (bounded by the 600 s tar and 600 s upload timeouts).
    """

    PARAMS_SCHEMA = CollectResultsParams
    OUTPUT_KEYS = ["results_size_bytes", "results_url"]
    DESCRIPTION = "Collect gem5 m5out results and store them as a downloadable artifact."

    SUPPORTED_OS = ["macos", "linux"]
    # Hint to the storage manager: prefer high-capacity backends.
    LARGE_OUTPUT = True

    # ── Validation ──

    @classmethod
    def input_rules(cls) -> list[InputRule]:
        """Make ``m5out_path`` required only when no upstream step supplies it.

        Returns:
            A single ``ContextSatisfiableRule``: submit-time validation passes
            if the user provided ``m5out_path`` explicitly *or* if some earlier
            step in the job declares ``m5out_path`` among its ``OUTPUT_KEYS``.

        AI Note: this rule is why a two-step "run gem5 then collect" job
        validates with an empty params block for the collect step. The server
        accumulates upstream ``OUTPUT_KEYS`` into the validation context at
        submit time specifically so this works — see the submit-time validation
        logic in the jobs route.

        AI Note: overriding ``input_rules`` replaces the base implementation
        entirely, so ``container`` and ``docker`` get no rules. That is fine
        (both are optional), but a future required field here would not be
        caught by the rule pass.
        """
        return [
            ContextSatisfiableRule(
                "m5out_path",
                context_key="m5out_path",
                description="Resolved from upstream gem5_run_simulation if omitted.",
            ),
        ]

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Tar the m5out directory and PUT it to the server's results endpoint.

        Args:
            params: Raw step params; usually empty, with everything resolved
                from context.
            ctx: Job context. Besides ``outputs`` this reads the agent-populated
                callback fields ``server_url``, ``job_id`` and ``node_api_key``.

        Returns:
            On success ``{"results_size_bytes", "results_url", "done": True,
            "_command_str"}``; on any failure a dict with a single ``error``
            key.

        Side effects:
            Creates a temp ``.tar.gz`` on the host (always removed in the
            ``finally`` block), may run ``docker exec ... tar`` inside the
            container, and performs an authenticated HTTP PUT that persists the
            archive server-side against this job.

        AI Note: every failure is reported by *returning* an ``error`` state
        rather than raising, including the catch-all ``except Exception``. This
        keeps a failed upload out of the agent's exception path and gives the
        operator a readable message in the job log.
        """
        resolved = ctx.resolve(params)
        validated = CollectResultsParams(**resolved)

        m5out = validated.m5out_path
        if not m5out:
            return {"error": "m5out_path not provided and not resolvable from context"}

        # AI Note: only the NAME is taken from NamedTemporaryFile; the handle is
        # dropped immediately and each branch below reopens the path itself.
        # delete=False is implied by that usage pattern being safe — the file is
        # unlinked explicitly in the finally block, which is the single cleanup
        # point for both success and failure.
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
                # AI Note: `tar -C <parent> <base>` rather than tarring the
                # absolute path keeps the archive rooted at the m5out directory
                # name, so extraction produces ./m5out_nexus_xxxx/ instead of a
                # deep absolute-path tree. The direct-mode branch achieves the
                # same shape via arcname=basename.
                parent = os.path.dirname(m5out.rstrip("/")) or "/"
                base = os.path.basename(m5out.rstrip("/"))
                with open(tar_path, "wb") as out:
                    # AI Note: `-czf -` writes the archive to the exec's stdout,
                    # which is redirected straight into the host file — the
                    # archive never lands inside the container, so a container
                    # with little free space still works. No text=True here:
                    # stdout is binary, and stderr must therefore be .decode()d
                    # manually below.
                    proc = subprocess.run(
                        [docker, "exec", validated.container, "tar", "-czf", "-",
                         "-C", parent, base],
                        stdout=out, stderr=subprocess.PIPE, timeout=600,
                    )
                if proc.returncode != 0:
                    # stderr truncated to keep the job log readable.
                    return {"error": f"tar in container failed: {proc.stderr.decode()[:300]}"}
            else:
                if not os.path.isdir(m5out):
                    return {"error": f"m5out directory not found on host: {m5out}"}
                # AI Note: local imports (tarfile here, httpx below) keep the
                # module import cheap — nexus_steps imports EVERY step module at
                # agent startup, so per-step heavyweight imports are deferred to
                # the point of use.
                import tarfile
                with tarfile.open(tar_path, "w:gz") as tar:
                    # arcname=basename → archive contains m5out_xxx/... rather
                    # than the full absolute path.
                    tar.add(m5out, arcname=os.path.basename(m5out))

            size = os.path.getsize(tar_path)

            # Upload to the server (PUT /api/jobs/{job_id}/results), authed by node key.
            #
            # AI Note: these three fields come from StepContext, populated by
            # the agent's executor (server_url is derived by rewriting the
            # ws:// WebSocket URL to http://). They are absent when a step is
            # run outside a real agent — e.g. in unit tests — so the guard is
            # what keeps that from turning into a confusing request to "None".
            if not (ctx.server_url and ctx.job_id and ctx.node_api_key):
                return {"error": "missing server callback info (server_url/job_id/node_api_key)"}
            import httpx
            url = f"{ctx.server_url}/api/jobs/{ctx.job_id}/results"
            with open(tar_path, "rb") as fh:
                # AI Note: authentication is the node's own API key in the
                # X-Node-Key header — NOT a user session. The server-side
                # endpoint must therefore verify that this node is actually the
                # one assigned to job_id, otherwise any node could overwrite
                # another job's results.
                #
                # AI Note: httpx.put with a file object streams via multipart;
                # the 600 s timeout is the total budget for a potentially
                # multi-GB upload and is not configurable via params.
                r = httpx.put(
                    url,
                    headers={"X-Node-Key": ctx.node_api_key},
                    files={"file": ("results.tar.gz", fh, "application/gzip")},
                    timeout=600,
                )
            # Any 3xx/4xx/5xx is a failure — a redirect would mean the upload
            # body was not accepted at this URL.
            if r.status_code >= 300:
                return {"error": f"upload failed: HTTP {r.status_code} {r.text[:200]}"}

            return {
                "results_size_bytes": size,
                "results_url": f"{ctx.server_url}/api/jobs/{ctx.job_id}/results/download",
                "done": True,
                # Display-only; read by StepExecutor._capture() for the job log.
                "_command_str": f"collect+upload m5out ({size} bytes) -> {url}",
            }
        except Exception as exc:  # noqa: BLE001
            # Broad by design: tar failures, network errors and disk-full all
            # become a clean FAILED step with a readable message instead of an
            # agent stack trace.
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            # AI Note: the archive is deleted on EVERY path, including success.
            # The server already holds the uploaded copy, so keeping the local
            # one would just fill the node's temp dir after a few large runs.
            # The m5out directory itself is deliberately left in place.
            try:
                os.unlink(tar_path)
            except OSError:
                pass

    def check(self, state: dict[str, Any]) -> StepResult:
        """Report the result already computed by ``startup()``. Idempotent.

        Args:
            state: The ``startup()`` state dict.

        Returns:
            ``FAILED`` when ``error`` is present, ``SUCCESS`` when ``done`` is
            set, else ``RUNNING``.

        AI Note: the trailing ``RUNNING`` is unreachable today — ``startup()``
        always returns either ``error`` or ``done`` — and exists so a
        partially-restored state keeps polling instead of reporting a bogus
        success.
        """
        if "error" in state:
            return StepResult.FAILED
        return StepResult.SUCCESS if state.get("done") else StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        """No-op — tar and upload already completed inside ``startup()``.

        Args:
            state: Unused; accepted to satisfy the FlowStep interface.

        AI Note: a cancel arriving mid-upload cannot abort the in-flight httpx
        request from here; only the executor's task cancellation tears it down,
        and the ``finally`` block still removes the temp archive.
        """
        # Collection is synchronous in startup(); nothing to cancel.
        pass
