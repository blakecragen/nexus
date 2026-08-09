"""Job runner package — orchestrates step execution across distributed agents.

This package is the server-side execution engine for Nexus jobs. It is the only
component that advances a Job through its ``steps_config`` and writes terminal
job/step state to the database.

Modules
-------
``runner``
    :class:`JobRunner` — one asyncio task per job; runs control-plane steps
    in-process and dispatches node-bound steps to agents over WebSocket.
``scheduler``
    :func:`~nexus_server.runner.scheduler.find_node_for_step` — picks which
    online node a remote step should land on (node pin > pool > any online).
``resume``
    :func:`resume_active_jobs` — crash recovery on server startup.

Wiring (who calls what)
-----------------------
- ``nexus_server.main.lifespan`` constructs the single ``JobRunner`` with the
  WebSocket connection manager (``api.routes.ws.manager``) plus the credential
  manager, stashes it on ``app.state.runner``, then calls
  ``resume_active_jobs``.
- ``api/routes/jobs.py`` calls ``runner.submit_job`` / ``runner.cancel_job``.
- ``api/routes/ws.py`` calls back into ``runner.on_step_completed`` /
  ``runner.on_step_failed`` when an agent reports a step's terminal state.

AI Note: exports here are the package's public surface. ``main.py`` imports
both names from ``nexus_server.runner`` (not the submodules), so renaming or
dropping either entry breaks server startup, not just this file.
"""

from nexus_server.runner.runner import JobRunner
from nexus_server.runner.resume import resume_active_jobs

__all__ = ["JobRunner", "resume_active_jobs"]
