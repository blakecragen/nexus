"""Auto-detect host info for node registration.

Gathers hardware info, OS details, and architecture so the scheduler can match
steps to OS-compatible nodes. (Software "capabilities" detection was removed —
whether a node can run gem5/git/etc. is the operator's responsibility, proven by
actually running a job; see the per-job terminal log.)
"""

from __future__ import annotations

import logging
import platform
import shutil
import socket
import subprocess
from typing import Any

import psutil

logger = logging.getLogger("nexus.agent.capability")


def detect_capabilities() -> dict[str, Any]:
    """Detect and return host info for this node.

    Returns a dict compatible with AgentRegister fields (no software list).
    """
    return {
        "hostname": platform.node(),
        "os_type": _detect_os_type(),
        "os_version": _detect_os_version(),
        "arch": _detect_arch(),
        "cpu_model": _detect_cpu_model(),
        "cpu_cores": psutil.cpu_count(logical=True) or 1,
        "ram_mb": round(psutil.virtual_memory().total / (1024 * 1024)),
        "gpu_info": _detect_gpu(),
        "ip_address": _detect_ip(),
    }


# ── OS ─────────────────────────────────────────────────────────────────


def _detect_os_type() -> str:
    """Return normalized OS type: macos, linux, or windows."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


def _detect_os_version() -> str:
    """Return OS version string."""
    system = platform.system().lower()
    if system == "darwin":
        return platform.mac_ver()[0] or platform.release()
    if system == "windows":
        return platform.version()
    # Linux — try to get distro info
    try:
        with open("/etc/os-release") as f:
            lines = f.readlines()
        info = {}
        for line in lines:
            if "=" in line:
                key, _, value = line.strip().partition("=")
                info[key] = value.strip('"')
        return f"{info.get('NAME', 'Linux')} {info.get('VERSION_ID', platform.release())}"
    except FileNotFoundError:
        return platform.release()


# ── CPU / Architecture ─────────────────────────────────────────────────


def _detect_arch() -> str:
    """Return architecture: arm64, x86_64, etc."""
    machine = platform.machine().lower()
    # Normalize common names
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    return machine


def _detect_cpu_model() -> str:
    """Best-effort CPU model name detection."""
    system = platform.system().lower()
    try:
        if system == "darwin":
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        elif system == "linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        elif system == "windows":
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return str(value)
    except Exception as exc:
        logger.debug("CPU model detection failed: %s", exc)

    return platform.processor() or "unknown"


# ── GPU ────────────────────────────────────────────────────────────────


def _detect_gpu() -> str | None:
    """Attempt to detect GPU info. Returns None if unavailable."""
    # Try nvidia-smi first
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                gpus = []
                for line in result.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 2:
                        gpus.append(f"{parts[0]} ({parts[1]} MB)")
                    else:
                        gpus.append(parts[0])
                return "; ".join(gpus)
        except Exception:
            pass

    # macOS — system_profiler
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType", "-detailLevel", "mini"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("Chipset Model:") or line.startswith("Chip:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass

    return None


# ── Network ────────────────────────────────────────────────────────────


def _detect_ip() -> str:
    """Detect the primary non-loopback IP address."""
    try:
        # Create a UDP socket to determine the default route IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
