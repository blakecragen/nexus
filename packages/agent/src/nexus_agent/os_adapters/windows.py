"""Windows adapter — PowerShell, Chocolatey, %TEMP%.

Concrete `OSAdapter` selected by `nexus_agent.os_adapters.get_adapter()` when
`platform.system()` is "Windows". Consumed by
`nexus_agent.executor.StepExecutor._run_subprocess`.

AI Note: This adapter is the least exercised of the three. The executor drives
subprocesses through `asyncio.create_subprocess_shell(..., executable=...)`,
and on Windows the `executable=` argument behaves differently than on POSIX
(cmd.exe is still the shell that interprets the command line). Treat Windows
step execution as best-effort until covered by an integration test.
"""

from __future__ import annotations

import os
import tempfile

from nexus_agent.os_adapters.base import OSAdapter


class WindowsAdapter(OSAdapter):
    """OS adapter for Windows systems. Stateless; construct freely."""

    def shell_command(self) -> str:
        """Return powershell.exe as the interpreter for step commands.

        Resolved via PATH rather than an absolute path so it works on both
        32/64-bit layouts. Note this selects Windows PowerShell 5.1, not
        PowerShell 7+ (`pwsh.exe`).
        """
        return "powershell.exe"

    def package_install(self, package: str) -> str:
        """Return a Chocolatey install command for `package`.

        Args:
            package: Chocolatey package id.

        Returns:
            A shell command string. `-y` auto-confirms so the subprocess never
            blocks waiting for input.

        Note:
            Requires `choco` on PATH and an elevated agent process; otherwise
            the step fails with Chocolatey's non-zero exit code.
        """
        return f"choco install {package} -y"

    def resolve_path(self, path: str) -> str:
        """Expand `~`/`$VAR` then normalize separators to backslashes.

        Args:
            path: Possibly-unexpanded path, which may use POSIX separators
                because step params are often authored cross-platform.

        Returns:
            A backslash-separated Windows path.

        AI Note: The blanket `/` → `\\` replacement is applied to the whole
        string, so it also rewrites forward slashes that were not path
        separators (e.g. a URL or a command flag embedded in the value).
        Callers should pass paths only, never full command lines.
        """
        expanded = os.path.expanduser(os.path.expandvars(path))
        # Normalize to Windows path separators
        return expanded.replace("/", "\\")

    def temp_dir(self) -> str:
        """Return the system temp directory (%TEMP%/%TMP% on Windows)."""
        return tempfile.gettempdir()  # %TEMP% on Windows

    def os_type(self) -> str:
        """Return the scheduler-facing OS literal for Windows nodes."""
        return "windows"
