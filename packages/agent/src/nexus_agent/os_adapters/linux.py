"""Linux adapter — bash, apt, /tmp."""

from __future__ import annotations

import os
import tempfile

from nexus_agent.os_adapters.base import OSAdapter


class LinuxAdapter(OSAdapter):
    """OS adapter for Linux systems (Debian/Ubuntu-based by default)."""

    def shell_command(self) -> str:
        return "/bin/bash"

    def package_install(self, package: str) -> str:
        return f"apt-get install -y {package}"

    def resolve_path(self, path: str) -> str:
        return os.path.expanduser(os.path.expandvars(path))

    def temp_dir(self) -> str:
        return tempfile.gettempdir()  # /tmp on Linux

    def os_type(self) -> str:
        return "linux"
