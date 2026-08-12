"""Run a gem5 simulation on a compute node.

Runs gem5 either directly on the node or, when ``container`` is set, inside a
Docker container via ``docker exec`` (the standard way to run a Linux gem5 build
on macOS). For the container path, ``working_dir`` must be a host path that is
bind-mounted at the SAME path inside the container (see docker_ensure_container)
so the gem5 binary, config, and the m5out stats directory resolve identically on
host and container — and the produced stats.txt lands back on the host.

stdout/stderr and the m5out stats directory are captured; ``LARGE_OUTPUT = True``
signals the storage manager to prefer high-capacity backends for the artifacts.

Where this fits
---------------
Registered as ``"gem5_run_simulation"``. Sits between
``docker_ensure_container`` (which publishes ``container`` + ``docker`` into the
job context, both of which this step picks up automatically via
``ctx.resolve()``) and ``gem5_collect_results`` (which consumes the
``m5out_path`` and ``container`` this step publishes).

``docker_ensure_container`` is **optional** as of the ``auto_start`` param: in
container mode this step ensures the Docker daemon is running and the container
is up (starting or creating it as needed) before its first ``docker exec``. A
job whose only step is ``gem5_run_simulation`` therefore works. Keep the
explicit ensure step when you need a non-default image, a specific mount set, or
``recreate`` semantics — the auto path deliberately only covers the common case.

The two execution modes
-----------------------
Direct mode: gem5 runs as a normal child of the agent; ``m5out`` is a host
tempdir; ``cwd`` is a host path.

Container mode: the agent's child is the **docker client**, not gem5. Every
gem5-visible path (binary, config script, working_dir, m5out) is a
*container* path, so the host process must never stat, mkdir or chdir into
them — that is why ``host_cwd`` is ``None`` and the m5out directory is created
with ``docker exec ... mkdir -p`` rather than ``tempfile.mkdtemp``. This
distinction is the single largest source of bugs in this file; preserve it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import uuid
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register
from nexus_steps.docker.util import docker_missing_error, ensure_container, ensure_daemon, find_docker

# Docker discovery is shared with ``docker_ensure_container`` and
# ``gem5_collect_results`` (see ``nexus_steps.docker.util``).
#
# AI Note: keep calling the module-level alias rather than ``find_docker``
# directly — the unit tests monkeypatch this name on this module.
_find_docker = find_docker


# ── Params ───────────────────────────────────────────────────────────────


class RunSimulationParams(BaseModel):
    """Parameters for the gem5_run_simulation step.

    ``container`` and ``docker`` are normally left unset and resolved from the
    upstream ``docker_ensure_container`` step's outputs. The
    ``description``/``examples`` text is user-facing via ``to_schema()`` →
    ``/api/steps``.
    """

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
    auto_start: bool = Field(
        True,
        description=(
            "With `container`: start the Docker daemon if it isn't running, and "
            "start (or create) the container if it isn't up, instead of failing."
        ),
    )
    image: str = Field(
        "ghcr.io/gem5/ubuntu-24.04_all-dependencies:latest",
        description=(
            "Image used only if `auto_start` has to CREATE a missing container. "
            "Ignored when the container already exists."
        ),
    )
    mounts: list[str] = Field(
        default_factory=list,
        description=(
            "Bind mounts used only when creating a missing container: "
            "'HOST:CONTAINER' or bare 'HOST' (same path inside). Defaults to "
            "mounting `working_dir` at the same path, which is what makes the "
            "gem5 binary, config and m5out paths resolve inside the container."
        ),
        examples=[["/Users/me/Desktop/gem5"]],
    )
    daemon_wait: int = Field(
        120,
        description="Seconds to wait for the Docker daemon to become ready.",
        ge=1,
        le=3600,
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
    """Run a gem5 architectural simulation.

    Simulations routinely run for hours; the step is asynchronous (detached
    child + polled ``check()``) so the agent stays responsive throughout.
    """

    PARAMS_SCHEMA = RunSimulationParams
    # AI Note: ``m5out_path`` and ``container`` are exported specifically so a
    # chained ``gem5_collect_results`` can resolve them from context without the
    # user repeating them. Removing either breaks that auto-wiring.
    OUTPUT_KEYS = ["exit_code", "stats_artifact_id", "m5out_path", "container"]
    DESCRIPTION = "Run a gem5 simulation and collect results."

    SUPPORTED_OS = ["macos", "linux"]
    # Hint to the storage manager: prefer high-capacity backends for artifacts.
    LARGE_OUTPUT = True

    # AI Note: the macOS default points at an ARM gem5 build, which in practice
    # only exists on a node with a native macOS gem5. Container mode is the
    # usual macOS route, and there this path is interpreted INSIDE the container
    # — so the OS variant is frequently overridden in the job definition.
    OS_VARIANTS = {
        "macos": {"gem5_binary": "/opt/gem5/build/ARM/gem5.opt"},
        "linux": {"gem5_binary": "/usr/local/bin/gem5.opt"},
    }

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Build the gem5 command for the selected mode and launch it detached.

        Args:
            params: Raw step params (already OS-resolved by the executor).
            ctx: Job context; ``ctx.resolve()`` supplies ``container``/``docker``
                published by an upstream ``docker_ensure_container``.

        Returns:
            A JSON-serialisable state dict: ``pid`` (of gem5, or of the docker
            client in container mode), ``m5out_path``, ``container``,
            ``docker``, ``stdout_path``, ``stderr_path``, ``timeout``,
            ``collect_stats``, a ``stats_artifact_id`` placeholder filled in by
            ``check()``, and the display-only ``_command_str``. On a
            container-setup failure, an ``error``/``exit_code`` dict instead.

        Side effects:
            Creates the m5out directory (on the host, or inside the container),
            creates two undeleted temp log files, and forks a long-running
            process in a new session.

        Raises:
            pydantic.ValidationError: if ``config_script`` is missing.

        AI Note: ``timeout`` is recorded in the state but nothing in this module
        enforces it — neither ``startup()`` nor ``check()`` compares elapsed
        time against it. A runaway simulation runs until the job is cancelled.
        """
        resolved = ctx.resolve(params)
        validated = RunSimulationParams(**resolved)

        # Last-resort default for when OS_VARIANTS did not apply (unknown OS
        # string, or a direct unit-test call bypassing resolve_for_os()).
        binary = validated.gem5_binary or "/usr/local/bin/gem5.opt"

        # Trail of any daemon/container preparation, surfaced as "_log" so the
        # operator can see whether Docker or the container had to be started.
        # Stays empty in direct mode.
        setup_log: list[str] = []

        if validated.container:
            # Run inside a Docker container. All gem5 paths (binary, config,
            # working_dir, m5out) are CONTAINER paths — they may not exist on the
            # host, so the host process must not touch them (no mkdtemp(dir=cwd),
            # no cwd=). `docker exec -w` sets the working dir inside the container.
            docker = _find_docker(validated.docker)
            if not docker:
                return {"error": docker_missing_error(), "exit_code": -1}
            if not validated.working_dir:
                # Required because the container m5out path is derived from it;
                # os.getcwd() would be a host path and meaningless in-container.
                return {"error": "working_dir is required when using `container`", "exit_code": -1}
            cwd = validated.working_dir

            # Make the environment usable before touching it. Ordering is
            # load-bearing: the daemon must answer before any `docker ps`, and
            # the container must be up before the `docker exec` below.
            #
            # AI Note: the daemon probe runs even when auto_start is False. It
            # costs one `docker info` and converts the raw
            # "Cannot connect to the Docker daemon at unix://...docker.sock"
            # socket error — which used to reach the operator disguised as a
            # "could not create m5out in container" failure — into a message
            # that names the actual problem.
            daemon_err = ensure_daemon(
                docker, validated.auto_start, validated.daemon_wait, setup_log,
            )
            if daemon_err:
                return {"error": daemon_err, "exit_code": -1,
                        "_log": "\n".join(setup_log)}

            if validated.auto_start:
                # AI Note: a missing container is created with `cwd` bind-mounted
                # at the SAME path inside. An unmounted gem5 container cannot
                # resolve the binary, config script or m5out paths, so creating
                # one without mounts would only relocate the failure to a more
                # confusing place. Callers override via the `mounts` param.
                result = ensure_container(
                    docker,
                    name=validated.container,
                    image=validated.image,
                    mounts=validated.mounts or [cwd],
                    workdir=cwd,
                    log=setup_log,
                )
                if result.error:
                    return {"error": result.error, "exit_code": result.exit_code,
                            "_log": "\n".join(setup_log)}

            # A unique m5out dir, relative to working_dir, created INSIDE the
            # container. Random suffix without host filesystem access.
            #
            # AI Note: uuid4 is used instead of tempfile.mkdtemp precisely
            # because mkdtemp would create the directory on the HOST filesystem
            # at a path that may not exist in the container. The 8-hex-char
            # truncation is ample here — collisions only matter within one
            # working_dir on one container.
            m5out_dir = f"{cwd.rstrip('/')}/m5out_nexus_{uuid.uuid4().hex[:8]}"
            mk = subprocess.run(
                [docker, "exec", validated.container, "mkdir", "-p", m5out_dir],
                capture_output=True, text=True, timeout=30,
            )
            if mk.returncode != 0:
                return {"error": f"could not create m5out in container: {mk.stderr.strip()}",
                        "exit_code": mk.returncode}
            inner = [binary, f"--outdir={m5out_dir}", validated.config_script, *validated.script_args]
            # AI Note: no `-t`/`-i` on the exec — the child must be fully
            # non-interactive with its stdio redirected to the temp log files.
            cmd = [docker, "exec", "-w", cwd, validated.container, *inner]
            host_cwd = None  # docker client runs from anywhere
        else:
            # Run gem5 directly on the node.
            cwd = validated.working_dir or os.getcwd()
            # Host tempdir is correct here: gem5 itself runs on the host.
            m5out_dir = tempfile.mkdtemp(prefix="nexus_m5out_")
            cmd = [
                binary,
                f"--outdir={m5out_dir}",
                validated.config_script,
                *validated.script_args,
            ]
            host_cwd = cwd

        # delete=False: these must survive startup() so the executor can read
        # them back after the simulation exits.
        stdout_file = tempfile.NamedTemporaryFile(
            prefix="nexus_gem5_out_", suffix=".log", delete=False,
        )
        stderr_file = tempfile.NamedTemporaryFile(
            prefix="nexus_gem5_err_", suffix=".log", delete=False,
        )

        # start_new_session=True: own process group so cancel()'s killpg()
        # reaches the whole tree. AI Note: in container mode that tree contains
        # only the docker client — see the caveat on cancel().
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
            # AI Note: re-resolving docker here (rather than reusing the local
            # `docker` variable) keeps this expression valid on the direct-mode
            # branch where that variable was never bound. The value is
            # persisted so check() and the downstream collect step do not have
            # to re-run discovery.
            "docker": _find_docker(validated.docker) if validated.container else None,
            "stdout_path": stdout_file.name,
            "stderr_path": stderr_file.name,
            "timeout": validated.timeout,
            "collect_stats": validated.collect_stats,
            "stats_artifact_id": None,
            # Display-only; read by StepExecutor._capture() for the job log.
            "_command_str": " ".join(cmd),
            "_log": "\n".join(setup_log),
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        """Reap the simulation, verify stats.txt was produced, report status.

        Args:
            state: The ``startup()`` state dict; mutated in place to record
                ``exit_code`` and (when found) ``stats_artifact_id``.

        Returns:
            ``RUNNING`` while gem5 is alive, ``SUCCESS`` on exit status 0,
            ``FAILED`` on non-zero status or signal death.

        Side effects:
            In container mode, runs ``docker exec ... test -f`` to probe for the
            stats file.

        AI Note: the ``error`` branch must stay first — an error state (from a
        container-setup failure in ``startup()``) has no ``pid`` and
        ``state["pid"]`` would raise ``KeyError``.

        AI Note: the stats probe is container-aware. A host ``os.path.isfile``
        on a container path would report False even when the file exists (and
        vice versa if a same-named host path happens to exist), so the
        ``container`` branch must be kept.

        AI Note: a missing stats.txt does NOT fail the step — only gem5's exit
        code decides. ``stats_artifact_id`` simply stays ``None``, and the
        downstream collect step is what will complain.
        """
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

        # WEXITSTATUS is only valid when WIFEXITED; a signal-killed simulation
        # (SIGTERM from cancel(), OOM kill) reports -1 → FAILED.
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
                #
                # AI Note: despite the name, this holds a filesystem PATH, not a
                # storage-backend ID. Consumers that treat it as an opaque
                # artifact handle will break once real uploads land. Actual
                # artifact upload currently happens in ``gem5_collect_results``.
                state["stats_artifact_id"] = stats_path

        return StepResult.SUCCESS if exit_status == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        """Best-effort graceful termination of the simulation's process group.

        Args:
            state: The ``startup()`` state dict; only ``pid`` is used. Safe on
                an error state (no ``pid`` → no-op).

        Side effects:
            Sends ``SIGTERM`` to the child's process group.

        AI Note: in CONTAINER mode the child is the docker client, not gem5.
        Killing the client detaches the CLI but the ``docker exec`` payload —
        the actual gem5 process — keeps running inside the container, burning
        CPU and continuing to write into m5out. Fully cancelling a containerised
        simulation additionally requires something like
        ``docker exec <c> pkill -f gem5`` or a container restart; that is not
        implemented here.
        """
        pid = state.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
