"""WebSocket routes — agent connections and dashboard broadcast."""

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
    """Tracks active agent and dashboard WebSocket connections."""

    def __init__(self):
        self.agent_connections: dict[str, WebSocket] = {}  # node_id -> ws
        self.dashboard_connections: list[WebSocket] = []

    async def connect_agent(self, node_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self.agent_connections[node_id] = ws
        logger.info("Agent connected: %s", node_id)

    def disconnect_agent(self, node_id: str) -> None:
        self.agent_connections.pop(node_id, None)
        logger.info("Agent disconnected: %s", node_id)

    async def connect_dashboard(self, ws: WebSocket) -> None:
        await ws.accept()
        self.dashboard_connections.append(ws)
        logger.info("Dashboard client connected")

    def disconnect_dashboard(self, ws: WebSocket) -> None:
        if ws in self.dashboard_connections:
            self.dashboard_connections.remove(ws)
        logger.info("Dashboard client disconnected")

    async def broadcast_to_dashboards(self, message: dict) -> None:
        """Send a message to all connected dashboard clients."""
        stale = []
        for ws in self.dashboard_connections:
            try:
                await ws.send_json(message)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect_dashboard(ws)

    async def send_to_agent(self, node_id: str, message: dict) -> bool:
        """Send a message to a specific agent. Returns False if not connected."""
        ws = self.agent_connections.get(node_id)
        if not ws:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception:
            self.disconnect_agent(node_id)
            return False


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
    """
    # Authenticate via api_key
    if not api_key:
        await ws.close(code=4001, reason="Missing api_key query parameter")
        return

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
    await manager.broadcast_to_dashboards(
        DashboardNodeStatus(node_id=node_id, status="online", hostname=node.hostname).model_dump(mode="json")
    )

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            async for db in get_session():
                await _handle_agent_message(db, node_id, msg_type, data, ws)
                break

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("Agent WS error for %s: %s", node_id, exc)
    finally:
        manager.disconnect_agent(node_id)
        # Mark node offline
        async for db in get_session():
            await ops.update_node(db, UUID(node_id), status="offline")
            break
        await manager.broadcast_to_dashboards(
            DashboardNodeStatus(node_id=node_id, status="offline").model_dump(mode="json")
        )


async def _handle_agent_message(
    db: AsyncSession, node_id: str, msg_type: str, data: dict, ws: WebSocket,
) -> None:
    """Route an inbound agent message to the appropriate handler."""
    if msg_type == "heartbeat":
        hb = AgentHeartbeat(**data)
        await ops.update_node(db, UUID(node_id), last_heartbeat=datetime.now(timezone.utc), status="online")
        await ws.send_json(ServerAck(message="heartbeat_ok").model_dump(mode="json"))

    elif msg_type == "register":
        reg = AgentRegister(**data)
        await ops.update_node(
            db, UUID(node_id),
            hostname=reg.hostname, os_type=reg.os_type, os_version=reg.os_version,
            arch=reg.arch, cpu_model=reg.cpu_model, cpu_cores=reg.cpu_cores,
            ram_mb=reg.ram_mb, gpu_info=reg.gpu_info, agent_version=reg.agent_version,
            ip_address=reg.ip_address, capabilities=reg.capabilities, tags=reg.tags,
        )
        await ws.send_json(ServerAck(message="registered").model_dump(mode="json"))

    elif msg_type == "step.started":
        info = StepStarted(**data)
        # Record the agent's startup() state on the latest step_run for this
        # (job_id, step_index) so crash recovery can resume polling without
        # re-running startup(). The runner is the single writer for the
        # final status / outputs / error fields.
        latest = await ops.get_latest_step_run(db, UUID(info.job_id), info.step_index)
        if latest is not None:
            await ops.update_step_run(
                db, latest.id, status="running",
                node_id=UUID(node_id), state=info.state,
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
        runner = ws.app.state.runner
        runner.on_step_completed(info.job_id, info.step_index, info.outputs)
        await manager.broadcast_to_dashboards(
            DashboardJobStatus(
                job_id=info.job_id, status="running", current_step=info.step_index,
            ).model_dump(mode="json")
        )

    elif msg_type == "step.failed":
        info = StepFailed(**data)
        runner = ws.app.state.runner
        runner.on_step_failed(info.job_id, info.step_index, info.error)
        await manager.broadcast_to_dashboards(
            DashboardJobStatus(
                job_id=info.job_id, status="failed", current_step=info.step_index,
            ).model_dump(mode="json")
        )

    elif msg_type == "step.log":
        info = StepLog(**data)
        await manager.broadcast_to_dashboards(data)

    elif msg_type == "step.progress":
        info = StepProgress(**data)
        await manager.broadcast_to_dashboards(data)

    else:
        logger.warning("Unknown agent message type: %s", msg_type)


# ── Dashboard WebSocket ──────────────────────────────────────────────────


@router.websocket("/ws/dashboard")
async def dashboard_websocket(ws: WebSocket):
    """Dashboard WebSocket — receives real-time broadcasts of node/job status.

    No authentication required (read-only feed). Production deployments
    should add a token check here.
    """
    await manager.connect_dashboard(ws)
    try:
        while True:
            # Keep connection alive; dashboard is receive-only but we must
            # read to detect disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("Dashboard WS error: %s", exc)
    finally:
        manager.disconnect_dashboard(ws)
