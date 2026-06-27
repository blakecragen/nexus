"""OS-aware package installation step.

Uses the platform-native package manager by default (Homebrew on macOS,
apt on Linux, Chocolatey on Windows) but allows an explicit override.
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
    """Parameters for the package_install step."""

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


_INSTALL_COMMANDS: dict[str, list[str]] = {
    "brew": ["brew", "install"],
    "apt": ["sudo", "apt-get", "install", "-y"],
    "choco": ["choco", "install", "-y"],
}


def _build_install_cmd(
    manager: str, packages: list[str],
) -> list[str]:
    """Build the full install command list for the given package manager."""
    base = _INSTALL_COMMANDS.get(manager)
    if base is None:
        # Treat the manager string as a raw command prefix.
        return [manager, "install"] + packages
    return list(base) + packages


# ── Step ─────────────────────────────────────────────────────────────────


@register("package_install")
class PackageInstallStep(FlowStep):
    """Install system packages using the platform-native package manager."""

    PARAMS_SCHEMA = PackageInstallParams
    OUTPUT_KEYS = ["installed"]
    DESCRIPTION = "Install system packages (brew/apt/choco) in an OS-aware manner."

    OS_VARIANTS = {
        "macos": {"_package_manager": "brew"},
        "linux": {"_package_manager": "apt"},
        "windows": {"_package_manager": "choco"},
    }

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = PackageInstallParams(**resolved)

        # Determine the package manager to use.
        if validated.package_manager_override:
            manager = validated.package_manager_override
        else:
            manager = resolved.get("_package_manager", "apt")

        cmd = _build_install_cmd(manager, validated.packages)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            return {
                "error": f"Package manager not found: {manager}",
                "installed": [],
            }
        except subprocess.TimeoutExpired:
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
        if "error" in state:
            return StepResult.FAILED
        if state.get("done"):
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        # Installation is synchronous in startup(); nothing to cancel.
        pass
