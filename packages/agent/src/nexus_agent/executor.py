"""Step execution engine.

Responsible for:
- Loading the step class from STEP_REGISTRY
- Resolving OS-specific parameters
- Running startup() and managing subprocess execution
- Streaming stdout/stderr back to the server via WebSocket
- Polling check() to determine the outcome
- Handling cancel() on CancelStepCommand

Role in the system:
    `nexus_agent.connection.AgentConnection` owns one `StepExecutor` and
    spawns `execute()` as a detached task per inbound `ExecuteStepCommand`.
    The executor reports back through `connection.send_message()`; the
    server's runner (`nexus_server.runner.runner`) is blocked on those
    step.completed / step.failed messages, which is why they are sent with
    `critical=True`.

Two execution shapes, chosen by whether startup() put a "command" in state:
    Command steps  — a shell subprocess whose stdout/stderr are streamed live
                     as step.log messages and buffered for the per-job log.
    Poll steps     — no subprocess here; startup() launched the work (often
                     redirecting output to temp files) and check() is polled
                     once a second until SUCCESS or FAILED.

Step classes themselves live in `nexus_steps` and derive from
`nexus_common.steps.base.FlowStep`; this module only drives their lifecycle
(startup → check → cancel) and never interprets their parameters.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from nexus_common.agent_protocol import (
    CancelStepCommand,
    ExecuteStepCommand,
    StepCompleted,
    StepFailed,
    StepLog,
    StepStarted,
)
from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import STEP_REGISTRY, get_step

# AI Note: Import-for-side-effect, not for symbols. Importing `nexus_steps`
# runs every module's @register decorator, which is the only thing that fills
# STEP_REGISTRY. Without this import `get_step()` raises KeyError for every
# step name, so the noqa must stay and the import must not be moved into a
# function.
import nexus_steps  # noqa: F401 — triggers @register decorators, populates STEP_REGISTRY

from nexus_agent.capability import _detect_os_type
from nexus_agent.os_adapters import get_adapter

if TYPE_CHECKING:
    # Import-cycle break: connection.py imports this module at runtime, so the
    # back-reference type is only available to type checkers.
    from nexus_agent.connection import AgentConnection

logger = logging.getLogger("nexus.agent.executor")


class StepExecutor:
    """Manages concurrent step executions on this node.

    One instance per `AgentConnection`, living for the whole agent process
    (it survives reconnects, so a step started on one socket can report its
    result on the next).

    Concurrency: `execute()` may run many times in parallel. All shared state
    is the `_running_steps` dict, mutated only from the single event loop
    thread, so no lock is needed — but every mutation must stay synchronous
    (no `await` between read and write of the same key).
    """

    def __init__(self, connection: AgentConnection) -> None:
        """Bind the executor to the connection it reports over.

        Args:
            connection: Owning `AgentConnection`; used for `send_message()`
                and for reading `config` (node id, API key, server URL).
        """
        self._connection = connection
        # AI Note: The "{job_id}:{step_index}" key is the cross-process
        # identity of a step run — the server's runner uses the same
        # composite key for its completion events. A job that could ever
        # dispatch the same step index twice concurrently would collide here.
        self._running_steps: dict[str, _RunningStep] = {}  # key: "{job_id}:{step_index}"

    @property
    def active_count(self) -> int:
        """Number of steps currently executing; reported in every heartbeat."""
        return len(self._running_steps)

    # ── Execute ────────────────────────────────────────────────────────

    async def execute(self, cmd: ExecuteStepCommand) -> None:
        """Execute a step command from the server.

        Full lifecycle for one step: resolve the class, run startup(), announce
        step.started, run the work (subprocess or poll), then announce
        step.completed or step.failed. Never raises — every failure path is
        converted into a step.failed message, because the server's runner is
        blocked waiting for a terminal message and would otherwise hang until
        its 7200s timeout.

        Args:
            cmd: The server's dispatch message. `cmd.params` are already
                context-merged and OS-resolved server-side; they are resolved
                again here against the *actual* host OS.

        Side effects:
            Registers/removes an entry in `_running_steps`, runs arbitrary
            step code (which may spawn subprocesses, write files, and make
            network calls), and sends three or more WebSocket messages.

        AI Note: `cmd.credential_config` and `cmd.artifacts` are never read
        here. The server resolves and transmits a decrypted credential, but
        the agent silently drops it — steps needing credentials will fail as
        if none was configured. See POSSIBLE BUG in the task summary.
        """
        key = f"{cmd.job_id}:{cmd.step_index}"
        logger.info("Executing step %s/%d (%s)", cmd.job_id, cmd.step_index, cmd.step_name)

        try:
            # Load step class
            # Raises KeyError if the server knows a step this agent's
            # nexus_steps build does not — a version-skew signal.
            step_cls = get_step(cmd.step_name)
            step = step_cls()

            # AI Note: The server already applied resolve_for_os() using the
            # node's *registered* os_type. Re-resolving with the locally
            # detected OS is deliberate belt-and-braces: it corrects a stale
            # node record, and is idempotent since explicit params always win
            # over OS defaults.
            # Resolve OS-specific parameters
            os_type = _detect_os_type()
            params = step_cls.resolve_for_os(cmd.params, os_type)

            # Build context (params already resolved server-side, but we carry os_type)
            cfg = self._connection.config
            # AI Note: Fragile but intentional URL derivation — steps that
            # upload results (e.g. gem5_collect_results) need an HTTP base,
            # and the agent only ever knows the ws:// URL. This assumes the
            # configured server_url contains "/ws/" and that HTTP is served on
            # the same host:port. If "/ws/" is absent, split() returns the
            # whole URL and the callback base ends up wrong.
            # HTTP base from the ws:// server URL (ws://host:8000/ws/agent/.. → http://host:8000)
            http_base = cfg.server_url.split("/ws/")[0].replace("ws://", "http://", 1).replace("wss://", "https://", 1)
            ctx = StepContext(
                outputs=params,
                os_type=os_type,
                node_id=cfg.node_id,
                job_id=cmd.job_id,
                server_url=http_base,
                # Steps authenticate their result uploads with the node key.
                node_api_key=cfg.api_key,
            )

            # AI Note: startup() is called synchronously on the event loop. A
            # step whose startup blocks (long file I/O, a blocking spawn)
            # stalls heartbeats and the listener for every other step on this
            # node. Step implementations must keep startup() fast.
            # Run startup()
            state = step.startup(params, ctx)

            # Track the running step
            running = _RunningStep(
                job_id=cmd.job_id,
                step_index=cmd.step_index,
                step=step,
                state=state,
                params=params,
            )
            self._running_steps[key] = running

            # AI Note: `state` is shipped to the server and persisted on the
            # step_run row for crash recovery, so startup() must return only
            # JSON-serializable values. Sent critical because the runner keys
            # its "running" transition off this message.
            # Notify server that step has started
            await self._connection.send_message(
                StepStarted(
                    job_id=cmd.job_id,
                    step_index=cmd.step_index,
                    state=state,
                ).model_dump(mode="json"),
                critical=True,
            )

            # AI Note: The presence of the "command" key in startup()'s state
            # is the *entire* contract deciding subprocess-vs-poll execution.
            # A poll-based step must not put a "command" key in state (use
            # "_command_str" instead — see _capture) or it will be re-executed
            # here as a shell command.
            # If the step has a "command" in state, run it as a subprocess
            if "command" in state:
                await self._run_subprocess(running)
            else:
                # Poll-based step — call check() in a loop
                await self._poll_step(running)

            # Step completed successfully. Build outputs from the step's declared
            # OUTPUT_KEYS (steps put values directly in state, not under
            # "outputs"). Fall back to an explicit "outputs" dict if present.
            # AI Note: This OUTPUT_KEYS extraction is the fix from commit
            # 87ea852 — remote steps previously always returned outputs={},
            # so chained steps never saw upstream values. Keys absent from
            # state are silently skipped rather than sent as None.
            outputs = state.get("outputs")
            if not isinstance(outputs, dict):
                outputs = {k: state[k] for k in step_cls.OUTPUT_KEYS if k in state}
            command, stdout, stderr, exit_code = self._capture(running)
            await self._connection.send_message(
                StepCompleted(
                    job_id=cmd.job_id,
                    step_index=cmd.step_index,
                    outputs=outputs,
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                ).model_dump(mode="json"),
                critical=True,
            )
            logger.info("Step %s/%d completed successfully", cmd.job_id, cmd.step_index)

        except asyncio.CancelledError:
            # AI Note: Cancellation is reported as a *failure* rather than
            # re-raised. Swallowing CancelledError normally breaks structured
            # concurrency, but here it is required: the server must receive a
            # terminal step message, and the critical send below needs to
            # complete after the cancel. Note the send itself can be
            # re-cancelled if the whole loop is shutting down.
            logger.info("Step %s/%d was cancelled", cmd.job_id, cmd.step_index)
            command, stdout, stderr, _ = self._capture(self._running_steps.get(key))
            await self._connection.send_message(
                StepFailed(
                    job_id=cmd.job_id,
                    step_index=cmd.step_index,
                    error="Step cancelled",
                    exit_code=None,
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                ).model_dump(mode="json"),
                critical=True,
            )
        except Exception as exc:
            logger.error("Step %s/%d failed: %s", cmd.job_id, cmd.step_index, exc, exc_info=True)
            command, stdout, stderr, exit_code = self._capture(self._running_steps.get(key))
            await self._connection.send_message(
                StepFailed(
                    job_id=cmd.job_id,
                    step_index=cmd.step_index,
                    error=str(exc),
                    # SubprocessError carries the child's exit status; other
                    # exceptions fall back to whatever landed in state.
                    exit_code=getattr(exc, "returncode", None) or exit_code,
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                ).model_dump(mode="json"),
                critical=True,
            )
        finally:
            # AI Note: Must run after _capture() in every branch above —
            # popping the entry first would discard the buffered stdout/stderr
            # that the terminal message reports. The pop is unconditional so a
            # step that failed before registration is a harmless no-op.
            self._running_steps.pop(key, None)

    # ── Cancel ─────────────────────────────────────────────────────────

    async def cancel(self, cmd: CancelStepCommand) -> None:
        """Cancel a running step.

        Escalates in three stages: the step's own `cancel()` hook (graceful,
        step-defined), SIGTERM then SIGKILL on any subprocess, and finally
        task cancellation so `execute()` unwinds and reports step.failed.

        Args:
            cmd: Identifies the step by job id and step index.

        Side effects:
            Terminates/kills a child process and cancels the asyncio task
            running the step. Does not itself send any message — the
            step.failed comes from `execute()`'s CancelledError handler.

        Note:
            An unknown key is logged and ignored (the step likely already
            finished), so cancelling twice is safe.

        AI Note: Awaited by the listener loop, unlike execute(). The
        `wait_for(..., timeout=5.0)` window is the only grace period a child
        gets between terminate() and kill(); a process that needs longer to
        flush results will lose them.
        """
        key = f"{cmd.job_id}:{cmd.step_index}"
        running = self._running_steps.get(key)
        if running is None:
            logger.warning("Cancel requested for unknown step %s", key)
            return

        logger.info("Cancelling step %s", key)

        # Call the step's cancel method
        try:
            running.step.cancel(running.state)
        except Exception as exc:
            # A misbehaving cancel() hook must not stop the kill escalation
            # below, or the subprocess would be orphaned.
            logger.warning("Step cancel() raised: %s", exc)

        # Kill subprocess if running
        if running.process is not None and running.process.returncode is None:
            try:
                running.process.terminate()
                # Give it 5 seconds to terminate gracefully
                try:
                    await asyncio.wait_for(running.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    running.process.kill()
            except ProcessLookupError:
                # Raced with natural exit between the returncode check and
                # terminate() — nothing left to kill.
                pass

        # AI Note: Only the shell process is signalled, not its process group
        # (no start_new_session / os.killpg). Grandchildren spawned by the
        # command — the common case for `make -j`, gem5 wrappers, etc. — keep
        # running after a cancel.
        # Cancel the task
        if running.task is not None and not running.task.done():
            running.task.cancel()

    # ── Subprocess Execution ───────────────────────────────────────────

    async def _run_subprocess(self, running: _RunningStep) -> None:
        """Execute a shell command as a subprocess and stream output.

        Args:
            running: Tracking record whose `state["command"]` holds the shell
                string and whose optional `state["work_dir"]` sets the cwd
                (defaults to the platform temp dir).

        Side effects:
            Creates `work_dir` if missing, spawns a shell, sends one step.log
            message per output line, and records `state["exit_code"]`. Also
            stores the process and current task on `running` so `cancel()` can
            reach them.

        Raises:
            SubprocessError: Non-zero exit code, carrying `returncode`.
            StepCheckFailed: The command succeeded but the step's post-run
                `check()` returned FAILED.

        AI Note: The command string is passed to a shell unquoted and unescaped
        — arbitrary command execution is the intended feature of this system,
        which is why node API keys and job-submission rights must be treated as
        equivalent to shell access on every node.
        """
        adapter = get_adapter()
        command = running.state["command"]
        shell_cmd = adapter.shell_command()
        work_dir = running.state.get("work_dir", adapter.temp_dir())

        # Ensure work directory exists
        os.makedirs(work_dir, exist_ok=True)

        logger.debug("Running command: %s", command)

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            # executable= overrides the default /bin/sh with the adapter's
            # shell so step commands can rely on bash/zsh syntax.
            executable=shell_cmd,
        )
        running.process = process
        # AI Note: `current_task()` is the task created by the listener loop
        # for execute(); storing it here is what makes cancel() able to unwind
        # the whole step, not just the child process.
        running.task = asyncio.current_task()

        # Stream stdout and stderr concurrently
        async def stream_pipe(pipe: asyncio.StreamReader, stream_name: str) -> None:
            """Forward one pipe line-by-line to the server and the local buffer.

            Args:
                pipe: The child's stdout or stderr reader.
                stream_name: "stdout" or "stderr" — used both as the protocol
                    `stream` discriminator and as the `running.captured` key.

            AI Note: `readline()` has no length limit, so a command emitting a
            huge line with no newline can exhaust memory. Log sends are
            best-effort (not critical): losing a line during a reconnect is
            acceptable because `running.captured` still holds it for the
            terminal step.completed/failed message.
            """
            while True:
                line = await pipe.readline()
                if not line:
                    break
                # errors="replace" so undecodable build output never kills the
                # stream; rstrip only "\n" to preserve intentional whitespace.
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                running.captured[stream_name].append(text)  # buffer for the per-job log
                await self._connection.send_message(
                    StepLog(
                        job_id=running.job_id,
                        step_index=running.step_index,
                        stream=stream_name,
                        line=text,
                        timestamp=datetime.now(timezone.utc),
                    ).model_dump(mode="json")
                )

        # AI Note: Both pipes must be drained concurrently. Reading them
        # sequentially would deadlock as soon as the child filled the other
        # pipe's OS buffer (~64KB) and blocked on write.
        await asyncio.gather(
            stream_pipe(process.stdout, "stdout"),
            stream_pipe(process.stderr, "stderr"),
        )

        # Both pipes are at EOF here, so wait() returns essentially immediately.
        exit_code = await process.wait()
        running.state["exit_code"] = exit_code

        if exit_code != 0:
            raise SubprocessError(
                f"Command exited with code {exit_code}",
                returncode=exit_code,
            )

        # AI Note: check() runs even for command steps, giving a step a chance
        # to reject a run that exited 0 but produced no/invalid artifacts
        # (e.g. gem5's stats-file check). Only FAILED is honored — a check()
        # that returns RUNNING here is treated as success.
        # After subprocess completes, run check() for final validation
        result = running.step.check(running.state)
        if result == StepResult.FAILED:
            raise StepCheckFailed(
                running.state.get("error") or "Step check() returned FAILED after subprocess"
            )

    # ── Output capture (for the per-job terminal log) ──────────────────

    _CAP_BYTES = 256 * 1024  # keep the tail of each stream, per step
    """Per-stream byte cap on captured output.

    AI Note: Bounds what one step can push into the job's `log_text` column
    and over the WebSocket in a single message. Raising it risks oversized
    frames and bloated rows for chatty builds.
    """

    def _capture(self, running: _RunningStep | None):
        """Return (command, stdout, stderr, exit_code) for a finished step.

        Command-streaming steps buffer lines in memory; poll-based steps (the
        shipped run_command/gem5 steps) wrote to temp files — read those back.
        Each stream is truncated to the last _CAP_BYTES.

        Args:
            running: The tracking record, or `None` when the step failed
                before it was registered (e.g. an unknown step name or a
                startup() crash).

        Returns:
            A 4-tuple of optional values feeding the per-job terminal log. All
            four are `None` when `running` is `None`; individual entries are
            `None` when there was no output or the temp file was unreadable.

        Side effects:
            Reads the step's stdout/stderr temp files from disk.

        AI Note: Never raises — it is called from `execute()`'s except/finally
        paths, where an exception would prevent the terminal step message from
        ever being sent and hang the job. Hence the OSError swallow in _read.
        """
        if running is None:
            return None, None, None, None
        state = running.state
        # "_command_str" is the poll-step convention for recording what was run
        # without triggering the subprocess path (which keys off "command").
        command = state.get("_command_str") or state.get("command")
        exit_code = state.get("exit_code")

        def _read(stream_name: str, path_key: str) -> str | None:
            """Resolve one stream's text from the in-memory buffer or temp file.

            Args:
                stream_name: "stdout" or "stderr".
                path_key: State key holding the temp file path for poll steps
                    ("stdout_path" / "stderr_path").

            Returns:
                The captured text (tail-truncated), or `None` if empty or
                unreadable. The in-memory buffer wins when non-empty, so a
                command step never re-reads from disk.
            """
            buf = running.captured.get(stream_name)
            if buf:
                text = "\n".join(buf)
            else:
                path = state.get(path_key)
                if not path:
                    return None
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    # File already cleaned up or never created — report no
                    # output rather than failing the whole capture.
                    return None
            if len(text) > self._CAP_BYTES:
                # Keep the tail: the end of a log holds the error, the start
                # rarely does.
                text = "…[truncated]…\n" + text[-self._CAP_BYTES:]
            return text or None

        return command, _read("stdout", "stdout_path"), _read("stderr", "stderr_path"), exit_code

    # ── Poll-based Execution ───────────────────────────────────────────

    async def _poll_step(self, running: _RunningStep) -> None:
        """Poll a step's check() method until it completes.

        Used for steps whose startup() already launched the work (detached
        process, remote request, timer) and that report progress through
        `check(state)`.

        Args:
            running: Tracking record; its `state` is passed to `check()` and
                may be mutated by the step across polls.

        Returns:
            When `check()` reports SUCCESS.

        Raises:
            StepCheckFailed: `check()` returned FAILED.
            asyncio.CancelledError: Propagated from the sleep when the step is
                cancelled; `execute()` converts it into step.failed.

        AI Note: There is no local timeout or max-iteration guard — a step
        whose check() never leaves RUNNING loops forever. The only backstop is
        the server runner's 7200s per-step `wait_for`, after which the server
        gives up but this loop keeps polling until the agent restarts.
        check() is called synchronously, so it must not block the event loop.
        """
        running.task = asyncio.current_task()

        while True:
            result = running.step.check(running.state)
            if result == StepResult.SUCCESS:
                return
            if result == StepResult.FAILED:
                raise StepCheckFailed(running.state.get("error") or "Step check() returned FAILED")
            # Still RUNNING — wait and poll again
            # 1s fixed interval: fast enough for UI responsiveness, cheap
            # enough for long-running jobs. This is also the cancellation
            # point for poll steps.
            await asyncio.sleep(1.0)


# ── Internal Types ─────────────────────────────────────────────────────


class _RunningStep:
    """Tracks a single step execution in progress.

    Mutable scratch record shared between `execute()`, `_run_subprocess()` /
    `_poll_step()` (which fill in `process` and `task`), `cancel()` (which
    reads them), and `_capture()` (which reads `state` and `captured`).
    Lifetime is exactly one entry in `StepExecutor._running_steps`.

    Attributes:
        job_id: Server-side job UUID as a string.
        step_index: Position of this step within the job's step list.
        step: The instantiated `FlowStep`; holds no cross-step state itself.
        state: The dict returned by `startup()`. Must stay JSON-serializable
            because it is sent to the server for crash recovery. Conventional
            keys: "command", "work_dir", "exit_code", "_command_str",
            "stdout_path", "stderr_path", plus the step's OUTPUT_KEYS.
        params: OS-resolved parameters, kept for debugging/introspection.
        process: The child process for command steps; `None` for poll steps.
        task: The asyncio task running `execute()`, so `cancel()` can unwind it.
        captured: In-memory stdout/stderr line buffers for the per-job log.
    """

    # AI Note: __slots__ makes this record unable to accept ad-hoc attributes.
    # Any new field must be added to this tuple as well as __init__, or
    # assigning it raises AttributeError at runtime.
    __slots__ = ("job_id", "step_index", "step", "state", "params", "process", "task", "captured")

    def __init__(
        self,
        job_id: str,
        step_index: int,
        step: FlowStep,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> None:
        """Create a tracking record for a step that has just completed startup().

        Args:
            job_id: Server-side job UUID as a string.
            step_index: Index of this step within the job.
            step: The instantiated step object.
            state: `startup()`'s return value (see class docstring).
            params: OS-resolved parameters passed to `startup()`.
        """
        self.job_id = job_id
        self.step_index = step_index
        self.step = step
        self.state = state
        self.params = params
        # Populated later by _run_subprocess/_poll_step; cancel() tolerates None.
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        # Unbounded until _capture() truncates — see _CAP_BYTES.
        self.captured: dict[str, list[str]] = {"stdout": [], "stderr": []}


class SubprocessError(Exception):
    """Raised when a subprocess exits with a non-zero code.

    Carries the child's exit status so `execute()` can forward it to the
    server on the step.failed message (via `getattr(exc, "returncode", None)`)
    instead of reporting a bare error string.
    """

    def __init__(self, message: str, returncode: int) -> None:
        """Record the failure message and the child's exit status.

        Args:
            message: Human-readable description, surfaced as the step error.
            returncode: Non-zero exit code of the subprocess.
        """
        super().__init__(message)
        self.returncode = returncode


class StepCheckFailed(Exception):
    """Raised when a step's check() method returns FAILED.

    Distinguishes a step-defined validation failure (bad output, missing
    artifact) from a subprocess-level failure. Carries no exit code, so the
    step.failed message reports whatever `state["exit_code"]` holds — commonly
    0, since a command can exit cleanly yet fail its check.

    The message is `state["error"]` when the step recorded one (e.g. a
    startup()-time setup failure that check() turns into FAILED rather than
    raising), falling back to a generic string otherwise — without this, the
    specific diagnostic a step went out of its way to record is discarded and
    the user sees only "Step check() returned FAILED".
    """
