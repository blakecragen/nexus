"""Ensure a named Docker container exists and is running on the node.

Idempotent "create-or-attach": if a container with the given name is already
running, it's reused; if it exists but is stopped, it's started; otherwise it's
created from the image with a keep-alive command (so it stays up for later
`docker exec` steps, e.g. running gem5 inside a Linux container on macOS).

Mounts default to binding a host directory at the SAME path inside the
container, so absolute paths (gem5 binary, configs, m5out) are valid in both —
and files the container writes land back on the host for collection.

Where this fits
---------------
Registered as ``"docker_ensure_container"``. It is the prerequisite step for
container-mode gem5: it publishes ``container`` and ``docker`` into the job
context, and ``gem5_run_simulation`` / ``gem5_collect_results`` pick both up
automatically through ``ctx.resolve()``. Restricted to ``SUPPORTED_OS =
["macos", "linux"]``.

Lifetime semantics
------------------
The container is deliberately **not** owned by the job: it is created detached
with ``sleep infinity`` and is never removed by this step. It outlives the job
so subsequent jobs attach to it instead of paying image-pull and start costs
again. Cleanup is an operator responsibility (or a ``recreate=true`` run).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register
from nexus_steps.docker.util import ensure_container as _ensure_container
from nexus_steps.docker.util import docker_missing_error, ensure_daemon, find_docker


# ── Params ───────────────────────────────────────────────────────────────


class EnsureContainerParams(BaseModel):
    """Parameters for the docker_ensure_container step.

    All defaults are gem5-flavoured (name ``gem5_img``, the official gem5
    all-dependencies image) because that is the only shipped consumer; the step
    itself is generic. The ``description``/``examples`` text is user-facing via
    ``to_schema()`` → ``/api/steps``.
    """

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
    auto_start_daemon: bool = Field(
        True,
        description=(
            "If the Docker daemon isn't running, try to start it (Docker Desktop "
            "on macOS, the docker service on Linux) and wait for it to be ready."
        ),
    )
    daemon_wait: int = Field(
        120,
        description="Seconds to wait for the Docker daemon to become ready.",
        ge=1,
        le=3600,
    )
    timeout: int = Field(
        600, description="Max time in seconds for image pull / container start.",
        ge=1, le=86400,
    )


# ── Step ─────────────────────────────────────────────────────────────────


# Docker discovery now lives in ``nexus_steps.docker.util`` (one copy, shared
# with the two gem5 steps that used to carry verbatim duplicates).
#
# AI Note: this module-level alias is not merely cosmetic — the unit tests
# monkeypatch ``ensure_container._find_docker``, and ``startup()`` below must
# keep calling it through this name for those patches to bind.
_find_docker = find_docker


@register("docker_ensure_container")
class EnsureContainerStep(FlowStep):
    """Create-or-attach a named Docker container, ready for `docker exec`.

    Security note: bind mounts are applied verbatim, so a job can mount any
    host path (including ``/``) into a container it then executes code in.
    Authorisation is enforced at job submission, not here.
    """

    PARAMS_SCHEMA = EnsureContainerParams
    OUTPUT_KEYS = ["container", "docker", "created", "exit_code"]
    DESCRIPTION = "Ensure a named Docker container exists and is running."

    # No Windows: the container paths-match-host trick and the docker discovery
    # paths below are POSIX-only.
    SUPPORTED_OS = ["macos", "linux"]

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Ensure the daemon is up, then attach/start/create the container.

        The container decision order — ``recreate`` → running (attach) →
        exists-but-stopped (start) → absent (run), with a final ``docker ps``
        confirmation — lives in :func:`nexus_steps.docker.util.ensure_container`
        so ``gem5_run_simulation`` applies exactly the same rules.

        Args:
            params: Raw step params.
            ctx: Job context; ``ctx.resolve()`` layers upstream step outputs
                beneath these params.

        Returns:
            On success, a state dict with ``container``, ``docker`` (resolved
            binary path, forwarded so later gem5 steps skip re-discovery),
            ``created`` (whether this run created it), ``exit_code = 0``, plus
            the display-only ``_command_str`` and ``_log``. On failure, a dict
            with ``error`` and a non-zero/negative ``exit_code``.

        Side effects:
            May pull a multi-GB image, create/start/remove a Docker container,
            and bind-mount host paths into it. The container is left running
            after the job finishes (see module docstring).

        AI Note: every failure path returns a state carrying ``exit_code``.
        That is load-bearing — ``check()`` decides purely on ``exit_code == 0``
        and does not look for an ``error`` key, so a new early return that
        forgets ``exit_code`` would be reported as FAILED via ``.get()``
        returning ``None``... but any future return of ``exit_code: 0`` beside
        an ``error`` would be reported as SUCCESS.
        """
        resolved = ctx.resolve(params)
        validated = EnsureContainerParams(**resolved)

        docker = _find_docker(validated.docker)
        if not docker:
            return {"error": docker_missing_error(), "exit_code": -1}

        # Human-readable trail of what was decided, surfaced as "_log" in the
        # returned state for debugging.
        log: list[str] = []

        # A live daemon is a precondition for every docker call that follows.
        #
        # AI Note: finding the binary is NOT evidence the daemon is up. Docker
        # Desktop installs the CLI shim permanently, so `_find_docker` happily
        # returns a path while `docker ps` fails with "Cannot connect to the
        # Docker daemon". That gap is exactly what used to surface downstream as
        # an unreadable m5out error in gem5_run_simulation.
        daemon_err = ensure_daemon(
            docker, validated.auto_start_daemon, validated.daemon_wait, log,
        )
        if daemon_err:
            return {"error": daemon_err, "exit_code": -1, "_log": "\n".join(log)}

        result = _ensure_container(
            docker,
            name=validated.name,
            image=validated.image,
            mounts=validated.mounts,
            workdir=validated.workdir,
            recreate=validated.recreate,
            timeout=validated.timeout,
            log=log,
        )
        if result.error:
            return {
                "error": result.error,
                "exit_code": result.exit_code,
                "_log": "\n".join(log),
            }

        return {
            "container": validated.name,
            "docker": docker,
            "created": result.created,
            "exit_code": 0,
            # Display-only: StepExecutor._capture() reads _command_str for
            # the per-job terminal log. It is NOT the command that ran.
            "_command_str": (
                f"docker ensure-container {validated.name} ({validated.image})"
            ),
            "_log": "\n".join(log),
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        """Report the outcome recorded by ``startup()``. Pure and idempotent.

        Args:
            state: The ``startup()`` state dict.

        Returns:
            ``SUCCESS`` iff ``state["exit_code"] == 0``, else ``FAILED``. Never
            returns ``RUNNING`` — the work is already complete by this point.

        AI Note: the decision is made on ``exit_code`` alone, not on the
        presence of an ``error`` key (unlike the git/package steps). Every
        return path in ``startup()`` must therefore set ``exit_code``.
        """
        # startup() does the work synchronously; this is a one-shot result.
        return StepResult.SUCCESS if state.get("exit_code") == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        """No-op — the container work completed synchronously in ``startup()``.

        Args:
            state: Unused; accepted to satisfy the FlowStep interface.

        AI Note: cancelling does not stop or remove the container. That is
        intentional — the container is shared infrastructure that may be in use
        by other jobs on this node.
        """
        # Nothing long-running to cancel.
        return None
