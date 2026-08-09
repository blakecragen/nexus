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
    timeout: int = Field(
        600, description="Max time in seconds for image pull / container start.",
        ge=1, le=86400,
    )


# ── Step ─────────────────────────────────────────────────────────────────


def _find_docker(explicit: str | None) -> str | None:
    """Locate the ``docker`` CLI on this node.

    Args:
        explicit: A caller-supplied path; returned as-is without an existence
            check, so an explicit-but-wrong path surfaces later as a
            ``FileNotFoundError`` from ``subprocess.run`` rather than a clean
            "not found" error here.

    Returns:
        The path to a docker binary, or ``None`` if nothing was found.

    AI Note: the PATH lookup alone is not enough. Agents are commonly launched
    as a LaunchAgent/systemd unit with a minimal PATH that omits
    ``/usr/local/bin`` and ``/opt/homebrew/bin``, so Docker Desktop's shim is
    invisible even though an interactive shell finds it. The hardcoded
    candidate list is that workaround — the last entry is Docker Desktop's
    in-bundle binary on macOS.

    AI Note: this helper is duplicated verbatim in
    ``nexus_steps.gem5.run_simulation`` and ``nexus_steps.gem5.collect_results``.
    Keep the three copies in sync, or hoist one shared implementation.
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
        """Attach to, start, or create the named container — synchronously.

        Decision order: ``recreate`` → running (attach) → exists-but-stopped
        (start) → absent (run). A final ``docker ps`` confirms the container is
        actually up before reporting success.

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
            return {"error": "docker binary not found on node", "exit_code": -1}

        name = validated.name

        def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
            """Run a docker subcommand, capturing output.

            Args:
                *args: Arguments appended after the docker binary (argv list —
                    no shell, so nothing is word-split).
                timeout: Per-call seconds budget. The 60 s default suits the
                    quick query commands; slow operations (``rm -f``, ``start``,
                    ``run``) pass a larger value explicitly.

            Returns:
                The ``CompletedProcess``; the return code is checked by callers
                rather than raised (no ``check=True``).

            Raises:
                subprocess.TimeoutExpired: propagates to ``startup()``'s handler,
                    which converts it into an error state.
            """
            return subprocess.run(
                [docker, *args], capture_output=True, text=True, timeout=timeout,
            )

        # Human-readable trail of what was decided, surfaced as "_log" in the
        # returned state for debugging.
        log: list[str] = []
        created = False
        try:
            # Is a container with this name running / present?
            #
            # AI Note: `name=^{name}$` anchors the filter — docker's --filter
            # name= is a substring match by default, so without the anchors a
            # container called "gem5_img_old" would satisfy a query for
            # "gem5_img". The `{{.Names}}` format plus the `== name` comparisons
            # below then make the match exact.
            running = _docker(
                "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"
            ).stdout.strip()
            exists = _docker(
                "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"
            ).stdout.strip()

            if validated.recreate and exists == name:
                log.append(f"removing existing container '{name}' (recreate=true)")
                # AI Note: `rm -f` result is intentionally not checked — if the
                # removal fails, the `docker run` below fails loudly with a
                # name-conflict error, which is a better message anyway. Any
                # state inside the old container is destroyed here.
                _docker("rm", "-f", name, timeout=120)
                exists = ""
                running = ""

            if running == name:
                log.append(f"container '{name}' already running — attaching")
                # AI Note: the attach path does NOT verify that the running
                # container's image, mounts or workdir match the requested ones.
                # A container left over from an earlier job with different
                # mounts is silently reused; use recreate=true when the mount
                # set changes, or the gem5 paths will not resolve.
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
                    # AI Note: bare "HOST" is expanded to "HOST:HOST" so the
                    # path is identical inside and outside the container. The
                    # whole container-mode gem5 design depends on that: the
                    # binary path, config path and m5out path are computed once
                    # and used on both sides without translation.
                    spec = m if ":" in m else f"{m}:{m}"  # same path in-container
                    args += ["-v", spec]
                if validated.workdir:
                    args += ["-w", validated.workdir]
                # Keep-alive so the container stays up for later exec steps.
                #
                # AI Note: the image's own entrypoint/CMD is replaced by
                # `sleep infinity`. Without it a gem5 image would run its
                # default command and exit immediately, leaving nothing to
                # `docker exec` into. This also means image entrypoint setup
                # logic never runs.
                args += [validated.image, "sleep", "infinity"]
                log.append(f"creating container '{name}' from {validated.image}")
                # `timeout` covers the image pull too, which is why the default
                # is 600 s rather than the 60 s used for queries.
                r = _docker(*args, timeout=validated.timeout)
                if r.returncode != 0:
                    return {"error": f"docker run failed: {r.stderr.strip()}",
                            "exit_code": r.returncode, "_log": "\n".join(log)}
                created = True

            # Confirm it's up now.
            #
            # AI Note: this re-query is not redundant. `docker run -d` succeeds
            # as soon as the container is created, so a container whose
            # keep-alive command dies instantly (bad image, wrong architecture)
            # would otherwise be reported as ready and every later `docker exec`
            # would fail with a confusing error.
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
                # Display-only: StepExecutor._capture() reads _command_str for
                # the per-job terminal log. It is NOT the command that ran.
                "_command_str": f"docker ensure-container {name} ({validated.image})",
                "_log": "\n".join(log),
            }
        except subprocess.TimeoutExpired:
            return {"error": "docker command timed out", "exit_code": -1, "_log": "\n".join(log)}
        except Exception as exc:  # noqa: BLE001
            # AI Note: the broad catch is deliberate. Docker failures are wildly
            # varied (daemon not running, socket permission denied, DNS failure
            # during pull); converting them all into a clean error state gives
            # the operator a readable job log instead of an agent stack trace.
            return {"error": f"{type(exc).__name__}: {exc}", "exit_code": -1, "_log": "\n".join(log)}

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
