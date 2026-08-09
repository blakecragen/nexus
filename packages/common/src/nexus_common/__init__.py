"""Nexus Common — shared types, step base classes, and protocol definitions.

This package is the dependency-free "contract layer" of the Nexus cluster. It is
installed into *every* other package (``nexus-server``, ``nexus-agent``,
``nexus-steps``, ``nexus-cli``) and holds only things that all of them must agree
on. It deliberately imports nothing from those packages, so the dependency graph
stays acyclic: common <- steps <- {server, agent, cli}.

Contents:
    - ``nexus_common.models.enums``    — status/role/type enums persisted in the
      DB and sent over the wire (string-valued, so DB and JSON share one form).
    - ``nexus_common.models.schemas``  — Pydantic request/response models used by
      the FastAPI routes, the CLI client, and the React frontend's generated types.
    - ``nexus_common.agent_protocol``  — the WebSocket message envelope shared by
      the server's ``/ws/agent`` route and the agent's connection loop.
    - ``nexus_common.steps``           — the ``FlowStep`` ABC, validation rules,
      ``StepContext``, and the global ``STEP_REGISTRY``. Concrete steps live in
      the separate ``nexus_steps`` package.
    - ``nexus_common.parser``          — the ``.nexus`` job DSL parser.

AI Note: Anything added here becomes a cross-package ABI. Changing a field name in
a schema or an enum value string is a breaking change for the server, the agent,
persisted DB rows, and the frontend simultaneously — there is no version
negotiation between agent and server, so old agents talking to a new server will
simply fail to deserialize.
"""

__version__ = "0.1.0"
"""Package version. Reported by agents in ``AgentRegister.agent_version`` and
stored on the ``Node`` row, so the dashboard can spot agents running stale code.
Bump this together with any wire-format change in ``agent_protocol``."""
