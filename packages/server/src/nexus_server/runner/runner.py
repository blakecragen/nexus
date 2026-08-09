"""Async job runner — orchestrates step execution across distributed agents.

Adapted from HVE-Automation-Worker's runner with distributed execution model:
steps are dispatched to remote agents via WebSocket rather than running locally.

Role in the system
------------------
This is the server's execution engine. Exactly one :class:`JobRunner` exists
per process (built in ``nexus_server.main.lifespan``, stored on
``app.state.runner``). It owns one asyncio task per in-flight job and is the
single writer of job/step terminal state to the database — the WebSocket layer
deliberately does not write job status, it just forwards agent notifications
here.

Execution model
---------------
Steps come from ``Job.steps_config`` (a list of ``{step, params, on_fail,
target_*}`` dicts) and run strictly sequentially per job. Two kinds exist:

- **Control-plane steps** (``step_cls.REQUIRES_NODE is False``, e.g. sleep,
  jump): run in this process via ``startup()`` / ``check()`` polling.
- **Remote steps**: serialized into an ``ExecuteStepCommand`` and pushed to a
  chosen agent's WebSocket. The runner then *blocks on an asyncio.Event* until
  the WS handler calls :meth:`JobRunner.on_step_completed` /
  :meth:`JobRunner.on_step_failed`.

Neighbouring modules
--------------------
- ``api/routes/jobs.py`` → :meth:`JobRunner.submit_job` / :meth:`cancel_job`.
- ``api/routes/ws.py`` → :meth:`on_step_completed` / :meth:`on_step_failed`
  (the completion half of the round trip) and provides ``send_to_agent``.
- ``runner/scheduler.py`` → node placement for each remote step.
- ``runner/resume.py`` → re-submits jobs after a server restart.
- ``db/ops.py`` → all persistence (job status, step_runs, aggregated log).

AI Note: the runner keeps per-step completion state in *process memory*
(``_step_events`` / ``_step_results``), not in the DB. That makes it
single-process only: running two server replicas against one database would
have replica A dispatch a step whose completion message lands on replica B,
where no waiting Event exists, and the step would hang until the 2h timeout.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_common.agent_protocol import ExecuteStepCommand
from nexus_common.steps.base import StepContext
from nexus_common.steps.registry import get_step
from nexus_server.db import ops
from nexus_server.db.models import Job
from nexus_server.runner.scheduler import find_node_for_step

logger = logging.getLogger(__name__)


def _format_log_block(idx: int, step_name: str, node_label: str, status: str, result: dict) -> str:
    """Render one step's command + output as a terminal-log block.

    Produces the human-readable chunk that gets appended to ``Job.log_text``
    and shown verbatim in the UI's job terminal view. Format is intentionally
    shell-transcript-like: a banner, the command with a ``$`` prompt, stdout,
    an optional stderr section, then a status footer.

    Args:
        idx: Zero-based index of the step within ``Job.steps_config``.
        step_name: Registry name of the step (e.g. ``"shell_command"``).
        node_label: Hostname of the executing node, or ``"control-plane"`` for
            steps that ran in the server process.
        status: ``"success"`` or ``"failed"`` — anything not ``"success"`` is
            treated as a failure for the purposes of emitting ``error:``.
        result: The runner's result dict. Reads the optional keys ``command``,
            ``stdout``, ``stderr``, ``error`` and ``exit_code``; all are
            tolerated as missing or ``None`` (control-plane steps supply none
            of them, yielding a banner + status footer only).

    Returns:
        The formatted block, always terminated with a blank line so successive
        blocks are visually separated when concatenated.

    AI Note: ``error`` is only emitted when there is no stderr — stderr is
    assumed to already contain the real diagnostic, and printing both just
    duplicates it. Also note the exit-code check is ``is not None``, so a
    legitimate ``exit_code=0`` on a failed step is still shown.
    """
    lines = [f"===== [step {idx}] {step_name} on {node_label} ====="]
    command = result.get("command")
    if command:
        lines.append(f"$ {command}")
    stdout = (result.get("stdout") or "").rstrip("\n")
    stderr = (result.get("stderr") or "").rstrip("\n")
    if stdout:
        lines.append(stdout)
    if stderr:
        lines.append("--- stderr ---")
        lines.append(stderr)
    if status != "success":
        err = result.get("error")
        if err and not stderr:
            lines.append(f"error: {err}")
    ec = result.get("exit_code")
    ec_part = f"exit code: {ec}  " if ec is not None else ""
    lines.append(f"[{ec_part}status: {status}]")
    return "\n".join(lines) + "\n\n"


class JobRunner:
    """Manages the lifecycle of job execution.

    For each running job, advances through its steps sequentially:
    1. Find a suitable node for the step
    2. Dispatch the step to the agent via WebSocket
    3. Wait for completion (agent sends step.completed / step.failed)
    4. Merge outputs into job context
    5. Advance to next step or complete the job

    Instantiated once per server process in ``main.lifespan`` and reachable as
    ``app.state.runner``. Jobs run concurrently with each other (one task
    each), but the steps *within* a job are strictly serial.

    Attributes:
        _ws: The WebSocket ``ConnectionManager`` from ``api/routes/ws.py``.
            Only ``send_to_agent(node_id, dict) -> bool`` is used, which is why
            tests can substitute a small fake.
        _cred_manager: Optional ``CredentialManager``. When ``None``, steps
            requesting ``credential_name`` silently run without credentials
            (the name is still stripped from params).
        _active_jobs: job_id → the asyncio task running it. Used by
            :meth:`cancel_job` and cleaned up in ``_run_job``'s ``finally``.
        _step_events: ``"{job_id}:{step_index}"`` → Event signalled by the WS
            callbacks to wake the dispatching coroutine.
        _step_results: same key → the result dict deposited by the WS
            callbacks just before signalling the Event.

    AI Note: the ``"{job_id}:{step_index}"`` key format is a cross-module
    contract. ``on_step_completed``/``on_step_failed`` receive ``job_id`` as a
    *string* straight off the wire, while ``_execute_remote_step`` builds its
    key from a ``UUID`` via f-string. Those must stringify identically —
    changing the Job PK representation (or f-string interpolation of it) on
    either side silently desynchronizes the two halves and every remote step
    hangs for two hours.
    """

    def __init__(self, ws_manager, credential_manager=None):
        """Wire the runner to its collaborators.

        Args:
            ws_manager: Object exposing ``async send_to_agent(node_id: str,
                message: dict) -> bool``. In production this is the module-level
                ``ConnectionManager`` singleton in ``api/routes/ws.py``.
            credential_manager: Optional service used to resolve a step's
                ``credential_name`` into a decrypted client config that is sent
                to the agent. Omitted in tests and in deployments without
                stored credentials.
        """
        self._ws = ws_manager  # WebSocket connection manager
        self._cred_manager = credential_manager
        self._active_jobs: dict[UUID, asyncio.Task] = {}
        self._step_events: dict[str, asyncio.Event] = {}  # job_id:step_idx -> event
        self._step_results: dict[str, dict] = {}  # job_id:step_idx -> result

    async def submit_job(self, db: AsyncSession, job_id: UUID) -> None:
        """Start processing a job asynchronously.

        Verifies the job exists, then spawns a detached task running
        :meth:`_run_job`. Returns as soon as the task is scheduled — callers
        (the ``POST /api/jobs`` handler and ``resume_active_jobs``) never wait
        for the job to finish.

        Args:
            db: Session used *only* for the existence check. ``_run_job`` opens
                its own session, because ``db`` here belongs to a request scope
                that is closed long before the job completes.
            job_id: Job to run.

        Side effects:
            Creates an asyncio task and registers it in ``_active_jobs``.

        AI Note: a missing job is logged and swallowed rather than raised, so a
        deleted-then-resumed job cannot take down startup or a request. Note
        also that no dedupe check happens here — calling ``submit_job`` twice
        for one job_id starts a second task and orphans the first entry in
        ``_active_jobs``, making it uncancellable.
        """
        job = await ops.get_job_by_id(db, job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        task = asyncio.create_task(self._run_job(job_id))
        self._active_jobs[job_id] = task

    async def cancel_job(self, db: AsyncSession, job_id: UUID) -> None:
        """Cancel a running job.

        Cancels the backing asyncio task (if this process owns one) and marks
        the job ``cancelled`` with a completion timestamp.

        Args:
            db: Session used for the status write.
            job_id: Job to cancel.

        Side effects:
            Task cancellation plus a DB update. The status write happens even
            when no task is found, so a job that is queued-but-not-running (or
            whose task died with the previous server process) can still be
            closed out.

        AI Note: cancellation is best-effort on the agent side. ``task.cancel()``
        unwinds the server-side coroutine, but no cancel message is sent over
        the WebSocket — a step already executing on a node keeps running to
        completion, and its late ``step.completed`` lands in ``_step_results``
        with no waiter (harmless; the entry is only reaped by the next
        ``_execute_remote_step`` for the same key, so it leaks otherwise).

        AI Note: the ``update_job`` call races the cancelled task's ``finally``
        / exception handling. The runner writes terminal status in ``_run_job``
        only on the success and ``on_fail="stop"`` paths — ``CancelledError`` is
        caught and merely logged — so the ``cancelled`` status written here is
        not overwritten. Adding a status write to that ``except
        asyncio.CancelledError`` branch would introduce a real race.
        """
        task = self._active_jobs.get(job_id)
        if task and not task.done():
            task.cancel()
        await ops.update_job(db, job_id, status="cancelled",
                             completed_at=datetime.now(timezone.utc))

    def on_step_completed(self, job_id: str, step_index: int, outputs: dict,
                          command: str | None = None, stdout: str | None = None,
                          stderr: str | None = None, exit_code: int | None = None) -> None:
        """Called by WebSocket handler when agent reports step completion.

        Deposits the result where the waiting ``_execute_remote_step``
        coroutine will find it, then wakes that coroutine. Deliberately
        synchronous and non-blocking so the WS receive loop is never stalled by
        job bookkeeping.

        Args:
            job_id: Job UUID **as a string**, exactly as it arrived in the
                agent's ``step.completed`` message.
            step_index: Index of the completed step within ``steps_config``.
            outputs: The step's declared ``OUTPUT_KEYS`` extracted from agent
                state; merged into the job context by the run loop.
            command: Command line the agent actually executed, for the log.
            stdout: Captured standard output, for the log.
            stderr: Captured standard error, for the log.
            exit_code: Process exit code when the step wrapped a subprocess.

        Side effects:
            Writes ``_step_results[key]`` and sets ``_step_events[key]``. No DB
            writes — the runner loop owns all persistence.

        AI Note: the result is stored *before* the Event is set. That ordering
        matters: the waiter reads ``_step_results`` immediately on wake, so
        signalling first would allow a read of a missing key.

        AI Note: if no Event is registered (job cancelled, step already timed
        out, or a stale message from a re-dispatched step after a server
        restart) the result is stored anyway and nothing consumes it. That is a
        deliberate no-op rather than an error, but it does mean the entry stays
        in ``_step_results`` until some later step reuses the same key.
        """
        key = f"{job_id}:{step_index}"
        self._step_results[key] = {
            "status": "success", "outputs": outputs,
            "command": command, "stdout": stdout, "stderr": stderr, "exit_code": exit_code,
        }
        event = self._step_events.get(key)
        if event:
            event.set()

    def on_step_failed(self, job_id: str, step_index: int, error: str,
                       command: str | None = None, stdout: str | None = None,
                       stderr: str | None = None, exit_code: int | None = None) -> None:
        """Called by WebSocket handler when agent reports step failure.

        Mirror image of :meth:`on_step_completed`: records a ``"failed"``
        result and wakes the dispatcher, which then applies the step's
        ``on_fail`` policy (``stop`` vs ``continue``).

        Args:
            job_id: Job UUID as a string, as received on the wire.
            step_index: Index of the failed step within ``steps_config``.
            error: Agent-supplied failure message; becomes ``Job.error`` when
                ``on_fail="stop"``.
            command: Command line the agent executed, for the log.
            stdout: Captured standard output, for the log.
            stderr: Captured standard error, for the log.
            exit_code: Process exit code, if the step wrapped a subprocess.

        Side effects:
            Writes ``_step_results[key]`` and sets ``_step_events[key]``. No DB
            writes here — a step failure only becomes a *job* failure inside
            ``_run_job``, which is what allows ``on_fail="continue"``.
        """
        key = f"{job_id}:{step_index}"
        self._step_results[key] = {
            "status": "failed", "error": error,
            "command": command, "stdout": stdout, "stderr": stderr, "exit_code": exit_code,
        }
        event = self._step_events.get(key)
        if event:
            event.set()

    async def _run_job(self, job_id: UUID) -> None:
        """Main job execution loop.

        Runs as a detached asyncio task for the lifetime of one job. Walks
        ``steps_config`` from ``Job.current_step``, executing each step locally
        or remotely, threading outputs through a :class:`StepContext`, honoring
        jump directives and ``on_fail`` policy, and writing terminal job status
        exactly once at the end.

        Args:
            job_id: Job to execute. Everything else is re-read from the DB, so
                this task is self-contained and safe to start from either a
                request handler or startup recovery.

        Side effects:
            Heavy DB writer — job status/progress/context, a ``StepRun`` row per
            step, and incremental appends to the aggregated job log. Also
            triggers agent-side subprocess execution for remote steps.

        AI Note: starts at ``job.current_step``, not 0. That is what makes
        ``resume_active_jobs`` skip already-finished steps after a restart, and
        it is why ``current_step`` must be written *before* a step runs (see
        the ``update_job`` below) rather than after.
        """
        # AI Note: the import is function-local on purpose. `db.session` holds
        # a module-global engine created by `init_db()` during lifespan startup;
        # importing at module scope would bind this module to session state
        # that may not exist yet at import time (and breaks test fixtures that
        # re-init the engine per test).
        from nexus_server.db.session import get_session_factory

        session_factory = get_session_factory()
        # AI Note: a dedicated, long-lived session per job — NOT the request
        # session that submit_job used. A job can run for hours, far outliving
        # any HTTP request scope.
        async with session_factory() as db:
            try:
                job = await ops.get_job_by_id(db, job_id)
                if not job:
                    return

                await ops.update_job(db, job_id, status="running",
                                     started_at=datetime.now(timezone.utc))

                steps_config = job.steps_config
                # AI Note: context is seeded from persisted `context_data`, so a
                # resumed job still sees outputs produced by steps that ran
                # before the crash (including the `_last_failed` flag).
                context = StepContext(outputs=job.context_data or {})
                idx = job.current_step

                # AI Note: `idx` is mutated by jump steps, so this cannot be a
                # `for` loop over steps_config. A jump target that points
                # backwards creates an intentional loop with no iteration cap —
                # a malformed template can spin forever; the only backstops are
                # job cancellation and the per-step timeout.
                while idx < len(steps_config):
                    step_cfg = steps_config[idx]
                    step_name = step_cfg["step"]
                    step_params = step_cfg.get("params", {})
                    # Default "stop": an unannotated failing step aborts the job.
                    on_fail = step_cfg.get("on_fail", "stop")

                    step_cls = get_step(step_name)

                    # Update job progress
                    # AI Note: written before execution so crash recovery
                    # resumes at the step that was in flight, not the one after.
                    await ops.update_job(db, job_id, current_step=idx)

                    # Create step run record
                    # AI Note: a fresh StepRun row per visit, so a jump-created
                    # loop produces several rows with the same step_index. The
                    # WS handler resolves this with `get_latest_step_run`.
                    step_run = await ops.create_step_run(
                        db, job_id=job_id, step_index=idx, step_name=step_name,
                        input_params=step_params,
                    )

                    if not step_cls.REQUIRES_NODE:
                        # Control-plane step — execute locally
                        result = await self._execute_local_step(
                            db, step_cls, step_params, context, step_run.id,
                        )
                    else:
                        # Remote step — dispatch to agent. Step-level targets
                        # override job-level; this lets one job hit multiple
                        # gem5 hosts on different OSes.
                        step_target_node = step_cfg.get("target_node_id") or job.target_node_id
                        step_target_pool = step_cfg.get("target_pool_id") or job.target_pool_id
                        # AI Note: target_os has no job-level fallback by
                        # design — an OS pin is always a per-step decision.
                        step_target_os = step_cfg.get("target_os")
                        result = await self._execute_remote_step(
                            db, job, step_cls, step_name, step_params, context,
                            step_run.id, idx,
                            target_node_id=step_target_node,
                            target_pool_id=step_target_pool,
                            target_os=step_target_os,
                        )

                    # Append this step's command + output to the per-job log
                    # (committed incrementally so a crash leaves a partial log).
                    node_label = result.get("node_label", "control-plane")
                    await ops.append_job_log(
                        db, job_id,
                        _format_log_block(idx, step_name, node_label, result["status"], result),
                    )

                    if result["status"] == "success":
                        outputs = result.get("outputs", {})
                        context.outputs.update(outputs)
                        # A successful step clears the prior failure flag so a
                        # later jump(on="fail") doesn't fire on a stale signal.
                        context.outputs.pop("_last_failed", None)
                        await ops.update_step_run(
                            db, step_run.id, status="success",
                            output_params=outputs,
                            finished_at=datetime.now(timezone.utc),
                        )
                        # Persist the merged context so a resumed job sees the
                        # same variable bindings downstream steps expect.
                        await ops.update_job(db, job_id, context_data=context.outputs)

                        # Check for jump directive
                        # AI Note: `continue` skips the `idx += 1` at the bottom
                        # — the jump target is an absolute step index, so
                        # jump_target=0 restarts the job's step list. Only
                        # successful steps can jump; a failed jump step falls
                        # through to normal on_fail handling.
                        jump_target = result.get("jump_target")
                        if jump_target is not None:
                            idx = jump_target
                            continue
                    else:
                        error = result.get("error", "Step failed")
                        await ops.update_step_run(
                            db, step_run.id, status="failed", error=error,
                            finished_at=datetime.now(timezone.utc),
                        )
                        if on_fail == "stop":
                            await ops.update_job(
                                db, job_id, status="failed", error=error,
                                completed_at=datetime.now(timezone.utc),
                            )
                            return
                        # on_fail="continue": flag the failure for downstream
                        # conditional steps (e.g. jump on="fail") and persist.
                        context.outputs["_last_failed"] = True
                        await ops.update_job(db, job_id, context_data=context.outputs)

                    idx += 1

                # All steps completed
                # AI Note: reaching here means idx ran past the end of
                # steps_config. The job is "completed" even if some steps failed
                # with on_fail="continue" — per-step outcomes live on the
                # StepRun rows and in the job log, not in the job status.
                await ops.update_job(
                    db, job_id, status="completed",
                    completed_at=datetime.now(timezone.utc),
                )

            except asyncio.CancelledError:
                # AI Note: intentionally does NOT write job status. cancel_job()
                # already wrote status="cancelled" before cancelling this task;
                # writing here would race it (and an await on a cancelled task
                # would likely re-raise anyway).
                logger.info(f"Job {job_id} cancelled")
            except Exception as e:
                logger.exception(f"Job {job_id} failed with error")
                # AI Note: a fresh session is required here. The original `db`
                # may be in a broken/rolled-back transaction after whatever
                # raised, so reusing it to record the failure would itself fail
                # and lose the job's terminal state.
                async with session_factory() as db2:
                    await ops.update_job(
                        db2, job_id, status="failed", error=str(e),
                        completed_at=datetime.now(timezone.utc),
                    )
            finally:
                # AI Note: must run on every exit path, including cancellation —
                # a stale entry here would make a later cancel_job() call
                # cancel() an already-finished task and, worse, keep the task
                # object (and its captured frames) alive for the process
                # lifetime.
                self._active_jobs.pop(job_id, None)

    async def _execute_local_step(
        self, db: AsyncSession, step_cls, params: dict,
        context: StepContext, step_run_id: UUID,
    ) -> dict:
        """Execute a control-plane step locally (e.g., sleep, jump).

        Runs the step's ``startup()`` once, persists the returned state for
        crash recovery, then polls ``check()`` until it reports a terminal
        :class:`StepResult`. Used for every step whose class sets
        ``REQUIRES_NODE = False`` — these need no agent and no node placement.

        Args:
            db: Job-scoped session (used for the ``StepRun`` updates).
            step_cls: Step class resolved from the registry; instantiated here.
            params: Raw, unresolved params from ``steps_config``. Template
                references are expanded against ``context`` below.
            context: Accumulated job outputs, used for ``${...}`` resolution.
            step_run_id: Row to annotate with running status and saved state.

        Returns:
            A result dict shaped like the remote path's so ``_run_job`` can
            treat both uniformly: ``{"status": "success", "outputs": {...}}``
            optionally with ``"jump_target"``, or ``{"status": "failed",
            "error": str}``. Never raises — exceptions are converted into a
            failed result so a bad control step fails the *step*, not the task.

        Side effects:
            Writes ``status="running"``, ``started_at`` and ``state`` to the
            step run. Does not write terminal step status — ``_run_job`` does
            that from the returned dict.

        AI Note: the local path returns no ``node_label``/``command``/``stdout``
        keys, which is why ``_run_job`` defaults the label to
        ``"control-plane"`` and ``_format_log_block`` tolerates missing keys.
        """
        step = step_cls()
        try:
            await ops.update_step_run(
                db, step_run_id, status="running",
                started_at=datetime.now(timezone.utc),
            )
            resolved = context.resolve(params)
            state = step.startup(resolved, context)

            # Save state for crash recovery
            # AI Note: persisted but not currently re-read on resume — a
            # resumed job re-runs startup() from scratch (see resume.py). The
            # write exists so recovery can be made state-aware later without a
            # schema change, and it is what the WS handler mirrors for remote
            # steps via `step.started`.
            await ops.update_step_run(db, step_run_id, state=state)

            # Poll until complete
            # AI Note: import is function-local to mirror the rest of the
            # module's deferred-import style and to keep the enum out of this
            # module's import graph; leave it here.
            from nexus_common.models.enums import StepResult
            while True:
                # AI Note: check() is a *synchronous* call inside an async
                # coroutine — a control step that blocks (sleeps, does I/O)
                # stalls the entire event loop, i.e. every other job on this
                # server. Control steps must return promptly and express
                # waiting as RUNNING + state, not as a blocking call.
                result = step.check(state)
                if result == StepResult.SUCCESS:
                    # Only keys the step class declares in OUTPUT_KEYS leak
                    # into the job context; internal state stays private.
                    outputs = {k: state.get(k) for k in step_cls.OUTPUT_KEYS if k in state}
                    # AI Note: the jump loop counter must persist across visits
                    # to the same jump step, but its key is computed at runtime
                    # (from target_step+on) and so cannot be declared in the
                    # static OUTPUT_KEYS list. Copy it across explicitly. This
                    # is the runner half of the max_jumps guard — without it the
                    # counter resets every visit and a backward jump never
                    # terminates. See nexus_steps/flow/jump.py for the other half.
                    counter_key = state.get("jump_counter_key")
                    if counter_key is not None:
                        outputs[counter_key] = state.get("jump_count")
                    # AI Note: `__jump_target` is the flow-control channel
                    # between a jump step and the run loop. It is deliberately
                    # read from raw state (not OUTPUT_KEYS) so it never lands
                    # in the persisted job context. `is not None` rather than
                    # truthiness — jumping to step 0 is legal.
                    jump_target = state.get("__jump_target")
                    ret = {"status": "success", "outputs": outputs}
                    if jump_target is not None:
                        ret["jump_target"] = jump_target
                    return ret
                elif result == StepResult.FAILED:
                    return {"status": "failed", "error": state.get("error", "Step failed")}
                # AI Note: fixed 1s poll interval, and no overall timeout on
                # this loop — a control step that never reaches a terminal
                # StepResult hangs the job forever (unlike remote steps, which
                # are bounded by the 2h wait_for). Cancellation is the only way
                # out.
                await asyncio.sleep(1)

        except Exception as e:
            # AI Note: broad catch is deliberate — the run loop's contract is
            # "a step returns a result dict", and letting a step's exception
            # escape would fail the whole job through the outer handler,
            # bypassing this step's on_fail="continue" policy.
            return {"status": "failed", "error": str(e)}

    async def _execute_remote_step(
        self, db: AsyncSession, job: Job, step_cls, step_name: str,
        params: dict, context: StepContext, step_run_id: UUID, step_index: int,
        target_node_id=None, target_pool_id=None, target_os: str | None = None,
    ) -> dict:
        """Dispatch a step to a remote agent and wait for completion.

        Step-level target_* overrides take precedence over the job-level
        targets stored on the Job row; the caller is responsible for that
        precedence.

        Sequence: pick a node → mark the step run running → resolve params
        (context templates, then OS variants) → resolve credentials → push an
        ``ExecuteStepCommand`` over the agent's WebSocket → block on an Event
        until the WS handler reports the outcome.

        Args:
            db: Job-scoped session, used for node lookup, step-run updates and
                credential decryption.
            job: The Job row; only ``job.id`` is used, to build the wire
                message and the completion-event key.
            step_cls: Step class from the registry, consulted for
                ``SUPPORTED_OS`` (via the scheduler) and ``resolve_for_os``.
            step_name: Registry name sent to the agent, which re-resolves it
                against its own copy of the registry.
            params: Raw params from ``steps_config``.
            context: Job outputs so far, for ``${...}`` resolution.
            step_run_id: StepRun row to stamp with node and running status.
            step_index: Position in ``steps_config``; forms half of the
                completion-event key and is echoed back by the agent.
            target_node_id: Already-merged node pin (step overrides job).
            target_pool_id: Already-merged pool target (step overrides job).
            target_os: Per-step OS pin, if any.

        Returns:
            The agent's result dict (``status`` plus ``outputs``/``error`` and
            the log fields), augmented with ``node_label``. On any local
            failure — no node, unknown credential, agent offline, timeout —
            returns a ``{"status": "failed", "error": ...}`` dict instead of
            raising.

        Side effects:
            Network send to an agent (which spawns real work on that machine),
            DB writes to the step run, and registration/cleanup of entries in
            ``_step_events`` / ``_step_results``.

        AI Note: this coroutine can be parked for up to two hours. It holds the
        job's ``AsyncSession`` open the whole time (the session is passed in
        from ``_run_job``), so long-running jobs each pin one DB connection —
        relevant when sizing the pool against expected job concurrency.
        """
        # Find a suitable node honoring per-step targeting overrides.
        # AI Note: placement happens per step, not per job, so a job can move
        # between machines mid-flight. Steps that depend on files left behind
        # by an earlier step must pin target_node_id.
        node = await find_node_for_step(
            db, step_name,
            target_pool_id=target_pool_id,
            target_node_id=target_node_id,
            target_os=target_os,
        )
        if not node:
            # AI Note: no queueing/retry — if nothing matches right now the
            # step fails immediately rather than waiting for a node to come
            # online. The qualifier string exists because "no available node"
            # alone is nearly undebuggable when targeting is in play.
            target_desc = []
            if target_os:
                target_desc.append(f"os={target_os}")
            if target_node_id:
                target_desc.append(f"node={target_node_id}")
            if target_pool_id:
                target_desc.append(f"pool={target_pool_id}")
            qualifier = f" ({', '.join(target_desc)})" if target_desc else ""
            return {
                "status": "failed",
                "error": f"No available node for step '{step_name}'{qualifier}",
            }

        await ops.update_step_run(
            db, step_run_id, status="running", node_id=node.id,
            started_at=datetime.now(timezone.utc),
        )

        # Resolve OS-specific params
        # AI Note: order matters — context templates are expanded first, then
        # OS variants are picked using the *chosen node's* os_type. Swapping
        # these would apply OS selection to unexpanded placeholders.
        resolved = context.resolve(params)
        resolved = step_cls.resolve_for_os(resolved, node.os_type)

        # Resolve credential if needed
        # AI Note: `pop` (not `get`) is a security boundary — the credential
        # *name* is stripped from the params sent over the wire, and only the
        # decrypted client config travels in the dedicated
        # `credential_config` field. Changing this to `get` would leak the
        # reference into step params and into agent-side logs.
        cred_config = None
        cred_name = resolved.pop("credential_name", None)
        if cred_name and self._cred_manager:
            # AI Note: when `_cred_manager` is None the name is silently
            # dropped and the step runs uncredentialed rather than failing.
            # Deliberate for credential-less deployments/tests, but it means a
            # misconfigured server degrades quietly instead of loudly.
            try:
                cred_config = await self._cred_manager.get_by_name(db, cred_name)
            except KeyError:
                return {"status": "failed", "error": f"Credential '{cred_name}' not found"}

        # Send execution command to agent
        command = ExecuteStepCommand(
            job_id=str(job.id),
            step_index=step_index,
            step_name=step_name,
            params=resolved,
            credential_config=cred_config,
        )

        # Set up event for completion notification
        # AI Note: the Event MUST be registered before send_to_agent — a fast
        # agent can complete and call on_step_completed() before this coroutine
        # resumes, and without the Event already in the dict that notification
        # would be dropped and the step would hang until the 2h timeout.
        key = f"{job.id}:{step_index}"
        self._step_events[key] = asyncio.Event()

        try:
            # send_to_agent → ws.send_json, which JSON-encodes once. Pass a dict
            # (not model_dump_json()'s string) or the agent receives a quoted
            # string and `data.get(...)` blows up.
            sent = await self._ws.send_to_agent(str(node.id), command.model_dump(mode="json"))
            if not sent:
                # The node row said online but no live socket exists (agent
                # died without a clean disconnect, or heartbeat is stale).
                return {"status": "failed", "error": f"Agent for node {node.hostname} not connected"}

            # Wait for agent to report completion
            # AI Note: 7200s = 2h hard ceiling per step, chosen for long gem5
            # simulations. It is a wall-clock cap on the *whole* step, not an
            # idle timeout — a step still producing output at 2h is killed
            # server-side (the agent keeps running it, orphaned). Raise this in
            # lockstep with the "(2h)" message below.
            await asyncio.wait_for(self._step_events[key].wait(), timeout=7200)

            result = self._step_results.pop(key, {"status": "failed", "error": "No result received"})
            # node_label feeds the job log's "on <host>" banner; fall back to
            # the UUID for a node that never reported a hostname.
            result["node_label"] = node.hostname or str(node.id)
            return result

        except asyncio.TimeoutError:
            return {"status": "failed", "error": "Step execution timed out (2h)"}
        finally:
            # AI Note: unconditional cleanup of both maps prevents unbounded
            # growth across a long-lived server and guarantees a late/duplicate
            # agent message for this key cannot be mistaken for the result of a
            # future step that reuses the same job_id:step_index (which jump
            # loops do).
            self._step_events.pop(key, None)
            self._step_results.pop(key, None)
