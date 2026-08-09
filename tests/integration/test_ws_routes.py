"""Integration tests for the WebSocket layer.

SUT
    * ``packages/server/src/nexus_server/api/routes/ws.py`` —
      :class:`ConnectionManager` (the process-wide socket registry + fan-out),
      the ``/ws/agent/{node_id}`` endpoint with its api_key handshake, the
      ``_handle_agent_message`` dispatch table, and the ``/ws/dashboard``
      receive-only feed.
    * ``packages/server/src/nexus_server/ws/__init__.py`` — a deliberately empty
      placeholder package. One test pins that emptiness, because the live
      ``ConnectionManager`` must stay a single per-process instance and a second
      copy hiding in that namespace would silently break step dispatch.

Two complementary drivers (why both exist)
    1. ``TestClient.websocket_connect`` — used for everything that depends on
       the real ASGI handshake: the 4001/4003 close codes, an end-to-end
       heartbeat/ack round trip, and the dashboard fan-out (all sockets opened
       from one ``TestClient`` context share a single ``BlockingPortal``, hence
       one event loop, so a broadcast raised by the agent socket really does
       reach a dashboard socket).
    2. Direct ``await ws_mod.agent_websocket(fake_socket, ...)`` with a scripted
       fake socket — used for everything where the ASGI client is
       *non-deterministic*. ``WebSocketTestSession.__exit__`` cancels the app
       task immediately after queueing the disconnect, so the endpoint's
       ``finally`` block may be cancelled at its first ``await``; the DB
       "mark offline" write is therefore not reliably observable through
       ``TestClient``. Driving the coroutine directly in the test's own event
       loop makes teardown, per-message DB effects and error branches exact.

Cross-session staleness
    The endpoint writes through its OWN session obtained from
    ``db.session.get_session()`` (repointed at the test engine by the ``app``
    fixture). Both that session and the ``db`` fixture use
    ``expire_on_commit=False``, so the ``db`` identity map can hand back a stale
    object. Every assertion on endpoint-written rows calls ``db.expunge_all()``
    first and re-reads through ``ops``. Never ``db.expire_all()`` — under async
    SQLAlchemy that triggers a lazy refresh on attribute access and raises
    ``MissingGreenlet``.

Shared mutable state
    ``ws.manager`` is a module-level singleton (``main.lifespan`` hands that
    exact object to ``JobRunner``). The autouse ``ws_manager`` fixture below
    empties both registries around every test and restores whatever was there,
    so a leaked socket in one test cannot make another test's assertions pass.

Division of responsibility being tested
    The WS layer writes only *observational* state: node status/heartbeat plus
    the agent-reported ``running`` snapshot of a step run. Terminal step status,
    outputs and job advancement belong to ``JobRunner`` — several tests assert
    the handler merely *calls back* into the runner and does not write those
    columns itself.

UUID/SQLite regression (the reason this file exists)
    Every ID column is ``String(36)``. Commit d895144 removed four
    ``UUID(node_id)`` / ``UUID(info.job_id)`` conversions from this module:
    binding a ``uuid.UUID`` to those columns raises
    ``sqlite3.ProgrammingError: type 'UUID' is not supported``, which escaped
    into ``agent_websocket``'s ``except Exception``, killed the socket on *every*
    step message, and turned into a reconnect storm with stuck jobs. The
    "UUID-typed ids" section drives UUID objects through the handler and the
    ``ops`` calls it makes, including the ``**kwargs`` path that was the last one
    left un-coerced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from starlette.websockets import WebSocketDisconnect

from nexus_server.api.routes import ws as ws_mod
from nexus_server.db import ops


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeDashboardSocket:
    """Stand-in for a browser dashboard socket.

    Only ``accept`` and ``send_json`` are exercised by
    :class:`ConnectionManager`, so nothing else is implemented. Being a plain
    object (not an ASGI socket) it is safe to call from any event loop, which is
    what lets the fake-socket agent tests observe broadcasts.

    Attributes:
        fail: When True, ``send_json`` raises — the "browser tab went away"
            case the manager must treat as a disconnect rather than propagate.
        accepted: Whether ``accept()`` was awaited.
        received: Every frame handed to ``send_json``, in order.
    """

    def __init__(self, fail: bool = False):
        """Create a fake dashboard client.

        Args:
            fail: Make every ``send_json`` raise, simulating a dead socket.
        """
        self.fail = fail
        self.accepted = False
        self.received: list[dict] = []

    async def accept(self) -> None:
        """Record that the handshake was accepted."""
        self.accepted = True

    async def send_json(self, message: dict) -> None:
        """Record a broadcast frame, or raise when ``fail`` is set.

        Raises:
            RuntimeError: When ``fail`` is True.
        """
        if self.fail:
            raise RuntimeError("dashboard socket is gone")
        self.received.append(message)


class _FakeAgentSocket:
    """Stand-in for an agent socket, used for the manager-only tests.

    Attributes:
        fail: When True, ``send_json`` raises so ``send_to_agent`` takes its
            eviction path.
        accepted: Whether ``accept()`` was awaited.
        sent: Every frame handed to ``send_json``.
    """

    def __init__(self, fail: bool = False):
        """Create a fake agent socket.

        Args:
            fail: Make every ``send_json`` raise.
        """
        self.fail = fail
        self.accepted = False
        self.sent: list[dict] = []

    async def accept(self) -> None:
        """Record that the handshake was accepted."""
        self.accepted = True

    async def send_json(self, message: dict) -> None:
        """Record an outbound frame, or raise when ``fail`` is set.

        Raises:
            RuntimeError: When ``fail`` is True.
        """
        if self.fail:
            raise RuntimeError("agent socket is gone")
        self.sent.append(message)


class _ScriptedAgentSocket:
    """A scripted agent socket for driving ``agent_websocket`` directly.

    ``receive_text`` replays ``frames`` and then raises
    ``WebSocketDisconnect``, which is exactly how a well-behaved agent session
    ends — so one call to :func:`agent_websocket` runs connect, N messages and
    teardown deterministically inside the test's own event loop.

    Attributes:
        accepted: Whether the endpoint accepted the handshake. Stays False on
            the auth-rejection paths, which close *before* accepting.
        sent: Frames the server pushed down the socket (``ack`` envelopes).
        closed: ``(code, reason)`` for every ``close()`` call — the 4001/4003
            rejections.
        app: Minimal ``ws.app.state.runner`` shim; ``step.completed`` /
            ``step.failed`` reach the runner through this attribute.
    """

    def __init__(self, frames=(), runner=None):
        """Create a scripted socket.

        Args:
            frames: Inbound frames. ``str`` entries are delivered verbatim (so
                malformed JSON can be tested); anything else is
                ``json.dumps``-ed first.
            runner: Object exposed as ``ws.app.state.runner``.
        """
        self._frames = list(frames)
        self.accepted = False
        self.sent: list[dict] = []
        self.closed: list[tuple[int, str | None]] = []
        self.app = SimpleNamespace(state=SimpleNamespace(runner=runner))

    async def accept(self) -> None:
        """Record that the handshake was accepted."""
        self.accepted = True

    async def receive_text(self) -> str:
        """Return the next scripted frame, or end the session.

        Raises:
            WebSocketDisconnect: Once the script is exhausted, mirroring a
                client that closed the connection.
        """
        if not self._frames:
            raise WebSocketDisconnect(code=1000)
        frame = self._frames.pop(0)
        return frame if isinstance(frame, str) else json.dumps(frame)

    async def send_json(self, message: dict) -> None:
        """Record a server->agent frame."""
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        """Record a close, as the auth-rejection branches perform it."""
        self.closed.append((code, reason))


class _RecordingRunner:
    """Captures the two ``JobRunner`` callbacks the WS handler invokes.

    The real runner deposits a result and sets an ``asyncio.Event``; all the WS
    layer owes it is a correctly-shaped *synchronous* call, so recording the
    arguments is a complete test of this seam.

    Attributes:
        completed: One dict per ``on_step_completed`` call.
        failed: One dict per ``on_step_failed`` call.
    """

    def __init__(self):
        """Start with no recorded callbacks."""
        self.completed: list[dict] = []
        self.failed: list[dict] = []

    def on_step_completed(self, job_id, step_index, outputs, command=None,
                          stdout=None, stderr=None, exit_code=None) -> None:
        """Record a success callback with every argument the handler passes."""
        self.completed.append({
            "job_id": job_id, "step_index": step_index, "outputs": outputs,
            "command": command, "stdout": stdout, "stderr": stderr,
            "exit_code": exit_code,
        })

    def on_step_failed(self, job_id, step_index, error, command=None,
                       stdout=None, stderr=None, exit_code=None) -> None:
        """Record a failure callback with every argument the handler passes."""
        self.failed.append({
            "job_id": job_id, "step_index": step_index, "error": error,
            "command": command, "stdout": stdout, "stderr": stderr,
            "exit_code": exit_code,
        })


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def ws_manager():
    """The module-level :data:`ws.manager`, emptied around every test.

    ``ws.manager`` is process-global and is the *same object* ``JobRunner``
    holds, so it cannot be replaced. Clearing both registries before and after
    each test keeps a socket leaked by one test from satisfying another test's
    assertion, and restores any pre-existing entries for the rest of the run.

    Yields:
        The live ``ConnectionManager`` singleton with empty registries.
    """
    saved_agents = dict(ws_mod.manager.agent_connections)
    saved_dashboards = list(ws_mod.manager.dashboard_connections)
    ws_mod.manager.agent_connections.clear()
    ws_mod.manager.dashboard_connections.clear()
    yield ws_mod.manager
    ws_mod.manager.agent_connections.clear()
    ws_mod.manager.agent_connections.update(saved_agents)
    ws_mod.manager.dashboard_connections.clear()
    ws_mod.manager.dashboard_connections.extend(saved_dashboards)


@pytest_asyncio.fixture
async def sentinel_dashboard(ws_manager):
    """A loop-agnostic fake dashboard client registered on the live manager.

    Two jobs:

    * It observes the real endpoint's broadcasts without needing an ASGI socket,
      so tests can assert on fan-out content from either event loop.
    * It is the completion signal used by :func:`_close_agent_socket`. The last
      thing ``agent_websocket``'s ``finally`` does is broadcast the ``offline``
      frame, so its arrival here proves the server-side teardown ran to the end.
      Without that wait, ``WebSocketTestSession.__exit__`` cancels the app task
      immediately after queueing the disconnect, which can abort the teardown
      mid-``await`` and (because the test engine uses a single StaticPool
      connection) leave the shared aiosqlite connection unusable.

    Yields:
        The registered :class:`_FakeDashboardSocket`.
    """
    sock = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(sock)
    yield sock
    ws_manager.disconnect_dashboard(sock)


# ── helpers ──────────────────────────────────────────────────────────────────


def _heartbeat(node_id: str, **overrides) -> dict:
    """A valid ``AgentHeartbeat`` frame.

    Args:
        node_id: Node id to report (informational; the server trusts the socket).
        **overrides: Field replacements, e.g. ``active_steps``.

    Returns:
        A JSON-able frame dict.
    """
    frame = {
        "type": "heartbeat",
        "node_id": str(node_id),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "load_avg": 0.5,
        "memory_used_pct": 42.0,
        "active_steps": 1,
    }
    frame.update(overrides)
    return frame


def _register(node_id: str, **overrides) -> dict:
    """A valid ``AgentRegister`` frame with every required field present.

    Args:
        node_id: Node id to report.
        **overrides: Field replacements — a 422-style ValidationError from a
            test using this helper means the override introduced it.

    Returns:
        A JSON-able frame dict.
    """
    frame = {
        "type": "register",
        "node_id": str(node_id),
        "hostname": "agent-reported.test",
        "os_type": "macos",
        "os_version": "15.1",
        "arch": "arm64",
        "cpu_model": "Apple M3 Max",
        "cpu_cores": 14,
        "ram_mb": 36864,
        "gpu_info": "M3 Max 30-core",
        "agent_version": "9.9.9",
        "ip_address": "10.7.7.7",
        "tags": ["agent-owned"],
    }
    frame.update(overrides)
    return frame


async def _make_node(db, **overrides):
    """Persist a node with the fields the WS layer cares about.

    Args:
        db: Test session.
        **overrides: Any ``ops.create_node`` kwarg; ``hostname`` must be unique
            per node within a test.

    Returns:
        The persisted ``Node`` (its generated ``api_key`` is the agent
        credential the socket authenticates with).
    """
    params = dict(
        hostname="ws-node.test",
        os_type="linux",
        os_version="Ubuntu 24.04",
        arch="x86_64",
        cpu_model="Xeon",
        cpu_cores=4,
        ram_mb=8192,
        agent_version="0.1.0",
        ip_address="10.0.0.9",
        status="online",
    )
    params.update(overrides)
    return await ops.create_node(db, **params)


async def _make_job_with_step_run(db, user, step_index: int = 0):
    """Persist a job plus one pending ``step_run`` at ``step_index``.

    The job id is passed in its string form, which is what the runner does;
    ``ops.create_step_run`` coerces it anyway (see the ``node_id`` kwarg test),
    so this is convention rather than a requirement.

    Args:
        db: Test session.
        user: Owner (``submitted_by``).
        step_index: Index the step run is recorded at.

    Returns:
        ``(job, step_run)``.
    """
    job = await ops.create_job(
        db, name="ws-job", submitted_by=user.id,
        steps_config=[{"step": "run_command", "params": {"command": "echo hi"}}],
    )
    step_run = await ops.create_step_run(
        db, str(job.id), step_index, "run_command", input_params={"command": "echo hi"},
    )
    return job, step_run


async def _run_agent_session(node, frames=(), runner=None, api_key=None, node_id=None):
    """Drive ``agent_websocket`` to completion against a scripted socket.

    Runs connect + auth, every frame in ``frames``, then the teardown path (the
    scripted socket raises ``WebSocketDisconnect`` when the script runs out).
    Everything happens in the test's own event loop, so DB effects are settled
    by the time this returns.

    Args:
        node: The ``Node`` the socket belongs to.
        frames: Inbound frames to replay.
        runner: Object exposed as ``ws.app.state.runner``.
        api_key: Credential to present; defaults to ``node.api_key``.
        node_id: Path parameter; defaults to ``str(node.id)``.

    Returns:
        The :class:`_ScriptedAgentSocket` after the session ended.
    """
    sock = _ScriptedAgentSocket(frames, runner=runner)
    await ws_mod.agent_websocket(
        sock,
        node_id=str(node.id) if node_id is None else node_id,
        api_key=node.api_key if api_key is None else api_key,
    )
    return sock


def _frames_of_type(frames: list[dict], msg_type: str) -> list[dict]:
    """Filter recorded frames by their ``type`` discriminator.

    Args:
        frames: Recorded frames.
        msg_type: The ``type`` value to keep.

    Returns:
        Matching frames in order.
    """
    return [f for f in frames if f.get("type") == msg_type]


async def _close_agent_socket(socket, sentinel: _FakeDashboardSocket) -> None:
    """Close a ``TestClient`` agent socket and wait for the server teardown.

    ``WebSocketTestSession.__exit__`` queues the disconnect and then cancels the
    app task, so the endpoint's ``finally`` block (which opens a session to mark
    the node offline) can be aborted at its first ``await``. Because the test
    engine shares one StaticPool connection, that mid-flight cancellation can
    also break the connection for the fixture teardown. Closing explicitly and
    waiting for the ``offline`` broadcast — the last statement in the ``finally``
    — makes every ``TestClient`` agent test deterministic.

    Args:
        socket: The open ``WebSocketTestSession`` for ``/ws/agent/...``.
        sentinel: A fake dashboard registered on the manager, used as the
            completion signal.

    Raises:
        AssertionError: If the teardown broadcast never arrives.
    """
    already_seen = len(sentinel.received)
    socket.close(1000)
    for _ in range(500):
        for frame in sentinel.received[already_seen:]:
            if frame.get("type") == "node.status" and frame.get("status") == "offline":
                return
        await asyncio.sleep(0.01)
    raise AssertionError("agent WebSocket teardown did not complete")


# ── ConnectionManager: agent registry ────────────────────────────────────────


async def test_connect_agent_accepts_handshake_and_registers_socket(ws_manager):
    """connect_agent accepts the socket and keys it by the node id string.

    The key must be exactly what ``JobRunner`` passes to ``send_to_agent``
    (``str(node.id)``); anything else makes every remote dispatch report the
    node as disconnected.
    """
    sock = _FakeAgentSocket()
    node_id = str(uuid.uuid4())

    await ws_manager.connect_agent(node_id, sock)

    assert sock.accepted is True
    assert ws_manager.agent_connections == {node_id: sock}


async def test_connect_agent_reconnect_replaces_entry_without_closing_old_socket(ws_manager):
    """A second connect for the same node overwrites the entry silently.

    Documents the intended reconnect-after-blip behaviour: the stale socket is
    dropped from the registry but never closed. Prevents a "fix" that starts
    rejecting reconnects (which would leave nodes permanently unreachable after
    a network blip) from landing unnoticed.
    """
    node_id = str(uuid.uuid4())
    first, second = _FakeAgentSocket(), _FakeAgentSocket()

    await ws_manager.connect_agent(node_id, first)
    await ws_manager.connect_agent(node_id, second)

    assert ws_manager.agent_connections[node_id] is second
    assert len(ws_manager.agent_connections) == 1
    # The old socket was never told to go away — only forgotten.
    assert first.sent == []


async def test_disconnect_agent_removes_registry_entry(ws_manager):
    """disconnect_agent evicts the node so the scheduler stops picking it."""
    node_id = str(uuid.uuid4())
    await ws_manager.connect_agent(node_id, _FakeAgentSocket())

    ws_manager.disconnect_agent(node_id)

    assert node_id not in ws_manager.agent_connections


async def test_disconnect_agent_is_idempotent_and_tolerates_unknown_nodes(ws_manager):
    """Disconnecting twice, or a node that never connected, does not raise.

    Both ``send_to_agent``'s failure path and ``agent_websocket``'s ``finally``
    call this for the same node, so double eviction is a normal occurrence
    rather than an error — the ``pop(..., None)`` must stay.
    """
    node_id = str(uuid.uuid4())
    await ws_manager.connect_agent(node_id, _FakeAgentSocket())

    ws_manager.disconnect_agent(node_id)
    ws_manager.disconnect_agent(node_id)
    ws_manager.disconnect_agent("never-connected")

    assert ws_manager.agent_connections == {}


async def test_send_to_agent_returns_false_when_node_has_no_socket(ws_manager):
    """send_to_agent reports False for an unconnected node instead of raising.

    The runner turns that False into an immediate step failure; an exception
    here would instead crash the job task.
    """
    assert await ws_manager.send_to_agent(str(uuid.uuid4()), {"type": "execute_step"}) is False


async def test_send_to_agent_delivers_payload_and_returns_true(ws_manager):
    """A connected agent receives the exact payload and send_to_agent returns True."""
    node_id = str(uuid.uuid4())
    sock = _FakeAgentSocket()
    await ws_manager.connect_agent(node_id, sock)
    payload = {"type": "execute_step", "job_id": "j1", "step_index": 0}

    assert await ws_manager.send_to_agent(node_id, payload) is True
    assert sock.sent == [payload]


async def test_send_to_agent_evicts_node_when_send_raises(ws_manager):
    """A failed send returns False and drops the connection from the registry.

    Eviction on write failure is what stops the scheduler from repeatedly
    dispatching to a socket whose transport is already dead.
    """
    node_id = str(uuid.uuid4())
    await ws_manager.connect_agent(node_id, _FakeAgentSocket(fail=True))

    assert await ws_manager.send_to_agent(node_id, {"type": "execute_step"}) is False
    assert node_id not in ws_manager.agent_connections


# ── ConnectionManager: dashboard registry + fan-out ──────────────────────────


async def test_connect_dashboard_accepts_and_appends_client(ws_manager):
    """connect_dashboard accepts the socket and appends it to the fan-out list."""
    sock = _FakeDashboardSocket()

    await ws_manager.connect_dashboard(sock)

    assert sock.accepted is True
    assert ws_manager.dashboard_connections == [sock]


async def test_disconnect_dashboard_removes_only_the_given_client(ws_manager):
    """Dropping one dashboard client leaves the others subscribed."""
    keep, drop = _FakeDashboardSocket(), _FakeDashboardSocket()
    await ws_manager.connect_dashboard(keep)
    await ws_manager.connect_dashboard(drop)

    ws_manager.disconnect_dashboard(drop)

    assert ws_manager.dashboard_connections == [keep]


async def test_disconnect_dashboard_for_unregistered_socket_is_a_noop(ws_manager):
    """Disconnecting an unknown dashboard socket does not raise.

    It is called from both the reaping loop in ``broadcast_to_dashboards`` and
    the endpoint's ``finally``, so it must tolerate an already-removed socket.
    """
    known = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(known)

    ws_manager.disconnect_dashboard(_FakeDashboardSocket())

    assert ws_manager.dashboard_connections == [known]


async def test_broadcast_to_dashboards_fans_out_to_every_client(ws_manager):
    """Every connected dashboard receives the same frame object."""
    a, b, c = _FakeDashboardSocket(), _FakeDashboardSocket(), _FakeDashboardSocket()
    for sock in (a, b, c):
        await ws_manager.connect_dashboard(sock)
    message = {"type": "node.status", "node_id": "n1", "status": "online"}

    await ws_manager.broadcast_to_dashboards(message)

    assert a.received == [message]
    assert b.received == [message]
    assert c.received == [message]


async def test_broadcast_to_dashboards_drops_failing_client_but_still_serves_the_rest(ws_manager):
    """A dead client in the middle is reaped *after* the loop, not during it.

    Regression guard for the deferred-removal comment: mutating
    ``dashboard_connections`` while iterating would skip the client positioned
    after the failing one, silently starving a healthy browser tab.
    """
    first, dead, last = _FakeDashboardSocket(), _FakeDashboardSocket(fail=True), _FakeDashboardSocket()
    for sock in (first, dead, last):
        await ws_manager.connect_dashboard(sock)

    await ws_manager.broadcast_to_dashboards({"type": "job.status", "job_id": "j", "status": "running"})

    assert len(first.received) == 1
    assert len(last.received) == 1
    assert ws_manager.dashboard_connections == [first, last]


async def test_broadcast_to_dashboards_never_raises_when_every_client_fails(ws_manager):
    """A broadcast where all sends fail completes quietly and empties the list.

    This must never raise: it is awaited from inside the agent receive loop, so
    an escaping exception would tear down a healthy agent connection because a
    browser tab went away.
    """
    for _ in range(3):
        await ws_manager.connect_dashboard(_FakeDashboardSocket(fail=True))

    await ws_manager.broadcast_to_dashboards({"type": "node.status", "node_id": "n", "status": "offline"})

    assert ws_manager.dashboard_connections == []


async def test_broadcast_to_dashboards_with_no_clients_is_a_noop(ws_manager):
    """Broadcasting with zero subscribers is legal (the common headless case)."""
    await ws_manager.broadcast_to_dashboards({"type": "node.status", "node_id": "n", "status": "online"})

    assert ws_manager.dashboard_connections == []


def test_manager_singleton_is_the_object_handed_to_the_job_runner(app, ws_manager):
    """``app.state.runner._ws`` is the very same ``ws.manager`` instance.

    The runner and the socket handler must observe one connection table: with
    two ``ConnectionManager`` objects, dispatched steps would wait forever on
    events delivered to the other one.
    """
    assert app.state.runner._ws is ws_mod.manager
    assert ws_manager is ws_mod.manager


def test_ws_subpackage_is_docstring_only_placeholder():
    """``nexus_server.ws`` exposes no ConnectionManager of its own.

    The package is a reserved namespace; the live registry lives in
    ``api/routes/ws.py``. Pins the invariant that exactly one
    ``ConnectionManager`` class/instance exists per process, so a partial
    extraction into this namespace cannot silently split the registry.
    """
    import nexus_server.ws as ws_pkg

    public = [name for name in vars(ws_pkg) if not name.startswith("_")]
    assert public == []
    assert not hasattr(ws_pkg, "ConnectionManager")
    assert not hasattr(ws_pkg, "manager")


# ── /ws/agent: authentication (real ASGI handshake) ──────────────────────────


async def test_agent_ws_without_api_key_is_closed_with_4001(client, sample_node, ws_manager):
    """A handshake with no ``api_key`` query parameter is rejected with 4001.

    The close happens before ``accept()``, so the client sees a rejected
    connection and nothing is registered.
    """
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/ws/agent/{sample_node.id}"):
            pass

    assert excinfo.value.code == 4001
    assert ws_manager.agent_connections == {}


async def test_agent_ws_with_unknown_api_key_is_closed_with_4003(client, sample_node, ws_manager):
    """An api_key that matches no node row is rejected with 4003."""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            f"/ws/agent/{sample_node.id}", params={"api_key": "not-a-real-key"},
        ):
            pass

    assert excinfo.value.code == 4003
    assert ws_manager.agent_connections == {}


async def test_agent_ws_with_another_nodes_valid_key_is_closed_with_4003(client, db, sample_node, ws_manager):
    """A valid key for node A cannot be used to impersonate node B.

    This is the ``str(node.id) != node_id`` guard — without it, any node's agent
    credential would grant control over every other node's socket.
    """
    other = await _make_node(db, hostname="impersonation-target.test")

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            f"/ws/agent/{other.id}", params={"api_key": sample_node.api_key},
        ):
            pass

    assert excinfo.value.code == 4003
    assert ws_manager.agent_connections == {}


async def test_agent_ws_with_valid_key_accepts_and_registers_the_connection(
    client, db, sample_node, ws_manager, sentinel_dashboard,
):
    """A correct api_key completes the upgrade and registers the live socket.

    The registry key is the node-id string, which is what makes the node
    reachable by ``JobRunner.send_to_agent``.
    """
    node_id = str(sample_node.id)

    with client.websocket_connect(f"/ws/agent/{node_id}", params={"api_key": sample_node.api_key}) as socket:
        assert node_id in ws_manager.agent_connections
        await _close_agent_socket(socket, sentinel_dashboard)

    # Teardown ran: the finally block evicts the socket.
    assert node_id not in ws_manager.agent_connections


async def test_agent_ws_heartbeat_round_trip_returns_ack_over_the_real_socket(
    client, sample_node, sentinel_dashboard,
):
    """End-to-end over ASGI: a heartbeat frame is answered with ``ack``.

    Exercises the real receive loop / json decode / dispatch / send_json path
    rather than the direct-coroutine shortcut the rest of the dispatch tests use.
    """
    with client.websocket_connect(
        f"/ws/agent/{sample_node.id}", params={"api_key": sample_node.api_key},
    ) as socket:
        socket.send_json(_heartbeat(sample_node.id))
        ack = socket.receive_json()
        await _close_agent_socket(socket, sentinel_dashboard)

    assert ack == {"type": "ack", "message": "heartbeat_ok"}


async def test_agent_ws_handles_many_step_messages_on_one_socket(
    client, db, regular_user, sample_node, sentinel_dashboard,
):
    """A full step message sequence is served on a single socket.

    THE headline regression (commit d895144): a ``uuid.UUID`` bound to a
    ``String(36)`` column raised inside ``_handle_agent_message``, escaped into
    ``agent_websocket``'s ``except Exception`` and killed the socket on *every*
    step message — producing a reconnect storm and stuck jobs. Here
    step.started/log/progress/completed all flow down one connection and the
    socket is still alive afterwards (proved by a trailing heartbeat that still
    gets acked).
    """
    job, step_run = await _make_job_with_step_run(db, regular_user)
    app_runner = _RecordingRunner()
    client.app.state.runner = app_runner
    job_id = str(job.id)

    with client.websocket_connect(
        f"/ws/agent/{sample_node.id}", params={"api_key": sample_node.api_key},
    ) as socket:
        socket.send_json({"type": "step.started", "job_id": job_id, "step_index": 0,
                          "state": {"pid": 4242}})
        socket.send_json({"type": "step.log", "job_id": job_id, "step_index": 0,
                          "stream": "stdout", "line": "hello",
                          "timestamp": datetime.now(timezone.utc).isoformat()})
        socket.send_json({"type": "step.progress", "job_id": job_id, "step_index": 0,
                          "percent": 50.0, "message": "halfway"})
        socket.send_json({"type": "step.completed", "job_id": job_id, "step_index": 0,
                          "outputs": {"stdout": "hello"}, "exit_code": 0})
        # Only heartbeat/register are acked, so a received ack proves every
        # preceding frame was processed without tearing the socket down.
        socket.send_json(_heartbeat(sample_node.id))
        ack = socket.receive_json()
        await _close_agent_socket(socket, sentinel_dashboard)

    assert ack == {"type": "ack", "message": "heartbeat_ok"}
    assert len(app_runner.completed) == 1
    assert app_runner.completed[0]["job_id"] == job_id
    assert len(_frames_of_type(sentinel_dashboard.received, "step.log")) == 1
    assert len(_frames_of_type(sentinel_dashboard.received, "step.progress")) == 1

    db.expunge_all()
    refreshed = await ops.get_latest_step_run(db, job_id, 0)
    assert refreshed.id == step_run.id
    assert refreshed.status == "running"
    assert refreshed.state == {"pid": 4242}


# ── /ws/agent: authentication (deterministic, fake socket) ───────────────────


async def test_agent_ws_missing_api_key_closes_before_accepting(app, db, sample_node, ws_manager):
    """The 4001 rejection closes with a reason and never accepts the socket."""
    sock = _ScriptedAgentSocket()

    await ws_mod.agent_websocket(sock, node_id=str(sample_node.id), api_key=None)

    assert sock.accepted is False
    assert sock.closed == [(4001, "Missing api_key query parameter")]
    assert ws_manager.agent_connections == {}


async def test_agent_ws_empty_api_key_is_treated_as_missing(app, db, sample_node, ws_manager):
    """An empty-string api_key takes the 4001 branch, not the DB lookup.

    ``if not api_key`` is falsy for ``""``, so no query runs — an empty key can
    never accidentally match a node whose key column is blank.
    """
    sock = _ScriptedAgentSocket()

    await ws_mod.agent_websocket(sock, node_id=str(sample_node.id), api_key="")

    assert sock.closed == [(4001, "Missing api_key query parameter")]
    assert sock.accepted is False


async def test_agent_ws_rejected_key_leaves_node_status_untouched(app, db, ws_manager):
    """A 4003 rejection performs no DB write and registers nothing.

    An unauthenticated socket must not be able to flip a node to "online".
    """
    node = await _make_node(db, hostname="stays-offline.test", status="offline")
    sock = _ScriptedAgentSocket()

    await ws_mod.agent_websocket(sock, node_id=str(node.id), api_key="wrong-key")

    assert sock.accepted is False
    assert sock.closed == [(4003, "Invalid api_key for this node")]
    assert ws_manager.agent_connections == {}
    db.expunge_all()
    assert (await ops.get_node_by_id(db, node.id)).status == "offline"


# ── /ws/agent: connect + teardown lifecycle (deterministic) ──────────────────


async def test_agent_ws_marks_node_online_on_connect_and_offline_on_teardown(app, db, ws_manager):
    """The session's entry stamps ``online`` + heartbeat; its exit stamps ``offline``.

    Node liveness is derived from the socket's lifetime, so both halves must
    happen even for a session that never sent a single message.
    """
    node = await _make_node(db, hostname="lifecycle.test", status="offline")
    assert node.last_heartbeat is None

    sock = await _run_agent_session(node)

    assert sock.accepted is True
    db.expunge_all()
    after = await ops.get_node_by_id(db, node.id)
    assert after.status == "offline"
    # The connect path stamped a heartbeat before the teardown flipped status.
    assert after.last_heartbeat is not None


async def test_agent_ws_teardown_evicts_the_socket_from_the_registry(app, db, ws_manager):
    """The ``finally`` block removes the node from ``agent_connections``."""
    node = await _make_node(db, hostname="evicted.test")

    await _run_agent_session(node)

    assert str(node.id) not in ws_manager.agent_connections


async def test_agent_ws_teardown_evicts_a_socket_that_already_reconnected(app, db, ws_manager):
    """A late teardown evicts the agent's NEW socket and marks the node offline.

    Documents actual (buggy) behaviour, flagged as POSSIBLE BUG: the ``finally``
    block removes whatever socket is registered under ``node_id`` without
    checking that it is still *this* one. If the agent reconnected before the
    old session finished unwinding, the fresh connection is dropped from the
    registry and the node is reported offline even though its socket is live —
    it only recovers on the next heartbeat. A correct fix makes this test's
    expectations flip, which is exactly the signal wanted here.
    """
    node = await _make_node(db, hostname="racy.test")
    replacement = _FakeAgentSocket()
    node_id = str(node.id)

    class _ReconnectingSocket(_ScriptedAgentSocket):
        """Registers a brand-new socket for the node, then drops this session."""

        async def receive_text(self):
            """Simulate a reconnect landing mid-teardown.

            Raises:
                WebSocketDisconnect: Always, after installing ``replacement``.
            """
            ws_mod.manager.agent_connections[node_id] = replacement
            raise WebSocketDisconnect(code=1006)

    await ws_mod.agent_websocket(_ReconnectingSocket(), node_id=node_id, api_key=node.api_key)

    assert node_id not in ws_manager.agent_connections
    db.expunge_all()
    assert (await ops.get_node_by_id(db, node.id)).status == "offline"


async def test_agent_ws_broadcasts_node_online_then_offline(app, db, ws_manager):
    """Dashboards see a ``node.status`` online frame on connect and offline on drop.

    The online frame carries the hostname (the dashboard's primary label) while
    the offline frame deliberately omits it.
    """
    node = await _make_node(db, hostname="broadcaster.test")
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)

    await _run_agent_session(node)

    statuses = _frames_of_type(dashboard.received, "node.status")
    assert [f["status"] for f in statuses] == ["online", "offline"]
    assert statuses[0] == {
        "type": "node.status", "node_id": str(node.id), "status": "online",
        "hostname": "broadcaster.test", "last_heartbeat": None,
    }
    assert statuses[1]["hostname"] is None


async def test_agent_ws_teardown_marks_node_offline_even_after_a_handler_crash(app, db, ws_manager):
    """A fatal handler error still runs the full offline/eviction teardown.

    The reconnect-storm failure mode depended on this path: whatever kills the
    receive loop, the node must be marked offline and the socket evicted so the
    scheduler stops selecting it.
    """
    node = await _make_node(db, hostname="crashy.test")
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)

    await _run_agent_session(node, frames=["}{ not json at all"])

    assert str(node.id) not in ws_manager.agent_connections
    db.expunge_all()
    assert (await ops.get_node_by_id(db, node.id)).status == "offline"
    assert [f["status"] for f in _frames_of_type(dashboard.received, "node.status")] == ["online", "offline"]


# ── _handle_agent_message: heartbeat / register ──────────────────────────────


async def test_heartbeat_stamps_last_heartbeat_and_acks(app, db, ws_manager):
    """A heartbeat updates ``last_heartbeat`` and replies ``heartbeat_ok``."""
    node = await _make_node(db, hostname="hb.test")

    sock = await _run_agent_session(node, frames=[_heartbeat(node.id)])

    assert _frames_of_type(sock.sent, "ack") == [{"type": "ack", "message": "heartbeat_ok"}]
    db.expunge_all()
    assert (await ops.get_node_by_id(db, node.id)).last_heartbeat is not None


async def test_heartbeat_restores_a_node_the_server_marked_offline(app, db, ws_manager):
    """A heartbeat re-asserts ``status="online"`` without needing a reconnect.

    This is how a node recovers from a transient server-side error or a
    maintenance toggle mid-session. Asserted mid-session by inspecting the row
    before the teardown flips it back to offline.
    """
    node = await _make_node(db, hostname="restored.test")
    observed: list[str] = []

    class _Peeking(_ScriptedAgentSocket):
        """Records the node's persisted status right after each ack."""

        async def send_json(self, message):
            await super().send_json(message)
            async with app_session_factory() as peek:
                row = await ops.get_node_by_id(peek, node.id)
                observed.append(row.status)

    from nexus_server.db import session as db_session
    app_session_factory = db_session.get_session_factory()

    # Force the row offline first, exactly as a transient error would.
    await ops.update_node(db, node.id, status="offline")

    sock = _Peeking([_heartbeat(node.id)], runner=None)
    await ws_mod.agent_websocket(sock, node_id=str(node.id), api_key=node.api_key)

    assert observed == ["online"]


async def test_register_overwrites_node_inventory_and_acks(app, db, ws_manager):
    """``register`` replaces the node's hardware/OS fields and acks "registered".

    The agent is authoritative for this inventory, including ``tags`` — values
    written through the HTTP API are overwritten on the next register.
    """
    node = await _make_node(db, hostname="placeholder.test", os_type="linux", tags=["api-owned"])

    sock = await _run_agent_session(node, frames=[_register(node.id)])

    assert _frames_of_type(sock.sent, "ack") == [{"type": "ack", "message": "registered"}]
    db.expunge_all()
    after = await ops.get_node_by_id(db, node.id)
    assert after.hostname == "agent-reported.test"
    assert after.os_type == "macos"
    assert after.os_version == "15.1"
    assert after.arch == "arm64"
    assert after.cpu_model == "Apple M3 Max"
    assert after.cpu_cores == 14
    assert after.ram_mb == 36864
    assert after.gpu_info == "M3 Max 30-core"
    assert after.agent_version == "9.9.9"
    assert after.ip_address == "10.7.7.7"
    assert after.tags == ["agent-owned"]


async def test_register_with_optional_gpu_info_omitted_is_accepted(app, db, ws_manager):
    """``gpu_info`` is optional; omitting it nulls the column rather than failing."""
    node = await _make_node(db, hostname="nogpu.test")
    frame = _register(node.id)
    frame.pop("gpu_info")

    sock = await _run_agent_session(node, frames=[frame])

    assert _frames_of_type(sock.sent, "ack") == [{"type": "ack", "message": "registered"}]
    db.expunge_all()
    assert (await ops.get_node_by_id(db, node.id)).gpu_info is None


# ── _handle_agent_message: step.started ──────────────────────────────────────


async def test_step_started_persists_resume_state_and_marks_step_run_running(
    app, db, regular_user, ws_manager,
):
    """``step.started`` writes ``state`` + node + ``running`` on the latest step run.

    ``state`` is the step's opaque resume handle (PIDs, container ids, temp
    paths); persisting it is what makes crash recovery able to call ``check()``
    without re-running ``startup()``. ``node_id`` is stored as a *string* — a
    ``uuid.UUID`` there is the exact bind that used to kill the socket.
    """
    node = await _make_node(db, hostname="starter.test")
    job, step_run = await _make_job_with_step_run(db, regular_user)

    await _run_agent_session(node, frames=[{
        "type": "step.started", "job_id": str(job.id), "step_index": 0,
        "state": {"pid": 999, "container": "abc123"},
    }])

    db.expunge_all()
    after = await ops.get_latest_step_run(db, str(job.id), 0)
    assert after.id == step_run.id
    assert after.status == "running"
    assert after.state == {"pid": 999, "container": "abc123"}
    assert after.node_id == str(node.id)
    assert isinstance(after.node_id, str)
    assert after.started_at is not None


async def test_step_started_broadcasts_job_status_running_with_the_step_index(
    app, db, regular_user, ws_manager,
):
    """Dashboards get ``job.status`` running with ``step_name`` left as None.

    The started path only knows the index, so the UI contract is that
    ``step_name`` may be absent and must not blank a previously shown name.
    """
    node = await _make_node(db, hostname="starter2.test")
    job, _ = await _make_job_with_step_run(db, regular_user, step_index=2)
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)

    await _run_agent_session(node, frames=[{
        "type": "step.started", "job_id": str(job.id), "step_index": 2, "state": {},
    }])

    assert _frames_of_type(dashboard.received, "job.status") == [{
        "type": "job.status", "job_id": str(job.id), "status": "running",
        "current_step": 2, "step_name": None,
    }]


async def test_step_started_without_a_matching_step_run_is_tolerated(
    app, db, regular_user, ws_manager,
):
    """A ``step.started`` for an abandoned step is a silent no-op, not a crash.

    Happens when the agent reports a step the runner already gave up on
    (cancelled job, timed-out dispatch). Raising would kill the whole socket
    over a stale message, so the frame is still broadcast and the socket stays
    usable — proved by the trailing heartbeat's ack.
    """
    node = await _make_node(db, hostname="orphan.test")
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)
    unknown_job = str(uuid.uuid4())

    sock = await _run_agent_session(node, frames=[
        {"type": "step.started", "job_id": unknown_job, "step_index": 0, "state": {"pid": 1}},
        _heartbeat(node.id),
    ])

    assert _frames_of_type(sock.sent, "ack") == [{"type": "ack", "message": "heartbeat_ok"}]
    assert _frames_of_type(dashboard.received, "job.status") == [{
        "type": "job.status", "job_id": unknown_job, "status": "running",
        "current_step": 0, "step_name": None,
    }]


async def test_step_started_does_not_write_terminal_step_fields(
    app, db, regular_user, ws_manager,
):
    """The WS layer writes only observational state, never step results.

    ``output_params`` / ``error`` / ``finished_at`` belong to ``JobRunner``; a
    second writer for those columns would produce lost updates.
    """
    node = await _make_node(db, hostname="observational.test")
    job, _ = await _make_job_with_step_run(db, regular_user)

    await _run_agent_session(node, frames=[{
        "type": "step.started", "job_id": str(job.id), "step_index": 0, "state": {"pid": 7},
    }])

    db.expunge_all()
    after = await ops.get_latest_step_run(db, str(job.id), 0)
    assert after.output_params is None
    assert after.error is None
    assert after.finished_at is None


# ── _handle_agent_message: step.completed / step.failed ──────────────────────


async def test_step_completed_forwards_every_field_to_the_runner(app, db, regular_user, ws_manager):
    """``step.completed`` calls ``runner.on_step_completed`` with the full result.

    The runner — not this module — owns the terminal DB write, context merge and
    step advancement, so the handler's whole job is this one synchronous call.
    """
    node = await _make_node(db, hostname="completer.test")
    job, _ = await _make_job_with_step_run(db, regular_user)
    runner = _RecordingRunner()

    await _run_agent_session(node, runner=runner, frames=[{
        "type": "step.completed", "job_id": str(job.id), "step_index": 0,
        "outputs": {"artifact": "s3://bucket/key"},
        "command": "echo hi", "stdout": "hi\n", "stderr": "", "exit_code": 0,
    }])

    assert runner.failed == []
    assert runner.completed == [{
        "job_id": str(job.id), "step_index": 0,
        "outputs": {"artifact": "s3://bucket/key"},
        "command": "echo hi", "stdout": "hi\n", "stderr": "", "exit_code": 0,
    }]


async def test_step_completed_leaves_the_step_run_row_untouched(app, db, regular_user, ws_manager):
    """A completed step is not persisted by the WS handler.

    Pins the single-writer rule: the step run stays ``pending`` here because
    only ``JobRunner`` writes ``success``. If this ever starts asserting
    ``success``, two writers are racing on the row.
    """
    node = await _make_node(db, hostname="nowrite.test")
    job, step_run = await _make_job_with_step_run(db, regular_user)

    await _run_agent_session(node, runner=_RecordingRunner(), frames=[{
        "type": "step.completed", "job_id": str(job.id), "step_index": 0, "outputs": {},
    }])

    db.expunge_all()
    after = await ops.get_latest_step_run(db, str(job.id), 0)
    assert after.id == step_run.id
    assert after.status == "pending"


async def test_step_completed_broadcasts_running_not_completed(app, db, regular_user, ws_manager):
    """The dashboard frame for a finished *step* still says ``running``.

    A finished step is not a finished job; the runner emits the job's terminal
    status once it advances past the last step.
    """
    node = await _make_node(db, hostname="completer2.test")
    job, _ = await _make_job_with_step_run(db, regular_user, step_index=3)
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)

    await _run_agent_session(node, runner=_RecordingRunner(), frames=[{
        "type": "step.completed", "job_id": str(job.id), "step_index": 3, "outputs": {},
    }])

    assert _frames_of_type(dashboard.received, "job.status") == [{
        "type": "job.status", "job_id": str(job.id), "status": "running",
        "current_step": 3, "step_name": None,
    }]


async def test_step_completed_with_only_required_fields_defaults_the_rest(
    app, db, regular_user, ws_manager,
):
    """``command``/``stdout``/``stderr``/``exit_code`` are optional and default to None.

    Control-plane steps with no subprocess send exactly this minimal frame.
    """
    node = await _make_node(db, hostname="minimal.test")
    job, _ = await _make_job_with_step_run(db, regular_user)
    runner = _RecordingRunner()

    await _run_agent_session(node, runner=runner, frames=[{
        "type": "step.completed", "job_id": str(job.id), "step_index": 0,
        "outputs": {"k": "v"},
    }])

    assert runner.completed[0]["command"] is None
    assert runner.completed[0]["stdout"] is None
    assert runner.completed[0]["stderr"] is None
    assert runner.completed[0]["exit_code"] is None


async def test_step_failed_forwards_error_to_the_runner(app, db, regular_user, ws_manager):
    """``step.failed`` calls ``runner.on_step_failed`` with the diagnostics."""
    node = await _make_node(db, hostname="failer.test")
    job, _ = await _make_job_with_step_run(db, regular_user)
    runner = _RecordingRunner()

    await _run_agent_session(node, runner=runner, frames=[{
        "type": "step.failed", "job_id": str(job.id), "step_index": 1,
        "error": "boom", "command": "/bin/false", "stdout": "", "stderr": "bad",
        "exit_code": 1,
    }])

    assert runner.completed == []
    assert runner.failed == [{
        "job_id": str(job.id), "step_index": 1, "error": "boom",
        "command": "/bin/false", "stdout": "", "stderr": "bad", "exit_code": 1,
    }]


async def test_step_failed_broadcasts_failed_optimistically(app, db, regular_user, ws_manager):
    """The dashboard is told ``failed`` before the runner applies ``on_fail``.

    Documents the known transient-UI behaviour: a step with
    ``on_fail="continue"`` keeps the job going, so the dashboard can briefly
    show a job as failed that then resumes. The DB is never wrong; only this
    frame is.
    """
    node = await _make_node(db, hostname="failer2.test")
    job, _ = await _make_job_with_step_run(db, regular_user)
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)

    await _run_agent_session(node, runner=_RecordingRunner(), frames=[{
        "type": "step.failed", "job_id": str(job.id), "step_index": 0, "error": "nope",
    }])

    assert _frames_of_type(dashboard.received, "job.status") == [{
        "type": "job.status", "job_id": str(job.id), "status": "failed",
        "current_step": 0, "step_name": None,
    }]


async def test_step_failed_does_not_write_the_error_to_the_step_run(
    app, db, regular_user, ws_manager,
):
    """A failure frame leaves ``error``/``status`` for the runner to write."""
    node = await _make_node(db, hostname="failer3.test")
    job, _ = await _make_job_with_step_run(db, regular_user)

    await _run_agent_session(node, runner=_RecordingRunner(), frames=[{
        "type": "step.failed", "job_id": str(job.id), "step_index": 0, "error": "nope",
    }])

    db.expunge_all()
    after = await ops.get_latest_step_run(db, str(job.id), 0)
    assert after.error is None
    assert after.status == "pending"


# ── _handle_agent_message: step.log / step.progress ──────────────────────────


async def test_step_log_is_relayed_to_dashboards_verbatim(app, db, ws_manager):
    """``step.log`` forwards the RAW inbound dict, extra fields included.

    The model is constructed for validation only; the raw dict is broadcast so
    additional agent-attached fields survive and the highest-volume message type
    skips a serialization round trip.
    """
    node = await _make_node(db, hostname="logger.test")
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)
    frame = {
        "type": "step.log", "job_id": str(uuid.uuid4()), "step_index": 0,
        "stream": "stderr", "line": "warning: something",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "extra_field_the_server_does_not_model": "kept",
    }

    await _run_agent_session(node, frames=[frame])

    assert _frames_of_type(dashboard.received, "step.log") == [frame]


async def test_step_log_does_not_ack_or_persist(app, db, ws_manager):
    """Log frames are fan-out only: no ack, no DB row."""
    node = await _make_node(db, hostname="logger2.test")

    sock = await _run_agent_session(node, frames=[{
        "type": "step.log", "job_id": str(uuid.uuid4()), "step_index": 0,
        "stream": "stdout", "line": "x", "timestamp": "2026-01-01T00:00:00+00:00",
    }])

    assert _frames_of_type(sock.sent, "ack") == []


async def test_step_progress_is_relayed_to_dashboards_verbatim(app, db, ws_manager):
    """``step.progress`` uses the same validate-then-forward-raw path as step.log."""
    node = await _make_node(db, hostname="progress.test")
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)
    frame = {
        "type": "step.progress", "job_id": str(uuid.uuid4()), "step_index": 4,
        "percent": 99.5, "message": "almost", "nonce": 17,
    }

    await _run_agent_session(node, frames=[frame])

    assert _frames_of_type(dashboard.received, "step.progress") == [frame]


async def test_step_progress_percent_is_not_clamped(app, db, ws_manager):
    """An out-of-range ``percent`` is accepted and relayed unchanged.

    Documents actual behaviour: the model does not validate or clamp 0-100, so
    the UI is responsible for sane rendering.
    """
    node = await _make_node(db, hostname="progress2.test")
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)
    frame = {"type": "step.progress", "job_id": str(uuid.uuid4()), "step_index": 0,
             "percent": 1000.0}

    await _run_agent_session(node, frames=[frame])

    assert _frames_of_type(dashboard.received, "step.progress")[0]["percent"] == 1000.0


# ── _handle_agent_message: unknown / missing type ────────────────────────────


async def test_unknown_message_type_is_logged_and_ignored(app, db, ws_manager, caplog):
    """A message type this server does not know degrades to a warning.

    Forward-compatibility: during a partial agent rollout a newer frame must not
    disconnect the node. The trailing heartbeat's ack proves the socket lived.
    """
    node = await _make_node(db, hostname="future.test")

    with caplog.at_level(logging.WARNING, logger="nexus.ws"):
        sock = await _run_agent_session(node, frames=[
            {"type": "step.teleported", "job_id": "j", "whatever": True},
            _heartbeat(node.id),
        ])

    assert _frames_of_type(sock.sent, "ack") == [{"type": "ack", "message": "heartbeat_ok"}]
    assert any("Unknown agent message type: step.teleported" in r.getMessage()
               for r in caplog.records)


async def test_message_without_a_type_field_is_ignored(app, db, ws_manager, caplog):
    """A frame with no ``type`` falls through to the unknown-type warning."""
    node = await _make_node(db, hostname="typeless.test")

    with caplog.at_level(logging.WARNING, logger="nexus.ws"):
        sock = await _run_agent_session(node, frames=[{"job_id": "j"}, _heartbeat(node.id)])

    assert _frames_of_type(sock.sent, "ack") == [{"type": "ack", "message": "heartbeat_ok"}]
    assert any("Unknown agent message type: None" in r.getMessage() for r in caplog.records)


async def test_empty_json_object_is_ignored(app, db, ws_manager):
    """``{}`` is a legal frame that dispatches to nothing and keeps the socket."""
    node = await _make_node(db, hostname="empty.test")

    sock = await _run_agent_session(node, frames=[{}, _heartbeat(node.id)])

    assert _frames_of_type(sock.sent, "ack") == [{"type": "ack", "message": "heartbeat_ok"}]


# ── malformed payloads ───────────────────────────────────────────────────────


async def test_non_json_frame_tears_down_the_socket(app, db, ws_manager, caplog):
    """Undecodable text ends the session and logs the "Agent WS error" line.

    Documents actual behaviour: ``json.loads`` raises before any dispatch, the
    blanket ``except Exception`` logs it, and the connection is dropped. That
    log line is the one to watch when nodes flap.
    """
    node = await _make_node(db, hostname="badjson.test")

    with caplog.at_level(logging.ERROR, logger="nexus.ws"):
        sock = await _run_agent_session(node, frames=["<html>not json</html>", _heartbeat(node.id)])

    # The heartbeat after the bad frame was never reached: the loop already died.
    assert _frames_of_type(sock.sent, "ack") == []
    assert any("Agent WS error" in r.getMessage() for r in caplog.records)
    db.expunge_all()
    assert (await ops.get_node_by_id(db, node.id)).status == "offline"


async def test_json_scalar_frame_tears_down_the_socket(app, db, ws_manager, caplog):
    """A bare JSON scalar decodes fine but has no ``.get``, killing the socket.

    Documents actual behaviour — ``data.get("type")`` assumes an object. The
    frame is not validated as an envelope before that call.
    """
    node = await _make_node(db, hostname="scalar.test")

    with caplog.at_level(logging.ERROR, logger="nexus.ws"):
        sock = await _run_agent_session(node, frames=["42", _heartbeat(node.id)])

    assert _frames_of_type(sock.sent, "ack") == []
    assert any("Agent WS error" in r.getMessage() for r in caplog.records)


async def test_json_array_frame_tears_down_the_socket(app, db, ws_manager, caplog):
    """A JSON array frame also fails at ``data.get`` and drops the connection."""
    node = await _make_node(db, hostname="array.test")

    with caplog.at_level(logging.ERROR, logger="nexus.ws"):
        sock = await _run_agent_session(node, frames=['["heartbeat"]', _heartbeat(node.id)])

    assert _frames_of_type(sock.sent, "ack") == []
    assert any("Agent WS error" in r.getMessage() for r in caplog.records)


async def test_heartbeat_missing_required_field_tears_down_the_socket(app, db, ws_manager, caplog):
    """A ValidationError from a malformed heartbeat is fatal to the session.

    ``_handle_agent_message`` deliberately does not catch ``ValidationError``;
    it propagates to ``agent_websocket``, which logs and drops the socket. A
    persistently malformed agent therefore reconnect-loops rather than being
    quietly ignored.
    """
    node = await _make_node(db, hostname="badhb.test")

    with caplog.at_level(logging.ERROR, logger="nexus.ws"):
        sock = await _run_agent_session(node, frames=[{"type": "heartbeat"}])

    assert _frames_of_type(sock.sent, "ack") == []
    assert any("Agent WS error" in r.getMessage() for r in caplog.records)
    assert str(node.id) not in ws_manager.agent_connections


async def test_register_missing_required_field_tears_down_the_socket(app, db, ws_manager):
    """An incomplete ``register`` frame is fatal and writes nothing.

    Partial inventory must never be persisted — the node keeps its previous
    hostname rather than acquiring half an update.
    """
    node = await _make_node(db, hostname="original-host.test")
    frame = _register(node.id)
    frame.pop("cpu_cores")

    sock = await _run_agent_session(node, frames=[frame])

    assert _frames_of_type(sock.sent, "ack") == []
    db.expunge_all()
    assert (await ops.get_node_by_id(db, node.id)).hostname == "original-host.test"


async def test_step_log_with_invalid_stream_literal_tears_down_the_socket(app, db, ws_manager):
    """``stream`` is a Literal["stdout","stderr"]; anything else is a fatal error.

    Also proves validation runs *before* the raw-dict relay — the bad frame
    never reaches dashboard clients.
    """
    node = await _make_node(db, hostname="badstream.test")
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)

    await _run_agent_session(node, frames=[{
        "type": "step.log", "job_id": str(uuid.uuid4()), "step_index": 0,
        "stream": "stdin", "line": "x", "timestamp": "2026-01-01T00:00:00+00:00",
    }])

    assert _frames_of_type(dashboard.received, "step.log") == []


async def test_step_started_with_non_dict_state_tears_down_the_socket(app, db, regular_user, ws_manager):
    """``state`` must be an object; a string there is a ValidationError.

    Guards the crash-recovery contract — ``state`` is replayed into
    ``check(state)``, so a non-mapping value must be rejected at the boundary
    rather than persisted.
    """
    node = await _make_node(db, hostname="badstate.test")
    job, step_run = await _make_job_with_step_run(db, regular_user)

    await _run_agent_session(node, frames=[{
        "type": "step.started", "job_id": str(job.id), "step_index": 0, "state": "pid=1",
    }])

    db.expunge_all()
    after = await ops.get_latest_step_run(db, str(job.id), 0)
    assert after.status == "pending"
    assert after.state is None


async def test_step_completed_without_a_runner_on_app_state_tears_down_the_socket(
    app, db, regular_user, ws_manager, caplog,
):
    """``step.completed`` requires ``ws.app.state.runner``; None kills the socket.

    Documents actual behaviour for the "message arrived before/after the runner
    existed" case: an ``AttributeError`` propagates and the session ends rather
    than the frame being dropped.
    """
    node = await _make_node(db, hostname="norunner.test")
    job, _ = await _make_job_with_step_run(db, regular_user)

    with caplog.at_level(logging.ERROR, logger="nexus.ws"):
        sock = await _run_agent_session(node, runner=None, frames=[
            {"type": "step.completed", "job_id": str(job.id), "step_index": 0, "outputs": {}},
            _heartbeat(node.id),
        ])

    assert _frames_of_type(sock.sent, "ack") == []
    assert any("Agent WS error" in r.getMessage() for r in caplog.records)


# ── UUID-typed ids flowing through the handler (regression, commit d895144) ──


async def test_handle_agent_message_heartbeat_accepts_a_uuid_typed_node_id(app, db, ws_manager):
    """A ``uuid.UUID`` node id survives the heartbeat handler's DB write.

    Direct regression pin for commit d895144, which removed
    ``ops.update_node(db, UUID(node_id), ...)`` from this branch. All ID columns
    are ``String(36)``; binding a ``uuid.UUID`` raises
    ``sqlite3.ProgrammingError: type 'UUID' is not supported``, which used to
    escape into ``agent_websocket`` and kill the socket. ``ops._sid`` now
    coerces, so a UUID must flow through harmlessly.
    """
    node = await _make_node(db, hostname="uuid-hb.test", status="offline")
    sock = _ScriptedAgentSocket()
    uuid_node_id = uuid.UUID(str(node.id))

    await ws_mod._handle_agent_message(
        db, uuid_node_id, "heartbeat", _heartbeat(node.id), sock,
    )

    assert sock.sent == [{"type": "ack", "message": "heartbeat_ok"}]
    db.expunge_all()
    after = await ops.get_node_by_id(db, node.id)
    assert after.status == "online"
    assert after.last_heartbeat is not None


async def test_handle_agent_message_register_accepts_a_uuid_typed_node_id(app, db, ws_manager):
    """A ``uuid.UUID`` node id survives the register handler's DB write.

    Same regression as the heartbeat case — this branch also used to wrap
    ``node_id`` in ``UUID(...)`` before handing it to ``ops.update_node``.
    """
    node = await _make_node(db, hostname="uuid-reg.test")
    sock = _ScriptedAgentSocket()

    await ws_mod._handle_agent_message(
        db, uuid.UUID(str(node.id)), "register", _register(node.id), sock,
    )

    assert sock.sent == [{"type": "ack", "message": "registered"}]
    db.expunge_all()
    assert (await ops.get_node_by_id(db, node.id)).hostname == "agent-reported.test"


async def test_handle_agent_message_leaves_the_session_usable_after_uuid_ids(
    app, db, ws_manager,
):
    """A UUID-typed id does not poison the session with PendingRollbackError.

    A failed bind used to leave the session in a "needs rollback" state, so
    every *subsequent* message on the same connection failed too. Two UUID-keyed
    messages in a row on one session prove nothing was poisoned.
    """
    node = await _make_node(db, hostname="uuid-session.test")
    uuid_node_id = uuid.UUID(str(node.id))
    sock = _ScriptedAgentSocket()

    await ws_mod._handle_agent_message(db, uuid_node_id, "heartbeat", _heartbeat(node.id), sock)
    await ws_mod._handle_agent_message(db, uuid_node_id, "register", _register(node.id), sock)
    await ws_mod._handle_agent_message(db, uuid_node_id, "heartbeat", _heartbeat(node.id), sock)

    assert [f["message"] for f in sock.sent] == ["heartbeat_ok", "registered", "heartbeat_ok"]
    db.expunge_all()
    assert (await ops.get_node_by_id(db, node.id)).status == "online"


async def test_get_latest_step_run_accepts_a_uuid_typed_job_id(app, db, regular_user):
    """The step-run lookup the handler makes tolerates a ``uuid.UUID`` job id.

    ``step.started`` used to call ``ops.get_latest_step_run(db, UUID(info.job_id),
    ...)``. The comparison is against a ``String(36)`` column, so the UUID had to
    be coerced; ``ops._sid`` now does it. Pinning the ``ops`` contract keeps a
    future reintroduction of ``UUID(...)`` in ws.py from crashing.
    """
    job, step_run = await _make_job_with_step_run(db, regular_user, step_index=1)

    found = await ops.get_latest_step_run(db, uuid.UUID(str(job.id)), 1)

    assert found is not None
    assert found.id == step_run.id


async def test_update_step_run_accepts_a_uuid_typed_primary_key(app, db, regular_user):
    """``ops.update_step_run`` coerces a ``uuid.UUID`` primary key.

    The handler passes ``latest.id`` (already a string), but the coercion is the
    safety net that stops the WS layer from re-acquiring the bind crash if a
    caller ever hands it a UUID.
    """
    job, step_run = await _make_job_with_step_run(db, regular_user)

    updated = await ops.update_step_run(db, uuid.UUID(str(step_run.id)), status="running")

    assert updated is not None
    assert updated.status == "running"


async def test_step_run_node_id_accepts_a_uuid_and_stores_it_as_a_string(app, db, regular_user):
    """A ``uuid.UUID`` in ``update_step_run``'s ``**kwargs`` is coerced, not crashed.

    The last un-coerced hazard on this path, now closed. ``update_step_run``
    applies its ``**kwargs`` with a blind ``setattr`` and ``StepRun.node_id`` is
    ``String(36)``, so ``node_id=UUID(...)`` used to raise
    ``sqlite3.ProgrammingError: type 'UUID' is not supported`` — the exact bind
    that killed the socket on every ``step.started`` and produced the reconnect
    storm. ``_sid_kwargs`` now stringifies every ``*_id``/``*_by`` kwarg before
    the ``setattr``, so ws.py can no longer re-acquire the crash by reintroducing
    a ``UUID(...)`` wrapper.

    Storing the *string* form is the load-bearing half: ``node_id`` is compared
    against ``Node.id`` (also a string) and echoed into dashboard payloads, so a
    coercion that kept the UUID object would merely move the failure downstream.
    The re-SELECT after ``expunge_all`` is what proves the coerced value reached
    the column rather than only the in-memory instance, and the follow-up write
    proves the session was never poisoned.
    """
    node = await _make_node(db, hostname="uuid-kwarg.test")
    job, step_run = await _make_job_with_step_run(db, regular_user)

    updated = await ops.update_step_run(db, step_run.id, node_id=uuid.UUID(str(node.id)))

    assert updated is not None
    assert updated.node_id == str(node.id)
    db.expunge_all()
    stored = await ops.get_latest_step_run(db, job.id, step_run.step_index)
    assert isinstance(stored.node_id, str)
    assert stored.node_id == str(node.id)
    # No rollback needed — the session is still usable for the next write.
    assert (await ops.update_step_run(db, step_run.id, status="running")).status == "running"


async def test_agent_session_survives_repeated_step_messages(app, db, regular_user, ws_manager):
    """Twenty step messages on one session all land; the socket is never dropped.

    The reconnect storm looked exactly like this workload: every step frame
    raised, so a job that logged steadily reconnected the node continuously and
    never advanced. Volume plus a trailing heartbeat ack is the assertion that
    the bind crash is gone.
    """
    node = await _make_node(db, hostname="storm.test")
    job, step_run = await _make_job_with_step_run(db, regular_user)
    job_id = str(job.id)
    runner = _RecordingRunner()
    dashboard = _FakeDashboardSocket()
    await ws_manager.connect_dashboard(dashboard)

    frames: list[dict] = [
        {"type": "step.started", "job_id": job_id, "step_index": 0, "state": {"pid": 1}},
    ]
    for i in range(15):
        frames.append({
            "type": "step.log", "job_id": job_id, "step_index": 0, "stream": "stdout",
            "line": f"line {i}", "timestamp": "2026-01-01T00:00:00+00:00",
        })
    frames.append({"type": "step.progress", "job_id": job_id, "step_index": 0, "percent": 100.0})
    frames.append({"type": "step.completed", "job_id": job_id, "step_index": 0, "outputs": {}})
    frames.append(_heartbeat(node.id))
    frames.append(_heartbeat(node.id))

    sock = await _run_agent_session(node, runner=runner, frames=frames)

    assert len(_frames_of_type(sock.sent, "ack")) == 2
    assert len(_frames_of_type(dashboard.received, "step.log")) == 15
    assert len(runner.completed) == 1
    db.expunge_all()
    assert (await ops.get_latest_step_run(db, job_id, 0)).status == "running"


# ── /ws/dashboard ────────────────────────────────────────────────────────────


async def test_dashboard_ws_connects_without_authentication(client, ws_manager):
    """The dashboard feed accepts any client — no token is checked.

    Documents the knowingly-present security gap: the frontend appends
    ``?token=<jwt>`` but the server never reads it, so anyone who can reach the
    port receives every node's hostname/status and every job's live stdout.
    """
    with client.websocket_connect("/ws/dashboard") as socket:
        assert len(ws_manager.dashboard_connections) == 1
        # A token is accepted and ignored rather than validated.
        socket.send_text("ping")

    assert ws_manager.dashboard_connections == []


async def test_dashboard_ws_with_a_bogus_token_is_still_accepted(client, ws_manager):
    """An invalid ``token`` query parameter does not prevent the upgrade."""
    with client.websocket_connect("/ws/dashboard", params={"token": "definitely-not-a-jwt"}):
        assert len(ws_manager.dashboard_connections) == 1

    assert ws_manager.dashboard_connections == []


async def test_dashboard_ws_unregisters_the_client_on_disconnect(client, ws_manager):
    """The endpoint's ``finally`` removes the socket from the fan-out list.

    Without cleanup the registry would grow until a later broadcast happened to
    fail on the dead socket.
    """
    with client.websocket_connect("/ws/dashboard"):
        pass

    assert ws_manager.dashboard_connections == []


async def test_dashboard_ws_receives_broadcasts_triggered_by_an_agent(
    client, sample_node, ws_manager, sentinel_dashboard,
):
    """A dashboard client receives the real ``node.status`` fan-out frames.

    End-to-end across two live sockets: the agent connect/teardown broadcasts
    reach the browser feed. Both sessions share the ``TestClient`` portal, hence
    one event loop, which is what makes this deliverable.
    """
    node_id = str(sample_node.id)

    with client.websocket_connect("/ws/dashboard") as dashboard:
        with client.websocket_connect(
            f"/ws/agent/{node_id}", params={"api_key": sample_node.api_key},
        ) as agent:
            online = dashboard.receive_json()
            agent.send_json(_heartbeat(node_id))
            agent.receive_json()  # drain the ack so the frame is fully handled
            await _close_agent_socket(agent, sentinel_dashboard)
        offline = dashboard.receive_json()

    assert online == {
        "type": "node.status", "node_id": node_id, "status": "online",
        "hostname": "node-1.test", "last_heartbeat": None,
    }
    assert offline == {
        "type": "node.status", "node_id": node_id, "status": "offline",
        "hostname": None, "last_heartbeat": None,
    }


async def test_dashboard_ws_relays_agent_step_logs(
    client, db, regular_user, sample_node, ws_manager, sentinel_dashboard,
):
    """Live log tailing works: an agent ``step.log`` frame lands on the dashboard.

    The relay is verbatim, which is why the dashboard and agent ``step.log``
    discriminators must stay identical.
    """
    job, _ = await _make_job_with_step_run(db, regular_user)
    log_frame = {
        "type": "step.log", "job_id": str(job.id), "step_index": 0,
        "stream": "stdout", "line": "tailing works",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }

    with client.websocket_connect("/ws/dashboard") as dashboard:
        with client.websocket_connect(
            f"/ws/agent/{sample_node.id}", params={"api_key": sample_node.api_key},
        ) as agent:
            dashboard.receive_json()  # node.status online
            agent.send_json(log_frame)
            relayed = dashboard.receive_json()
            await _close_agent_socket(agent, sentinel_dashboard)

    assert relayed == log_frame


async def test_dashboard_ws_fans_out_to_multiple_clients(
    client, sample_node, ws_manager, sentinel_dashboard,
):
    """Two dashboard clients both receive the same broadcast frame."""
    node_id = str(sample_node.id)

    with client.websocket_connect("/ws/dashboard") as first:
        with client.websocket_connect("/ws/dashboard") as second:
            # The sentinel fake is registered too, hence 3.
            assert len(ws_manager.dashboard_connections) == 3
            with client.websocket_connect(
                f"/ws/agent/{node_id}", params={"api_key": sample_node.api_key},
            ) as agent:
                a = first.receive_json()
                b = second.receive_json()
                await _close_agent_socket(agent, sentinel_dashboard)

    assert a == b
    assert a["type"] == "node.status"
    assert a["status"] == "online"


async def test_dashboard_ws_discards_inbound_text_and_stays_connected(
    client, sample_node, ws_manager, sentinel_dashboard,
):
    """Text sent by a dashboard client is dropped; the socket keeps receiving.

    The dashboard protocol is one-way — the endpoint's ``receive_text`` exists
    only so the coroutine parks until the peer disconnects.
    """
    node_id = str(sample_node.id)

    with client.websocket_connect("/ws/dashboard") as dashboard:
        dashboard.send_text("subscribe: everything")
        dashboard.send_json({"type": "client.command", "do": "something"})
        # Still subscribed (this dashboard plus the sentinel fake).
        assert len(ws_manager.dashboard_connections) == 2

        with client.websocket_connect(
            f"/ws/agent/{node_id}", params={"api_key": sample_node.api_key},
        ) as agent:
            frame = dashboard.receive_json()
            await _close_agent_socket(agent, sentinel_dashboard)

    assert frame["type"] == "node.status"
