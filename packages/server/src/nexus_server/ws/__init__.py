"""``nexus_server.ws`` — reserved namespace for WebSocket infrastructure.

This sub-package is currently **empty and unused**. It exists as a placeholder
for extracting the WebSocket transport layer out of the API router once it
outgrows a single module.

Where the live WebSocket code actually is
-----------------------------------------
All agent and dashboard socket handling lives in
``nexus_server/api/routes/ws.py``:

* ``ConnectionManager`` — process-wide registry of open sockets
  (``node_id -> WebSocket`` for agents, a list for dashboard clients) plus the
  broadcast helpers.
* ``manager`` — the single module-level ``ConnectionManager`` instance. It is
  shared state: :func:`nexus_server.main.lifespan` hands it to the
  :class:`~nexus_server.runner.JobRunner` so the runner can push
  ``execute_step`` commands to agents, and the WS route handler feeds
  ``step.completed`` / ``step.failed`` events back into the runner.
* ``agent_websocket`` / ``_handle_agent_message`` — the ``/ws/agent`` endpoint
  and its message dispatch, keyed off the message types in
  ``nexus_common.agent_protocol``.

AI Note: do not confuse this package with that module. Inside
``nexus_server/main.py`` the bare name ``ws`` is bound to
``nexus_server.api.routes.ws`` (``from nexus_server.api.routes import ... ws``),
*not* to ``nexus_server.ws``. If code is ever moved here, be explicit about the
import path in every call site, and preserve the invariant that exactly one
``ConnectionManager`` instance exists per process — the runner and the socket
handler must observe the same connection table or dispatched steps will hang
waiting on events that are delivered to a different manager.

AI Note: this file is intentionally left with no runtime content. Keep it that
way (docstring only) unless the extraction above actually happens — the empty
module is imported implicitly by nothing, and ``packages/server/pyproject.toml``
ships the whole ``src/nexus_server`` tree, so adding side effects here would
change process startup behaviour for anyone who imports the sub-package.
"""
