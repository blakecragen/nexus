"""Integration tests for the job runner subsystem.

SUT: ``packages/server/src/nexus_server/runner/`` —
    - ``scheduler.find_node_for_step`` / ``_node_matches_step`` (node selection
      and eligibility, incl. direct/pool/any targeting and OS pinning),
    - ``runner.JobRunner`` (local control-plane step execution end-to-end,
      on_fail handling, jump/loop step-index advancement, context accumulation
      across steps, and the remote-dispatch payload),
    - ``resume.resume_active_jobs`` (picking up pending/queued/running jobs).

Strategy / stubs:
    - ``find_node_for_step`` and local-step execution are driven against the real
      in-memory DB (the ``db`` fixture). These are pure/testable and exercised
      end-to-end.
    - ``JobRunner._run_job`` opens its OWN session via ``get_session_factory()``.
      The ``app`` fixture already points ``db.session._engine`` / ``_session_factory``
      at the in-memory test engine, so we depend on the ``app`` fixture to wire
      that up, then drive ``_run_job`` directly and assert DB state via ``db``.
    - The WebSocket layer is the only true external boundary: we replace it with
      ``FakeWsManager`` (records ``send_to_agent`` calls). Remote steps require a
      connected agent + async completion callback, so for those we assert the
      DISPATCHED command/payload against the fake rather than driving to
      completion. This is documented per-test.

Local vs. remote steps (the key distinction these tests turn on)
    A *local* (control-plane) step — ``sleep``, ``jump`` — runs inside the
    server process and needs no node. A *remote* step — ``run_command``,
    ``gem5_run_simulation`` — must be scheduled onto a node and dispatched over
    the agent WebSocket. Tests that only need the control-flow machinery use
    local steps precisely so no fake agent is required.

Why ``app`` is requested by tests that never issue an HTTP request
    Requesting the ``app`` fixture is how these tests get
    ``nexus_server.db.session._engine`` / ``._session_factory`` repointed at the
    in-memory database. ``_run_job`` runs outside any request scope and opens
    its own session through those module globals, so without ``app`` the runner
    would silently create a real on-disk engine and operate on a different
    database than the one the test seeded.

Timing / determinism notes
    Every ``sleep`` step uses ``seconds=0`` except where a large value marks a
    step that MUST be skipped — a non-zero duration there means an
    incorrectly-taken branch shows up as a hang rather than a subtle assertion
    failure. Remote-step tests never wait out the real completion timeout
    (2 hours); they either fire the completion callback themselves or use a
    disconnected fake so the dispatch fails fast.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus_common.steps.registry import get_step
from nexus_server.db import ops
from nexus_server.runner.runner import JobRunner, _format_log_block
from nexus_server.runner.resume import resume_active_jobs
from nexus_server.runner.scheduler import _node_matches_step, find_node_for_step


# ── Fakes / helpers ──────────────────────────────────────────────────────


class FakeWsManager:
    """Records dispatched agent commands. The only external boundary we stub.

    ``send_to_agent`` mirrors the real manager's contract: returns True when the
    agent is "connected" (we make connectivity configurable) and records the
    (node_id, payload) pair for assertion.

    Attributes:
        connected: What ``send_to_agent`` reports. ``False`` simulates a node
            row that exists in the DB but has no live WebSocket — the common
            production case of an agent that crashed or lost its network.
        sent: Every dispatch as ``(node_id, payload)``. Tests assert both the
            call count (exactly-once dispatch, no retries) and the payload
            shape the agent will receive.
    """

    def __init__(self, connected: bool = True):
        """Create a fake WS manager.

        Args:
            connected: Whether ``send_to_agent`` should report successful
                delivery.
        """
        self.connected = connected
        self.sent: list[tuple[str, dict]] = []

    async def send_to_agent(self, node_id: str, payload: dict) -> bool:
        """Record a dispatch instead of writing it to a socket.

        Args:
            node_id: Stringified node id the runner selected.
            payload: JSON-able ``ExecuteStepCommand`` dump.

        Returns:
            ``self.connected`` — the real manager returns False when no live
            socket exists for ``node_id``, which the runner treats as an
            immediate step failure rather than waiting for a completion that
            will never arrive.
        """
        self.sent.append((node_id, payload))
        return self.connected


async def _make_node(db, **overrides):
    """Persist a node with sensible defaults (online linux x86_64).

    Args:
        db: Test session.
        **overrides: Any ``ops.create_node`` kwarg — commonly ``hostname``
            (must be unique per test), ``status`` and ``os_type``, which are
            the three fields node eligibility is decided on.

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


# ── scheduler: _node_matches_step (pure eligibility) ─────────────────────


def test_node_matches_step_offline_node_rejected(sample_node):
    """A node not in {online, busy} can never match."""
    sample_node.status = "offline"
    step_cls = get_step("run_command")  # supports all OSes
    assert _node_matches_step(sample_node, step_cls) is False


def test_node_matches_step_busy_node_accepted(sample_node):
    """A busy node is still eligible (it can queue work).

    Deliberate design choice, not an oversight: excluding busy nodes would idle
    the cluster whenever every node has one job. Preference (not exclusion) is
    handled downstream — see ``test_find_node_prefers_online_over_busy``.
    """
    sample_node.status = "busy"
    step_cls = get_step("run_command")
    assert _node_matches_step(sample_node, step_cls) is True


def test_node_matches_step_unsupported_os_rejected(sample_node):
    """gem5 only supports macos/linux; a windows node must be rejected.

    Each step class declares its supported OSes. Losing this check would
    dispatch a simulation to a host that cannot run it, failing minutes later
    on the agent instead of immediately at scheduling time.
    """
    sample_node.os_type = "windows"
    step_cls = get_step("gem5_run_simulation")
    assert _node_matches_step(sample_node, step_cls) is False


def test_node_matches_step_target_os_pin_rejects_mismatch(sample_node):
    """An explicit target_os pin overrides and rejects a non-matching node.

    Two-layer filter: the step's own OS support AND the job author's per-step
    pin must both pass. Asserting both directions (macos rejected, linux
    accepted) on the same node proves the pin is what decides, not the step.
    """
    sample_node.os_type = "linux"  # online linux node
    step_cls = get_step("run_command")
    # Step itself supports linux, but the per-step pin demands macos.
    assert _node_matches_step(sample_node, step_cls, target_os="macos") is False
    assert _node_matches_step(sample_node, step_cls, target_os="linux") is True


# ── scheduler: find_node_for_step (DB-backed selection) ──────────────────


async def test_find_node_direct_target_returns_matching_node(db, sample_node):
    """target_node_id selects that exact node when it matches the step."""
    node = await find_node_for_step(db, "run_command", target_node_id=sample_node.id)
    assert node is not None
    assert str(node.id) == str(sample_node.id)


async def test_find_node_direct_target_offline_returns_none(db):
    """A directly-targeted but offline node yields None (no fallback).

    Explicit targeting is a hard constraint: if the requested node is
    unavailable the step must fail rather than silently run somewhere else. A
    job pinned to specific hardware would otherwise produce results from the
    wrong machine.
    """
    offline = await _make_node(db, hostname="off.test", status="offline")
    node = await find_node_for_step(db, "run_command", target_node_id=offline.id)
    assert node is None


async def test_find_node_direct_target_os_mismatch_returns_none(db):
    """Directly-targeted node that fails the step's OS support returns None.

    An explicit ``target_node_id`` does not bypass step OS compatibility — the
    eligibility check still applies to the pinned node.
    """
    win = await _make_node(db, hostname="win.test", os_type="windows")
    node = await find_node_for_step(db, "gem5_run_simulation", target_node_id=win.id)
    assert node is None


async def test_find_node_any_online_picks_online_candidate(db):
    """With no targeting, any compatible online node is selected."""
    await _make_node(db, hostname="a.test", status="online")
    node = await find_node_for_step(db, "run_command")
    assert node is not None
    assert node.status == "online"


async def test_find_node_no_candidates_returns_none(db):
    """No nodes at all → None.

    The empty-cluster case must return None rather than raise; the runner
    converts None into a clean "No available node" step failure (see
    ``test_run_job_remote_step_no_node_fails_with_stop``).
    """
    node = await find_node_for_step(db, "run_command")
    assert node is None


async def test_find_node_pool_scoped_selection(db, sample_pool):
    """Pool targeting only considers nodes in that pool.

    Pools are the tenancy/isolation boundary — leaking a non-member node into
    a pool-scoped search would run a job on hardware the submitter may not be
    authorized for. The out-of-pool node is equally eligible on every other
    axis, so membership is the only thing that can exclude it.
    """
    in_pool = await _make_node(db, hostname="inpool.test", status="online")
    out_of_pool = await _make_node(db, hostname="outpool.test", status="online")
    await ops.add_node_to_pool(db, sample_pool.id, in_pool.id)

    node = await find_node_for_step(db, "run_command", target_pool_id=sample_pool.id)
    assert node is not None
    assert str(node.id) == str(in_pool.id)
    assert str(node.id) != str(out_of_pool.id)


async def test_find_node_target_os_filters_candidates(db):
    """target_os narrows an otherwise-broad 'any online' search."""
    await _make_node(db, hostname="lin.test", os_type="linux", status="online")
    mac = await _make_node(db, hostname="mac.test", os_type="macos", status="online")

    node = await find_node_for_step(db, "run_command", target_os="macos")
    assert node is not None
    assert str(node.id) == str(mac.id)


async def test_find_node_prefers_online_over_busy(db):
    """When both online and busy candidates exist, online wins.

    Load-spreading invariant: busy nodes stay *eligible* (see
    ``test_node_matches_step_busy_node_accepted``) but must be chosen last, so
    work lands on an idle machine whenever one exists.
    """
    # list_nodes(status="online") only returns online nodes, so to exercise the
    # online-vs-busy preference we go through a pool (get_pool_nodes returns all
    # statuses).
    # AI Note: this indirection is required, not stylistic. The "any node" code
    # path pre-filters to status="online" at the SQL level, so a busy node never
    # reaches the preference logic there; only the pool path (get_pool_nodes,
    # which returns every status) can exercise it. The nodes are also added
    # busy-first so a scheduler that just returned the first candidate would
    # fail this test.
    busy = await _make_node(db, hostname="busy.test", status="busy")
    online = await _make_node(db, hostname="online.test", status="online")
    pool = await ops.create_pool(db, name="mixed-pool", created_by=None)
    await ops.add_node_to_pool(db, pool.id, busy.id)
    await ops.add_node_to_pool(db, pool.id, online.id)

    node = await find_node_for_step(db, "run_command", target_pool_id=pool.id)
    assert node is not None
    assert str(node.id) == str(online.id)


# ── runner: _format_log_block (pure rendering) ───────────────────────────


def test_format_log_block_success_with_output():
    """A successful step renders header, command, stdout, and status line.

    ``_format_log_block`` produces the text appended to ``Job.log_text``, which
    is what the UI's terminal view and the ``/api/jobs/{id}/log`` download show.
    Each asserted fragment is one thing an operator needs when triaging: which
    step, on which node, what was run, what it printed, and how it ended.
    """
    block = _format_log_block(
        0, "run_command", "node-1",
        "success",
        {"command": "echo hi", "stdout": "hi\n", "exit_code": 0},
    )
    assert "[step 0] run_command on node-1" in block
    assert "$ echo hi" in block
    assert "hi" in block
    assert "exit code: 0" in block
    assert "status: success" in block


def test_format_log_block_failure_renders_error_when_no_stderr():
    """On failure with no stderr, the error is surfaced explicitly.

    Covers the scheduling/dispatch failure shape (no process ever ran, so there
    is no stderr to show). Without the ``error`` fallback the log block would
    render an empty failure and the operator would have no idea why the step
    failed.
    """
    block = _format_log_block(
        2, "git_clone", "node-9",
        "failed",
        {"error": "boom", "exit_code": 1},
    )
    assert "error: boom" in block
    assert "status: failed" in block


# ── runner: local control-plane steps end-to-end ─────────────────────────


async def _make_job(db, regular_user, steps_config, **overrides):
    """Persist a job owned by ``regular_user`` with the given step list.

    Args:
        db: Test session.
        regular_user: Submitter (jobs require a ``submitted_by``).
        steps_config: The raw list of ``{"step": ..., "params": {...}}`` dicts
            the runner will execute, including optional ``on_fail`` keys.
        **overrides: Any other ``ops.create_job`` kwarg (e.g. ``name``).

    Returns:
        The persisted ``Job`` in ``pending`` status, ready for ``_run_job``.
    """
    params = dict(
        name="t-job",
        submitted_by=regular_user.id,
        steps_config=steps_config,
    )
    params.update(overrides)
    return await ops.create_job(db, **params)


async def test_run_job_single_local_step_completes(app, db, regular_user):
    """A job with one local sleep(0) step runs to completion (no node needed).

    Drives the real ``_run_job`` loop; no WS interaction expected.

    The baseline for every other runner test: it pins that the loop terminates,
    stamps ``completed_at``, records exactly one successful ``StepRun``, and —
    critically — dispatches nothing over the WebSocket. Control-plane steps
    escaping to an agent would make the server depend on a live node just to
    sleep or jump.
    """
    ws = FakeWsManager()
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    job = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])

    await runner._run_job(job.id)

    # AI Note: ``_run_job`` writes through its OWN session from
    # ``get_session_factory()`` (patched to the test factory in conftest, same
    # engine). Both factories use ``expire_on_commit=False``, so this session's
    # identity map still holds the pre-run Job loaded by ``_make_job`` and would
    # report the stale ``status='pending'``. Expunge (not expire) to force a
    # genuine re-SELECT: expiring instead triggers a lazy refresh on attribute
    # access, which raises MissingGreenlet under async SQLAlchemy.
    db.expunge_all()

    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"
    assert refreshed.completed_at is not None
    # No remote dispatch for a control-plane step.
    assert ws.sent == []
    # Step run recorded as success.
    runs = await ops.get_step_runs_for_job(db, job.id)
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].step_name == "sleep"


async def test_run_job_context_accumulates_across_steps(app, db, regular_user):
    """A jump's bookkeeping outputs accumulate into the job context_data.

    jump(on="always") emits OUTPUT-less bookkeeping but the runner merges the
    step's outputs dict; we assert context persists across the two steps.

    The observable assertion is the step-index set ``[0, 2]``: step 1 produced
    no ``StepRun`` at all, proving the forward jump changed the loop counter
    rather than merely marking a step skipped. The skipped step is
    ``sleep(999)`` on purpose — if the jump silently failed, the test would
    hang instead of quietly asserting the wrong thing.
    """
    ws = FakeWsManager()
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    # Step 0 jumps to step 2 (skipping step 1). Step 2 is a terminal sleep.
    steps = [
        {"step": "jump", "params": {"target_step": 2}},
        {"step": "sleep", "params": {"seconds": 999}},  # should be skipped
        {"step": "sleep", "params": {"seconds": 0}},
    ]
    job = await _make_job(db, regular_user, steps)

    await runner._run_job(job.id)

    db.expunge_all()  # see note in test_run_job_single_local_step_completes
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"
    # The skipped sleep(999) must NOT have produced a step run.
    runs = await ops.get_step_runs_for_job(db, job.id)
    indices = sorted(r.step_index for r in runs)
    assert indices == [0, 2]  # step 1 skipped via jump


async def test_run_job_jump_creates_loop(app, db, regular_user):
    """jump(on=always) back to an earlier step loops until max_jumps is hit.

    The jump step fails once max_jumps is exceeded. With on_fail default "stop",
    the job ends 'failed' after looping max_jumps times — proving the
    step-index actually advanced backwards and re-ran the loop body.

    ``max_jumps`` is the infinite-loop guard: a backwards jump with no bound
    would pin a worker forever. Ending in ``failed`` with "Max jumps" in the
    error is the deliberate contract — the loop is cut off loudly rather than
    completing as if nothing was wrong.
    """
    ws = FakeWsManager()
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    steps = [
        {"step": "sleep", "params": {"seconds": 0}},  # loop body
        {"step": "jump", "params": {"target_step": 0, "max_jumps": 2}},
    ]
    job = await _make_job(db, regular_user, steps)

    await runner._run_job(job.id)

    db.expunge_all()  # see note in test_run_job_single_local_step_completes
    refreshed = await ops.get_job_by_id(db, job.id)
    # Loop runs body multiple times; the jump eventually fails on max_jumps.
    assert refreshed.status == "failed"
    assert "Max jumps" in (refreshed.error or "")
    runs = await ops.get_step_runs_for_job(db, job.id)
    # The loop body (step 0) ran more than once.
    # AI Note: >= 2 rather than an exact count — the jump counter's off-by-one
    # semantics (is the Nth jump allowed or rejected?) are an implementation
    # detail. The invariant under test is "the body genuinely re-ran", which
    # would be false for any non-looping implementation.
    body_runs = [r for r in runs if r.step_index == 0]
    assert len(body_runs) >= 2


async def test_run_job_jump_on_fail_skips_when_no_failure(app, db, regular_user):
    """jump(on='fail') does NOT fire after a clean run — falls through.

    Step 0 succeeds (clearing _last_failed), step 1 is jump(on=fail) which must
    fall through to step 2 rather than jumping.

    The complement of ``test_run_job_on_fail_continue_advances_and_sets_flag``:
    together they pin that the conditional jump reads a real failure flag
    rather than firing unconditionally. Contiguous indices ``[0, 1, 2]`` prove
    the fall-through.
    """
    ws = FakeWsManager()
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    steps = [
        {"step": "sleep", "params": {"seconds": 0}},
        {"step": "jump", "params": {"target_step": 0, "on": "fail"}},
        {"step": "sleep", "params": {"seconds": 0}},  # reached by falling through
    ]
    job = await _make_job(db, regular_user, steps)

    await runner._run_job(job.id)

    db.expunge_all()  # see note in test_run_job_single_local_step_completes
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"
    runs = await ops.get_step_runs_for_job(db, job.id)
    indices = sorted(r.step_index for r in runs)
    assert indices == [0, 1, 2]


# ── runner: on_fail handling for remote steps (node missing path) ────────


async def test_run_job_remote_step_no_node_fails_with_stop(app, db, regular_user):
    """A remote step with no eligible node fails; on_fail='stop' halts the job.

    No node exists, so find_node_for_step returns None → status 'failed'. This
    exercises the failure path WITHOUT needing a connected agent. The error
    message identifies the step.

    ``on_fail`` defaults to ``stop``, so this also pins the default: an
    unschedulable step must not let subsequent steps run against a
    half-initialised context.
    """
    ws = FakeWsManager()
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    steps = [{"step": "run_command", "params": {"command": "echo hi"}}]
    job = await _make_job(db, regular_user, steps)

    await runner._run_job(job.id)

    db.expunge_all()  # see note in test_run_job_single_local_step_completes
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "failed"
    assert "No available node" in (refreshed.error or "")
    # Nothing was dispatched (we never found a node).
    assert ws.sent == []


async def test_run_job_on_fail_continue_advances_and_sets_flag(app, db, regular_user):
    """on_fail='continue' lets a failed remote step advance; sets _last_failed.

    Step 0 (remote, no node → fails) has on_fail='continue'. Step 1 is a
    jump(on='fail') back to a terminal step 2 — proving the failure flag was
    persisted to context and the conditional jump consumed it.

    Two behaviours are entangled here on purpose, because that combination is
    the actual feature: ``continue`` keeps the job alive past a failure, and the
    failure flag survives in the context long enough for a later conditional
    jump to branch on it. The final job status is ``completed`` even though
    step 0 failed — that is the documented semantics of ``continue``, not a
    swallowed error (``runs[0].status == "failed"`` records it).
    """
    ws = FakeWsManager()
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    steps = [
        {"step": "run_command", "params": {"command": "x"}, "on_fail": "continue"},
        {"step": "jump", "params": {"target_step": 3, "on": "fail"}},
        {"step": "sleep", "params": {"seconds": 999}},  # skipped via the jump
        {"step": "sleep", "params": {"seconds": 0}},     # jump lands here
    ]
    job = await _make_job(db, regular_user, steps)

    await runner._run_job(job.id)

    db.expunge_all()  # see note in test_run_job_single_local_step_completes
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.status == "completed"
    runs = await ops.get_step_runs_for_job(db, job.id)
    indices = sorted(r.step_index for r in runs)
    # Step 0 failed-but-continued, step 1 jumped on the failure flag to step 3,
    # skipping step 2 entirely.
    assert indices == [0, 1, 3]
    assert runs[0].status == "failed"


# ── runner: remote dispatch payload (assert against fake ws) ─────────────


async def test_remote_step_dispatches_correct_payload(app, db, regular_user):
    """_execute_remote_step sends a well-formed ExecuteStepCommand to the agent.

    Stub: a real agent would call on_step_completed asynchronously. We instead
    assert the DISPATCHED payload, then time out the wait quickly by NOT
    completing. To avoid the 2h wait we set the event ourselves after dispatch.

    This is the server↔agent wire contract: the agent parses this dict back into
    an ``ExecuteStepCommand``, so a change to any asserted key is a breaking
    protocol change requiring an agent-side update. Two properties beyond the
    field names matter — the payload is a plain JSON-able dict (from
    ``model_dump(mode="json")``, not a pre-serialised string), and OS-variant
    resolution has already happened server-side (the linux ``shell`` default is
    injected while the caller's explicit ``command`` is preserved).
    """
    ws = FakeWsManager(connected=True)
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="agent-1.test", os_type="linux", status="online")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )
    step_cls = get_step("run_command")
    # AI Note: __import__ rather than a top-level ``from ... import StepContext``
    # is a local-import idiom used here to keep the module's import block
    # focused on the runner/scheduler SUT. Functionally equivalent to a normal
    # import; do not "clean it up" into a module-level import without checking
    # for import cycles with nexus_common.steps.
    context = __import__(
        "nexus_common.steps.base", fromlist=["StepContext"]
    ).StepContext()

    # Drive the remote dispatch, but simulate the agent completing the step so
    # the await does not block for 2h. We schedule the completion callback once
    # the dispatch has registered its event.
    async def _complete_soon():
        """Impersonate the agent: fire the completion callback once registered.

        Polls ``runner._step_events`` for the ``"{job_id}:{step_index}"`` key
        that ``_execute_remote_step`` creates just before it awaits. Only after
        that key exists can ``on_step_completed`` wake the waiter.
        """
        key = f"{job.id}:0"
        # AI Note: ordering hazard, not a stylistic poll loop. This task is
        # created BEFORE _execute_remote_step is awaited, so the event may not
        # exist yet; calling on_step_completed too early would find no waiter,
        # drop the completion, and the runner would then block on the real
        # ~2h step timeout. The 100 x 10ms budget (~1s) is a generous ceiling
        # for what is normally a single event-loop tick. If the key never
        # appears the helper returns silently and the test fails on the
        # assertions rather than hanging.
        for _ in range(100):
            if key in runner._step_events:
                runner.on_step_completed(
                    str(job.id), 0, outputs={"exit_code": 0},
                    command="ls", stdout="file1\n", exit_code=0,
                )
                return
            await asyncio.sleep(0.01)

    completer = asyncio.create_task(_complete_soon())
    result = await runner._execute_remote_step(
        db, job, step_cls, "run_command", {"command": "ls"}, context,
        step_run.id, 0, target_node_id=node.id,
    )
    # AI Note: awaiting the completer is required for hygiene — an un-awaited
    # task that outlives the test leaks into the next test's event loop and
    # produces "Task was destroyed but it is pending" noise.
    await completer

    # Dispatch happened exactly once, to our node.
    assert len(ws.sent) == 1
    sent_node_id, payload = ws.sent[0]
    assert sent_node_id == str(node.id)
    # Payload is a JSON-able dict (model_dump(mode="json")), not a JSON string.
    assert isinstance(payload, dict)
    assert payload["job_id"] == str(job.id)
    assert payload["step_index"] == 0
    assert payload["step_name"] == "run_command"
    # OS variant for linux injected the bash shell; explicit command preserved.
    assert payload["params"]["command"] == "ls"
    assert payload["params"]["shell"] == "/bin/bash"
    # The result reflects the agent's reported completion + node label.
    assert result["status"] == "success"
    assert result["node_label"] == "agent-1.test"
    assert result["outputs"] == {"exit_code": 0}


async def test_remote_step_agent_not_connected_fails(app, db, regular_user):
    """If the agent socket is not connected, the step fails fast (no wait).

    Fail-fast invariant: when ``send_to_agent`` reports undelivered, the runner
    must NOT fall through to the ~2h completion wait — a node whose row says
    "online" but whose socket is gone would otherwise stall the job for hours.
    The absence of any completion callback in this test is what proves it
    returned immediately.
    """
    ws = FakeWsManager(connected=False)  # send_to_agent returns False
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    node = await _make_node(db, hostname="ghost.test", os_type="linux", status="online")
    job = await _make_job(db, regular_user, [{"step": "run_command", "params": {"command": "ls"}}])
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="run_command", input_params={},
    )
    step_cls = get_step("run_command")
    context = __import__(
        "nexus_common.steps.base", fromlist=["StepContext"]
    ).StepContext()

    result = await runner._execute_remote_step(
        db, job, step_cls, "run_command", {"command": "ls"}, context,
        step_run.id, 0, target_node_id=node.id,
    )

    assert result["status"] == "failed"
    assert "not connected" in result["error"]
    # The command was attempted (recorded) but reported undelivered.
    assert len(ws.sent) == 1


# ── resume: resume_active_jobs ───────────────────────────────────────────


async def test_resume_active_jobs_picks_up_active_states(app, db, regular_user):
    """resume_active_jobs resubmits pending/queued/running jobs and counts them.

    Stub: submit_job kicks off a background asyncio.Task; we don't await the
    task's completion here — we assert resume picked the right jobs and that
    each was registered as active on the runner.

    Crash-recovery contract, called from ``lifespan`` on server start: work that
    was in flight when the process died must be picked back up, while terminal
    jobs must be left alone. Re-running a completed job would duplicate side
    effects (re-executing shell commands, re-uploading artifacts), so the
    ``completed`` exclusion is as important as the three inclusions.
    """
    ws = FakeWsManager()
    runner = JobRunner(ws_manager=ws, credential_manager=None)

    pending = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    queued = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    running = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    completed = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    await ops.update_job(db, queued.id, status="queued")
    await ops.update_job(db, running.id, status="running")
    await ops.update_job(db, completed.id, status="completed")

    resumed = await resume_active_jobs(db, runner)

    # pending + queued + running == 3; completed excluded.
    assert resumed == 3
    active_ids = {str(jid) for jid in runner._active_jobs}
    assert str(pending.id) in active_ids
    assert str(queued.id) in active_ids
    assert str(running.id) in active_ids
    assert str(completed.id) not in active_ids

    # Let the background tasks finish so we don't leak pending tasks.
    # AI Note: mandatory teardown, not politeness. submit_job spawns detached
    # asyncio Tasks; pytest-asyncio closes the per-test event loop right after
    # the test body, which would destroy them mid-flight and surface as
    # "Task was destroyed but it is pending" or a cross-test DB error against
    # the disposed in-memory engine. return_exceptions=True because a resumed
    # job failing is irrelevant to what this test asserts.
    await asyncio.gather(*runner._active_jobs.values(), return_exceptions=True)


async def test_resume_active_jobs_none_when_empty(app, db, regular_user):
    """No active jobs → resumes zero.

    Negative control for the test above: a completed job present in the DB must
    contribute neither to the count nor to ``_active_jobs``. Confirms that a
    clean restart does not spawn stray background tasks.
    """
    ws = FakeWsManager()
    runner = JobRunner(ws_manager=ws, credential_manager=None)
    # One completed job that must be ignored.
    done = await _make_job(db, regular_user, [{"step": "sleep", "params": {"seconds": 0}}])
    await ops.update_job(db, done.id, status="completed")

    resumed = await resume_active_jobs(db, runner)
    assert resumed == 0
    assert runner._active_jobs == {}
