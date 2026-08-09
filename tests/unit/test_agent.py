"""Unit tests for the Nexus agent package (packages/agent/src/nexus_agent).

Scope — only the hermetic, pure parts that are safe to exercise on this host:
- config.py     : AgentConfig load/save/create semantics + CLI overrides
- capability.py : host-info detection structure + OS/arch normalization
- os_adapters/* : adapter selection logic + per-adapter command/path building
- executor.py   : StepExecutor.execute()/cancel() step lookup, output-key
                  extraction and outcome messaging, driven with a *fake* step
                  and a *fake* connection (the WebSocket is the only boundary
                  we stub; the executor logic itself is exercised for real).

Anything needing a live server/WS or a non-host OS is skipped (see comments).
"""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
import types

import pytest

# ── psutil shim ──────────────────────────────────────────────────────────
# capability.py imports `psutil` at module scope for cpu_count / virtual_memory.
# psutil is a third-party hardware-info dependency that is NOT installed in this
# offline test venv, so we install a minimal stand-in *before* importing
# nexus_agent. This stubs only a true external boundary (the OS metrics
# library) — the detection logic under test still runs for real.
if "psutil" not in sys.modules:
    _psutil = types.ModuleType("psutil")
    _psutil.cpu_count = lambda logical=True: 8

    class _VMem:
        """Minimal psutil.virtual_memory() result exposing only ``total``."""
        total = 16 * 1024 * 1024 * 1024  # 16 GiB

    _psutil.virtual_memory = lambda: _VMem()
    sys.modules["psutil"] = _psutil

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext

from nexus_agent import capability
from nexus_agent.config import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_FILE,
    AgentConfig,
)
from nexus_agent.executor import (
    StepCheckFailed,
    StepExecutor,
    SubprocessError,
    _RunningStep,
)
from nexus_agent.os_adapters import get_adapter
from nexus_agent.os_adapters.base import OSAdapter
from nexus_agent.os_adapters.linux import LinuxAdapter
from nexus_agent.os_adapters.macos import MacOSAdapter
from nexus_agent.os_adapters.windows import WindowsAdapter


# ════════════════════════════════════════════════════════════════════════
# config.py
# ════════════════════════════════════════════════════════════════════════


def test_config_path_property_derives_from_config_dir(tmp_path):
    """config_path is always <config_dir>/config.json.

    The filename is fixed, so pointing the agent at a different directory (a test
    tmpdir, or a second agent on one host) is enough to isolate its config.
    """
    cfg = AgentConfig(server_url="ws://h:8000/ws", api_key="k", config_dir=str(tmp_path))
    assert cfg.config_path == tmp_path / "config.json"


def test_node_id_defaults_to_platform_node_or_random():
    """node_id always resolves to a non-empty string.

    The factory prefers platform.node(), but that returns '' on some hosts (bare
    containers), so it falls back to a generated 'node-<hex>'. An empty node_id
    would produce an unroutable WebSocket registration.
    """
    cfg = AgentConfig(server_url="ws://h", api_key="k")
    # platform.node() may be "" on some hosts; the factory then falls back to a
    # generated "node-<hex>" id. Either way the field is a non-empty string.
    assert isinstance(cfg.node_id, str) and cfg.node_id


def test_save_writes_json_with_restricted_permissions(tmp_path):
    """save() writes exactly the four persisted keys and chmods the file to 0o600.

    SECURITY: the file holds the node's API key in plaintext, so it must not be
    world- or group-readable on a shared host. The exact-dict assertion also
    guards against transient fields (config_dir) leaking into the file.
    """
    cfg = AgentConfig(
        server_url="ws://srv:8000/ws/agent",
        api_key="secret-key",
        node_id="node-a",
        config_dir=str(tmp_path),
        tags=["gpu", "linux"],
    )
    path = cfg.save()

    assert path == tmp_path / "config.json"
    data = json.loads(path.read_text())
    assert data == {
        "server_url": "ws://srv:8000/ws/agent",
        "api_key": "secret-key",
        "node_id": "node-a",
        "tags": ["gpu", "linux"],
    }
    # API key on disk → must be owner-only (0o600).
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_save_creates_missing_parent_directories(tmp_path):
    """save() creates the full parent chain, so first-run bootstrap needs no mkdir."""
    nested = tmp_path / "deep" / "nested" / "dir"
    cfg = AgentConfig(server_url="ws://h", api_key="k", config_dir=str(nested))
    path = cfg.save()
    assert path.exists()
    assert path.parent == nested


def test_create_persists_and_returns_config(tmp_path):
    """create() writes the config to disk and load() reads back identical values.

    This is the `nexus-agent register` bootstrap path.
    """
    cfg = AgentConfig.create(
        server_url="ws://srv/ws",
        api_key="abc",
        node_id="my-node",
        config_dir=str(tmp_path),
    )
    assert cfg.node_id == "my-node"
    # create() writes to disk; load() back from that exact path must round-trip.
    loaded = AgentConfig.load(config_path=str(tmp_path / "config.json"))
    assert loaded.server_url == "ws://srv/ws"
    assert loaded.api_key == "abc"
    assert loaded.node_id == "my-node"


def test_load_round_trips_tags_and_config_dir(tmp_path):
    """tags survive the round trip, and config_dir is re-derived from the file's parent.

    config_dir is not stored in the JSON — deriving it on load means a config file
    stays valid after being moved or mounted at a different path.
    """
    AgentConfig(
        server_url="ws://h/ws",
        api_key="k",
        node_id="n1",
        config_dir=str(tmp_path),
        tags=["fast"],
    ).save()

    loaded = AgentConfig.load(config_path=str(tmp_path / "config.json"))
    assert loaded.tags == ["fast"]
    # config_dir is derived from the file's parent on load.
    assert loaded.config_dir == str(tmp_path)


def test_load_cli_overrides_take_priority_over_file(tmp_path):
    """An explicit CLI argument overrides the file value; unspecified fields still load.

    Precedence order: CLI > file. This is how an operator temporarily runs an agent
    under a different node id without editing the file.
    """
    AgentConfig(
        server_url="ws://file-host/ws",
        api_key="file-key",
        node_id="file-node",
        config_dir=str(tmp_path),
    ).save()

    loaded = AgentConfig.load(
        config_path=str(tmp_path / "config.json"),
        node_id="cli-node",
    )
    # node_id overridden by the explicit arg; the rest still comes from file.
    assert loaded.node_id == "cli-node"
    assert loaded.server_url == "ws://file-host/ws"
    assert loaded.api_key == "file-key"


def test_load_transient_config_when_server_and_key_both_given(tmp_path):
    # Both server_url and api_key supplied → no file is read or written.
    """Supplying BOTH server_url and api_key builds an in-memory config, touching no disk.

    The ephemeral/containerized-agent path: nothing is read and, critically,
    nothing is written, so a read-only or shared filesystem is fine.
    """
    missing = tmp_path / "does-not-exist.json"
    cfg = AgentConfig.load(
        config_path=str(missing),
        server_url="ws://transient/ws",
        api_key="transient-key",
        node_id="t-node",
    )
    assert cfg.server_url == "ws://transient/ws"
    assert cfg.api_key == "transient-key"
    assert cfg.node_id == "t-node"
    assert not missing.exists()  # transient config never touches disk


def test_load_missing_file_raises_file_not_found(tmp_path):
    """Without a config file and without full CLI credentials, load() fails loudly.

    Better than starting with a half-populated config that fails later at connect
    time with an opaque auth error.
    """
    missing = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError):
        AgentConfig.load(config_path=str(missing))


def test_default_config_paths_under_home():
    # The module-level defaults live under ~/.nexus-agent/.
    """The default config lives at ~/.nexus-agent/config.json.

    Documented location operators are told to inspect; changing it would strand
    existing installs.
    """
    assert DEFAULT_CONFIG_DIR.name == ".nexus-agent"
    assert DEFAULT_CONFIG_FILE == DEFAULT_CONFIG_DIR / "config.json"


# ════════════════════════════════════════════════════════════════════════
# capability.py
# ════════════════════════════════════════════════════════════════════════


def test_detect_capabilities_returns_full_register_shape():
    """detect_capabilities() returns exactly the keys the server's register message expects.

    Also asserts the removed 'capabilities'/'software' keys stay gone — the software
    capabilities concept was deleted (no scheduler gate uses it) and re-adding them
    would reintroduce a field the server no longer accepts.
    """
    info = capability.detect_capabilities()
    expected_keys = {
        "hostname", "os_type", "os_version", "arch", "cpu_model",
        "cpu_cores", "ram_mb", "gpu_info", "ip_address",
    }
    assert expected_keys == set(info.keys())
    # Software "capabilities" were intentionally removed — must NOT be present.
    assert "capabilities" not in info
    assert "software" not in info


def test_detect_capabilities_value_types():
    """Each detected value has the type/range the server's node record requires.

    cpu_cores and ram_mb are ints (they feed capacity display), and gpu_info is
    nullable since most nodes have no discrete GPU.
    """
    info = capability.detect_capabilities()
    assert isinstance(info["cpu_cores"], int) and info["cpu_cores"] >= 1
    assert isinstance(info["ram_mb"], int) and info["ram_mb"] > 0
    assert info["os_type"] in ("macos", "linux", "windows")
    assert isinstance(info["arch"], str) and info["arch"]
    # gpu_info is optional — either a string or None.
    assert info["gpu_info"] is None or isinstance(info["gpu_info"], str)


@pytest.mark.parametrize(
    "system_name,expected",
    [("Darwin", "macos"), ("Windows", "windows"), ("Linux", "linux"),
     ("FreeBSD", "linux")],  # anything non-mac/non-win normalizes to linux
)
def test_detect_os_type_normalizes(monkeypatch, system_name, expected):
    """platform.system() is normalized to the three OS tokens the scheduler knows.

    Anything that isn't Darwin/Windows collapses to 'linux' — a deliberate
    best-effort default so a BSD-ish host still schedules rather than being
    rejected as unknown.
    """
    monkeypatch.setattr(capability.platform, "system", lambda: system_name)
    assert capability._detect_os_type() == expected


@pytest.mark.parametrize(
    "machine,expected",
    [("aarch64", "arm64"), ("arm64", "arm64"),
     ("x86_64", "x86_64"), ("amd64", "x86_64"),
     ("ARM64", "arm64"),  # case-insensitive (machine is lowered)
     ("riscv64", "riscv64")],  # unknown → passthrough (lowered)
)
def test_detect_arch_normalizes(monkeypatch, machine, expected):
    """Architecture aliases collapse to canonical tokens; unknowns pass through lowered.

    aarch64/arm64 and x86_64/amd64 must unify, or the same machine would report
    different arches depending on the OS's naming convention.
    """
    monkeypatch.setattr(capability.platform, "machine", lambda: machine)
    assert capability._detect_arch() == expected


def test_detect_os_version_macos(monkeypatch):
    """On macOS the version comes from platform.mac_ver() (e.g. '14.5'), not the kernel."""
    monkeypatch.setattr(capability.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(capability.platform, "mac_ver", lambda: ("14.5", ("", "", ""), "arm64"))
    assert capability._detect_os_version() == "14.5"


def test_detect_os_version_macos_falls_back_to_release(monkeypatch):
    """When mac_ver() is empty the Darwin kernel release is used instead.

    mac_ver() returns '' inside some sandboxed/containerized environments; the
    fallback keeps os_version populated rather than blank.
    """
    monkeypatch.setattr(capability.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(capability.platform, "mac_ver", lambda: ("", ("", "", ""), ""))
    monkeypatch.setattr(capability.platform, "release", lambda: "23.5.0")
    assert capability._detect_os_version() == "23.5.0"


def test_detect_ip_returns_string():
    # On this host the UDP-socket trick should yield a non-loopback IP, but the
    # function is defined to fall back to 127.0.0.1 on any error — so we only
    # assert it returns a plausible dotted-quad string.
    """_detect_ip() returns a dotted-quad string.

    The shape is asserted rather than an exact value because the result depends on
    the host's routing table (and falls back to 127.0.0.1 on error).
    """
    ip = capability._detect_ip()
    assert isinstance(ip, str)
    assert ip.count(".") == 3


def test_detect_ip_falls_back_on_socket_error(monkeypatch):
    """A socket failure yields 127.0.0.1 instead of raising.

    Capability detection runs during agent startup; an exception here would stop
    the agent from ever registering just because the network was briefly down.
    """
    def _boom(*a, **k):
        """Simulate a hard socket failure (no network)."""
        raise OSError("no network")

    monkeypatch.setattr(capability.socket, "socket", _boom)
    assert capability._detect_ip() == "127.0.0.1"


# ════════════════════════════════════════════════════════════════════════
# os_adapters
# ════════════════════════════════════════════════════════════════════════


def test_get_adapter_selects_macos(monkeypatch):
    """platform.system()=='Darwin' selects the macOS adapter."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert isinstance(get_adapter(), MacOSAdapter)


def test_get_adapter_selects_windows(monkeypatch):
    """platform.system()=='Windows' selects the Windows adapter."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert isinstance(get_adapter(), WindowsAdapter)


def test_get_adapter_defaults_to_linux(monkeypatch):
    # Anything that isn't darwin/windows falls through to Linux.
    """Any other platform falls back to the Linux adapter.

    Mirrors _detect_os_type's best-effort default: an unrecognized Unix gets
    POSIX-ish behavior rather than crashing at startup.
    """
    monkeypatch.setattr(platform, "system", lambda: "SunOS")
    assert isinstance(get_adapter(), LinuxAdapter)


def test_get_adapter_on_this_host_is_concrete():
    """The adapter chosen for the real host is usable and its temp_dir() exists.

    temp_dir() is where every shell/python step writes its capture files, so a
    bogus path would break all step output collection.
    """
    adapter = get_adapter()
    assert isinstance(adapter, OSAdapter)
    # temp_dir must point at a real, existing directory on this machine.
    assert os.path.isdir(adapter.temp_dir())


def test_macos_adapter_commands_and_paths():
    """macOS adapter: zsh shell, brew installs, and ~ expansion in resolve_path."""
    a = MacOSAdapter()
    assert a.shell_command() == "/bin/zsh"
    assert a.package_install("git") == "brew install git"
    assert a.os_type() == "macos"
    # resolve_path expands ~ and env vars (safe to call on this host).
    home = os.path.expanduser("~")
    assert a.resolve_path("~/work") == os.path.join(home, "work")


def test_macos_resolve_path_expands_env_var(monkeypatch):
    """resolve_path expands $ENV_VAR references as well as ~.

    Lets job authors write portable paths like $HOME/work or $WORKSPACE/out.
    """
    monkeypatch.setenv("NEXUS_TEST_DIR", "/opt/nexus")
    a = MacOSAdapter()
    assert a.resolve_path("$NEXUS_TEST_DIR/bin") == "/opt/nexus/bin"


def test_linux_adapter_commands():
    """Linux adapter: bash shell and non-interactive apt-get installs.

    The -y flag matters — an interactive prompt inside an agent step would hang
    until the step timeout.
    """
    a = LinuxAdapter()
    assert a.shell_command() == "/bin/bash"
    assert a.package_install("curl") == "apt-get install -y curl"
    assert a.os_type() == "linux"


def test_windows_adapter_commands_and_path_normalization():
    """Windows adapter: PowerShell, choco installs, and forward->back slash normalization.

    Path normalization lets a job written with POSIX separators run on a Windows
    node unchanged.
    """
    a = WindowsAdapter()
    assert a.shell_command() == "powershell.exe"
    assert a.package_install("git") == "choco install git -y"
    assert a.os_type() == "windows"
    # Windows adapter normalizes forward slashes to backslashes.
    assert a.resolve_path("/opt/tools/bin") == "\\opt\\tools\\bin"


# ════════════════════════════════════════════════════════════════════════
# executor.py — driven with a fake step + fake connection
# ════════════════════════════════════════════════════════════════════════


class _FakeConnection:
    """Stand-in for AgentConnection.

    Captures every message the executor would push over the WebSocket and
    exposes the ``config`` the executor reads (server_url / node_id / api_key).
    The WS itself is the only boundary we fake; executor logic runs for real.
    """

    class _Cfg:
        """Stand-in agent config exposing the three fields the executor reads."""
        server_url = "ws://host:8000/ws/agent/node-1"
        node_id = "node-1"
        api_key = "node-key"

    def __init__(self) -> None:
        """Start with the fake config and an empty outbound-message log."""
        self.config = self._Cfg()
        self.sent: list[dict] = []

    async def send_message(self, msg: dict, critical: bool = False) -> None:
        """Record the outbound message instead of sending it over the WebSocket.

        The ``critical`` flag (used by the real connection for retry/queueing) is
        accepted and ignored — these tests assert message content, not delivery
        guarantees.
        """
        self.sent.append(msg)

    def messages_of(self, type_: str) -> list[dict]:
        """Return every recorded message whose 'type' matches ``type_``.

        Used to assert both presence and COUNT (e.g. exactly one step.completed and
        zero step.failed), which is how duplicate-emission regressions are caught.
        """
        return [m for m in self.sent if m.get("type") == type_]


class _SuccessStep(FlowStep):
    """A poll-based step that succeeds immediately and exposes OUTPUT_KEYS.

    Note: NOT decorated with @register — we never touch the global registry.
    The executor's get_step() is monkeypatched to hand back this class.
    """

    OUTPUT_KEYS = ["result_path", "count"]

    def startup(self, params, ctx):
        # startup receives the resolved params and a populated StepContext.
        """Return state containing both declared OUTPUT_KEYS plus an undeclared extra.

        The extra ('ctx_node') proves the executor exports only declared keys.
        """
        return {"result_path": "/tmp/out", "count": 3, "ctx_node": ctx.node_id}

    def check(self, state):
        """Complete immediately on the first poll."""
        return StepResult.SUCCESS

    def cancel(self, state):
        """No-op: nothing was spawned."""
        return None


class _ExplicitOutputsStep(FlowStep):
    """A step that places an explicit 'outputs' dict in state (the override path)."""

    OUTPUT_KEYS = ["ignored"]

    def startup(self, params, ctx):
        """Return an explicit 'outputs' dict alongside a conflicting OUTPUT_KEYS value."""
        return {"outputs": {"explicit": True}, "ignored": "should-not-win"}

    def check(self, state):
        """Complete immediately."""
        return StepResult.SUCCESS

    def cancel(self, state):
        """No-op."""
        return None


class _FailingCheckStep(FlowStep):
    """A poll-based step whose check() reports FAILED."""

    def startup(self, params, ctx):
        """Return empty state; the failure is produced by check()."""
        return {}

    def check(self, state):
        """Always report FAILED."""
        return StepResult.FAILED

    def cancel(self, state):
        """No-op."""
        return None


class _FailingCheckWithErrorStep(FlowStep):
    """A poll-based step whose startup() records a specific error and check() fails on it.

    Models the gem5 run_simulation container-setup-failure shape: startup()
    stores a diagnostic in state["error"] without raising, and check() reports
    FAILED once it sees that key.
    """

    def startup(self, params, ctx):
        """Return a state carrying a specific, step-defined error message."""
        return {"error": "could not create m5out in container: no such container", "exit_code": 1}

    def check(self, state):
        """Fail whenever a startup()-time error was recorded."""
        if "error" in state:
            return StepResult.FAILED
        return StepResult.SUCCESS

    def cancel(self, state):
        """No-op."""
        return None


def _execute_cmd(step_name="any", params=None):
    """Build an ExecuteStepCommand for job 'job-1', step 0.

    The import is local because the protocol module must not be imported before the
    psutil shim at the top of this file is installed.
    """
    from nexus_common.agent_protocol import ExecuteStepCommand

    return ExecuteStepCommand(
        job_id="job-1",
        step_index=0,
        step_name=step_name,
        params=params or {},
    )


async def test_execute_success_sends_started_then_completed(monkeypatch):
    """A successful step emits exactly one step.started and one step.completed, no failure.

    Exact counts matter: a duplicated completion would double-advance the runner's
    step pointer. step.started carries the post-startup state so the server can
    persist it for crash recovery.
    """
    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _SuccessStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    started = conn.messages_of("step.started")
    completed = conn.messages_of("step.completed")
    assert len(started) == 1
    assert len(completed) == 1
    assert conn.messages_of("step.failed") == []
    # The persisted state (from startup) is echoed in step.started.
    assert started[0]["state"]["result_path"] == "/tmp/out"


async def test_execute_extracts_outputs_from_output_keys(monkeypatch):
    """Only declared OUTPUT_KEYS present in state are published as step outputs.

    This is the fix for the bug where the agent returned outputs={} for every
    remote step. Undeclared state keys are dropped so internal scratch values don't
    leak into the downstream job context.
    """
    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _SuccessStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    outputs = conn.messages_of("step.completed")[0]["outputs"]
    # Only declared OUTPUT_KEYS present in state are surfaced — ctx_node is dropped.
    assert outputs == {"result_path": "/tmp/out", "count": 3}


async def test_execute_explicit_outputs_dict_overrides_output_keys(monkeypatch):
    """An explicit state['outputs'] dict takes precedence over OUTPUT_KEYS scraping.

    The escape hatch for steps whose outputs are computed rather than stored under
    fixed key names.
    """
    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _ExplicitOutputsStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    outputs = conn.messages_of("step.completed")[0]["outputs"]
    # When state has an explicit 'outputs' dict, it wins over OUTPUT_KEYS scraping.
    assert outputs == {"explicit": True}


async def test_execute_non_dict_outputs_falls_back_to_output_keys(monkeypatch):
    # state["outputs"] present but NOT a dict → the isinstance() guard rejects it
    # and OUTPUT_KEYS scraping is used instead (this is the fallback branch).
    """A non-dict state['outputs'] is rejected by the isinstance guard, falling back to scraping.

    Without the guard, a step that happens to store a string under 'outputs' would
    ship a non-dict to the server and break JSON handling of the completion message.
    """
    class _BadOutputsStep(_SuccessStep):
        """Step whose state['outputs'] is a string, not a dict."""
        OUTPUT_KEYS = ["count"]

        def startup(self, params, ctx):
            """Return a bogus non-dict 'outputs' plus a real declared key."""
            return {"outputs": "not-a-dict", "count": 7}

    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _BadOutputsStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    outputs = conn.messages_of("step.completed")[0]["outputs"]
    assert outputs == {"count": 7}


async def test_execute_passes_resolved_context_to_startup(monkeypatch):
    """The executor builds a StepContext carrying node/job identity and server callback info.

    Steps that upload artifacts back to the server (gem5 collect_results) need
    server_url plus the node API key. AI Note: the ws:// URL is rewritten to an
    http:// BASE with the /ws/ path stripped — steps make plain HTTP calls, not
    WebSocket ones.
    """
    captured = {}

    class _CtxStep(_SuccessStep):
        """Step that captures everything the executor put on the StepContext."""
        def startup(self, params, ctx):
            """Record the context fields and params, then return empty state."""
            captured["node_id"] = ctx.node_id
            captured["job_id"] = ctx.job_id
            captured["server_url"] = ctx.server_url
            captured["api_key"] = ctx.node_api_key
            captured["params"] = params
            return {}

    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _CtxStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd(params={"foo": "bar"}))

    assert captured["node_id"] == "node-1"
    assert captured["job_id"] == "job-1"
    assert captured["api_key"] == "node-key"
    # ws:// server URL is rewritten to an http:// base, stripping the /ws/ path.
    assert captured["server_url"] == "http://host:8000"
    assert captured["params"] == {"foo": "bar"}


async def test_execute_rewrites_wss_server_url_to_https_base(monkeypatch):
    # Secure ws:// (wss://) must map to https:// (not http://) and drop the /ws/ path.
    """A wss:// agent URL maps to an https:// base, not http://.

    Downgrading a TLS deployment to plaintext would send the node API key in the
    clear on every artifact upload.
    """
    captured = {}

    class _CtxStep(_SuccessStep):
        """Step that captures the rewritten server_url."""
        def startup(self, params, ctx):
            """Record the derived server_url."""
            captured["server_url"] = ctx.server_url
            return {}

    class _SecureConn(_FakeConnection):
        """Fake connection configured with a wss:// (TLS) server URL."""
        class _Cfg(_FakeConnection._Cfg):
            """Config overriding only the URL scheme/host."""
            server_url = "wss://secure.example:8443/ws/agent/node-9"

    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _CtxStep)
    conn = _SecureConn()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    assert captured["server_url"] == "https://secure.example:8443"


async def test_execute_applies_os_variant_defaults_to_params(monkeypatch):
    # The executor resolves OS-specific defaults via resolve_for_os() before
    # startup(). An OS_VARIANTS default for the host's os_type must reach params,
    # while explicit params still win.
    """The executor applies resolve_for_os() for the HOST's OS before calling startup().

    Variant defaults are merged in while explicit params still win — the same
    precedence proven in the base-class tests, here asserted end-to-end through the
    executor using the real detected host OS.
    """
    host_os = capability._detect_os_type()
    captured = {}

    class _OSVariantStep(_SuccessStep):
        """Step declaring OS_VARIANTS keyed on the actual host OS."""
        OS_VARIANTS = {host_os: {"shell": "host-default", "extra": "kept"}}

        def startup(self, params, ctx):
            """Capture the params the executor resolved."""
            captured["params"] = params
            return {}

    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _OSVariantStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    # Explicit param overrides the OS-variant default for "shell".
    await ex.execute(_execute_cmd(params={"shell": "explicit"}))

    assert captured["params"]["shell"] == "explicit"   # explicit wins
    assert captured["params"]["extra"] == "kept"        # variant default merged in


async def test_execute_poll_loop_waits_for_running_then_success(monkeypatch):
    # A poll-based step that reports RUNNING before SUCCESS exercises the
    # _poll_step loop body and the asyncio.sleep() between polls.
    """The poll loop keeps calling check() while RUNNING and completes on the terminal result.

    asyncio.sleep is patched to a no-op so the loop is exercised without the real
    inter-poll delay; the poll count proves the loop actually iterated rather than
    accepting the first result.
    """
    polls = {"n": 0}

    class _SlowPollStep(_SuccessStep):
        """Step reporting RUNNING twice before succeeding on the third poll."""
        OUTPUT_KEYS = []

        def check(self, state):
            """Return RUNNING for the first two polls, then SUCCESS."""
            polls["n"] += 1
            return StepResult.RUNNING if polls["n"] < 3 else StepResult.SUCCESS

    # Make the inter-poll sleep instant so the test is fast.
    async def _no_sleep(_secs):
        """Replace the inter-poll delay with an immediate return."""
        return None

    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _SlowPollStep)
    monkeypatch.setattr("nexus_agent.executor.asyncio.sleep", _no_sleep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    assert polls["n"] == 3  # RUNNING, RUNNING, SUCCESS
    assert len(conn.messages_of("step.completed")) == 1
    assert conn.messages_of("step.failed") == []


# ── subprocess path (safe local command on this host) ────────────────────


class _CommandStep(_SuccessStep):
    """A step whose startup() puts a real 'command' in state → subprocess path.

    The command and a post-run check() result are parameterized via class attrs
    so individual tests can drive success / non-zero-exit / check-failed paths.
    """

    _COMMAND = "echo hello-stdout; echo oops 1>&2"
    _CHECK = StepResult.SUCCESS
    OUTPUT_KEYS = []

    def startup(self, params, ctx):
        """Put a real shell command in state, selecting the executor's subprocess path."""
        return {"command": self._COMMAND}

    def check(self, state):
        """Return the class-configured post-run result."""
        return self._CHECK


async def test_execute_subprocess_streams_output_and_completes(monkeypatch):
    """A shell command's stdout/stderr are streamed as step.log lines and echoed on completion.

    The live step.log messages drive the per-job terminal view in the UI, while the
    final stdout/stderr on step.completed are what get persisted to Job.log_text.
    Both channels must carry both streams.
    """
    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _CommandStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    # Completion (exit 0) and streamed log lines on both stdout and stderr.
    completed = conn.messages_of("step.completed")
    assert len(completed) == 1
    assert completed[0]["exit_code"] == 0
    assert completed[0]["stdout"] == "hello-stdout"
    assert completed[0]["stderr"] == "oops"
    logs = conn.messages_of("step.log")
    streams = {(m["stream"], m["line"]) for m in logs}
    assert ("stdout", "hello-stdout") in streams
    assert ("stderr", "oops") in streams
    assert conn.messages_of("step.failed") == []


async def test_execute_subprocess_nonzero_exit_sends_failed(monkeypatch):
    """A non-zero exit produces step.failed carrying the real exit code and prior stdout.

    Attaching the pre-failure stdout is what makes a failed job debuggable — the
    log would otherwise be empty for exactly the runs that need it.
    """
    class _FailCmd(_CommandStep):
        """Command that prints, then exits 7."""
        _COMMAND = "echo before-fail; exit 7"

    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _FailCmd)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    # SubprocessError → step.failed; the non-zero returncode is surfaced as exit_code.
    assert len(conn.messages_of("step.started")) == 1
    failed = conn.messages_of("step.failed")
    assert len(failed) == 1
    assert failed[0]["exit_code"] == 7
    assert "7" in failed[0]["error"]
    # Captured stdout from before the failure is attached for the per-job log.
    assert failed[0]["stdout"] == "before-fail"
    assert conn.messages_of("step.completed") == []


async def test_execute_subprocess_check_failed_after_success_exit(monkeypatch):
    # exit 0 but check() returns FAILED → StepCheckFailed → step.failed.
    """exit 0 but a FAILED check() still fails the step.

    Some tools exit 0 while producing no/invalid output (e.g. gem5 without
    stats.txt); check() is the semantic verdict and it overrides the exit code.
    """
    class _ZeroExitCheckFail(_CommandStep):
        """Command exiting 0 whose check() nonetheless reports FAILED."""
        _COMMAND = "echo ok"
        _CHECK = StepResult.FAILED

    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _ZeroExitCheckFail)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    failed = conn.messages_of("step.failed")
    assert len(failed) == 1
    assert "FAILED" in failed[0]["error"]
    assert conn.messages_of("step.completed") == []


async def test_execute_unknown_step_sends_failed(monkeypatch):
    """An unknown step name fails before startup — step.failed with NO step.started.

    The absent step.started matters: the server uses it to mark a step in-flight,
    so emitting one for a step that never began would strand it as running.
    """
    def _raise(name):
        """Simulate the registry raising for an unregistered step name."""
        raise KeyError(f"Unknown step '{name}'")

    monkeypatch.setattr("nexus_agent.executor.get_step", _raise)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd(step_name="missing"))

    failed = conn.messages_of("step.failed")
    assert len(failed) == 1
    assert conn.messages_of("step.completed") == []
    # No step.started either — failure happened during lookup, before startup.
    assert conn.messages_of("step.started") == []


async def test_execute_check_failed_sends_failed(monkeypatch):
    """A step that starts but whose check() fails emits step.started AND step.failed.

    The complement of the unknown-step case: startup ran, so the started message is
    required.
    """
    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _FailingCheckStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    # Started fires (startup ran) but the outcome is a failure from check().
    assert len(conn.messages_of("step.started")) == 1
    failed = conn.messages_of("step.failed")
    assert len(failed) == 1
    assert "FAILED" in failed[0]["error"]


async def test_execute_check_failed_reports_state_error_when_present(monkeypatch):
    """A recorded state["error"] reaches step.failed verbatim, not a generic message.

    Without this, a step-specific diagnostic (e.g. "docker daemon not
    running", "no such container") recorded by startup() and turned into
    FAILED by check() would be discarded in favor of the generic "Step
    check() returned FAILED" — leaving the user to guess at the real cause.
    """
    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _FailingCheckWithErrorStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    await ex.execute(_execute_cmd())

    failed = conn.messages_of("step.failed")
    assert len(failed) == 1
    assert failed[0]["error"] == "could not create m5out in container: no such container"


async def test_execute_clears_running_step_after_completion(monkeypatch):
    """The running-step registry is emptied after execute() returns.

    The pop lives in a finally block, so this must hold for every outcome; a leak
    would make the node look permanently busy and block future cancels for that key.
    """
    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _SuccessStep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    assert ex.active_count == 0
    await ex.execute(_execute_cmd())
    # The finally-block must pop the running step regardless of outcome.
    assert ex.active_count == 0


# ── cancel() ────────────────────────────────────────────────────────────


async def test_cancel_unknown_step_is_a_noop(monkeypatch):
    """Cancelling an unknown job/step key sends nothing and does not raise.

    Cancel races completion routinely — the server may cancel a step that already
    finished, and that must not produce a spurious message or crash the handler.
    """
    from nexus_common.agent_protocol import CancelStepCommand

    conn = _FakeConnection()
    ex = StepExecutor(conn)
    # No running step under this key → cancel returns without sending anything.
    await ex.cancel(CancelStepCommand(job_id="ghost", step_index=9))
    assert conn.sent == []


async def test_cancel_invokes_step_cancel_hook():
    """cancel() invokes the step's own cancel() hook for a registered running step.

    The running step is injected directly (no subprocess/task) so only the hook
    dispatch is exercised.
    """
    from nexus_common.agent_protocol import CancelStepCommand

    cancelled = {"called": False}

    class _CancelTrackStep(_SuccessStep):
        """Step recording whether its cancel hook was invoked."""
        def cancel(self, state):
            """Flag the invocation."""
            cancelled["called"] = True

    conn = _FakeConnection()
    ex = StepExecutor(conn)
    # Inject a running step directly (no subprocess/task → cancel just calls hook).
    running = _RunningStep(
        job_id="job-2",
        step_index=1,
        step=_CancelTrackStep(),
        state={},
        params={},
    )
    ex._running_steps["job-2:1"] = running

    await ex.cancel(CancelStepCommand(job_id="job-2", step_index=1))
    assert cancelled["called"] is True


async def test_execute_cancelled_task_emits_step_failed(monkeypatch):
    # Drive the CancelledError branch of execute(): a poll-based step that never
    # finishes, whose running task is cancelled mid-flight, must emit a
    # "Step cancelled" step.failed message and clear the running step.
    """Cancelling the execute() task emits 'Step cancelled' and clears the running step.

    execute() swallows CancelledError internally (hence `await task` returns rather
    than raising) so it can report the outcome to the server before unwinding.
    AI Note: the real asyncio.sleep is bound BEFORE monkeypatching, otherwise the
    replacement would recurse into itself.
    """
    import asyncio as _asyncio

    started = _asyncio.Event()
    _real_sleep = _asyncio.sleep  # bind before patching to avoid recursion

    class _ForeverStep(_SuccessStep):
        """Step that never leaves RUNNING, so the task can be cancelled mid-poll."""
        def check(self, state):
            """Signal that polling began, then stay RUNNING forever."""
            started.set()
            return StepResult.RUNNING  # never completes

    # Real (fast) sleep so the task yields and can be cancelled.
    async def _fast_sleep(_secs):
        """Yield to the event loop instantly so the task reaches a cancellable await."""
        await _real_sleep(0)

    monkeypatch.setattr("nexus_agent.executor.get_step", lambda name: _ForeverStep)
    monkeypatch.setattr("nexus_agent.executor.asyncio.sleep", _fast_sleep)
    conn = _FakeConnection()
    ex = StepExecutor(conn)

    task = _asyncio.create_task(ex.execute(_execute_cmd()))
    await started.wait()          # ensure startup ran and we're polling
    await _asyncio.sleep(0)       # let the poll loop reach an await point
    task.cancel()
    await task                    # execute() swallows CancelledError internally

    failed = conn.messages_of("step.failed")
    assert len(failed) == 1
    assert failed[0]["error"] == "Step cancelled"
    assert ex.active_count == 0   # finally-block popped the running step


# ── _capture() ──────────────────────────────────────────────────────────


def test_capture_returns_none_tuple_for_missing_step():
    """_capture(None) returns a 4-tuple of Nones rather than raising.

    It is called on failure paths where the running step may already be gone; an
    exception here would mask the original error.
    """
    ex = StepExecutor(_FakeConnection())
    assert ex._capture(None) == (None, None, None, None)


def test_capture_reads_buffered_stream_lines():
    """_capture joins buffered stream lines and reads the command/exit code from state.

    stderr is None (not '') when there is neither a file path nor a buffer, which
    lets the server distinguish 'no stderr captured' from 'empty stderr'.
    """
    ex = StepExecutor(_FakeConnection())
    running = _RunningStep(
        job_id="j", step_index=0, step=_SuccessStep(),
        state={"command": "echo hi", "exit_code": 0}, params={},
    )
    running.captured["stdout"] = ["line1", "line2"]
    command, stdout, stderr, exit_code = ex._capture(running)
    assert command == "echo hi"
    assert stdout == "line1\nline2"
    assert stderr is None  # no stdout/stderr path and no buffer → None
    assert exit_code == 0


def test_capture_reads_from_temp_file_paths(tmp_path):
    """_capture reads stdout back from a temp file and prefers '_command_str' over 'command'.

    _command_str holds the fully resolved invocation (shell + flags), which is the
    more useful thing to show in the job log.
    """
    ex = StepExecutor(_FakeConnection())
    out_file = tmp_path / "out.log"
    out_file.write_text("from-file\n")
    running = _RunningStep(
        job_id="j", step_index=0, step=_SuccessStep(),
        state={"_command_str": "run", "stdout_path": str(out_file)}, params={},
    )
    command, stdout, stderr, _ = ex._capture(running)
    # _command_str is preferred over 'command'; stdout read back from the file.
    assert command == "run"
    assert stdout == "from-file\n"


def test_capture_truncates_oversized_stream():
    """Oversized output is truncated to the TAIL with a leading marker.

    Keeping the tail (not the head) preserves the error messages that usually
    appear at the end of a failing run, and the cap keeps a runaway log from
    blowing up the WebSocket frame and the DB column.
    """
    ex = StepExecutor(_FakeConnection())
    big = "x" * (ex._CAP_BYTES + 5000)
    running = _RunningStep(
        job_id="j", step_index=0, step=_SuccessStep(),
        state={}, params={},
    )
    running.captured["stdout"] = [big]
    _, stdout, _, _ = ex._capture(running)
    assert stdout.startswith("…[truncated]…\n")
    # Truncation keeps the tail capped at _CAP_BYTES (+ the marker prefix).
    assert len(stdout) <= ex._CAP_BYTES + len("…[truncated]…\n")


# ── exception types ─────────────────────────────────────────────────────


def test_subprocess_error_carries_returncode():
    """SubprocessError carries the child's returncode alongside its message.

    The executor reads .returncode to populate exit_code on step.failed.
    """
    err = SubprocessError("boom", returncode=42)
    assert err.returncode == 42
    assert str(err) == "boom"


def test_step_check_failed_is_exception():
    """StepCheckFailed is a real Exception so it can be raised/caught in the executor flow."""
    assert issubclass(StepCheckFailed, Exception)
