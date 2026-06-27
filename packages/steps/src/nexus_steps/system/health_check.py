"""Basic node health check step.

Runs lightweight probes for CPU, memory, disk, and network and returns a
structured health report.  Designed to be the first step in a job to
verify the node is in good shape before heavier workloads.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import time
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


VALID_CHECKS = {"cpu", "memory", "disk", "network"}


class HealthCheckParams(BaseModel):
    """Parameters for the health_check step."""

    checks: list[str] = Field(
        default=["cpu", "memory", "disk", "network"],
        description=(
            "List of health checks to run. "
            "Valid values: cpu, memory, disk, network."
        ),
        examples=[["cpu", "memory"], ["disk"]],
    )


# ── Probe Helpers ────────────────────────────────────────────────────────


def _check_cpu() -> dict[str, Any]:
    """Basic CPU probe: count and 1-minute load average."""
    cpu_count = os.cpu_count() or 0
    try:
        load_1, load_5, load_15 = os.getloadavg()
    except (OSError, AttributeError):
        # Windows doesn't support getloadavg.
        load_1 = load_5 = load_15 = -1.0
    return {
        "status": "ok",
        "cpu_count": cpu_count,
        "load_1m": round(load_1, 2),
        "load_5m": round(load_5, 2),
        "load_15m": round(load_15, 2),
        "arch": platform.machine(),
    }


def _check_memory() -> dict[str, Any]:
    """Memory probe using /proc/meminfo (Linux) or vm_stat-like fallback."""
    try:
        with open("/proc/meminfo") as fh:
            lines = fh.readlines()
        info = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                info[key] = int(parts[1])  # kB
        total_mb = info.get("MemTotal", 0) / 1024
        avail_mb = info.get("MemAvailable", info.get("MemFree", 0)) / 1024
        return {
            "status": "ok",
            "total_mb": round(total_mb, 1),
            "available_mb": round(avail_mb, 1),
            "used_pct": round((1 - avail_mb / max(total_mb, 1)) * 100, 1),
        }
    except FileNotFoundError:
        # macOS / Windows -- return a best-effort report.
        return {
            "status": "ok",
            "note": "Detailed memory info not available on this OS.",
        }


def _check_disk() -> dict[str, Any]:
    """Disk probe for the root filesystem."""
    usage = shutil.disk_usage("/")
    total_gb = usage.total / (1024 ** 3)
    free_gb = usage.free / (1024 ** 3)
    return {
        "status": "ok",
        "total_gb": round(total_gb, 2),
        "free_gb": round(free_gb, 2),
        "used_pct": round((1 - free_gb / max(total_gb, 0.01)) * 100, 1),
    }


def _check_network() -> dict[str, Any]:
    """Network probe: DNS resolution and basic connectivity."""
    hostname = socket.gethostname()
    start = time.monotonic()
    try:
        socket.getaddrinfo("dns.google", 443, socket.AF_INET, socket.SOCK_STREAM)
        dns_ok = True
    except socket.gaierror:
        dns_ok = False
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    return {
        "status": "ok" if dns_ok else "degraded",
        "hostname": hostname,
        "dns_reachable": dns_ok,
        "dns_lookup_ms": elapsed_ms,
    }


_PROBES = {
    "cpu": _check_cpu,
    "memory": _check_memory,
    "disk": _check_disk,
    "network": _check_network,
}


# ── Step ─────────────────────────────────────────────────────────────────


@register("health_check")
class HealthCheckStep(FlowStep):
    """Run basic health probes on a compute node."""

    PARAMS_SCHEMA = HealthCheckParams
    OUTPUT_KEYS = ["health_report"]
    DESCRIPTION = "Run basic node health checks (CPU, memory, disk, network)."

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = HealthCheckParams(**resolved)

        report: dict[str, Any] = {}
        overall_ok = True

        for check_name in validated.checks:
            if check_name not in _PROBES:
                report[check_name] = {
                    "status": "error",
                    "message": f"Unknown check: {check_name}",
                }
                overall_ok = False
                continue

            try:
                result = _PROBES[check_name]()
                report[check_name] = result
                if result.get("status") not in ("ok",):
                    overall_ok = False
            except Exception as exc:
                report[check_name] = {"status": "error", "message": str(exc)}
                overall_ok = False

        return {
            "health_report": report,
            "overall_ok": overall_ok,
            "done": True,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        if state.get("done"):
            return StepResult.SUCCESS if state.get("overall_ok") else StepResult.FAILED
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        # Health checks are synchronous; nothing to cancel.
        pass
