"""OS-aware package installation step.

Uses the platform-native package manager by default (Homebrew on macOS,
apt on Linux, Chocolatey on Windows) but allows an explicit override.

Where this fits
---------------
Registered as ``"package_install"``. Intended as a provisioning step near the
start of a job so later steps can assume their tools exist (``git`` for
``git_clone``, build deps for a gem5 run, and so on).

Execution model
---------------
Fully synchronous: the install runs to completion inside ``startup()``,
``check()`` merely reports the stored outcome, and ``cancel()`` is a no-op.
A package install can therefore block the agent's step coroutine for up to the
hard 600 s timeout.

AI Note: the chosen manager is threaded through a *pseudo-parameter*
``_package_manager`` rather than a real schema field — see the note on
``OS_VARIANTS`` below. That indirection is the least obvious thing in this
file.
"""

from __future__ import annotations

import subprocess
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class PackageInstallParams(BaseModel):
    """Parameters for the package_install step.

    Note there is deliberately no ``_package_manager`` field here; the OS-chosen
    manager arrives as an extra key that Pydantic ignores (see
    :attr:`PackageInstallStep.OS_VARIANTS`). The ``description``/``examples``
    text is user-facing via ``to_schema()`` → ``/api/steps``.
    """

    packages: list[str] = Field(
        ...,
        description="List of package names to install.",
        min_length=1,
        examples=[["git", "curl", "jq"]],
    )
    package_manager_override: str | None = Field(
        None,
        description=(
            "Override the default package manager command "
            "(e.g., 'dnf' instead of 'apt')."
        ),
    )


# ── Helpers ──────────────────────────────────────────────────────────────


# Exported-by-convention lookup of manager name → non-interactive install argv
# prefix. Packages are appended to whichever prefix is selected.
#
# AI Note: every entry must be non-interactive — "-y" for apt/choco, and brew
# needs no flag. A prompting install would hang until the 600 s timeout, since
# no stdin is attached. The apt entry hardcodes ``sudo``, so a Linux node's
# agent user must have passwordless sudo for apt-get or this step fails with a
# password prompt / permission error.
_INSTALL_COMMANDS: dict[str, list[str]] = {
    "brew": ["brew", "install"],
    "apt": ["sudo", "apt-get", "install", "-y"],
    "choco": ["choco", "install", "-y"],
}


def _build_install_cmd(
    manager: str, packages: list[str],
) -> list[str]:
    """Build the full install command list for the given package manager.

    Args:
        manager: A key of :data:`_INSTALL_COMMANDS` (``brew``/``apt``/``choco``)
            or, for anything else, a bare executable name to be used directly.
        packages: Package names appended verbatim as separate argv entries.

    Returns:
        A complete argv list suitable for ``subprocess.run`` without a shell.

    AI Note: the unknown-manager fallback guesses ``[manager, "install", ...]``.
    That works for ``dnf``/``yum``/``pacman -S``-style tools only by luck and
    provides no non-interactive flag, so an override like ``"dnf"`` will prompt
    and then time out. Callers needing full control should use a
    ``run_command`` step instead of ``package_manager_override``.

    AI Note: ``list(base)`` copies the module-level template — mutating it in
    place would corrupt ``_INSTALL_COMMANDS`` for every subsequent call.
    """
    base = _INSTALL_COMMANDS.get(manager)
    if base is None:
        # Treat the manager string as a raw command prefix.
        return [manager, "install"] + packages
    return list(base) + packages


# ── Step ─────────────────────────────────────────────────────────────────


@register("package_install")
class PackageInstallStep(FlowStep):
    """Install system packages using the platform-native package manager.

    Security note: package names are passed as argv entries (no shell), so
    there is no injection vector through ``packages`` — but installing an
    attacker-chosen package is itself privileged code execution, and the apt
    path runs under ``sudo``.
    """

    PARAMS_SCHEMA = PackageInstallParams
    OUTPUT_KEYS = ["installed"]
    DESCRIPTION = "Install system packages (brew/apt/choco) in an OS-aware manner."

    # AI Note: ``_package_manager`` is NOT a field of PackageInstallParams. It
    # is injected into the params dict by ``FlowStep.resolve_for_os()`` on the
    # agent, survives into ``ctx.resolve()``'s merged dict, and is read there by
    # ``startup()`` via ``resolved.get(...)``. Pydantic silently drops it when
    # constructing the model (extra keys are ignored by default), which is why
    # this works without a schema entry — and also why the submit-time
    # unknown-param check never sees it: OS resolution happens after validation.
    # Adding a real ``_package_manager`` field would expose it in the UI palette.
    OS_VARIANTS = {
        "macos": {"_package_manager": "brew"},
        "linux": {"_package_manager": "apt"},
        "windows": {"_package_manager": "choco"},
    }

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Select a package manager and run the install to completion.

        Args:
            params: Raw step params, expected to already carry the OS-injected
                ``_package_manager`` key (added by ``resolve_for_os()``).
            ctx: Job context; ``ctx.resolve()`` layers upstream step outputs
                beneath these params.

        Returns:
            On success ``{"installed": [...], "done": True}``; on failure a
            dict with ``error`` and an empty ``installed`` list.

        Side effects:
            Runs a privileged package installation on the node (apt via
            ``sudo``), mutating system state outside the job's sandbox. There
            is no uninstall/rollback.

        Raises:
            pydantic.ValidationError: if ``packages`` is missing or empty.

        AI Note: ``installed`` reports the *requested* package list, not what
        the manager actually installed — already-present packages, transitively
        pulled dependencies and partial successes are all invisible here.
        Downstream steps must not treat it as ground truth.
        """
        resolved = ctx.resolve(params)
        validated = PackageInstallParams(**resolved)

        # Determine the package manager to use.
        #
        # AI Note: the "apt" fallback applies when OS resolution did not inject
        # ``_package_manager`` — an unrecognised OS string, or a direct
        # unit-test call that bypassed resolve_for_os(). On a macOS node that
        # somehow reached this branch it would try (and fail on) apt rather
        # than brew, so a missing-manager error here usually means OS detection
        # went wrong upstream, not that the node lacks the tool.
        if validated.package_manager_override:
            manager = validated.package_manager_override
        else:
            manager = resolved.get("_package_manager", "apt")

        cmd = _build_install_cmd(manager, validated.packages)

        try:
            # AI Note: no check=True here — the return code is inspected
            # explicitly below so the error message can include stderr. Hard
            # 600 s cap; not configurable via params, and a large install on a
            # slow link can legitimately exceed it.
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            # The manager binary itself is absent (e.g. brew not installed).
            return {
                "error": f"Package manager not found: {manager}",
                "installed": [],
            }
        except subprocess.TimeoutExpired:
            # AI Note: on timeout the install process is killed mid-flight, so
            # the node may be left with a partially-configured package database
            # (e.g. apt needing `dpkg --configure -a`). Nothing recovers that.
            return {
                "error": f"Package install timed out after 600s",
                "installed": [],
            }

        if result.returncode != 0:
            return {
                "error": (
                    f"Install failed (exit {result.returncode}): "
                    f"{result.stderr.strip()}"
                ),
                "installed": [],
            }

        return {
            "installed": validated.packages,
            "done": True,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        """Report the result already computed by ``startup()``. Idempotent.

        Args:
            state: The ``startup()`` state dict.

        Returns:
            ``FAILED`` when ``error`` is present, ``SUCCESS`` when ``done`` is
            set, else ``RUNNING``.

        AI Note: the trailing ``RUNNING`` is unreachable today; it is a safety
        default so a partially-restored state keeps polling instead of
        reporting a bogus success.
        """
        if "error" in state:
            return StepResult.FAILED
        if state.get("done"):
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        """No-op — the install already finished (or failed) inside ``startup()``.

        Args:
            state: Unused; accepted to satisfy the FlowStep interface.

        AI Note: interrupting a package manager mid-transaction is unsafe
        anyway, so having nothing to cancel here is arguably the right
        behaviour rather than a gap.
        """
        # Installation is synchronous in startup(); nothing to cancel.
        pass
