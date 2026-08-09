"""Agent configuration — persisted to ~/.nexus-agent/config.json.

Supports three ways to load configuration (in priority order):
1. Explicit CLI flags (--server, --api-key, --node-id)
2. Explicit --config path
3. Default path ~/.nexus-agent/config.json

Role in the system:
    `nexus_agent.main` builds an `AgentConfig` (via `create()` for the `init`
    subcommand, `load()` for `run`) and hands it to
    `nexus_agent.connection.AgentConnection`, which reads `server_url`,
    `api_key`, `node_id`, and `tags`. `nexus_agent.executor` additionally
    derives the HTTP callback base URL from `server_url` and passes `api_key`
    to steps so they can upload results back to the server.

Security:
    The config file holds the node's API key in plaintext — the same secret
    the server uses to authenticate the WebSocket. `save()` chmods it to 0600.
    Never log the file contents or `api_key`.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


# AI Note: These are resolved at import time, so `Path.home()` is captured once
# per process. Tests that monkeypatch HOME after importing this module will
# still see the original default path.
DEFAULT_CONFIG_DIR = Path.home() / ".nexus-agent"
"""Default directory holding the agent's config.json (~/.nexus-agent)."""

DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"
"""Default config file path used by `load()` when no explicit path is given."""


@dataclass
class AgentConfig:
    """Runtime configuration for the Nexus agent.

    Attributes:
        server_url: Full agent WebSocket endpoint, e.g.
            `ws://host:8000/ws/agent/<node-uuid>`. The node UUID is part of
            the path, and `connection._build_url()` appends the API key as a
            query parameter. `executor` also string-slices this at "/ws/" to
            derive the HTTP base for result uploads, so the "/ws/" segment
            must be present for the callback URL to be correct.
        api_key: Per-node secret issued by the server; authenticates the
            WebSocket and the agent→server upload endpoints.
        node_id: Identifier the agent registers under. Defaults to the
            hostname, falling back to a random `node-<hex>` when
            `platform.node()` returns an empty string.
        config_dir: Directory this config was loaded from / will be saved to.
            Not persisted inside the file itself — it is derived from the
            file's own location on load.
        tags: Free-form scheduling labels forwarded in AgentRegister and used
            server-side for node/pool targeting.
    """

    server_url: str
    api_key: str
    # AI Note: default_factory (not a plain default) so the hostname is read at
    # instantiation time, and so each instance gets its own random fallback id.
    node_id: str = field(default_factory=lambda: platform.node() or f"node-{uuid.uuid4().hex[:8]}")
    config_dir: str = str(DEFAULT_CONFIG_DIR)
    tags: list[str] = field(default_factory=list)

    @property
    def config_path(self) -> Path:
        """Absolute path of the config.json this instance reads from/writes to."""
        return Path(self.config_dir) / "config.json"

    # ── Persistence ────────────────────────────────────────────────────

    def save(self) -> Path:
        """Write configuration to disk. Creates parent directories.

        Side effects:
            Creates `config_dir` (including parents) if missing, overwrites
            `config_path`, and tightens its mode to 0600.

        Returns:
            The path that was written.

        Raises:
            OSError: If the directory cannot be created or the file cannot be
                written / chmod'ed.

        Note:
            `config_dir` is intentionally not serialized — it is inferred from
            the file location on `load()`, so a config directory can be moved
            or copied without editing its contents.
        """
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "server_url": self.server_url,
            "api_key": self.api_key,
            "node_id": self.node_id,
            "tags": self.tags,
        }
        path.write_text(json.dumps(data, indent=2) + "\n")
        # AI Note: Security-sensitive. The file contains the node API key, which
        # grants WebSocket access and result-upload rights for this node. The
        # chmod happens *after* the write, so there is a brief window where the
        # file exists at the process umask; acceptable because the parent
        # directory is under the agent user's home.
        # Restrict permissions — config contains the API key
        os.chmod(path, 0o600)
        return path

    @classmethod
    def create(
        cls,
        server_url: str,
        api_key: str,
        node_id: str | None = None,
        config_dir: str | None = None,
    ) -> AgentConfig:
        """Create a new config and persist it to disk.

        Backs the `nexus-agent init` subcommand.

        Args:
            server_url: Agent WebSocket endpoint (see class docstring).
            api_key: Node API key issued by the server.
            node_id: Override for the registered node id; defaults to the
                hostname, or a random `node-<hex>` if the hostname is empty.
            config_dir: Directory to write config.json into; defaults to
                ~/.nexus-agent.

        Returns:
            The saved `AgentConfig`.

        Side effects:
            Writes (and overwrites without prompting) `config_dir/config.json`.

        Raises:
            OSError: Propagated from `save()`.
        """
        config = cls(
            server_url=server_url,
            api_key=api_key,
            node_id=node_id or (platform.node() or f"node-{uuid.uuid4().hex[:8]}"),
            config_dir=config_dir or str(DEFAULT_CONFIG_DIR),
        )
        config.save()
        return config

    @classmethod
    def load(
        cls,
        config_path: str | None = None,
        server_url: str | None = None,
        api_key: str | None = None,
        node_id: str | None = None,
    ) -> AgentConfig:
        """Load configuration from disk, with optional CLI overrides.

        If server_url and api_key are both provided as arguments, no file
        is needed — a transient config is returned without writing to disk.

        Args:
            config_path: Explicit config.json path; defaults to
                DEFAULT_CONFIG_FILE.
            server_url: CLI override for the server URL.
            api_key: CLI override for the node API key.
            node_id: CLI override for the node id.

        Returns:
            A populated `AgentConfig`. Nothing is written to disk by this
            method, including when overrides differ from the stored file.

        Raises:
            FileNotFoundError: No file at the resolved path (and the
                server_url + api_key shortcut was not taken). `main()` catches
                this to print the "run nexus-agent init first" hint.
            json.JSONDecodeError: The file exists but is not valid JSON.
            KeyError: The file is missing the required "server_url" or
                "api_key" keys.

        AI Note: The `tags` list is only ever populated from the file — there
        is no CLI override — so a transient (flags-only) config always
        registers with an empty tag list, which can make the node ineligible
        for tag-targeted scheduling.
        """
        # AI Note: This early return is what makes `--server`/`--api-key` usable
        # on a host with no config file at all. `config_dir` keeps its default
        # here even though nothing is read from or written to it.
        # Full CLI override — no file needed
        if server_url and api_key:
            return cls(
                server_url=server_url,
                api_key=api_key,
                node_id=node_id or (platform.node() or f"node-{uuid.uuid4().hex[:8]}"),
            )

        # Load from file
        path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE
        if not path.exists():
            raise FileNotFoundError(f"Config not found at {path}")

        data = json.loads(path.read_text())
        return cls(
            server_url=server_url or data["server_url"],
            api_key=api_key or data["api_key"],
            node_id=node_id or data.get("node_id", platform.node()),
            # config_dir is derived from where the file actually lives, so a
            # later save() rewrites the same file rather than the default one.
            config_dir=str(path.parent),
            tags=data.get("tags", []),
        )
