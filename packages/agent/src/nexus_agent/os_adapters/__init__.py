"""OS adapter registry — returns the correct adapter for the current platform.

This is the single place that maps `platform.system()` onto a concrete
`OSAdapter`. `nexus_agent.executor` calls `get_adapter()` on every subprocess
step; nothing else in the agent should branch on the host OS for shell/path
decisions.

Design notes:
    - Adapter classes are imported lazily inside `get_adapter()` so that
      importing this package never pulls in code for platforms that are not
      running (notably the Windows adapter on POSIX hosts and vice versa), and
      so agent startup stays cheap.
    - `base.OSAdapter` is imported only under TYPE_CHECKING for the same
      reason — it exists here purely as a return-type annotation, which is a
      string at runtime thanks to `from __future__ import annotations`.
"""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus_agent.os_adapters.base import OSAdapter


def get_adapter() -> OSAdapter:
    """Return the OS adapter for the current platform.

    Returns:
        A freshly constructed `MacOSAdapter`, `WindowsAdapter`, or
        `LinuxAdapter`. Adapters are stateless, so callers may construct one
        per use rather than caching it.

    Note:
        This dispatches on `platform.system()` directly rather than on
        `capability._detect_os_type()`. Both normalize the same three
        platforms, so they agree in practice — but if a new OS is ever added,
        both mappings must be updated together or the agent will report one
        `os_type` to the scheduler and use a different adapter locally.
    """
    system = platform.system().lower()

    if system == "darwin":
        from nexus_agent.os_adapters.macos import MacOSAdapter
        return MacOSAdapter()

    if system == "windows":
        from nexus_agent.os_adapters.windows import WindowsAdapter
        return WindowsAdapter()

    # AI Note: Deliberate catch-all. Any non-Darwin, non-Windows host (BSD,
    # Solaris, unrecognized values) is treated as Linux rather than raising,
    # so an unusual host still runs steps with /bin/bash instead of refusing
    # to start. The mismatch surfaces as a failing command, not a crashed agent.
    # Default to Linux for all other Unix-like systems
    from nexus_agent.os_adapters.linux import LinuxAdapter
    return LinuxAdapter()
