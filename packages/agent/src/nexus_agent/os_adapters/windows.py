"""Windows adapter — PowerShell, Chocolatey, %TEMP%."""

from __future__ import annotations

import os
import tempfile

from nexus_agent.os_adapters.base import OSAdapter


class WindowsAdapter(OSAdapter):
    """OS adapter for Windows systems."""

    def shell_command(self) -> str:
        return "powershell.exe"

    def package_install(self, package: str) -> str:
        return f"choco install {package} -y"

    def resolve_path(self, path: str) -> str:
        expanded = os.path.expanduser(os.path.expandvars(path))
        # Normalize to Windows path separators
        return expanded.replace("/", "\\")

    def temp_dir(self) -> str:
        return tempfile.gettempdir()  # %TEMP% on Windows

    def os_type(self) -> str:
        return "windows"
