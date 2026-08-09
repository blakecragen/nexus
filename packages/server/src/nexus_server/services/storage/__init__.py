"""Artifact storage subsystem — pluggable backends behind one async interface.

Nexus jobs produce artifacts (logs, tarballs, gem5 ``m5out`` bundles, ...) that
must land somewhere durable and may later be moved between tiers (fast object
store -> cheap NAS, or vice versa). This package provides that indirection.

Layout
------
- :mod:`~nexus_server.services.storage.base`
      ``StorageBackendBase`` (the ABC every backend implements) and
      ``StorageRef`` (the value object returned by uploads). Also supplies the
      default cross-backend ``stream_to`` implementation.
- :mod:`~nexus_server.services.storage.minio_backend`
      ``MinIOBackend`` — boto3/S3 API; used for MinIO, AWS S3, B2, Wasabi, ...
- :mod:`~nexus_server.services.storage.nas_backend`
      ``NASBackend`` — a locally mounted filesystem path.
- :mod:`~nexus_server.services.storage.manager`
      ``StorageManager`` — reads ``storage_backends`` rows from the DB, resolves
      each one's credential through the ``CredentialManager``, constructs the
      matching backend instance, and exposes upload/download/transfer helpers
      that also write the ``artifacts`` / ``storage_transfers`` bookkeeping rows.

Wiring
------
``nexus_server.main.lifespan`` builds a single ``StorageManager`` and calls
``init_backends()`` once at startup; ``api/routes/storage.py`` reaches it via
the ``StorageMgr`` dependency declared in ``nexus_server.api.deps``.

Adding a backend
----------------
Subclass ``StorageBackendBase``, implement every ``@abstractmethod``, then
register the class in ``manager.BACKEND_CLASSES`` *and* add a construction
branch in ``StorageManager._create_backend_instance`` (the dict alone is not
enough — see the note in that method).

This ``__init__`` intentionally re-exports nothing: importing the package must
not drag boto3 (or any other backend-specific dependency) into unrelated import
paths. Import the concrete symbol from its own module.
"""
