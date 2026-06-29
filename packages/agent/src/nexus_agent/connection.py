"""WebSocket connection to the Nexus server.

Handles:
- Connection with API key authentication
- Agent registration on connect
- Periodic heartbeat (every 10 seconds)
- Dispatching incoming ExecuteStepCommand and CancelStepCommand
- Auto-reconnect with exponential backoff on disconnect
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import psutil
import websockets
from websockets.asyncio.client import ClientConnection

from nexus_agent import __version__
from nexus_agent.capability import detect_capabilities
from nexus_agent.config import AgentConfig
from nexus_agent.executor import StepExecutor

from nexus_common.agent_protocol import (
    AgentHeartbeat,
    AgentRegister,
    CancelStepCommand,
    ExecuteStepCommand,
)

logger = logging.getLogger("nexus.agent.connection")

HEARTBEAT_INTERVAL = 10  # seconds
MAX_BACKOFF = 60  # maximum reconnect delay in seconds
INITIAL_BACKOFF = 1  # initial reconnect delay in seconds


class AgentConnection:
    """Manages the WebSocket lifecycle between the agent and the Nexus server."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.executor = StepExecutor(self)
        self._ws: ClientConnection | None = None
        self._running = True

    # ── Public API ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop: connect, register, heartbeat, handle messages. Reconnects on failure."""
        backoff = INITIAL_BACKOFF

        while self._running:
            try:
                await self._connect_and_run()
                # If we exit cleanly (server sent close), reset backoff
                backoff = INITIAL_BACKOFF
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.InvalidStatusCode,
                OSError,
            ) as exc:
                logger.warning("Connection lost: %s. Reconnecting in %ds...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except asyncio.CancelledError:
                logger.info("Agent connection cancelled")
                self._running = False
                break

    async def send_message(self, message: dict[str, Any]) -> None:
        """Send a JSON message to the server. Silently drops if not connected."""
        if self._ws is None:
            logger.warning("Cannot send message — not connected")
            return
        try:
            await self._ws.send(json.dumps(message))
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Send failed — connection closed")

    def stop(self) -> None:
        """Signal the agent to stop after the current iteration."""
        self._running = False

    # ── Internal ───────────────────────────────────────────────────────

    async def _connect_and_run(self) -> None:
        """Single connection lifecycle: connect -> register -> heartbeat + listen."""
        url = self._build_url()
        logger.info("Connecting to %s", url)

        async with websockets.connect(url) as ws:
            self._ws = ws
            logger.info("Connected to server")

            # Send registration
            await self._send_registration()

            # Run heartbeat and message handler concurrently
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            listener_task = asyncio.create_task(self._listen_loop(ws))

            try:
                # Wait for either task to finish (i.e., on disconnect or error)
                done, pending = await asyncio.wait(
                    [heartbeat_task, listener_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Cancel the other task
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                # Re-raise any exception from completed tasks
                for task in done:
                    task.result()
            finally:
                self._ws = None

    def _build_url(self) -> str:
        """Construct the WebSocket URL with API key in query params."""
        base = self.config.server_url.rstrip("/")
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}api_key={self.config.api_key}&node_id={self.config.node_id}"

    async def _send_registration(self) -> None:
        """Build and send the AgentRegister message."""
        caps = detect_capabilities()
        msg = AgentRegister(
            node_id=self.config.node_id,
            hostname=caps["hostname"],
            os_type=caps["os_type"],
            os_version=caps["os_version"],
            arch=caps["arch"],
            cpu_model=caps["cpu_model"],
            cpu_cores=caps["cpu_cores"],
            ram_mb=caps["ram_mb"],
            gpu_info=caps.get("gpu_info"),
            agent_version=__version__,
            ip_address=caps["ip_address"],
            tags=self.config.tags,
        )
        await self.send_message(msg.model_dump(mode="json"))
        logger.info("Registered as %s (%s %s)", self.config.node_id, caps["os_type"], caps["arch"])

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat messages every HEARTBEAT_INTERVAL seconds."""
        while self._running and self._ws is not None:
            try:
                mem = psutil.virtual_memory()
                load = psutil.cpu_percent(interval=None) / 100.0

                msg = AgentHeartbeat(
                    node_id=self.config.node_id,
                    timestamp=datetime.now(timezone.utc),
                    load_avg=load,
                    memory_used_pct=mem.percent,
                    active_steps=self.executor.active_count,
                )
                await self.send_message(msg.model_dump(mode="json"))
            except Exception as exc:
                logger.debug("Heartbeat send error: %s", exc)

            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _listen_loop(self, ws: ClientConnection) -> None:
        """Listen for incoming server commands and dispatch them."""
        async for raw in ws:
            try:
                data = json.loads(raw)
                msg_type = data.get("type")
                logger.debug("Received: %s", msg_type)

                if msg_type == "execute_step":
                    cmd = ExecuteStepCommand(**data)
                    asyncio.create_task(self.executor.execute(cmd))

                elif msg_type == "cancel_step":
                    cmd = CancelStepCommand(**data)
                    await self.executor.cancel(cmd)

                elif msg_type == "ack":
                    logger.debug("Server ack: %s", data.get("message", "ok"))

                else:
                    logger.warning("Unknown server message type: %s", msg_type)

            except json.JSONDecodeError:
                logger.warning("Received non-JSON message, ignoring")
            except Exception as exc:
                logger.error("Error handling message: %s", exc, exc_info=True)
