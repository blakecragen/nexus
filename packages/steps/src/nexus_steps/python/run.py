"""Run Python code on a compute node.

Accepts either inline ``code`` (a string evaluated by the chosen
interpreter) or a pre-existing ``script_path`` on the node's filesystem.
``OS_VARIANTS`` picks a sensible default interpreter per platform; users
can override via the ``interpreter`` parameter.

Like the shell ``run_command`` step, stdout and stderr are routed to
temporary files so large outputs don't bloat the persisted state, and the
exit code is exposed via ``OUTPUT_KEYS`` for downstream conditional
control flow.

Where this fits
---------------
Registered as ``"run_python"``. The server validates params at submit time
against :class:`RunPythonParams`, then dispatches to an agent; the agent's
:class:`~nexus_agent.executor.StepExecutor` calls ``startup()`` once and polls
``check()`` every second until it returns a terminal result.

Source selection is validated in two complementary places:

* :meth:`RunPythonStep.input_rules` (an ``AtLeastOneRule``) enforces "at least
  one of code/script_path" during the rule pass, producing a friendly
  field-level error;
* :meth:`RunPythonParams._exactly_one_source` tightens that to "exactly one"
  during the Pydantic pass.

AI Note: as with the shell steps, the returned state has no ``"command"`` key,
which routes the executor down its poll-based branch rather than making it
re-execute the process itself.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from typing import Any

from pydantic import BaseModel, Field, model_validator

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import (
    AtLeastOneRule,
    FlowStep,
    InputRule,
    StepContext,
)
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class RunPythonParams(BaseModel):
    """Parameters for the run_python step.

    Exactly one of ``code`` or ``script_path`` must be supplied; see
    :meth:`_exactly_one_source`. The ``description``/``examples`` text is
    user-facing — ``to_schema()`` publishes it to ``/api/steps`` for the
    frontend step palette.
    """

    code: str | None = Field(
        None,
        description="Inline Python source to execute. Mutually inclusive with script_path.",
        examples=["import sys; print(sys.version)"],
    )
    script_path: str | None = Field(
        None,
        description="Absolute path to a .py file already on the node.",
        examples=["/tmp/nexus_scripts/run.py"],
    )
    args: list[str] = Field(
        default_factory=list,
        description="Positional arguments passed to the script.",
    )
    working_dir: str | None = Field(
        None,
        description="Working directory. Defaults to the agent's cwd.",
    )
    timeout: int = Field(
        3600,
        description="Maximum execution time in seconds.",
        ge=1,
        le=86400,
    )
    interpreter: str | None = Field(
        None,
        description=(
            "Path to the Python interpreter. Auto-selected per OS when omitted."
        ),
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables for the subprocess (merged into os.environ).",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "RunPythonParams":
        """Reject supplying both, or neither, of ``code`` and ``script_path``.

        Returns:
            ``self`` when the params are valid (required by Pydantic's
            ``mode="after"`` validator protocol).

        Raises:
            ValueError: when both sources are set or both are absent. Pydantic
                wraps this into a ``ValidationError``, which
                ``FlowStep.validate_params`` reports as a ``_schema`` field
                error at submit time.

        AI Note: the check is ``bool(a) == bool(b)``, i.e. truthiness, not
        ``is None``. An empty-string ``code=""`` therefore counts as *absent*,
        so ``code="" `` + ``script_path=...`` is accepted rather than being
        rejected as "both supplied". That is the desired behaviour here (an
        empty program is meaningless), but it means ``code`` cannot be used to
        run a deliberately empty script.
        """
        if bool(self.code) == bool(self.script_path):
            raise ValueError("provide exactly one of 'code' or 'script_path'")
        return self


# ── Step ─────────────────────────────────────────────────────────────────


@register("run_python")
class RunPythonStep(FlowStep):
    """Run Python code on a compute node (inline or from a script file).

    Security note: this is arbitrary code execution on the node by design, and
    ``env`` lets the caller inject environment variables into the child (it is
    merged over a copy of the agent's own ``os.environ``, so it can shadow
    agent-level variables for the child only). Authorisation is enforced
    upstream at job submission and on the agent WebSocket.
    """

    PARAMS_SCHEMA = RunPythonParams
    OUTPUT_KEYS = ["exit_code", "stdout_path", "stderr_path"]
    DESCRIPTION = "Run Python (inline code or a script) on a compute node."

    # AI Note: merged into params by ``resolve_for_os()`` before ``startup()``;
    # an explicit ``interpreter`` always wins. These are absolute system paths,
    # so a node whose Python lives in a virtualenv or under Homebrew must set
    # ``interpreter`` explicitly.
    OS_VARIANTS = {
        "macos": {"interpreter": "/usr/bin/python3"},
        "linux": {"interpreter": "/usr/bin/python3"},
        "windows": {"interpreter": "python.exe"},
    }

    @classmethod
    def input_rules(cls) -> list[InputRule]:
        """Replace the default per-field rules with a single at-least-one rule.

        Returns:
            A one-element list containing an ``AtLeastOneRule`` over
            ``code``/``script_path``.

        AI Note: this deliberately overrides the base implementation entirely,
        which means the remaining fields get NO rules at all — they are
        effectively optional as far as the rule pass is concerned. That is
        correct (they all have defaults), but adding a new *required* field to
        ``RunPythonParams`` will not be enforced by the rule pass here; it will
        only be caught later by the Pydantic type pass. Also note the rule is
        context-aware: ``code`` or ``script_path`` published by an upstream step
        satisfies it.
        """
        # Either code OR script_path satisfies the source requirement; the
        # exclusivity (not-both) is enforced by the model_validator above.
        return [AtLeastOneRule(["code", "script_path"], "Python source")]

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Materialise the source if needed and spawn a detached interpreter.

        Args:
            params: Raw step params (already OS-resolved by the executor).
            ctx: Job context; ``ctx.resolve()`` layers upstream step outputs
                beneath these params.

        Returns:
            A JSON-serialisable state dict with ``pid``, ``source_path``,
            ``cleanup_source`` (the temp file to delete afterwards, or
            ``None``), ``stdout_path``, ``stderr_path`` and ``timeout``. If
            ``script_path`` does not exist, returns an ``error``/``exit_code``
            dict instead and spawns nothing.

        Side effects:
            May create a temp ``.py`` file for inline code; always creates two
            undeleted temp log files; forks a process in a new session.

        Raises:
            pydantic.ValidationError: if neither/both sources were given or
                another field fails validation.
        """
        resolved = ctx.resolve(params)
        validated = RunPythonParams(**resolved)

        # Bare "python3" (resolved via PATH) is the last-resort fallback for
        # when OS_VARIANTS did not apply — e.g. an unrecognised OS string or a
        # direct unit-test call that bypassed resolve_for_os().
        interpreter = validated.interpreter or "python3"
        cwd = validated.working_dir or os.getcwd()

        # Resolve the source: inline code is materialized to a tempfile so
        # the subprocess invocation looks identical to the script_path path.
        #
        # AI Note: ``cleanup_source`` is set ONLY for the inline-code case, so
        # that check() never deletes a user-supplied script.
        cleanup_source: str | None = None
        if validated.code is not None:
            src = tempfile.NamedTemporaryFile(
                prefix="nexus_pycode_", suffix=".py", delete=False, mode="w",
            )
            src.write(validated.code)
            src.close()
            source_path = src.name
            cleanup_source = source_path
        else:
            source_path = validated.script_path
            if not os.path.isfile(source_path):
                # Return (rather than raise) so check() surfaces a clean
                # message instead of the executor reporting a stack trace.
                return {"error": f"Script not found: {source_path}", "exit_code": -1}

        # delete=False: these must outlive startup() so the executor can read
        # them back once the process has exited.
        stdout_file = tempfile.NamedTemporaryFile(
            prefix="nexus_python_out_", suffix=".log", delete=False,
        )
        stderr_file = tempfile.NamedTemporaryFile(
            prefix="nexus_python_err_", suffix=".log", delete=False,
        )

        # AI Note: the child inherits a *copy* of the agent's environment with
        # the user's ``env`` layered on top — user keys can shadow agent keys
        # for the child, but nothing here mutates the agent's own os.environ.
        env = os.environ.copy()
        env.update(validated.env)
        # Unbuffered output so the dashboard sees stdout in real time when
        # the executor switches to its streaming path in the future.
        #
        # AI Note: setdefault, not assignment — a caller who explicitly passes
        # PYTHONUNBUFFERED in ``env`` keeps control.
        env.setdefault("PYTHONUNBUFFERED", "1")

        cmd = [interpreter, source_path, *validated.args]
        # start_new_session=True gives the child its own process group so
        # cancel()'s killpg() can reach any processes it spawns in turn.
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

        stdout_file.close()
        stderr_file.close()

        return {
            "pid": proc.pid,
            "source_path": source_path,
            "cleanup_source": cleanup_source,
            "stdout_path": stdout_file.name,
            "stderr_path": stderr_file.name,
            "timeout": validated.timeout,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        """Reap the interpreter, clean up any generated source, report status.

        Args:
            state: The ``startup()`` state dict; mutated in place to record
                ``exit_code`` (exported to the job context via ``OUTPUT_KEYS``).

        Returns:
            ``FAILED`` if ``startup()`` recorded an ``error``; otherwise
            ``RUNNING`` while the interpreter lives, ``SUCCESS`` on exit status
            0, ``FAILED`` on non-zero status or death by signal.

        Side effects:
            On completion, unlinks the temp file created for inline ``code``
            (never a caller-supplied ``script_path``).

        AI Note: the ``error`` branch must stay first — an error state has no
        ``pid`` and ``state["pid"]`` would raise ``KeyError``.

        AI Note: ``os.waitpid`` consumes the zombie, so a second call after
        completion raises ``ChildProcessError``; that branch reports FAILED
        because the true exit status is unrecoverable at that point.
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

        # WEXITSTATUS is only meaningful when WIFEXITED; a signal-killed
        # interpreter (e.g. SIGTERM from cancel()) reports -1 → FAILED.
        exit_status = os.WEXITSTATUS(result[1]) if os.WIFEXITED(result[1]) else -1
        state["exit_code"] = exit_status

        # Best-effort cleanup of the materialized inline-code tempfile.
        #
        # AI Note: cleanup happens here rather than in startup()'s finally
        # block because the interpreter is still reading the file while it
        # runs. It is skipped entirely on the RUNNING and error paths, so an
        # inline-code temp file leaks whenever the step is cancelled or the
        # agent dies mid-run — acceptable (it lives in the OS temp dir), but
        # worth knowing when auditing disk usage on long-lived nodes.
        cleanup = state.get("cleanup_source")
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass

        return StepResult.SUCCESS if exit_status == 0 else StepResult.FAILED

    def cancel(self, state: dict[str, Any]) -> None:
        """Best-effort graceful termination of the interpreter's process group.

        Args:
            state: The ``startup()`` state dict; only ``pid`` is used. Safe on
                an error state (no ``pid`` → no-op).

        Side effects:
            Sends ``SIGTERM`` to the child's whole process group.

        AI Note: group signalling depends on ``start_new_session=True`` in
        ``startup()`` and is what stops subprocesses the Python program itself
        launched. ``ProcessLookupError``/``PermissionError`` are normal
        cancel-race outcomes and are swallowed on purpose.
        """
        pid = state.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
