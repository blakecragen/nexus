"""Execute an uploaded script file on a compute node.

The script must already exist on the target node's filesystem (typically
placed there by a preceding file-transfer step or pre-provisioned).  The
step makes the file executable, invokes it, and captures the exit code.

Where this fits
---------------
Registered as ``"run_script"``. Same execution model as its sibling
``run_command``: ``startup()`` forks a detached child and returns a
serialisable state dict, then the agent's
:class:`~nexus_agent.executor.StepExecutor` polls ``check()`` once a second.

Differences from ``run_command``
--------------------------------
* No shell — the script is exec'd directly, so it needs a working shebang
  line and ``args`` are passed as a real argv list (no word splitting, no
  shell metacharacter interpretation).
* No ``OS_VARIANTS`` / no ``_command_str``; the per-job log therefore shows no
  command line for this step, only the captured stdout/stderr temp files.
* ``chmod +x`` is applied to the target file as a side effect.

AI Note: there is no ``"command"`` key in the returned state, which is what
routes the executor down its poll-based branch instead of re-running the
process itself. See the module docstring of ``run_command`` for details.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import tempfile
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class RunScriptParams(BaseModel):
    """Parameters for the run_script step.

    The ``description``/``examples`` text is user-facing — ``to_schema()``
    publishes it to ``/api/steps`` for the frontend step palette.
    """

    script_path: str = Field(
        ...,
        description="Absolute path to the script file on the node.",
        examples=["/tmp/nexus_scripts/setup.sh"],
    )
    args: list[str] = Field(
        default_factory=list,
        description="Positional arguments passed to the script.",
    )
    working_dir: str | None = Field(
        None,
        description="Working directory. Defaults to the script's parent directory.",
    )
    timeout: int = Field(
        3600,
        description="Maximum execution time in seconds.",
        ge=1,
        le=86400,
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("run_script")
class RunScriptStep(FlowStep):
    """Execute an uploaded script file on a compute node.

    Security note: this runs an arbitrary on-node executable as the agent user
    and additionally makes it world-executable. Trust boundaries are enforced
    upstream (authenticated job submission, authenticated agent WebSocket);
    nothing is validated here beyond the file existing.
    """

    PARAMS_SCHEMA = RunScriptParams
    OUTPUT_KEYS = ["exit_code"]
    DESCRIPTION = "Execute a script file with optional arguments."

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Make the script executable and spawn it as a detached subprocess.

        Args:
            params: Raw step params (already OS-resolved by the executor).
            ctx: Job context; ``ctx.resolve()`` layers upstream step outputs
                beneath these params, so e.g. a ``clone_path`` from a previous
                ``git_clone`` can supply an omitted value.

        Returns:
            A JSON-serialisable state dict with ``pid``, ``stdout_path``,
            ``stderr_path`` and ``timeout`` — or, if the script is missing, a
            dict carrying ``error``/``exit_code`` and no ``pid``.

        Side effects:
            ``chmod``s the script (adds the executable bit for user, group and
            other), creates two undeleted temp log files, and forks a process
            in a new session.

        AI Note: a missing script is reported by *returning* an error state
        rather than raising. That is intentional — the executor turns a raised
        exception into a ``StepFailed`` with a stack trace, whereas this path
        produces a clean ``check() -> FAILED`` with a readable message and a
        deterministic ``exit_code`` of -1. Any early return here must therefore
        include the ``error`` key that ``check()`` looks for first.
        """
        resolved = ctx.resolve(params)
        validated = RunScriptParams(**resolved)

        script = validated.script_path
        if not os.path.isfile(script):
            return {"error": f"Script not found: {script}", "exit_code": -1}

        # Ensure the script is executable.
        #
        # AI Note: the mode is OR-ed onto the *existing* mode rather than set
        # absolutely, so pre-existing permission bits survive. This grants
        # o+x — broader than strictly needed — because the agent may run as a
        # different user than the one that placed the file. The change is
        # permanent; nothing restores the original mode afterwards.
        current_mode = os.stat(script).st_mode
        os.chmod(script, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        cwd = validated.working_dir or os.path.dirname(script)

        # delete=False: the files must outlive this call so the executor can
        # read them back after the process exits.
        stdout_file = tempfile.NamedTemporaryFile(
            prefix="nexus_script_out_", suffix=".log", delete=False,
        )
        stderr_file = tempfile.NamedTemporaryFile(
            prefix="nexus_script_err_", suffix=".log", delete=False,
        )

        # AI Note: argv list (no shell), so the script's shebang decides the
        # interpreter and ``args`` entries are passed verbatim — no quoting or
        # glob expansion happens.
        cmd = [script, *validated.args]
        # start_new_session=True gives the child its own process group, which
        # is what makes cancel()'s killpg() reach the whole descendant tree.
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            start_new_session=True,
        )

        stdout_file.close()
        stderr_file.close()

        return {
            "pid": proc.pid,
            "stdout_path": stdout_file.name,
            "stderr_path": stderr_file.name,
            "timeout": validated.timeout,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        """Reap the subprocess non-blockingly and map its exit to a StepResult.

        Args:
            state: The dict returned by ``startup()``; mutated in place to
                record ``exit_code`` (published to the job context via
                ``OUTPUT_KEYS``).

        Returns:
            ``FAILED`` immediately if ``startup()`` recorded an ``error``;
            otherwise ``RUNNING`` while the child lives, ``SUCCESS`` on exit
            status 0, ``FAILED`` on any non-zero status or signal death.

        AI Note: the ``error`` check must stay first — the error state has no
        ``pid`` key and ``state["pid"]`` below would raise ``KeyError``.

        AI Note: ``os.waitpid`` consumes the zombie, so a second call after
        completion raises ``ChildProcessError``. That branch reports FAILED
        rather than SUCCESS because the real exit status is no longer
        recoverable; failing loudly is safer than a false success.
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

        # WEXITSTATUS is only valid when WIFEXITED; a signal-killed child
        # (e.g. SIGTERM from cancel()) reports -1 and fails the step.
        exit_status = os.WEXITSTATUS(result[1]) if os.WIFEXITED(result[1]) else -1
        state["exit_code"] = exit_status
        return StepResult.SUCCESS if exit_status == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        """Best-effort graceful termination of the script's process group.

        Args:
            state: The ``startup()`` state dict; only ``pid`` is used. Safe to
                call on an error state (no ``pid`` → no-op).

        Side effects:
            Sends ``SIGTERM`` to the child's whole process group.

        AI Note: signalling the group (not just the pid) relies on
        ``start_new_session=True`` in ``startup()`` and is what stops
        grandchildren the script spawned. ``ProcessLookupError`` (already
        exited) and ``PermissionError`` (re-parented / setuid child) are both
        expected outcomes of a cancel race and are swallowed deliberately.
        """
        pid = state.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
