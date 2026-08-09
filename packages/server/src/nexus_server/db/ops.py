"""Repository pattern — public database operations API.

All callers import from here. Internal models and session management are
implementation details that can be swapped without breaking consumers.

Following the HVE-Automation-Worker pattern: ops.py is the ONLY stable
public interface to the database.

Consumers: the FastAPI routes under ``api/routes/`` (each gets an
``AsyncSession`` from the ``db.session.get_session`` dependency), the job
runner and scheduler under ``runner/``, and the service layer under
``services/`` (credential manager, storage manager). Nothing outside
``nexus_server.db`` should import ``db.models`` or build its own queries.

House rules for every function in this module:

- **Signature shape.** The first parameter is always the ``AsyncSession``. It
  is caller-owned: this module never opens or closes sessions, only uses them.
- **Transactions.** Almost every mutating helper ends in ``await db.commit()``,
  so each call is its own transaction. There is no way to compose several ops
  calls into one atomic unit — if you need that, write a new function rather
  than removing an existing commit (routes rely on the current boundaries).
  ``expire_on_commit=False`` on the session factory is what makes returning a
  live ORM object after commit safe.
- **Return conventions.** Getters return ``None`` when the row is missing (no
  raising); ``delete_*`` returns ``bool`` for found-and-deleted; ``update_*``
  returns the refreshed object or ``None`` if the ID did not exist. Routes turn
  those ``None``s into 404s.
- **``**kwargs`` updaters.** ``update_node`` / ``update_job`` /
  ``update_step_run`` / ``update_credential`` / ``update_transfer`` /
  ``update_user`` blind-``setattr`` whatever they are given. A typo'd key sets
  a junk Python attribute that is silently dropped instead of erroring, and
  nothing validates values against the column types. Only pass real column
  names.

AI Note: the ``UUID`` type hints on ``*_id`` parameters are aspirational. The
schema stores IDs as ``String(36)``, so every value handed to SQLAlchemy must
be a ``str`` — see :func:`_sid` and :func:`_sid_kwargs`. Every ID-taking
function in this module now coerces, so passing a ``uuid.UUID`` is safe in any
id position, including a caller-supplied primary key.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_server.db.models import (
    Artifact,
    AuditLog,
    Credential,
    Group,
    GroupPoolAccess,
    Job,
    Node,
    Pool,
    PoolNodeMembership,
    SavedTemplate,
    StepRun,
    StorageBackend,
    StorageTransfer,
    User,
    UserGroupMembership,
)


def _sid(val) -> str | None:
    """Coerce a UUID or string to a plain string for SQLite compatibility.

    Args:
        val: A ``uuid.UUID``, a string, or ``None``.

    Returns:
        ``str(val)``, or ``None`` if ``val`` is ``None`` (so it can be passed
        straight into a nullable column or an ``IS NULL`` comparison).

    AI Note: this exists because of a real production bug. All ID columns are
    ``String(36)``; handing ``db.get()`` or a ``==`` filter a ``uuid.UUID``
    object made aiosqlite raise on bind, which 500'd the request and — in the
    agent WebSocket handler — killed the socket on every step message, causing
    a reconnect storm and stuck jobs. Wrap every ID that reaches a query with
    this. It is intentionally permissive (no UUID validation) so that already-
    string IDs pass through untouched.
    """
    return str(val) if val is not None else None


def _sid_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return ``kwargs`` with every ID-shaped value coerced via :func:`_sid`.

    Args:
        kwargs: Column-name -> value mapping bound for a model constructor or
            a blind-``setattr`` updater.

    Returns:
        A new dict; entries whose key ends in ``_id`` or ``_by`` are
        stringified, everything else is passed through untouched.

    AI Note: this exists because the ``**kwargs`` creators/updaters hand their
    mapping straight to SQLAlchemy, so a route that declares a parameter as
    ``UUID`` used to bind a ``uuid.UUID`` onto a ``String(36)`` column and crash
    aiosqlite with "type 'UUID' is not supported" -> 500 (and poison the session
    with ``PendingRollbackError``). Coercing here fixes every current and future
    caller at once instead of at each call site.

    Both suffixes are load-bearing. An earlier version matched only ``_id`` and
    silently missed ``Artifact.uploaded_by`` and ``StorageTransfer.requested_by``
    — both ``String(36)`` FKs to ``users.id`` — so ``uploaded_by=UUID(...)``
    still crashed, and for artifacts it crashed *after* the bytes were already
    written to the backend, orphaning them with no row. The bare ``"id"`` key is
    matched explicitly because a caller-supplied primary key (e.g.
    ``create_node(id=...)``) has neither suffix. The match is name-based rather
    than type-based so that a plain string ID passes through untouched and no
    isinstance ladder is needed; a non-ID column ending in ``_id``/``_by`` would
    be stringified too, which is acceptable because the project uses those
    suffixes exclusively for ID columns.
    """
    return {
        k: (_sid(v) if k == "id" or k.endswith(("_id", "_by")) else v)
        for k, v in kwargs.items()
    }


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Returns:
        ``datetime.now(timezone.utc)``.

    AI Note: must stay tz-aware. SQLite drops tzinfo on write, so a naive local
    timestamp reads back as if it were UTC and the frontend renders negative
    "time ago" values. Do not substitute ``datetime.utcnow()``.
    """
    return datetime.now(timezone.utc)


# ── Users ───────────────────────────────────────────────────────────────


async def create_user(
    db: AsyncSession, username: str, password_hash: str, email: str | None = None,
    role: str = "user",
) -> User:
    """Insert a new user and mint their API key.

    Called by the signup/admin-create route and by ``main.lifespan()`` to seed
    the default ``admin`` account on an empty database.

    Args:
        db: Caller-owned session.
        username: Unique login name; a collision raises ``IntegrityError``
            (this function does not pre-check).
        password_hash: **Already hashed** — pass the output of
            ``AuthService.hash_password``. This function never hashes.
        email: Optional contact address; not verified or required unique.
        role: ``"user"`` (default) or ``"admin"``. ``"admin"`` bypasses all
            pool access checks, so never take this straight from unauthenticated
            request input.

    Returns:
        The persisted, refreshed :class:`User`, including its generated ``id``
        and ``api_key``.

    Side effects:
        Commits. Generates a cryptographically random API key with
        ``secrets.token_urlsafe(32)``.

    AI Note: the returned object carries the plaintext ``api_key`` and the
    ``password_hash``; it is the caller's job to strip both before serialising.
    """
    user = User(
        username=username, password_hash=password_hash, email=email,
        role=role, api_key=secrets.token_urlsafe(32),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """Look up a user by their unique login name.

    Primary lookup for password login and for the startup check that decides
    whether the seed ``admin`` account already exists.

    Args:
        db: Caller-owned session.
        username: Exact match; the comparison is case-sensitive on SQLite for
            non-ASCII and depends on column collation otherwise.

    Returns:
        The :class:`User`, or ``None`` if no such username exists.
    """
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    """Fetch a user by primary key.

    Args:
        db: Caller-owned session.
        user_id: UUID or string; stringified via :func:`_sid` before lookup.

    Returns:
        The :class:`User`, or ``None`` if not found.

    Note:
        Uses ``db.get``, so an instance already in the session's identity map
        is returned without a round trip — it may be stale relative to writes
        made by another session.
    """
    return await db.get(User, _sid(user_id))


async def get_user_by_api_key(db: AsyncSession, api_key: str) -> User | None:
    """Resolve a bearer API key to its owning user.

    Used by the auth dependency for non-JWT (machine/CLI) requests.

    Args:
        db: Caller-owned session.
        api_key: The opaque token presented by the client.

    Returns:
        The matching :class:`User`, or ``None`` if the key is unknown.

    AI Note: security-relevant. This is a plain equality match against a
    column that stores the key in the clear, and it does **not** check
    ``User.is_active`` — deactivating a user does not invalidate their API key
    here, so any active/inactive gating must happen in the caller.
    """
    result = await db.execute(select(User).where(User.api_key == api_key))
    return result.scalar_one_or_none()


async def list_users(db: AsyncSession) -> list[User]:
    """Return every user, ordered by username.

    Args:
        db: Caller-owned session.

    Returns:
        All :class:`User` rows, alphabetical by ``username``.

    AI Note: unpaginated by design (admin screen, small table). If the user
    count ever grows, add limit/offset here rather than slicing in the route.
    """
    result = await db.execute(select(User).order_by(User.username))
    return list(result.scalars().all())


async def update_user(db: AsyncSession, user_id: UUID, **kwargs: Any) -> User | None:
    """Apply arbitrary column updates to a user.

    Args:
        db: Caller-owned session.
        user_id: Primary key of the user to update.
        **kwargs: Column-name -> new-value pairs, applied with ``setattr``.
            Typical keys: ``last_login_at``, ``is_active``, ``role``,
            ``password_hash``.

    Returns:
        The refreshed :class:`User`, or ``None`` if no such user exists.

    Side effects:
        Commits.

    AI Note: ``user_id`` is coerced with :func:`_sid` and ``**kwargs`` through
    :func:`_sid_kwargs`, so a ``uuid.UUID`` is safe in either position. Both
    were previously raw and 500'd on bind.
    """
    user = await db.get(User, _sid(user_id))
    if not user:
        return None
    # AI Note: _sid_kwargs before the blind setattr — these updaters accept
    # arbitrary column names, so a *_id / *_by kwarg holding a uuid.UUID
    # would bind raw onto a String(36) column and crash aiosqlite.
    for k, v in _sid_kwargs(kwargs).items():
        setattr(user, k, v)
    await db.commit()
    await db.refresh(user)
    return user


# ── Groups ──────────────────────────────────────────────────────────────


async def create_group(
    db: AsyncSession, name: str, created_by: UUID, description: str | None = None,
) -> Group:
    """Create a new group.

    Args:
        db: Caller-owned session.
        name: Unique group name; a duplicate raises ``IntegrityError``.
        created_by: ID of the creating user, stored for provenance only.
        description: Optional free text.

    Returns:
        The persisted, refreshed :class:`Group`.

    Side effects:
        Commits.

    AI Note: ``created_by`` is coerced with :func:`_sid`. The column is
    nullable and SQLite does not enforce foreign keys here, so a *well-formed
    but unknown* user id is accepted silently rather than rejected.
    """
    # AI Note: _sid() on every ID — the column is String(36) and a raw
    # uuid.UUID crashes aiosqlite on bind (500 + poisoned session).
    group = Group(name=name, description=description, created_by=_sid(created_by))
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def list_groups(db: AsyncSession) -> list[Group]:
    """Return every group, ordered by name.

    Args:
        db: Caller-owned session.

    Returns:
        All :class:`Group` rows, alphabetical by ``name``. Unpaginated.
    """
    result = await db.execute(select(Group).order_by(Group.name))
    return list(result.scalars().all())


async def add_user_to_group(
    db: AsyncSession, user_id: UUID, group_id: UUID, role_in_group: str = "member",
) -> UserGroupMembership:
    """Add a user to a group.

    Args:
        db: Caller-owned session.
        user_id: The user to add.
        group_id: The group to add them to.
        role_in_group: Per-group role label, ``"member"`` by default. Stored
            but not currently consulted by any permission check.

    Returns:
        The new :class:`UserGroupMembership`.

    Raises:
        sqlalchemy.exc.IntegrityError: If the pair already exists — the
            composite primary key ``(user_id, group_id)`` forbids duplicates
            and this function does **not** upsert (contrast
            :func:`set_group_pool_access`, which does).

    Side effects:
        Commits. Grants the user every pool permission the group holds, so
        this call is effectively a privilege grant.

    AI Note: no ``db.refresh`` here — the composite-PK row has no
    server-generated columns to read back, so the returned object is fine as
    is.
    """
    # AI Note: _sid() on every ID — the column is String(36) and a raw
    # uuid.UUID crashes aiosqlite on bind (500 + poisoned session).
    membership = UserGroupMembership(
        user_id=_sid(user_id), group_id=_sid(group_id), role_in_group=role_in_group,
    )
    db.add(membership)
    await db.commit()
    return membership


async def remove_user_from_group(db: AsyncSession, user_id: UUID, group_id: UUID) -> None:
    """Revoke a user's membership of a group.

    Args:
        db: Caller-owned session.
        user_id: The user to remove.
        group_id: The group to remove them from.

    Returns:
        ``None`` — deleting a membership that does not exist is a silent no-op,
        so the caller cannot distinguish "removed" from "was not a member".

    Side effects:
        Commits. Immediately withdraws any pool access the user had solely
        through this group.
    """
    await db.execute(
        delete(UserGroupMembership).where(
            UserGroupMembership.user_id == _sid(user_id),
            UserGroupMembership.group_id == _sid(group_id),
        )
    )
    await db.commit()


async def set_group_pool_access(
    db: AsyncSession, group_id: UUID, pool_id: UUID, permission: str = "submit",
) -> GroupPoolAccess:
    """Grant (or re-level) a group's access to a pool. Idempotent upsert.

    Args:
        db: Caller-owned session.
        group_id: Group receiving access.
        pool_id: Pool being granted.
        permission: Level string, ``"submit"`` by default. **Stored but not
            enforced** — ``check_user_pool_access`` only tests row existence,
            so any value here confers full submit access today.

    Returns:
        The created or updated :class:`GroupPoolAccess` row.

    Side effects:
        Commits. Grants pool access to every current member of the group.

    AI Note: the select-then-insert upsert is not atomic. Two concurrent calls
    for the same ``(group_id, pool_id)`` can both miss and then both insert,
    with the loser raising ``IntegrityError`` on the composite PK. Acceptable
    for an admin-triggered action; do not reuse this pattern on a hot path.
    """
    # Upsert
    existing = await db.execute(
        select(GroupPoolAccess).where(
            GroupPoolAccess.group_id == _sid(group_id),
            GroupPoolAccess.pool_id == _sid(pool_id),
        )
    )
    access = existing.scalar_one_or_none()
    if access:
        access.permission = permission
    else:
        # AI Note: the SELECT/UPDATE branches above already coerce; this
        # INSERT branch did not, so only a *first-time* grant crashed.
        access = GroupPoolAccess(
            group_id=_sid(group_id), pool_id=_sid(pool_id), permission=permission,
        )
        db.add(access)
    await db.commit()
    return access


async def check_user_pool_access(db: AsyncSession, user_id: UUID, pool_id: UUID) -> bool:
    """Check if user has access to a pool (admin bypasses, otherwise checks group membership).

    This is the authorization gate for job submission: the jobs route calls it
    before creating a job targeted at a pool.

    Args:
        db: Caller-owned session.
        user_id: The requesting user.
        pool_id: The pool they want to use.

    Returns:
        ``True`` if the user is an admin, or if any group they belong to has a
        :class:`GroupPoolAccess` row for this pool. ``False`` otherwise —
        including when the user does not exist (fail closed).

    AI Note: security-relevant, read carefully before changing.
    1. ``user.role == "admin"`` is an unconditional bypass — an admin reaches
       every pool with no grant rows at all.
    2. The query tests only row *existence*; ``GroupPoolAccess.permission`` is
       never inspected, so introducing a read-only level requires editing this
       function or it will silently behave as submit access.
    3. ``user_id`` is coerced with :func:`_sid` before the lookup, matching the
       join below. Before that fix a ``uuid.UUID`` argument raised on bind (or
       missed the row), denying access rather than granting it.
    """
    user = await db.get(User, _sid(user_id))
    if not user:
        return False
    if user.role == "admin":
        return True
    result = await db.execute(
        select(GroupPoolAccess)
        .join(UserGroupMembership, UserGroupMembership.group_id == GroupPoolAccess.group_id)
        .where(
            UserGroupMembership.user_id == _sid(user_id),
            GroupPoolAccess.pool_id == _sid(pool_id),
        )
    )
    return result.first() is not None


# ── Nodes ───────────────────────────────────────────────────────────────


async def create_node(db: AsyncSession, **kwargs: Any) -> Node:
    """Register a new agent node and mint its API key.

    Args:
        db: Caller-owned session.
        **kwargs: :class:`Node` column values. ``hostname`` and ``os_type`` are
            NOT NULL and must be supplied; everything else (inventory fields,
            ``tags``, ``display_name``) is optional. Do **not** pass
            ``api_key`` — it is generated here and a duplicate keyword raises
            ``TypeError``.

    Returns:
        The persisted, refreshed :class:`Node`, including its ``id`` and the
        freshly generated ``api_key``.

    Side effects:
        Commits. Generates the node's bearer token via
        ``secrets.token_urlsafe(32)``.

    AI Note: the plaintext ``api_key`` on the returned object is the *only*
    time the token is available to hand to the operator (it is stored as-is but
    should never be echoed back on later reads). ``hostname`` is not unique, so
    re-running registration for the same machine creates a second node with a
    second key rather than rotating the first.
    """
    node = Node(api_key=secrets.token_urlsafe(32), **_sid_kwargs(kwargs))
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return node


async def get_node_by_id(db: AsyncSession, node_id: UUID) -> Node | None:
    """Fetch a node by primary key.

    Args:
        db: Caller-owned session.
        node_id: UUID or string; stringified via :func:`_sid`.

    Returns:
        The :class:`Node`, or ``None`` if not found.
    """
    return await db.get(Node, _sid(node_id))


async def get_node_by_api_key(db: AsyncSession, api_key: str) -> Node | None:
    """Authenticate an agent by its node API key.

    Called on every agent WebSocket connect and on the agent->server artifact
    upload path.

    Args:
        db: Caller-owned session.
        api_key: Token presented by the agent.

    Returns:
        The owning :class:`Node`, or ``None`` if the key is unknown.

    AI Note: security-relevant. Plain equality against a cleartext column, with
    no rate limiting and no expiry — a node key is a permanent credential until
    the row is deleted or the column overwritten.
    """
    result = await db.execute(select(Node).where(Node.api_key == api_key))
    return result.scalar_one_or_none()


async def list_nodes(
    db: AsyncSession,
    os_type: str | None = None,
    status: str | None = None,
    pool_id: UUID | None = None,
) -> list[Node]:
    """List nodes with optional filters, ordered by hostname.

    The scheduler uses this with ``status="online"`` to find dispatch
    candidates; the UI uses the other filters for the Nodes page.

    Args:
        db: Caller-owned session.
        os_type: Exact match on ``Node.os_type`` when provided.
        status: Exact match on ``Node.status`` (``"online"`` / ``"offline"``).
        pool_id: Restrict to members of this pool by joining
            ``pool_node_memberships``.

    Returns:
        Matching :class:`Node` rows sorted by ``hostname``. Unpaginated.

    AI Note: filters are applied only when truthy, so ``status=""`` behaves as
    "no filter" rather than "match empty status". That is intentional for
    optional query params but means a falsy sentinel can never be filtered on.
    """
    query = select(Node)
    if os_type:
        query = query.where(Node.os_type == os_type)
    if status:
        query = query.where(Node.status == status)
    if pool_id:
        query = query.join(PoolNodeMembership).where(PoolNodeMembership.pool_id == _sid(pool_id))
    query = query.order_by(Node.hostname)
    result = await db.execute(query)
    return list(result.scalars().all())


async def update_node(db: AsyncSession, node_id: UUID, **kwargs: Any) -> Node | None:
    """Apply arbitrary column updates to a node.

    The hottest write in the system: the WebSocket handler calls it on every
    agent heartbeat with ``last_heartbeat`` and ``status``.

    Args:
        db: Caller-owned session.
        node_id: Primary key of the node.
        **kwargs: Column-name -> value pairs applied with ``setattr``; typical
            keys are ``status``, ``last_heartbeat``, ``ip_address``,
            ``agent_version``, ``tags``.

    Returns:
        The refreshed :class:`Node`, or ``None`` if no such node exists (e.g.
        a heartbeat from a node that was deleted server-side).

    Side effects:
        Commits, once per call — so one transaction per heartbeat per node.

    AI Note: this was one of the two call sites fixed by :func:`_sid`; passing
    a raw ``uuid.UUID`` here used to raise inside the WS handler and tear down
    the agent connection.
    """
    node = await db.get(Node, _sid(node_id))
    if not node:
        return None
    # AI Note: _sid_kwargs before the blind setattr — these updaters accept
    # arbitrary column names, so a *_id / *_by kwarg holding a uuid.UUID
    # would bind raw onto a String(36) column and crash aiosqlite.
    for k, v in _sid_kwargs(kwargs).items():
        setattr(node, k, v)
    await db.commit()
    await db.refresh(node)
    return node


async def delete_node(db: AsyncSession, node_id: UUID) -> bool:
    """Delete a node registration.

    Args:
        db: Caller-owned session.
        node_id: Primary key of the node.

    Returns:
        ``True`` if a node was found and deleted, ``False`` if the ID did not
        exist (routes map ``False`` to a 404).

    Side effects:
        Commits. Invalidates the node's API key, so a still-running agent will
        fail to reconnect.

    AI Note: leaves dependent rows behind. ``pool_node_memberships`` and
    ``step_runs.node_id`` still reference the deleted ID — there is no cascade
    declared and SQLite foreign keys are not enforced, so historical step runs
    keep a dangling node reference by design (the audit trail survives) while
    pool memberships become genuine orphans.
    """
    node = await db.get(Node, _sid(node_id))
    if not node:
        return False
    await db.delete(node)
    await db.commit()
    return True


# ── Pools ───────────────────────────────────────────────────────────────


async def create_pool(
    db: AsyncSession, name: str, created_by: UUID, description: str | None = None,
) -> Pool:
    """Create an empty node pool.

    Args:
        db: Caller-owned session.
        name: Unique pool name; duplicates raise ``IntegrityError``.
        created_by: ID of the creating user (provenance only).
        description: Optional free text.

    Returns:
        The persisted, refreshed :class:`Pool`. It has no nodes and no group
        grants yet, so nothing can be scheduled onto it until
        :func:`add_node_to_pool` and :func:`set_group_pool_access` are called.

    Side effects:
        Commits.
    """
    # AI Note: _sid() on every ID — the column is String(36) and a raw
    # uuid.UUID crashes aiosqlite on bind (500 + poisoned session).
    pool = Pool(name=name, description=description, created_by=_sid(created_by))
    db.add(pool)
    await db.commit()
    await db.refresh(pool)
    return pool


async def get_pool_by_id(db: AsyncSession, pool_id: UUID) -> Pool | None:
    """Fetch a pool by primary key.

    Args:
        db: Caller-owned session.
        pool_id: UUID or string; stringified via :func:`_sid`.

    Returns:
        The :class:`Pool`, or ``None`` if not found.
    """
    return await db.get(Pool, _sid(pool_id))


async def list_pools(db: AsyncSession) -> list[Pool]:
    """Return every pool, ordered by name.

    Args:
        db: Caller-owned session.

    Returns:
        All :class:`Pool` rows, alphabetical by ``name``. No access filtering
        is applied — the caller decides which pools a given user may see.
    """
    result = await db.execute(select(Pool).order_by(Pool.name))
    return list(result.scalars().all())


async def add_node_to_pool(db: AsyncSession, pool_id: UUID, node_id: UUID) -> PoolNodeMembership:
    """Make a node a member of a pool.

    Args:
        db: Caller-owned session.
        pool_id: Target pool.
        node_id: Node to add. A node may belong to several pools.

    Returns:
        The new :class:`PoolNodeMembership`.

    Raises:
        sqlalchemy.exc.IntegrityError: If the node is already in this pool —
            ``(pool_id, node_id)`` is the composite primary key and this
            function does not upsert. Callers that may re-add should check
            first or catch.

    Side effects:
        Commits. Immediately makes the node a scheduling candidate for jobs
        targeting the pool.
    """
    membership = PoolNodeMembership(pool_id=_sid(pool_id), node_id=_sid(node_id))
    db.add(membership)
    await db.commit()
    return membership


async def remove_node_from_pool(db: AsyncSession, pool_id: UUID, node_id: UUID) -> None:
    """Remove a node from a pool.

    Args:
        db: Caller-owned session.
        pool_id: Pool to remove from.
        node_id: Node to remove.

    Returns:
        ``None``; removing a non-existent membership is a silent no-op.

    Side effects:
        Commits.

    AI Note: this does not touch jobs already dispatched to that node. A step
    currently running there keeps running and still reports back — removal only
    affects future scheduling decisions.
    """
    await db.execute(
        delete(PoolNodeMembership).where(
            PoolNodeMembership.pool_id == _sid(pool_id),
            PoolNodeMembership.node_id == _sid(node_id),
        )
    )
    await db.commit()


async def get_pool_nodes(db: AsyncSession, pool_id: UUID) -> list[Node]:
    """Return every node in a pool.

    Args:
        db: Caller-owned session.
        pool_id: The pool to expand.

    Returns:
        The member :class:`Node` rows in unspecified order — **including
        offline ones**. Schedulers must filter on ``status``/``last_heartbeat``
        themselves (or use ``list_nodes(pool_id=..., status="online")``).

    AI Note: unlike :func:`list_nodes` this applies no ``order_by``, so result
    ordering is whatever SQLite returns. Do not rely on it for round-robin or
    any deterministic node selection.
    """
    result = await db.execute(
        select(Node).join(PoolNodeMembership).where(PoolNodeMembership.pool_id == _sid(pool_id))
    )
    return list(result.scalars().all())


# ── Jobs ────────────────────────────────────────────────────────────────


async def create_job(
    db: AsyncSession, name: str, submitted_by: UUID, steps_config: list[dict],
    target_pool_id: UUID | None = None, target_node_id: UUID | None = None,
    priority: int = 1, storage_target: str | None = None,
) -> Job:
    """Persist a newly submitted job in ``pending`` state.

    Called by ``POST /api/jobs`` after the route has validated ``steps_config``
    against the step registry and checked pool access. The job sits at
    ``status="pending"``, ``current_step=0`` until ``runner/scheduler.py``
    picks it up.

    Args:
        db: Caller-owned session.
        name: Human-facing job name (not unique).
        submitted_by: Owning user's ID.
        steps_config: The workflow definition, stored verbatim as JSON. Must
            already be JSON-serialisable; no validation happens here.
        target_pool_id: Schedule onto any online member of this pool.
        target_node_id: Pin execution to one specific node.
        priority: Higher means more important to the scheduler; defaults to 1.
        storage_target: Name of the storage backend for artifacts; ``None``
            means "use the default backend".

    Returns:
        The persisted, refreshed :class:`Job`.

    Side effects:
        Commits.

    AI Note: every ID argument is passed through :func:`_sid` here — this
    function is on the request path and was part of the UUID/SQLite bind fix.
    Note that nothing enforces "exactly one of pool/node"; passing both, or
    neither, is accepted at this layer and the scheduler decides what to do.
    """
    job = Job(
        name=name, submitted_by=_sid(submitted_by), steps_config=steps_config,
        target_pool_id=_sid(target_pool_id), target_node_id=_sid(target_node_id),
        priority=priority, storage_target=storage_target,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def get_job_by_id(db: AsyncSession, job_id: UUID) -> Job | None:
    """Fetch a job by primary key.

    Args:
        db: Caller-owned session.
        job_id: UUID or string; stringified via :func:`_sid`.

    Returns:
        The :class:`Job`, or ``None`` if not found.

    AI Note: relationships (``step_runs``, ``artifacts``) are lazy-loaded. On
    an async session, touching them outside an explicit ``await`` /
    eager-load raises ``MissingGreenlet`` — use
    :func:`get_step_runs_for_job` / :func:`list_artifacts_for_job` instead of
    ``job.step_runs``.
    """
    return await db.get(Job, _sid(job_id))


async def list_jobs(
    db: AsyncSession,
    status: str | None = None,
    submitted_by: UUID | None = None,
    pool_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Job]:
    """List jobs newest-first, with optional filters and pagination.

    Backs the Jobs page and the dashboard's recent-jobs panel.

    Args:
        db: Caller-owned session.
        status: Exact match on ``Job.status`` when provided.
        submitted_by: Restrict to one submitter's jobs.
        pool_id: Restrict to jobs targeting this pool.
        limit: Page size, default 50.
        offset: Rows to skip, for paging.

    Returns:
        Matching :class:`Job` rows ordered by ``created_at`` descending.

    AI Note: ``submitted_by`` is compared **raw** while ``pool_id`` goes
    through :func:`_sid` — the inconsistency means a ``uuid.UUID`` submitter
    filter silently matches nothing instead of erroring. Pass strings.
    """
    query = select(Job)
    if status:
        query = query.where(Job.status == status)
    if submitted_by:
        query = query.where(Job.submitted_by == _sid(submitted_by))
    if pool_id:
        query = query.where(Job.target_pool_id == _sid(pool_id))
    query = query.order_by(Job.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())


async def update_job(db: AsyncSession, job_id: UUID, **kwargs: Any) -> Job | None:
    """Apply arbitrary column updates to a job.

    The runner's main state-transition primitive: it is how ``status``,
    ``current_step``, ``context_data``, ``error``, ``started_at`` and
    ``completed_at`` all get written.

    Args:
        db: Caller-owned session.
        job_id: Primary key of the job.
        **kwargs: Column-name -> value pairs applied with ``setattr``.

    Returns:
        The refreshed :class:`Job`, or ``None`` if the ID does not exist.

    Side effects:
        Commits.

    AI Note: no concurrency control. The runner loop and the WebSocket
    ``step.completed``/``step.failed`` handler both reach this function from
    different tasks with different sessions; because each call is a full
    read-modify-write of the loaded object, two overlapping updates can clobber
    each other's fields (last writer wins). Keep updates narrowly scoped to the
    columns you actually own.
    """
    job = await db.get(Job, _sid(job_id))
    if not job:
        return None
    # AI Note: _sid_kwargs before the blind setattr — these updaters accept
    # arbitrary column names, so a *_id / *_by kwarg holding a uuid.UUID
    # would bind raw onto a String(36) column and crash aiosqlite.
    for k, v in _sid_kwargs(kwargs).items():
        setattr(job, k, v)
    await db.commit()
    await db.refresh(job)
    return job


async def append_job_log(db: AsyncSession, job_id: UUID, text: str) -> None:
    """Append a block of text to a job's aggregated terminal log. Committed
    incrementally so a crash mid-job still leaves a partial log.

    Feeds ``GET /api/jobs/{id}/log`` and the live log pane in the UI.

    Args:
        db: Caller-owned session.
        job_id: Job whose log to extend.
        text: Chunk to append verbatim; include your own trailing newline, no
            separator is inserted.

    Returns:
        ``None``. A missing job is silently ignored (no exception) so late log
        chunks from a deleted job cannot crash the WebSocket handler.

    Side effects:
        Commits on every call.

    AI Note: read-concat-write of the whole ``log_text`` column, so cost is
    O(existing log size) per chunk and the column grows unbounded — a chatty
    long-running job makes this progressively more expensive. Also unsynchronised:
    two concurrent appends for the same job can lose one chunk. The incremental
    commit is deliberate (partial logs survive a crash), so do not batch it
    away without providing another durability story.
    """
    job = await db.get(Job, _sid(job_id))
    if job:
        job.log_text = (job.log_text or "") + text
        await db.commit()


async def get_active_jobs(db: AsyncSession) -> list[Job]:
    """Return every job that is not in a terminal state.

    Used by ``runner/resume.py`` at startup to re-adopt jobs that were still in
    flight when the server stopped, and by the dashboard's activity counters.

    Args:
        db: Caller-owned session.

    Returns:
        Jobs whose ``status`` is ``"pending"``, ``"queued"`` or ``"running"``,
        in unspecified order. Terminal states (``completed``, ``failed``,
        ``cancelled``) are excluded.

    AI Note: this status triple is the definition of "active" across the
    codebase. If you add a new non-terminal status you must add it here too, or
    jobs in that state will be dropped on restart instead of resumed.
    """
    result = await db.execute(
        select(Job).where(Job.status.in_(["pending", "queued", "running"]))
    )
    return list(result.scalars().all())# ── Step Runs ───────────────────────────────────────────────────────────


async def create_step_run(
    db: AsyncSession, job_id: UUID, step_index: int, step_name: str,
    input_params: dict | None = None,
) -> StepRun:
    """Record the start of one execution of one job step.

    The runner calls this immediately before dispatching a step, so the row
    exists (in ``pending``) even if the dispatch itself fails.

    Args:
        db: Caller-owned session.
        job_id: Owning job.
        step_index: Position in ``Job.steps_config``. Not unique — loops
            create several rows at the same index (see
            :func:`get_latest_step_run`).
        step_name: Registry name of the step type, for display and debugging.
        input_params: Inputs *after* ``${...}`` substitution against the job
            context. Stored as JSON.

    Returns:
        The persisted, refreshed :class:`StepRun` in ``status="pending"``.

    Side effects:
        Commits.

    AI Note: ``job_id`` is coerced with :func:`_sid`. It was previously raw,
    which killed any job whose runner was handed a ``uuid.UUID`` — the insert
    failed on bind and the job died at its first step with no ``StepRun`` row.
    ``input_params`` may contain resolved secrets if a step template
    interpolated one; be careful about surfacing it in the UI.
    """
    # AI Note: _sid() on every ID — the column is String(36) and a raw
    # uuid.UUID crashes aiosqlite on bind (500 + poisoned session).
    step_run = StepRun(
        job_id=_sid(job_id), step_index=step_index, step_name=step_name,
        input_params=input_params,
    )
    db.add(step_run)
    await db.commit()
    await db.refresh(step_run)
    return step_run


async def update_step_run(db: AsyncSession, step_run_id: UUID, **kwargs: Any) -> StepRun | None:
    """Apply arbitrary column updates to a step run.

    How the runner and the agent WebSocket handler record progress: ``status``,
    ``node_id``, ``state``, ``output_params``, ``error``, ``started_at`` and
    ``finished_at`` are all written through here.

    Args:
        db: Caller-owned session.
        step_run_id: Primary key of the step run.
        **kwargs: Column-name -> value pairs applied with ``setattr``.

    Returns:
        The refreshed :class:`StepRun`, or ``None`` if the ID does not exist.

    Side effects:
        Commits.

    AI Note: terminal ``status`` for a step run is ``"success"``, not
    ``"completed"`` — ``Job.status`` uses ``"completed"``. Passing the wrong
    string here does not error; it just writes a value nothing matches on, and
    the step silently never appears finished.
    """
    step_run = await db.get(StepRun, _sid(step_run_id))
    if not step_run:
        return None
    # AI Note: _sid_kwargs before the blind setattr — these updaters accept
    # arbitrary column names, so a *_id / *_by kwarg holding a uuid.UUID
    # would bind raw onto a String(36) column and crash aiosqlite.
    for k, v in _sid_kwargs(kwargs).items():
        setattr(step_run, k, v)
    await db.commit()
    await db.refresh(step_run)
    return step_run


async def get_step_runs_for_job(db: AsyncSession, job_id: UUID) -> list[StepRun]:
    """Return all step runs for a job, ordered by step index.

    Backs the per-step timeline on the Job Detail page.

    Args:
        db: Caller-owned session.
        job_id: The job to expand.

    Returns:
        Every :class:`StepRun` for the job ordered by ``step_index``.

    AI Note: for a job containing loops there are multiple rows per index and
    the tie-break between them is unspecified, so repeated attempts at the same
    index may come back in any relative order.
    """
    result = await db.execute(
        select(StepRun).where(StepRun.job_id == _sid(job_id)).order_by(StepRun.step_index)
    )
    return list(result.scalars().all())


async def get_latest_step_run(
    db: AsyncSession, job_id: UUID, step_index: int,
) -> StepRun | None:
    """Return the most recently created step_run for (job_id, step_index).

    Loops produce multiple step_runs at the same step_index; this picks the
    one currently in flight.

    Args:
        db: Caller-owned session.
        job_id: The job.
        step_index: Position in ``Job.steps_config``.

    Returns:
        A single :class:`StepRun`, or ``None`` if the step has never run.

    AI Note: the "most recent" ordering is ``StepRun.id DESC``, but ``id`` is a
    random UUID4 **string** (``models._new_uuid``), not a monotonic key — so
    the ordering is lexicographic over random values, i.e. arbitrary. For a
    non-looping step there is only one row and the result is correct; for a
    looped step this can return a previous iteration instead of the current
    one. See the POSSIBLE BUG note for this file.
    """
    result = await db.execute(
        select(StepRun)
        .where(StepRun.job_id == _sid(job_id), StepRun.step_index == step_index)
        .order_by(StepRun.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── Credentials ─────────────────────────────────────────────────────────


async def create_credential(
    db: AsyncSession, name: str, credential_type: str, encrypted_fields: bytes,
    owner_id: UUID, is_shared: bool = False, allowed_groups: list | None = None,
    description: str | None = None,
) -> Credential:
    """Store an already-encrypted credential blob.

    Only ``services/credentials/manager.py`` should call this: it validates the
    plaintext against the type's strategy, serialises it, and encrypts it with
    ``FieldEncryptor`` before handing the ciphertext here.

    Args:
        db: Caller-owned session.
        name: Unique credential name; job steps reference credentials by name.
            Duplicates raise ``IntegrityError``.
        credential_type: Strategy key (``"ssh"``, ``"s3"``, ``"git"``, ...).
            Must match a registered strategy or later reads raise ``KeyError``.
        encrypted_fields: **Ciphertext.** Never pass plaintext here — this
            function performs no encryption of its own.
        owner_id: Owning user.
        is_shared: Whether other users may use it.
        allowed_groups: List of group-ID strings; defaults to ``[]``. Stored as
            JSON with no referential integrity.
        description: Optional free text (safe to display; do not put secrets
            in it).

    Returns:
        The persisted, refreshed :class:`Credential`.

    Side effects:
        Commits.

    AI Note: ``allowed_groups or []`` collapses both ``None`` and an explicitly
    passed empty list to ``[]`` — deliberate, so the JSON column is never NULL
    and consumers can always iterate it.
    """
    cred = Credential(
        name=name, credential_type=credential_type, encrypted_fields=encrypted_fields,
        owner_id=_sid(owner_id), is_shared=is_shared,
        allowed_groups=allowed_groups or [], description=description,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return cred


async def get_credential_by_id(db: AsyncSession, cred_id: UUID) -> Credential | None:
    """Fetch a credential row (still encrypted) by primary key.

    Args:
        db: Caller-owned session.
        cred_id: UUID or string; stringified via :func:`_sid`.

    Returns:
        The :class:`Credential`, or ``None`` if not found.

    AI Note: returns the raw row including ``encrypted_fields``. Decryption is
    the credential manager's job — never expose this object directly through
    an API response.
    """
    return await db.get(Credential, _sid(cred_id))


async def get_credential_by_name(db: AsyncSession, name: str) -> Credential | None:
    """Resolve a credential by its unique name.

    This is the lookup the job runner uses when a step declares
    ``credential: "<name>"``; a rename therefore breaks every step referencing
    the old name.

    Args:
        db: Caller-owned session.
        name: Exact credential name.

    Returns:
        The :class:`Credential`, or ``None`` if no credential has that name.
    """
    result = await db.execute(select(Credential).where(Credential.name == name))
    return result.scalar_one_or_none()


async def list_credentials(db: AsyncSession) -> list[Credential]:
    """Return every credential, ordered by name.

    Args:
        db: Caller-owned session.

    Returns:
        All :class:`Credential` rows alphabetical by ``name``.

    AI Note: no ownership or sharing filter is applied here — every row is
    returned regardless of ``owner_id``/``is_shared``. The credentials route is
    responsible for filtering and for projecting away ``encrypted_fields``
    before serialising.
    """
    result = await db.execute(select(Credential).order_by(Credential.name))
    return list(result.scalars().all())


async def update_credential(db: AsyncSession, cred_id: UUID, **kwargs: Any) -> Credential | None:
    """Apply column updates to a credential and stamp ``updated_at``.

    Args:
        db: Caller-owned session.
        cred_id: Primary key of the credential.
        **kwargs: Column-name -> value pairs. To rotate the secret, pass a
            freshly encrypted ``encrypted_fields`` blob — this function does
            not encrypt.

    Returns:
        The refreshed :class:`Credential`, or ``None`` if the ID does not
        exist.

    Side effects:
        Commits. Overwrites ``kwargs["updated_at"]`` with the current UTC time,
        so a caller-supplied ``updated_at`` is ignored by design.

    AI Note: ``cred_id`` is passed to ``db.get`` **raw**, without :func:`_sid`
    — it only works for callers that already hold string IDs. See the POSSIBLE
    BUG note for this file.
    """
    cred = await db.get(Credential, _sid(cred_id))
    if not cred:
        return None
    kwargs["updated_at"] = _utcnow()
    # AI Note: _sid_kwargs before the blind setattr — these updaters accept
    # arbitrary column names, so a *_id / *_by kwarg holding a uuid.UUID
    # would bind raw onto a String(36) column and crash aiosqlite.
    for k, v in _sid_kwargs(kwargs).items():
        setattr(cred, k, v)
    await db.commit()
    await db.refresh(cred)
    return cred


async def delete_credential(db: AsyncSession, cred_id: UUID) -> bool:
    """Delete a credential.

    Args:
        db: Caller-owned session.
        cred_id: Primary key of the credential.

    Returns:
        ``True`` if found and deleted, ``False`` if the ID did not exist.

    Side effects:
        Commits. Irreversible — the ciphertext is the only copy of the secret.

    AI Note: ``StorageBackend.credential_id`` may still point here. With no
    cascade and no enforced foreign keys in SQLite, deleting a credential in
    use silently breaks that backend at its next initialisation
    (``main.lifespan`` logs a warning and continues rather than failing). Also
    passes ``cred_id`` raw, without :func:`_sid`.
    """
    cred = await db.get(Credential, _sid(cred_id))
    if not cred:
        return False
    await db.delete(cred)
    await db.commit()
    return True


# ── Storage Backends ────────────────────────────────────────────────────


async def create_storage_backend(db: AsyncSession, **kwargs: Any) -> StorageBackend:
    """Register a storage backend configuration.

    Args:
        db: Caller-owned session.
        **kwargs: :class:`StorageBackend` column values. ``name`` (unique) and
            ``backend_type`` are required; ``credential_id`` should point at
            the credential the backend authenticates with; ``config`` holds the
            backend-specific JSON.

    Returns:
        The persisted, refreshed :class:`StorageBackend`.

    Side effects:
        Commits. The row is **not** instantiated as a live client here —
        ``StorageManager.init_backends`` does that, so a backend added at
        runtime is only usable after the manager re-reads the table.

    AI Note: nothing validates ``config`` against the backend type or checks
    that ``credential_id`` exists, and nothing prevents setting
    ``is_default=True`` on a second backend. Misconfiguration surfaces later as
    a warning from the storage manager, not as an error here.
    """
    backend = StorageBackend(**_sid_kwargs(kwargs))
    db.add(backend)
    await db.commit()
    await db.refresh(backend)
    return backend


async def get_storage_backend_by_id(db: AsyncSession, backend_id: UUID) -> StorageBackend | None:
    """Fetch a storage backend by primary key.

    Args:
        db: Caller-owned session.
        backend_id: UUID or string; stringified via :func:`_sid`.

    Returns:
        The :class:`StorageBackend`, or ``None`` if not found. Ignores
        ``is_active`` — an inactive backend is still returned, which is what
        artifact *reads* need in order to fetch historical files.
    """
    return await db.get(StorageBackend, _sid(backend_id))


async def get_default_storage_backend(db: AsyncSession) -> StorageBackend | None:
    """Return the backend to use when a job names no ``storage_target``.

    Args:
        db: Caller-owned session.

    Returns:
        The backend flagged both default and active, or ``None`` if there is
        none — in which case artifact upload has nowhere to go.

    Raises:
        sqlalchemy.exc.MultipleResultsFound: If more than one active backend is
            flagged ``is_default``. Nothing at the DB level prevents that, so
            callers of the storage-backend admin API must keep the flag unique
            themselves.

    AI Note: ``== True`` (rather than ``is True``) is required — SQLAlchemy
    overloads ``==`` to build the SQL comparison, and a linter "fix" to ``is``
    would evaluate in Python and break the query. Both conditions matter:
    deactivating the default backend leaves the cluster with no default at all
    rather than falling back to another.
    """
    result = await db.execute(
        select(StorageBackend).where(
            StorageBackend.is_default == True, StorageBackend.is_active == True
        )
    )
    return result.scalar_one_or_none()


async def list_storage_backends(db: AsyncSession) -> list[StorageBackend]:
    """Return every storage backend, best-priority first.

    Args:
        db: Caller-owned session.

    Returns:
        All :class:`StorageBackend` rows ordered by ``priority`` ascending
        (lower number = preferred), then ``name`` as a stable tie-break.
        **Includes inactive backends** — filter on ``is_active`` if you are
        choosing a write target.
    """
    result = await db.execute(
        select(StorageBackend).order_by(StorageBackend.priority, StorageBackend.name)
    )
    return list(result.scalars().all())


# ── Artifacts ───────────────────────────────────────────────────────────


async def create_artifact(db: AsyncSession, **kwargs: Any) -> Artifact:
    """Index a file that has already been uploaded to a storage backend.

    Called after the bytes land (e.g. the gem5 ``m5out`` tarball an agent pushes
    back through the results endpoint), so that the job's results become
    listable and downloadable.

    Args:
        db: Caller-owned session.
        **kwargs: :class:`Artifact` column values. ``job_id``, ``filename``,
            ``storage_backend_id`` and ``storage_key`` are required;
            ``step_run_id``, ``content_type``, ``size_bytes`` and
            ``uploaded_by`` are optional.

    Returns:
        The persisted, refreshed :class:`Artifact`.

    Side effects:
        Commits.

    AI Note: write ordering matters — create this row only *after* a successful
    upload. A row whose ``storage_key`` points at bytes that were never written
    produces a download that 404s from the backend rather than from the API.
    """
    artifact = Artifact(**_sid_kwargs(kwargs))
    db.add(artifact)
    await db.commit()
    await db.refresh(artifact)
    return artifact


async def list_artifacts_for_job(db: AsyncSession, job_id: UUID) -> list[Artifact]:
    """Return a job's artifacts in upload order.

    Args:
        db: Caller-owned session.
        job_id: The job whose outputs to list.

    Returns:
        Matching :class:`Artifact` rows ordered by ``created_at`` ascending
        (oldest first), so the list reads chronologically in the UI.
    """
    result = await db.execute(
        select(Artifact).where(Artifact.job_id == _sid(job_id)).order_by(Artifact.created_at)
    )
    return list(result.scalars().all())


async def get_artifact_by_id(db: AsyncSession, artifact_id: UUID) -> Artifact | None:
    """Fetch one artifact's metadata by primary key.

    Entry point for the download and results-manifest endpoints, which then use
    ``storage_backend_id`` + ``storage_key`` to stream the bytes.

    Args:
        db: Caller-owned session.
        artifact_id: UUID or string; stringified via :func:`_sid`.

    Returns:
        The :class:`Artifact`, or ``None`` if not found.

    AI Note: security-relevant. This performs no authorization — it does not
    check that the requesting user owns or can see the parent job. The route
    must do that before streaming the file.
    """
    return await db.get(Artifact, _sid(artifact_id))


# ── Storage Transfers ───────────────────────────────────────────────────


async def create_transfer(db: AsyncSession, **kwargs: Any) -> StorageTransfer:
    """Record a request to copy an artifact between storage backends.

    Args:
        db: Caller-owned session.
        **kwargs: :class:`StorageTransfer` column values; ``artifact_id``,
            ``source_backend_id`` and ``dest_backend_id`` are the meaningful
            ones. ``requested_by`` is optional.

    Returns:
        The persisted, refreshed :class:`StorageTransfer` in
        ``status="pending"``.

    Side effects:
        Commits. Creating the row does **not** start a copy — it only records
        the intent for whatever worker processes pending transfers.
    """
    transfer = StorageTransfer(**_sid_kwargs(kwargs))
    db.add(transfer)
    await db.commit()
    await db.refresh(transfer)
    return transfer


async def update_transfer(db: AsyncSession, transfer_id: UUID, **kwargs: Any) -> StorageTransfer | None:
    """Apply column updates to a transfer (progress, status, error, timings).

    Args:
        db: Caller-owned session.
        transfer_id: Primary key of the transfer.
        **kwargs: Column-name -> value pairs, typically ``status``,
            ``bytes_transferred``, ``error``, ``started_at``,
            ``completed_at``.

    Returns:
        The refreshed :class:`StorageTransfer`, or ``None`` if the ID does not
        exist.

    Side effects:
        Commits. A progress-reporting caller therefore issues one transaction
        per update — throttle updates for large files.

    AI Note: marking a transfer complete does **not** repoint the artifact.
    Whoever finishes the copy must also update
    ``Artifact.storage_backend_id``/``storage_key``, or the artifact keeps
    resolving to the source backend.
    """
    transfer = await db.get(StorageTransfer, _sid(transfer_id))
    if not transfer:
        return None
    # AI Note: _sid_kwargs before the blind setattr — these updaters accept
    # arbitrary column names, so a *_id / *_by kwarg holding a uuid.UUID
    # would bind raw onto a String(36) column and crash aiosqlite.
    for k, v in _sid_kwargs(kwargs).items():
        setattr(transfer, k, v)
    await db.commit()
    await db.refresh(transfer)
    return transfer


async def list_transfers(db: AsyncSession, status: str | None = None) -> list[StorageTransfer]:
    """List transfers, most recently started first.

    Args:
        db: Caller-owned session.
        status: Optional exact match on ``StorageTransfer.status``; a worker
            polls with ``status="pending"``.

    Returns:
        Matching :class:`StorageTransfer` rows ordered by ``started_at``
        descending.

    AI Note: ``nullslast()`` is load-bearing. Pending transfers have
    ``started_at IS NULL``; without it SQLite would sort NULLs first and the
    not-yet-started rows would masquerade as the newest entries.
    """
    query = select(StorageTransfer)
    if status:
        query = query.where(StorageTransfer.status == status)
    query = query.order_by(StorageTransfer.started_at.desc().nullslast())
    result = await db.execute(query)
    return list(result.scalars().all())


# ── Saved Templates ────────────────────────────────────────────────────


async def create_template(
    db: AsyncSession, name: str, steps_config: list[dict], created_by: UUID,
    description: str | None = None,
) -> SavedTemplate:
    """Save a reusable workflow definition.

    Args:
        db: Caller-owned session.
        name: Display name. **Not unique** — duplicates are allowed, so the UI
            must disambiguate by ``id``.
        steps_config: Same JSON shape as ``Job.steps_config``; not validated
            against the step registry until a job is submitted from it.
        created_by: Owning user's ID.
        description: Optional free text.

    Returns:
        The persisted, refreshed :class:`SavedTemplate`.

    Side effects:
        Commits.
    """
    template = SavedTemplate(
        name=name, steps_config=steps_config, created_by=_sid(created_by),
        description=description,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


async def list_templates(db: AsyncSession) -> list[SavedTemplate]:
    """Return every saved template, ordered by name.

    Args:
        db: Caller-owned session.

    Returns:
        All :class:`SavedTemplate` rows alphabetical by ``name``. No
        per-user filtering — every template is visible to every caller.
    """
    result = await db.execute(select(SavedTemplate).order_by(SavedTemplate.name))
    return list(result.scalars().all())


async def delete_template(db: AsyncSession, template_id: UUID) -> bool:
    """Delete a saved template.

    Args:
        db: Caller-owned session.
        template_id: Primary key of the template.

    Returns:
        ``True`` if found and deleted, ``False`` if the ID did not exist.

    Side effects:
        Commits. Jobs already created from the template are unaffected — a job
        gets its own copy of ``steps_config`` at submit time.

    AI Note: no ownership check happens here; the route must verify the caller
    may delete this template.
    """
    template = await db.get(SavedTemplate, _sid(template_id))
    if not template:
        return False
    await db.delete(template)
    await db.commit()
    return True


# ── Audit Log ───────────────────────────────────────────────────────────


async def create_audit_entry(
    db: AsyncSession, action: str, user_id: UUID | None = None,
    target_type: str | None = None, target_id: UUID | None = None,
    details: dict | None = None,
) -> AuditLog:
    """Append a row to the audit log.

    Args:
        db: Caller-owned session.
        action: Short verb identifying what happened, e.g. ``"job.submit"``.
        user_id: Acting user, or ``None`` for system-initiated actions.
        target_type: Loose label for the affected entity type (``"job"``,
            ``"node"``, ...).
        target_id: ID of the affected entity. Deliberately not a foreign key,
            so the entry outlives deletion of its target.
        details: Free-form JSON context.

    Returns:
        The new :class:`AuditLog` entry.

    Side effects:
        Commits. Insert-only: nothing in this module updates or deletes audit
        rows, and the table grows without bound (no retention/pruning yet).

    AI Note: security-relevant. ``details`` is stored verbatim and is readable
    by anyone with audit access — never pass credential plaintext, tokens or
    password hashes. Note also that ``user_id``/``target_id`` are stored raw
    without :func:`_sid`, so pass strings.
    """
    entry = AuditLog(
        user_id=_sid(user_id), action=action, target_type=target_type,
        target_id=_sid(target_id), details=details,
    )
    db.add(entry)
    await db.commit()
    return entry
