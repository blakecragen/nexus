"""WebSocket routes — agent connections and dashboard broadcast.

Role in the system
------------------
This module is the server's real-time nervous system. It owns two endpoints
and the process-wide connection registry that ties them together:

- ``/ws/agent/{node_id}`` — the *only* channel between the server and a remote
  ``nexus-agent``. Steps are pushed down it; step lifecycle events come back up
  it; node liveness is derived from it.
- ``/ws/dashboard`` — a fan-out, receive-only feed the React UI subscribes to
  for live node and job status.

Message flow
------------
::

    agent ──step.started/completed/failed/log/progress──▶ _handle_agent_message
                                                            │
                                       ops.update_* (DB) ◀──┤
                                       JobRunner callbacks ◀┤
                                       broadcast_to_dashboards
                                                            ▼
                                                     dashboard clients

    JobRunner ──ExecuteStepCommand──▶ manager.send_to_agent ──▶ agent

Neighbouring modules
--------------------
- ``nexus_common.agent_protocol`` defines every message model used here; it is
  shared verbatim with the agent, so any change is a wire-protocol change that
  must be rolled out to both sides.
- ``nexus_server.runner.runner.JobRunner`` is constructed at startup with
  ``ws_manager=ws.manager`` (see ``main.lifespan``) and reached back from here
  via ``ws.app.state.runner``. That mutual reference is why the manager is a
  module-level singleton rather than app state.
- ``nexus_server.db.ops`` for node/step-run persistence.
- ``frontend/src/hooks/useWebSocket.ts`` is the dashboard client.

Division of responsibility (important)
--------------------------------------
This module writes only *observational* state: node status/heartbeat, and the
agent-reported ``running`` snapshot of a step run. The ``JobRunner`` is the
single writer for terminal step status, outputs, context merging and job
advancement. Adding DB writes for step results here would create two writers
for the same rows and produce lost updates.

AI Note: ``manager`` is module-level mutable state holding live socket objects.
It is correct only because the server runs as a single process with one event
loop. Under multiple uvicorn workers, an agent connected to worker A is
invisible to a job dispatched on worker B, and dashboard broadcasts only reach
the clients on the worker that emitted them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_common.agent_protocol import (
    AgentHeartbeat,
    AgentRegister,
    DashboardJobStatus,
    DashboardNodeStatus,
    ServerAck,
    StepCompleted,
    StepFailed,
    StepLog,
    StepProgress,
    StepStarted,
)
from nexus_server.db import ops
from nexus_server.db.session import get_session

logger = logging.getLogger("nexus.ws")

router = APIRouter()


# ── Connection Manager ────────────────────────────────────────────────────


class ConnectionManager:
    """Tracks active agent and dashboard WebSocket connections.

    A single instance (:data:`manager`) is shared by this module and by
    :class:`~nexus_server.runner.runner.JobRunner`, which uses
    :meth:`send_to_agent` to dispatch steps. It is the in-memory source of
    truth for "is this node reachable right now", complementing the ``status``
    column in the DB (which can go stale if the process dies).

    Attributes:
        agent_connections: Node ID (string form of the node UUID) to its live
            socket. At most one connection per node.
        dashboard_connections: Every subscribed UI client; unauthenticated and
            anonymous, so a list rather than a keyed dict.

    AI Note: not thread-safe, and intentionally so — every method is only ever
    awaited from the single asyncio event loop. The mutations are also
    non-atomic across awaits, which is why :meth:`broadcast_to_dashboards`
    collects stale sockets and removes them *after* iterating instead of
    mutating the list mid-loop.
    """

    def __init__(self):
        """Initialize an empty registry. Called once at import time."""
        self.agent_connections: dict[str, WebSocket] = {}  # node_id -> ws
        self.dashboard_connections: list[WebSocket] = []

    async def connect_agent(self, node_id: str, ws: WebSocket) -> None:
        """Accept an agent's WebSocket handshake and register it.

        Args:
            node_id: String form of the node's UUID; the dict key used by
                :meth:`send_to_agent`, so it must match exactly what the runner
                passes (``str(node.id)``).
            ws: The socket to accept.

        AI Note: accepting happens *after* authentication in
        :func:`agent_websocket` — the caller validates the api_key first and
        closes with a 4001/4003 code on failure, so an unauthenticated socket
        never reaches this registry.

        AI Note: a reconnect silently replaces any existing entry for the same
        node without closing the old socket. That is the desired behaviour for
        an agent that reconnected after a network blip (the stale socket is
        already dead), but it means a duplicate agent process would hijack step
        delivery for that node.
        """
        await ws.accept()
        self.agent_connections[node_id] = ws
        logger.info("Agent connected: %s", node_id)

    def disconnect_agent(self, node_id: str) -> None:
        """Remove an agent from the registry.

        Args:
            node_id: String form of the node's UUID.

        AI Note: uses ``pop(..., None)`` so calling it twice — which happens,
        because both :meth:`send_to_agent`'s failure path and
        :func:`agent_websocket`'s ``finally`` block call it — is harmless.
        """
        self.agent_connections.pop(node_id, None)
        logger.info("Agent disconnected: %s", node_id)

    async def connect_dashboard(self, ws: WebSocket) -> None:
        """Accept a dashboard client's handshake and add it to the fan-out list.

        Args:
            ws: The socket to accept.
        """
        await ws.accept()
        self.dashboard_connections.append(ws)
        logger.info("Dashboard client connected")

    def disconnect_dashboard(self, ws: WebSocket) -> None:
        """Remove a dashboard client from the fan-out list.

        Args:
            ws: The socket to drop. Ignored if already absent, so this is safe
                to call from both the reaping loop in
                :meth:`broadcast_to_dashboards` and the endpoint's ``finally``.
        """
        if ws in self.dashboard_connections:
            self.dashboard_connections.remove(ws)
        logger.info("Dashboard client disconnected")

    async def broadcast_to_dashboards(self, message: dict) -> None:
        """Send a message to all connected dashboard clients.

        Failures are treated as disconnects: a socket that raises on send is
        collected and dropped from the registry rather than propagating the
        error to the caller.

        Args:
            message: Already JSON-serializable payload. Callers pass
                ``model_dump(mode="json")`` output (or, for log/progress
                frames, the raw inbound dict).

        AI Note: this must never raise. It is invoked from the agent message
        loop, so an exception escaping here would tear down a healthy agent
        connection because one browser tab went away.

        AI Note: broadcasts are sequential and awaited. One slow/blocked
        dashboard client adds latency to the agent's message loop. It has not
        mattered at current scale, but it is the reason a chatty ``step.log``
        stream can feel laggy when many tabs are open.
        """
        stale = []
        for ws in self.dashboard_connections:
            try:
                await ws.send_json(message)
            except Exception:
                stale.append(ws)
        # AI Note: deferred removal — mutating self.dashboard_connections while
        # iterating it above would skip entries.
        for ws in stale:
            self.disconnect_dashboard(ws)

    async def send_to_agent(self, node_id: str, message: dict) -> bool:
        """Send a message to a specific agent. Returns False if not connected.

        This is the runner's outbound path for ``ExecuteStepCommand`` and
        cancellations.

        Args:
            node_id: String form of the node's UUID.
            message: JSON-serializable payload.

        Returns:
            bool: True if the frame was handed to the socket. False if the node
            has no live connection, or if the send failed — in which case the
            connection is also evicted from the registry.

        AI Note: a True return only means the frame was written to the
        transport, not that the agent processed it. Step delivery is confirmed
        later by the ``step.started`` message coming back; the runner's own
        timeout is what covers a silently dropped dispatch.
        """
        ws = self.agent_connections.get(node_id)
        if not ws:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception:
            # AI Note: evict on send failure so the scheduler stops picking
            # this node. The DB `status` column is NOT updated here — that
            # happens in agent_websocket()'s finally block when the socket's
            # own receive loop unwinds.
            self.disconnect_agent(node_id)
            return False


# AI Note: module-level singleton. `main.lifespan` passes this exact object to
# JobRunner(ws_manager=...), so importing `ws.manager` elsewhere yields the same
# live registry. Replacing it at runtime would leave the runner holding the old
# one and silently break step dispatch.
manager = ConnectionManager()


# ── Agent WebSocket ───────────────────────────────────────────────────────


@router.websocket("/ws/agent/{node_id}")
async def agent_websocket(ws: WebSocket, node_id: str, api_key: str | None = None):
    """Agent WebSocket connection — authenticates via api_key query param.

    Protocol:
    1. Agent connects with ?api_key=<key>
    2. Server validates api_key matches node_id
    3. Agent sends register, heartbeat, step.* messages
    4. Server sends execute_step, cancel_step, ack messages

    Lifetime of one call == lifetime of one agent session. On entry the node is
    marked online and dashboards are notified; the body then loops reading
    frames until the socket drops; on exit the node is marked offline and
    dashboards are notified again.

    Args:
        ws: The upgraded connection.
        node_id: Node UUID as a string, taken from the path. Used verbatim as
            the registry key and as the DB lookup key.
        api_key: The node's agent credential, supplied as a query parameter.

    Side effects:
        Writes ``status``/``last_heartbeat`` on the node row, registers the
        socket in :data:`manager`, and broadcasts node status transitions.

    Close codes:
        - ``4001``: no ``api_key`` query parameter was supplied.
        - ``4003``: the key is unknown, or belongs to a different node than the
          one named in the path.

    AI Note: security-sensitive. Authentication is a query parameter, not a
    header, because browser and CLI WebSocket clients cannot set
    ``Authorization`` during the handshake. Consequences: the key can appear in
    proxy/access logs, and it is compared by an exact DB lookup rather than a
    constant-time comparison. The ``str(node.id) != node_id`` check is what
    stops a valid key for node A being used to impersonate node B.
    """
    # Authenticate via api_key
    if not api_key:
        # AI Note: close() before accept() — Starlette responds with an HTTP
        # 403 to the handshake rather than completing the upgrade, so the
        # client sees a rejected connection, not a closed one.
        await ws.close(code=4001, reason="Missing api_key query parameter")
        return

    # AI Note: `async for ... break` is the idiom used throughout this file to
    # borrow one session from the async-generator dependency outside of
    # FastAPI's DI (WebSocket routes here do not use deps.get_db). The break
    # runs the generator's finally, which closes the session.
    async for db in get_session():
        node = await ops.get_node_by_api_key(db, api_key)
        if not node or str(node.id) != node_id:
            await ws.close(code=4003, reason="Invalid api_key for this node")
            return

        # Mark node as online
        await ops.update_node(db, node.id, status="online", last_heartbeat=datetime.now(timezone.utc))
        break

    await manager.connect_agent(node_id, ws)

    # Notify dashboards
    # AI Note: `node` leaks out of the `async for` scope above. That is
    # deliberate (it holds the hostname for the broadcast) but it is only bound
    # because the generator always yields at least once — a session factory
    # that yielded nothing would raise NameError here rather than a clean
    # error.
    await manager.broadcast_to_dashboards(
        DashboardNodeStatus(node_id=node_id, status="online", hostname=node.hostname).model_dump(mode="json")
    )

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            # AI Note: a FRESH session per inbound message, by design. A single
            # long-lived session across the whole agent session would cache
            # stale rows (the runner commits step/job updates on its own
            # session) and would hold a SQLite connection open for hours.
            async for db in get_session():
                await _handle_agent_message(db, node_id, msg_type, data, ws)
                break

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # AI Note: any unhandled error kills the socket for this agent. The
        # agent then reconnects, so a bug that throws on every message becomes
        # a reconnect storm rather than an obvious crash — this exact failure
        # mode was previously caused by passing raw UUIDs to SQLite. Watch this
        # log line when nodes flap.
        logger.error("Agent WS error for %s: %s", node_id, exc)
    finally:
        # AI Note: ordering/race hazard. If the agent already reconnected on a
        # new socket before this teardown runs, this unconditionally evicts the
        # NEW connection and marks the node offline — the node then appears
        # down despite having a live socket, until its next heartbeat restores
        # "online". Any fix must check that the registered socket is still
        # THIS one before removing it.
        manager.disconnect_agent(node_id)
        # Mark node offline
        async for db in get_session():
            await ops.update_node(db, node_id, status="offline")
            break
        await manager.broadcast_to_dashboards(
            DashboardNodeStatus(node_id=node_id, status="offline").model_dump(mode="json")
        )


async def _handle_agent_message(
    db: AsyncSession, node_id: str, msg_type: str, data: dict, ws: WebSocket,
) -> None:
    """Route an inbound agent message to the appropriate handler.

    Dispatches on the ``type`` field of the agent protocol envelope. Each
    branch parses the raw dict into its ``nexus_common.agent_protocol`` model
    (which validates the payload), performs its side effects, and where
    relevant mirrors the event to dashboard clients.

    Args:
        db: A fresh session, valid only for this one message.
        node_id: String form of the reporting node's UUID. Trusted, because the
            connection was authenticated in :func:`agent_websocket`.
        msg_type: The ``type`` discriminator. ``None``/unknown values are
            logged and ignored rather than raising.
        data: The decoded JSON frame.
        ws: The agent's socket — used both to send acks and to reach
            ``ws.app.state.runner``.

    Side effects:
        DB writes (node heartbeat/identity, step-run ``running`` snapshot),
        runner callbacks that unblock the job's step-wait, and dashboard
        broadcasts.

    Raises:
        pydantic.ValidationError: If a frame does not match its declared model.
            Not caught here — it propagates to :func:`agent_websocket`, which
            logs it and drops the connection.

    AI Note: message types are matched as string literals against the ``type``
    defaults in ``nexus_common.agent_protocol``. Renaming a message type there
    without updating these branches makes the message fall through to the
    "Unknown agent message type" warning — a silent no-op, not an error.
    """
    if msg_type == "heartbeat":
        hb = AgentHeartbeat(**data)
        # AI Note: heartbeats also re-assert status="online". That is what
        # restores a node the server marked offline during a transient error or
        # a maintenance toggle, without needing a reconnect.
        await ops.update_node(db, node_id, last_heartbeat=datetime.now(timezone.utc), status="online")
        await ws.send_json(ServerAck(message="heartbeat_ok").model_dump(mode="json"))

    elif msg_type == "register":
        reg = AgentRegister(**data)
        # AI Note: this is where the placeholder hardware fields written by
        # POST /api/nodes/provision ("pending", "unknown", "0.0.0.0") get
        # replaced with the machine's real specs. The agent is authoritative
        # for all of these, including `tags` — tags set through the API are
        # overwritten on the agent's next register.
        await ops.update_node(
            db, node_id,
            hostname=reg.hostname, os_type=reg.os_type, os_version=reg.os_version,
            arch=reg.arch, cpu_model=reg.cpu_model, cpu_cores=reg.cpu_cores,
            ram_mb=reg.ram_mb, gpu_info=reg.gpu_info, agent_version=reg.agent_version,
            ip_address=reg.ip_address, tags=reg.tags,
        )
        await ws.send_json(ServerAck(message="registered").model_dump(mode="json"))

    elif msg_type == "step.started":
        info = StepStarted(**data)
        # Record the agent's startup() state on the latest step_run for this
        # (job_id, step_index) so crash recovery can resume polling without
        # re-running startup(). The runner is the single writer for the
        # final status / outputs / error fields.
        # AI Note: `state` is the step's opaque resume handle (PIDs, container
        # IDs, temp paths). Persisting it here is what makes crash recovery
        # possible; dropping this write would strand long-running steps after a
        # server restart.
        latest = await ops.get_latest_step_run(db, info.job_id, info.step_index)
        # AI Note: a missing step_run is tolerated silently. It happens when the
        # agent reports a step the runner has already abandoned (cancelled job,
        # timed-out dispatch) — raising here would kill the whole socket over a
        # stale message.
        if latest is not None:
            await ops.update_step_run(
                db, latest.id, status="running",
                node_id=node_id, state=info.state,
                started_at=datetime.now(timezone.utc),
            )
        await manager.broadcast_to_dashboards(
            DashboardJobStatus(
                job_id=info.job_id, status="running",
                current_step=info.step_index, step_name=None,
            ).model_dump(mode="json")
        )

    elif msg_type == "step.completed":
        info = StepCompleted(**data)
        # Notify the runner; it owns the DB writes for terminal state +
        # context merging + step advancement.
        # AI Note: `on_step_completed` is a plain sync method — it stores the
        # result and sets an asyncio.Event that the runner's job task is
        # awaiting. It must not be awaited, and it must not be replaced with a
        # DB write here: two writers for the same step_run row would race.
        runner = ws.app.state.runner
        runner.on_step_completed(
            info.job_id, info.step_index, info.outputs,
            command=info.command, stdout=info.stdout, stderr=info.stderr,
            exit_code=info.exit_code,
        )
        # AI Note: broadcasts status="running", not "completed" — a finished
        # *step* does not mean a finished *job*. The runner emits the job's
        # terminal status once it advances past the last step.
        await manager.broadcast_to_dashboards(
            DashboardJobStatus(
                job_id=info.job_id, status="running", current_step=info.step_index,
            ).model_dump(mode="json")
        )

    elif msg_type == "step.failed":
        info = StepFailed(**data)
        runner = ws.app.state.runner
        runner.on_step_failed(
            info.job_id, info.step_index, info.error,
            command=info.command, stdout=info.stdout, stderr=info.stderr,
            exit_code=info.exit_code,
        )
        # AI Note: broadcasts "failed" optimistically, before the runner has
        # decided anything. A step with on_fail="continue" will keep the job
        # going, so the dashboard can briefly show a job as failed that then
        # resumes. The DB is never wrong; only this transient UI frame is.
        await manager.broadcast_to_dashboards(
            DashboardJobStatus(
                job_id=info.job_id, status="failed", current_step=info.step_index,
            ).model_dump(mode="json")
        )

    elif msg_type == "step.log":
        # AI Note: parsed for validation only, then the RAW `data` dict is
        # forwarded rather than the model dump. That preserves any extra fields
        # the agent attached and avoids a serialization round-trip on the
        # highest-volume message type. `info` being unused is intentional.
        info = StepLog(**data)
        await manager.broadcast_to_dashboards(data)

    elif msg_type == "step.progress":
        # AI Note: same validate-then-forward-raw pattern as step.log.
        info = StepProgress(**data)
        await manager.broadcast_to_dashboards(data)

    else:
        # AI Note: forward-compatibility. A newer agent sending a message type
        # this server does not know is logged and ignored, so a partial rollout
        # degrades rather than disconnecting every upgraded node.
        logger.warning("Unknown agent message type: %s", msg_type)


# ── Dashboard WebSocket ──────────────────────────────────────────────────


@router.websocket("/ws/dashboard")
async def dashboard_websocket(ws: WebSocket):
    """Dashboard WebSocket — receives real-time broadcasts of node/job status.

    No authentication required (read-only feed). Production deployments
    should add a token check here.

    Frames pushed to this socket originate from
    :meth:`ConnectionManager.broadcast_to_dashboards`: ``node.status`` and
    ``job.status`` envelopes plus verbatim ``step.log`` / ``step.progress``
    frames relayed from agents.

    Args:
        ws: The upgraded connection.

    AI Note: security gap, knowingly present. The frontend appends
    ``?token=<jwt>`` (see ``frontend/src/hooks/useWebSocket.ts``) but the
    server never reads it — anyone who can reach this port receives every
    node's hostname/status and every job's live stdout. Adding a token check
    here is safe from the client's side because the query parameter is already
    being sent.
    """
    await manager.connect_dashboard(ws)
    try:
        while True:
            # Keep connection alive; dashboard is receive-only but we must
            # read to detect disconnects.
            # AI Note: the received text is intentionally discarded — the
            # dashboard protocol is one-way. This await exists purely so the
            # coroutine parks until the peer disconnects, which raises
            # WebSocketDisconnect and triggers cleanup. Removing it would leak
            # the socket in `dashboard_connections` until a broadcast happened
            # to fail on it.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("Dashboard WS error: %s", exc)
    finally:
        manager.disconnect_dashboard(ws)
