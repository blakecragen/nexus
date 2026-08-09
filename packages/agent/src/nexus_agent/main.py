"""Nexus Agent — entry point.

Usage:
    nexus-agent --server ws://localhost:8000/ws/agent --api-key <key>
    nexus-agent --config ~/.nexus-agent/config.json
    nexus-agent init --server ws://localhost:8000/ws/agent --api-key <key>

This module is the console-script target (`nexus-agent`) declared in
packages/agent/pyproject.toml. It owns argument parsing, logging setup, and
process lifetime only — all real work lives downstream:

    main() → AgentConfig.create()/load()   (nexus_agent.config)
           → run_agent() → AgentConnection.run()  (nexus_agent.connection)
           → StepExecutor                          (nexus_agent.executor)

Two subcommands:
    init  Write ~/.nexus-agent/config.json and exit. Run once per node,
          typically by scripts/add_node.sh or the server's provisioner.
    run   (default when no subcommand is given) Start the long-lived agent
          loop; it never returns until interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from nexus_agent import __version__
from nexus_agent.config import AgentConfig

logger = logging.getLogger("nexus.agent")
"""Root logger for the agent process; submodules log under `nexus.agent.*`."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments.

    Args:
        argv: Argument list to parse; `None` means read from `sys.argv`.
            Passing an explicit list makes this testable.

    Returns:
        A namespace whose `command` attribute is "init", "run", or `None`
        (no subcommand given, which `main()` treats as "run").

    Raises:
        SystemExit: argparse exits the process on `--help`, `--version`, or
            an invalid argument. Callers in tests must expect this.

    AI Note: `run`'s options are only defined on the `run` subparser, not the
    top-level parser. The documented bare form `nexus-agent --server ... `
    (no subcommand) therefore fails argparse — `--server` is unrecognized at
    the top level. `main()` compensates with `getattr(args, ..., None)` for
    the no-subcommand path, but the flags themselves must follow an explicit
    `run`.
    """
    parser = argparse.ArgumentParser(
        prog="nexus-agent",
        description="Nexus compute node agent",
    )
    parser.add_argument(
        "--version", action="version", version=f"nexus-agent {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── init subcommand ──
    # One-shot: writes config.json (including the API key) and exits.
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
    # All options default to None so AgentConfig.load() can distinguish
    # "not supplied" (fall back to the config file) from an explicit value.
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
    """Configure logging for the agent process.

    Installs a root stderr handler with a timestamped format shared by all
    `nexus.agent.*` loggers.

    Args:
        level: One of DEBUG/INFO/WARNING/ERROR (argparse already constrains
            the value); matched case-insensitively.

    Side effects:
        Mutates global logging state. `basicConfig` is a no-op if the root
        logger already has handlers, so calling this after another library has
        configured logging silently does nothing.

    Raises:
        AttributeError: If `level` is not a valid `logging` level name — only
            reachable when called directly rather than through argparse.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def run_agent(config: AgentConfig) -> None:
    """Main agent event loop: connect, register, heartbeat, execute steps.

    Thin async wrapper that constructs the connection and hands off to it.

    Args:
        config: Fully resolved agent configuration.

    Returns:
        Only when `AgentConnection.run()` exits — i.e. after `stop()` or
        cancellation. Under normal operation this coroutine runs forever,
        reconnecting internally rather than propagating network errors.
    """
    # AI Note: Deferred import on purpose. `connection` transitively imports the
    # executor, which imports `nexus_steps` to populate STEP_REGISTRY — an
    # expensive import chain that `nexus-agent init` and `--version` must not pay.
    # Deferred import to keep startup fast
    from nexus_agent.connection import AgentConnection

    connection = AgentConnection(config)
    await connection.run()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for nexus-agent.

    Dispatches to `init` (write config and return) or `run` (block in the
    asyncio event loop until interrupted).

    Args:
        argv: Argument list; `None` reads `sys.argv`.

    Side effects:
        `init` writes config.json to disk and prints its path plus the node
        id to stdout. `run` configures global logging and starts an asyncio
        event loop that opens a WebSocket and spawns subprocesses.

    Raises:
        SystemExit: With code 1 when no configuration can be resolved, and
            via argparse for `--help`/`--version`/bad arguments.

    AI Note: `init` runs before `setup_logging()`, so it reports via `print`
    rather than the logger — any failure inside `AgentConfig.create()`
    surfaces as an unhandled traceback rather than a formatted log line.
    """
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
    # AI Note: getattr-with-default throughout this block because `args` may
    # come from the bare no-subcommand invocation, where the run subparser
    # never ran and none of its attributes exist on the namespace.
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

    # AI Note: Logs the server URL but never the API key — server_url carries
    # only the node id in its path, while the key is appended as a query
    # parameter later, inside connection._build_url().
    logger.info("Nexus Agent %s starting (node_id=%s)", __version__, config.node_id)
    logger.info("Server: %s", config.server_url)

    try:
        asyncio.run(run_agent(config))
    except KeyboardInterrupt:
        # Ctrl-C is a normal shutdown path: asyncio.run() cancels the
        # connection task, so exit quietly with status 0.
        logger.info("Agent stopped by user")


if __name__ == "__main__":
    main()
