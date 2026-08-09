"""Integration tests for crash recovery and the runner's lifecycle surface.

SUT:
    * ``packages/server/src/nexus_server/runner/resume.py`` —
      ``resume_active_jobs`` (startup crash recovery: which jobs are re-adopted,
      where they re-enter their step list, and what happens when a resubmission
      blows up).
    * ``packages/server/src/nexus_server/runner/runner.py`` — the lifecycle and
      wire-protocol halves of ``JobRunner`` that the run-loop suite does not
      touch: ``submit_job``, ``cancel_job``, the ``on_step_completed`` /
      ``on_step_failed`` WebSocket callbacks, the ``_execute_remote_step``
      dispatch / credential / disconnect / timeout / bookkeeping paths, and the
      ``_format_log_block`` rendering edge cases.

Division of labour with the other runner suite
    ``tests/integration/test_runner_scheduler.py`` owns node selection
    (``find_node_for_step`` / ``_node_matches_step``) and the ``_run_job``
    control-flow semantics (jump/loop, ``on_fail``, context accumulation) plus
    the two happy-path shapes of ``_format_log_block`` and remote dispatch. This
    file deliberately picks up everything *around* that loop: how a job gets
    handed to the runner, how it gets taken away again, how the agent's
    completion messages are routed back into a parked coroutine, and the failure
    modes of the dispatch itself.

Strategy / stubs
    * The database is real (in-memory SQLite via the ``db`` fixture) and all
      persistence goes through ``nexus_server.db.ops``.
    * The agent WebSocket is the only true external boundary and is replaced by
      ``RecordingWsManager``. Because a real agent would call back
      asynchronously, tests that need a *completed* remote step spawn
      ``_impersonate_agent``, which polls for the runner's completion Event and
      then invokes the real ``on_step_completed`` / ``on_step_failed``
      callbacks. Nothing inside the runner is mocked.
    * The one exception to "no time mocking" is ``shrink_step_timeout``, which
      rewrites *only* the runner's 7200-second per-step ceiling to 50 ms so the
      real ``asyncio.TimeoutError`` branch can be exercised in a normal test
      run. The wait itself is genuine.
    * ``resume_active_jobs``'s error branch needs a *collaborator* that raises.
      ``StubRunner`` duck-types ``JobRunner.submit_job`` for those two tests
      only; every other resume test drives the real ``JobRunner``.

Why ``app`` is requested by tests that never issue an HTTP request
    Requesting ``app`` repoints ``nexus_server.db.session._engine`` /
    ``._session_factory`` at the in-memory database. ``JobRunner._run_job`` runs
    in a detached task with no request scope and opens its own session through
    those module globals, so without ``app`` the runner would operate on a
    different (real, on-disk) database than the one the test seeded.

Cross-session staleness
    ``_run_job`` writes through its own session. Both factories use
    ``expire_on_commit=False``, so the ``db`` fixture's identity map keeps
    handing back the pre-run row. Every test calls ``db.expunge_all()`` after
    driving the runner and before asserting, which forces a genuine re-SELECT.
    ``expire_all()`` would instead arm a lazy refresh that raises
    ``MissingGreenlet`` on an async session.

Timing / determinism notes
    ``sleep(0)`` marks a step that must run; ``sleep(999)`` marks a step that
    must NOT run (or a job that must stay parked so it can be cancelled), so an
    incorrectly-taken branch shows up as a hang rather than a subtle assertion
    failure. Every detached task a test creates is awaited or cancelled before
    the test returns — pytest-asyncio tears the event loop down immediately
    afterwards, and a surviving task would either warn or fail a later test
    against the disposed engine.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from nexus_common.steps.base import StepContext
from nexus_common.steps.registry import get_step
from nexus_server.db import ops
from nexus_server.runner.resume import resume_active_jobs
from nexus_server.runner.runner import JobRunner, _format_log_block


# ── Fakes / helpers ──────────────────────────────────────────────────────


class RecordingWsManager:
    """Stands in for ``api/routes/ws.ConnectionManager`` — the only stub.

    Attributes:
        connected: What ``send_to_agent`` reports. ``False`` reproduces the
            production case of a node row that says "online" while its socket is
            already gone.
        sent: Every dispatch as ``(node_id, payload)``, so tests can assert both
            that a dispatch did *not* happen (fail-before-send paths) and the
            exact payload the agent would have received.
    """

    def __init__(self, connected: bool = True):
        """Create a fake WS manager.

        Args:
            connected: Whether ``send_to_agent`` should report delivery.
        """
        self.connected = connected
        self.sent: list[tuple[str, dict]] = []

    async def send_to_agent(self, node_id: str, payload: dict) -> bool:
        """Record a dispatch instead of writing it to a socket.

        Args:
            node_id: Stringified node id the runner selected.
            payload: JSON-able ``ExecuteStepCommand`` dump.

        Returns:
            ``self.connected``.
        """
        self.sent.append((node_id, payload))
        return self.connected


class StubRunner:
    """Duck-typed ``JobRunner`` whose ``submit_job`` fails for chosen job ids.

    Only used by the two ``resume_active_jobs`` error-branch tests: resume's
    contract is "one bad job must not abort startup", and the only way to
    provoke that is for the collaborator it calls to raise. Everything else in
    those tests (the candidate query, the count, the ``status="failed"`` write)
    is the real code under test.

    Attributes:
        submitted: Every job id resume handed over, in call order, as given
            (not coerced) so tests can assert the argument's *type* as well as
            its value.
        fail_ids: Job ids (as strings) for which ``submit_job`` raises.
    """

    def __init__(self, fail_ids):
        """Create the stub.

        Args:
            fail_ids: Iterable of job ids whose submission must raise. Compared
                as strings so callers can pass either ``str`` or ``uuid.UUID``.
        """
        self.submitted: list = []
        self.fail_ids = {str(j) for j in fail_ids}

    async def submit_job(self, db, job_id) -> None:
        """Record the submission, raising for ids in ``fail_ids``.

        Args:
            db: Session resume passed through (unused).
            job_id: Job resume is trying to resume.

        Raises:
            RuntimeError: when ``job_id`` is in ``fail_ids``.
        """
        self.submitted.append(job_id)
        if str(job_id) in self.fail_ids:
            raise RuntimeError("submit exploded")


@pytest.fixture
def shrink_step_timeout(monkeypatch):
    """Rewrite the runner's 2h per-step ceiling to 50 ms for one test.

    ``_execute_remote_step`` hard-codes ``asyncio.wait_for(..., timeout=7200)``.
    The only way to exercise its real ``except asyncio.TimeoutError`` branch
    inside a normal test run is to shorten that wait, so this patches
    ``asyncio.wait_for`` with a passthrough that substitutes 50 ms **only** when
    the requested timeout is exactly 7200. Any other caller (pytest-asyncio's
    own teardown, for instance) is delegated to the genuine implementation
    unchanged, and the wait itself is a real event-loop wait rather than a
    simulated one.

    Args:
        monkeypatch: pytest's patcher; restores ``asyncio.wait_for`` on teardown.

    Returns:
        ``None`` — requested for its side effect.
    """
    real_wait_for = asyncio.wait_for

    async def _shrunk(awaitable, timeout=None):
        """Delegate to the real ``wait_for``, collapsing the 2h step ceiling."""
        return await real_wait_for(awaitable, 0.05 if timeout == 7200 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", _shrunk)


async def _make_node(db, **overrides):
    """Persist a node with sensible defaults (online linux x86_64).

    Args:
        db: Test session.
        **overrides: Any ``ops.create_node`` kwarg — usually ``hostname`` (not
            unique, but kept distinct per test for readable assertions),
            ``status`` or ``os_type``.

    Returns:
        The persisted ``Node``.
    """
    params = dict(
        hostname="node.test",
        os_type="linux",
        arch="x86_64",
        agent_version="0.1.0",
        status="online",
    )
    params.update(overrides)
    return await ops.create_node(db, **params)


async def _make_job(db, regular_user, steps_config, **overrides):
    """Persist a ``pending`` job owned by ``regular_user``.

    Args:
        db: Test session.
        regular_user: Submitter (``Job.submitted_by`` is required).
        steps_config: Raw list of ``{"step": ..., "params": {...}}`` dicts.
        **overrides: Any other ``ops.create_job`` kwarg.

    Returns:
        The persisted ``Job`` at ``status="pending"``, ``current_step=0``.
    """
    params = dict(name="t-job", submitted_by=regular_user.id, steps_config=steps_config)
    params.update(overrides)
    return await ops.create_job(db, **params)


async def _impersonate_agent(runner, job_id, step_index, *, outcome="success",
                             outputs=None, error="agent reported failure", **log_fields):
    """Play the agent half of one remote step's round trip.

    ``_execute_remote_step`` registers ``_step_events["{job_id}:{idx}"]`` and
    only then parks on it, so a callback fired too early would find no waiter,
    be dropped, and leave the runner blocked on its real 2h ceiling. This polls
    for the key first and only then invokes the genuine callback.

    Args:
        runner: The ``JobRunner`` under test.
        job_id: Job id used to build the completion key (str or ``UUID``).
        step_index: Step index half of the completion key.
        outcome: ``"success"`` → ``on_step_completed``; ``"failed"`` →
            ``on_step_failed``; ``"silent"`` → set the Event *without*
            depositing a result, which is the "wire message lost its payload"
            case the runner defends against with a fallback result.
        outputs: Declared step outputs for the success path.
        error: Failure message for the failed path.
        **log_fields: ``command`` / ``stdout`` / ``stderr`` / ``exit_code``
            forwarded verbatim to the callback.

    Returns:
        ``True`` if the handshake happened. Tests assert this so a missed
        handshake fails loudly instead of masquerading as a timeout.
    """
    key = f"{job_id}:{step_index}"
    for _ in range(200):
        if key in runner._step_events:
            if outcome == "success":
                runner.on_step_completed(
                    str(job_id), step_index, outputs=outputs or {}, **log_fields,
                )
            elif outcome == "failed":
                runner.on_step_failed(str(job_id), step_index, error=error, **log_fields)
            else:
                runner._step_events[key].set()
            return True
        await asyncio.sleep(0.01)
    return False


# ── runner: _format_log_block edge cases ─────────────────────────────────


def test_format_log_block_control_plane_step_renders_banner_and_status_only():
    """A result dict with none of the optional keys yields banner + status only.

    This is the exact shape ``_execute_local_step`` returns: no ``command``, no
    streams, no ``exit_code``. Every lookup in the formatter must tolerate the
    key being absent, so a control-plane step cannot ``KeyError`` its way into
    failing an otherwise-successful job at log-append time.
    """
    block = _format_log_block(0, "sleep", "control-plane", "success", {})

    assert block == "===== [step 0] sleep on control-plane =====\n[status: success]\n\n"
    # No process ran, so none of the subprocess-shaped sections appear.
    assert "$ " not in block
    assert "exit code:" not in block
    assert "--- stderr ---" not in block
    # Trailing blank line separates successive blocks in the concatenated log.
    assert block.endswith("\n\n")


def test_format_log_block_stderr_section_suppresses_the_error_line():
    """When stderr is present, ``error`` is deliberately NOT echoed as well.

    stderr is assumed to already carry the real diagnostic; printing the
    runner's ``error`` string on top of it duplicates the same message in the
    operator-facing log. Asserting the *absence* of ``error:`` is the whole
    point — a refactor that emits both would double every failed step's log.
    """
    block = _format_log_block(
        1, "run_command", "node-1", "failed",
        {"command": "false", "stderr": "permission denied\n",
         "error": "permission denied", "exit_code": 13},
    )

    assert "--- stderr ---" in block
    assert "permission denied" in block
    assert "error:" not in block
    assert "exit code: 13" in block
    assert "status: failed" in block


def test_format_log_block_zero_exit_code_is_still_rendered_on_a_failed_step():
    """``exit_code=0`` on a failed step is shown, because the check is ``is not None``.

    Boundary value for the one falsy-but-meaningful field in the result dict. A
    truthiness check here would hide the exit code of exactly the steps an
    operator most needs to explain — the ones that exited 0 yet were judged
    failed (e.g. a required output file never appeared).
    """
    block = _format_log_block(
        4, "gem5_run_simulation", "sim-1", "failed",
        {"error": "stats.txt missing", "exit_code": 0},
    )

    assert "exit code: 0" in block
    assert "status: failed" in block
    assert "error: stats.txt missing" in block


def test_format_log_block_missing_exit_code_omits_the_field_entirely():
    """A ``None`` exit code produces no ``exit code:`` fragment at all.

    Complement of the zero-exit-code case: steps that never wrapped a
    subprocess must not render a misleading ``exit code: None``.
    """
    block = _format_log_block(
        0, "run_command", "node-1", "success", {"command": "true", "exit_code": None},
    )

    assert "exit code" not in block
    assert block.endswith("[status: success]\n\n")


def test_format_log_block_strips_only_trailing_newlines_from_streams():
    """Stream padding is ``rstrip("\\n")``-ed, so blocks don't accumulate blank lines.

    Captured output almost always ends in one or more newlines; without the
    strip, every concatenated block would drift further apart and the log would
    grow ragged. Interior blank lines and trailing *spaces* must survive,
    because they are part of the program's real output.
    """
    block = _format_log_block(
        0, "run_command", "node-1", "failed",
        {"command": "noisy", "stdout": "first\n\nsecond\n\n\n", "stderr": "warn  \n\n"},
    )

    # Interior blank line preserved; trailing newlines collapsed.
    assert "first\n\nsecond\n--- stderr ---\nwarn  \n[status: failed]" in block
    assert block.endswith("\n\n")


def test_format_log_block_skips_falsy_command_and_whitespace_only_streams():
    """An empty command and newline-only streams add no lines.

    ``command`` is gated on truthiness and the streams are gated *after*
    stripping, so ``""`` and ``"\\n"`` are all equivalent to "absent". This
    keeps a step that produced no output from rendering a bare ``$`` prompt or
    an empty ``--- stderr ---`` header, either of which reads as a bug in the
    log.
    """
    block = _format_log_block(
        7, "run_command", "node-1", "success",
        {"command": "", "stdout": "\n", "stderr": "\n\n"},
    )

    assert block == "===== [step 7] run_command on node-1 =====\n[status: success]\n\n"


def test_format_log_block_tolerates_explicit_none_streams():
    """``stdout``/``stderr`` explicitly set to ``None`` behave like absent keys.

    The agent's protocol fields are all ``str | None``, so ``None`` reaches the
    formatter in normal operation (not just in malformed input). The ``or ""``
    guards must absorb it rather than raising ``AttributeError`` on
    ``None.rstrip``.
    """
    block = _format_log_block(
        2, "git_clone", "node-1", "failed",
        {"command": "git clone x", "stdout": None, "stderr": None, "error": "no such repo"},
    )

    assert "--- stderr ---" not in block
    # With no stderr, the error IS surfaced — otherwise the failure is unexplained.
    assert "error: no such repo" in block


def test_format_log_block_success_never_emits_an_error_line():
    """A stray ``error`` key on a successful result is not rendered.

    ``on_step_completed`` never sets ``error``, but a merged/reused result dict
    could carry one. The ``status != "success"`` gate must keep it out of the
    log so a green step never looks red to the operator.
    """
    block = _format_log_block(
        0, "run_command", "node-1", "success",
        {"command": "true", "error": "stale message", "exit_code": 0},
    )

    assert "error:" not in block
    assert "stale message" not in block
    assert "status: success" in block


# ── runner: on_step_completed / on_step_failed (WS callbacks) ─────────────


def test_on_step_completed_stores_success_result_and_sets_registered_event():
    """The callback deposits a success result under the key and wakes the waiter.

    This is the completion half of the server↔agent round trip. The stored dict
    is exactly what ``_execute_remote_step`` pops and what ``_run_job`` then
    merges into the job context and renders into the log, so every field the
    agent reported has to survive the hand-off.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job_id = str(uuid.uuid4())
    runner._step_events[f"{job_id}:0"] = asyncio.Event()

    runner.on_step_completed(
        job_id, 0, outputs={"exit_code": 0}, command="echo hi",
        stdout="hi\n", stderr="", exit_code=0,
    )

    assert runner._step_results[f"{job_id}:0"] == {
        "status": "success", "outputs": {"exit_code": 0},
        "command": "echo hi", "stdout": "hi\n", "stderr": "", "exit_code": 0,
    }
    assert runner._step_events[f"{job_id}:0"].is_set() is True


def test_on_step_failed_stores_failure_result_and_sets_registered_event():
    """Mirror image of the completion callback: a ``failed`` result plus a wake-up.

    A step failure must NOT be turned into a job failure here — the callback
    only records the outcome, because ``_run_job`` is what applies the step's
    ``on_fail`` policy. Writing job status from the WS layer would make
    ``on_fail="continue"`` impossible.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job_id = str(uuid.uuid4())
    runner._step_events[f"{job_id}:3"] = asyncio.Event()

    runner.on_step_failed(
        job_id, 3, error="exit 1", command="false",
        stdout="", stderr="bad\n", exit_code=1,
    )

    assert runner._step_results[f"{job_id}:3"] == {
        "status": "failed", "error": "exit 1",
        "command": "false", "stdout": "", "stderr": "bad\n", "exit_code": 1,
    }
    assert runner._step_events[f"{job_id}:3"].is_set() is True


def test_step_callbacks_default_every_log_field_to_none():
    """Only ``outputs``/``error`` are required; the log fields default to ``None``.

    Not all steps wrap a subprocess, and older agents may omit the fields
    entirely. The keys must still be *present* (set to ``None``) because
    ``_format_log_block`` reads them with ``.get`` and ``_run_job`` passes the
    dict straight through — a missing key here would silently change the log.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job_id = str(uuid.uuid4())

    runner.on_step_completed(job_id, 0, outputs={})
    runner.on_step_failed(job_id, 1, error="nope")

    for key in (f"{job_id}:0", f"{job_id}:1"):
        stored = runner._step_results[key]
        assert stored["command"] is None
        assert stored["stdout"] is None
        assert stored["stderr"] is None
        assert stored["exit_code"] is None


def test_step_callbacks_without_a_registered_event_store_without_raising():
    """A callback with no waiter is a deliberate no-op, not an error.

    Happens routinely in production: the job was cancelled, the step already hit
    its 2h ceiling, or the agent re-sent a message for a step that was
    re-dispatched after a restart. The WS receive loop must not see an exception
    from any of those, or one late message would tear down the socket.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job_id = str(uuid.uuid4())

    runner.on_step_completed(job_id, 0, outputs={"a": 1})
    runner.on_step_failed(job_id, 1, error="late")

    assert runner._step_events == {}
    # The results are still stored — nothing consumes them (documented leak).
    assert runner._step_results[f"{job_id}:0"]["status"] == "success"
    assert runner._step_results[f"{job_id}:1"]["status"] == "failed"


def test_step_callback_for_another_index_does_not_wake_this_step():
    """The key includes the step index, so completions cannot cross-signal.

    Within one job the step index is the only thing separating two waiters. If
    the key degenerated to the job id alone, one step's completion would wake
    (and hand its result to) a different step, silently mixing outputs between
    steps of the same job.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job_id = str(uuid.uuid4())
    runner._step_events[f"{job_id}:0"] = asyncio.Event()

    runner.on_step_completed(job_id, 1, outputs={})

    assert runner._step_events[f"{job_id}:0"].is_set() is False
    assert f"{job_id}:1" in runner._step_results


def test_step_callback_key_is_identical_for_uuid_and_string_job_ids():
    """A ``uuid.UUID`` job id builds the same key as its string form.

    The cross-module contract: ``on_step_completed`` receives ``job_id`` as a
    string off the wire, while ``_execute_remote_step`` interpolates a value
    taken off the ``Job`` row. Both sides use an f-string, so the two
    representations must stringify identically — otherwise every remote step
    would park until the 2h ceiling. Passing a real ``UUID`` here is the
    regression guard for that.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job_uuid = uuid.uuid4()
    runner._step_events[f"{job_uuid}:2"] = asyncio.Event()

    runner.on_step_completed(job_uuid, 2, outputs={"ok": True})

    assert list(runner._step_results) == [f"{str(job_uuid)}:2"]
    assert runner._step_events[f"{job_uuid}:2"].is_set() is True


def test_step_callback_overwrites_a_previously_stored_result_for_the_same_key():
    """Two messages for one key: last writer wins.

    A duplicate or contradictory agent message must not accumulate — the waiter
    pops exactly one dict. Documents the actual resolution (overwrite) so a
    future change to first-writer-wins is a visible, deliberate decision.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job_id = str(uuid.uuid4())

    runner.on_step_completed(job_id, 0, outputs={"first": True})
    runner.on_step_failed(job_id, 0, error="second")

    assert runner._step_results[f"{job_id}:0"] == {
        "status": "failed", "error": "second",
        "command": None, "stdout": None, "stderr": None, "exit_code": None,
    }


async def test_step_callback_result_is_already_visible_when_the_waiter_wakes():
    """The result is stored BEFORE the Event is set, so a waiter never reads a hole.

    Ordering regression, driven through a real coroutine rather than asserted
    statically: the waiter reads ``_step_results`` the instant it wakes, so
    signalling first would expose a window where the key is missing and the
    runner would report the "No result received" fallback for a step that
    actually succeeded.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job_id = str(uuid.uuid4())
    key = f"{job_id}:0"
    runner._step_events[key] = asyncio.Event()
    seen: list = []

    async def _waiter():
        """Wake on the Event and immediately read what the callback deposited."""
        await runner._step_events[key].wait()
        seen.append(runner._step_results.get(key))

    waiter = asyncio.create_task(_waiter())
    await asyncio.sleep(0)  # let the waiter reach its await
    runner.on_step_completed(job_id, 0, outputs={"exit_code": 0})
    await waiter

    assert seen[0] is not None
    assert seen[0]["outputs"] == {"exit_code": 0}


# ── runner: submit_job ───────────────────────────────────────────────────


async def test_submit_job_for_unknown_id_is_swallowed_and_spawns_no_task(app, db):
    """A missing job is logged and ignored — no task, no exception.

    Both callers matter here: ``resume_active_jobs`` must not abort startup
    because one row was deleted between the query and the submit, and the POST
    handler must not 500. Asserting ``_active_jobs`` stays empty proves the
    existence check runs *before* the task is created, so no background
    coroutine is left to fail against a nonexistent row.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)

    await runner.submit_job(db, uuid.uuid4())

    assert runner._active_jobs == {}


async def test_submit_job_registers_the_task_then_runs_it_to_completion(app, db, regular_user):
    """submit_job returns immediately, and the spawned task drives the job.

    The fire-and-forget contract the POST /api/jobs handler depends on: the task
    is registered synchronously (so ``cancel_job`` can find it straight away)
    but nothing is awaited, which is what keeps submission fast. The ``finally``
    in ``_run_job`` must then deregister it, or the finished task object — and
    every frame it captured — is pinned for the process lifetime.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])

    await runner.submit_job(db, job.id)

    assert list(runner._active_jobs) == [job.id]
    task = runner._active_jobs[job.id]
    assert task.done() is False  # nothing was awaited on our behalf

    await asyncio.gather(task, return_exceptions=True)

    db.expunge_all()  # see module docstring: cross-session staleness
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"
    # The finally clause deregistered the finished task.
    assert runner._active_jobs == {}


async def test_submit_job_keys_active_jobs_by_the_exact_id_object_it_was_given(
    app, db, regular_user,
):
    """``_active_jobs`` is keyed by the caller's value, so a ``UUID`` keys by ``UUID``.

    Documents ACTUAL behaviour, and it is load-bearing: ``cancel_job`` looks the
    task up with ``dict.get``, which is hash-based, so a job submitted with a
    string id is unreachable via its ``UUID`` form and vice versa. The job has
    an empty step list so it completes without touching ``create_step_run``,
    isolating the key-type question from any DB binding concern.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [])
    job_uuid = uuid.UUID(job.id)

    await runner.submit_job(db, job_uuid)

    assert job_uuid in runner._active_jobs
    assert job.id not in runner._active_jobs  # the string form does NOT resolve

    await asyncio.gather(*runner._active_jobs.values(), return_exceptions=True)

    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"


async def test_run_job_accepts_a_uuid_job_id_and_records_step_runs(
    app, db, regular_user,
):
    """A ``uuid.UUID`` job id runs the job to completion and its ``StepRun`` lands.

    Regression guard for the UUID/SQLite bug class, now closed. Every id column
    is ``String(36)``, and ``ops.create_step_run`` used to hand ``job_id``
    straight to the ``StepRun`` constructor: a ``uuid.UUID`` reaching the run
    loop therefore raised on bind, and the outer handler could do nothing but
    record ``failed`` — the step itself never appeared anywhere for the operator
    to see. ``create_step_run`` now coerces through ``_sid`` (and
    ``update_step_run`` routes its ``**kwargs`` through ``_sid_kwargs``), so the
    whole run loop tolerates a ``UUID``.

    Both halves are asserted because either alone would let the bug back in: the
    job must reach ``completed`` *and* the persisted ``job_id`` must be the plain
    string form of the UUID, since that is the representation every later lookup
    (``get_step_runs_for_job``, the Job Detail timeline, the WS handler's
    ``get_latest_step_run``) compares against.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    job_uuid = uuid.UUID(job.id)

    await runner._run_job(job_uuid)

    db.expunge_all()  # see module docstring: cross-session staleness
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"
    assert refreshed.error is None
    assert refreshed.completed_at is not None
    # The step WAS recorded, and it is keyed by the stringified UUID.
    runs = await ops.get_step_runs_for_job(db, job.id)
    assert [(r.step_index, r.status) for r in runs] == [(0, "success")]
    assert isinstance(runs[0].job_id, str)
    assert runs[0].job_id == str(job_uuid) == job.id


async def test_submit_job_twice_orphans_the_first_task(app, db, regular_user):
    """No dedupe: a second submit starts a second task and orphans the first.

    Documents ACTUAL behaviour. ``_active_jobs[job_id]`` is overwritten, so the
    first task becomes unreachable from ``cancel_job`` — it keeps running (and
    keeps writing job/step state for the same job) until the process exits.
    Pinning this makes the absence of an idempotency guard explicit rather than
    an accident nobody noticed.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 999}}])

    await runner.submit_job(db, job.id)
    first = runner._active_jobs[job.id]
    await runner.submit_job(db, job.id)
    second = runner._active_jobs[job.id]

    assert first is not second
    assert len(runner._active_jobs) == 1

    # cancel_job can only reach the second task; the first is orphaned.
    await runner.cancel_job(db, job.id)
    await asyncio.sleep(0.05)
    assert first.done() is False

    first.cancel()
    await asyncio.gather(first, second, return_exceptions=True)


# ── runner: cancel_job ───────────────────────────────────────────────────


async def test_cancel_job_marks_cancelled_and_stamps_completed_at(app, db, regular_user):
    """A queued job with no live task is still closed out as ``cancelled``.

    The status write is unconditional on purpose: a job that was queued but
    never dispatched, or whose task died with a previous server process, has no
    entry in ``_active_jobs`` yet must still be cancellable. Without this the
    row would sit ``pending`` forever and ``resume_active_jobs`` would re-adopt
    it on every restart.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    await ops.update_job(db, job.id, status="queued")

    await runner.cancel_job(db, job.id)

    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "cancelled"
    assert refreshed.completed_at is not None
    assert runner._active_jobs == {}


async def test_cancel_job_for_unknown_id_is_a_noop_without_raising(app, db):
    """Cancelling a nonexistent job neither raises nor creates anything.

    ``ops.update_job`` returns ``None`` for a missing row and ``cancel_job``
    ignores the return value. The route guards with its own 404, so this is the
    defence for the racy case where the job is deleted between that check and
    the cancel.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)

    await runner.cancel_job(db, uuid.uuid4())

    assert runner._active_jobs == {}


async def test_cancel_job_cancels_the_task_it_owns_and_marks_the_job_cancelled(
    app, db, regular_user,
):
    """A live task registered in ``_active_jobs`` is cancelled and the row updated.

    Both halves of the contract in one pass: the asyncio task really is
    cancelled (not merely abandoned) and the row gets ``cancelled`` plus a
    ``completed_at`` stamp.

    The parked task is a plain sleeper rather than a real ``_run_job``, and that
    is deliberate: a real job task owns a DB session, and tearing that session
    down interleaves with this write on the single shared in-memory SQLite
    connection the test engine uses (StaticPool). Isolating ``cancel_job`` from
    that harness artifact is what makes the assertion on ``completed_at``
    meaningful — the live-``_run_job`` case is covered by the next test.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    parked = asyncio.Event()

    async def _parked_job():
        """Stand in for a job task that is mid-step when the operator cancels."""
        parked.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(_parked_job())
    runner._active_jobs[job.id] = task
    await parked.wait()

    await runner.cancel_job(db, job.id)
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled() is True
    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "cancelled"
    assert refreshed.completed_at is not None


async def test_cancel_job_stops_a_live_run_job_without_writing_terminal_status(
    app, db, regular_user,
):
    """Cancelling a real job task halts it and ``_run_job`` records no outcome of its own.

    The ``sleep(999)`` step keeps ``_run_job`` inside its poll loop, so
    cancellation is delivered at a genuine await point. ``_run_job`` catches
    ``CancelledError`` and only logs — writing no job status — which is what
    lets ``cancel_job``'s ``cancelled`` stand in production. Asserting the
    absence of a ``completed``/``failed`` write is the durable form of that
    contract: adding a status write to that except branch would make the final
    status depend on which coroutine committed last.

    The status is not asserted to *equal* ``cancelled`` here because the
    cancelled task's session teardown and this write share one SQLite
    connection under StaticPool (see the previous test); the step-run and
    ``_active_jobs`` assertions below are the harness-independent evidence that
    execution genuinely stopped.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    steps = [
        {"step": "sleep", "params": {"seconds": 999}},  # cancelled mid-step
        {"step": "sleep", "params": {"seconds": 0}},    # must never be reached
    ]
    job = await _make_job(db, regular_user, steps)

    await runner.submit_job(db, job.id)
    task = runner._active_jobs[job.id]
    await asyncio.sleep(0.05)  # let the job reach its poll loop
    assert task.done() is False

    await runner.cancel_job(db, job.id)
    await asyncio.gather(task, return_exceptions=True)

    assert task.done() is True
    # The finally clause runs on the cancellation path too.
    assert runner._active_jobs == {}
    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    # The runner claimed no outcome for the job — only cancel_job may.
    assert refreshed.status not in ("completed", "failed")
    # Execution stopped: the cancelled step never finished and step 1 never began.
    runs = await ops.get_step_runs_for_job(db, job.id)
    assert [(r.step_index, r.status) for r in runs] == [(0, "running")]


async def test_cancel_job_with_a_uuid_id_writes_status_but_misses_the_task(
    app, db, regular_user,
):
    """Documents ACTUAL behaviour: a ``UUID`` lookup cannot find a str-keyed task.

    ``POST /api/jobs/{job_id}/cancel`` declares ``job_id: UUID`` while
    ``POST /api/jobs`` submits with the ``Job.id`` *string*, so in production the
    dict lookup misses: the row flips to ``cancelled`` while the background task
    keeps executing the job. ``ops.update_job`` is ``_sid``-guarded so the write
    itself succeeds, which is exactly what makes the mismatch silent. Reported
    as POSSIBLE BUG rather than adapted to.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 999}}])

    await runner.submit_job(db, job.id)  # keyed by the string id, as the route does
    task = runner._active_jobs[job.id]
    await asyncio.sleep(0.05)

    await runner.cancel_job(db, uuid.UUID(job.id))  # as the cancel route does
    await asyncio.sleep(0.05)

    # Status was written...
    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "cancelled"
    # ...but the task was never cancelled and is still executing the job.
    assert task.done() is False
    assert job.id in runner._active_jobs

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_cancel_job_does_not_re_cancel_an_already_finished_task(
    app, db, regular_user,
):
    """The ``task.done()`` guard keeps cancel off a completed task.

    Calling ``cancel()`` on a finished task is a no-op in asyncio, but the guard
    documents intent and protects the (previously real) case of a stale
    ``_active_jobs`` entry. The status write must still happen so a job whose
    task finished without recording terminal state can be closed out.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])

    async def _already_done():
        """A task that has already returned by the time cancel_job runs."""
        return None

    finished = asyncio.create_task(_already_done())
    await finished
    runner._active_jobs[job.id] = finished

    await runner.cancel_job(db, job.id)

    assert finished.cancelled() is False
    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "cancelled"


# ── runner: _execute_remote_step ─────────────────────────────────────────


async def test_remote_step_no_node_error_names_every_targeting_constraint(
    app, db, regular_user,
):
    """The "no available node" message spells out os/node/pool qualifiers.

    "No available node" on its own is nearly undebuggable once targeting is in
    play — the operator cannot tell whether the pin, the pool or the OS filter
    excluded everything. All three qualifiers are asserted together (and in
    order) because the message is assembled from a list.
    """
    ws = RecordingWsManager()
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="lin.test", os_type="linux")
    pool = await ops.create_pool(db, name="empty-pool", created_by=None)
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0,
        target_node_id=node.id, target_pool_id=pool.id, target_os="macos",
    )

    assert result["status"] == "failed"
    assert result["error"] == (
        f"No available node for step 'run_command' "
        f"(os=macos, node={node.id}, pool={pool.id})"
    )
    # Nothing was dispatched — placement failed before the send.
    assert ws.sent == []


async def test_remote_step_no_node_without_targeting_has_no_qualifier(
    app, db, regular_user,
):
    """With no targets the message carries no parenthesised qualifier.

    Negative control for the qualifier assembly: the trailing ``(...)`` must be
    omitted entirely rather than rendered empty, so the untargeted
    empty-cluster case reads cleanly.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0,
    )

    assert result["error"] == "No available node for step 'run_command'"
    assert "(" not in result["error"]


async def test_remote_step_marks_the_step_run_running_on_the_chosen_node(
    app, db, regular_user,
):
    """Before dispatch the step run records ``running``, its node and a start time.

    This row is the only record of *where* a step went, and it is written before
    the send so a crash mid-dispatch still attributes the step to the right
    machine. The Job Detail timeline and the WS handler's
    ``get_latest_step_run`` lookup both depend on it existing by then.
    """
    ws = RecordingWsManager(connected=False)  # fail right after the send
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="agent-2.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )

    db.expunge_all()
    runs = await ops.get_step_runs_for_job(db, job.id)
    assert len(runs) == 1
    assert runs[0].status == "running"
    assert str(runs[0].node_id) == str(node.id)
    assert runs[0].started_at is not None


async def test_remote_step_accepts_uuid_objects_for_node_and_step_run_ids(
    app, db, regular_user,
):
    """Passing real ``uuid.UUID`` ids through the dispatch path does not crash.

    Regression guard for the UUID/SQLite bug class: every id column is
    ``String(36)``, so a raw ``uuid.UUID`` reaching a bind parameter raises
    ``sqlite3.ProgrammingError`` and can poison the session. This drives the
    whole dispatch — node lookup (``get_node_by_id``) and the step-run update
    (``update_step_run``) — with ``UUID`` objects to pin that both are
    ``_sid``-coerced.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="uuid-node.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    agent = asyncio.create_task(
        _impersonate_agent(runner, job.id, 0, outputs={"exit_code": 0}, exit_code=0)
    )
    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), uuid.UUID(step_run.id), 0, target_node_id=uuid.UUID(node.id),
    )
    assert await agent is True

    assert result["status"] == "success"
    assert result["node_label"] == "uuid-node.test"
    db.expunge_all()
    runs = await ops.get_step_runs_for_job(db, job.id)
    assert str(runs[0].node_id) == str(node.id)


async def test_remote_step_completion_key_includes_a_nonzero_step_index(
    app, db, regular_user,
):
    """A step at index 5 parks on ``"{job_id}:5"`` and is woken by that key alone.

    Guards the ``"{job_id}:{step_index}"`` contract at a non-zero index, where a
    hard-coded or off-by-one index would still pass an index-0 test. The
    ``step_index`` is also echoed to the agent so its reply routes back to the
    same waiter.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="agent-5.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=5, step_name="run_command", input_params={},
    )

    agent = asyncio.create_task(_impersonate_agent(runner, job.id, 5, outputs={"x": 1}))
    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 5, target_node_id=node.id,
    )
    assert await agent is True

    assert result["status"] == "success"
    assert result["outputs"] == {"x": 1}
    assert ws.sent[0][1]["step_index"] == 5


async def test_remote_step_strips_credential_name_from_the_dispatched_params(
    app, db, regular_user,
):
    """``credential_name`` is popped from params even with no credential manager.

    Security boundary: the reference must never travel inside step params (where
    it would land in agent-side logs and in the persisted ``input_params``).
    With ``_cred_manager=None`` the name is dropped and the step runs
    uncredentialed — a quiet degradation that this test makes visible.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="nocred.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    agent = asyncio.create_task(_impersonate_agent(runner, job.id, 0, outputs={}))
    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command",
        {"command": "ls", "credential_name": "secret-svc"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )
    assert await agent is True

    assert result["status"] == "success"
    _, payload = ws.sent[0]
    assert "credential_name" not in payload["params"]
    assert payload["credential_config"] is None


async def test_remote_step_resolves_a_stored_credential_into_credential_config(
    app, db, regular_user, admin_user, credential_manager,
):
    """A named credential is decrypted server-side and sent in its own field.

    The decrypted client config travels in ``credential_config``, never in
    ``params`` — that separation is what keeps the secret out of the step's
    persisted inputs and the agent's step log. Uses the real
    ``CredentialManager`` (no stub) so encryption, strategy dispatch and
    name lookup are all exercised.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=credential_manager)
    await credential_manager.store(
        db, name="svc-account", credential_type="basic",
        fields={"username": "svc", "password": "hunter2"}, owner_id=admin_user.id,
    )
    node = await _make_node(db, hostname="cred.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    agent = asyncio.create_task(_impersonate_agent(runner, job.id, 0, outputs={}))
    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command",
        {"command": "ls", "credential_name": "svc-account"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )
    assert await agent is True

    assert result["status"] == "success"
    _, payload = ws.sent[0]
    assert payload["credential_config"] == {"username": "svc", "password": "hunter2"}
    assert "credential_name" not in payload["params"]
    assert "hunter2" not in str(payload["params"])


async def test_remote_step_unknown_credential_fails_before_dispatch(
    app, db, regular_user, credential_manager,
):
    """An unresolvable ``credential_name`` fails the step and sends nothing.

    ``CredentialManager.get_by_name`` raises ``KeyError`` for a missing name;
    the runner converts it into a step failure. Failing *before* the send is the
    point — dispatching a credentialed step with no credential would have the
    agent authenticate as nobody and fail minutes later with a far worse error.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=credential_manager)
    node = await _make_node(db, hostname="badcred.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command",
        {"command": "ls", "credential_name": "no-such-cred"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )

    assert result == {"status": "failed", "error": "Credential 'no-such-cred' not found"}
    assert ws.sent == []
    # Bailing out this early leaves no completion bookkeeping behind.
    assert runner._step_events == {}


async def test_remote_step_node_label_falls_back_to_the_node_id(app, db, regular_user):
    """A node with a blank hostname is labelled by its id in the job log.

    ``node_label`` feeds the "on <host>" banner in the terminal log. An empty
    hostname would render ``on `` and leave the operator unable to tell which
    machine ran the step, so the id is used instead.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    agent = asyncio.create_task(_impersonate_agent(runner, job.id, 0, outputs={}))
    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )
    assert await agent is True

    assert result["node_label"] == str(node.id)


async def test_remote_step_woken_with_no_stored_result_reports_no_result_received(
    app, db, regular_user,
):
    """An Event set without a deposited result yields the fallback failure.

    Defends the ``_step_results.pop(key, default)`` fallback. Any path that
    signals the Event without storing a result — a future callback that
    signals first, or an internal ``set()`` — must surface as a clean step
    failure rather than a ``KeyError`` that fails the whole job through the
    outer handler and bypasses ``on_fail``.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="silent.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    agent = asyncio.create_task(_impersonate_agent(runner, job.id, 0, outcome="silent"))
    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )
    assert await agent is True

    assert result["status"] == "failed"
    assert result["error"] == "No result received"
    assert result["node_label"] == "silent.test"


async def test_remote_step_failure_callback_propagates_error_and_node_label(
    app, db, regular_user,
):
    """An agent-reported failure comes back with its error, logs and node label.

    The agent's diagnostic has to survive the hand-off intact: ``error`` becomes
    ``Job.error`` under ``on_fail="stop"`` and the stream/exit-code fields are
    what ``_format_log_block`` renders. ``node_label`` is added by the runner,
    not the agent, so it is asserted alongside.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="failing.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    agent = asyncio.create_task(
        _impersonate_agent(
            runner, job.id, 0, outcome="failed", error="exit status 2",
            command="ls /nope", stderr="No such file\n", exit_code=2,
        )
    )
    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )
    assert await agent is True

    assert result["status"] == "failed"
    assert result["error"] == "exit status 2"
    assert result["stderr"] == "No such file\n"
    assert result["exit_code"] == 2
    assert result["node_label"] == "failing.test"


async def test_remote_step_clears_its_bookkeeping_after_a_successful_step(
    app, db, regular_user,
):
    """``_step_events`` and ``_step_results`` are both emptied in the ``finally``.

    Two reasons this matters on a long-lived server: unbounded growth of both
    dicts, and — worse — a leftover entry being mistaken for the result of a
    *future* step that reuses the same ``job_id:step_index`` key, which jump
    loops do by construction.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="clean.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    agent = asyncio.create_task(_impersonate_agent(runner, job.id, 0, outputs={"a": 1}))
    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )
    assert await agent is True

    assert result["status"] == "success"
    assert runner._step_events == {}
    assert runner._step_results == {}


async def test_remote_step_clears_its_bookkeeping_when_the_agent_is_disconnected(
    app, db, regular_user,
):
    """The early ``return`` on an undelivered send still runs the ``finally``.

    The Event is registered *before* the send (so a fast agent cannot outrun the
    waiter), which means the disconnect path returns with an entry already in
    the map. Only the ``finally`` reaps it — an early return that skipped
    cleanup would leak one Event per offline node, forever.
    """
    ws = RecordingWsManager(connected=False)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="ghost.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )

    assert result == {"status": "failed", "error": "Agent for node ghost.test not connected"}
    assert runner._step_events == {}
    assert runner._step_results == {}
    # A disconnected send carries no node_label — the step never ran anywhere.
    assert "node_label" not in result


async def test_remote_step_timeout_fails_the_step_and_clears_bookkeeping(
    app, db, regular_user, shrink_step_timeout,
):
    """A step whose completion never arrives fails with the 2h timeout message.

    Exercises the real ``except asyncio.TimeoutError`` branch (the ceiling is
    shortened to 50 ms by the fixture; the wait itself is genuine). No agent
    callback fires, which is what a node that died mid-step looks like. The
    ``finally`` must still reap the Event, or every timed-out step would leak
    one for the lifetime of the process.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="stalled.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )

    assert result == {"status": "failed", "error": "Step execution timed out (2h)"}
    # The command WAS delivered — the agent simply never answered.
    assert len(ws.sent) == 1
    assert runner._step_events == {}
    assert runner._step_results == {}


async def test_remote_step_timed_out_then_late_completion_is_discarded(
    app, db, regular_user, shrink_step_timeout,
):
    """A completion arriving after the timeout has no waiter and no live key.

    The orphaned-agent scenario: the server gave up at the ceiling but the node
    kept working and eventually replied. The late message must not resurrect the
    step, and because the ``finally`` already reaped the key, the stale result
    cannot be picked up by a later step that reuses the same key either.
    """
    ws = RecordingWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="late.test")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )

    result = await runner._execute_remote_step(
        db, job, get_step("run_command"), "run_command", {"command": "ls"},
        StepContext(), step_run.id, 0, target_node_id=node.id,
    )
    assert result["status"] == "failed"

    # The agent finally answers, long after nobody is listening.
    runner.on_step_completed(str(job.id), 0, outputs={"exit_code": 0})

    assert runner._step_events == {}
    # Stored with no consumer (documented no-op), and no Event to signal.
    assert runner._step_results[f"{job.id}:0"]["status"] == "success"


# ── resume: resume_active_jobs ───────────────────────────────────────────


async def test_resume_active_jobs_excludes_failed_and_cancelled_jobs(
    app, db, regular_user,
):
    """Only pending/queued/running are re-adopted; failed and cancelled are not.

    Complements the completed-job exclusion covered elsewhere. Re-running a
    terminal job would repeat its side effects (shell commands, artifact
    uploads) and overwrite a recorded outcome, so every terminal status must be
    filtered. The resumed ids are also asserted to be *strings*, which is the
    representation ``cancel_job`` later needs for its ``_active_jobs`` lookup.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    step = [{"step": "sleep", "params": {"seconds": 0}}]
    live = await _make_job(db, regular_user, step, name="live")
    failed = await _make_job(db, regular_user, step, name="failed")
    cancelled = await _make_job(db, regular_user, step, name="cancelled")
    await ops.update_job(db, failed.id, status="failed")
    await ops.update_job(db, cancelled.id, status="cancelled")

    resumed = await resume_active_jobs(db, runner)

    assert resumed == 1
    assert list(runner._active_jobs) == [live.id]
    assert all(isinstance(k, str) for k in runner._active_jobs)

    await asyncio.gather(*runner._active_jobs.values(), return_exceptions=True)


async def test_resume_active_jobs_re_enters_the_job_at_its_persisted_current_step(
    app, db, regular_user,
):
    """A job resumed at ``current_step=2`` runs only step 2, not the whole list.

    The core of crash recovery: ``current_step`` is written *before* a step
    executes, so the step that was in flight is re-run and the ones already done
    are skipped. Steps 0 and 1 are ``sleep(999)`` so a resume that restarted
    from the top would hang rather than quietly assert the wrong thing, and the
    single ``StepRun`` at index 2 is the positive proof.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    steps = [
        {"step": "sleep", "params": {"seconds": 999}},  # already done pre-crash
        {"step": "sleep", "params": {"seconds": 999}},  # already done pre-crash
        {"step": "sleep", "params": {"seconds": 0}},    # in flight at crash time
    ]
    job = await _make_job(db, regular_user, steps)
    await ops.update_job(db, job.id, status="running", current_step=2)

    resumed = await resume_active_jobs(db, runner)
    assert resumed == 1
    await asyncio.gather(*runner._active_jobs.values(), return_exceptions=True)

    db.expunge_all()  # see module docstring: cross-session staleness
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"
    runs = await ops.get_step_runs_for_job(db, job.id)
    assert [r.step_index for r in runs] == [2]


async def test_resume_active_jobs_seeds_the_step_context_from_persisted_data(
    app, db, regular_user,
):
    """A resumed job's steps see ``context_data`` produced before the crash.

    Without this, every ``${...}`` reference and every context-satisfiable
    parameter downstream of the crash point would be unbound. The resumed step
    supplies no ``seconds`` param at all, so it can only validate if the
    persisted ``context_data`` was loaded into the ``StepContext`` — otherwise
    ``SleepParams`` rejects it and the job ends ``failed``.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {}}])
    await ops.update_job(db, job.id, status="running", context_data={"seconds": 0})

    resumed = await resume_active_jobs(db, runner)
    assert resumed == 1
    await asyncio.gather(*runner._active_jobs.values(), return_exceptions=True)

    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"
    runs = await ops.get_step_runs_for_job(db, job.id)
    assert [r.status for r in runs] == ["success"]


async def test_resume_active_jobs_appends_the_resumed_step_to_the_job_log(
    app, db, regular_user,
):
    """A resumed job keeps its pre-crash log and appends the new block to it.

    ``append_job_log`` is read-concat-write, so a resume that reset the column
    would destroy the operator's record of everything that ran before the crash
    — the single most useful artefact when diagnosing why the server died.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    await ops.update_job(db, job.id, status="running")
    await ops.append_job_log(db, job.id, "pre-crash output\n")

    resumed = await resume_active_jobs(db, runner)
    assert resumed == 1
    await asyncio.gather(*runner._active_jobs.values(), return_exceptions=True)

    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.log_text.startswith("pre-crash output\n")
    assert "[step 0] sleep on control-plane" in refreshed.log_text


async def test_resume_active_jobs_marks_a_job_failed_when_submission_raises(
    app, db, regular_user,
):
    """A job whose resubmission raises is marked failed and excluded from the count.

    Startup must not die because of one bad row: the broad ``except`` swallows
    the error, records it on the job so the operator can see why it never came
    back, and the return value counts only jobs actually handed to the runner.
    ``StubRunner`` is the collaborator that raises; resume itself is real.
    """
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    await ops.update_job(db, job.id, status="running")
    stub = StubRunner(fail_ids=[job.id])

    resumed = await resume_active_jobs(db, stub)

    assert resumed == 0
    assert stub.submitted == [job.id]
    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "failed"
    assert refreshed.error == "Resume failed: submit exploded"


async def test_resume_active_jobs_keeps_going_after_one_submission_fails(
    app, db, regular_user,
):
    """One failing job does not stop the others from being resumed.

    The loop-level ``try`` (rather than one around the whole loop) is what makes
    recovery all-but-one instead of all-or-nothing. Asserting the survivors are
    untouched — still ``running``, no error — proves the failure was attributed
    to exactly one job.
    """
    step = [{"step": "sleep", "params": {"seconds": 0}}]
    doomed = await _make_job(db, regular_user, step, name="doomed")
    ok_a = await _make_job(db, regular_user, step, name="ok-a")
    ok_b = await _make_job(db, regular_user, step, name="ok-b")
    for j in (doomed, ok_a, ok_b):
        await ops.update_job(db, j.id, status="running")
    stub = StubRunner(fail_ids=[doomed.id])

    resumed = await resume_active_jobs(db, stub)

    assert resumed == 2
    assert sorted(stub.submitted) == sorted([doomed.id, ok_a.id, ok_b.id])
    db.expunge_all()
    assert (await ops.get_job_by_id(db, doomed.id)).status == "failed"
    for j in (ok_a, ok_b):
        survivor = await ops.get_job_by_id(db, j.id)
        assert survivor.status == "running"
        assert survivor.error is None


async def test_submit_job_absorbs_a_job_deleted_after_the_resume_query(
    app, db, regular_user,
):
    """A job that vanishes between resume's query and its submit is absorbed.

    Reproduces the TOCTOU window inside ``resume_active_jobs``: the candidate
    list is captured by ``get_active_jobs``, and a row can be deleted before
    ``submit_job`` reaches it. The loop is hand-rolled here (rather than calling
    ``resume_active_jobs``, which would re-query and see nothing) precisely so
    the stale id is what gets submitted.

    Documents ACTUAL behaviour: ``submit_job`` logs and swallows the miss, so
    resume counts the job as resumed but spawns no task. The guarantee that
    matters is that the deleted row neither raises out of ``lifespan`` nor leaves
    a background task writing against a nonexistent job.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    await ops.update_job(db, job.id, status="running")
    active = await ops.get_active_jobs(db)
    assert len(active) == 1
    # ``DELETE /api/jobs/{id}`` removes the row through the session directly;
    # there is no ops helper for it.
    await db.delete(job)
    await db.commit()

    # Feed resume the stale candidate list by re-submitting the now-deleted job:
    # this is the window between ``get_active_jobs`` and ``submit_job``.
    resumed = 0
    for stale_id in [j.id for j in active]:
        await runner.submit_job(db, stale_id)
        resumed += 1

    assert resumed == 1
    assert runner._active_jobs == {}
    assert await ops.get_job_by_id(db, job.id) is None


async def test_resume_active_jobs_handles_a_job_with_an_empty_step_list(
    app, db, regular_user,
):
    """A resumed job with no steps completes immediately instead of hanging.

    Boundary value for the ``while idx < len(steps_config)`` loop: an empty list
    must fall straight through to the terminal ``completed`` write. A job like
    this cannot be created through the API, but it can exist in the database, and
    on restart it must reach a terminal state rather than being re-adopted on
    every subsequent boot.
    """
    runner = JobRunner(ws_manager=RecordingWsManager(), credential_manager=None)
    job = await _make_job(db, regular_user, [])
    await ops.update_job(db, job.id, status="queued")

    resumed = await resume_active_jobs(db, runner)
    assert resumed == 1
    await asyncio.gather(*runner._active_jobs.values(), return_exceptions=True)

    db.expunge_all()
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"
    assert refreshed.completed_at is not None
    assert await ops.get_step_runs_for_job(db, job.id) == []
    # Nothing is left active, so the next restart resumes nothing.
    assert await ops.get_active_jobs(db) == []
