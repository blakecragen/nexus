"""OS-aware shell command execution step.

Executes a shell command on a compute node, routing stdout and stderr to
temporary files so large outputs don't bloat the job state.  OS_VARIANTS
select the default shell binary per platform.

Where this fits
---------------
Registered as ``"run_command"`` and discovered through
:mod:`nexus_steps` → :data:`nexus_common.steps.registry.STEP_REGISTRY`. The
server validates submitted params against :class:`RunCommandParams` at submit
time, then dispatches an ``ExecuteStepCommand`` to an agent; the agent's
:class:`~nexus_agent.executor.StepExecutor` calls ``startup()`` once and then
polls ``check()`` once a second until it stops returning ``RUNNING``.

Execution model
---------------
``startup()`` spawns a *detached* process and returns immediately — the step
is asynchronous from the executor's point of view. The returned state dict is
persisted to the DB for crash recovery, so every value in it must be JSON
serialisable (hence a bare ``pid`` and file paths rather than live handles).

AI Note: the returned state deliberately does NOT contain a ``"command"`` key.
``StepExecutor.execute()`` branches on exactly that key: with it, the executor
re-runs the command itself and streams output live; without it, the executor
takes the poll path and this step owns the subprocess. The human-readable
command is therefore exported under the ``_command_str`` key instead, which
``StepExecutor._capture()`` reads for the per-job terminal log. Renaming
``_command_str`` to ``command`` would cause the command to be executed twice.
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


class RunCommandParams(BaseModel):
    """Parameters for the run_command step.

    Field ``description`` and ``examples`` text is user-facing: ``to_schema()``
    exports it to ``/api/steps`` and the frontend renders it in the step
    palette. Keep the wording accurate and professional.
    """

    command: str = Field(
        ...,
        description="Shell command string to execute.",
        examples=["echo hello", "ls -la /tmp"],
    )
    working_dir: str | None = Field(
        None,
        description="Working directory for the command. Defaults to the agent's cwd.",
    )
    timeout: int = Field(
        3600,
        description="Maximum execution time in seconds.",
        ge=1,
        le=86400,
    )
    shell: str | None = Field(
        None,
        description=(
            "Path to the shell binary. Auto-selected per OS when omitted."
        ),
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("run_command")
class RunCommandStep(FlowStep):
    """Execute a shell command on a compute node.

    Security note: ``command`` is passed to a shell with ``-c``, so it is
    arbitrary code execution on the node by design. Authorisation is enforced
    upstream (job submission requires an authenticated user, and the agent only
    accepts commands over its authenticated WebSocket) — this class performs no
    sanitising of its own and must not be exposed to untrusted input.
    """

    PARAMS_SCHEMA = RunCommandParams
    OUTPUT_KEYS = ["exit_code", "stdout_path", "stderr_path"]
    DESCRIPTION = "Run an OS-aware shell command on a compute node."

    # AI Note: merged into params by ``FlowStep.resolve_for_os()`` before
    # ``startup()``; an explicitly supplied ``shell`` always wins. These are the
    # only per-OS knobs — the rest of the step assumes POSIX semantics.
    OS_VARIANTS = {
        "macos": {"shell": "/bin/zsh"},
        "linux": {"shell": "/bin/bash"},
        "windows": {"shell": "powershell.exe"},
    }

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Spawn the command as a detached subprocess and return its state.

        Args:
            params: Raw step params (already OS-resolved by the executor).
            ctx: Job context; ``ctx.resolve()`` layers upstream step outputs
                underneath these params so e.g. a ``clone_path`` published by a
                previous ``git_clone`` can satisfy an omitted field.

        Returns:
            A JSON-serialisable state dict consumed by ``check()`` /
            ``cancel()`` and by ``StepExecutor._capture()``:
            ``pid``, ``stdout_path``, ``stderr_path``, ``timeout`` and the
            display-only ``_command_str``.

        Side effects:
            Creates two undeleted temp files and forks a process in a new
            session. Nothing here cleans those temp files up — they are read
            back by the executor after completion and then left on the node
            (see the possible-leak note below).

        Raises:
            pydantic.ValidationError: if the merged params fail schema checks.
            OSError: if the shell binary or working directory does not exist.
        """
        resolved = ctx.resolve(params)
        validated = RunCommandParams(**resolved)

        # AI Note: fallback to /bin/sh only matters when OS_VARIANTS did not
        # apply (unknown OS string, or a direct unit-test call bypassing
        # resolve_for_os).
        shell = validated.shell or "/bin/sh"
        cwd = validated.working_dir or os.getcwd()

        # AI Note: delete=False is required — the files must outlive this
        # function so check()/the executor can read them after the process
        # exits. They are intentionally not cleaned up here.
        stdout_file = tempfile.NamedTemporaryFile(
            prefix="nexus_stdout_", suffix=".log", delete=False,
        )
        stderr_file = tempfile.NamedTemporaryFile(
            prefix="nexus_stderr_", suffix=".log", delete=False,
        )

        # AI Note: start_new_session=True puts the child in its own process
        # group. That is what makes cancel()'s killpg() able to take down the
        # whole descendant tree (a shell plus whatever it spawned), and it also
        # detaches the child from the agent's terminal so a Ctrl-C against the
        # agent does not kill running jobs.
        proc = subprocess.Popen(
            [shell, "-c", validated.command],
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            start_new_session=True,
        )

        # Close our handles; the child holds its own inherited descriptors.
        stdout_file.close()
        stderr_file.close()

        return {
            "pid": proc.pid,
            "stdout_path": stdout_file.name,
            "stderr_path": stderr_file.name,
            "timeout": validated.timeout,
            "_command_str": f"{shell} -c {validated.command!r}",
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        """Reap the subprocess non-blockingly and map its exit to a StepResult.

        Args:
            state: The dict returned by ``startup()``. Mutated in place —
                ``exit_code`` is written here so it can be picked up as an
                ``OUTPUT_KEYS`` value and published to the job context.

        Returns:
            ``RUNNING`` while the child is alive, ``SUCCESS`` on exit status 0,
            ``FAILED`` otherwise (including abnormal termination by signal,
            which is reported as ``exit_code == -1``).

        AI Note: ``os.waitpid(pid, WNOHANG)`` only works from the process that
        forked the child, and it *consumes* the zombie. Calling ``check()``
        again after it has already reaped will raise ``ChildProcessError``,
        which is why that branch exists. It is treated as FAILED rather than
        SUCCESS on purpose: the true exit status is unrecoverable at that
        point, so failing loudly beats silently reporting success. This makes
        ``check()`` NOT idempotent after completion even though the FlowStep
        contract asks for idempotency — the executor's poll loop returns as
        soon as it sees a terminal result, so it never calls twice in practice.
        """
        pid = state["pid"]
        try:
            result = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # Process already reaped -- treat as completed.
            state["exit_code"] = state.get("exit_code", -1)
            return StepResult.FAILED

        if result == (0, 0):
            # Still running.
            return StepResult.RUNNING

        # AI Note: WEXITSTATUS is only meaningful when WIFEXITED is true. A
        # child killed by a signal (e.g. SIGTERM from cancel(), or an OOM kill)
        # takes the -1 branch and is reported as FAILED.
        exit_status = os.WEXITSTATUS(result[1]) if os.WIFEXITED(result[1]) else -1
        state["exit_code"] = exit_status
        return StepResult.SUCCESS if exit_status == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        """Best-effort graceful termination of the command's process group.

        Args:
            state: The ``startup()`` state dict; only ``pid`` is used.

        Side effects:
            Sends ``SIGTERM`` to the child's entire process group.

        AI Note: signalling the *group* (not just the pid) is what kills
        grandchildren spawned by the shell; it depends on the
        ``start_new_session=True`` in ``startup()``. Failures are swallowed
        because both losing the race with natural exit (``ProcessLookupError``)
        and lacking permission on a re-parented group (``PermissionError``) are
        normal outcomes of a cancel, not step errors. Note this only requests
        termination — the executor separately escalates to SIGKILL.
        """
        pid = state.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
