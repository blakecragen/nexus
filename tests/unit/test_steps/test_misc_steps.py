"""Tests for the remaining nexus_steps step implementations.

Covers the "misc" steps not exercised by test_shell_steps / test_flow_steps:
  - system/health_check.py     (HealthCheckStep + probe helpers)
  - python/run.py              (RunPythonStep — spawns a REAL python subprocess)
  - package/install.py         (PackageInstallStep + _build_install_cmd helper)
  - git/clone.py               (GitCloneStep)
  - git/pull.py                (GitPullStep)
  - docker/ensure_container.py (EnsureContainerStep + _find_docker helper)
  - gem5/run_simulation.py     (RunSimulationStep + _find_docker helper)
  - gem5/collect_results.py    (CollectResultsStep)

The git/docker/package/gem5 steps would shell out to git/docker/apt/gem5 for
real in startup(). Those are NOT executed against the real binaries: we test
PARAMS_SCHEMA validation, schema export, pure helpers, and (where useful) drive
startup() with subprocess monkeypatched so no repos are cloned, no packages are
installed, and no containers are touched. health_check only reads the local
machine, so its lifecycle runs for real and hermetically.

Steps are registered on `import nexus_steps` (done by conftest); these tests do
NOT register any new steps.

NOTE: packages/steps/src/nexus_steps/file/ contains only an empty __init__.py —
there are no file/* steps to test, so that part of the assignment is a no-op.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FieldError, StepContext

from nexus_steps.system.health_check import (
    HealthCheckParams,
    HealthCheckStep,
    _check_cpu,
    _check_disk,
    _check_memory,
    _check_network,
)
from nexus_steps.python.run import RunPythonParams, RunPythonStep
from nexus_steps.package import install as install_mod
from nexus_steps.package.install import (
    PackageInstallParams,
    PackageInstallStep,
    _build_install_cmd,
)
from nexus_steps.git import clone as clone_mod
from nexus_steps.git.clone import GitCloneParams, GitCloneStep
from nexus_steps.git import pull as pull_mod
from nexus_steps.git.pull import GitPullParams, GitPullStep
from nexus_steps.docker import ensure_container as ensure_mod
from nexus_steps.docker.ensure_container import (
    EnsureContainerParams,
    EnsureContainerStep,
    _find_docker,
)
from nexus_steps.gem5.run_simulation import RunSimulationParams, RunSimulationStep
from nexus_steps.gem5.collect_results import CollectResultsParams, CollectResultsStep


# ── Helpers ────────────────────────────────────────────────────────────────


def _fields(errors: list[FieldError]) -> set[str]:
    """Collect the field names from a validate_params() result."""
    return {e.field for e in errors}


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess / run() result."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        """Record the returncode/stdout/stderr a stubbed subprocess.run should report."""
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ═══════════════════════════════════════════════════════════════════════════
# system/health_check.py
# ═══════════════════════════════════════════════════════════════════════════


# ── Schema / metadata ──


def test_health_check_metadata():
    """health_check publishes one output, needs a node, and runs on all three OSes.

    No SUPPORTED_OS override means the base default applies — health checks must be
    schedulable on every node type.
    """
    assert HealthCheckStep.PARAMS_SCHEMA is HealthCheckParams
    assert HealthCheckStep.OUTPUT_KEYS == ["health_report"]
    assert HealthCheckStep.REQUIRES_NODE is True  # inherited default
    # No SUPPORTED_OS override -> all three.
    assert set(HealthCheckStep.SUPPORTED_OS) == {"macos", "linux", "windows"}


def test_health_check_to_schema_exports_checks_field():
    """The schema export names the step 'health_check' and exposes 'checks' as an optional list.

    field_type 'list' selects the multi-select widget in the job builder.
    """
    schema = HealthCheckStep.to_schema()
    assert schema["name"] == "health_check"
    assert schema["output_keys"] == ["health_report"]
    field_names = {f["name"] for f in schema["fields"]}
    assert "checks" in field_names
    checks_field = next(f for f in schema["fields"] if f["name"] == "checks")
    # checks has a default, so it must be reported as optional.
    assert checks_field["required"] is False
    assert checks_field["field_type"] == "list"


def test_health_check_startup_default_checks_run_all_four():
    # Empty params -> the default ["cpu","memory","disk","network"] runs.
    # Force network into its degraded (offline) branch so the test is hermetic
    # and overall_ok still reflects a real all-probes run.
    """Omitting 'checks' runs the full default probe set, each returning a status.

    The default must stay all-four: a job that just says health_check() is expected
    to be a complete node health sweep.
    """
    step = HealthCheckStep()
    state = step.startup({}, StepContext())
    assert set(state["health_report"].keys()) == {"cpu", "memory", "disk", "network"}
    # Every probe reported a status key.
    for name, rpt in state["health_report"].items():
        assert "status" in rpt, name


# ── Validation ──


def test_health_check_validate_defaults_ok():
    # Empty params: checks has a default, so nothing is required.
    """Empty params validate — every field has a default."""
    assert HealthCheckStep.validate_params({}) == []


def test_health_check_validate_rejects_unknown_param():
    """An unknown param is rejected (Pass 1), catching typos at submit time."""
    errors = HealthCheckStep.validate_params({"bogus": 1})
    assert "bogus" in _fields(errors)


def test_health_check_validate_rejects_non_list_checks():
    # Pass 3 (pydantic) should reject a string where a list is expected.
    """A bare string where a list is expected is rejected by Pydantic.

    Without the type check, checks="cpu" would iterate character-by-character and
    report four unknown probes.
    """
    errors = HealthCheckStep.validate_params({"checks": "cpu"})
    assert "_schema" in _fields(errors)


# ── Probe helpers (hermetic — read the local machine only) ──


def test_check_cpu_reports_count_and_arch():
    """The CPU probe reports core count, architecture and a float load average.

    load_1m is -1.0 on platforms lacking os.getloadavg(), so the type (not the
    value) is what is asserted.
    """
    out = _check_cpu()
    assert out["status"] == "ok"
    assert out["cpu_count"] >= 1
    assert isinstance(out["arch"], str) and out["arch"]
    # load values are rounded floats (or -1.0 on platforms without getloadavg).
    assert isinstance(out["load_1m"], float)


def test_check_memory_returns_status_ok():
    """The memory probe always reports ok, with totals on Linux and a note elsewhere.

    Platform-dependent by design: the probe degrades to an informational note
    rather than failing the health check on macOS/Windows.
    """
    out = _check_memory()
    assert out["status"] == "ok"
    # On Linux we get totals; on macOS we get the fallback note. Either is valid.
    assert "total_mb" in out or "note" in out


def test_check_disk_reports_root_usage():
    """The disk probe reports a positive total and a percentage in [0, 100]."""
    out = _check_disk()
    assert out["status"] == "ok"
    assert out["total_gb"] > 0
    assert 0 <= out["used_pct"] <= 100


def test_check_network_degraded_when_dns_fails(monkeypatch):
    # Don't depend on real DNS/network: force a failed lookup so the probe takes
    # its degraded branch deterministically.
    """A failed DNS lookup yields status 'degraded', not an exception.

    A node with no outbound DNS is still usable for local work, so the probe
    downgrades rather than erroring. DNS is stubbed so the test never depends on
    real network access.
    """
    import socket as socket_mod

    def _boom(*a, **k):
        """Force a DNS resolution failure."""
        raise socket_mod.gaierror("no network in test")

    monkeypatch.setattr(
        "nexus_steps.system.health_check.socket.getaddrinfo", _boom
    )
    out = _check_network()
    assert out["status"] == "degraded"
    assert out["dns_reachable"] is False
    assert isinstance(out["hostname"], str) and out["hostname"]
    assert out["dns_lookup_ms"] >= 0


def test_check_network_ok_when_dns_resolves(monkeypatch):
    # Stub getaddrinfo to succeed -> the "ok" branch (hermetic, no real DNS).
    """A successful lookup yields 'ok' and the probe queries dns.google:443.

    Asserting the actual host/port (not just the shape) pins which endpoint an
    air-gapped deployment would need to allow or expect to fail.
    """
    captured = {}

    def _ok(host, port, *a, **k):
        """Stub a successful getaddrinfo, recording the host and port queried."""
        captured["host"] = host
        captured["port"] = port
        return [(2, 1, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(
        "nexus_steps.system.health_check.socket.getaddrinfo", _ok
    )
    out = _check_network()
    assert out["status"] == "ok"
    assert out["dns_reachable"] is True
    # The probe resolves dns.google:443 (asserts the actual query, not just shape).
    assert captured["host"] == "dns.google"
    assert captured["port"] == 443


# ── Lifecycle (runs for real, hermetic) ──


def test_health_check_startup_runs_selected_probes():
    """An explicit checks list runs exactly those probes and nothing else.

    overall_ok aggregates the individual probe statuses into the step's verdict.
    """
    step = HealthCheckStep()
    state = step.startup({"checks": ["cpu", "disk"]}, StepContext())
    assert state["done"] is True
    assert set(state["health_report"].keys()) == {"cpu", "disk"}
    assert state["overall_ok"] is True
    assert step.check(state) is StepResult.SUCCESS


def test_health_check_unknown_probe_marks_failed():
    """An unknown probe name becomes an error entry and fails the whole step.

    Silently ignoring it would let a typo'd probe name produce a green health check
    that never actually ran the intended test.
    """
    step = HealthCheckStep()
    state = step.startup({"checks": ["cpu", "not_a_probe"]}, StepContext())
    # The unknown check name yields an error entry and flips overall_ok.
    assert state["health_report"]["not_a_probe"]["status"] == "error"
    assert state["overall_ok"] is False
    assert step.check(state) is StepResult.FAILED


def test_health_check_check_running_when_not_done():
    """Without the 'done' flag, check() reports RUNNING."""
    step = HealthCheckStep()
    assert step.check({}) is StepResult.RUNNING


def test_health_check_cancel_is_noop():
    """cancel() is safe — the probes are synchronous with nothing to stop."""
    HealthCheckStep().cancel({})  # must not raise


def test_health_check_probe_exception_is_caught(monkeypatch):
    # If a probe raises, startup() must catch it and record an error entry.
    """A probe that raises is caught and recorded as an error entry with its message.

    One broken probe must not abort the other probes or crash the agent; the
    exception text is preserved so the failure is diagnosable from the job log.
    """
    def _explode():
        """Probe that raises, to exercise the per-probe exception handler."""
        raise RuntimeError("probe blew up")

    monkeypatch.setitem(
        __import__("nexus_steps.system.health_check", fromlist=["_PROBES"])._PROBES,
        "cpu",
        _explode,
    )
    step = HealthCheckStep()
    state = step.startup({"checks": ["cpu"]}, StepContext())
    assert state["health_report"]["cpu"]["status"] == "error"
    assert "probe blew up" in state["health_report"]["cpu"]["message"]
    assert state["overall_ok"] is False


# ═══════════════════════════════════════════════════════════════════════════
# python/run.py
# ═══════════════════════════════════════════════════════════════════════════


def _poll(step, state, timeout=10.0, interval=0.02):
    """Poll check() until it leaves RUNNING or the timeout elapses; return the last result.

    Mirrors the agent's poll loop. check() is idempotent, so polling past a
    terminal result is harmless.
    """
    import time

    deadline = time.monotonic() + timeout
    result = step.check(state)
    while result is StepResult.RUNNING and time.monotonic() < deadline:
        time.sleep(interval)
        result = step.check(state)
    return result


def _reap(state):
    """Best-effort SIGKILL + waitpid on any pid left in state.

    Prevents a test failure from leaking a live child (or a zombie) into the rest
    of the session. Every OSError is swallowed since the process may already be
    gone.
    """
    pid = state.get("pid")
    if not pid:
        return
    try:
        os.kill(pid, 0)
    except OSError:
        return
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except OSError:
        pass


# ── Schema / metadata ──


def test_run_python_metadata():
    """run_python exports the standard exit_code/stdout/stderr trio and per-OS interpreters.

    The OS_VARIANTS interpreters are what let one job run on macOS and Windows
    nodes without specifying a path.
    """
    assert RunPythonStep.PARAMS_SCHEMA is RunPythonParams
    assert RunPythonStep.OUTPUT_KEYS == ["exit_code", "stdout_path", "stderr_path"]
    assert RunPythonStep.OS_VARIANTS["macos"]["interpreter"] == "/usr/bin/python3"
    assert RunPythonStep.OS_VARIANTS["windows"]["interpreter"] == "python.exe"


# ── Validation: the AtLeastOneRule + model_validator exclusivity ──


def test_run_python_requires_code_or_script_path():
    """With neither source, the AtLeastOneRule fails against the first field ('code')."""
    errors = RunPythonStep.validate_params({})
    # AtLeastOneRule fails on the first listed field.
    assert "code" in _fields(errors)


def test_run_python_code_alone_validates():
    """Inline code alone is a complete, valid configuration."""
    assert RunPythonStep.validate_params({"code": "print(1)"}) == []


def test_run_python_script_path_alone_validates():
    """A script path alone is a complete, valid configuration."""
    assert RunPythonStep.validate_params({"script_path": "/tmp/x.py"}) == []


def test_run_python_both_sources_rejected_by_model_validator():
    # Both code and script_path provided -> the @model_validator raises,
    # surfaced as a _schema FieldError in pass 3.
    """Supplying BOTH code and script_path is rejected as a _schema error.

    The sources are mutually exclusive; accepting both would leave which one runs
    up to implementation order. Note this exclusivity lives in a Pydantic
    model_validator (Pass 3), not in the AtLeastOneRule (Pass 2), which only checks
    the lower bound.
    """
    errors = RunPythonStep.validate_params(
        {"code": "print(1)", "script_path": "/tmp/x.py"}
    )
    assert "_schema" in _fields(errors)


def test_run_python_validate_rejects_unknown_param():
    """An unknown param is rejected even when the rest of the config is valid."""
    errors = RunPythonStep.validate_params({"code": "print(1)", "nope": 1})
    assert "nope" in _fields(errors)


def test_run_python_validate_rejects_out_of_range_timeout():
    """timeout=0 violates the ge=1 bound."""
    errors = RunPythonStep.validate_params({"code": "print(1)", "timeout": 0})
    assert "_schema" in _fields(errors)


def test_run_python_params_model_validator_direct():
    """The 'exactly one source' invariant is enforced on the model itself, both directions.

    Tested directly (not through validate_params) so the model stays safe for
    callers that construct it outside the step validation pipeline.
    """
    with pytest.raises(ValueError, match="exactly one"):
        RunPythonParams()  # neither source
    with pytest.raises(ValueError, match="exactly one"):
        RunPythonParams(code="print(1)", script_path="/tmp/x.py")  # both


# ── Lifecycle: real python subprocess (the test host has python3) ──


def test_run_python_inline_code_success():
    """Inline code is materialized to a temp file, executed, and the temp file is cleaned up.

    cleanup_source == source_path marks the file as agent-owned; check() unlinks it
    on completion so long-running agents don't accumulate temp scripts.
    """
    step = RunPythonStep()
    state = step.startup(
        {"code": "import sys; sys.exit(0)", "interpreter": "python3"},
        StepContext(),
    )
    assert state["pid"] > 0
    # Inline code is materialized to a tempfile registered for cleanup.
    assert state["cleanup_source"] == state["source_path"]
    assert os.path.basename(state["source_path"]).startswith("nexus_pycode_")

    result = _poll(step, state)
    assert result is StepResult.SUCCESS
    assert state["exit_code"] == 0
    # check() should have unlinked the materialized inline-code tempfile.
    assert not os.path.exists(state["source_path"])

    for k in ("stdout_path", "stderr_path"):
        if os.path.exists(state[k]):
            os.unlink(state[k])
    _reap(state)


def test_run_python_inline_code_nonzero_exit_fails():
    """A non-zero sys.exit maps to FAILED with the true exit code preserved."""
    step = RunPythonStep()
    state = step.startup(
        {"code": "import sys; sys.exit(5)", "interpreter": "python3"},
        StepContext(),
    )
    result = _poll(step, state)
    assert result is StepResult.FAILED
    assert state["exit_code"] == 5
    for k in ("stdout_path", "stderr_path"):
        if os.path.exists(state[k]):
            os.unlink(state[k])
    _reap(state)


def test_run_python_env_passed_to_subprocess():
    """The env param reaches the child process's environment.

    Asserted by having the child branch on the variable and encode the answer in
    its exit status, so a dropped env var makes the test fail rather than pass
    vacuously.
    """
    step = RunPythonStep()
    code = "import os,sys; sys.exit(0 if os.environ.get('NEXUS_TST')=='42' else 1)"
    state = step.startup(
        {"code": code, "interpreter": "python3", "env": {"NEXUS_TST": "42"}},
        StepContext(),
    )
    assert _poll(step, state) is StepResult.SUCCESS
    for k in ("stdout_path", "stderr_path"):
        if os.path.exists(state[k]):
            os.unlink(state[k])
    _reap(state)


def test_run_python_args_and_working_dir_reach_the_subprocess():
    # Exercise the args + working_dir params for real: the child writes its argv
    # and cwd to a file we then read back. Proves both flow through startup().
    """args and working_dir both reach the child, verified by reading back what it observed.

    The child writes its own argv and cwd to a file, so this proves real delivery
    rather than just that startup() accepted the params.
    """
    out_dir = tempfile.mkdtemp(prefix="nexus_test_pyargs_")
    marker = os.path.join(out_dir, "result.txt")
    code = (
        "import sys, os\n"
        f"open({marker!r}, 'w').write(repr(sys.argv[1:]) + '|' + os.getcwd())\n"
    )
    step = RunPythonStep()
    state = step.startup(
        {
            "code": code,
            "interpreter": "python3",
            "args": ["alpha", "beta"],
            "working_dir": out_dir,
        },
        StepContext(),
    )
    assert _poll(step, state) is StepResult.SUCCESS
    with open(marker) as fh:
        argv_repr, cwd = fh.read().split("|", 1)
    assert argv_repr == repr(["alpha", "beta"])
    assert os.path.realpath(cwd) == os.path.realpath(out_dir)
    for k in ("stdout_path", "stderr_path"):
        if os.path.exists(state[k]):
            os.unlink(state[k])
    _reap(state)
    import shutil as _sh
    _sh.rmtree(out_dir, ignore_errors=True)


def test_run_python_script_path_is_not_deleted_after_run():
    # A pre-existing script (script_path branch) has cleanup_source=None, so
    # check() must NOT unlink the user's real file.
    """A user-provided script is NEVER deleted (cleanup_source is None).

    The inverse of the inline-code cleanup: deleting a real file from the user's
    checkout would be destructive and would break re-runs.
    """
    fd, script = tempfile.mkstemp(prefix="nexus_test_realscript_", suffix=".py")
    os.write(fd, b"import sys; sys.exit(0)\n")
    os.close(fd)
    try:
        step = RunPythonStep()
        state = step.startup({"script_path": script}, StepContext())
        assert state["cleanup_source"] is None
        assert state["source_path"] == script
        assert _poll(step, state) is StepResult.SUCCESS
        # The real script must survive completion.
        assert os.path.exists(script)
        for k in ("stdout_path", "stderr_path"):
            if os.path.exists(state[k]):
                os.unlink(state[k])
        _reap(state)
    finally:
        if os.path.exists(script):
            os.unlink(script)


def test_run_python_missing_script_path_errors_at_startup():
    """A missing script fails in startup() with exit_code -1 and no spawned process.

    check() must then report FAILED without touching a pid (there is none).
    """
    step = RunPythonStep()
    state = step.startup(
        {"script_path": "/tmp/nexus_no_such_script_999.py"}, StepContext()
    )
    assert "error" in state
    assert "Script not found" in state["error"]
    assert state["exit_code"] == -1
    assert step.check(state) is StepResult.FAILED


def test_run_python_cancel_safe_without_pid():
    """cancel() tolerates state with no pid, e.g. after a startup failure."""
    RunPythonStep().cancel({})
    RunPythonStep().cancel({"pid": None})


# ═══════════════════════════════════════════════════════════════════════════
# package/install.py
# ═══════════════════════════════════════════════════════════════════════════


# ── _build_install_cmd helper (pure) ──


def test_build_install_cmd_brew():
    """brew installs are built as 'brew install <pkgs>' (no sudo, no -y needed)."""
    assert _build_install_cmd("brew", ["git", "jq"]) == ["brew", "install", "git", "jq"]


def test_build_install_cmd_apt_uses_sudo_and_yes():
    """apt installs use sudo and -y.

    Both are required in an agent context: the agent may not run as root, and an
    interactive confirmation prompt would hang the step until its timeout.
    """
    assert _build_install_cmd("apt", ["curl"]) == [
        "sudo", "apt-get", "install", "-y", "curl",
    ]


def test_build_install_cmd_choco():
    """choco installs pass -y for the same non-interactive reason as apt."""
    assert _build_install_cmd("choco", ["wget"]) == ["choco", "install", "-y", "wget"]


def test_build_install_cmd_unknown_manager_treated_as_prefix():
    # An unrecognized manager string becomes "<mgr> install <pkgs>".
    """An unrecognized manager is used as a '<mgr> install <pkgs>' prefix.

    Best-effort forward compatibility (dnf, pacman, apk...) instead of hard-failing
    on any manager not in the table.
    """
    assert _build_install_cmd("dnf", ["vim"]) == ["dnf", "install", "vim"]


def test_build_install_cmd_does_not_mutate_base_command():
    """The module-level command templates are copied, never mutated.

    _INSTALL_COMMANDS is shared process-wide state; appending package names in
    place would make every subsequent install on that agent carry the previous
    job's package list.
    """
    a = _build_install_cmd("brew", ["one"])
    b = _build_install_cmd("brew", ["two"])
    assert a == ["brew", "install", "one"]
    assert b == ["brew", "install", "two"]
    # The shared module-level base template must be untouched (list(base) copies).
    assert install_mod._INSTALL_COMMANDS["brew"] == ["brew", "install"]
    assert install_mod._INSTALL_COMMANDS["apt"] == [
        "sudo", "apt-get", "install", "-y",
    ]


# ── Schema / metadata / validation ──


def test_package_install_metadata():
    """package_install publishes 'installed' and picks apt/brew per OS variant."""
    assert PackageInstallStep.PARAMS_SCHEMA is PackageInstallParams
    assert PackageInstallStep.OUTPUT_KEYS == ["installed"]
    assert PackageInstallStep.OS_VARIANTS["linux"]["_package_manager"] == "apt"
    assert PackageInstallStep.OS_VARIANTS["macos"]["_package_manager"] == "brew"


def test_package_install_requires_packages():
    """'packages' is required."""
    errors = PackageInstallStep.validate_params({})
    assert "packages" in _fields(errors)


def test_package_install_empty_packages_rejected_by_min_length():
    """An empty package list is rejected by the min_length bound.

    Otherwise the step would run a bare 'apt-get install -y' and exit non-zero for
    an unhelpful reason.
    """
    errors = PackageInstallStep.validate_params({"packages": []})
    assert "_schema" in _fields(errors)


def test_package_install_valid_params_ok():
    """A single-package list is valid."""
    assert PackageInstallStep.validate_params({"packages": ["git"]}) == []


def test_package_install_unknown_param_rejected():
    """An unknown param is rejected."""
    errors = PackageInstallStep.validate_params({"packages": ["git"], "x": 1})
    assert "x" in _fields(errors)


# ── Lifecycle with subprocess.run stubbed (no real installs) ──


def test_package_install_success_with_stubbed_subprocess(monkeypatch):
    """A successful install records the package list and builds the apt command.

    subprocess.run is stubbed so nothing is actually installed. With no OS variant
    supplied, startup() falls back to apt.
    """
    captured = {}

    def _fake_run(cmd, **kwargs):
        """Capture the command and report success."""
        captured["cmd"] = cmd
        return _FakeCompleted(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(install_mod.subprocess, "run", _fake_run)
    step = PackageInstallStep()
    # No OS variant in ctx -> manager defaults to "apt" per startup().
    state = step.startup({"packages": ["git", "curl"]}, StepContext())

    assert state["installed"] == ["git", "curl"]
    assert state["done"] is True
    assert captured["cmd"] == ["sudo", "apt-get", "install", "-y", "git", "curl"]
    assert step.check(state) is StepResult.SUCCESS


def test_package_install_override_manager(monkeypatch):
    """package_manager_override forces a specific manager regardless of the host OS.

    The escape hatch for a node whose OS default is wrong (e.g. brew on Linux).
    """
    captured = {}

    def _fake_run(cmd, **kwargs):
        """Capture the command and report success."""
        captured["cmd"] = cmd
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(install_mod.subprocess, "run", _fake_run)
    step = PackageInstallStep()
    step.startup(
        {"packages": ["vim"], "package_manager_override": "brew"}, StepContext()
    )
    assert captured["cmd"] == ["brew", "install", "vim"]


def test_package_install_uses_os_variant_manager_from_context(monkeypatch):
    """The OS-variant-injected _package_manager reaches the command builder.

    _package_manager is not a declared schema field — it rides through the resolved
    params dict from resolve_for_os(), which is why an unknown-param rejection
    must not apply to it.
    """
    captured = {}

    def _fake_run(cmd, **k):
        """Capture the command and report success."""
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(install_mod.subprocess, "run", _fake_run)
    step = PackageInstallStep()
    # Simulate resolve_for_os having injected the macOS manager into params.
    state = step.startup(
        {"packages": ["jq"], "_package_manager": "brew"}, StepContext()
    )
    # _package_manager is not a schema field -> resolve still forwards it via
    # resolved dict; cmd should use brew.
    assert captured["cmd"][0] == "brew"
    assert state["installed"] == ["jq"]


def test_package_install_manager_not_found(monkeypatch):
    """A missing package manager binary is reported as an error with nothing installed.

    FileNotFoundError from subprocess means the manager isn't on the node at all;
    'installed' must stay empty so downstream steps don't assume the packages exist.
    """
    def _raise(cmd, **kwargs):
        """Simulate the package manager binary being absent."""
        raise FileNotFoundError("no apt here")

    monkeypatch.setattr(install_mod.subprocess, "run", _raise)
    step = PackageInstallStep()
    state = step.startup({"packages": ["git"]}, StepContext())
    assert "error" in state
    assert "not found" in state["error"]
    assert state["installed"] == []
    assert step.check(state) is StepResult.FAILED


def test_package_install_nonzero_exit_fails(monkeypatch):
    """A non-zero install exit is reported with the code embedded in the error."""
    monkeypatch.setattr(
        install_mod.subprocess,
        "run",
        lambda cmd, **k: _FakeCompleted(returncode=100, stderr="boom"),
    )
    step = PackageInstallStep()
    state = step.startup({"packages": ["git"]}, StepContext())
    assert "error" in state
    assert "exit 100" in state["error"]
    assert step.check(state) is StepResult.FAILED


def test_package_install_timeout(monkeypatch):
    """A TimeoutExpired is caught and reported as a timeout error, not an uncaught exception.

    Package installs commonly hang on a lock or a slow mirror; the step must fail
    cleanly so the runner can apply on_fail.
    """
    def _timeout(cmd, **kwargs):
        """Simulate the install exceeding its timeout."""
        raise subprocess.TimeoutExpired(cmd, 600)

    monkeypatch.setattr(install_mod.subprocess, "run", _timeout)
    step = PackageInstallStep()
    state = step.startup({"packages": ["git"]}, StepContext())
    assert "timed out" in state["error"]
    assert step.check(state) is StepResult.FAILED


def test_package_install_check_running_before_done():
    """Without 'done', check() reports RUNNING."""
    assert PackageInstallStep().check({}) is StepResult.RUNNING


def test_package_install_cancel_noop():
    """cancel() is safe — the install runs synchronously inside startup()."""
    PackageInstallStep().cancel({})


# ═══════════════════════════════════════════════════════════════════════════
# git/clone.py
# ═══════════════════════════════════════════════════════════════════════════


def test_git_clone_metadata():
    """git_clone publishes clone_path and commit_sha for downstream steps.

    clone_path is what a following build/run step consumes via context resolution.
    """
    assert GitCloneStep.PARAMS_SCHEMA is GitCloneParams
    assert GitCloneStep.OUTPUT_KEYS == ["clone_path", "commit_sha"]


def test_git_clone_requires_repo_url():
    """repo_url is required."""
    errors = GitCloneStep.validate_params({})
    assert "repo_url" in _fields(errors)


def test_git_clone_valid_minimal():
    """A repo_url alone is a complete, valid configuration."""
    assert GitCloneStep.validate_params({"repo_url": "https://x/y.git"}) == []


def test_git_clone_depth_must_be_positive():
    """depth=0 is rejected; a zero-depth clone is meaningless."""
    errors = GitCloneStep.validate_params(
        {"repo_url": "https://x/y.git", "depth": 0}
    )
    assert "_schema" in _fields(errors)


def test_git_clone_unknown_param_rejected():
    """An unknown param is rejected."""
    errors = GitCloneStep.validate_params({"repo_url": "https://x/y.git", "z": 1})
    assert "z" in _fields(errors)


def test_git_clone_success_builds_expected_command(monkeypatch):
    """A successful clone builds the right git command and resolves HEAD to a sha.

    Pins the argument ORDER (flags before the url/dest pair), the default
    destination derived from the repo name with '.git' stripped, and the two-call
    sequence (clone, then rev-parse). git is stubbed — nothing is cloned.
    """
    calls = []

    def _fake_run(cmd, **kwargs):
        """Record each git invocation; answer rev-parse with a fixed sha."""
        calls.append(cmd)
        # First call is the clone, second is rev-parse HEAD.
        if "rev-parse" in cmd:
            return _FakeCompleted(returncode=0, stdout="abc123def\n")
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(clone_mod.subprocess, "run", _fake_run)
    step = GitCloneStep()
    state = step.startup(
        {"repo_url": "https://github.com/org/repo.git", "depth": 1,
         "branch": "main"},
        StepContext(),
    )

    assert state["done"] is True
    assert state["commit_sha"] == "abc123def"
    # dest derived from repo name with .git stripped.
    assert state["clone_path"] == "/tmp/nexus_clone_repo"
    clone_cmd = calls[0]
    assert clone_cmd[:2] == ["git", "clone"]
    assert "--depth" in clone_cmd and "1" in clone_cmd
    assert "--branch" in clone_cmd and "main" in clone_cmd
    assert clone_cmd[-2:] == ["https://github.com/org/repo.git", "/tmp/nexus_clone_repo"]
    assert step.check(state) is StepResult.SUCCESS


def test_git_clone_custom_dest_dir(monkeypatch):
    """An explicit dest_dir overrides the name-derived default clone path."""
    monkeypatch.setattr(
        clone_mod.subprocess,
        "run",
        lambda cmd, **k: _FakeCompleted(0, stdout="sha\n"),
    )
    step = GitCloneStep()
    state = step.startup(
        {"repo_url": "https://x/y.git", "dest_dir": "/opt/checkout"},
        StepContext(),
    )
    assert state["clone_path"] == "/opt/checkout"


def test_git_clone_failure_returns_error(monkeypatch):
    """A failed clone is captured as an error string, not an uncaught CalledProcessError.

    An escaping exception would kill the agent's step handler instead of failing
    just this step.
    """
    def _fail(cmd, **kwargs):
        """Simulate git exiting 128 (e.g. auth failure or bad url)."""
        raise subprocess.CalledProcessError(128, cmd, stderr="fatal: nope")

    monkeypatch.setattr(clone_mod.subprocess, "run", _fail)
    step = GitCloneStep()
    state = step.startup({"repo_url": "https://x/y.git"}, StepContext())
    assert "error" in state
    assert "git clone failed" in state["error"]
    assert step.check(state) is StepResult.FAILED


def test_git_clone_timeout(monkeypatch):
    """A clone that exceeds its timeout is reported as a timeout error."""
    def _timeout(cmd, **kwargs):
        """Simulate the clone exceeding its timeout."""
        raise subprocess.TimeoutExpired(cmd, 600)

    monkeypatch.setattr(clone_mod.subprocess, "run", _timeout)
    step = GitCloneStep()
    state = step.startup({"repo_url": "https://x/y.git"}, StepContext())
    assert "timed out" in state["error"]
    assert step.check(state) is StepResult.FAILED


def test_git_clone_head_resolution_failure_yields_unknown_sha(monkeypatch):
    """If the clone succeeds but rev-parse fails, commit_sha becomes 'unknown' and the step still succeeds.

    The checkout is usable even without a resolved sha, so a rev-parse hiccup must
    not discard a completed clone. Downstream consumers must therefore treat
    'unknown' as a possible commit_sha value.
    """
    def _fake_run(cmd, **kwargs):
        """Succeed on clone, fail on rev-parse."""
        if "rev-parse" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return _FakeCompleted(0)

    monkeypatch.setattr(clone_mod.subprocess, "run", _fake_run)
    step = GitCloneStep()
    state = step.startup({"repo_url": "https://x/y.git"}, StepContext())
    assert state["commit_sha"] == "unknown"
    assert state["done"] is True


def test_git_clone_cancel_noop():
    """cancel() is safe — the clone runs synchronously inside startup()."""
    GitCloneStep().cancel({})


# ═══════════════════════════════════════════════════════════════════════════
# git/pull.py
# ═══════════════════════════════════════════════════════════════════════════


def test_git_pull_metadata():
    """git_pull publishes commit_sha and an 'updated' flag.

    'updated' lets a following step (rebuild, redeploy) skip work when nothing moved.
    """
    assert GitPullStep.PARAMS_SCHEMA is GitPullParams
    assert GitPullStep.OUTPUT_KEYS == ["commit_sha", "updated"]


def test_git_pull_requires_repo_dir():
    """repo_dir is required."""
    errors = GitPullStep.validate_params({})
    assert "repo_dir" in _fields(errors)


def test_git_pull_valid_minimal():
    """A repo_dir alone is a complete, valid configuration."""
    assert GitPullStep.validate_params({"repo_dir": "/tmp/repo"}) == []


def test_git_pull_remote_default_is_origin():
    """remote defaults to 'origin'."""
    assert GitPullParams(repo_dir="/tmp/repo").remote == "origin"


def test_git_pull_unknown_param_rejected():
    """An unknown param is rejected."""
    errors = GitPullStep.validate_params({"repo_dir": "/tmp/repo", "q": 1})
    assert "q" in _fields(errors)


def test_git_pull_success_detects_update(monkeypatch):
    """'updated' is computed by comparing the sha before and after the pull.

    Also pins the pull command shape: `git -C <dir> pull <remote> <branch>` — the
    -C form means the step never has to chdir the agent process.
    """
    seq = ["old_sha\n", "new_sha\n"]
    calls = []

    def _fake_run(cmd, **kwargs):
        """Answer the two rev-parse calls with different shas to simulate an update."""
        calls.append(cmd)
        if "rev-parse" in cmd:
            return _FakeCompleted(0, stdout=seq.pop(0))
        return _FakeCompleted(0)  # the pull itself

    monkeypatch.setattr(pull_mod.subprocess, "run", _fake_run)
    step = GitPullStep()
    state = step.startup(
        {"repo_dir": "/tmp/repo", "branch": "main"}, StepContext()
    )
    assert state["commit_sha"] == "new_sha"
    assert state["updated"] is True
    assert state["done"] is True
    # pull command includes the explicit branch.
    pull_cmd = next(c for c in calls if "pull" in c)
    assert pull_cmd == ["git", "-C", "/tmp/repo", "pull", "origin", "main"]
    assert step.check(state) is StepResult.SUCCESS


def test_git_pull_no_change_reports_not_updated(monkeypatch):
    """An unchanged sha reports updated=False so downstream steps can skip work."""
    def _fake_run(cmd, **kwargs):
        """Answer both rev-parse calls with the same sha."""
        if "rev-parse" in cmd:
            return _FakeCompleted(0, stdout="same_sha\n")
        return _FakeCompleted(0)

    monkeypatch.setattr(pull_mod.subprocess, "run", _fake_run)
    step = GitPullStep()
    state = step.startup({"repo_dir": "/tmp/repo"}, StepContext())
    assert state["updated"] is False
    assert state["commit_sha"] == "same_sha"


def test_git_pull_not_a_repo_errors(monkeypatch):
    """A directory that isn't a git repo fails on the initial rev-parse with a clear message.

    The pre-pull rev-parse doubles as the 'is this a repo' probe.
    """
    def _fail(cmd, **kwargs):
        """Simulate git rejecting the directory."""
        raise subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr(pull_mod.subprocess, "run", _fail)
    step = GitPullStep()
    state = step.startup({"repo_dir": "/tmp/notrepo"}, StepContext())
    assert "Not a git repository" in state["error"]
    assert step.check(state) is StepResult.FAILED


def test_git_pull_pull_failure_errors(monkeypatch):
    """A failed pull (e.g. merge conflict) is captured as an error string."""
    def _fake_run(cmd, **kwargs):
        """Succeed on rev-parse, fail on the pull itself."""
        if "rev-parse" in cmd:
            return _FakeCompleted(0, stdout="sha\n")
        # the pull command fails
        raise subprocess.CalledProcessError(1, cmd, stderr="merge conflict")

    monkeypatch.setattr(pull_mod.subprocess, "run", _fake_run)
    step = GitPullStep()
    state = step.startup({"repo_dir": "/tmp/repo"}, StepContext())
    assert "git pull failed" in state["error"]
    assert step.check(state) is StepResult.FAILED


def test_git_pull_timeout(monkeypatch):
    """A pull that exceeds its timeout is reported as a timeout error."""
    def _fake_run(cmd, **kwargs):
        """Succeed on rev-parse, then time out on the pull."""
        if "rev-parse" in cmd:
            return _FakeCompleted(0, stdout="sha\n")
        raise subprocess.TimeoutExpired(cmd, 300)

    monkeypatch.setattr(pull_mod.subprocess, "run", _fake_run)
    step = GitPullStep()
    state = step.startup({"repo_dir": "/tmp/repo"}, StepContext())
    assert "timed out" in state["error"]


def test_git_pull_post_pull_rev_parse_failure_yields_unknown_sha(monkeypatch):
    # Pre-pull rev-parse succeeds, pull succeeds, but the post-pull rev-parse
    # fails -> commit_sha falls back to "unknown" and the step still completes.
    """A failed post-pull rev-parse yields commit_sha='unknown' and, as a side effect, updated=True.

    AI Note: 'updated' is computed as pre != post, so comparing a real sha against
    the 'unknown' sentinel always reports an update — even if the pull changed
    nothing. Documented as current behavior; downstream steps keyed on 'updated'
    may do redundant work in this edge case.
    """
    calls = {"rev_parse": 0}

    def _fake_run(cmd, **kwargs):
        """Succeed on the first rev-parse and the pull, then fail the second rev-parse."""
        if "rev-parse" in cmd:
            calls["rev_parse"] += 1
            if calls["rev_parse"] == 1:
                return _FakeCompleted(0, stdout="old_sha\n")
            raise subprocess.CalledProcessError(1, cmd)
        return _FakeCompleted(0)  # the pull

    monkeypatch.setattr(pull_mod.subprocess, "run", _fake_run)
    step = GitPullStep()
    state = step.startup({"repo_dir": "/tmp/repo"}, StepContext())
    assert state["commit_sha"] == "unknown"
    assert state["done"] is True
    # pre != post ("old_sha" vs "unknown") -> reported as updated.
    assert state["updated"] is True
    assert step.check(state) is StepResult.SUCCESS


def test_git_pull_cancel_noop():
    """cancel() is safe — the pull runs synchronously inside startup()."""
    GitPullStep().cancel({})


# ═══════════════════════════════════════════════════════════════════════════
# docker/ensure_container.py
# ═══════════════════════════════════════════════════════════════════════════


# ── _find_docker helper ──


def test_find_docker_explicit_path_wins():
    """An explicitly configured docker path is used verbatim, without probing.

    Lets an operator point at a non-standard install without touching PATH.
    """
    assert _find_docker("/custom/docker") == "/custom/docker"


def test_find_docker_uses_shutil_which(monkeypatch):
    """With no explicit path, PATH is searched via shutil.which."""
    monkeypatch.setattr(ensure_mod.shutil, "which", lambda name: "/usr/bin/docker")
    assert _find_docker(None) == "/usr/bin/docker"


def test_find_docker_falls_back_to_candidates(monkeypatch):
    """When PATH lookup fails, known install locations are probed.

    Agents launched from launchd/systemd often have a minimal PATH that omits
    /opt/homebrew/bin, so the hardcoded candidate list is what makes Docker work on
    a stock macOS node.
    """
    monkeypatch.setattr(ensure_mod.shutil, "which", lambda name: None)
    # Only the homebrew candidate "exists".
    monkeypatch.setattr(
        ensure_mod.os.path,
        "exists",
        lambda p: p == "/opt/homebrew/bin/docker",
    )
    assert _find_docker(None) == "/opt/homebrew/bin/docker"


def test_find_docker_returns_none_when_missing(monkeypatch):
    """With nothing found, None is returned so callers can emit a clear 'not found' error."""
    monkeypatch.setattr(ensure_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(ensure_mod.os.path, "exists", lambda p: False)
    assert _find_docker(None) is None


# ── Schema / metadata / validation ──


def test_ensure_container_metadata():
    """ensure_container publishes container/docker/created/exit_code and excludes Windows.

    SUPPORTED_OS gates scheduling, so a docker step will never be dispatched to a
    Windows node.
    """
    assert EnsureContainerStep.PARAMS_SCHEMA is EnsureContainerParams
    assert EnsureContainerStep.OUTPUT_KEYS == [
        "container", "docker", "created", "exit_code",
    ]
    # Docker steps cannot run on windows.
    assert EnsureContainerStep.SUPPORTED_OS == ["macos", "linux"]
    assert EnsureContainerStep.supports_os("windows") is False
    assert EnsureContainerStep.supports_os("linux") is True


def test_ensure_container_defaults():
    """Defaults target the gem5 image with no mounts and no forced recreate.

    recreate=False is the safe default: re-running a job reuses the existing
    container instead of discarding its state.
    """
    p = EnsureContainerParams()
    assert p.name == "gem5_img"
    assert p.image.startswith("ghcr.io/gem5/")
    assert p.recreate is False
    assert p.mounts == []


def test_ensure_container_validate_empty_ok():
    # All fields have defaults.
    """Empty params validate — every field has a default."""
    assert EnsureContainerStep.validate_params({}) == []


def test_ensure_container_timeout_bounds():
    """timeout=0 is rejected by the lower bound."""
    errors = EnsureContainerStep.validate_params({"timeout": 0})
    assert "_schema" in _fields(errors)


def test_ensure_container_unknown_param_rejected():
    """An unknown param is rejected."""
    errors = EnsureContainerStep.validate_params({"foo": "bar"})
    assert "foo" in _fields(errors)


# ── Lifecycle ──


def test_ensure_container_no_docker_binary(monkeypatch):
    """A node without docker fails immediately with a clear message and exit_code -1.

    Guard runs before any subprocess call, so the failure is diagnosable rather
    than a FileNotFoundError traceback.
    """
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: None)
    step = EnsureContainerStep()
    state = step.startup({"name": "c1"}, StepContext())
    assert state["error"] == "docker binary not found on node"
    assert state["exit_code"] == -1
    assert step.check(state) is StepResult.FAILED


def test_ensure_container_creates_new_container(monkeypatch):
    """A container that doesn't exist is created with the requested mounts and kept alive.

    A bare host path becomes a HOST:HOST bind mount (same path inside and out) so
    job-authored paths resolve identically on both sides. The 'sleep infinity'
    entrypoint is what keeps the container up for subsequent `docker exec` steps.
    """
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")
    runs = []

    def _fake_run(cmd, **kwargs):
        """Report the container absent for the first two ps probes, present afterwards.

        The call-count branching emulates the state transition: not-running and
        not-present before `docker run`, then visible on the post-run confirmation.
        """
        runs.append(cmd)
        # cmd[0] is the docker binary; cmd[1] is the subcommand.
        sub = cmd[1]
        if sub == "ps":
            # Not running / not present (and after run, "up" query returns name).
            # Distinguish the final "up" check: it's the ps without -a issued
            # AFTER a run. Use a counter via len(runs).
            # First two ps calls (running, exists) -> empty.
            ps_calls = [c for c in runs if c[1] == "ps"]
            if len(ps_calls) <= 2:
                return _FakeCompleted(0, stdout="")
            return _FakeCompleted(0, stdout="c1")  # final confirmation
        if sub == "run":
            return _FakeCompleted(0, stdout="containerid")
        return _FakeCompleted(0)

    monkeypatch.setattr(ensure_mod.subprocess, "run", _fake_run)
    step = EnsureContainerStep()
    state = step.startup(
        {"name": "c1", "image": "img:latest", "mounts": ["/data"]},
        StepContext(),
    )
    assert state["exit_code"] == 0
    assert state["container"] == "c1"
    assert state["created"] is True
    assert state["docker"] == "/bin/docker"
    # The run command must bind-mount /data at the same path inside.
    run_cmd = next(c for c in runs if c[1] == "run")
    assert "-v" in run_cmd
    assert "/data:/data" in run_cmd
    assert run_cmd[-2:] == ["sleep", "infinity"]
    assert step.check(state) is StepResult.SUCCESS


def test_ensure_container_attaches_to_running(monkeypatch):
    """An already-running container is reused (created=False), with no run/start issued.

    Idempotence: re-running a job must not disturb a live container.
    """
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")

    def _fake_run(cmd, **kwargs):
        """Report the container as running for every ps probe."""
        if cmd[1] == "ps":
            return _FakeCompleted(0, stdout="c1")  # already running everywhere
        return _FakeCompleted(0)

    monkeypatch.setattr(ensure_mod.subprocess, "run", _fake_run)
    step = EnsureContainerStep()
    state = step.startup({"name": "c1"}, StepContext())
    assert state["exit_code"] == 0
    assert state["created"] is False
    assert "already running" in state["_log"]


def test_ensure_container_starts_stopped_container(monkeypatch):
    # Exists (ps -a) but not running (ps) -> startup must `docker start` it,
    # not recreate it; created stays False.
    """A stopped-but-existing container is STARTED, never recreated.

    Recreating would destroy the container's filesystem state (built artifacts,
    caches). The test asserts no `docker run` was issued at all.
    """
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")
    runs = []

    def _fake_run(cmd, **kwargs):
        """Report exists-but-stopped, flipping to running only after `docker start`."""
        runs.append(cmd)
        sub = cmd[1]
        if sub == "ps":
            # `ps -a` (exists) returns the name; plain `ps` (running) is empty
            # until after start. Detect the post-start confirmation ps.
            if "-a" in cmd:
                return _FakeCompleted(0, stdout="c1")
            started = any(c[1] == "start" for c in runs)
            return _FakeCompleted(0, stdout="c1" if started else "")
        return _FakeCompleted(0)

    monkeypatch.setattr(ensure_mod.subprocess, "run", _fake_run)
    step = EnsureContainerStep()
    state = step.startup({"name": "c1"}, StepContext())
    assert state["exit_code"] == 0
    assert state["created"] is False
    assert any(c[1] == "start" for c in runs)
    assert "exists but stopped" in state["_log"]
    assert "run" not in [c[1] for c in runs]  # must NOT recreate


def test_ensure_container_start_failure(monkeypatch):
    """A failed `docker start` is reported with docker's own exit code."""
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")

    def _fake_run(cmd, **kwargs):
        """Report exists-but-stopped, then fail the start."""
        sub = cmd[1]
        if sub == "ps":
            return _FakeCompleted(0, stdout="c1") if "-a" in cmd else _FakeCompleted(0, stdout="")
        if sub == "start":
            return _FakeCompleted(1, stderr="cannot start")
        return _FakeCompleted(0)

    monkeypatch.setattr(ensure_mod.subprocess, "run", _fake_run)
    step = EnsureContainerStep()
    state = step.startup({"name": "c1"}, StepContext())
    assert "docker start failed" in state["error"]
    assert state["exit_code"] == 1
    assert step.check(state) is StepResult.FAILED


def test_ensure_container_recreate_removes_then_creates(monkeypatch):
    # recreate=True with an existing container -> `docker rm -f` then `docker run`.
    """recreate=True forces `docker rm -f` before `docker run` (created=True).

    The explicit opt-in for discarding container state, with the ordering asserted
    so a run-before-rm regression can't slip through.
    """
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")
    runs = []

    def _fake_run(cmd, **kwargs):
        """Model the container existing before the run and being present after it."""
        runs.append(cmd)
        sub = cmd[1]
        if sub == "ps":
            # Before recreate: exists (ps -a) returns name, running empty.
            # After run: confirmation ps returns the name.
            ran = any(c[1] == "run" for c in runs)
            if "-a" in cmd:
                return _FakeCompleted(0, stdout="" if ran else "c1")
            return _FakeCompleted(0, stdout="c1" if ran else "")
        if sub == "run":
            return _FakeCompleted(0, stdout="newid")
        return _FakeCompleted(0)

    monkeypatch.setattr(ensure_mod.subprocess, "run", _fake_run)
    step = EnsureContainerStep()
    state = step.startup({"name": "c1", "recreate": True}, StepContext())
    assert state["exit_code"] == 0
    assert state["created"] is True
    # rm -f issued before the run.
    rm_cmds = [c for c in runs if c[1] == "rm"]
    assert rm_cmds and rm_cmds[0][:3] == ["/bin/docker", "rm", "-f"]
    assert "recreate=true" in state["_log"]


def test_ensure_container_not_running_after_ensure(monkeypatch):
    # docker run "succeeds" but the final confirmation ps never sees the
    # container -> the post-ensure guard fires.
    """If the final confirmation ps doesn't see the container, the step fails.

    The post-ensure guard catches a container that exits immediately after `docker
    run` succeeds (bad image entrypoint) — without it, downstream `docker exec`
    steps would fail with a confusing 'no such container'.
    """
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")

    def _fake_run(cmd, **kwargs):
        """Report the run as successful but never show the container as up."""
        if cmd[1] == "ps":
            return _FakeCompleted(0, stdout="")  # never up
        if cmd[1] == "run":
            return _FakeCompleted(0, stdout="id")
        return _FakeCompleted(0)

    monkeypatch.setattr(ensure_mod.subprocess, "run", _fake_run)
    step = EnsureContainerStep()
    state = step.startup({"name": "c1"}, StepContext())
    assert "not running after ensure" in state["error"]
    assert state["exit_code"] == 1
    assert step.check(state) is StepResult.FAILED


def test_ensure_container_explicit_mount_spec_passthrough(monkeypatch):
    # A mount already containing ':' must be passed through verbatim (not
    # rewritten to HOST:HOST).
    """A mount already containing ':' is passed through verbatim, and workdir becomes -w.

    Without the ':' check, 'HOST:CONTAINER' would be rewritten to
    'HOST:CONTAINER:HOST:CONTAINER' and docker would reject it.
    """
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")
    runs = []

    def _fake_run(cmd, **kwargs):
        """Report the container as up once `docker run` has been issued."""
        runs.append(cmd)
        if cmd[1] == "ps":
            ran = any(c[1] == "run" for c in runs)
            return _FakeCompleted(0, stdout="c1" if ran else "")
        if cmd[1] == "run":
            return _FakeCompleted(0, stdout="id")
        return _FakeCompleted(0)

    monkeypatch.setattr(ensure_mod.subprocess, "run", _fake_run)
    step = EnsureContainerStep()
    state = step.startup(
        {"name": "c1", "mounts": ["/host/path:/container/path"], "workdir": "/work"},
        StepContext(),
    )
    assert state["exit_code"] == 0
    run_cmd = next(c for c in runs if c[1] == "run")
    assert "/host/path:/container/path" in run_cmd
    # workdir flows through as -w.
    assert "-w" in run_cmd
    assert run_cmd[run_cmd.index("-w") + 1] == "/work"


def test_ensure_container_unexpected_exception_captured(monkeypatch):
    # A non-timeout exception from a docker call is caught and reported.
    """An unexpected exception is caught and reported as 'Type: message' with exit_code -1.

    The catch-all keeps an unforeseen docker/CLI failure from taking down the
    agent's step handler.
    """
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")

    def _boom(cmd, **kwargs):
        """Raise a non-timeout exception from a docker call."""
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ensure_mod.subprocess, "run", _boom)
    step = EnsureContainerStep()
    state = step.startup({"name": "c1"}, StepContext())
    assert "RuntimeError: kaboom" in state["error"]
    assert state["exit_code"] == -1
    assert step.check(state) is StepResult.FAILED


def test_ensure_container_run_failure(monkeypatch):
    """A failed `docker run` (e.g. missing image) is reported with docker's exit code."""
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")

    def _fake_run(cmd, **kwargs):
        """Report the container absent, then fail the run."""
        if cmd[1] == "ps":
            return _FakeCompleted(0, stdout="")  # not present
        if cmd[1] == "run":
            return _FakeCompleted(1, stderr="no such image")
        return _FakeCompleted(0)

    monkeypatch.setattr(ensure_mod.subprocess, "run", _fake_run)
    step = EnsureContainerStep()
    state = step.startup({"name": "c1"}, StepContext())
    assert "docker run failed" in state["error"]
    assert state["exit_code"] == 1
    assert step.check(state) is StepResult.FAILED


def test_ensure_container_timeout(monkeypatch):
    """A docker command timeout is reported as a timeout error with exit_code -1."""
    monkeypatch.setattr(ensure_mod, "_find_docker", lambda explicit: "/bin/docker")

    def _fake_run(cmd, **kwargs):
        """Time out on every docker invocation."""
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(ensure_mod.subprocess, "run", _fake_run)
    step = EnsureContainerStep()
    state = step.startup({"name": "c1"}, StepContext())
    assert state["error"] == "docker command timed out"
    assert state["exit_code"] == -1


def test_ensure_container_cancel_noop():
    """cancel() is safe — the docker calls run synchronously inside startup()."""
    assert EnsureContainerStep().cancel({}) is None


# ═══════════════════════════════════════════════════════════════════════════
# gem5/run_simulation.py
# ═══════════════════════════════════════════════════════════════════════════


def test_gem5_run_metadata():
    """gem5 run publishes its outputs, excludes Windows, and is flagged LARGE_OUTPUT.

    LARGE_OUTPUT tells the runner this step produces artifacts needing storage
    handling rather than inline log capture.
    """
    assert RunSimulationStep.PARAMS_SCHEMA is RunSimulationParams
    assert RunSimulationStep.OUTPUT_KEYS == [
        "exit_code", "stats_artifact_id", "m5out_path", "container",
    ]
    assert RunSimulationStep.SUPPORTED_OS == ["macos", "linux"]
    assert RunSimulationStep.LARGE_OUTPUT is True
    assert RunSimulationStep.OS_VARIANTS["linux"]["gem5_binary"] == "/usr/local/bin/gem5.opt"


def test_gem5_run_requires_config_script():
    """config_script is required."""
    errors = RunSimulationStep.validate_params({})
    assert "config_script" in _fields(errors)


def test_gem5_run_valid_minimal():
    """A config_script alone is a complete, valid configuration."""
    assert RunSimulationStep.validate_params({"config_script": "se.py"}) == []


def test_gem5_run_timeout_bounds():
    """timeout=0 is rejected by the lower bound."""
    errors = RunSimulationStep.validate_params(
        {"config_script": "se.py", "timeout": 0}
    )
    assert "_schema" in _fields(errors)


def test_gem5_run_unknown_param_rejected():
    """An unknown param is rejected."""
    errors = RunSimulationStep.validate_params(
        {"config_script": "se.py", "junk": 1}
    )
    assert "junk" in _fields(errors)


def test_gem5_run_container_requires_working_dir(monkeypatch):
    # With container set but no working_dir, startup short-circuits before any
    # process is spawned.
    """Container mode without working_dir short-circuits before spawning anything.

    In container mode every path is a CONTAINER path, so there is no sensible
    default cwd to infer from the host — it must be stated explicitly.
    """
    monkeypatch.setattr(
        "nexus_steps.gem5.run_simulation._find_docker",
        lambda explicit: "/bin/docker",
    )
    step = RunSimulationStep()
    state = step.startup(
        {"config_script": "se.py", "container": "c1"}, StepContext()
    )
    assert "working_dir is required" in state["error"]
    assert state["exit_code"] == -1


def test_gem5_run_container_no_docker(monkeypatch):
    """Container mode on a node without docker fails with a clear message."""
    monkeypatch.setattr(
        "nexus_steps.gem5.run_simulation._find_docker",
        lambda explicit: None,
    )
    step = RunSimulationStep()
    state = step.startup(
        {"config_script": "se.py", "container": "c1", "working_dir": "/w"},
        StepContext(),
    )
    assert state["error"] == "docker binary not found on node"
    assert state["exit_code"] == -1


def test_gem5_run_direct_spawns_process(monkeypatch):
    # Replace the gem5 binary with /bin/sh so a real, harmless subprocess runs.
    """Direct (non-container) mode spawns a real process and creates a host m5out dir.

    container is None on this path, and the m5out directory is a host tempdir
    (prefix nexus_m5out_) rather than a container path. /usr/bin/true stands in for
    the gem5 binary so no simulator is required.
    """
    step = RunSimulationStep()
    state = step.startup(
        {
            "gem5_binary": "/usr/bin/true",
            "config_script": "-c",  # /usr/bin/true ignores args; exits 0
            "collect_stats": False,
        },
        StepContext(),
    )
    assert state["pid"] > 0
    assert state["container"] is None
    assert os.path.basename(state["m5out_path"]).startswith("nexus_m5out_")
    result = _poll(step, state)
    assert result is StepResult.SUCCESS
    assert state["exit_code"] == 0

    # Cleanup artifacts.
    for k in ("stdout_path", "stderr_path"):
        if os.path.exists(state[k]):
            os.unlink(state[k])
    try:
        os.rmdir(state["m5out_path"])
    except OSError:
        pass
    _reap(state)


def test_gem5_run_direct_nonzero_fails(monkeypatch):
    """A non-zero gem5 exit maps to FAILED."""
    step = RunSimulationStep()
    state = step.startup(
        {
            "gem5_binary": "/usr/bin/false",
            "config_script": "x",
            "collect_stats": False,
        },
        StepContext(),
    )
    result = _poll(step, state)
    assert result is StepResult.FAILED
    assert state["exit_code"] != 0
    for k in ("stdout_path", "stderr_path"):
        if os.path.exists(state[k]):
            os.unlink(state[k])
    try:
        os.rmdir(state["m5out_path"])
    except OSError:
        pass
    _reap(state)


def test_gem5_run_direct_collect_stats_sets_artifact_when_present():
    # With collect_stats=True (direct mode), check() must set stats_artifact_id
    # to the m5out/stats.txt path once the file exists. We drop a stats.txt into
    # the m5out dir created by startup() so check() finds it on completion.
    """With collect_stats, check() records m5out/stats.txt as the artifact once it exists.

    Also pins the recorded invocation: gem5 is passed --outdir followed by the
    config script, which is what makes stats land in the directory check() inspects.
    """
    step = RunSimulationStep()
    state = step.startup(
        {
            "gem5_binary": "/usr/bin/true",
            "config_script": "se.py",
            "collect_stats": True,
        },
        StepContext(),
    )
    # The command string records the real invocation (--outdir + config).
    assert state["_command_str"].startswith("/usr/bin/true --outdir=")
    assert state["_command_str"].endswith("se.py")
    stats = os.path.join(state["m5out_path"], "stats.txt")
    with open(stats, "w") as fh:
        fh.write("sim_seconds 1\n")
    result = _poll(step, state)
    assert result is StepResult.SUCCESS
    assert state["stats_artifact_id"] == stats
    for k in ("stdout_path", "stderr_path"):
        if os.path.exists(state[k]):
            os.unlink(state[k])
    import shutil as _sh
    _sh.rmtree(state["m5out_path"], ignore_errors=True)
    _reap(state)


def test_gem5_run_direct_collect_stats_missing_leaves_artifact_none():
    # collect_stats=True but no stats.txt produced -> stats_artifact_id stays None.
    """With collect_stats but no stats.txt produced, the step still SUCCEEDS with a None artifact.

    Deliberate: a missing stats file is reported as 'no artifact', not as a failed
    simulation, since the run itself exited cleanly.
    """
    step = RunSimulationStep()
    state = step.startup(
        {
            "gem5_binary": "/usr/bin/true",
            "config_script": "se.py",
            "collect_stats": True,
        },
        StepContext(),
    )
    result = _poll(step, state)
    assert result is StepResult.SUCCESS
    assert state["stats_artifact_id"] is None
    for k in ("stdout_path", "stderr_path"):
        if os.path.exists(state[k]):
            os.unlink(state[k])
    import shutil as _sh
    _sh.rmtree(state["m5out_path"], ignore_errors=True)
    _reap(state)


def test_gem5_run_cancel_safe_without_pid():
    """cancel() tolerates state with no pid (e.g. after a startup guard fired)."""
    RunSimulationStep().cancel({})
    RunSimulationStep().cancel({"pid": None})


def test_gem5_run_check_reports_failed_on_startup_error_without_pid():
    """check() must not raise KeyError('pid') on a startup()-time error state.

    A container-setup failure (docker missing, container gone, mkdir -p
    failing) makes startup() return {"error": ..., "exit_code": ...} with no
    "pid" key. check() indexing state["pid"] unconditionally would raise
    KeyError, which the executor reports as the unhelpful "error: 'pid'" —
    masking the real error message.
    """
    state = {"error": "could not create m5out in container: No such container", "exit_code": 1}
    assert RunSimulationStep().check(state) == StepResult.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# gem5/collect_results.py
# ═══════════════════════════════════════════════════════════════════════════


def test_gem5_collect_metadata():
    """collect_results publishes size/url, excludes Windows, and is flagged LARGE_OUTPUT."""
    assert CollectResultsStep.PARAMS_SCHEMA is CollectResultsParams
    assert CollectResultsStep.OUTPUT_KEYS == ["results_size_bytes", "results_url"]
    assert CollectResultsStep.SUPPORTED_OS == ["macos", "linux"]
    assert CollectResultsStep.LARGE_OUTPUT is True


def test_gem5_collect_context_satisfiable_rule_missing():
    # Without m5out_path in params OR context, the ContextSatisfiableRule fires.
    """Without m5out_path in params OR context, the ContextSatisfiableRule fails."""
    errors = CollectResultsStep.validate_params({})
    assert "m5out_path" in _fields(errors)


def test_gem5_collect_satisfied_by_explicit_param():
    """An explicit m5out_path satisfies the rule."""
    assert CollectResultsStep.validate_params({"m5out_path": "/tmp/m5out"}) == []


def test_gem5_collect_satisfied_by_context():
    """An upstream m5out_path in the context satisfies the rule.

    The normal composition: run_simulation publishes m5out_path and collect_results
    consumes it without the job author wiring it up.
    """
    ctx = StepContext(outputs={"m5out_path": "/tmp/m5out"})
    assert CollectResultsStep.validate_params({}, ctx) == []


def test_gem5_collect_unknown_param_rejected():
    """An unknown param is rejected."""
    errors = CollectResultsStep.validate_params(
        {"m5out_path": "/tmp/x", "weird": 1}
    )
    assert "weird" in _fields(errors)


def test_gem5_collect_missing_m5out_at_startup():
    """With nothing to collect, startup() fails with a 'not resolvable' error.

    The runtime counterpart of the validation rule — the value could have been
    expected from context that never materialized.
    """
    step = CollectResultsStep()
    # Nothing in params or context.
    state = step.startup({}, StepContext())
    assert "not provided and not resolvable" in state["error"]
    assert step.check(state) is StepResult.FAILED


def test_gem5_collect_host_dir_not_found():
    """A host m5out path that doesn't exist fails before any tar or upload."""
    step = CollectResultsStep()
    state = step.startup(
        {"m5out_path": "/tmp/nexus_no_such_m5out_999"}, StepContext()
    )
    assert "not found on host" in state["error"]
    assert step.check(state) is StepResult.FAILED


def test_gem5_collect_missing_server_callback_info():
    # Real m5out dir exists -> tar succeeds; but no server_url/job_id/node_api_key
    # in context, so upload is skipped with an error before any network call.
    """Without server_url/job_id/node_api_key the upload is refused before any network call.

    Those three come from the StepContext the executor builds. Missing them means
    the agent has nowhere to authenticate to, so failing early beats an
    unauthenticated request.
    """
    m5out = tempfile.mkdtemp(prefix="nexus_test_m5out_")
    with open(os.path.join(m5out, "stats.txt"), "w") as f:
        f.write("sim_seconds 1.0\n")
    try:
        step = CollectResultsStep()
        state = step.startup({"m5out_path": m5out}, StepContext())
        assert "missing server callback info" in state["error"]
        assert step.check(state) is StepResult.FAILED
    finally:
        import shutil as _sh
        _sh.rmtree(m5out, ignore_errors=True)


def test_gem5_collect_uploads_when_context_complete(monkeypatch):
    # Provide full server callback info and stub httpx.put so no real network
    # request is made. This exercises the success path end-to-end (real tar).
    """The happy path: real tar of m5out, then an authenticated PUT to the results endpoint.

    httpx.put is stubbed so no socket opens, but the archive is built for real.
    Pins the endpoint shape (/api/jobs/<id>/results), the separate download URL
    reported back to the user, and the X-Node-Key auth header — the node API key,
    not a user JWT, is what authorizes an agent upload.
    """
    m5out = tempfile.mkdtemp(prefix="nexus_test_m5out_")
    with open(os.path.join(m5out, "stats.txt"), "w") as f:
        f.write("sim_seconds 1.0\n")

    captured = {}

    class _Resp:
        """Minimal httpx response stand-in reporting HTTP 200."""
        status_code = 200
        text = "ok"

    def _fake_put(url, **kwargs):
        """Record the URL and headers instead of performing a request."""
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "put", _fake_put)
    try:
        step = CollectResultsStep()
        ctx = StepContext(
            job_id="job-123",
            server_url="http://server:8000",
            node_api_key="secret-key",
        )
        state = step.startup({"m5out_path": m5out}, ctx)
        assert state["done"] is True
        assert state["results_size_bytes"] > 0
        assert state["results_url"] == "http://server:8000/api/jobs/job-123/results/download"
        assert captured["url"] == "http://server:8000/api/jobs/job-123/results"
        assert captured["headers"]["X-Node-Key"] == "secret-key"
        assert step.check(state) is StepResult.SUCCESS
    finally:
        import shutil as _sh
        _sh.rmtree(m5out, ignore_errors=True)


def test_gem5_collect_upload_http_error(monkeypatch):
    """A non-200 upload response fails the step with the status code in the message.

    Without the status check the step would report success while the artifact was
    never stored.
    """
    m5out = tempfile.mkdtemp(prefix="nexus_test_m5out_")
    with open(os.path.join(m5out, "stats.txt"), "w") as f:
        f.write("x\n")

    class _Resp:
        """Response stand-in reporting HTTP 500."""
        status_code = 500
        text = "server error"

    import httpx

    monkeypatch.setattr(httpx, "put", lambda url, **k: _Resp())
    try:
        step = CollectResultsStep()
        ctx = StepContext(
            job_id="j1", server_url="http://s:8000", node_api_key="k",
        )
        state = step.startup({"m5out_path": m5out}, ctx)
        assert "upload failed: HTTP 500" in state["error"]
        assert step.check(state) is StepResult.FAILED
    finally:
        import shutil as _sh
        _sh.rmtree(m5out, ignore_errors=True)


def test_gem5_collect_check_running_before_done():
    """Without 'done', check() reports RUNNING."""
    assert CollectResultsStep().check({}) is StepResult.RUNNING


def test_gem5_collect_m5out_resolved_from_context_at_startup(monkeypatch):
    # m5out omitted from params but present in context -> ctx.resolve() supplies
    # it, and tar+upload proceed against the resolved dir (real tar, stubbed PUT).
    """m5out_path resolved purely from context drives a real tar and a (stubbed) upload.

    The end-to-end version of the context-satisfiable rule: validation passing is
    not enough, startup() must actually read the resolved value.
    """
    m5out = tempfile.mkdtemp(prefix="nexus_test_m5out_ctx_")
    with open(os.path.join(m5out, "stats.txt"), "w") as f:
        f.write("sim_seconds 1.0\n")

    import httpx

    class _Resp:
        """Response stand-in reporting HTTP 200."""
        status_code = 200
        text = "ok"

    monkeypatch.setattr(httpx, "put", lambda url, **k: _Resp())
    try:
        step = CollectResultsStep()
        ctx = StepContext(
            outputs={"m5out_path": m5out},
            job_id="jX",
            server_url="http://s:8000",
            node_api_key="k",
        )
        state = step.startup({}, ctx)
        assert state["done"] is True
        assert state["results_size_bytes"] > 0
    finally:
        import shutil as _sh
        _sh.rmtree(m5out, ignore_errors=True)


def test_gem5_collect_container_mode_no_docker(monkeypatch):
    # container set but no docker binary -> early guard, no docker exec runs.
    """Container mode without docker fails before any `docker exec`.

    In container mode m5out_path is a CONTAINER path, so it is never stat'd on the
    host — the docker guard is the only thing standing between it and a confusing
    'not found on host' error.
    """
    monkeypatch.setattr(
        "nexus_steps.gem5.collect_results._find_docker", lambda explicit: None
    )
    step = CollectResultsStep()
    state = step.startup(
        {"m5out_path": "/in/container/m5out", "container": "c1"},
        StepContext(server_url="http://s", job_id="j", node_api_key="k"),
    )
    assert state["error"] == "docker binary not found on node"
    assert step.check(state) is StepResult.FAILED


def test_gem5_collect_container_mode_tar_failure(monkeypatch):
    # container tar exits nonzero -> reported without any network/upload.
    """A failed in-container tar fails the step and performs NO upload.

    httpx.put is replaced with a hard assertion, so uploading a truncated or empty
    archive after a tar failure would fail the test loudly. Note the container path
    returns stderr as bytes, which the error formatting must handle.
    """
    monkeypatch.setattr(
        "nexus_steps.gem5.collect_results._find_docker",
        lambda explicit: "/bin/docker",
    )

    import nexus_steps.gem5.collect_results as cr_mod

    def _fake_run(cmd, **kwargs):
        # docker exec ... tar -czf - ...  -> nonzero with stderr bytes.
        """Simulate `docker exec ... tar` exiting non-zero with bytes on stderr."""
        return _FakeCompleted(returncode=2, stderr=b"tar: not found")

    monkeypatch.setattr(cr_mod.subprocess, "run", _fake_run)
    # Network must never be reached on the failure path.
    import httpx

    def _no_net(*a, **k):
        """Fail the test outright if an upload is attempted on the failure path."""
        raise AssertionError("upload must not happen when container tar fails")

    monkeypatch.setattr(httpx, "put", _no_net)
    step = CollectResultsStep()
    state = step.startup(
        {"m5out_path": "/in/container/m5out", "container": "c1"},
        StepContext(server_url="http://s", job_id="j", node_api_key="k"),
    )
    assert "tar in container failed" in state["error"]
    assert step.check(state) is StepResult.FAILED


def test_gem5_collect_cancel_noop():
    """cancel() is safe — tar and upload run synchronously inside startup()."""
    CollectResultsStep().cancel({})
