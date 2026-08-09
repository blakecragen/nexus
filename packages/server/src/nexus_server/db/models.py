"""SQLAlchemy ORM models for the Nexus database.

Single source of truth for the Nexus schema. Every table in the cluster's
control-plane database is declared here; ``db/ops.py`` is the only module that
should query these classes directly, and the API layer converts them into
Pydantic schemas (``api/schemas.py``) before anything leaves the process.

Domain map (in declaration order, matching the section banners below):

- **Users & Groups** — ``User``, ``Group``, ``UserGroupMembership``. Identity
  plus the group layer that pool permissions hang off.
- **Nodes & Pools** — ``Node`` (an agent machine), ``Pool`` (a named set of
  nodes a job can target), ``PoolNodeMembership``, ``GroupPoolAccess``
  (which groups may submit to which pools).
- **Credentials** — ``Credential``: encrypted secret blobs; decryption lives in
  ``services/credentials/manager.py``, never here.
- **Storage** — ``StorageBackend``: a configured artifact destination (local
  disk, S3, ...) resolved by ``services/storage/manager.py``.
- **Jobs & Steps** — ``Job`` (a submitted workflow) and ``StepRun`` (one
  execution of one step). Driven by ``runner/runner.py`` and
  ``runner/scheduler.py``.
- **Artifacts / Transfers** — ``Artifact`` (a stored file) and
  ``StorageTransfer`` (a backend-to-backend copy).
- **Saved Templates** — ``SavedTemplate``: reusable ``steps_config`` payloads.
- **Audit Log** — ``AuditLog``: append-only record of privileged actions.

Schema-change warning: there are no Alembic revisions. Tables are created by
``Base.metadata.create_all`` at startup, which adds missing *tables* but never
alters existing ones — adding a column here will not appear in an existing
``nexus.db``. See ``db/migrations/__init__.py``.

AI Note: every primary key and foreign key is ``String(36)`` holding a UUID4
*string*, not a native UUID type. That is deliberate for SQLite, which has no
UUID affinity. Callers that pass a ``uuid.UUID`` object into a query will match
nothing (or, historically, 500 the request) — which is exactly why
``ops._sid()`` exists to stringify IDs at the boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Used as the ``default=`` callable for every ``created_at``-style column so
    inserts stamp aware UTC rather than naive local time.

    Returns:
        ``datetime.now(timezone.utc)`` — tz-aware.

    AI Note: awareness matters. SQLite stores no tzinfo, so a value written as
    naive local time reads back as if it were UTC and the frontend renders
    nonsense "in the future" relative timestamps (the historical
    "-17990s ago" bug). Never swap this for ``datetime.utcnow()``, which is
    naive.
    """
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    """Generate a fresh primary key: a UUID4 rendered as a 36-char string.

    Returns:
        e.g. ``"3f2b1c4e-...-9a7d"``; width matches the ``String(36)`` columns.

    AI Note: IDs are generated Python-side, not by the database, so an object
    has its ``id`` before ``flush()``. Keep returning ``str`` — returning a
    ``uuid.UUID`` would break every ``db.get()`` lookup against these columns.
    """
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base shared by all Nexus ORM models.

    ``Base.metadata`` is what ``nexus_server.main.lifespan()`` passes to
    ``create_all`` at startup, and what the test fixtures in
    ``tests/conftest.py`` use to build a throwaway in-memory schema. A model
    class only exists as a table if it subclasses this **and** its module has
    been imported before ``create_all`` runs.
    """

    pass


# ── Users & Groups ──────────────────────────────────────────────────────


class User(Base):
    """A human account that can authenticate and submit jobs.

    Two independent authentication paths exist against this row:
    ``password_hash`` (bcrypt, verified by ``services/auth_service.py`` to mint
    JWTs) and ``api_key`` (an opaque bearer token looked up directly by
    ``ops.get_user_by_api_key``). Both must be treated as secrets and must
    never be serialised into an API response.

    Notable columns:
        role: ``"admin"`` or ``"user"``. ``"admin"`` is a hard bypass in
            ``ops.check_user_pool_access`` — admins reach every pool regardless
            of group membership.
        api_key: Nullable, unique. Generated in ``ops.create_user`` via
            ``secrets.token_urlsafe(32)``.
        is_active: Soft-disable flag.
        last_login_at: Stamped by the auth flow, not by the ORM.

    AI Note: ``is_active`` is stored but is **not** consulted by
    ``check_user_pool_access``; any gating on it has to happen in the auth
    dependency layer. Deactivating a user here does not by itself revoke an
    already-issued JWT.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    api_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    group_memberships: Mapped[list[UserGroupMembership]] = relationship(back_populates="user")
    jobs: Mapped[list[Job]] = relationship(back_populates="submitted_by_user")


class Group(Base):
    """A named set of users; the unit that pool permissions are granted to.

    Authorization chain: ``User`` -> ``UserGroupMembership`` -> ``Group`` ->
    ``GroupPoolAccess`` -> ``Pool``. A non-admin user may submit to a pool only
    if some group they belong to has a ``GroupPoolAccess`` row for it (see
    ``ops.check_user_pool_access``). ``Credential.allowed_groups`` also stores
    group IDs, as a JSON list rather than a foreign key.

    Notable columns:
        name: Unique, human-facing.
        created_by: FK to the creating user; nullable so seeded/system groups
            can exist without an owner.
    """

    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    memberships: Mapped[list[UserGroupMembership]] = relationship(back_populates="group")
    pool_access: Mapped[list[GroupPoolAccess]] = relationship(back_populates="group")


class UserGroupMembership(Base):
    """Join table: which users belong to which groups, and in what capacity.

    Composite primary key ``(user_id, group_id)`` enforces at most one
    membership row per pair — re-adding an existing member raises an
    IntegrityError rather than duplicating (``ops.add_user_to_group`` does not
    upsert).

    Notable columns:
        role_in_group: ``"member"`` by default; a per-group role distinct from
            ``User.role``. Nothing in ``ops`` branches on it today, so treat it
            as forward-looking metadata rather than an enforced permission.
    """

    __tablename__ = "user_group_memberships"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("groups.id"), primary_key=True
    )
    role_in_group: Mapped[str] = mapped_column(String(16), default="member")

    user: Mapped[User] = relationship(back_populates="group_memberships")
    group: Mapped[Group] = relationship(back_populates="memberships")


# ── Nodes & Pools ───────────────────────────────────────────────────────


class Node(Base):
    """A registered agent machine that can execute job steps.

    One row per worker host running the Nexus agent. The agent authenticates
    its WebSocket connection with ``api_key`` (``ops.get_node_by_api_key``);
    the handler in ``api/routes/ws.py`` then flips ``status`` to ``"online"``
    and refreshes ``last_heartbeat`` on every heartbeat frame, and back to
    ``"offline"`` on disconnect. ``runner/scheduler.py`` only ever considers
    nodes with ``status == "online"``.

    Notable columns:
        hostname: Not unique — the same host may legitimately be registered
            twice (e.g. re-registration after a wipe). ``id`` is the identity.
        os_type / os_version / arch / cpu_* / ram_mb / gpu_info: Inventory
            reported by the agent at registration; ``os_type`` is also a
            filter in ``ops.list_nodes``.
        status: ``"offline"`` (default) | ``"online"``. Set by the WS handler,
            never by the agent directly.
        tags: Free-form JSON list used for display/filtering.
        api_key: Node bearer token, ``secrets.token_urlsafe(32)`` from
            ``ops.create_node``. Secret — do not serialise it back to clients.

    AI Note: a node's ``status`` is only corrected when a socket opens or
    closes. If the server process dies while agents are connected, rows stay
    ``"online"`` across the restart until each agent reconnects; a stale
    ``last_heartbeat`` is the reliable liveness signal, not ``status``. There
    is deliberately no "capabilities" column — that concept was removed and
    the scheduler does no capability matching.
    """

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    os_type: Mapped[str] = mapped_column(String(16), nullable=False)
    os_version: Mapped[str | None] = mapped_column(String(64))
    arch: Mapped[str | None] = mapped_column(String(32))
    cpu_model: Mapped[str | None] = mapped_column(String(128))
    cpu_cores: Mapped[int | None] = mapped_column(Integer)
    ram_mb: Mapped[int | None] = mapped_column(Integer)
    gpu_info: Mapped[str | None] = mapped_column(String(255))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    ip_address: Mapped[str | None] = mapped_column(String(45))  # 45 chars = max IPv6 literal
    status: Mapped[str] = mapped_column(String(16), default="offline")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    api_key: Mapped[str | None] = mapped_column(String(64), unique=True)

    pool_memberships: Mapped[list[PoolNodeMembership]] = relationship(back_populates="node")


class Pool(Base):
    """A named collection of nodes that a job can be targeted at.

    Pools are both the scheduling unit (``Job.target_pool_id`` -> scheduler
    picks an online member) and the permission unit (``GroupPoolAccess`` grants
    a group the right to submit here).

    Notable columns:
        name: Unique, user-facing.
        is_default: Marks the fallback pool for submissions that name no
            target.

    AI Note: ``is_default`` is a plain boolean with no uniqueness constraint —
    nothing at the DB level stops two pools being flagged default at once, so
    any "the default pool" lookup must tolerate multiple matches.
    """

    __tablename__ = "pools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    node_memberships: Mapped[list[PoolNodeMembership]] = relationship(back_populates="pool")
    group_access: Mapped[list[GroupPoolAccess]] = relationship(back_populates="pool")


class PoolNodeMembership(Base):
    """Join table: which nodes belong to which pools (many-to-many).

    Composite PK ``(pool_id, node_id)`` means a node can sit in several pools
    but only once per pool. ``ops.get_pool_nodes`` and the ``pool_id`` filter
    in ``ops.list_nodes`` both join through this table.

    AI Note: no ``ON DELETE CASCADE`` is declared, and SQLite does not enforce
    foreign keys unless ``PRAGMA foreign_keys=ON`` is set (it is not). Deleting
    a ``Node`` via ``ops.delete_node`` therefore leaves orphaned membership
    rows pointing at a missing node, which then surface as empty joins.
    """

    __tablename__ = "pool_node_memberships"

    pool_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pools.id"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("nodes.id"), primary_key=True
    )

    pool: Mapped[Pool] = relationship(back_populates="node_memberships")
    node: Mapped[Node] = relationship(back_populates="pool_memberships")


class GroupPoolAccess(Base):
    """Grant: a group may use a pool, at a given permission level.

    The row's mere existence is what ``ops.check_user_pool_access`` tests —
    it joins users -> memberships -> this table and returns True if any row
    matches, without inspecting ``permission``.

    Notable columns:
        permission: ``"submit"`` by default. Stored for future finer-grained
            checks; **not currently enforced anywhere**, so any grant is
            effectively full submit access.

    AI Note: security-relevant. If you introduce a stricter level (e.g.
    ``"read"``), you must also teach ``check_user_pool_access`` to filter on
    it — today adding such a row would silently confer submit rights.
    """

    __tablename__ = "group_pool_access"

    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("groups.id"), primary_key=True
    )
    pool_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pools.id"), primary_key=True
    )
    permission: Mapped[str] = mapped_column(String(16), default="submit")

    group: Mapped[Group] = relationship(back_populates="pool_access")
    pool: Mapped[Pool] = relationship(back_populates="group_access")


# ── Credentials ─────────────────────────────────────────────────────────


class Credential(Base):
    """An encrypted secret bundle (SSH key, S3 keypair, git token, ...).

    Only ciphertext ever lives in this row. ``services/credentials/manager.py``
    validates the plaintext fields against a per-type strategy, serialises
    them, and encrypts the blob with ``FieldEncryptor`` before calling
    ``ops.create_credential``; decryption happens exclusively in that manager.
    The job runner resolves credentials by *name* at step-dispatch time.

    Notable columns:
        credential_type: Selects the strategy (e.g. ``"ssh"``, ``"s3"``,
            ``"git"``). Must match a registered strategy or lookups raise
            ``KeyError``.
        encrypted_fields: Ciphertext blob. Never log, never return over the
            API, never include in an audit-log ``details`` payload.
        is_shared / allowed_groups: Intended sharing controls;
            ``allowed_groups`` is a JSON list of group-ID strings rather than a
            relationship, so there is no referential integrity — a deleted
            group leaves a dangling ID.

    AI Note: security-relevant. ``encrypted_fields`` is only readable with the
    server's ``credential_encryption_key`` from settings. Rotating or losing
    that key makes every stored credential permanently undecryptable — there is
    no key-version column and no re-encryption path.
    """

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False)
    encrypted_fields: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_groups: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Storage ─────────────────────────────────────────────────────────────


class StorageBackend(Base):
    """A configured destination where job artifacts are stored.

    ``services/storage/manager.py`` reads these rows at startup
    (``init_backends``) and instantiates a concrete backend client per row,
    pulling the secret from ``credential_id`` through the credential manager.
    ``Artifact.storage_backend_id`` records where each file actually landed.

    Notable columns:
        backend_type: Discriminator selecting the backend implementation
            (e.g. local filesystem, S3-compatible object store).
        config: Backend-specific JSON (bucket, endpoint, base path, ...). Shape
            is owned by the backend class, not validated here.
        credential_id: FK to the ``Credential`` used to authenticate.
        capacity_bytes: Advisory capacity for reporting; not enforced on write.
        is_default / is_active: ``ops.get_default_storage_backend`` requires
            **both** true, so deactivating the default backend leaves the
            cluster with no default at all.
        priority: Lower sorts first in ``ops.list_storage_backends``.

    AI Note: startup is tolerant by design — ``main.lifespan()`` wraps
    ``init_backends`` in try/except and only logs a warning, so a broken or
    unreachable backend config degrades artifact upload rather than preventing
    the server from booting.
    """

    __tablename__ = "storage_backends"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    backend_type: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("credentials.id")
    )
    capacity_bytes: Mapped[int | None] = mapped_column(BigInteger)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    artifacts: Mapped[list[Artifact]] = relationship(back_populates="storage_backend")


# ── Jobs & Steps ────────────────────────────────────────────────────────


class Job(Base):
    """A submitted workflow: an ordered list of steps plus its execution state.

    Created by ``POST /api/jobs`` (``api/routes/jobs.py`` -> ``ops.create_job``),
    picked up by ``runner/scheduler.py``, and driven to completion by
    ``runner/runner.py``, which dispatches each step to an agent over WebSocket
    and writes progress back here. ``runner/resume.py`` re-adopts unfinished
    jobs after a server restart.

    Notable columns:
        target_pool_id / target_node_id: Both nullable. A node target pins
            execution to one machine; a pool target lets the scheduler choose
            any online member.
        status: ``"pending"`` (default) -> ``"queued"`` -> ``"running"`` ->
            ``"completed"`` | ``"failed"`` | ``"cancelled"``.
            ``ops.get_active_jobs`` treats pending/queued/running as active.
        steps_config: The submitted step list as JSON — the immutable
            definition of the workflow. Mutating it mid-run desynchronises
            ``current_step`` and the existing ``StepRun`` rows.
        current_step: Index into ``steps_config`` of the step in flight.
        context_data: Accumulated ``OUTPUT_KEYS`` from completed steps, used to
            resolve ``${...}`` references in later steps. Rewritten wholesale
            by the runner after each step (``context.outputs``), so it is also
            what a resumed job replays from.
        log_text: Aggregated per-job terminal output, appended incrementally by
            ``ops.append_job_log`` and served by ``GET /api/jobs/{id}/log``.
        storage_target: Optional name of the storage backend artifacts should
            be routed to; ``None`` means use the default backend.

    AI Note: ``status`` and ``current_step`` are written from two directions —
    the runner's own loop and the WebSocket handler reacting to
    ``step.completed`` / ``step.failed`` frames. Both go through
    ``ops.update_job`` with short-lived sessions; there is no row-level lock or
    optimistic version column, so a last-writer-wins race is possible if you
    add a third writer.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    target_pool_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pools.id")
    )
    target_node_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("nodes.id")
    )
    priority: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    steps_config: Mapped[list] = mapped_column(JSON, nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    context_data: Mapped[dict] = mapped_column(JSON, default=dict)
    log_text: Mapped[str | None] = mapped_column(Text)  # aggregated per-job terminal log
    storage_target: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    submitted_by_user: Mapped[User] = relationship(back_populates="jobs")
    step_runs: Mapped[list[StepRun]] = relationship(back_populates="job", order_by="StepRun.step_index")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="job")


class StepRun(Base):
    """One execution attempt of one step of a job.

    Created by ``ops.create_step_run`` just before dispatch and updated as the
    agent reports progress. Carries the per-step audit trail: which node ran
    it, the resolved inputs, the raw state the agent returned, the extracted
    outputs, and timing.

    Notable columns:
        step_index: Position within ``Job.steps_config``. **Not unique** —
            loop constructs re-run the same index, producing several rows; use
            ``ops.get_latest_step_run`` to find the one in flight.
        status: ``"pending"`` (default) -> ``"running"`` -> ``"success"`` |
            ``"failed"``. Note the terminal success value is ``"success"``
            here while ``Job.status`` uses ``"completed"`` — the two
            vocabularies are intentionally different, do not "unify" them
            without updating every comparison.
        node_id: Which node executed it; ``None`` for steps that run
            server-side rather than on an agent.
        input_params: Inputs after ``${...}`` substitution against the job
            context.
        state: The agent's raw returned state dict, kept verbatim for
            debugging.
        output_params: The subset of ``state`` named by the step's
            ``OUTPUT_KEYS``; this is what feeds ``Job.context_data`` for
            downstream steps.

    AI Note: ``ops.get_latest_step_run`` orders by ``StepRun.id`` descending to
    pick the current attempt. Since ``id`` is a random UUID4 string, that
    ordering is arbitrary rather than chronological — see the POSSIBLE BUG note
    in ops.py.
    """

    __tablename__ = "step_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"))
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    node_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("nodes.id"))
    input_params: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[dict | None] = mapped_column(JSON)
    output_params: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[Job] = relationship(back_populates="step_runs")


# ── Artifacts ───────────────────────────────────────────────────────────


class Artifact(Base):
    """Metadata for one file produced by a job and persisted to a backend.

    The bytes live in the storage backend; this row is the index entry that
    lets the API list and download them (``GET /api/jobs/{id}/artifacts`` and
    the results-download endpoint). Rows are written after a successful upload
    — chiefly the gem5 ``m5out`` tarball that agents push back to the server.

    Notable columns:
        step_run_id: Nullable — an artifact may be attributed to the job as a
            whole rather than to a specific step.
        filename: Display name shown in the UI.
        storage_backend_id + storage_key: Together they locate the bytes;
            ``storage_key`` is the backend-internal path/object key and is the
            value actually handed to the backend client on read.
        size_bytes: ``BigInteger`` because result tarballs routinely exceed the
            2 GB signed-32-bit limit.

    AI Note: nothing deletes the underlying object when this row is removed, so
    dropping an ``Artifact`` orphans bytes in the backend. Conversely, the row
    is the only index — a lost row makes the object effectively unreachable
    because ``storage_key`` is not reconstructible from the job.
    """

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"))
    step_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("step_runs.id")
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_backend_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("storage_backends.id")
    )
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="artifacts")
    storage_backend: Mapped[StorageBackend] = relationship(back_populates="artifacts")


# ── Storage Transfers ───────────────────────────────────────────────────


class StorageTransfer(Base):
    """A request to copy one artifact from one storage backend to another.

    Progress record for backend-to-backend migration, created by
    ``ops.create_transfer`` and advanced via ``ops.update_transfer``. Listed by
    the storage routes, filtered on ``status``.

    Notable columns:
        source_backend_id / dest_backend_id: Both FK to ``storage_backends``;
            nothing prevents them being equal.
        status: ``"pending"`` (default) then whatever the transfer worker sets
            (running / completed / failed).
        bytes_transferred: Running progress counter, ``BigInteger`` for
            multi-GB payloads.

    AI Note: this table records intent and progress only — it does **not**
    repoint ``Artifact.storage_backend_id``/``storage_key``. A completed
    transfer that omits that follow-up update leaves the artifact still
    resolving to the source backend.
    """

    __tablename__ = "storage_transfers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    artifact_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifacts.id"))
    source_backend_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("storage_backends.id")
    )
    dest_backend_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("storage_backends.id")
    )
    status: Mapped[str] = mapped_column(String(16), default="pending")
    bytes_transferred: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id")
    )


# ── Saved Templates ────────────────────────────────────────────────────


class SavedTemplate(Base):
    """A reusable ``steps_config`` payload users can re-submit as a new job.

    Pure definition storage — a template has no execution state. The UI copies
    ``steps_config`` into a new ``Job`` at submit time, so editing or deleting
    a template never affects jobs already created from it.

    Notable columns:
        name: **Not unique** (unlike ``Pool.name``/``Credential.name``), so
            duplicate template names are allowed and callers must disambiguate
            by ``id``.
        steps_config: Same JSON shape as ``Job.steps_config``; not validated
            against the step registry until a job is actually submitted.
    """

    __tablename__ = "saved_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    steps_config: Mapped[list] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ── Audit Log ───────────────────────────────────────────────────────────


class AuditLog(Base):
    """Append-only record of a privileged or state-changing action.

    Written through ``ops.create_audit_entry``. Intended to be insert-only:
    nothing in ``ops`` updates or deletes these rows.

    Notable columns:
        user_id: Nullable so system/automated actions can be recorded with no
            actor.
        action: Short verb string, e.g. ``"job.submit"``.
        target_type / target_id: Loose polymorphic pointer at the affected
            entity. Deliberately **not** a foreign key, so the audit trail
            survives deletion of the thing it describes.
        details: Free-form JSON context.

    AI Note: ``details`` is serialised verbatim into the DB and is readable by
    anyone who can read the audit table — never put credential plaintext,
    tokens, or password hashes in it.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict | None] = mapped_column(JSON)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
