"""OS adapter registry — returns the correct adapter for the current platform."""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus_agent.os_adapters.base import OSAdapter


def get_adapter() -> OSAdapter:
    """Return the OS adapter for the current platform."""
    system = platform.system().lower()

    if system == "darwin":
        from nexus_agent.os_adapters.macos import MacOSAdapter
        return MacOSAdapter()

    if system == "windows":
        from nexus_agent.os_adapters.windows import WindowsAdapter
        return WindowsAdapter()

    # Default to Linux for all other Unix-like systems
    from nexus_agent.os_adapters.linux import LinuxAdapter
    return LinuxAdapter()
