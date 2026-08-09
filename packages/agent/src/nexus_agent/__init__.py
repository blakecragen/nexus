"""Nexus Agent — lightweight compute node agent.

This package is the worker half of Nexus. It runs as a long-lived process on
each compute node, holds a WebSocket to the Nexus server, and executes the
steps the server's runner dispatches to it.

Module map:
    main.py        CLI entry point (`nexus-agent init` / `nexus-agent run`).
    config.py      Load/persist ~/.nexus-agent/config.json (server URL + API key).
    connection.py  WebSocket lifecycle: connect, register, heartbeat, reconnect.
    executor.py    Runs steps (subprocess or poll-based) and reports results.
    capability.py  Host introspection used to populate the node record on register.
    os_adapters/   Per-platform shell/package-manager/path behavior.

Dependencies flow one way: the agent imports shared step classes and the wire
protocol from `nexus_common` (and step implementations from `nexus_steps`), but
never imports `nexus_server`. Everything the server needs to know about a node
travels over the WebSocket protocol in `nexus_common.agent_protocol`.
"""

# AI Note: This string is the agent's reported `agent_version` in AgentRegister
# and is surfaced in the Nodes UI. It is also the version used by
# `nexus-agent --version`. Bumping it is a protocol-visible change.
__version__ = "0.1.0"
