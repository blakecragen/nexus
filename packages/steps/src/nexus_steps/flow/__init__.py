"""Control-plane flow steps (``sleep``, ``jump``).

Steps in this package set ``REQUIRES_NODE = False``, which makes the server's
:class:`~nexus_server.runner.runner.JobRunner` execute them locally via
``_execute_local_step`` instead of dispatching them to an agent over the
WebSocket. They therefore run inside the server process and must never block
it: ``sleep`` implements its delay by returning ``RUNNING`` from ``check()``
so the runner's ``await asyncio.sleep(1)`` poll loop yields between polls.

This package is a plain namespace — module registration happens through the
``_STEP_MODULES`` list in :mod:`nexus_steps`.
"""
