"""macOS adapter — zsh, Homebrew, /tmp."""

from __future__ import annotations

import os
import tempfile

from nexus_agent.os_adapters.base import OSAdapter


class MacOSAdapter(OSAdapter):
    """OS adapter for macOS systems."""

    def shell_command(self) -> str:
        return "/bin/zsh"

    def package_install(self, package: str) -> str:
        return f"brew install {package}"

    def resolve_path(self, path: str) -> str:
        return os.path.expanduser(os.path.expandvars(path))

    def temp_dir(self) -> str:
        return tempfile.gettempdir()  # /tmp on macOS

    def os_type(self) -> str:
        return "macos"
