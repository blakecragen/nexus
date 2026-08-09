"""Tests for the shell run_command and run_script steps.

These steps spawn REAL subprocesses. The test host is macOS, so we drive
everything through /bin/sh with trivial commands (exit 0, exit 3, sleep ...)
and poll check() to a terminal StepResult.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time

import pytest
from pydantic import ValidationError

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import StepContext
from nexus_steps.shell.run_command import RunCommandParams, RunCommandStep
from nexus_steps.shell.run_script import RunScriptParams, RunScriptStep


# ── Helpers ──────────────────────────────────────────────────────────────


def _poll_to_terminal(step, state, timeout=10.0, interval=0.02):
    """Poll check() until it leaves RUNNING or the timeout elapses.

    Returns the final StepResult. The check() contract is idempotent, so
    re-calling after a terminal result is harmless.
    """
    deadline = time.monotonic() + timeout
    result = step.check(state)
    while result is StepResult.RUNNING and time.monotonic() < deadline:
        time.sleep(interval)
        result = step.check(state)
    return result


def _reap(state):
    """Best-effort cleanup: ensure the spawned pid is reaped/terminated."""
    pid = state.get("pid")
    if not pid:
        return
    try:
        os.kill(pid, 0)
    except OSError:
        return  # already gone
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except OSError:
        pass


# ── RunCommandStep: success path ─────────────────────────────────────────


def test_run_command_exit_zero_succeeds():
    """A trivial exit-0 command reaches SUCCESS with exit_code 0.

    Also pins two contracts the agent's log capture relies on: ``_command_str``
    records the fully resolved invocation, and the default timeout is 3600s.
    """
    step = RunCommandStep()
    ctx = StepContext()
    state = step.startup({"command": "exit 0", "shell": "/bin/sh"}, ctx)

    assert "pid" in state and state["pid"] > 0
    # startup records the resolved invocation for later inspection/logging.
    assert state["_command_str"] == "/bin/sh -c 'exit 0'"
    assert state["timeout"] == 3600  # default
    result = _poll_to_terminal(step, state)

    assert result is StepResult.SUCCESS
    assert state["exit_code"] == 0

    # check() is idempotent once terminal: re-polling a reaped child returns
    # the already-stored exit code rather than RUNNING.
    again = step.check(state)
    assert again in (StepResult.SUCCESS, StepResult.FAILED)
    _reap(state)


def test_run_command_writes_stdout_and_stderr_files():
    """stdout and stderr are captured to separate temp files on disk.

    The agent's _capture() reads these paths to build the per-job log, so both
    files must exist and hold the right stream.
    """
    step = RunCommandStep()
    ctx = StepContext()
    state = step.startup(
        {"command": "echo out_marker; echo err_marker 1>&2", "shell": "/bin/sh"},
        ctx,
    )

    result = _poll_to_terminal(step, state)
    assert result is StepResult.SUCCESS

    # Temp files must exist and carry the captured streams.
    assert os.path.isfile(state["stdout_path"])
    assert os.path.isfile(state["stderr_path"])
    with open(state["stdout_path"]) as f:
        assert "out_marker" in f.read()
    with open(state["stderr_path"]) as f:
        assert "err_marker" in f.read()

    os.unlink(state["stdout_path"])
    os.unlink(state["stderr_path"])
    _reap(state)


def test_run_command_temp_files_use_expected_prefixes():
    """Capture files use the nexus_stdout_/nexus_stderr_ prefixes and .log suffix.

    Operators grep for these prefixes when reclaiming leaked temp files, so the
    naming is part of the operational contract.
    """
    step = RunCommandStep()
    state = step.startup({"command": "exit 0", "shell": "/bin/sh"}, StepContext())
    _poll_to_terminal(step, state)

    assert os.path.basename(state["stdout_path"]).startswith("nexus_stdout_")
    assert os.path.basename(state["stderr_path"]).startswith("nexus_stderr_")
    assert state["stdout_path"].endswith(".log")

    os.unlink(state["stdout_path"])
    os.unlink(state["stderr_path"])
    _reap(state)


# ── RunCommandStep: failure path ─────────────────────────────────────────


def test_run_command_nonzero_exit_fails_with_exit_code():
    """A non-zero exit maps to FAILED and the real exit code is preserved.

    The runner surfaces exit_code to the UI, so it must be the child's status, not
    a generic -1.
    """
    step = RunCommandStep()
    state = step.startup({"command": "exit 3", "shell": "/bin/sh"}, StepContext())

    result = _poll_to_terminal(step, state)

    assert result is StepResult.FAILED
    assert state["exit_code"] == 3
    _reap(state)


def test_run_command_running_before_completion():
    """A slow command must report RUNNING on the first poll."""
    step = RunCommandStep()
    state = step.startup({"command": "sleep 2", "shell": "/bin/sh"}, StepContext())

    # Immediately after startup the child should still be alive.
    assert step.check(state) is StepResult.RUNNING

    step.cancel(state)
    _poll_to_terminal(step, state, timeout=5.0)
    _reap(state)


def test_run_command_check_handles_already_reaped_child():
    """If the child was reaped elsewhere, os.waitpid raises ChildProcessError;
    check() must swallow it and report a terminal (FAILED) result instead of
    crashing the agent's poll loop."""
    step = RunCommandStep()
    state = step.startup({"command": "exit 0", "shell": "/bin/sh"}, StepContext())
    pid = state["pid"]

    # Reap the child out from under the step to force ChildProcessError on the
    # next os.waitpid() inside check().
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            break
        time.sleep(0.02)
    else:  # pragma: no cover - safety net
        pytest.fail("child never exited so could not be pre-reaped")

    # The pid is now unknown to this process; check() hits ChildProcessError.
    result = step.check(state)
    assert result is StepResult.FAILED
    assert state["exit_code"] == -1
    # state no longer references a live pid for stdout/stderr cleanup
    for key in ("stdout_path", "stderr_path"):
        if os.path.isfile(state[key]):
            os.unlink(state[key])


# ── RunCommandStep: cancel path ──────────────────────────────────────────


def test_run_command_cancel_terminates_long_running_process():
    """cancel() actually kills a long-running child (SIGTERM to the process group).

    Without the process-group signal a `sleep 30` under `sh -c` would survive job
    cancellation and leak a runaway process on the node.
    """
    step = RunCommandStep()
    state = step.startup({"command": "sleep 30", "shell": "/bin/sh"}, StepContext())
    pid = state["pid"]

    assert step.check(state) is StepResult.RUNNING

    step.cancel(state)

    # After SIGTERM to the process group, the child must die promptly and
    # check() must leave RUNNING.
    result = _poll_to_terminal(step, state, timeout=5.0)
    assert result is not StepResult.RUNNING

    # The pid should no longer be a live, signalable process.
    # AI Note: the raises() wraps the WHOLE polling loop, not one kill. The loop
    # spins until os.kill raises ESRCH (process gone) — that raise is the pass
    # condition. If the process is still alive after 3s the loop exits normally
    # and pytest.raises fails the test. Deliberate: exit-vs-reap is racy, so a
    # single os.kill right after cancel() would be flaky.
    with pytest.raises(OSError):
        # Loop a moment in case the kernel hasn't reaped it yet.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            os.kill(pid, 0)
            time.sleep(0.02)
    _reap(state)


def test_run_command_cancel_is_safe_with_no_pid():
    """cancel() on state lacking a pid must not raise."""
    step = RunCommandStep()
    step.cancel({})  # no pid key
    step.cancel({"pid": None})


# ── RunCommandStep: context resolution / OS variants ─────────────────────


def test_run_command_working_dir_is_respected():
    """The working_dir param becomes the child's cwd.

    Steps chained after a git_clone rely on this to run inside the checkout.
    """
    step = RunCommandStep()
    tmpdir = tempfile.mkdtemp(prefix="nexus_cwd_")
    state = step.startup(
        {"command": "pwd", "working_dir": tmpdir, "shell": "/bin/sh"},
        StepContext(),
    )
    result = _poll_to_terminal(step, state)
    assert result is StepResult.SUCCESS

    with open(state["stdout_path"]) as f:
        printed = f.read().strip()
    # macOS /tmp is a symlink to /private/tmp; compare resolved paths.
    assert os.path.realpath(printed) == os.path.realpath(tmpdir)

    os.unlink(state["stdout_path"])
    os.unlink(state["stderr_path"])
    _reap(state)


def test_run_command_context_outputs_resolve_into_params():
    """Context outputs should feed startup via ctx.resolve()."""
    step = RunCommandStep()
    ctx = StepContext(outputs={"command": "exit 0", "shell": "/bin/sh"})
    state = step.startup({"command": None, "shell": None}, ctx)
    result = _poll_to_terminal(step, state)
    assert result is StepResult.SUCCESS
    assert state["exit_code"] == 0
    _reap(state)


def test_run_command_metadata_attributes():
    """Class metadata the scheduler/UI depend on is pinned.

    OUTPUT_KEYS drives what lands in the downstream StepContext; the OS_VARIANTS
    shells decide which interpreter a command runs under per platform.
    """
    assert RunCommandStep.OUTPUT_KEYS == ["exit_code", "stdout_path", "stderr_path"]
    assert RunCommandStep.PARAMS_SCHEMA is RunCommandParams
    assert RunCommandStep.OS_VARIANTS["macos"]["shell"] == "/bin/zsh"
    assert RunCommandStep.OS_VARIANTS["linux"]["shell"] == "/bin/bash"


def test_run_command_params_validation_rejects_bad_timeout():
    # timeout has ge=1, le=86400 bounds.
    """timeout is bounded to [1, 86400] inclusive.

    Zero/negative would mean an instantly-expiring step; unbounded would let one
    job pin a node forever.
    """
    with pytest.raises(ValidationError):
        RunCommandParams(command="echo hi", timeout=0)
    with pytest.raises(ValidationError):
        RunCommandParams(command="echo hi", timeout=999999)
    # Boundary values are accepted.
    assert RunCommandParams(command="echo hi", timeout=1).timeout == 1
    assert RunCommandParams(command="echo hi", timeout=86400).timeout == 86400


def test_run_command_params_requires_command():
    # command is a required field with no default.
    """``command`` has no default, so omitting it is a validation error.

    Catches an empty step config at submit time instead of spawning `sh -c ''`.
    """
    with pytest.raises(ValidationError):
        RunCommandParams(shell="/bin/sh")


# ── RunScriptStep: success path ──────────────────────────────────────────


def _write_script(body: str) -> str:
    """Write ``body`` to a fresh temp .sh file and return its path.

    The caller owns the file and must unlink it. The file is created WITHOUT the
    execute bit in some tests on purpose so startup()'s chmod can be observed.
    """
    fd, path = tempfile.mkstemp(prefix="nexus_test_script_", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(body)
    return path


def test_run_script_exit_zero_succeeds():
    """A script exiting 0 reaches SUCCESS with exit_code 0."""
    path = _write_script("#!/bin/sh\nexit 0\n")
    try:
        step = RunScriptStep()
        state = step.startup({"script_path": path}, StepContext())
        assert "pid" in state
        result = _poll_to_terminal(step, state)
        assert result is StepResult.SUCCESS
        assert state["exit_code"] == 0
        _reap(state)
    finally:
        os.unlink(path)


def test_run_script_temp_files_use_script_prefixes():
    """RunScriptStep uses its own distinct temp-file prefixes (not the
    run_command ones), so output files are attributable to the script step."""
    path = _write_script("#!/bin/sh\necho hi\nexit 0\n")
    try:
        step = RunScriptStep()
        state = step.startup({"script_path": path}, StepContext())
        _poll_to_terminal(step, state)
        assert os.path.basename(state["stdout_path"]).startswith("nexus_script_out_")
        assert os.path.basename(state["stderr_path"]).startswith("nexus_script_err_")
        os.unlink(state["stdout_path"])
        os.unlink(state["stderr_path"])
        _reap(state)
    finally:
        os.unlink(path)


def test_run_script_defaults_working_dir_to_script_parent():
    """With no working_dir, the script runs in its own parent directory."""
    scriptdir = tempfile.mkdtemp(prefix="nexus_scriptdir_")
    path = os.path.join(scriptdir, "show_pwd.sh")
    with open(path, "w") as f:
        f.write("#!/bin/sh\npwd\nexit 0\n")
    try:
        step = RunScriptStep()
        state = step.startup({"script_path": path}, StepContext())
        result = _poll_to_terminal(step, state)
        assert result is StepResult.SUCCESS
        with open(state["stdout_path"]) as f:
            printed = f.read().strip()
        assert os.path.realpath(printed) == os.path.realpath(scriptdir)
        os.unlink(state["stdout_path"])
        os.unlink(state["stderr_path"])
        _reap(state)
    finally:
        os.unlink(path)
        os.rmdir(scriptdir)


def test_run_script_makes_file_executable():
    """startup() adds the owner execute bit before spawning the script.

    Scripts fetched by an upstream step (git clone, artifact download) frequently
    arrive non-executable; without this chmod the step would fail with EACCES.
    """
    path = _write_script("#!/bin/sh\nexit 0\n")
    try:
        # Strip exec bits so we can prove startup() re-adds them.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        assert not (os.stat(path).st_mode & stat.S_IXUSR)

        step = RunScriptStep()
        state = step.startup({"script_path": path}, StepContext())

        assert os.stat(path).st_mode & stat.S_IXUSR
        _poll_to_terminal(step, state)
        _reap(state)
    finally:
        os.unlink(path)


def test_run_script_passes_args_to_script():
    """The args list is forwarded as positional $1..$N to the script.

    Args must be passed as a real argv list (not string-concatenated), which is
    also what keeps values with spaces from being re-split.
    """
    path = _write_script('#!/bin/sh\necho "got:$1:$2"\nexit 0\n')
    try:
        step = RunScriptStep()
        state = step.startup(
            {"script_path": path, "args": ["alpha", "beta"]}, StepContext()
        )
        result = _poll_to_terminal(step, state)
        assert result is StepResult.SUCCESS
        with open(state["stdout_path"]) as f:
            assert "got:alpha:beta" in f.read()
        os.unlink(state["stdout_path"])
        os.unlink(state["stderr_path"])
        _reap(state)
    finally:
        os.unlink(path)


# ── RunScriptStep: failure path ──────────────────────────────────────────


def test_run_script_nonzero_exit_fails():
    """A script exiting non-zero maps to FAILED with the true exit code."""
    path = _write_script("#!/bin/sh\nexit 7\n")
    try:
        step = RunScriptStep()
        state = step.startup({"script_path": path}, StepContext())
        result = _poll_to_terminal(step, state)
        assert result is StepResult.FAILED
        assert state["exit_code"] == 7
        _reap(state)
    finally:
        os.unlink(path)


def test_run_script_missing_file_fails_at_startup():
    """A missing script_path short-circuits in startup() without spawning anything.

    startup() records an error sentinel plus exit_code -1 and leaves 'pid' unset;
    check() must then report FAILED without dereferencing a pid (which would raise
    and kill the agent poll loop).
    """
    step = RunScriptStep()
    state = step.startup(
        {"script_path": "/tmp/nexus_does_not_exist_12345.sh"}, StepContext()
    )
    # startup short-circuits with an error sentinel + exit_code -1.
    assert "error" in state
    assert "not found" in state["error"].lower()
    assert state["exit_code"] == -1
    # No process was spawned.
    assert "pid" not in state
    # check() must turn that into FAILED without touching a pid.
    assert step.check(state) is StepResult.FAILED


# ── RunScriptStep: cancel path ───────────────────────────────────────────


def test_run_script_cancel_terminates_long_running_script():
    """cancel() kills a long-running script and its children."""
    path = _write_script("#!/bin/sh\nsleep 30\n")
    try:
        step = RunScriptStep()
        state = step.startup({"script_path": path}, StepContext())
        pid = state["pid"]
        assert step.check(state) is StepResult.RUNNING

        step.cancel(state)
        result = _poll_to_terminal(step, state, timeout=5.0)
        assert result is not StepResult.RUNNING

        # AI Note: same pattern as the run_command cancel test — the loop is
        # inside raises() so the ESRCH from os.kill is the pass condition.
        with pytest.raises(OSError):
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                os.kill(pid, 0)
                time.sleep(0.02)
        _reap(state)
    finally:
        os.unlink(path)


def test_run_script_cancel_safe_with_no_pid():
    """cancel() tolerates state without a live pid.

    The runner may cancel a step that failed during startup, so this must not
    raise KeyError/TypeError.
    """
    step = RunScriptStep()
    step.cancel({})
    step.cancel({"pid": None})


def test_run_script_metadata_attributes():
    """RunScriptStep exports only exit_code and uses its own params schema."""
    assert RunScriptStep.OUTPUT_KEYS == ["exit_code"]
    assert RunScriptStep.PARAMS_SCHEMA is RunScriptParams


def test_run_script_params_defaults_and_bounds():
    # args defaults to an empty list; timeout default is 3600.
    """args defaults to [], timeout defaults to 3600 and is bounded to [1, 86400].

    Also confirms script_path is required — the same fail-fast guarantee as
    run_command's ``command``.
    """
    p = RunScriptParams(script_path="/tmp/x.sh")
    assert p.args == []
    assert p.timeout == 3600
    # script_path is required.
    with pytest.raises(ValidationError):
        RunScriptParams(args=["a"])
    # timeout bounds (ge=1, le=86400) are enforced.
    with pytest.raises(ValidationError):
        RunScriptParams(script_path="/tmp/x.sh", timeout=0)
    with pytest.raises(ValidationError):
        RunScriptParams(script_path="/tmp/x.sh", timeout=100000)
