"""HTTP/WebSocket API layer for the Nexus server.

This package holds everything that turns an incoming request into a call on the
service layer, and nothing else. It is deliberately thin: routes validate input,
resolve dependencies, call into ``nexus_server.db.ops`` / the service singletons,
and shape the result into a ``nexus_common.models.schemas`` response model.
Business logic belongs in ``nexus_server.runner`` / ``nexus_server.services``,
persistence in ``nexus_server.db``.

Contents
    deps
        FastAPI dependency-injection helpers plus the ``Annotated`` shortcuts
        (``DbSession``, ``CurrentUser``, ``AdminUser``, ``Auth``, ``Runner``,
        ``CredMgr``, ``StorageMgr``) that every route module imports. User auth
        is JWT-bearer based; the service singletons are pulled off
        ``app.state``, which ``nexus_server.main``'s lifespan handler populates
        at startup.
    routes
        One module per resource (auth, nodes, pools, jobs, steps, credentials,
        storage, artifacts, ws). Each exposes a module-level ``router``.

Wiring: ``nexus_server.main.create_app()`` imports each ``routes.<name>.router``
and mounts it under ``/api/<name>`` (the WebSocket router is mounted at the
root). Because the prefix lives in ``main.py`` and not in the route modules,
paths declared here are relative — e.g. ``@router.get("")`` in ``jobs.py``
serves ``GET /api/jobs``.

This module intentionally contains no code so that importing
``nexus_server.api`` never drags in the DB engine or the service layer; import
the specific submodule you need instead.
"""
