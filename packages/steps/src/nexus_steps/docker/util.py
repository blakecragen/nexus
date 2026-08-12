"""Shared Docker plumbing for the ``docker`` and ``gem5`` step packages.

Three steps shell out to Docker — ``docker_ensure_container``,
``gem5_run_simulation`` and ``gem5_collect_results`` — and every one of them
needs the same two guarantees before it can run a ``docker exec``:

1. the ``docker`` CLI exists on this node, and
2. the Docker **daemon** is actually accepting connections.

Guarantee (2) used to be assumed rather than checked, which produced the
failure this module exists to fix: a gem5 job died at step 0 with
``could not create m5out in container: Cannot connect to the Docker daemon at
unix:///.../docker.sock``. ``find_docker()`` had succeeded — the binary was
right there — but nothing had asked whether the daemon behind it was alive.

Where this fits
---------------
Pure helpers, no :class:`~nexus_common.steps.base.FlowStep` subclass and no
``@register`` call. It is therefore deliberately absent from ``_STEP_MODULES``
in :mod:`nexus_steps`, which is the discovery list for *step* modules only.

AI Note: this module is the single home for logic that was previously
triplicated. ``find_docker`` in particular existed as three verbatim copies,
each carrying a comment asking the next reader to keep them in sync. The step
modules retain a module-level ``_find_docker`` alias pointing here, both for
backwards compatibility and because the unit tests monkeypatch that name on the
step module rather than on this one.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from typing import NamedTuple

# Probed in order when ``docker`` is not on PATH.
#
# AI Note: the PATH lookup alone is not enough. Agents are commonly launched as
# a LaunchAgent/systemd unit with a minimal PATH that omits ``/usr/local/bin``
# and ``/opt/homebrew/bin``, so Docker Desktop's shim is invisible even though
# an interactive shell finds it. The last entry is Docker Desktop's in-bundle
# binary on macOS.
DOCKER_CANDIDATES = (
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "~/.docker/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
)

# Seconds between ``docker info`` probes while waiting for a daemon to come up.
_POLL_INTERVAL = 2.0


class ContainerResult(NamedTuple):
    """Outcome of :func:`ensure_container`.

    Attributes:
        created: True only when this call issued the ``docker run`` that
            brought the container into existence. False when it attached to a
            running container or started a stopped one.
        error: Human-readable failure description, or ``None`` on success.
        exit_code: 0 on success. Otherwise the failing docker command's return
            code where one exists, ``1`` for the post-ensure liveness check, and
            ``-1`` for timeouts and unexpected exceptions.

    AI Note: ``exit_code`` is carried separately rather than derived from
    ``error`` because ``EnsureContainerStep.check()`` decides purely on
    ``exit_code == 0`` and never inspects the error key.
    """

    created: bool
    error: str | None
    exit_code: int


def find_docker(explicit: str | None) -> str | None:
    """Locate the ``docker`` CLI on this node.

    Args:
        explicit: A caller-supplied path; returned as-is without an existence
            check, so an explicit-but-wrong path surfaces later as a
            ``FileNotFoundError`` from ``subprocess.run`` rather than a clean
            "not found" error here.

    Returns:
        The path to a docker binary, or ``None`` if nothing was found.
    """
    if explicit:
        return explicit
    found = shutil.which("docker")
    if found:
        return found
    for cand in DOCKER_CANDIDATES:
        expanded = os.path.expanduser(cand)
        if os.path.exists(expanded):
            return expanded
    return None


def docker_missing_error() -> str:
    """The error text for "no docker CLI on this node", naming the node.

    Returns:
        A message identifying the host and the paths that were searched.

    AI Note: shared by all three docker-touching steps for the same reason the
    rest of this module exists — the string was previously duplicated verbatim
    per step, so improving one copy silently left the other two behind. Naming
    ``platform.node()`` matters because the step runs remotely: a bare "docker
    not found" sends the operator to check the machine they are sitting at
    rather than the node that actually failed.
    """
    return (
        f"docker CLI not found on node {platform.node()} "
        f"(searched PATH and {', '.join(DOCKER_CANDIDATES)})"
    )


def daemon_ready(docker: str, timeout: int = 15) -> bool:
    """Report whether the Docker daemon is accepting connections.

    Args:
        docker: Path to the docker binary.
        timeout: Seconds to allow the probe itself. A wedged daemon can leave
            ``docker info`` hanging, so this must never be unbounded.

    Returns:
        True iff ``docker info`` exits 0.

    AI Note: ``docker info`` is the probe rather than ``docker ps`` because it
    is the cheapest command that still requires a live daemon connection, and
    its failure message is the one operators recognise. Any exception —
    including the binary not existing — is reported as "not ready" rather than
    raised, so callers can treat this as a pure predicate.
    """
    try:
        return subprocess.run(
            [docker, "info"], capture_output=True, text=True, timeout=timeout,
        ).returncode == 0
    except Exception:  # noqa: BLE001 - a failed probe is simply "not ready"
        return False


def _start_commands(docker: str) -> list[list[str]]:
    """Return the platform's candidate daemon-start commands, in priority order.

    Args:
        docker: Path to the docker binary, used for the Docker Desktop CLI.

    Returns:
        A list of argv lists. Each is attempted in turn until one exits 0.
        Empty on platforms with no known mechanism, which callers surface as an
        actionable error rather than a silent no-op.

    AI Note: on macOS ``docker desktop start`` is tried FIRST and ``open -ga
    Docker`` only as a fallback, because the agent is normally a background
    process launched over SSH or by launchd. ``open`` asks the login session's
    LaunchServices to front a GUI app, which fails outright when the node is
    headless or logged out — the exact situation a remote compute node is
    usually in. The ``docker desktop`` CLI plugin (Docker Desktop 4.37+) talks
    to Docker Desktop's own service instead, so it works in contexts where
    ``open`` cannot. The fallback is retained for older Docker Desktop installs
    that ship no such plugin.

    AI Note: ``open -ga`` keeps ``-g`` so that when it *is* the one that runs,
    Docker Desktop starts without stealing focus — an agent running a
    background job must not yank the operator out of whatever they are doing.

    AI Note: on Linux the plain ``systemctl`` call comes first for the
    root/rootless cases, with ``sudo -n`` as the fallback. ``-n`` is critical:
    without it, sudo prompts for a password on a terminal the agent does not
    have, and the step hangs until its timeout instead of failing usefully.
    """
    system = platform.system()
    if system == "Darwin":
        return [[docker, "desktop", "start"], ["open", "-ga", "Docker"]]
    if system == "Linux":
        return [
            ["systemctl", "start", "docker"],
            ["sudo", "-n", "systemctl", "start", "docker"],
        ]
    return []


def start_daemon(docker: str, wait: int, log: list[str] | None = None) -> str | None:
    """Attempt to start the Docker daemon, then block until it is ready.

    Args:
        docker: Path to the docker binary.
        wait: Total seconds to wait for readiness after issuing the start
            command. Docker Desktop routinely takes 30-60s from cold.
        log: Optional list appended with a human-readable trail, surfaced as
            ``_log`` in the calling step's state.

    Returns:
        ``None`` once the daemon is ready, else an error string.

    Side effects:
        Launches Docker Desktop (macOS) or starts the docker service (Linux),
        and blocks the calling thread for up to ``wait`` seconds.

    AI Note: readiness is polled rather than assumed from the start command's
    exit status. ``open -ga Docker`` returns 0 the instant the app is
    *launched*, long before the daemon socket accepts connections; treating
    that 0 as success would hand the caller a daemon that is still booting and
    reproduce the original bug with extra steps.

    AI Note: every start command failing is NOT treated as fatal on its own —
    the poll still runs. The daemon may be mid-start from another source (a
    login item, a concurrent job's step), in which case waiting succeeds where
    the start command could not.

    AI Note: every returned error names ``platform.node()``. This step runs on
    a remote compute node, so "Docker is not running" is ambiguous and was
    actually misread in practice — an operator confirmed Docker was up on the
    machine they were sitting at while the job failed on a different node.
    Naming the host is what makes the message actionable.
    """
    trail = log if log is not None else []
    node = platform.node()
    commands = _start_commands(docker)
    if not commands:
        return (
            f"Docker daemon is not running on {node} and there is no known way "
            f"to start it on {platform.system()}; start Docker there and re-run"
        )

    started = False
    reasons: list[str] = []
    for cmd in commands:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            trail.append(f"daemon start '{' '.join(cmd)}' errored: {exc}")
            reasons.append(f"{' '.join(cmd)}: {exc}")
            continue
        if r.returncode == 0:
            trail.append(f"issued daemon start: {' '.join(cmd)}")
            started = True
            break
        detail = (r.stderr or r.stdout or "").strip()
        trail.append(f"daemon start '{' '.join(cmd)}' failed: {detail}")
        reasons.append(f"{' '.join(cmd)}: {detail}")

    if not started:
        trail.append("no daemon start command succeeded — polling anyway")

    deadline = time.monotonic() + wait
    while True:
        if daemon_ready(docker):
            trail.append("docker daemon is ready")
            return None
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL)

    msg = f"Docker daemon on {node} did not become ready within {wait}s"
    if not started:
        msg += f" (no start command succeeded: {'; '.join(reasons)})"
    if platform.system() == "Darwin":
        # The single most common cause on a remote Mac node, and not something
        # the agent can fix from a background/SSH context.
        msg += (
            f". On macOS, Docker Desktop only runs inside an active GUI login "
            f"session — if {node} is headless or logged out, log in there and "
            f"start Docker Desktop (or add it as a login item so it survives "
            f"reboots)"
        )
    return msg


def ensure_daemon(
    docker: str,
    auto_start: bool = True,
    wait: int = 120,
    log: list[str] | None = None,
) -> str | None:
    """Guarantee the Docker daemon is up, starting it when permitted.

    Args:
        docker: Path to the docker binary.
        auto_start: When False, a dead daemon is reported rather than started.
        wait: Seconds to wait for readiness once a start has been issued.
        log: Optional trail list, appended in place.

    Returns:
        ``None`` when the daemon is ready, else an error string suitable for a
        step's ``error`` state.

    AI Note: the ready path costs exactly one ``docker info`` and touches
    nothing else, so putting this in front of every docker step is cheap. Do
    not "optimise" it away with a cached flag — the daemon can die mid-job, and
    a stale cache would resurrect the confusing downstream errors this replaces.
    """
    trail = log if log is not None else []
    if daemon_ready(docker):
        return None
    if not auto_start:
        return (
            f"Docker daemon is not running on {platform.node()} "
            f"(auto_start disabled); start Docker there and re-run"
        )
    trail.append("docker daemon not responding — attempting to start it")
    return start_daemon(docker, wait, trail)


def ensure_container(
    docker: str,
    name: str,
    image: str,
    mounts: list[str] | None = None,
    workdir: str | None = None,
    recreate: bool = False,
    timeout: int = 600,
    log: list[str] | None = None,
) -> ContainerResult:
    """Create, start, or attach to a named container so it is running.

    Decision order: ``recreate`` → running (attach) → exists-but-stopped
    (start) → absent (run). A final ``docker ps`` confirms the container is
    actually up before reporting success.

    Args:
        docker: Path to the docker binary. The daemon must already be up —
            call :func:`ensure_daemon` first.
        name: Container name to create or attach to.
        image: Image used only when the container has to be created.
        mounts: Bind mounts as ``HOST:CONTAINER`` or bare ``HOST``.
        workdir: Default working directory inside a newly created container.
        recreate: Remove any existing container of this name first.
        timeout: Seconds for the create path, which may include an image pull.
        log: Optional trail list, appended in place.

    Returns:
        A :class:`ContainerResult`.

    Side effects:
        May pull a multi-GB image, and create, start or remove a container. The
        container is left running afterwards — it is deliberately not owned by
        the caller's job, so later jobs attach instead of paying start costs.
    """
    trail = log if log is not None else []
    created = False

    def _docker(*args: str, cmd_timeout: int = 60) -> subprocess.CompletedProcess:
        """Run a docker subcommand as an argv list, capturing output."""
        return subprocess.run(
            [docker, *args], capture_output=True, text=True, timeout=cmd_timeout,
        )

    try:
        # AI Note: `name=^{name}$` anchors the filter — docker's --filter name=
        # is a substring match by default, so without the anchors a container
        # called "gem5_img_old" would satisfy a query for "gem5_img". The
        # `{{.Names}}` format plus the `== name` comparisons make it exact.
        running = _docker(
            "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"
        ).stdout.strip()
        exists = _docker(
            "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"
        ).stdout.strip()

        if recreate and exists == name:
            trail.append(f"removing existing container '{name}' (recreate=true)")
            # AI Note: the `rm -f` result is intentionally unchecked — if removal
            # fails, the `docker run` below fails loudly with a name conflict,
            # which is the better message. State inside the old container is
            # destroyed here.
            _docker("rm", "-f", name, cmd_timeout=120)
            exists = ""
            running = ""

        if running == name:
            trail.append(f"container '{name}' already running — attaching")
            # AI Note: the attach path does NOT verify that the running
            # container's image, mounts or workdir match the requested ones. A
            # container left over from an earlier job with different mounts is
            # silently reused; use recreate=true when the mount set changes, or
            # the gem5 paths will not resolve.
        elif exists == name:
            trail.append(f"container '{name}' exists but stopped — starting")
            r = _docker("start", name, cmd_timeout=120)
            if r.returncode != 0:
                return ContainerResult(
                    False, f"docker start failed: {r.stderr.strip()}", r.returncode,
                )
        else:
            args = ["run", "-d", "--name", name]
            for m in mounts or []:
                # AI Note: bare "HOST" is expanded to "HOST:HOST" so the path is
                # identical inside and outside the container. The whole
                # container-mode gem5 design depends on that: the binary path,
                # config path and m5out path are computed once and used on both
                # sides without translation.
                args += ["-v", m if ":" in m else f"{m}:{m}"]
            if workdir:
                args += ["-w", workdir]
            # AI Note: the image's own entrypoint/CMD is replaced by
            # `sleep infinity`. Without it a gem5 image would run its default
            # command and exit immediately, leaving nothing to `docker exec`
            # into. This also means image entrypoint setup logic never runs.
            args += [image, "sleep", "infinity"]
            trail.append(f"creating container '{name}' from {image}")
            r = _docker(*args, cmd_timeout=timeout)
            if r.returncode != 0:
                return ContainerResult(
                    False, f"docker run failed: {r.stderr.strip()}", r.returncode,
                )
            created = True

        # AI Note: this re-query is not redundant. `docker run -d` succeeds as
        # soon as the container is created, so a container whose keep-alive
        # command dies instantly (bad image, wrong architecture) would otherwise
        # be reported as ready, and every later `docker exec` would fail with a
        # confusing error.
        up = _docker(
            "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"
        ).stdout.strip()
        if up != name:
            return ContainerResult(
                created, f"container '{name}' is not running after ensure", 1,
            )

        return ContainerResult(created, None, 0)
    except subprocess.TimeoutExpired:
        return ContainerResult(created, "docker command timed out", -1)
    except Exception as exc:  # noqa: BLE001
        # AI Note: the broad catch is deliberate. Docker failures are wildly
        # varied (socket permission denied, DNS failure during pull); converting
        # them into a clean error keeps the operator's job log readable instead
        # of dumping an agent stack trace.
        return ContainerResult(created, f"{type(exc).__name__}: {exc}", -1)
