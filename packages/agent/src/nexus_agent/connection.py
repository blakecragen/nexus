"""WebSocket connection to the Nexus server.

Handles:
- Connection with API key authentication
- Agent registration on connect
- Periodic heartbeat (every 10 seconds)
- Dispatching incoming ExecuteStepCommand and CancelStepCommand
- Auto-reconnect with exponential backoff on disconnect

Role in the system:
    This is the agent's only link to the control plane. `nexus_agent.main`
    constructs `AgentConnection` and awaits `run()`, which never returns under
    normal operation. The peer on the other end is
    `nexus_server.api.routes.ws.agent_websocket` (`/ws/agent/{node_id}`),
    which authenticates the key, marks the node online, and forwards step
    lifecycle messages to the server's runner.

    Inbound  (server → agent): execute_step, cancel_step, ack.
    Outbound (agent → server): register, heartbeat, step.started, step.log,
    step.completed, step.failed — all Pydantic models from
    `nexus_common.agent_protocol`, dumped with `mode="json"`.

Concurrency model:
    One connection at a time. Inside a connection, a heartbeat task and a
    listener task run concurrently; `execute_step` is spawned as a detached
    task so a long-running step never blocks the listener from receiving a
    cancel. `send_message` is therefore called from several tasks against the
    same socket — safe because `websockets` serializes sends internally.
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
"""Seconds between heartbeats.

AI Note: The server's offline-detection threshold is derived from this
cadence — raising it without matching the server side makes healthy nodes get
marked offline. Heartbeats are also the agent's only liveness signal, since the
listener task can sit idle indefinitely between step dispatches.
"""

MAX_BACKOFF = 60  # maximum reconnect delay in seconds
"""Ceiling on reconnect delay, so a long server outage retries once a minute."""

INITIAL_BACKOFF = 1  # initial reconnect delay in seconds
"""First reconnect delay; doubles per consecutive failure up to MAX_BACKOFF."""


class AgentConnection:
    """Manages the WebSocket lifecycle between the agent and the Nexus server.

    Owns the single `ClientConnection` and the `StepExecutor` that runs work
    dispatched over it. The executor holds a back-reference to this object and
    calls `send_message()` to report step progress, so the two are
    deliberately circular.
    """

    def __init__(self, config: AgentConfig) -> None:
        """Wire up the connection. Does not open a socket.

        Args:
            config: Resolved agent configuration supplying the server URL,
                API key, node id, and tags.

        Note:
            `StepExecutor(self)` is constructed here with a partially
            initialized `AgentConnection`; that is fine because the executor
            only stores the reference and uses it later, after `run()` starts.
        """
        self.config = config
        self.executor = StepExecutor(self)
        # None whenever no socket is established — send_message() keys its
        # retry behavior off this being repopulated by the reconnect loop.
        self._ws: ClientConnection | None = None
        self._running = True

    # ── Public API ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop: connect, register, heartbeat, handle messages. Reconnects on failure.

        Runs until `stop()` is called or the task is cancelled. Network
        failures are absorbed and retried with exponential backoff, so this
        coroutine does not raise for ordinary connectivity problems.

        Side effects:
            Opens a WebSocket, registers this node with the server (which
            flips it to "online"), and indirectly spawns step subprocesses.

        AI Note: Backoff resets to INITIAL_BACKOFF after a *clean* return from
        `_connect_and_run()` (server-initiated close), but grows on each
        exception. A server that accepts and immediately closes the socket
        therefore reconnects in a tight 1-second loop rather than backing off.
        """
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
                # AI Note: This tuple is the "expected transient failure" set:
                # dropped socket, HTTP-level rejection during the handshake,
                # and DNS/refused-connection errors. Anything outside it
                # (e.g. a Pydantic ValidationError) escapes the loop and kills
                # the agent process — intentionally loud, since those indicate
                # a protocol mismatch rather than a flaky network.
                logger.warning("Connection lost: %s. Reconnecting in %ds...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except asyncio.CancelledError:
                logger.info("Agent connection cancelled")
                self._running = False
                break

    async def send_message(self, message: dict[str, Any], *, critical: bool = False) -> None:
        """Send a JSON message to the server.

        Heartbeats/logs are best-effort (dropped if disconnected). Step lifecycle
        messages (started/completed/failed) are `critical`: if the socket is down
        — e.g. a brief reconnect or a server `--reload` restart — we wait for the
        connection to come back and retry, so a step result is never lost.

        Args:
            message: An already-`model_dump(mode="json")`ed protocol message.
                Passed to `json.dumps`, so every value must be JSON-native
                (no datetimes or UUIDs).
            critical: When True, retry across reconnects instead of dropping.
                Reserve this for step.started/completed/failed — the server's
                runner blocks on those, and a lost one strands the job until
                its 7200s timeout.

        Side effects:
            Writes to the WebSocket. Never raises on send failure; a
            best-effort message is dropped with a warning and a critical one
            gives up after ~90s with an error log.

        AI Note: Ordering hazard. A critical send that has to wait can be
        overtaken by a later best-effort send once the socket returns, so
        step.log lines may arrive after the step.completed for the same step.
        The server tolerates this because logs are broadcast-only and the
        runner keys terminal state off (job_id, step_index).
        """
        payload = json.dumps(message)
        if not critical:
            # Snapshot _ws into a local: the reconnect loop can null out the
            # attribute between the check and the await.
            ws = self._ws
            if ws is None:
                logger.warning("Cannot send message — not connected")
                return
            try:
                await ws.send(payload)
            except websockets.exceptions.ConnectionClosed:
                logger.warning("Send failed — connection closed")
            return

        # Critical: retry across reconnects (the run() loop re-establishes _ws).
        # AI Note: 90 x 1s is sized to outlast the worst reconnect backoff
        # (MAX_BACKOFF = 60s) plus a uvicorn --reload restart. Shrinking it
        # below ~65s means a single max-backoff gap can drop a step result.
        attempts = 90  # ~90s of 1s retries — covers reconnect backoff + reload
        for i in range(attempts):
            ws = self._ws
            if ws is not None:
                try:
                    await ws.send(payload)
                    return
                except websockets.exceptions.ConnectionClosed:
                    # Socket died mid-send; fall through to the sleep and pick
                    # up whatever the reconnect loop installs next.
                    pass
            if i == 0:
                logger.warning(
                    "Not connected — will retry delivering %s until reconnected",
                    message.get("type", "message"),
                )
            await asyncio.sleep(1.0)
        logger.error(
            "Gave up delivering %s after %ds — server unreachable",
            message.get("type", "message"), attempts,
        )

    def stop(self) -> None:
        """Signal the agent to stop after the current iteration.

        Cooperative and non-blocking: it only clears the `_running` flag. The
        `run()` loop exits on its next pass and `_heartbeat_loop()` on its next
        wake-up, so an in-flight `_listen_loop` keeps waiting on the socket
        until the server closes it or the task is cancelled. Currently unused
        by the CLI, which relies on KeyboardInterrupt instead.
        """
        self._running = False

    # ── Internal ───────────────────────────────────────────────────────

    async def _connect_and_run(self) -> None:
        """Single connection lifecycle: connect -> register -> heartbeat + listen.

        Returns when either the heartbeat or the listener task finishes —
        normally because the server closed the socket.

        Raises:
            websockets.exceptions.ConnectionClosed / InvalidStatusCode /
            OSError: Propagated to `run()`, which handles backoff. Any
            exception raised inside the heartbeat or listener task is
            re-raised here via `task.result()`.
        """
        url = self._build_url()
        logger.info("Connecting to %s", url)

        async with websockets.connect(url) as ws:
            self._ws = ws
            logger.info("Connected to server")

            # AI Note: Registration is sent before the listener starts. The
            # server tolerates this because it only *reads* after accepting,
            # but it also means registration goes out non-critically — if it
            # is lost, the node stays online with stale hardware info until
            # the next reconnect.
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
                # AI Note: Must be cleared before the `async with` closes the
                # socket, so critical sends see None and enter their retry
                # wait instead of writing to a dead connection. Note that
                # step tasks spawned by the listener are *not* cancelled here
                # — a running step survives a reconnect and delivers its
                # result over the next socket via critical send.
                self._ws = None

    def _build_url(self) -> str:
        """Construct the WebSocket URL with API key in query params.

        Returns:
            `<server_url>?api_key=...&node_id=...`, using "&" when the
            configured URL already carries a query string.

        AI Note: The API key travels as a query parameter, so it can land in
        server access logs and any intermediary proxy logs. It is also
        unencrypted unless `server_url` uses `wss://`. Values are interpolated
        without URL-encoding, so a key or node id containing "&", "=", or
        whitespace would corrupt the query string.
        """
        base = self.config.server_url.rstrip("/")
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}api_key={self.config.api_key}&node_id={self.config.node_id}"

    async def _send_registration(self) -> None:
        """Build and send the AgentRegister message.

        Re-runs full host detection on every connect, so hardware/OS changes
        (and agent upgrades) are picked up on reconnect. The server copies
        these fields onto the node row.

        Side effects:
            Runs the `capability` probes (subprocesses, ~seconds) and sends
            one message.

        Raises:
            KeyError: If `detect_capabilities()` stops returning one of the
                keys indexed below.
            pydantic.ValidationError: If a detected value violates the
                `AgentRegister` schema. Neither is caught by `run()`'s handler,
                so either kills the agent process.
        """
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
            # .get() because gpu_info is legitimately absent/None on most hosts.
            gpu_info=caps.get("gpu_info"),
            agent_version=__version__,
            ip_address=caps["ip_address"],
            tags=self.config.tags,
        )
        await self.send_message(msg.model_dump(mode="json"))
        logger.info("Registered as %s (%s %s)", self.config.node_id, caps["os_type"], caps["arch"])

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat messages every HEARTBEAT_INTERVAL seconds.

        Each heartbeat carries current CPU load, memory pressure, and the
        number of in-flight steps, which the server stores as node telemetry
        and uses to keep the node marked online.

        Exits when `stop()` is called or `_ws` is cleared; that return is what
        `_connect_and_run()`'s `asyncio.wait` may observe as "first completed".

        AI Note: `psutil.cpu_percent(interval=None)` is non-blocking and
        returns utilization *since the previous call* — so the very first
        heartbeat after every reconnect reports a meaningless value (0.0 or
        the average since process start). Dividing by 100 means `load_avg` is
        a 0.0–1.0 fraction, not a Unix load average, despite the field name.
        """
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
                # Best-effort: a dropped heartbeat is harmless, the next one
                # (or the reconnect's register) re-establishes liveness.
                await self.send_message(msg.model_dump(mode="json"))
            except Exception as exc:
                # Swallow everything so a transient psutil error never tears
                # down the connection via _connect_and_run's task.result().
                logger.debug("Heartbeat send error: %s", exc)

            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _listen_loop(self, ws: ClientConnection) -> None:
        """Listen for incoming server commands and dispatch them.

        Args:
            ws: The live connection. Passed explicitly rather than read from
                `self._ws` so this loop keeps iterating the socket it was
                started for, even if the attribute is cleared.

        Returns:
            When the async iteration ends, i.e. the server closed the socket.

        Side effects:
            Spawns a detached task per `execute_step`, which runs subprocesses
            and sends step lifecycle messages.

        AI Note: `execute_step` is fire-and-forget (`create_task` with no
        stored reference) while `cancel_step` is awaited. That asymmetry is
        required: awaiting a step would block this loop and make the matching
        cancel undeliverable. The task reference is dropped, so nothing here
        prevents garbage collection mid-run — `StepExecutor._running_steps`
        holds the state that keeps the work discoverable for cancellation.
        Note there is no cap on concurrent steps; the server-side scheduler is
        the only thing limiting how many land on one node.
        """
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
                # AI Note: Catch-all keeps one malformed/unschema'd command
                # from killing the socket. Historically a raise here caused a
                # reconnect storm plus stuck jobs (see the UUID/SQLite fix in
                # commit d895144) — keep this broad.
                logger.error("Error handling message: %s", exc, exc_info=True)
