"""Abstract base class for OS-specific adapters.

Each platform (macOS, Linux, Windows) provides an implementation
that tells the executor how to run commands, install packages, and
resolve paths on that system.

Role in the system:
    `nexus_agent.executor.StepExecutor._run_subprocess()` asks
    `os_adapters.get_adapter()` for the concrete adapter matching the host,
    then uses `shell_command()` as the subprocess `executable=` and
    `temp_dir()` as the fallback working directory. Keeping this behind an
    interface means step implementations stay OS-agnostic: they emit a plain
    command string and the adapter decides which shell interprets it.

Contract for implementers:
    - Every method must be pure and cheap — they are called on the hot path of
      each step execution and must not perform I/O or spawn processes.
    - `os_type()` must return one of the exact strings "macos" / "linux" /
      "windows". Those literals are compared against `FlowStep.SUPPORTED_OS`
      and `OS_VARIANTS` keys in `nexus_common.steps.base`, and against the
      `os_type` column on the server's node records — renaming them silently
      breaks scheduling.
    - Adding a method here is a breaking change for all three subclasses;
      `OSAdapter` is an ABC, so a missing override raises TypeError at
      instantiation time inside `get_adapter()`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class OSAdapter(ABC):
    """Interface for OS-specific behavior used by the step executor.

    Concrete implementations live alongside this module: `MacOSAdapter`,
    `LinuxAdapter`, and `WindowsAdapter`. Instances are stateless and are
    constructed fresh per call by `get_adapter()`, so they must not cache
    anything that varies over the life of the agent process.
    """

    @abstractmethod
    def shell_command(self) -> str:
        """Return the path to the default shell executable.

        Examples: /bin/zsh, /bin/bash, powershell.exe

        Returns:
            An absolute path (or a PATH-resolvable name on Windows) that is
            passed as `executable=` to `asyncio.create_subprocess_shell()`.
            The value must exist on the host; a wrong path makes every
            command-style step fail immediately with an OSError.
        """

    @abstractmethod
    def package_install(self, package: str) -> str:
        """Return the shell command string to install a system package.

        Args:
            package: Package name (e.g., "git", "curl").

        Returns:
            A shell command string ready for subprocess execution.

        Note:
            The returned string is *not* escaped or validated — callers are
            responsible for ensuring `package` comes from a trusted source,
            since the result is handed to a shell.
        """

    @abstractmethod
    def resolve_path(self, path: str) -> str:
        """Resolve a platform-specific path.

        Handles things like ~ expansion, environment variable expansion,
        and path separator normalization.

        Args:
            path: A possibly-unexpanded path, e.g. "~/work/$JOB_ID".

        Returns:
            An expanded path string using the host's separator conventions.
            Unset environment variables are left verbatim (the standard
            `os.path.expandvars` behavior), not replaced with an empty string.
        """

    @abstractmethod
    def temp_dir(self) -> str:
        """Return the system temporary directory path.

        Used by the executor as the working directory for a step that did not
        set `work_dir` in its startup() state. The directory is assumed to
        already exist and be writable by the agent user.
        """

    @abstractmethod
    def os_type(self) -> str:
        """Return the normalized OS type string: macos, linux, or windows.

        These exact literals are the cross-package contract for OS matching
        (see the module docstring) — do not return values like "darwin" or
        "Windows".
        """
