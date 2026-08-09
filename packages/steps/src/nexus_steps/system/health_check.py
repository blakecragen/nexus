"""Basic node health check step.

Runs lightweight probes for CPU, memory, disk, and network and returns a
structured health report.  Designed to be the first step in a job to
verify the node is in good shape before heavier workloads.

Where this fits
---------------
Registered as ``"health_check"``. Runs on a node like any other remote step
(``REQUIRES_NODE`` stays at its ``True`` default) and publishes a single
``health_report`` dict into the job context, so a later step can branch on it.
It is unrelated to the agent's periodic heartbeat/liveness reporting — this is
an on-demand, job-scoped probe.

Design constraints
------------------
Probes must be cheap, read-only and never block for long: the whole step runs
synchronously inside ``startup()``. They are also expected to degrade rather
than raise on platforms that lack a data source, so one unsupported metric
never fails an otherwise healthy node.
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


# The set of probe names this step understands.
#
# AI Note: declared but never referenced — validation of unknown names happens
# against the ``_PROBES`` dict inside ``startup()`` instead. Keep the two in
# sync if you start using this constant, or delete it; a stale copy here would
# be worse than no copy.
VALID_CHECKS = {"cpu", "memory", "disk", "network"}


class HealthCheckParams(BaseModel):
    """Parameters for the health_check step.

    The ``description``/``examples`` text is user-facing via ``to_schema()`` →
    ``/api/steps``.
    """

    checks: list[str] = Field(
        # AI Note: a mutable list literal as a Pydantic default is safe —
        # Pydantic deep-copies defaults per model instance, so one job cannot
        # mutate the default for the next.
        default=["cpu", "memory", "disk", "network"],
        description=(
            "List of health checks to run. "
            "Valid values: cpu, memory, disk, network."
        ),
        examples=[["cpu", "memory"], ["disk"]],
    )


# ── Probe Helpers ────────────────────────────────────────────────────────


def _check_cpu() -> dict[str, Any]:
    """Basic CPU probe: count and 1-minute load average.

    Returns:
        A report dict with ``status`` (always ``"ok"``), ``cpu_count``, the
        1/5/15-minute load averages, and the CPU ``arch``.

    AI Note: ``status`` is unconditionally "ok" — this probe reports numbers
    but applies no thresholds, so a machine at load 400 still passes. Any
    "is this node too busy?" policy has to live in a downstream step.

    AI Note: the load averages fall back to the sentinel ``-1.0`` on Windows,
    where ``os.getloadavg`` does not exist. Consumers must treat -1 as "not
    available", not as an actual load value.
    """
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
    """Memory probe using /proc/meminfo (Linux) or vm_stat-like fallback.

    Returns:
        On Linux, a dict with ``total_mb``, ``available_mb`` and ``used_pct``;
        on macOS/Windows, a ``status: ok`` dict carrying only an explanatory
        ``note`` (no figures). ``status`` is "ok" in both cases.

    AI Note: platform detection is done by *attempting* the Linux read and
    catching ``FileNotFoundError``, rather than by checking ``sys.platform``.
    That is deliberate — it also covers containers and unusual Linux images
    where /proc is not mounted. The docstring's mention of a "vm_stat-like
    fallback" is aspirational; no macOS-specific probe is implemented.

    AI Note: ``max(total_mb, 1)`` guards against division by zero when
    MemTotal is missing from the parsed output. It skews the percentage in that
    degenerate case, which is preferable to raising.
    """
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
        # AI Note: MemAvailable (kernel's estimate including reclaimable cache)
        # is the right number for "can I start a big job?"; MemFree is only the
        # fallback for kernels too old to publish it, and badly understates
        # usable memory.
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
    """Disk probe for the root filesystem.

    Returns:
        A dict with ``total_gb``, ``free_gb`` and ``used_pct`` for ``/``.

    AI Note: only the ROOT filesystem is measured. Nodes that put job
    workspaces, Docker images or gem5 m5out output on a separate volume can
    report plenty of free space here while the volume that matters is full.

    AI Note: ``max(total_gb, 0.01)`` avoids a ZeroDivisionError on exotic
    pseudo-filesystems reporting zero total. Unlike its siblings this probe has
    no try/except — an OSError propagates to ``startup()``, which converts it
    into a ``status: error`` entry.
    """
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
    """Network probe: DNS resolution and basic connectivity.

    Returns:
        A dict with ``status`` (``"ok"`` or ``"degraded"``), ``hostname``,
        ``dns_reachable`` and the ``dns_lookup_ms`` latency.

    AI Note: this is the ONLY probe that can return a non-"ok" status, so it is
    effectively the only thing that can make ``health_check`` fail a node.

    AI Note: it resolves the hardcoded external host ``dns.google``. Two
    consequences: an air-gapped or split-horizon-DNS node is reported as
    degraded even when it can reach the Nexus server perfectly well, and the
    call has no explicit timeout — it inherits the resolver's, which can be
    several seconds on a broken network. It also does not open a connection, so
    "reachable" here means "resolvable", not "routable".

    AI Note: ``time.monotonic`` (not ``time.time``) is correct for measuring
    elapsed duration — immune to wall-clock adjustments.
    """
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


# Probe-name → callable registry. This dict — not ``VALID_CHECKS`` — is what
# ``startup()`` validates requested check names against, so adding a probe means
# adding it here.
_PROBES = {
    "cpu": _check_cpu,
    "memory": _check_memory,
    "disk": _check_disk,
    "network": _check_network,
}


# ── Step ─────────────────────────────────────────────────────────────────


@register("health_check")
class HealthCheckStep(FlowStep):
    """Run basic health probes on a compute node.

    Read-only and side-effect free apart from one outbound DNS lookup; safe to
    run repeatedly and cheap enough to prepend to any job.
    """

    PARAMS_SCHEMA = HealthCheckParams
    OUTPUT_KEYS = ["health_report"]
    DESCRIPTION = "Run basic node health checks (CPU, memory, disk, network)."

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Run each requested probe, aggregating results into one report.

        Args:
            params: Raw step params; ``checks`` selects which probes run and in
                what order.
            ctx: Job context; ``ctx.resolve()`` layers upstream outputs beneath
                these params.

        Returns:
            ``{"health_report": {<check>: <probe result>}, "overall_ok": bool,
            "done": True}``. ``health_report`` is the only key exported to the
            job context.

        Side effects:
            One outbound DNS lookup when the ``network`` probe is included;
            everything else is local reads.

        AI Note: probe failures are contained per-check — an exception from one
        probe becomes a ``status: error`` entry and flips ``overall_ok``, but
        the remaining probes still run. That is why the report is always
        complete for every requested name, even a bogus one.

        AI Note: ``overall_ok`` is false unless EVERY probe reports exactly
        ``"ok"``; ``"degraded"`` counts as a failure. Since the network probe is
        the only one that can be degraded, in practice a node with no external
        DNS fails this step outright.
        """
        resolved = ctx.resolve(params)
        validated = HealthCheckParams(**resolved)

        report: dict[str, Any] = {}
        overall_ok = True

        for check_name in validated.checks:
            if check_name not in _PROBES:
                # Unknown names are reported in-band and fail the step, rather
                # than being silently skipped — a typo'd check must not look
                # like a passing health check.
                report[check_name] = {
                    "status": "error",
                    "message": f"Unknown check: {check_name}",
                }
                overall_ok = False
                continue

            try:
                result = _PROBES[check_name]()
                report[check_name] = result
                # Strict comparison: anything other than "ok" (e.g. "degraded")
                # fails the overall verdict.
                if result.get("status") not in ("ok",):
                    overall_ok = False
            except Exception as exc:
                # Contain the failure to this probe so the rest still run.
                report[check_name] = {"status": "error", "message": str(exc)}
                overall_ok = False

        return {
            "health_report": report,
            "overall_ok": overall_ok,
            "done": True,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        """Report the verdict already computed by ``startup()``. Idempotent.

        Args:
            state: The ``startup()`` state dict.

        Returns:
            ``SUCCESS`` when ``done`` and ``overall_ok``, ``FAILED`` when
            ``done`` but not ok, else ``RUNNING``.

        AI Note: unlike the other synchronous steps this one has no ``error``
        key branch — failures are represented inside ``health_report`` and
        summarised by ``overall_ok``, so that flag is the whole decision.
        """
        if state.get("done"):
            return StepResult.SUCCESS if state.get("overall_ok") else StepResult.FAILED
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        """No-op — all probes completed inside ``startup()``.

        Args:
            state: Unused; accepted to satisfy the FlowStep interface.
        """
        # Health checks are synchronous; nothing to cancel.
        pass
