"""Auto-detect host info for node registration.

Gathers hardware info, OS details, and architecture so the scheduler can match
steps to OS-compatible nodes. (Software "capabilities" detection was removed —
whether a node can run gem5/git/etc. is the operator's responsibility, proven by
actually running a job; see the per-job terminal log.)

Role in the system:
    `nexus_agent.connection.AgentConnection._send_registration()` calls
    `detect_capabilities()` once per WebSocket connection and copies the
    result field-by-field into an `AgentRegister` message. The server's
    `_handle_agent_message` writes those values straight onto the node row,
    which the Nodes UI displays and the runner's node picker filters on
    (`os_type` in particular). `nexus_agent.executor` also imports
    `_detect_os_type()` directly to resolve OS-specific step params.

Design constraints:
    - Every probe is best-effort: a failure degrades to a placeholder rather
      than raising, because a node that cannot describe its GPU should still
      be able to join the cluster.
    - Detection runs on every reconnect, so probes must stay cheap and
      bounded. Subprocess calls all pass an explicit `timeout=`.
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

    Returns:
        A dict with keys `hostname`, `os_type`, `os_version`, `arch`,
        `cpu_model`, `cpu_cores`, `ram_mb`, `gpu_info`, and `ip_address`.
        Every key except `gpu_info` is non-None; `gpu_info` is `None` when no
        GPU could be identified.

    Side effects:
        May spawn short-lived subprocesses (`sysctl`, `nvidia-smi`,
        `system_profiler`), read /proc and /etc files, and open a UDP socket.
        Total worst case is roughly 25s if every probe hits its timeout.

    AI Note: The key names here are a hand-maintained contract with the
    `AgentRegister` model — `_send_registration()` indexes this dict with
    literal strings (`caps["hostname"]`, etc.), so renaming a key raises
    KeyError at registration time and takes the node offline, not at import.
    """
    return {
        "hostname": platform.node(),
        "os_type": _detect_os_type(),
        "os_version": _detect_os_version(),
        "arch": _detect_arch(),
        "cpu_model": _detect_cpu_model(),
        # AI Note: `or 1` guards psutil returning None on exotic platforms;
        # cpu_cores is a non-nullable int on the wire and is used as a
        # capacity hint, so 1 is the safe floor.
        "cpu_cores": psutil.cpu_count(logical=True) or 1,
        "ram_mb": round(psutil.virtual_memory().total / (1024 * 1024)),
        "gpu_info": _detect_gpu(),
        "ip_address": _detect_ip(),
    }


# ── OS ─────────────────────────────────────────────────────────────────


def _detect_os_type() -> str:
    """Return normalized OS type: macos, linux, or windows.

    Returns:
        One of the three literals. Anything that is not Darwin or Windows is
        reported as "linux".

    AI Note: These literals are the cluster-wide OS vocabulary — they are
    matched against `FlowStep.SUPPORTED_OS` / `OS_VARIANTS` keys and stored in
    the node's `os_type` column that the scheduler filters on. This mapping
    must stay in sync with `OSAdapter.os_type()` in each os_adapters module.
    """
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


def _detect_os_version() -> str:
    """Return OS version string.

    Display-only; nothing schedules on this value.

    Returns:
        macOS: the product version (e.g. "14.5"), falling back to the kernel
        release when `mac_ver()` is empty. Windows: `platform.version()`.
        Linux: "<NAME> <VERSION_ID>" parsed from /etc/os-release, falling back
        to the kernel release.

    AI Note: The /etc/os-release parser only guards FileNotFoundError. A file
    that exists but is unreadable (PermissionError) or contains undecodable
    bytes propagates out of here and aborts the whole registration, taking the
    node offline — see POSSIBLE BUG in the task summary.
    """
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
                # partition (not split) so values containing "=" survive intact
                key, _, value = line.strip().partition("=")
                info[key] = value.strip('"')
        return f"{info.get('NAME', 'Linux')} {info.get('VERSION_ID', platform.release())}"
    except FileNotFoundError:
        return platform.release()


# ── CPU / Architecture ─────────────────────────────────────────────────


def _detect_arch() -> str:
    """Return architecture: arm64, x86_64, etc.

    Returns:
        A normalized architecture string. aarch64/arm64 collapse to "arm64"
        and x86_64/amd64 collapse to "x86_64"; anything else is passed through
        lowercased so unusual hosts still report something meaningful.

    AI Note: Normalization matters because the same machine reports different
    raw values across OSes (Linux says "aarch64", macOS says "arm64"). The
    normalized value is what appears in the UI and in any arch-based
    targeting, so both spellings must map to one canonical string.
    """
    machine = platform.machine().lower()
    # Normalize common names
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    return machine


def _detect_cpu_model() -> str:
    """Best-effort CPU model name detection.

    Uses the most descriptive per-platform source available: `sysctl
    machdep.cpu.brand_string` on macOS, the first "model name" line of
    /proc/cpuinfo on Linux, and the CentralProcessor registry key on Windows.

    Returns:
        A human-readable CPU name, or `platform.processor()` / "unknown" when
        every probe fails. Display-only — never parsed.

    Side effects:
        Spawns `sysctl` on macOS (5s timeout), reads /proc/cpuinfo on Linux,
        opens a registry key on Windows.

    AI Note: The bare `except Exception` is intentional — this covers a
    subprocess timeout, a missing /proc, and the Windows-only `winreg` import
    all at once. A node must never fail to register because it could not name
    its CPU. Note the registry key is left open (no close/context manager),
    leaking a handle per registration on Windows.
    """
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
            # Imported inline: winreg does not exist on POSIX, so a top-level
            # import would break the module everywhere else.
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
    """Attempt to detect GPU info. Returns None if unavailable.

    Tries NVIDIA first (works on both Linux and Windows), then falls back to
    macOS `system_profiler`. Display-only; nothing schedules on this value.

    Returns:
        For NVIDIA: a "; "-joined list like "NVIDIA A100 (81920 MB)" covering
        every visible GPU. For macOS: the chipset/chip name. `None` when no
        GPU could be identified — including on AMD/Intel Linux hosts, which
        have no probe here.

    Side effects:
        Spawns `nvidia-smi` (10s timeout) and/or `system_profiler` (10s
        timeout).

    AI Note: The `nvidia-smi` branch runs before the macOS branch and the
    `shutil.which` guard avoids paying for a missing-binary exception. Both
    `except Exception: pass` blocks are deliberate — a hung or broken GPU tool
    must degrade to `None`, never block registration.
    """
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
                        # `nounits` in the query means memory.total is a bare
                        # number of MiB, hence the literal "MB" suffix here.
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
                    # "Chipset Model:" on Intel Macs, "Chip:" on Apple silicon.
                    if line.startswith("Chipset Model:") or line.startswith("Chip:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass

    return None


# ── Network ────────────────────────────────────────────────────────────


def _detect_ip() -> str:
    """Detect the primary non-loopback IP address.

    Returns:
        The local address of the interface the OS would use to reach the
        public internet, or "127.0.0.1" if that cannot be determined. Reported
        to the server for display and for operator SSH/provisioning hints.

    AI Note: Connecting a UDP socket sends no packets — it only asks the
    kernel to select a source address via the routing table. That is why this
    works offline and returns instantly, and why 8.8.8.8 is a routing probe
    rather than a dependency on Google DNS. The trade-off: on a multi-homed
    host it reports whichever interface serves the default route, which may
    not be the one the Nexus server actually reaches this node on.
    """
    try:
        # Create a UDP socket to determine the default route IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
