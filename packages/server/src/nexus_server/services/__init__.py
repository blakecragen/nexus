"""Server-side service layer — stateful helpers that sit between the HTTP/WS
API and the database.

Everything in this package is *infrastructure* rather than request handling:
services own connections, secrets, and long-lived state, while the routes in
``nexus_server.api.routes`` stay thin and only translate HTTP <-> service calls.

Sub-modules
-----------
- :mod:`nexus_server.services.auth_service`
      JWT issuing/decoding, bcrypt password hashing, and the lookup that turns
      a bearer token into a ``User`` row.
- :mod:`nexus_server.services.provisioner`
      Blocking paramiko/SSH routines that install and start a Nexus agent on a
      remote machine (driven by ``api/routes/nodes.py`` via ``asyncio.to_thread``).
- :mod:`nexus_server.services.credentials`
      Encrypted credential storage plus per-type strategies; every other
      service asks the ``CredentialManager`` for secrets rather than reading
      them from config.
- :mod:`nexus_server.services.storage`
      Pluggable artifact storage backends (S3/MinIO, NAS) and the
      ``StorageManager`` that instantiates and routes between them.

Lifecycle
---------
Singletons for these services are constructed once in
``nexus_server.main.lifespan`` and stashed on ``app.state``; request handlers
receive them through the ``Annotated[..., Depends(...)]`` shortcuts declared in
``nexus_server.api.deps`` (``Auth``, ``CredMgr``, ``StorageMgr``). Because they
are process-wide singletons, anything added here must be safe to share across
concurrent requests.

This ``__init__`` deliberately re-exports nothing: sub-modules pull in heavy or
optional third-party dependencies (paramiko, boto3, bcrypt), so importing the
package should stay cheap and side-effect free. Import concrete symbols from
their own module, e.g. ``from nexus_server.services import provisioner``.
"""
