"""Extended integration tests for the repository layer (nexus_server.db.ops).

Companion to ``tests/integration/test_db_ops.py``, which covers the happy paths
and the ``_sid`` coercions that were already fixed. This file deliberately does
NOT repeat those cases. It picks up what that file leaves open:

* the ops (and op *branches*) it never calls at all,
* boundary arguments — ``limit=0``, an offset past the end, an empty log chunk,
  an empty-string filter,
* documented ``Raises:`` contracts — duplicate composite primary keys, a
  duplicate unique name, two backends both flagged default,
* the silent no-op deletes, where the caller cannot tell "removed" from "was
  never there",
* referential decay: nothing in the schema declares ``ON DELETE CASCADE`` and
  SQLite foreign keys are off in this app. Those tests pin what ACTUALLY
  happens, which is not what the source docstrings claim:
    - ``delete_credential`` does leave ``StorageBackend.credential_id``
      dangling, and the backend keeps being offered as the cluster default;
    - ``delete_node`` leaves ``step_runs.node_id`` dangling (intended — the
      audit trail outlives the node);
    - but ``delete_node`` does **not** orphan a ``pool_node_memberships`` row as
      documented. It raises ``AssertionError`` and deletes nothing at all for
      any node that is still in a pool. See
      ``test_delete_node_raises_assertion_error_while_still_in_a_pool``.

The UUID sweep (the reason this file exists)
    Every id column in ``db/models.py`` is ``String(36)``. Handing SQLAlchemy a
    raw ``uuid.UUID`` for one of them makes aiosqlite raise
    ``sqlite3.ProgrammingError: type 'UUID' is not supported`` at *bind* time,
    which SQLAlchemy re-raises as ``sqlalchemy.exc.ProgrammingError``. Worse, the
    failure happens inside the op's own ``await db.commit()``, so the caller's
    session is left needing a rollback and every later statement on it raises
    ``PendingRollbackError`` — one bad id poisons a whole request (this is the
    bug that used to tear down the agent WebSocket on every step message).

    ``ops._sid`` / ``ops._sid_kwargs`` exist to stop that, and the bug class is
    now closed: every id-taking op coerces, either by wrapping the argument in
    ``_sid`` at the constructor/filter or by routing ``**kwargs`` through
    ``_sid_kwargs`` (which matches both the ``_id`` and the ``_by`` suffix, so
    ``Artifact.uploaded_by`` and ``StorageTransfer.requested_by`` are covered
    too). ``_COERCED_ID_OPS`` below is the regression guard for that: it walks
    *every* id-taking op and op *branch* with a real ``uuid.UUID`` and asserts
    each one returns the right row. Dropping a single ``_sid()`` turns the
    matching case red and names the exact site.

    The last hole — ``create_node(id=<uuid.UUID>)``, a caller-supplied PRIMARY
    key, which the suffix-based match missed because a bare ``"id"`` ends in
    neither ``_id`` nor ``_by`` — is closed too: ``_sid_kwargs`` now matches a
    bare ``"id"`` as well. That case is in ``_COERCED_ID_OPS`` like the rest.

    No ``xfail``/``skip`` is used anywhere in this file.

Session isolation
    Because a bind error poisons a session, every intentionally-crashing call
    runs on its own throwaway session from ``_probe_session`` rather than on the
    shared ``db`` fixture. Without that, one pinned crash would cascade into
    unrelated failures during fixture teardown.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import (
    IntegrityError,
    MultipleResultsFound,
    PendingRollbackError,
    ProgrammingError,
)

from nexus_server.db import ops
from nexus_server.db.models import (
    Artifact,
    AuditLog,
    Node,
    PoolNodeMembership,
    StepRun,
    StorageBackend,
    UserGroupMembership,
)
from nexus_server.services.auth_service import AuthService

# Shorthand: every sweep case below builds a real uuid.UUID from a stored
# string id, which is exactly the shape FastAPI hands routes for a ``{id}``
# path parameter declared as ``UUID``.
U = uuid.UUID

_NODE_PK = uuid.UUID("00000000-0000-4000-8000-000000000abc")
"""A caller-supplied primary key, fixed so the sweep stays deterministic."""


# ── local helpers / fixtures ─────────────────────────────────────────────


async def _mk_user(db, username="bob", role="user"):
    """Persist a user with a hashed throwaway password.

    Local copy of the same helper in ``test_db_ops.py`` — conftest is shared and
    must not be edited, and duplicating four lines is cheaper than coupling the
    two files.

    Args:
        db: Test ``AsyncSession``.
        username: Unique username; also used to derive the email.
        role: ``"user"`` or ``"admin"`` (admins bypass pool access checks).

    Returns:
        The persisted ``User``.

    Side effects:
        INSERTs one row into ``users``. Usernames are unique per test database,
        so every call inside one test needs a distinct name.
    """
    return await ops.create_user(
        db, username=username, password_hash=AuthService.hash_password("pw"),
        email=f"{username}@example.com", role=role,
    )


async def _mk_backend(db, owner, name, **kw):
    """Persist a storage backend plus the credential it references.

    Args:
        db: Test session.
        owner: ``User`` that owns the generated credential.
        name: Backend name; also used to keep the credential name unique.
        **kw: Extra ``create_storage_backend`` kwargs (``is_default``,
            ``is_active``, ``priority``, ``config``).

    Returns:
        The persisted ``StorageBackend``.

    Side effects:
        INSERTs two rows (one credential, one backend). The encrypted blob is a
        dummy — nothing here decrypts it.
    """
    cred = await ops.create_credential(
        db, name=f"cred-{name}", credential_type="s3", encrypted_fields=b"x",
        owner_id=owner.id,
    )
    return await ops.create_storage_backend(
        db, name=name, backend_type="minio", credential_id=cred.id, **kw,
    )


@asynccontextmanager
async def _probe_session(session_factory):
    """Yield a disposable session for a call that is expected to crash on bind.

    A UUID bind error fires inside the op's own ``commit()``, leaving the session
    in "needs rollback" state; any further use raises ``PendingRollbackError``.
    Running such calls on the shared ``db`` fixture would therefore break
    unrelated assertions and fixture teardown in the same test.

    Args:
        session_factory: The ``async_sessionmaker`` from conftest. It shares the
            in-memory database with the ``db`` fixture (StaticPool), so rows
            committed through ``db`` are visible here.

    Yields:
        A fresh ``AsyncSession``, rolled back and closed on exit even if the body
        raised.
    """
    session = session_factory()
    try:
        yield session
    finally:
        try:
            await session.rollback()
        except Exception:  # pragma: no cover - defensive; a poisoned session
            pass          # must never mask the assertion under test
        await session.close()


# ── Users ────────────────────────────────────────────────────────────────


async def test_create_user_duplicate_username_raises_integrity_error(db, session_factory):
    """A second user with an existing username violates the UNIQUE constraint.

    ``create_user`` documents that it does not pre-check, so the admin-create and
    signup routes must catch this themselves. If the constraint were ever dropped
    from the model, two accounts could share a login name and
    ``get_user_by_username`` — the password-login lookup — would raise
    ``MultipleResultsFound`` instead of authenticating.
    """
    await _mk_user(db, "dupe")
    async with _probe_session(session_factory) as probe:
        with pytest.raises(IntegrityError):
            await _mk_user(probe, "dupe")
    # The original row is untouched and still the only one.
    assert (await ops.get_user_by_username(db, "dupe")) is not None
    assert len([u for u in await ops.list_users(db) if u.username == "dupe"]) == 1


async def test_update_user_silently_drops_unknown_column_name(db):
    """A typo'd kwarg is set as a junk Python attribute, not rejected.

    Pins the documented ``**kwargs`` hazard shared by every ``update_*`` op: the
    blind ``setattr`` means ``rolle="admin"`` neither errors nor changes the
    ``role`` column. Callers get a success response for a write that never
    happened, so the guard has to live in the route's Pydantic model.
    """
    user = await _mk_user(db, "typo")
    updated = await ops.update_user(db, user.id, rolle="admin")
    assert updated is not None
    assert updated.role == "user"          # the real column is untouched
    assert updated.rolle == "admin"        # ...it became a plain attribute
    # And it was never persisted: a fresh read still shows the original role.
    reread = await ops.get_user_by_id(db, user.id)
    assert reread.role == "user"


# ── Groups, memberships and pool access ──────────────────────────────────


async def test_create_group_duplicate_name_raises_integrity_error(db, session_factory):
    """``groups.name`` is UNIQUE, so re-creating a group name raises.

    Group names are the handle admins use in the UI; allowing duplicates would
    make a pool grant ambiguous about which group it applies to.
    """
    admin = await _mk_user(db, "gadm", role="admin")
    await ops.create_group(db, name="dupe-grp", created_by=admin.id)
    async with _probe_session(session_factory) as probe:
        with pytest.raises(IntegrityError):
            await ops.create_group(probe, name="dupe-grp", created_by=admin.id)
    assert len([g for g in await ops.list_groups(db) if g.name == "dupe-grp"]) == 1


async def test_add_user_to_group_duplicate_pair_raises_integrity_error(db, session_factory):
    """Re-adding an existing member violates the composite PK (no upsert).

    Documented contract, and the asymmetry that makes it worth pinning:
    ``set_group_pool_access`` *does* upsert while ``add_user_to_group`` does not.
    A route that re-POSTs a membership therefore has to check first or catch —
    otherwise a harmless double-click on "add member" 500s.

    The duplicate runs on a throwaway session because a failed flush leaves the
    session needing a rollback, and ``rollback()`` expires every ORM object on it
    — including the fixture rows this test still needs to assert against.
    """
    admin = await _mk_user(db, "gadm2", role="admin")
    member = await _mk_user(db, "dupmember")
    group = await ops.create_group(db, name="dup-pair", created_by=admin.id)
    member_id, group_id = member.id, group.id

    await ops.add_user_to_group(db, member_id, group_id)
    async with _probe_session(session_factory) as probe:
        with pytest.raises(IntegrityError):
            await ops.add_user_to_group(probe, member_id, group_id, role_in_group="lead")

    # Exactly one membership row survived — the failed INSERT changed nothing.
    count = (
        await db.execute(
            select(func.count())
            .select_from(UserGroupMembership)
            .where(
                UserGroupMembership.user_id == member_id,
                UserGroupMembership.group_id == group_id,
            )
        )
    ).scalar_one()
    assert count == 1


async def test_remove_user_from_group_nonexistent_pair_is_silent_noop(db):
    """Removing a membership that was never created neither raises nor reports.

    ``remove_user_from_group`` returns ``None`` unconditionally, so "revoked" and
    "was not a member" are indistinguishable. The DELETE route consequently
    cannot honestly answer 404, and a caller who typo'd an id gets a 204. This
    test exists so that contract is a deliberate choice rather than an accident.
    """
    admin = await _mk_user(db, "gadm3", role="admin")
    member = await _mk_user(db, "neverjoined")
    group = await ops.create_group(db, name="noop-grp", created_by=admin.id)

    # No membership exists at all...
    assert await ops.remove_user_from_group(db, member.id, group.id) is None
    # ...and removing entirely unknown ids is equally quiet.
    assert await ops.remove_user_from_group(db, str(uuid.uuid4()), str(uuid.uuid4())) is None
    # Nothing was created as a side effect either.
    total = (
        await db.execute(select(func.count()).select_from(UserGroupMembership))
    ).scalar_one()
    assert total == 0


async def test_remove_user_from_group_revokes_pool_access_immediately(db):
    """Dropping the membership withdraws the pool access it conferred.

    The privilege half of the contract: access is computed by joining membership
    to ``group_pool_access`` on every check, so there is no cache to invalidate.
    Asserting True *then* False around the removal proves the revocation is
    effective at once rather than at next login.
    """
    admin = await _mk_user(db, "gadm4", role="admin")
    user = await _mk_user(db, "revokee")
    group = await ops.create_group(db, name="revoke-grp", created_by=admin.id)
    pool = await ops.create_pool(db, name="revoke-pool", created_by=admin.id)
    await ops.add_user_to_group(db, user.id, group.id)
    await ops.set_group_pool_access(db, group.id, pool.id)

    assert await ops.check_user_pool_access(db, user.id, pool.id) is True
    await ops.remove_user_from_group(db, user.id, group.id)
    assert await ops.check_user_pool_access(db, user.id, pool.id) is False


async def test_set_group_pool_access_permission_value_is_stored_but_not_enforced(db):
    """Any permission string grants full access — the level is never read.

    Security-relevant and easy to misread as implemented: the column accepts
    ``"read"`` and the row is stored faithfully, but
    ``check_user_pool_access`` only tests row *existence*. So a grant intended as
    read-only confers submit access today. Pinning it means anyone adding a
    read-only tier gets a failing test pointing at the check that must change.
    """
    admin = await _mk_user(db, "gadm5", role="admin")
    user = await _mk_user(db, "readonly")
    group = await ops.create_group(db, name="ro-grp", created_by=admin.id)
    pool = await ops.create_pool(db, name="ro-pool", created_by=admin.id)
    await ops.add_user_to_group(db, user.id, group.id)

    access = await ops.set_group_pool_access(db, group.id, pool.id, permission="read")
    assert access.permission == "read"
    # Stored faithfully, yet still a full grant as far as the gate is concerned.
    assert await ops.check_user_pool_access(db, user.id, pool.id) is True


# ── Nodes ────────────────────────────────────────────────────────────────


async def test_create_node_rejects_caller_supplied_api_key(db):
    """Passing ``api_key`` collides with the one ``create_node`` mints itself.

    The op always generates the token, so the keyword is already bound and Python
    raises ``TypeError`` before any SQL runs. That is the desired outcome — it
    makes it impossible for a registration route to let a client choose its own
    node credential.
    """
    with pytest.raises(TypeError):
        await ops.create_node(db, hostname="byok", os_type="linux", api_key="chosen")


async def test_list_nodes_empty_string_filters_are_treated_as_no_filter(db):
    """Falsy filter values mean "unfiltered", not "match the empty value".

    Every filter in ``list_nodes`` is applied only when truthy. A route that
    forwards an empty query string therefore gets the whole table rather than
    nothing — convenient for optional params, but it also means a falsy sentinel
    can never be filtered on, so no node with ``status=""`` is reachable.
    """
    await ops.create_node(db, hostname="a-node", os_type="linux", status="online")
    await ops.create_node(db, hostname="b-node", os_type="macos", status="offline")

    assert {n.hostname for n in await ops.list_nodes(db, status="")} == {"a-node", "b-node"}
    assert {n.hostname for n in await ops.list_nodes(db, os_type="")} == {"a-node", "b-node"}
    # Explicit None behaves the same way.
    assert len(await ops.list_nodes(db, os_type=None, status=None, pool_id=None)) == 2


async def test_list_nodes_unknown_pool_returns_empty_list(db, sample_node):
    """A pool id with no memberships yields nothing rather than every node.

    Guards the join: if the ``pool_id`` predicate were dropped, an unknown pool
    would return the whole fleet and the scheduler would dispatch outside the
    pool the job asked for.
    """
    assert await ops.list_nodes(db, pool_id=str(uuid.uuid4())) == []


async def test_delete_node_raises_assertion_error_while_still_in_a_pool(
    db, sample_pool, session_factory
):
    """POSSIBLE BUG, pinned: deleting a pool member raises and deletes nothing.

    The assignment for this file assumed ``delete_node`` succeeds and leaves an
    orphaned ``pool_node_memberships`` row (which is what the ``ops.delete_node``
    and ``models.PoolNodeMembership`` docstrings claim). It does not. What
    actually happens:

    ``Node.pool_memberships`` is a real ORM ``relationship`` with no ``cascade``
    and no ``passive_deletes``. On ``db.delete(node)`` SQLAlchemy therefore tries
    to *de-associate* the children by NULLing ``pool_node_memberships.node_id`` —
    but that column is half of the composite primary key, so the unit of work
    aborts with a bare ``AssertionError`` ("tried to blank-out primary key
    column") during flush. The node is NOT deleted, the membership is NOT
    deleted, and the session is left needing a rollback.

    Reproduces on a session that never touched the membership too (verified), so
    it is not an identity-map artifact: SQLAlchemy lazy-loads the collection as
    part of the delete. That makes ``DELETE /api/nodes/{node_id}``
    (``api/routes/nodes.py``) a guaranteed 500 for any node that belongs to a
    pool — i.e. every node an operator would actually want to deregister —
    and ``AssertionError`` is not an exception any route maps to a clean status.

    Not fixed here (tests must not change source). See the POSSIBLE BUG note in
    the summary; the fix is a ``cascade="all, delete-orphan"`` on the
    relationship, or deleting memberships before the node.
    """
    node = await ops.create_node(db, hostname="pooled", os_type="linux")
    node_id, pool_id = node.id, sample_pool.id
    await ops.add_node_to_pool(db, pool_id, node_id)

    async with _probe_session(session_factory) as probe:
        with pytest.raises(AssertionError, match="blank-out primary key"):
            await ops.delete_node(probe, node_id)

    # Nothing was removed: both rows are still there.
    surviving_node = (
        await db.execute(select(Node).where(Node.id == node_id))
    ).scalar_one()
    assert surviving_node.hostname == "pooled"
    memberships = (
        await db.execute(
            select(PoolNodeMembership).where(PoolNodeMembership.node_id == node_id)
        )
    ).scalars().all()
    assert len(memberships) == 1
    assert memberships[0].pool_id == pool_id


async def test_delete_node_succeeds_once_pool_memberships_are_removed(db, sample_pool):
    """The workaround for the bug above: unjoin every pool, then delete.

    With no ``pool_node_memberships`` rows left there is no child collection for
    SQLAlchemy to de-associate, so the DELETE goes through normally and the
    idempotent ``True``/``False`` contract holds. This is the sequence a fixed
    route (or a cascade on the relationship) has to end up performing.
    """
    node = await ops.create_node(db, hostname="unjoinable", os_type="linux")
    node_id, pool_id = node.id, sample_pool.id
    await ops.add_node_to_pool(db, pool_id, node_id)

    await ops.remove_node_from_pool(db, pool_id, node_id)
    assert await ops.delete_node(db, node_id) is True
    assert await ops.get_node_by_id(db, node_id) is None
    assert await ops.delete_node(db, node_id) is False


async def test_delete_node_leaves_dangling_step_run_node_id(db):
    """A deleted node's id survives in ``step_runs.node_id`` — by design.

    The half of the no-cascade story that works and is *wanted*: there is no
    ``ON DELETE`` clause anywhere and this app never enables SQLite foreign keys,
    so the historical record of which machine ran a step outlives the node
    registration. ``StepRun`` has no ORM relationship back to ``Node`` (only the
    raw FK column), which is exactly why this path avoids the de-association
    crash that pool memberships hit.

    The final assertion is the important one: the id is still readable but no
    longer resolves, so any consumer rendering a step's node must tolerate
    ``get_node_by_id`` returning None.
    """
    user = await _mk_user(db, "orphan-owner")
    node = await ops.create_node(db, hostname="doomed", os_type="linux")
    node_id = node.id
    job = await ops.create_job(db, name="oj", submitted_by=user.id, steps_config=[])
    step_run = await ops.create_step_run(db, job_id=job.id, step_index=0, step_name="s")
    await ops.update_step_run(db, step_run.id, node_id=node_id)

    assert await ops.delete_node(db, node_id) is True
    assert await ops.get_node_by_id(db, node_id) is None

    surviving = (
        await db.execute(select(StepRun).where(StepRun.id == step_run.id))
    ).scalar_one()
    assert surviving.node_id == node_id
    assert await ops.get_node_by_id(db, surviving.node_id) is None
    # The step run is still listed for its job, dangling reference and all.
    assert [r.id for r in await ops.get_step_runs_for_job(db, job.id)] == [step_run.id]


# ── Pools ────────────────────────────────────────────────────────────────


async def test_create_pool_duplicate_name_raises_integrity_error(
    db, admin_user, session_factory
):
    """``pools.name`` is UNIQUE — jobs target pools by name in the UI."""
    await ops.create_pool(db, name="dupe-pool", created_by=admin_user.id)
    async with _probe_session(session_factory) as probe:
        with pytest.raises(IntegrityError):
            await ops.create_pool(probe, name="dupe-pool", created_by=admin_user.id)
    assert len([p for p in await ops.list_pools(db) if p.name == "dupe-pool"]) == 1


async def test_add_node_to_pool_duplicate_pair_raises_integrity_error(
    db, sample_pool, sample_node, session_factory
):
    """Adding a node twice to the same pool violates the composite PK.

    ``(pool_id, node_id)`` is the primary key and the op does not upsert, so the
    "add node to pool" admin action must be guarded. The follow-up count proves
    the failed INSERT left the single good membership intact rather than
    corrupting it.
    """
    pool_id, node_id = sample_pool.id, sample_node.id
    await ops.add_node_to_pool(db, pool_id, node_id)

    async with _probe_session(session_factory) as probe:
        with pytest.raises(IntegrityError):
            await ops.add_node_to_pool(probe, pool_id, node_id)

    count = (
        await db.execute(
            select(func.count())
            .select_from(PoolNodeMembership)
            .where(
                PoolNodeMembership.pool_id == pool_id,
                PoolNodeMembership.node_id == node_id,
            )
        )
    ).scalar_one()
    assert count == 1
    assert [n.id for n in await ops.get_pool_nodes(db, pool_id)] == [node_id]


async def test_remove_node_from_pool_nonexistent_pair_is_silent_noop(
    db, sample_pool, sample_node
):
    """Removing a membership that does not exist neither raises nor reports.

    Same shape as ``remove_user_from_group``: ``None`` either way. Also checks
    that removing a *different* pool's membership does not touch this one, which
    is what the composite WHERE clause is for — a predicate on ``node_id`` alone
    would evict the node from every pool at once.
    """
    owner = await _mk_user(db, "op-owner", role="admin")
    other_pool = await ops.create_pool(db, name="other-pool", created_by=owner.id)
    await ops.add_node_to_pool(db, sample_pool.id, sample_node.id)

    # never-a-member pair → quiet no-op
    assert await ops.remove_node_from_pool(db, other_pool.id, sample_node.id) is None
    # totally unknown ids → also quiet
    assert await ops.remove_node_from_pool(db, str(uuid.uuid4()), str(uuid.uuid4())) is None
    # the real membership is untouched
    assert [n.id for n in await ops.get_pool_nodes(db, sample_pool.id)] == [sample_node.id]


async def test_get_pool_nodes_includes_offline_members_and_unknown_pool_is_empty(db, sample_pool):
    """Pool expansion returns members in EVERY status; unknown pools give [].

    Documented and load-bearing: ``get_pool_nodes`` applies no status filter, so
    a scheduler that used it directly would dispatch to an offline agent. The
    contrast with ``list_nodes(pool_id=..., status="online")`` in the same
    assertion block is the point.
    """
    online = await ops.create_node(db, hostname="on", os_type="linux", status="online")
    offline = await ops.create_node(db, hostname="off", os_type="linux", status="offline")
    await ops.add_node_to_pool(db, sample_pool.id, online.id)
    await ops.add_node_to_pool(db, sample_pool.id, offline.id)

    assert {n.id for n in await ops.get_pool_nodes(db, sample_pool.id)} == {online.id, offline.id}
    narrowed = await ops.list_nodes(db, pool_id=sample_pool.id, status="online")
    assert [n.id for n in narrowed] == [online.id]
    assert await ops.get_pool_nodes(db, str(uuid.uuid4())) == []


# ── Jobs ─────────────────────────────────────────────────────────────────


async def test_create_job_accepts_neither_pool_nor_node_target(db, regular_user):
    """Nothing at this layer enforces "exactly one of pool/node".

    Both targets default to ``None`` and the row is accepted, so an untargeted
    job reaches ``pending`` and the scheduler — not the repository — owns the
    decision about what that means. Worth pinning because it looks like an
    invariant and is not one.
    """
    job = await ops.create_job(db, name="untargeted", submitted_by=regular_user.id, steps_config=[])
    assert job.target_pool_id is None
    assert job.target_node_id is None
    assert job.status == "pending"
    assert job.current_step == 0
    assert job.priority == 1
    assert job.log_text is None
    assert job.context_data == {}


async def test_list_jobs_limit_zero_returns_empty_list(db, regular_user):
    """``limit=0`` means zero rows, not "no limit".

    The falsy-means-default idiom used by ``list_nodes``' filters is NOT applied
    to ``limit``: it goes straight into ``.limit()``, so a page-size of 0 emits
    ``LIMIT 0`` and returns nothing. A route that forwarded an unvalidated
    ``?limit=0`` would render an empty Jobs page even though jobs exist.
    """
    for i in range(3):
        await ops.create_job(db, name=f"lz{i}", submitted_by=regular_user.id, steps_config=[])
    assert await ops.list_jobs(db, limit=0) == []
    # ...while the same query with the default limit sees all three.
    assert len(await ops.list_jobs(db)) == 3


async def test_list_jobs_offset_past_end_returns_empty_list(db, regular_user):
    """An offset beyond the last row yields [] instead of clamping or raising.

    This is how the Jobs page behaves when the user pages forward after jobs are
    deleted; it must be an empty page, not an error and not a silent snap back to
    page 1 (which would loop the UI forever).
    """
    for i in range(3):
        await ops.create_job(db, name=f"off{i}", submitted_by=regular_user.id, steps_config=[])
    assert await ops.list_jobs(db, offset=3) == []
    assert await ops.list_jobs(db, offset=9999) == []
    # The boundary: the last valid offset still returns exactly one row.
    assert len(await ops.list_jobs(db, offset=2)) == 1


async def test_list_jobs_combined_filters_intersect_and_paginate(db, regular_user, admin_user):
    """status + submitted_by + pool_id AND together, and compose with paging.

    Each filter is individually covered elsewhere; what is untested is that they
    intersect rather than union. The decoy rows differ from the target in exactly
    one dimension each, so dropping any single predicate makes the assertion
    fail. The final page check proves ``limit``/``offset`` apply *after* the
    filters, not to the unfiltered table.
    """
    pool_a = await ops.create_pool(db, name="pool-a", created_by=admin_user.id)
    pool_b = await ops.create_pool(db, name="pool-b", created_by=admin_user.id)

    async def _job(name, user, pool, status):
        job = await ops.create_job(
            db, name=name, submitted_by=user.id, steps_config=[], target_pool_id=pool.id,
        )
        await ops.update_job(db, job.id, status=status)
        return job

    want1 = await _job("want1", regular_user, pool_a, "running")
    want2 = await _job("want2", regular_user, pool_a, "running")
    await _job("wrong-status", regular_user, pool_a, "completed")   # status differs
    await _job("wrong-user", admin_user, pool_a, "running")         # submitter differs
    await _job("wrong-pool", regular_user, pool_b, "running")       # pool differs

    found = await ops.list_jobs(
        db, status="running", submitted_by=regular_user.id, pool_id=pool_a.id,
    )
    assert {j.name for j in found} == {"want1", "want2"}
    assert {j.id for j in found} == {want1.id, want2.id}

    # Paging applies to the filtered set, newest-first.
    page = await ops.list_jobs(
        db, status="running", submitted_by=regular_user.id, pool_id=pool_a.id,
        limit=1, offset=0,
    )
    assert [j.name for j in page] == ["want2"]
    assert await ops.list_jobs(
        db, status="running", submitted_by=regular_user.id, pool_id=pool_a.id, offset=2,
    ) == []


async def test_list_jobs_unknown_status_returns_empty_list(db, regular_user):
    """An unrecognised status string matches nothing instead of being ignored.

    Contrast with the empty string, which is falsy and therefore skips the filter
    entirely — the two behave completely differently and both reach this op from
    the same optional query parameter.
    """
    await ops.create_job(db, name="s", submitted_by=regular_user.id, steps_config=[])
    assert await ops.list_jobs(db, status="not-a-status") == []
    assert len(await ops.list_jobs(db, status="")) == 1


async def test_append_job_log_accumulates_across_many_chunks(db, regular_user):
    """Many small appends concatenate in order with no inserted separators.

    The runner appends per line/step, so a long job produces hundreds of calls.
    Two things are pinned: strict order preservation (the op is a
    read-concat-write, so a lost read would silently truncate history) and that
    nothing is inserted between chunks — the caller owns its own newlines, which
    is why chunk 50 below has none and must still land mid-line.
    """
    job = await ops.create_job(db, name="chatty", submitted_by=regular_user.id, steps_config=[])
    chunks = [f"line-{i}\n" for i in range(100)]
    chunks[50] = "no-newline-here"  # a chunk without its own terminator
    for chunk in chunks:
        await ops.append_job_log(db, job.id, chunk)

    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.log_text == "".join(chunks)
    assert refreshed.log_text.count("\n") == 99  # 100 chunks, one lost its newline
    # No separator was injected: chunk 50 runs straight into chunk 51.
    assert "no-newline-hereline-51\n" in refreshed.log_text


async def test_append_job_log_empty_chunk_initialises_log_to_empty_string(
    db, regular_user, session_factory
):
    """An empty chunk still commits, turning ``log_text`` from NULL into ''.

    A visible state change for a semantically empty append: ``(None or "") + ""``
    is ``""``, which is a different value from ``None``. Anything distinguishing
    "no log yet" from "empty log" (a ``log_text is None`` check, or the log
    endpoint's 404-vs-200 decision) is affected by a single no-op append. Later
    empty chunks are then genuinely inert. The re-read on a second session proves
    the ``''`` was persisted, not just left on the in-memory object.
    """
    job = await ops.create_job(db, name="emptylog", submitted_by=regular_user.id, steps_config=[])
    assert job.log_text is None

    await ops.append_job_log(db, job.id, "")
    assert (await ops.get_job_by_id(db, job.id)).log_text == ""

    async with _probe_session(session_factory) as other:
        persisted = await ops.get_job_by_id(other, job.id)
        assert persisted.log_text == ""
        assert persisted.log_text is not None

    # An empty chunk appended to real content leaves it byte-identical.
    await ops.append_job_log(db, job.id, "content\n")
    await ops.append_job_log(db, job.id, "")
    await ops.append_job_log(db, job.id, "")
    assert (await ops.get_job_by_id(db, job.id)).log_text == "content\n"


async def test_get_active_jobs_excludes_cancelled_and_failed(db, regular_user):
    """Only pending/queued/running are active — failed and cancelled are terminal.

    ``test_db_ops.py`` covers the ``completed`` exclusion; the other two terminal
    states are the interesting ones because they are the states a job lands in
    *without* finishing its work. If either leaked into this set,
    ``resume_active_jobs`` would re-adopt it on the next server start and re-run
    a job an operator deliberately cancelled.
    """
    async def _job(name, status=None):
        job = await ops.create_job(
            db, name=name, submitted_by=regular_user.id, steps_config=[],
        )
        if status:
            await ops.update_job(db, job.id, status=status)
        return job

    pending = await _job("pending")
    queued = await _job("queued", "queued")
    running = await _job("running", "running")
    cancelled = await _job("cancelled", "cancelled")
    failed = await _job("failed", "failed")
    completed = await _job("completed", "completed")

    active_ids = {j.id for j in await ops.get_active_jobs(db)}
    assert active_ids == {pending.id, queued.id, running.id}
    assert cancelled.id not in active_ids
    assert failed.id not in active_ids
    assert completed.id not in active_ids

    # An unknown//custom status is also excluded — the list is an allow-list.
    weird = await _job("weird", "paused")
    assert weird.id not in {j.id for j in await ops.get_active_jobs(db)}


async def test_get_active_jobs_empty_when_no_jobs_exist(db):
    """With no jobs at all the resume set is [], not None.

    ``resume_active_jobs`` iterates the result directly at startup, so a ``None``
    here would crash a cold server on its first boot.
    """
    assert await ops.get_active_jobs(db) == []


async def test_update_job_silently_drops_unknown_column_name(db, regular_user):
    """A typo'd job field is set as a junk attribute and never persisted.

    Same blind-``setattr`` hazard as ``update_user``, restated on the op the
    runner uses for every state transition — a misspelled ``statuss`` would leave
    a job stuck in ``pending`` with no error anywhere in the logs.
    """
    job = await ops.create_job(db, name="typo", submitted_by=regular_user.id, steps_config=[])
    updated = await ops.update_job(db, job.id, statuss="running")
    assert updated.status == "pending"
    assert (await ops.get_job_by_id(db, job.id)).status == "pending"


# ── Step runs ────────────────────────────────────────────────────────────


async def test_create_step_run_defaults_optional_columns_to_none(db, regular_user):
    """A new step run starts pending with node/state/output/error all unset.

    The row is created *before* dispatch, so this is the shape the job-detail UI
    and the resume logic see for a step that has not started. ``input_params``
    defaults to NULL rather than ``{}``, which matters to any consumer that
    iterates it without a None guard.
    """
    job = await ops.create_job(db, name="sr", submitted_by=regular_user.id, steps_config=[])
    run = await ops.create_step_run(db, job_id=job.id, step_index=0, step_name="compile")
    assert run.status == "pending"
    assert run.node_id is None
    assert run.input_params is None
    assert run.state is None
    assert run.output_params is None
    assert run.error is None
    assert run.started_at is None
    assert run.finished_at is None


async def test_get_step_runs_for_job_unknown_job_returns_empty_list(db):
    """An unknown job id yields [] so the job-detail page can render.

    Filter-based, unlike ``db.get`` — a missing parent produces an empty result
    set rather than None, so callers never need a two-step existence check.
    """
    assert await ops.get_step_runs_for_job(db, str(uuid.uuid4())) == []
    assert await ops.get_latest_step_run(db, str(uuid.uuid4()), 0) is None


async def test_update_step_run_records_node_state_and_terminal_status(db, regular_user):
    """The agent-progress write path: node_id, state, output and 'success'.

    Every field the WebSocket handler sets on a completing step, in one call.
    Pins the naming trap in the docstring: a step run's terminal status is
    ``"success"`` while a job's is ``"completed"``, and nothing validates either
    — writing the wrong one is accepted and the step just never looks finished.
    """
    job = await ops.create_job(db, name="progress", submitted_by=regular_user.id, steps_config=[])
    node = await ops.create_node(db, hostname="worker", os_type="linux")
    run = await ops.create_step_run(db, job_id=job.id, step_index=0, step_name="run")

    updated = await ops.update_step_run(
        db, run.id, node_id=node.id, status="success",
        state={"cwd": "/tmp"}, output_params={"rc": 0}, error=None,
    )
    assert updated.node_id == node.id
    assert updated.status == "success"
    assert updated.state == {"cwd": "/tmp"}
    assert updated.output_params == {"rc": 0}

    # The wrong terminal spelling is accepted without complaint.
    mistyped = await ops.update_step_run(db, run.id, status="completed")
    assert mistyped.status == "completed"


# ── Credentials ──────────────────────────────────────────────────────────


async def test_create_credential_duplicate_name_raises_integrity_error(
    db, regular_user, session_factory
):
    """``credentials.name`` is UNIQUE because steps reference credentials by name.

    Two rows with the same name would make ``get_credential_by_name`` — the
    runner's resolution path for ``credential: "<name>"`` — ambiguous, so the
    constraint is what keeps a step from picking up the wrong secret.
    """
    original = await ops.create_credential(
        db, name="dupe-cred", credential_type="ssh", encrypted_fields=b"a",
        owner_id=regular_user.id,
    )
    async with _probe_session(session_factory) as probe:
        with pytest.raises(IntegrityError):
            await ops.create_credential(
                probe, name="dupe-cred", credential_type="s3", encrypted_fields=b"b",
                owner_id=regular_user.id,
            )
    # The name still resolves to the first (ssh) credential, not the rejected one.
    resolved = await ops.get_credential_by_name(db, "dupe-cred")
    assert resolved.id == original.id
    assert resolved.credential_type == "ssh"


async def test_list_credentials_ordered_by_name_and_unfiltered_by_owner(db, regular_user):
    """Credentials list name-sorted, with no ownership or sharing filter.

    Security-relevant: every row is returned regardless of ``owner_id`` and
    ``is_shared``, so the *route* must filter. A test that only checked ordering
    would let someone "helpfully" add a filter here and silently break the admin
    view; asserting the other user's private credential IS present pins the
    division of responsibility.
    """
    other = await _mk_user(db, "cred-other")
    await ops.create_credential(
        db, name="zeta", credential_type="ssh", encrypted_fields=b"x",
        owner_id=regular_user.id,
    )
    await ops.create_credential(
        db, name="alpha", credential_type="ssh", encrypted_fields=b"x",
        owner_id=other.id, is_shared=False,
    )
    await ops.create_credential(
        db, name="mid", credential_type="ssh", encrypted_fields=b"x", owner_id=regular_user.id,
    )

    listed = await ops.list_credentials(db)
    names = [c.name for c in listed]
    assert names == ["alpha", "mid", "zeta"] == sorted(names)
    # the other user's private credential is included — filtering is the route's job
    assert {c.owner_id for c in listed} == {regular_user.id, other.id}


async def test_update_credential_overwrites_caller_supplied_updated_at(db, regular_user):
    """``updated_at`` is always stamped by the op, ignoring what the caller passes.

    Deliberate per the docstring: the timestamp is server-owned so a client
    cannot backdate a rotation. The assertion is that the sentinel value did NOT
    survive, which is stronger than merely checking the field is set.
    """
    from datetime import datetime, timezone

    cred = await ops.create_credential(
        db, name="stamped", credential_type="ssh", encrypted_fields=b"x",
        owner_id=regular_user.id,
    )
    assert cred.updated_at is None

    backdated = datetime(2000, 1, 1, tzinfo=timezone.utc)
    updated = await ops.update_credential(db, cred.id, description="d", updated_at=backdated)
    assert updated.updated_at is not None
    assert updated.updated_at.replace(tzinfo=timezone.utc) != backdated
    assert updated.updated_at.year >= 2024
    assert updated.description == "d"


async def test_delete_credential_leaves_storage_backend_credential_id_dangling(
    db, regular_user
):
    """Deleting an in-use credential silently breaks the backend that used it.

    The other half of the no-cascade story. The ``storage_backends`` row survives
    with a ``credential_id`` that resolves to nothing, so:
      * the backend still lists and still looks like a valid upload target,
      * ``get_default_storage_backend`` still hands it out,
      * the failure only surfaces at ``StorageManager.init_backends``, which logs
        a warning and continues rather than failing loudly.
    Pinned as current behaviour so that adding a cascade (or a pre-delete
    in-use check) is a visible, deliberate change.
    """
    backend = await _mk_backend(db, regular_user, "orphaned-backend", is_default=True)
    cred_id = backend.credential_id
    assert await ops.get_credential_by_id(db, cred_id) is not None

    assert await ops.delete_credential(db, cred_id) is True
    assert await ops.get_credential_by_id(db, cred_id) is None

    # The backend row is untouched and still points at the deleted credential.
    surviving = (
        await db.execute(select(StorageBackend).where(StorageBackend.id == backend.id))
    ).scalar_one()
    assert surviving.credential_id == cred_id
    assert await ops.get_credential_by_id(db, surviving.credential_id) is None

    # ...and it is still offered as the cluster default.
    assert (await ops.get_default_storage_backend(db)).id == backend.id
    assert backend.id in {b.id for b in await ops.list_storage_backends(db)}


# ── Storage backends ─────────────────────────────────────────────────────


async def test_get_default_storage_backend_none_when_table_empty(db):
    """No backends at all → None, meaning artifact upload has nowhere to go.

    The cold-start shape. ``main.lifespan`` tolerates it, so this must return
    None rather than raising.
    """
    assert await ops.get_default_storage_backend(db) is None


async def test_get_default_storage_backend_raises_when_two_actives_are_default(db, regular_user):
    """Two active is_default backends make the lookup raise MultipleResultsFound.

    Nothing at the DB level enforces a single default, and this op uses
    ``scalar_one_or_none``. So a second "make default" admin call does not
    override the first — it breaks every artifact upload with a 500 until an
    operator un-flags one. The route layer owns that uniqueness.
    """
    first = await _mk_backend(db, regular_user, "default-one", is_default=True, is_active=True)
    second = await _mk_backend(db, regular_user, "default-two", is_default=True, is_active=True)
    first_id, second_id = first.id, second.id

    with pytest.raises(MultipleResultsFound):
        await ops.get_default_storage_backend(db)

    # The query itself succeeded (both rows matched), so the session is still
    # usable — unlike a bind error, this does not need a rollback.
    dupe = await ops.get_storage_backend_by_id(db, second_id)
    dupe.is_active = False
    await db.commit()

    # Deactivating one restores a unique answer: the ``is_active`` predicate is
    # what disambiguates, not the ``is_default`` flag alone.
    found = await ops.get_default_storage_backend(db)
    assert found is not None and found.id == first_id
    assert found.name == "default-one"


async def test_storage_backend_reads_include_inactive_backends(db, regular_user):
    """``get_storage_backend_by_id`` and ``list_storage_backends`` ignore is_active.

    Intentional asymmetry with ``get_default_storage_backend``: historical
    artifacts live on backends that may since have been disabled, and a *read*
    still has to resolve them. Only the write-target selection filters on
    ``is_active``.
    """
    dead = await _mk_backend(db, regular_user, "retired", is_active=False)
    live = await _mk_backend(db, regular_user, "current", is_active=True)

    assert (await ops.get_storage_backend_by_id(db, dead.id)).name == "retired"
    listed = {b.name for b in await ops.list_storage_backends(db)}
    assert listed == {"retired", "current"}
    # ...but the disabled one is never selectable as the default write target.
    assert await ops.get_default_storage_backend(db) is None
    assert live.is_active is True


async def test_create_storage_backend_defaults(db, regular_user):
    """A backend defaults to active, non-default, priority 10, empty config.

    ``priority`` seeds the failover ordering and ``is_active=True`` means a
    freshly registered backend is immediately usable; flipping either default
    would change cluster behaviour with no code change at the call sites.
    """
    backend = await _mk_backend(db, regular_user, "defaults")
    assert backend.is_active is True
    assert backend.is_default is False
    assert backend.priority == 10
    assert backend.config == {}
    assert backend.capacity_bytes is None


# ── Artifacts ────────────────────────────────────────────────────────────


async def test_create_artifact_defaults_and_optional_columns(db, regular_user):
    """Only the four required columns are needed; the rest default sensibly.

    ``size_bytes`` defaults to 0 rather than NULL, which is what lets the results
    UI sum sizes without a None guard. ``step_run_id`` being optional is what
    allows a job-level artifact (the gem5 ``m5out`` tarball) that belongs to no
    single step.
    """
    job = await ops.create_job(db, name="aj", submitted_by=regular_user.id, steps_config=[])
    backend = await _mk_backend(db, regular_user, "art-backend")
    artifact = await ops.create_artifact(
        db, job_id=job.id, filename="results.tar.gz",
        storage_backend_id=backend.id, storage_key="jobs/aj/results.tar.gz",
    )
    assert artifact.size_bytes == 0
    assert artifact.step_run_id is None
    assert artifact.content_type is None
    assert artifact.uploaded_by is None
    assert artifact.created_at is not None
    assert [a.id for a in await ops.list_artifacts_for_job(db, job.id)] == [artifact.id]


# ── Storage transfers ────────────────────────────────────────────────────


async def test_list_transfers_without_status_returns_every_transfer(db, regular_user):
    """No status argument means no filter — pending and finished rows alike.

    The unfiltered call backs the admin "all transfers" view. Asserting the full
    set (rather than just a count) catches a stray default predicate that would
    quietly hide terminal rows.
    """
    job = await ops.create_job(db, name="tj", submitted_by=regular_user.id, steps_config=[])
    src = await _mk_backend(db, regular_user, "t-src")
    dst = await _mk_backend(db, regular_user, "t-dst")
    artifact = await ops.create_artifact(
        db, job_id=job.id, filename="f", storage_backend_id=src.id, storage_key="k",
    )

    async def _transfer(status=None):
        t = await ops.create_transfer(
            db, artifact_id=artifact.id, source_backend_id=src.id, dest_backend_id=dst.id,
        )
        if status:
            await ops.update_transfer(db, t.id, status=status)
        return t

    pending = await _transfer()
    running = await _transfer("running")
    done = await _transfer("completed")
    failed = await _transfer("failed")

    all_ids = {t.id for t in await ops.list_transfers(db)}
    assert all_ids == {pending.id, running.id, done.id, failed.id}
    # An explicit None is the same as omitting it (the filter is truthiness-gated).
    assert {t.id for t in await ops.list_transfers(db, status=None)} == all_ids
    assert {t.id for t in await ops.list_transfers(db, status="")} == all_ids


async def test_list_transfers_status_filter_partitions_the_table(db, regular_user):
    """Each status filter returns exactly its own rows; unknown status gives [].

    The pending filter is the one a copy worker polls with, so it must not pick
    up a running or failed row. Checking that the four filtered sets partition
    the table proves the predicate is an equality match and not a prefix or
    ``IN``-style match.
    """
    job = await ops.create_job(db, name="tj2", submitted_by=regular_user.id, steps_config=[])
    src = await _mk_backend(db, regular_user, "p-src")
    dst = await _mk_backend(db, regular_user, "p-dst")
    artifact = await ops.create_artifact(
        db, job_id=job.id, filename="f", storage_backend_id=src.id, storage_key="k",
    )

    ids = {}
    for status in ("running", "completed", "failed"):
        t = await ops.create_transfer(
            db, artifact_id=artifact.id, source_backend_id=src.id, dest_backend_id=dst.id,
        )
        await ops.update_transfer(db, t.id, status=status)
        ids[status] = t.id
    # one left at the default "pending"
    ids["pending"] = (
        await ops.create_transfer(
            db, artifact_id=artifact.id, source_backend_id=src.id, dest_backend_id=dst.id,
        )
    ).id

    for status, expected_id in ids.items():
        selected = await ops.list_transfers(db, status=status)
        assert [t.id for t in selected] == [expected_id], status

    assert await ops.list_transfers(db, status="nope") == []
    assert await ops.list_transfers(db, status="complete") == []  # not a prefix match


async def test_update_transfer_completion_does_not_repoint_the_artifact(db, regular_user):
    """Marking a transfer complete leaves ``Artifact.storage_backend_id`` alone.

    The gap the docstring warns about: whoever finishes a copy must repoint the
    artifact separately, or downloads keep resolving to the *source* backend even
    though the bytes were moved. Pinning it stops someone assuming this op does
    the bookkeeping.
    """
    job = await ops.create_job(db, name="tj3", submitted_by=regular_user.id, steps_config=[])
    src = await _mk_backend(db, regular_user, "m-src")
    dst = await _mk_backend(db, regular_user, "m-dst")
    artifact = await ops.create_artifact(
        db, job_id=job.id, filename="f", storage_backend_id=src.id, storage_key="k",
    )
    transfer = await ops.create_transfer(
        db, artifact_id=artifact.id, source_backend_id=src.id, dest_backend_id=dst.id,
    )

    done = await ops.update_transfer(db, transfer.id, status="completed", bytes_transferred=99)
    assert done.status == "completed"
    assert done.bytes_transferred == 99

    still_on_source = (
        await db.execute(select(Artifact).where(Artifact.id == artifact.id))
    ).scalar_one()
    assert still_on_source.storage_backend_id == src.id
    assert still_on_source.storage_key == "k"


async def test_create_transfer_defaults(db, regular_user):
    """A new transfer is 'pending' with zero bytes and no timings.

    Creating the row records intent only — no copy is started — so the absence of
    ``started_at`` is what ``list_transfers``' NULLS-LAST ordering depends on.
    """
    job = await ops.create_job(db, name="tj4", submitted_by=regular_user.id, steps_config=[])
    src = await _mk_backend(db, regular_user, "d-src")
    dst = await _mk_backend(db, regular_user, "d-dst")
    artifact = await ops.create_artifact(
        db, job_id=job.id, filename="f", storage_backend_id=src.id, storage_key="k",
    )
    transfer = await ops.create_transfer(
        db, artifact_id=artifact.id, source_backend_id=src.id, dest_backend_id=dst.id,
    )
    assert transfer.status == "pending"
    assert transfer.bytes_transferred == 0
    assert transfer.started_at is None
    assert transfer.completed_at is None
    assert transfer.error is None
    assert transfer.requested_by is None


# ── Saved templates ──────────────────────────────────────────────────────


async def test_create_template_allows_duplicate_names(db, admin_user):
    """``saved_templates.name`` is NOT unique — two templates may share a name.

    Deliberate (documented) and the opposite of every other named entity here, so
    the job-builder UI must disambiguate by id. Asserting both rows exist with
    distinct ids is what would fail if someone "tidied up" the model by adding
    ``unique=True``, which would break existing databases on migration.
    """
    first = await ops.create_template(
        db, name="same-name", steps_config=[{"s": 1}], created_by=admin_user.id,
    )
    second = await ops.create_template(
        db, name="same-name", steps_config=[{"s": 2}], created_by=admin_user.id,
    )
    assert first.id != second.id
    listed = [t for t in await ops.list_templates(db) if t.name == "same-name"]
    assert len(listed) == 2
    assert {t.id for t in listed} == {first.id, second.id}
    # Deleting one leaves the other.
    assert await ops.delete_template(db, first.id) is True
    remaining = [t for t in await ops.list_templates(db) if t.name == "same-name"]
    assert [t.id for t in remaining] == [second.id]


# ── Audit log ────────────────────────────────────────────────────────────


async def test_create_audit_entry_outlives_its_target(db, regular_user):
    """``target_id`` is not a foreign key, so the entry survives target deletion.

    That is the whole point of an audit trail: the row recording "node deleted"
    must still be readable after the node is gone. Pinned because "fixing" this
    into a real FK with a cascade would erase exactly the history the table
    exists to keep.
    """
    node = await ops.create_node(db, hostname="audited", os_type="linux")
    entry = await ops.create_audit_entry(
        db, action="node.delete", user_id=regular_user.id,
        target_type="node", target_id=node.id, details={"hostname": node.hostname},
    )
    assert await ops.delete_node(db, node.id) is True

    surviving = (
        await db.execute(select(AuditLog).where(AuditLog.id == entry.id))
    ).scalar_one()
    assert surviving.target_id == node.id
    assert surviving.details == {"hostname": "audited"}
    assert await ops.get_node_by_id(db, surviving.target_id) is None


# ══ The UUID coercion sweep ══════════════════════════════════════════════
#
# One seeded world, then a parametrized pass over every id-taking op with a real
# ``uuid.UUID`` (``_COERCED_ID_OPS``). The case table is keyed ``op.parameter`` so
# a failure names the exact site.


async def _seed_world(db):
    """Create one row of every entity the sweep needs and return their ids.

    The owning user is deliberately ``role="user"``, not admin, so
    ``check_user_pool_access`` has to traverse membership -> grant instead of
    short-circuiting on the admin bypass.

    Args:
        db: Test session. Every ``ops`` call commits, so the rows are visible to
            the separate probe sessions used by the crash sweep.

    Returns:
        A dict of **string** ids: ``user_id``, ``group_id``, ``group2_id`` (no
        pool grant, so the ``set_group_pool_access`` INSERT branch is reachable),
        ``pool_id``, ``pool2_id`` (empty, so ``add_node_to_pool`` can insert
        without hitting the composite PK), ``node_id``, ``lone_node_id`` (in no
        pool, because ``delete_node`` raises for a pool member — see
        ``test_delete_node_raises_assertion_error_while_still_in_a_pool``),
        ``job_id``, ``step_run_id``, ``cred_id``, ``backend_id``,
        ``artifact_id``, ``transfer_id``, ``template_id``.

    Side effects:
        INSERTs ~16 rows and wires up one pool membership, one group membership
        and one group->pool grant.
    """
    user = await _mk_user(db, "sweep-user")
    group = await ops.create_group(db, name="sweep-group", created_by=user.id)
    group2 = await ops.create_group(db, name="sweep-group-2", created_by=user.id)
    pool = await ops.create_pool(db, name="sweep-pool", created_by=user.id)
    pool2 = await ops.create_pool(db, name="sweep-pool-2", created_by=user.id)
    node = await ops.create_node(db, hostname="sweep-node", os_type="linux", status="online")
    lone_node = await ops.create_node(db, hostname="sweep-lone", os_type="linux")
    job = await ops.create_job(
        db, name="sweep-job", submitted_by=user.id, steps_config=[],
        target_pool_id=pool.id, target_node_id=node.id,
    )
    step_run = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="sweep-step",
    )
    cred = await ops.create_credential(
        db, name="sweep-cred", credential_type="s3", encrypted_fields=b"x", owner_id=user.id,
    )
    backend = await ops.create_storage_backend(
        db, name="sweep-backend", backend_type="minio", credential_id=cred.id,
    )
    artifact = await ops.create_artifact(
        db, job_id=job.id, filename="f", storage_backend_id=backend.id, storage_key="k",
    )
    transfer = await ops.create_transfer(
        db, artifact_id=artifact.id, source_backend_id=backend.id, dest_backend_id=backend.id,
    )
    template = await ops.create_template(
        db, name="sweep-tpl", steps_config=[], created_by=user.id,
    )
    await ops.add_node_to_pool(db, pool.id, node.id)
    await ops.add_user_to_group(db, user.id, group.id)
    await ops.set_group_pool_access(db, group.id, pool.id)

    return {
        "user_id": user.id, "group_id": group.id, "group2_id": group2.id,
        "pool_id": pool.id, "pool2_id": pool2.id, "node_id": node.id,
        "lone_node_id": lone_node.id,
        "job_id": job.id, "step_run_id": step_run.id, "cred_id": cred.id,
        "backend_id": backend.id, "artifact_id": artifact.id,
        "transfer_id": transfer.id, "template_id": template.id,
    }


# ``op.parameter`` -> (call, result predicate). Every call passes a real
# ``uuid.UUID`` where the schema stores a ``String(36)``; these are the sites
# that coerce with ``_sid`` / ``_sid_kwargs`` — which, as of the sweep that
# closed this bug class, is every id-taking op and op *branch* in the module,
# including the caller-supplied primary key ``create_node(id=...)``. The predicate is
# not decoration: several of these ops fail *silently* when coercion is missing —
# a string column compared against a UUID object simply matches no row — so
# "did not raise" alone would not detect a regression.
_COERCED_ID_OPS: dict = {
    "get_user_by_id.user_id": (
        lambda s, w: ops.get_user_by_id(s, U(w["user_id"])),
        lambda r, w: r is not None and r.id == w["user_id"],
    ),
    "update_user.user_id": (
        lambda s, w: ops.update_user(s, U(w["user_id"]), email="swept@example.com"),
        lambda r, w: r is not None and r.email == "swept@example.com",
    ),
    "create_group.created_by": (
        lambda s, w: ops.create_group(s, name="swept-grp", created_by=U(w["user_id"])),
        lambda r, w: r.created_by == w["user_id"] and isinstance(r.created_by, str),
    ),
    # A caller-supplied PRIMARY key. _sid_kwargs matches a bare "id" as well as
    # the _id/_by suffixes, so this is covered too; it was the last hole in the
    # sweep and is kept as the guard against that special case regressing.
    "create_node.id": (
        lambda s, w: ops.create_node(
            s, id=_NODE_PK, hostname="swept-node-2", os_type="linux",
        ),
        lambda r, w: r.id == str(_NODE_PK) and isinstance(r.id, str),
    ),
    # ``add_user_to_group`` does not ``refresh``, so the returned membership
    # carries exactly what the constructor stored — the ``isinstance`` check is
    # what proves ``_sid`` ran rather than the DB round-trip doing it for us.
    "add_user_to_group.user_id": (
        lambda s, w: ops.add_user_to_group(s, U(w["user_id"]), w["group2_id"]),
        lambda r, w: r.user_id == w["user_id"] and isinstance(r.user_id, str),
    ),
    "add_user_to_group.group_id": (
        lambda s, w: ops.add_user_to_group(s, w["user_id"], U(w["group2_id"])),
        lambda r, w: r.group_id == w["group2_id"] and isinstance(r.group_id, str),
    ),
    "remove_user_from_group.pair": (
        lambda s, w: ops.remove_user_from_group(s, U(w["user_id"]), U(w["group_id"])),
        lambda r, w: r is None,
    ),
    "set_group_pool_access.update_branch": (
        lambda s, w: ops.set_group_pool_access(
            s, U(w["group_id"]), U(w["pool_id"]), permission="admin",
        ),
        lambda r, w: r is not None and r.permission == "admin",
    ),
    # ``group2_id`` holds no grant yet, so this takes the INSERT half of the
    # upsert — a separate construction site from the UPDATE branch above.
    "set_group_pool_access.insert_branch": (
        lambda s, w: ops.set_group_pool_access(s, U(w["group2_id"]), U(w["pool_id"])),
        lambda r, w: (
            r.group_id == w["group2_id"] and r.pool_id == w["pool_id"]
            and isinstance(r.group_id, str) and isinstance(r.pool_id, str)
        ),
    ),
    "check_user_pool_access.pair": (
        lambda s, w: ops.check_user_pool_access(s, U(w["user_id"]), U(w["pool_id"])),
        lambda r, w: r is True,
    ),
    "get_node_by_id.node_id": (
        lambda s, w: ops.get_node_by_id(s, U(w["node_id"])),
        lambda r, w: r is not None and r.id == w["node_id"],
    ),
    "list_nodes.pool_id": (
        lambda s, w: ops.list_nodes(s, pool_id=U(w["pool_id"])),
        lambda r, w: [n.id for n in r] == [w["node_id"]],
    ),
    "update_node.node_id": (
        lambda s, w: ops.update_node(s, U(w["node_id"]), status="offline"),
        lambda r, w: r is not None and r.status == "offline",
    ),
    "delete_node.node_id": (
        # ``lone_node_id`` deliberately belongs to no pool: ``delete_node`` raises
        # AssertionError for a pool member, which would mask the coercion check.
        lambda s, w: ops.delete_node(s, U(w["lone_node_id"])),
        lambda r, w: r is True,
    ),
    "create_pool.created_by": (
        lambda s, w: ops.create_pool(s, name="swept-pool", created_by=U(w["user_id"])),
        lambda r, w: r.created_by == w["user_id"] and isinstance(r.created_by, str),
    ),
    "get_pool_by_id.pool_id": (
        lambda s, w: ops.get_pool_by_id(s, U(w["pool_id"])),
        lambda r, w: r is not None and r.id == w["pool_id"],
    ),
    "add_node_to_pool.pair": (
        lambda s, w: ops.add_node_to_pool(s, U(w["pool2_id"]), U(w["node_id"])),
        lambda r, w: r is not None and r.pool_id == w["pool2_id"],
    ),
    "remove_node_from_pool.pair": (
        lambda s, w: ops.remove_node_from_pool(s, U(w["pool_id"]), U(w["node_id"])),
        lambda r, w: r is None,
    ),
    "get_pool_nodes.pool_id": (
        lambda s, w: ops.get_pool_nodes(s, U(w["pool_id"])),
        lambda r, w: [n.id for n in r] == [w["node_id"]],
    ),
    "create_job.submitted_by": (
        lambda s, w: ops.create_job(
            s, name="swept", submitted_by=U(w["user_id"]), steps_config=[],
        ),
        lambda r, w: r.submitted_by == w["user_id"],
    ),
    "create_job.target_pool_id": (
        lambda s, w: ops.create_job(
            s, name="swept", submitted_by=w["user_id"], steps_config=[],
            target_pool_id=U(w["pool_id"]),
        ),
        lambda r, w: r.target_pool_id == w["pool_id"],
    ),
    "create_job.target_node_id": (
        lambda s, w: ops.create_job(
            s, name="swept", submitted_by=w["user_id"], steps_config=[],
            target_node_id=U(w["node_id"]),
        ),
        lambda r, w: r.target_node_id == w["node_id"],
    ),
    "get_job_by_id.job_id": (
        lambda s, w: ops.get_job_by_id(s, U(w["job_id"])),
        lambda r, w: r is not None and r.id == w["job_id"],
    ),
    "list_jobs.submitted_by": (
        lambda s, w: ops.list_jobs(s, submitted_by=U(w["user_id"])),
        lambda r, w: [j.id for j in r] == [w["job_id"]],
    ),
    "list_jobs.pool_id": (
        lambda s, w: ops.list_jobs(s, pool_id=U(w["pool_id"])),
        lambda r, w: [j.id for j in r] == [w["job_id"]],
    ),
    "update_job.job_id": (
        lambda s, w: ops.update_job(s, U(w["job_id"]), status="running"),
        lambda r, w: r is not None and r.status == "running",
    ),
    # The kwargs half of ``update_job``: the blind ``setattr`` payload is routed
    # through ``_sid_kwargs``, so an id *value* is coerced too, not just the PK.
    # ``lone_node_id`` differs from the seeded ``target_node_id``, so the
    # assertion fails if the write silently did not land.
    "update_job.kwargs_target_node_id": (
        lambda s, w: ops.update_job(s, w["job_id"], target_node_id=U(w["lone_node_id"])),
        lambda r, w: r is not None and r.target_node_id == w["lone_node_id"],
    ),
    "append_job_log.job_id": (
        lambda s, w: ops.append_job_log(s, U(w["job_id"]), "swept\n"),
        lambda r, w: r is None,
    ),
    "create_step_run.job_id": (
        lambda s, w: ops.create_step_run(
            s, job_id=U(w["job_id"]), step_index=1, step_name="swept",
        ),
        lambda r, w: r.job_id == w["job_id"] and r.step_index == 1,
    ),
    "update_step_run.step_run_id": (
        lambda s, w: ops.update_step_run(s, U(w["step_run_id"]), status="success"),
        lambda r, w: r is not None and r.status == "success",
    ),
    # The seeded step run starts with ``node_id=None``, so this is a real write:
    # a dropped coercion in the ``**kwargs`` path shows up here, not just on the PK.
    "update_step_run.kwargs_node_id": (
        lambda s, w: ops.update_step_run(s, w["step_run_id"], node_id=U(w["node_id"])),
        lambda r, w: r is not None and r.node_id == w["node_id"],
    ),
    "get_step_runs_for_job.job_id": (
        lambda s, w: ops.get_step_runs_for_job(s, U(w["job_id"])),
        lambda r, w: [x.id for x in r] == [w["step_run_id"]],
    ),
    "get_latest_step_run.job_id": (
        lambda s, w: ops.get_latest_step_run(s, U(w["job_id"]), 0),
        lambda r, w: r is not None and r.id == w["step_run_id"],
    ),
    "create_credential.owner_id": (
        lambda s, w: ops.create_credential(
            s, name="swept-cred", credential_type="s3", encrypted_fields=b"x",
            owner_id=U(w["user_id"]),
        ),
        lambda r, w: r.owner_id == w["user_id"],
    ),
    "get_credential_by_id.cred_id": (
        lambda s, w: ops.get_credential_by_id(s, U(w["cred_id"])),
        lambda r, w: r is not None and r.id == w["cred_id"],
    ),
    "update_credential.cred_id": (
        lambda s, w: ops.update_credential(s, U(w["cred_id"]), description="swept"),
        lambda r, w: r is not None and r.description == "swept",
    ),
    # Re-owning a credential is the ``**kwargs`` path: ``owner_id`` is an id
    # *value*, and ``_sid_kwargs`` has to catch it before the blind ``setattr``.
    "update_credential.kwargs_owner_id": (
        lambda s, w: ops.update_credential(s, w["cred_id"], owner_id=U(w["user_id"])),
        lambda r, w: r is not None and r.owner_id == w["user_id"],
    ),
    "delete_credential.cred_id": (
        lambda s, w: ops.delete_credential(s, U(w["cred_id"])),
        lambda r, w: r is True,
    ),
    "create_storage_backend.credential_id": (
        lambda s, w: ops.create_storage_backend(
            s, name="swept-backend", backend_type="minio", credential_id=U(w["cred_id"]),
        ),
        lambda r, w: r.credential_id == w["cred_id"],
    ),
    "get_storage_backend_by_id.backend_id": (
        lambda s, w: ops.get_storage_backend_by_id(s, U(w["backend_id"])),
        lambda r, w: r is not None and r.id == w["backend_id"],
    ),
    "create_artifact.job_id": (
        lambda s, w: ops.create_artifact(
            s, job_id=U(w["job_id"]), filename="swept", storage_key="sk",
            storage_backend_id=w["backend_id"],
        ),
        lambda r, w: r.job_id == w["job_id"],
    ),
    "create_artifact.storage_backend_id": (
        lambda s, w: ops.create_artifact(
            s, job_id=w["job_id"], filename="swept", storage_key="sk",
            storage_backend_id=U(w["backend_id"]),
        ),
        lambda r, w: r.storage_backend_id == w["backend_id"],
    ),
    "create_artifact.step_run_id": (
        lambda s, w: ops.create_artifact(
            s, job_id=w["job_id"], filename="swept", storage_key="sk",
            storage_backend_id=w["backend_id"], step_run_id=U(w["step_run_id"]),
        ),
        lambda r, w: r.step_run_id == w["step_run_id"],
    ),
    # ``uploaded_by`` is a ``String(36)`` FK to ``users.id`` that does not use the
    # ``_id`` suffix. ``_sid_kwargs`` matches ``_by`` as well precisely so this
    # works; when it matched only ``_id`` this crashed *after* the bytes were
    # already written to the backend, orphaning them with no row.
    "create_artifact.uploaded_by": (
        lambda s, w: ops.create_artifact(
            s, job_id=w["job_id"], filename="swept", storage_key="sk",
            storage_backend_id=w["backend_id"], uploaded_by=U(w["user_id"]),
        ),
        lambda r, w: r.uploaded_by == w["user_id"],
    ),
    "list_artifacts_for_job.job_id": (
        lambda s, w: ops.list_artifacts_for_job(s, U(w["job_id"])),
        lambda r, w: [a.id for a in r] == [w["artifact_id"]],
    ),
    "get_artifact_by_id.artifact_id": (
        lambda s, w: ops.get_artifact_by_id(s, U(w["artifact_id"])),
        lambda r, w: r is not None and r.id == w["artifact_id"],
    ),
    "create_transfer.artifact_id": (
        lambda s, w: ops.create_transfer(
            s, artifact_id=U(w["artifact_id"]), source_backend_id=w["backend_id"],
            dest_backend_id=w["backend_id"],
        ),
        lambda r, w: r.artifact_id == w["artifact_id"],
    ),
    "create_transfer.backend_ids": (
        lambda s, w: ops.create_transfer(
            s, artifact_id=w["artifact_id"], source_backend_id=U(w["backend_id"]),
            dest_backend_id=U(w["backend_id"]),
        ),
        lambda r, w: r.source_backend_id == w["backend_id"] == r.dest_backend_id,
    ),
    # The other ``_by`` column: ``StorageTransfer.requested_by``. Same suffix
    # story as ``create_artifact.uploaded_by`` above.
    "create_transfer.requested_by": (
        lambda s, w: ops.create_transfer(
            s, artifact_id=w["artifact_id"], source_backend_id=w["backend_id"],
            dest_backend_id=w["backend_id"], requested_by=U(w["user_id"]),
        ),
        lambda r, w: r.requested_by == w["user_id"],
    ),
    "update_transfer.transfer_id": (
        lambda s, w: ops.update_transfer(s, U(w["transfer_id"]), status="running"),
        lambda r, w: r is not None and r.status == "running",
    ),
    "update_transfer.kwargs_artifact_id": (
        lambda s, w: ops.update_transfer(s, w["transfer_id"], artifact_id=U(w["artifact_id"])),
        lambda r, w: r is not None and r.artifact_id == w["artifact_id"],
    ),
    "create_template.created_by": (
        lambda s, w: ops.create_template(
            s, name="swept-tpl-2", steps_config=[], created_by=U(w["user_id"]),
        ),
        lambda r, w: r.created_by == w["user_id"],
    ),
    "delete_template.template_id": (
        lambda s, w: ops.delete_template(s, U(w["template_id"])),
        lambda r, w: r is True,
    ),
    # ``create_audit_entry`` does not ``refresh`` either, so these predicates read
    # straight off the constructed instance.
    "create_audit_entry.user_id": (
        lambda s, w: ops.create_audit_entry(s, action="swept", user_id=U(w["user_id"])),
        lambda r, w: r.user_id == w["user_id"] and isinstance(r.user_id, str),
    ),
    "create_audit_entry.target_id": (
        lambda s, w: ops.create_audit_entry(s, action="swept", target_id=U(w["user_id"])),
        lambda r, w: r.target_id == w["user_id"] and isinstance(r.target_id, str),
    ),
}




@pytest.mark.parametrize("case", sorted(_COERCED_ID_OPS))
async def test_id_taking_op_accepts_uuid_object(db, case):
    """Every ``_sid``-protected op works when handed a real ``uuid.UUID``.

    FastAPI parses a ``{id}`` path param declared as ``UUID`` into a
    ``uuid.UUID``, and routes pass it straight down, so this is the argument type
    production actually supplies. The per-case predicate checks the op returned
    the RIGHT row, not merely that it did not raise — a missing coercion on a
    filter-based op (``list_jobs``, ``get_pool_nodes``) produces an empty result
    rather than an error, which is the harder regression to notice.

    This is the regression guard for the whole ``_sid`` sweep: the table now
    covers every id-taking op and op *branch* in ``db/ops.py`` (constructor args,
    ``db.get`` primary keys, filter predicates, and the ``**kwargs`` payloads of
    all six blind-``setattr`` updaters), so removing one ``_sid()`` /
    ``_sid_kwargs()`` call fails the case named after that exact site.

    Args:
        db: Shared test session; these calls all succeed, so no isolation is
            needed.
        case: ``"<op>.<parameter>"`` key into ``_COERCED_ID_OPS``.
    """
    call, check = _COERCED_ID_OPS[case]
    world = await _seed_world(db)
    result = await call(db, world)
    assert check(result, world), f"{case} returned the wrong result: {result!r}"




async def test_sid_coerces_uuid_and_passes_none_through():
    """``_sid`` stringifies a UUID, leaves a str alone, and preserves None.

    The None passthrough is load-bearing: ``create_job`` feeds
    ``_sid(target_pool_id)`` into a nullable column, so coercing None to the
    string ``"None"`` would write a garbage FK instead of NULL. The permissive
    no-validation behaviour is also deliberate — already-string ids must not pay
    a parse cost or start rejecting non-UUID keys.
    """
    real = uuid.uuid4()
    assert ops._sid(real) == str(real)
    assert isinstance(ops._sid(real), str)
    assert ops._sid(str(real)) == str(real)
    assert ops._sid(None) is None
    # intentionally permissive: no UUID validation happens
    assert ops._sid("not-a-uuid") == "not-a-uuid"


async def test_sid_kwargs_coerces_id_and_by_suffixes_and_passes_others_through():
    """``_sid_kwargs`` stringifies ``*_id`` **and** ``*_by`` keys, nothing else.

    Both suffixes are load-bearing. An earlier version matched only ``_id`` and
    silently missed ``Artifact.uploaded_by`` and ``StorageTransfer.requested_by``
    — both ``String(36)`` FKs to ``users.id`` that do not follow the ``_id``
    naming convention — so ``uploaded_by=UUID(...)`` still crashed on bind, and
    for artifacts it crashed *after* the bytes were written to the backend,
    orphaning them with no row.

    The other half of the contract matters just as much: matching is by key
    suffix, not by value type, so a non-id column must come through *untouched*.
    Stringifying ``status`` or ``name`` would be a silent data corruption for
    every non-string column (``size_bytes``, ``priority``, JSON blobs,
    ``datetime``s) that flows through the same ``**kwargs`` updaters.
    """
    val = uuid.uuid4()
    out = ops._sid_kwargs(
        {"job_id": val, "uploaded_by": val, "requested_by": val,
         "created_by": val, "submitted_by": val, "step_run_id": None,
         "filename": "f", "size_bytes": 3, "status": "pending", "name": "n"}
    )
    # ``_id`` suffix → coerced.
    assert out["job_id"] == str(val) and isinstance(out["job_id"], str)
    # ``_by`` suffix → coerced too (this is the part that used to be missed).
    for key in ("uploaded_by", "requested_by", "created_by", "submitted_by"):
        assert out[key] == str(val), key
        assert isinstance(out[key], str), key
    # None survives as NULL rather than becoming the string "None".
    assert out["step_run_id"] is None
    # Everything else is passed through by identity, not stringified.
    assert out["filename"] == "f"
    assert out["size_bytes"] == 3 and isinstance(out["size_bytes"], int)
    assert out["status"] == "pending"
    assert out["name"] == "n"
    # An already-string id is untouched (no parse, no validation).
    assert ops._sid_kwargs({"job_id": "abc"})["job_id"] == "abc"
    # The flip side of suffix matching: a non-id column ending in ``_id`` or
    # ``_by`` would be stringified anyway. Acceptable — the project uses those
    # suffixes exclusively for id columns.
    assert ops._sid_kwargs({"grid_id": 7})["grid_id"] == "7"
    assert ops._sid_kwargs({"sorted_by": 7})["sorted_by"] == "7"
    # Non-mutating: a new dict is returned and the input is left alone.
    original = {"job_id": val}
    assert ops._sid_kwargs(original) is not original
    assert original["job_id"] is val
    assert ops._sid_kwargs({}) == {}


async def test_uuid_bind_error_poisons_the_session_until_rollback(db, session_factory):
    """One raw-UUID bind makes every later statement on that session raise.

    The mechanism behind the outage the whole ``_sid`` effort was about. Every op
    in ``db/ops.py`` now coerces, so this can no longer be provoked *through*
    ``ops`` — the trigger here is the underlying bind itself: a ``StepRun`` whose
    ``job_id`` (a ``String(36)`` column) holds a real ``uuid.UUID``, added to the
    session and committed directly through the ORM. That is exactly what
    ``ops.create_step_run`` used to do before it wrapped the argument in ``_sid``.

    What the poisoning looks like: the bind error fires inside ``commit()``, so
    the session is left in "needs rollback" state and the next unrelated read —
    a heartbeat update, another step message — fails with ``PendingRollbackError``
    rather than with anything naming the real cause. In the agent WebSocket
    handler that killed the socket on every step message, producing a reconnect
    storm and stuck jobs.

    Still worth pinning even with the ops layer fixed:
      * it is why the intentionally-crashing probes in this file need isolated
        sessions, and why ``_probe_session`` exists at all;
      * no ``ops`` function can reach this state any more, but any future code
        that binds a UUID without going through ``ops`` still can — which is why
        this pins the mechanic directly against the ORM;
      * an explicit ``rollback()`` is the only recovery, which is what every
        route and handler that can hit a bind error needs.
    """
    world = await _seed_world(db)

    async with _probe_session(session_factory) as probe:
        # This read works fine before the poisoning.
        assert (await ops.get_job_by_id(probe, world["job_id"])) is not None

        # Bind a raw uuid.UUID straight onto StepRun.job_id — String(36) — with
        # no _sid() in the way. aiosqlite refuses the parameter at bind time.
        probe.add(
            StepRun(
                job_id=U(world["job_id"]), step_index=1, step_name="poison",
                input_params=None,
            )
        )
        with pytest.raises(ProgrammingError) as exc_info:
            await probe.commit()
        assert "UUID" in str(exc_info.value)

        # Now an unrelated, perfectly valid read fails with a different error
        # that says nothing about UUIDs.
        with pytest.raises(PendingRollbackError):
            await ops.get_user_by_id(probe, world["user_id"])

        # Only an explicit rollback restores the session.
        await probe.rollback()
        assert (await ops.get_user_by_id(probe, world["user_id"])) is not None
        # ...and the poisoned row was discarded, not quietly written.
        assert [r.step_index for r in await ops.get_step_runs_for_job(probe, world["job_id"])] == [0]
