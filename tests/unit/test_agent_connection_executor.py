"""Unit tests for the agent's WebSocket connection layer (connection.py).

Scope — this file deliberately covers ``nexus_agent.connection``, which had **no
test coverage at all**. The neighbouring ``tests/unit/test_agent.py`` already
exercises ``config.py``, ``capability.py``, ``os_adapters/*`` and ``executor.py``
(including the ``OUTPUT_KEYS``-extraction regression at
``test_execute_extracts_outputs_from_output_keys``), so none of that is repeated
here.

What is exercised for real: URL construction, the best-effort vs. ``critical``
send paths, the reconnect/backoff state machine in ``run()``, registration
assembly, the heartbeat loop's exit conditions and error swallowing, and the
listener's message dispatch. The only stubbed boundaries are the WebSocket
itself, ``psutil``, ``detect_capabilities`` and ``asyncio.sleep`` — the
connection logic under test always runs genuinely.

AI Note: every test that touches a retry/backoff path patches
``asyncio.sleep`` via the ``no_sleep`` fixture. Without it, ``send_message``'s
critical path alone would block a test for 90 real seconds and ``run()``'s
backoff would add 60 more. The fake still yields control to the event loop, so
task interleaving (which several tests depend on) behaves normally.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

# ── psutil shim ──────────────────────────────────────────────────────────
# Mirrors the guard in test_agent.py. psutil is a real dependency here (7.x),
# but test_agent.py installs a *partial* stand-in into sys.modules at import
# time, and pytest imports every test module in the session — so by the time
# this file runs, `psutil` may be that shim, which lacks the
# `virtual_memory().percent` and `cpu_percent` attributes connection.py needs.
# Rather than depend on module import order, every heartbeat test below
# monkeypatches `connection.psutil` directly. This block only guarantees the
# import itself resolves.
if "psutil" not in sys.modules:  # pragma: no cover - real psutil is installed
    _psutil = types.ModuleType("psutil")
    _psutil.cpu_count = lambda logical=True: 8
    _psutil.cpu_percent = lambda interval=None: 12.5

    class _VMem:
        """Minimal psutil.virtual_memory() result."""

        total = 16 * 1024 * 1024 * 1024
        percent = 42.0

    _psutil.virtual_memory = lambda: _VMem()
    sys.modules["psutil"] = _psutil

import websockets

from nexus_agent import connection as conn_mod
from nexus_agent.config import AgentConfig
from nexus_agent.connection import (
    HEARTBEAT_INTERVAL,
    INITIAL_BACKOFF,
    MAX_BACKOFF,
    AgentConnection,
)


# ════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════


_REAL_SLEEP = asyncio.sleep
"""The genuine asyncio.sleep, captured before any monkeypatching.

AI Note: load-bearing. Several tests replace ``conn_mod.asyncio.sleep`` to make
the heartbeat loop terminate, and ``conn_mod.asyncio`` *is* the global asyncio
module — so a replacement that itself calls ``asyncio.sleep(0)`` to yield
control re-enters its own patch and dies with RecursionError. Always yield via
``_REAL_SLEEP``.
"""


def _config(**overrides) -> AgentConfig:
    """Build an AgentConfig with predictable, URL-safe values."""
    params = dict(
        server_url="ws://server:8000/ws/agent/node-1",
        api_key="key-abc",
        node_id="node-1",
        tags=["gpu", "linux"],
    )
    params.update(overrides)
    return AgentConfig(**params)


def _closed_exc() -> websockets.exceptions.ConnectionClosed:
    """Construct a ConnectionClosed instance across websockets versions.

    AI Note: the constructor signature has changed between websockets releases,
    so this probes rather than hardcoding it — a version bump must not silently
    turn these tests into no-ops.
    """
    try:
        return websockets.exceptions.ConnectionClosed(None, None)
    except TypeError:  # pragma: no cover - older/newer signature
        return websockets.exceptions.ConnectionClosed(1006, "abnormal")


class _FakeWS:
    """Records every payload sent, and can be told to fail.

    Args:
        fail_times: Raise ConnectionClosed on the first N ``send`` calls, then
            succeed. Used to drive the critical-send retry loop.
        always_fail: Raise ConnectionClosed on every ``send``.
    """

    def __init__(self, *, fail_times: int = 0, always_fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail_times = fail_times
        self._always_fail = always_fail
        self.send_calls = 0

    async def send(self, payload: str) -> None:
        self.send_calls += 1
        if self._always_fail or self._fail_times > 0:
            self._fail_times -= 1
            raise _closed_exc()
        self.sent.append(payload)

    def messages(self) -> list[dict]:
        """Decode every successfully-sent payload."""
        return [json.loads(p) for p in self.sent]


class _IterWS(_FakeWS):
    """A fake socket that is also async-iterable, for _listen_loop tests."""

    def __init__(self, frames: list[str]) -> None:
        super().__init__()
        self._frames = list(frames)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(0)
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


class _FakePsutil:
    """Deterministic psutil stand-in for heartbeat assertions."""

    def __init__(self, *, percent: float = 55.0, cpu: float = 25.0, raises: bool = False) -> None:
        self._percent = percent
        self._cpu = cpu
        self._raises = raises

    def virtual_memory(self):
        if self._raises:
            raise RuntimeError("psutil exploded")
        return types.SimpleNamespace(percent=self._percent)

    def cpu_percent(self, interval=None):
        return self._cpu


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace asyncio.sleep with an instant version that records its delays.

    Returns the list of requested delays, so backoff progressions can be
    asserted exactly. Still yields to the event loop so concurrent tasks
    interleave as they would in production.
    """
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *a, **kw):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(conn_mod.asyncio, "sleep", fake_sleep)
    return delays


@pytest.fixture
def stub_caps(monkeypatch):
    """Stub detect_capabilities so registration never runs real host probes."""
    caps = {
        "hostname": "host-1",
        "os_type": "linux",
        "os_version": "6.1",
        "arch": "x86_64",
        "cpu_model": "Xeon",
        "cpu_cores": 8,
        "ram_mb": 16384,
        "ip_address": "10.0.0.5",
    }
    monkeypatch.setattr(conn_mod, "detect_capabilities", lambda: dict(caps))
    return caps


# ════════════════════════════════════════════════════════════════════════
# _build_url
# ════════════════════════════════════════════════════════════════════════


def test_build_url_appends_query_with_question_mark():
    """A URL with no existing query gets "?api_key=...&node_id=...".

    Pins the happy path an operator's config produces; a wrong separator makes
    the server read no api_key and reject the handshake.
    """
    c = AgentConnection(_config())
    assert c._build_url() == "ws://server:8000/ws/agent/node-1?api_key=key-abc&node_id=node-1"


def test_build_url_uses_ampersand_when_query_already_present():
    """An existing query string switches the separator to "&"."""
    c = AgentConnection(_config(server_url="ws://server:8000/ws/agent/n?x=1"))
    assert c._build_url() == "ws://server:8000/ws/agent/n?x=1&api_key=key-abc&node_id=node-1"


def test_build_url_strips_trailing_slashes():
    """Trailing slashes are stripped so the query is not appended after "/"."""
    c = AgentConnection(_config(server_url="ws://server:8000/ws/agent/n///"))
    assert c._build_url() == "ws://server:8000/ws/agent/n?api_key=key-abc&node_id=node-1"


def test_build_url_does_not_url_encode_the_api_key():
    """Values are interpolated raw — a key containing "&" corrupts the query.

    Documents the hazard called out in the source's AI Note rather than
    endorsing it: this is why keys must stay URL-safe. If encoding is ever
    added, this test fails deliberately.
    """
    c = AgentConnection(_config(api_key="a&node_id=evil"))
    assert c._build_url().endswith("?api_key=a&node_id=evil&node_id=node-1")


def test_build_url_does_not_encode_whitespace_in_node_id():
    """A node id with a space is emitted literally, producing an invalid URL."""
    c = AgentConnection(_config(node_id="bad id"))
    assert c._build_url().endswith("&node_id=bad id")


# ════════════════════════════════════════════════════════════════════════
# send_message — best effort (default)
# ════════════════════════════════════════════════════════════════════════


async def test_send_message_best_effort_writes_json_to_socket():
    """A best-effort send serializes the dict and writes it once."""
    c = AgentConnection(_config())
    ws = _FakeWS()
    c._ws = ws

    await c.send_message({"type": "heartbeat", "n": 1})

    assert ws.messages() == [{"type": "heartbeat", "n": 1}]


async def test_send_message_best_effort_drops_when_disconnected(no_sleep):
    """With no socket, a best-effort message is dropped without sleeping.

    The regression this guards: routing a heartbeat through the critical retry
    path would block the heartbeat loop for 90s per beat while offline.
    """
    c = AgentConnection(_config())
    c._ws = None

    await c.send_message({"type": "heartbeat"})

    assert no_sleep == []


async def test_send_message_best_effort_swallows_connection_closed():
    """A socket that dies mid-send is logged, not raised.

    A raise here would propagate into _heartbeat_loop and tear down the
    connection through _connect_and_run's task.result().
    """
    c = AgentConnection(_config())
    c._ws = _FakeWS(always_fail=True)

    await c.send_message({"type": "heartbeat"})  # must not raise


async def test_send_message_best_effort_does_not_retry_after_failure(no_sleep):
    """A failed best-effort send is attempted exactly once."""
    c = AgentConnection(_config())
    ws = _FakeWS(always_fail=True)
    c._ws = ws

    await c.send_message({"type": "step.log"})

    assert ws.send_calls == 1
    assert no_sleep == []


# ════════════════════════════════════════════════════════════════════════
# send_message — critical
# ════════════════════════════════════════════════════════════════════════


async def test_send_message_critical_sends_immediately_when_connected(no_sleep):
    """A critical send on a live socket writes once and does not sleep."""
    c = AgentConnection(_config())
    ws = _FakeWS()
    c._ws = ws

    await c.send_message({"type": "step.completed"}, critical=True)

    assert ws.messages() == [{"type": "step.completed"}]
    assert no_sleep == []


async def test_send_message_critical_waits_for_reconnect_then_delivers(no_sleep):
    """A critical send parked while offline is delivered once _ws reappears.

    This is the guarantee that a finished step's result survives a server
    restart — losing it strands the job until the runner's 7200s timeout.
    """
    c = AgentConnection(_config())
    c._ws = None
    ws = _FakeWS()

    async def reconnect_after_a_few_ticks():
        for _ in range(3):
            await asyncio.sleep(0)
        c._ws = ws

    send = asyncio.create_task(
        c.send_message({"type": "step.completed"}, critical=True)
    )
    await reconnect_after_a_few_ticks()
    await send

    assert ws.messages() == [{"type": "step.completed"}]


async def test_send_message_critical_retries_across_a_mid_send_death(no_sleep):
    """A ConnectionClosed raised mid-send is retried rather than lost."""
    c = AgentConnection(_config())
    ws = _FakeWS(fail_times=2)
    c._ws = ws

    await c.send_message({"type": "step.failed"}, critical=True)

    assert ws.send_calls == 3
    assert ws.messages() == [{"type": "step.failed"}]


async def test_send_message_critical_gives_up_after_90_attempts(no_sleep):
    """Critical delivery is bounded: 90 one-second retries, then it gives up.

    Pins the documented ceiling. Shrinking it below ~65s would let a single
    max-backoff reconnect gap drop a step result.
    """
    c = AgentConnection(_config())
    c._ws = None

    await c.send_message({"type": "step.completed"}, critical=True)

    assert len(no_sleep) == 90
    assert set(no_sleep) == {1.0}


async def test_send_message_critical_that_always_fails_exhausts_retries(no_sleep):
    """A permanently broken socket burns all 90 attempts and delivers nothing."""
    c = AgentConnection(_config())
    ws = _FakeWS(always_fail=True)
    c._ws = ws

    await c.send_message({"type": "step.completed"}, critical=True)

    assert ws.send_calls == 90
    assert ws.sent == []


# ════════════════════════════════════════════════════════════════════════
# stop()
# ════════════════════════════════════════════════════════════════════════


def test_stop_clears_the_running_flag():
    """stop() is cooperative — it only flips the flag, closing nothing."""
    c = AgentConnection(_config())
    assert c._running is True

    c.stop()

    assert c._running is False
    assert c._ws is None


async def test_stop_makes_run_exit_without_connecting(monkeypatch):
    """A stopped connection's run() returns without opening a socket."""
    c = AgentConnection(_config())
    c.stop()
    calls = []
    monkeypatch.setattr(
        c, "_connect_and_run", lambda: calls.append(1)  # never awaited
    )

    await c.run()

    assert calls == []


# ════════════════════════════════════════════════════════════════════════
# run() — reconnect / backoff state machine
# ════════════════════════════════════════════════════════════════════════


def _stub_connect(c, behaviours):
    """Install a _connect_and_run that follows a script of behaviours.

    Each entry is either an exception instance to raise or the string "clean"
    for a normal return. The connection stops once the script is exhausted.
    """
    seq = list(behaviours)

    async def fake():
        if not seq:
            c._running = False
            return
        item = seq.pop(0)
        if item == "clean":
            return
        raise item

    c._connect_and_run = fake


async def test_run_backoff_doubles_on_consecutive_failures(no_sleep):
    """Each consecutive transient failure doubles the reconnect delay."""
    c = AgentConnection(_config())
    _stub_connect(c, [OSError("refused")] * 4)

    await c.run()

    assert no_sleep == [1, 2, 4, 8]


async def test_run_backoff_is_capped_at_max_backoff(no_sleep):
    """Backoff saturates at MAX_BACKOFF instead of growing unbounded."""
    c = AgentConnection(_config())
    _stub_connect(c, [OSError("refused")] * 10)

    await c.run()

    assert no_sleep[-1] == MAX_BACKOFF
    assert max(no_sleep) == MAX_BACKOFF


async def test_run_resets_backoff_after_a_clean_return(no_sleep):
    """A clean disconnect resets the delay to INITIAL_BACKOFF.

    So a node that reconnects successfully does not inherit a 60s penalty from
    an earlier outage.
    """
    c = AgentConnection(_config())
    _stub_connect(c, [OSError("a"), OSError("b"), "clean", OSError("c")])

    await c.run()

    assert no_sleep == [1, 2, INITIAL_BACKOFF]


async def test_run_treats_connection_closed_as_transient(no_sleep):
    """A dropped socket is retried rather than killing the agent."""
    c = AgentConnection(_config())
    _stub_connect(c, [_closed_exc()])

    await c.run()

    assert no_sleep == [1]


async def test_run_clean_return_does_not_sleep_and_tight_loops(no_sleep):
    """A server that accepts then immediately closes reconnects with no delay.

    Documents the hazard in run()'s AI Note: only the exception path backs off,
    so an accept-then-close server produces a hot reconnect loop.
    """
    c = AgentConnection(_config())
    _stub_connect(c, ["clean", "clean", "clean"])

    await c.run()

    assert no_sleep == []


async def test_run_lets_unexpected_exceptions_escape(no_sleep):
    """A non-transient error (e.g. a protocol mismatch) kills the agent loudly.

    Deliberate: a ValidationError means agent and server disagree on the
    protocol, which retrying forever would only hide.
    """
    c = AgentConnection(_config())
    _stub_connect(c, [ValueError("protocol mismatch")])

    with pytest.raises(ValueError, match="protocol mismatch"):
        await c.run()


async def test_run_stops_cleanly_on_cancelled_error(no_sleep):
    """CancelledError exits the loop and clears _running rather than retrying."""
    c = AgentConnection(_config())
    _stub_connect(c, [asyncio.CancelledError()])

    await c.run()

    assert c._running is False
    assert no_sleep == []


# ════════════════════════════════════════════════════════════════════════
# _send_registration
# ════════════════════════════════════════════════════════════════════════


async def test_send_registration_reports_detected_host_info(stub_caps):
    """Registration carries the probe results and the configured identity."""
    c = AgentConnection(_config())
    ws = _FakeWS()
    c._ws = ws

    await c._send_registration()

    msg = ws.messages()[0]
    assert msg["type"] == "register"
    assert msg["node_id"] == "node-1"
    assert msg["hostname"] == "host-1"
    assert msg["os_type"] == "linux"
    assert msg["arch"] == "x86_64"
    assert msg["cpu_cores"] == 8
    assert msg["ram_mb"] == 16384
    assert msg["ip_address"] == "10.0.0.5"


async def test_send_registration_forwards_configured_tags(stub_caps):
    """Tags come from config, not detection — they drive scheduler placement."""
    c = AgentConnection(_config(tags=["a", "b", "c"]))
    ws = _FakeWS()
    c._ws = ws

    await c._send_registration()

    assert ws.messages()[0]["tags"] == ["a", "b", "c"]


async def test_send_registration_tolerates_absent_gpu_info(stub_caps):
    """gpu_info is read with .get() because most hosts legitimately lack it."""
    c = AgentConnection(_config())
    ws = _FakeWS()
    c._ws = ws

    await c._send_registration()

    assert ws.messages()[0]["gpu_info"] is None


async def test_send_registration_includes_gpu_info_when_detected(monkeypatch, stub_caps):
    """A detected GPU is forwarded so the server can record it."""
    caps = dict(stub_caps, gpu_info="NVIDIA A100")
    monkeypatch.setattr(conn_mod, "detect_capabilities", lambda: caps)
    c = AgentConnection(_config())
    ws = _FakeWS()
    c._ws = ws

    await c._send_registration()

    assert ws.messages()[0]["gpu_info"] == "NVIDIA A100"


async def test_send_registration_raises_key_error_on_missing_capability(monkeypatch):
    """A capability dict missing a required key fails loudly, not silently.

    Documents that this escapes run()'s transient-error handler and kills the
    agent — the intended behaviour for a detection contract break.
    """
    monkeypatch.setattr(conn_mod, "detect_capabilities", lambda: {"hostname": "h"})
    c = AgentConnection(_config())
    c._ws = _FakeWS()

    with pytest.raises(KeyError):
        await c._send_registration()


async def test_send_registration_is_sent_best_effort(monkeypatch, stub_caps, no_sleep):
    """Registration is NOT critical — it is dropped if the socket is down.

    Pins the documented consequence: a lost register leaves the node online
    with stale hardware info until the next reconnect, rather than blocking.
    """
    c = AgentConnection(_config())
    c._ws = None

    await c._send_registration()

    assert no_sleep == []


# ════════════════════════════════════════════════════════════════════════
# _heartbeat_loop
# ════════════════════════════════════════════════════════════════════════


async def test_heartbeat_reports_load_memory_and_active_steps(monkeypatch, no_sleep):
    """A heartbeat carries CPU load, memory pressure and in-flight step count."""
    monkeypatch.setattr(conn_mod, "psutil", _FakePsutil(percent=55.0, cpu=25.0))
    c = AgentConnection(_config())
    ws = _FakeWS()
    c._ws = ws

    async def stop_after_first(*a, **kw):
        c._running = False
        await _REAL_SLEEP(0)

    monkeypatch.setattr(conn_mod.asyncio, "sleep", stop_after_first)
    await c._heartbeat_loop()

    msg = ws.messages()[0]
    assert msg["type"] == "heartbeat"
    assert msg["node_id"] == "node-1"
    assert msg["memory_used_pct"] == 55.0
    assert msg["active_steps"] == 0


async def test_heartbeat_load_avg_is_a_fraction_not_a_percentage(monkeypatch):
    """cpu_percent is divided by 100, so load_avg is 0.0-1.0.

    Pins the documented naming trap: despite the field name, this is not a
    Unix load average. A consumer treating it as one would be off by 100x.
    """
    monkeypatch.setattr(conn_mod, "psutil", _FakePsutil(cpu=75.0))
    c = AgentConnection(_config())
    ws = _FakeWS()
    c._ws = ws

    async def stop(*a, **kw):
        c._running = False
        await _REAL_SLEEP(0)

    monkeypatch.setattr(conn_mod.asyncio, "sleep", stop)
    await c._heartbeat_loop()

    assert ws.messages()[0]["load_avg"] == 0.75


async def test_heartbeat_exits_immediately_when_socket_is_none(monkeypatch, no_sleep):
    """No socket means the loop never runs — the guard is on _ws, not just _running."""
    monkeypatch.setattr(conn_mod, "psutil", _FakePsutil())
    c = AgentConnection(_config())
    c._ws = None

    await c._heartbeat_loop()

    assert no_sleep == []


async def test_heartbeat_exits_when_stopped(monkeypatch, no_sleep):
    """A stopped connection sends no heartbeats."""
    monkeypatch.setattr(conn_mod, "psutil", _FakePsutil())
    c = AgentConnection(_config())
    c._ws = _FakeWS()
    c._running = False

    await c._heartbeat_loop()

    assert c._ws.sent == []


async def test_heartbeat_swallows_psutil_errors_and_keeps_looping(monkeypatch):
    """A psutil failure is logged, never raised.

    Critical: _connect_and_run calls task.result(), so an escaping exception
    here would tear down a healthy connection over a transient metrics blip.
    """
    monkeypatch.setattr(conn_mod, "psutil", _FakePsutil(raises=True))
    c = AgentConnection(_config())
    c._ws = _FakeWS()
    ticks = []

    async def stop_after_two(*a, **kw):
        ticks.append(1)
        if len(ticks) >= 2:
            c._running = False
        await _REAL_SLEEP(0)

    monkeypatch.setattr(conn_mod.asyncio, "sleep", stop_after_two)
    await c._heartbeat_loop()  # must not raise

    assert len(ticks) == 2
    assert c._ws.sent == []


async def test_heartbeat_sleeps_the_configured_interval(monkeypatch):
    """The cadence is HEARTBEAT_INTERVAL; the server's offline threshold derives from it."""
    monkeypatch.setattr(conn_mod, "psutil", _FakePsutil())
    c = AgentConnection(_config())
    c._ws = _FakeWS()
    delays = []

    async def record(delay, *a, **kw):
        delays.append(delay)
        c._running = False
        await _REAL_SLEEP(0)

    monkeypatch.setattr(conn_mod.asyncio, "sleep", record)
    await c._heartbeat_loop()

    assert delays == [HEARTBEAT_INTERVAL]


async def test_heartbeat_reports_executor_active_count(monkeypatch):
    """active_steps mirrors the executor, giving the server real load telemetry."""
    monkeypatch.setattr(conn_mod, "psutil", _FakePsutil())
    c = AgentConnection(_config())
    ws = _FakeWS()
    c._ws = ws
    c.executor._running_steps = {("j", 0): object(), ("j", 1): object()}

    async def stop(*a, **kw):
        c._running = False
        await _REAL_SLEEP(0)

    monkeypatch.setattr(conn_mod.asyncio, "sleep", stop)
    await c._heartbeat_loop()

    assert ws.messages()[0]["active_steps"] == 2


# ════════════════════════════════════════════════════════════════════════
# _listen_loop — dispatch
# ════════════════════════════════════════════════════════════════════════


async def test_listen_loop_dispatches_execute_step_to_the_executor():
    """An execute_step frame reaches the executor with its params intact."""
    c = AgentConnection(_config())
    seen = []

    async def fake_execute(cmd):
        seen.append(cmd)

    c.executor.execute = fake_execute
    frame = json.dumps({
        "type": "execute_step", "job_id": "j1", "step_index": 2,
        "step_name": "run_command", "params": {"command": "echo hi"},
    })

    await c._listen_loop(_IterWS([frame]))
    await asyncio.sleep(0)  # let the detached task run

    assert len(seen) == 1
    assert seen[0].job_id == "j1"
    assert seen[0].step_index == 2
    assert seen[0].params == {"command": "echo hi"}


async def test_listen_loop_does_not_await_execute_step():
    """execute_step is fire-and-forget so a long step cannot block the listener.

    This asymmetry is required: awaiting the step would make the matching
    cancel_step undeliverable, which is the whole point of the split.
    """
    c = AgentConnection(_config())
    started = asyncio.Event()

    async def slow_execute(cmd):
        started.set()
        await asyncio.sleep(3600)

    c.executor.execute = slow_execute
    frame = json.dumps({
        "type": "execute_step", "job_id": "j", "step_index": 0,
        "step_name": "s", "params": {},
    })

    # Returns promptly even though the step never finishes.
    await asyncio.wait_for(c._listen_loop(_IterWS([frame])), timeout=1.0)
    await asyncio.sleep(0)
    assert started.is_set()


async def test_listen_loop_awaits_cancel_step():
    """cancel_step IS awaited, so cancellation is applied before the next frame."""
    c = AgentConnection(_config())
    order = []

    async def fake_cancel(cmd):
        order.append(("cancel", cmd.job_id, cmd.step_index))

    c.executor.cancel = fake_cancel
    frame = json.dumps({"type": "cancel_step", "job_id": "j9", "step_index": 4})

    await c._listen_loop(_IterWS([frame]))

    assert order == [("cancel", "j9", 4)]


async def test_listen_loop_handles_ack_without_dispatching():
    """An ack is a debug-log no-op — it must not reach the executor."""
    c = AgentConnection(_config())
    calls = []
    c.executor.execute = lambda cmd: calls.append(cmd)
    c.executor.cancel = lambda cmd: calls.append(cmd)

    await c._listen_loop(_IterWS([json.dumps({"type": "ack", "message": "ok"})]))

    assert calls == []


async def test_listen_loop_ignores_unknown_message_type():
    """An unrecognized type is logged and skipped, keeping the socket alive."""
    c = AgentConnection(_config())
    ws = _IterWS([json.dumps({"type": "who_knows"}), json.dumps({"type": "ack"})])

    await c._listen_loop(ws)  # must not raise

    assert ws._frames == []


async def test_listen_loop_ignores_non_json_frames():
    """Garbage on the wire is dropped rather than killing the connection."""
    c = AgentConnection(_config())
    ws = _IterWS(["not json at all", json.dumps({"type": "ack"})])

    await c._listen_loop(ws)  # must not raise

    assert ws._frames == []


async def test_listen_loop_survives_a_malformed_execute_step():
    """A schema-invalid command is logged, and the socket keeps serving.

    Regression guard for the reconnect storm fixed in d895144: a raise here
    killed the socket on every bad message, producing stuck jobs.
    """
    c = AgentConnection(_config())
    calls = []

    async def fake_execute(cmd):
        calls.append(cmd)

    c.executor.execute = fake_execute
    bad = json.dumps({"type": "execute_step", "job_id": "j"})  # missing fields
    good = json.dumps({
        "type": "execute_step", "job_id": "j", "step_index": 0,
        "step_name": "s", "params": {},
    })

    await c._listen_loop(_IterWS([bad, good]))
    await asyncio.sleep(0)

    # The bad frame was swallowed; the good one still dispatched.
    assert len(calls) == 1


async def test_listen_loop_survives_an_executor_cancel_raising():
    """An exception from executor.cancel is contained by the catch-all."""
    c = AgentConnection(_config())

    async def boom(cmd):
        raise RuntimeError("cancel blew up")

    c.executor.cancel = boom
    ws = _IterWS([
        json.dumps({"type": "cancel_step", "job_id": "j", "step_index": 0}),
        json.dumps({"type": "ack"}),
    ])

    await c._listen_loop(ws)  # must not raise

    assert ws._frames == []


async def test_listen_loop_handles_a_frame_with_no_type_field():
    """A JSON object without "type" falls through to the unknown branch."""
    c = AgentConnection(_config())

    await c._listen_loop(_IterWS([json.dumps({"job_id": "j"})]))  # must not raise


async def test_listen_loop_returns_when_the_socket_closes():
    """Exhausting the iterator returns, which is how a server close is observed."""
    c = AgentConnection(_config())

    await c._listen_loop(_IterWS([]))  # returns immediately


async def test_listen_loop_processes_many_frames_in_order():
    """A burst of cancels is applied in arrival order on one socket."""
    c = AgentConnection(_config())
    order = []

    async def fake_cancel(cmd):
        order.append(cmd.step_index)

    c.executor.cancel = fake_cancel
    frames = [
        json.dumps({"type": "cancel_step", "job_id": "j", "step_index": i})
        for i in range(20)
    ]

    await c._listen_loop(_IterWS(frames))

    assert order == list(range(20))


async def test_listen_loop_a_json_scalar_frame_is_swallowed():
    """A bare JSON scalar (not an object) is contained by the catch-all.

    `data.get("type")` would raise AttributeError on an int; the broad except
    is what keeps the agent socket alive.
    """
    c = AgentConnection(_config())
    ws = _IterWS(["42", json.dumps({"type": "ack"})])

    await c._listen_loop(ws)  # must not raise

    assert ws._frames == []


# ════════════════════════════════════════════════════════════════════════
# module constants
# ════════════════════════════════════════════════════════════════════════


def test_backoff_constants_are_coherent():
    """INITIAL_BACKOFF < MAX_BACKOFF, and the critical-send window outlasts both.

    The 90s critical-retry budget must exceed MAX_BACKOFF or a single
    worst-case reconnect gap silently drops a step result.
    """
    assert 0 < INITIAL_BACKOFF < MAX_BACKOFF
    assert 90 > MAX_BACKOFF


def test_heartbeat_interval_is_positive():
    """A non-positive interval would spin the heartbeat loop."""
    assert HEARTBEAT_INTERVAL > 0
