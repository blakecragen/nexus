"""Route modules for the Nexus HTTP/WebSocket API.

Every module in this package follows the same shape, and new ones are expected
to as well:

1. A module-level ``router = APIRouter()`` with **no** prefix — the prefix and
   OpenAPI tag are applied once, centrally, by
   ``nexus_server.main.create_app()`` via ``app.include_router(...)``. Moving a
   prefix into a route module would silently double it.
2. Private ``_<model>_to_info(...)`` helpers that map a SQLAlchemy ORM row onto
   the matching Pydantic schema from ``nexus_common.models.schemas``. Keeping
   this mapping explicit (rather than ``from_attributes``) is what stops
   internal columns such as ``Node.api_key``, ``User.password_hash`` or
   ``StepRun.state`` from leaking into API responses.
3. Handlers that receive their session/user/services through the ``Annotated``
   dependency aliases in ``nexus_server.api.deps``.

Current modules and their mount points:

===============  ====================  ====================================
module           prefix                responsibility
===============  ====================  ====================================
``auth``         ``/api/auth``         login, refresh, register, ``/me``
``nodes``        ``/api/nodes``        agent registry, SSH provisioning
``pools``        ``/api/pools``        node grouping + membership
``jobs``         ``/api/jobs``         submit/list/cancel/delete, log, results
``steps``        ``/api/steps``        step registry schema for the job builder
``credentials``  ``/api/credentials``  encrypted secret storage
``storage``      ``/api/storage``      storage backend config + browsing
``artifacts``    ``/api/artifacts``    per-job artifact index
``ws``           (root)                agent and dashboard WebSockets
===============  ====================  ====================================

Two distinct authentication schemes coexist across these modules, and confusing
them is the usual source of unexpected 401s:

* **User JWT** (``Authorization: Bearer <access token>``) — resolved by the
  ``CurrentUser`` / ``AdminUser`` dependencies. Used by the frontend.
* **Node API key** — a per-node secret checked by hand inside the handler
  (``ws.py`` for the agent socket, ``jobs.upload_job_results`` for the results
  tarball ``PUT``). Used by agents, which have no user identity and therefore
  cannot satisfy the JWT dependency.

This file contains no code on purpose: ``nexus_server.main`` imports the
submodules explicitly, so re-exporting them here would only risk import cycles
between the route modules and the app factory.
"""
