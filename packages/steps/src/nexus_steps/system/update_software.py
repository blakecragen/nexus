"""Update a node's own Nexus installation and restart its agent.

Pulls the latest commit from GitHub, reinstalls the editable packages, and
(by default) bounces the agent process so the new code actually takes effect
— a git pull alone does nothing for a process whose modules are already
loaded in memory.

Where this fits
----------------
Registered as ``"update_software"``. Runs on a node like any other remote
step (``REQUIRES_NODE`` stays at its ``True`` default). Mirrors the update
path ``nexus_deploy.py``'s ``INSTALL_SH`` takes when re-provisioning an
existing checkout (fetch + hard-checkout, then reinstall the same three
packages), so a node stays reproducible from GitHub rather than drifting
into an ad hoc local state.

Execution model
----------------
The git fetch/checkout and pip install run synchronously inside
``startup()``, same as ``git_pull``/``package_install`` — capped at a hard,
non-configurable timeout per phase. The restart, however, is deliberately
NOT synchronous: killing this process while it is still trying to report the
step's own result would race the WebSocket message that tells the server
the step succeeded. Instead, a detached, delayed restart is scheduled as a
fire-and-forget subprocess and this step returns immediately after
scheduling it.

AI Note: this step should generally be the last (or only) step in a job. Any
step dispatched to this node after the restart fires races the agent's
downtime — there is no coordination with the scheduler to prevent that.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Helpers ──────────────────────────────────────────────────────────────


def _detect_repo_dir() -> str | None:
    """Find the git repo root by walking up from this module's own file.

    Works because this package is normally editable-installed (``pip install
    -e``), so ``__file__`` resolves into the actual node checkout rather than
    a copied site-packages location — the same trick that lets ``sys.executable``
    double as "the venv this agent is already running from".

    Returns:
        The repo root path, or ``None`` if no ``.git`` directory is found in
        any ancestor.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").is_dir():
            return str(parent)
    return None


def _unhide_editable_pth(venv_python: str) -> None:
    """Best-effort clear of macOS's UF_HIDDEN flag on editable-install .pth shims.

    Current CPython's ``site.py`` silently SKIPS hidden ``.pth`` files (see
    ``site.addpackage``'s ``UF_HIDDEN`` check), and setuptools' editable-wheel
    writer has been observed leaving that flag set on the
    ``_editable_impl_*.pth`` files it generates — breaking every import of the
    packages just installed with no hint why (a bare ``ModuleNotFoundError``
    and nothing else). This is a defensive touch-up, not part of the actual
    update, so failures here are swallowed; Linux has no ``chflags`` at all,
    hence the platform check.

    AI Note: verified empirically on 3.11.14 as well as 3.14 — the UF_HIDDEN
    check was backported, so this is NOT a 3.13+-only concern. An earlier
    version of this docstring said "3.13+", which would tell a reader on 3.11
    they were safe when they are not.

    AI Note: the site-packages directory is obtained by ASKING the interpreter
    (``sysconfig``), never by walking up from ``venv_python``. A venv's
    ``bin/python`` is a symlink to the base interpreter, so the previous
    ``Path(venv_python).resolve().parent.parent / "lib"`` resolved straight out
    of the venv and globbed the base interpreter's lib directory instead —
    matching zero files, which meant this self-heal never ran on the very
    layout ``python -m venv`` produces. Reproduced: the glob returned [] while
    the real venv held 3 matching shims.
    """
    if sys.platform != "darwin":
        return
    try:
        # Ask the target interpreter where ITS site-packages actually is.
        purelib = subprocess.run(
            [venv_python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if not purelib:
            return
        for pth in Path(purelib).glob("_editable_impl_*.pth"):
            subprocess.run(["chflags", "nohidden", str(pth)], capture_output=True, timeout=10)
    except Exception:
        pass


# The editable-install targets a node actually needs (nexus_deploy.py's
# INSTALL_SH installs exactly these three; no server package on a node).
#
# AI Note: deliberately NOT a step param. A list-typed field default hits a
# real bug in FlowStep.to_schema()/the JobBuilder frontend: to_schema()
# exports a non-primitive default as a Python repr string (a display hint,
# per its own docstring/tests), but the frontend pre-fills the form with
# that string and submits it back verbatim — Pydantic then rejects it as
# "Input should be a valid list". There is also no real use case for
# reinstalling a different package set on an update, so hardcoding it here
# both sidesteps the bug and keeps this step configuration-free.
_UPDATE_PACKAGES = ["packages/common", "packages/steps", "packages/agent"]


def _schedule_restart(repo_dir: str, delay: float) -> None:
    """Fire-and-forget a delayed agent restart, detached from this process.

    The delay gives the executor time to send this step's completion message
    over the still-alive WebSocket connection before the process reporting it
    gets killed. The restart cascades launchd -> systemd --user -> nexusctl so
    it does the right thing whether the node was deployed with ``--service``
    or the nohup-background default — ``nexus_deploy.py``'s ``INSTALL_SH``
    performs the same three-way teardown when re-provisioning, and each
    attempt fails harmlessly (``2>/dev/null``) when that mode isn't the one
    in use.

    Side effects:
        Spawns a detached ``bash`` process that outlives this one.

    AI Note: ``start_new_session=True`` is what makes this survive the agent
    being killed — without it the restart script would be in the agent's
    process group and die alongside it before ever running.
    """
    script = (
        f"sleep {delay} && ("
        f'launchctl kickstart -k "gui/$(id -u)/com.nexus.agent" 2>/dev/null || '
        f"systemctl --user restart nexus-agent 2>/dev/null || "
        f"{shlex.quote(repo_dir)}/nexusctl restart)"
    )
    subprocess.Popen(
        ["bash", "-c", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ── Params ───────────────────────────────────────────────────────────────


class UpdateSoftwareParams(BaseModel):
    """Parameters for the update_software step.

    The ``description``/``examples`` text is user-facing via ``to_schema()``
    → ``/api/steps``.
    """

    repo_dir: str | None = Field(
        None,
        description=(
            "Path to the node's Nexus git checkout. Auto-detected from the "
            "running agent's own installation when omitted."
        ),
    )
    remote: str = Field(
        "origin",
        description="Git remote name.",
    )
    branch: str = Field(
        "main",
        description="Branch to update to.",
    )
    restart: bool = Field(
        True,
        description="Restart the agent process after a successful update so the new code takes effect.",
    )
    restart_delay: float = Field(
        3.0,
        description="Seconds to wait before restarting, so this step's result reaches the server first.",
        ge=0,
        le=60,
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("update_software")
class UpdateSoftwareStep(FlowStep):
    """Pull the latest code from GitHub, reinstall packages, and restart the agent.

    Security note: like ``git_pull``, this trusts the node's existing git
    remote configuration and runs whatever code is on the target branch —
    no different in kind from every other step here, which is why a node's
    api_key is already treated as equivalent to shell access on that host.
    """

    PARAMS_SCHEMA = UpdateSoftwareParams
    OUTPUT_KEYS = ["commit_sha", "updated", "restart_scheduled"]
    DESCRIPTION = "Pull the latest code from GitHub, reinstall packages, and restart the agent."

    SUPPORTED_OS = ["macos", "linux"]

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Fetch + hard-checkout the target branch, reinstall, then schedule a restart.

        Args:
            params: Raw step params.
            ctx: Job context; ``ctx.resolve()`` layers upstream step outputs
                beneath these params.

        Returns:
            On success ``{"commit_sha", "updated", "restart_scheduled",
            "done": True}``; on failure a dict with an ``error`` key and no
            ``done``.

        Side effects:
            Runs ``git fetch``/``checkout`` and ``pip install -e`` against
            ``repo_dir``, and — when ``restart`` is true and the update
            succeeded — spawns a detached process that will kill and restart
            this node's agent after ``restart_delay`` seconds.

        Raises:
            pydantic.ValidationError: if params fail schema validation.

        AI Note: unlike ``git_pull``'s plain ``git pull``, this does
        ``fetch`` + ``checkout -B <branch> <remote>/<branch>`` — a hard reset
        to match the remote, discarding any node-local commits on that
        branch. Deliberate: nodes are meant to be disposable clones of
        GitHub, not development checkouts, and an unattended merge is more
        likely to produce a silent surprise than a fast-forward would.
        """
        resolved = ctx.resolve(params)
        validated = UpdateSoftwareParams(**resolved)

        repo_dir = validated.repo_dir or _detect_repo_dir()
        if not repo_dir:
            return {"error": "could not auto-detect repo_dir (no .git ancestor found); pass it explicitly"}
        if not Path(repo_dir, ".git").is_dir():
            return {"error": f"not a git repository: {repo_dir}"}

        try:
            pre_sha = subprocess.run(
                ["git", "-C", repo_dir, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            return {"error": f"not a git repository: {repo_dir}"}

        try:
            subprocess.run(
                ["git", "-C", repo_dir, "fetch", "--depth", "1", validated.remote, validated.branch],
                check=True, capture_output=True, text=True, timeout=120,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "checkout", "-q", "-B", validated.branch,
                 f"{validated.remote}/{validated.branch}"],
                check=True, capture_output=True, text=True, timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            return {"error": f"git update failed: {exc.stderr.strip()}"}
        except subprocess.TimeoutExpired:
            return {"error": "git update timed out"}

        # AI Note: degrades to "unknown" instead of failing — the update
        # already succeeded, so this is metadata loss, not a job failure.
        try:
            post_sha = subprocess.run(
                ["git", "-C", repo_dir, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            post_sha = "unknown"

        # AI Note: sys.executable is THIS agent's own interpreter — reusing
        # it (rather than re-deriving a venv path) guarantees the reinstall
        # targets the exact environment that is about to be restarted.
        install_cmd = [sys.executable, "-m", "pip", "install", "-q"]
        for pkg in _UPDATE_PACKAGES:
            install_cmd += ["-e", pkg]
        try:
            result = subprocess.run(
                install_cmd, cwd=repo_dir, capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            return {"error": "pip install timed out after 600s", "commit_sha": post_sha,
                    "updated": pre_sha != post_sha}
        if result.returncode != 0:
            return {"error": f"pip install failed: {result.stderr.strip()}", "commit_sha": post_sha,
                    "updated": pre_sha != post_sha}

        _unhide_editable_pth(sys.executable)

        restart_scheduled = False
        if validated.restart:
            _schedule_restart(repo_dir, validated.restart_delay)
            restart_scheduled = True

        return {
            "commit_sha": post_sha,
            "updated": pre_sha != post_sha,
            "restart_scheduled": restart_scheduled,
            "done": True,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        """Report the result already computed by ``startup()``. Idempotent.

        Args:
            state: The ``startup()`` state dict.

        Returns:
            ``FAILED`` when ``error`` is present, ``SUCCESS`` when ``done`` is
            set, else ``RUNNING``.
        """
        if "error" in state:
            return StepResult.FAILED
        if state.get("done"):
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        """No-op — the update already finished (or failed) inside ``startup()``.

        Args:
            state: Unused; accepted to satisfy the FlowStep interface.

        AI Note: a cancel arriving after ``startup()`` returned cannot stop an
        already-scheduled restart — there is no handle to it here, only the
        detached process's own sleep timer.
        """
        pass
