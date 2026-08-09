"""macOS adapter — zsh, Homebrew, /tmp.

Concrete `OSAdapter` selected by `nexus_agent.os_adapters.get_adapter()` when
`platform.system()` is "Darwin". Consumed by
`nexus_agent.executor.StepExecutor._run_subprocess`.
"""

from __future__ import annotations

import os
import tempfile

from nexus_agent.os_adapters.base import OSAdapter


class MacOSAdapter(OSAdapter):
    """OS adapter for macOS systems. Stateless; construct freely."""

    def shell_command(self) -> str:
        """Return /bin/zsh as the interpreter for step commands.

        zsh is the macOS default login shell since Catalina and is always
        present at this path, so it is hardcoded rather than read from $SHELL.
        """
        return "/bin/zsh"

    def package_install(self, package: str) -> str:
        """Return a Homebrew install command for `package`.

        Args:
            package: Formula name to install.

        Returns:
            A shell command string.

        Note:
            Assumes `brew` is on the agent's PATH. Homebrew refuses to run as
            root, and launchd/LaunchAgent environments frequently omit
            /opt/homebrew/bin from PATH — both show up as a non-zero exit code
            on the step rather than an adapter-level error.
        """
        return f"brew install {package}"

    def resolve_path(self, path: str) -> str:
        """Expand `~` and `$VAR` references in `path` using POSIX rules.

        Args:
            path: Possibly-unexpanded path.

        Returns:
            The expanded path. Env vars are expanded before `~` so a variable
            holding "~" still resolves to the home directory.
        """
        return os.path.expanduser(os.path.expandvars(path))

    def temp_dir(self) -> str:
        """Return the system temp directory.

        AI Note: On macOS this is the per-user, per-boot sandbox directory
        under /var/folders/... (via $TMPDIR), not literally /tmp — the inline
        comment below is a simplification. It is periodically pruned by the
        OS, so steps that need results to survive must not rely on it.
        """
        return tempfile.gettempdir()  # /tmp on macOS

    def os_type(self) -> str:
        """Return the scheduler-facing OS literal for macOS nodes."""
        return "macos"
