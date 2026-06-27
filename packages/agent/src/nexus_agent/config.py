"""Agent configuration — persisted to ~/.nexus-agent/config.json.

Supports three ways to load configuration (in priority order):
1. Explicit CLI flags (--server, --api-key, --node-id)
2. Explicit --config path
3. Default path ~/.nexus-agent/config.json
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


DEFAULT_CONFIG_DIR = Path.home() / ".nexus-agent"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"


@dataclass
class AgentConfig:
    """Runtime configuration for the Nexus agent."""

    server_url: str
    api_key: str
    node_id: str = field(default_factory=lambda: platform.node() or f"node-{uuid.uuid4().hex[:8]}")
    config_dir: str = str(DEFAULT_CONFIG_DIR)
    tags: list[str] = field(default_factory=list)

    @property
    def config_path(self) -> Path:
        return Path(self.config_dir) / "config.json"

    # ── Persistence ────────────────────────────────────────────────────

    def save(self) -> Path:
        """Write configuration to disk. Creates parent directories."""
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "server_url": self.server_url,
            "api_key": self.api_key,
            "node_id": self.node_id,
            "tags": self.tags,
        }
        path.write_text(json.dumps(data, indent=2) + "\n")
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
        """Create a new config and persist it to disk."""
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
        """
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
            config_dir=str(path.parent),
            tags=data.get("tags", []),
        )
