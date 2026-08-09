"""Linux adapter — bash, apt, /tmp.

Concrete `OSAdapter` used on every host that is not macOS or Windows (see the
fallback in `nexus_agent.os_adapters.get_adapter`). Consumed by
`nexus_agent.executor.StepExecutor._run_subprocess`.
"""

from __future__ import annotations

import os
import tempfile

from nexus_agent.os_adapters.base import OSAdapter


class LinuxAdapter(OSAdapter):
    """OS adapter for Linux systems (Debian/Ubuntu-based by default).

    Stateless; construct freely. "Debian/Ubuntu-based by default" refers only
    to `package_install()`, which assumes apt — everything else is
    distro-neutral.
    """

    def shell_command(self) -> str:
        """Return /bin/bash as the interpreter for step commands.

        AI Note: Hardcoded rather than read from $SHELL. Step command strings
        are authored against bash semantics, and $SHELL on a service account
        is often /usr/sbin/nologin or a login shell that would break
        non-interactive execution.
        """
        return "/bin/bash"

    def package_install(self, package: str) -> str:
        """Return an apt-get install command for `package`.

        Args:
            package: Package name to install.

        Returns:
            A shell command string. `-y` is included so the command never
            blocks on a confirmation prompt in a non-interactive subprocess.

        Note:
            No `sudo` prefix and no `apt-get update` — the agent must already
            run with sufficient privileges and a reasonably fresh package
            index, otherwise the command fails with a non-zero exit code that
            surfaces as a failed step.
        """
        return f"apt-get install -y {package}"

    def resolve_path(self, path: str) -> str:
        """Expand `~` and `$VAR` references in `path` using POSIX rules.

        Args:
            path: Possibly-unexpanded path.

        Returns:
            The expanded path. Expansion order matters: env vars are expanded
            first so that a variable holding "~" still gets tilde-expanded.
        """
        return os.path.expanduser(os.path.expandvars(path))

    def temp_dir(self) -> str:
        """Return the system temp directory, honoring $TMPDIR when set."""
        return tempfile.gettempdir()  # /tmp on Linux

    def os_type(self) -> str:
        """Return the scheduler-facing OS literal for Linux nodes."""
        return "linux"
