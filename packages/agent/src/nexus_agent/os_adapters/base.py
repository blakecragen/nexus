"""Abstract base class for OS-specific adapters.

Each platform (macOS, Linux, Windows) provides an implementation
that tells the executor how to run commands, install packages, and
resolve paths on that system.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class OSAdapter(ABC):
    """Interface for OS-specific behavior used by the step executor."""

    @abstractmethod
    def shell_command(self) -> str:
        """Return the path to the default shell executable.

        Examples: /bin/zsh, /bin/bash, powershell.exe
        """

    @abstractmethod
    def package_install(self, package: str) -> str:
        """Return the shell command string to install a system package.

        Args:
            package: Package name (e.g., "git", "curl").

        Returns:
            A shell command string ready for subprocess execution.
        """

    @abstractmethod
    def resolve_path(self, path: str) -> str:
        """Resolve a platform-specific path.

        Handles things like ~ expansion, environment variable expansion,
        and path separator normalization.
        """

    @abstractmethod
    def temp_dir(self) -> str:
        """Return the system temporary directory path."""

    @abstractmethod
    def os_type(self) -> str:
        """Return the normalized OS type string: macos, linux, or windows."""
