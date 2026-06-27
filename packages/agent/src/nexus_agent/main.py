"""Nexus Agent — entry point.

Usage:
    nexus-agent --server ws://localhost:8000/ws/agent --api-key <key>
    nexus-agent --config ~/.nexus-agent/config.json
    nexus-agent init --server ws://localhost:8000/ws/agent --api-key <key>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from nexus_agent import __version__
from nexus_agent.config import AgentConfig

logger = logging.getLogger("nexus.agent")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="nexus-agent",
        description="Nexus compute node agent",
    )
    parser.add_argument(
        "--version", action="version", version=f"nexus-agent {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── init subcommand ──
    init_parser = subparsers.add_parser("init", help="Create agent configuration file")
    init_parser.add_argument("--server", required=True, help="Nexus server WebSocket URL")
    init_parser.add_argument("--api-key", required=True, help="Node API key from server")
    init_parser.add_argument(
        "--node-id", default=None,
        help="Custom node ID (default: hostname)",
    )
    init_parser.add_argument(
        "--config-dir", default=None,
        help="Config directory (default: ~/.nexus-agent)",
    )

    # ── run (default) subcommand ──
    run_parser = subparsers.add_parser("run", help="Start the agent (default)")
    run_parser.add_argument("--server", default=None, help="Nexus server WebSocket URL")
    run_parser.add_argument("--api-key", default=None, help="Node API key")
    run_parser.add_argument("--config", default=None, help="Path to config.json")
    run_parser.add_argument("--node-id", default=None, help="Custom node ID")
    run_parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    return parser.parse_args(argv)


def setup_logging(level: str) -> None:
    """Configure logging for the agent process."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def run_agent(config: AgentConfig) -> None:
    """Main agent event loop: connect, register, heartbeat, execute steps."""
    # Deferred import to keep startup fast
    from nexus_agent.connection import AgentConnection

    connection = AgentConnection(config)
    await connection.run()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for nexus-agent."""
    args = parse_args(argv)

    # Default to "run" when no subcommand given
    command = args.command or "run"

    if command == "init":
        config = AgentConfig.create(
            server_url=args.server,
            api_key=args.api_key,
            node_id=args.node_id,
            config_dir=args.config_dir,
        )
        print(f"Configuration written to {config.config_path}")
        print(f"Node ID: {config.node_id}")
        return

    # ── run ──
    log_level = getattr(args, "log_level", "INFO")
    setup_logging(log_level)

    try:
        config = AgentConfig.load(
            config_path=getattr(args, "config", None),
            server_url=getattr(args, "server", None),
            api_key=getattr(args, "api_key", None),
            node_id=getattr(args, "node_id", None),
        )
    except FileNotFoundError:
        logger.error(
            "No configuration found. Run 'nexus-agent init' first or "
            "pass --server and --api-key."
        )
        sys.exit(1)

    logger.info("Nexus Agent %s starting (node_id=%s)", __version__, config.node_id)
    logger.info("Server: %s", config.server_url)

    try:
        asyncio.run(run_agent(config))
    except KeyboardInterrupt:
        logger.info("Agent stopped by user")


if __name__ == "__main__":
    main()
