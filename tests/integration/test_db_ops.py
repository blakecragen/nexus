"""Integration tests for the repository layer (nexus_server.db.ops).

These exercise the real async ops against the in-memory SQLite DB provided by
the ``db`` fixture. We cover positive paths, negative/missing-id paths, filter
combinations, ordering guarantees, and the ``_sid`` UUID/str coercion that lets
callers pass either a ``uuid.UUID`` or a plain string.

Position in the stack
    ``ops`` is the ONLY module that talks to the ORM. Routes
    (``api/routes/*.py``), the job runner and the WebSocket agent handler all go
    through it, so a regression here fans out everywhere. Nothing is stubbed in
    this file — it is the repository layer against a real (in-memory) database.

The ``_sid`` invariant (why so many tests pass a ``uuid.UUID``)
    Every id column is ``String(36)``, but callers routinely hold a
    ``uuid.UUID`` (FastAPI converts ``{id}`` path params to ``UUID``). The
    helper ``ops._sid()`` normalises either form to ``str``. Ops that forget to
    call it either crash the SQLite driver ("type 'UUID' is not supported") or —
    worse — compare a string column against a UUID object and silently return
    nothing. That silent-wrong-answer class of bug is why several tests here
    deliberately pass ``uuid.UUID(...)`` where a string would work.

xfail policy
    ``pytest.mark.xfail(strict=True)`` marks a *known source bug*, not a flaky
    test. Each one names the offending function and what is missing. When the
    source is fixed the strict marker turns the now-passing test into a failure,
    which is the signal to delete the marker. Do not "fix" these by relaxing the
    assertion.
"""

from __future__ import annotations

import uuid

import pytest

from nexus_server.db import ops
from nexus_server.services.auth_service import AuthService


# ── helpers ──────────────────────────────────────────────────────────────


async def _mk_user(db, username="bob", role="user"):
    """Persist a user with a hashed throwaway password.

    Args:
        db: Test ``AsyncSession``.
        username: Unique username; also used to derive the email.
        role: ``"user"`` or ``"admin"`` (admins bypass pool access checks).

    Returns:
        The persisted ``User``.

    Side effects:
        INSERTs a row into ``users``. Every call must use a distinct username —
        the column is unique and tests share one database per test function.
    """
    return await ops.create_user(
        db, username=username, password_hash=AuthService.hash_password("pw"),
        email=f"{username}@example.com", role=role,
    )


# ── Users ────────────────────────────────────────────────────────────────


async def test_create_user_persists_with_generated_api_key(db):
    """create_user assigns an id, an api_key and defaults is_active to True.

    The auto-generated ``api_key`` is what the CLI and agent use for
    non-interactive auth, so silently dropping it would break those clients
    without breaking the web login. This pins that it is minted at creation
    time rather than lazily.
    """
    user = await _mk_user(db, "carol")
    assert user.id is not None
    assert user.username == "carol"
    assert user.role == "user"
    assert user.api_key  # auto-generated token
    assert user.is_active is True


async def test_get_user_by_id_accepts_str_and_uuid(db):
    """get_user_by_id works with both a str id and a uuid.UUID (``_sid`` path).

    JWT subjects arrive as strings while FastAPI path params arrive as
    ``UUID``; both reach this op. Losing the ``_sid`` coercion would make the
    UUID form raise a SQLite bind error, 500-ing authenticated requests.
    """
    user = await _mk_user(db, "dave")
    # id is stored as a string; passing the raw string works
    by_str = await ops.get_user_by_id(db, user.id)
    assert by_str is not None and by_str.username == "dave"
    # _sid coerces a real uuid.UUID to the matching string form
    by_uuid = await ops.get_user_by_id(db, uuid.UUID(user.id))
    assert by_uuid is not None and by_uuid.id == user.id


async def test_get_user_by_id_missing_returns_none(db):
    """A well-formed but unknown id returns None rather than raising.

    Callers (notably ``deps.get_current_user``) branch on None to emit a clean
    401; an exception here would surface as a 500 for a merely-stale token.
    """
    assert await ops.get_user_by_id(db, uuid.uuid4()) is None


async def test_get_user_by_username(db):
    """Username lookup finds an existing user and returns None for an unknown one.

    This is the first step of password login, so the None path is what makes
    "unknown user" indistinguishable from "wrong password" at the route layer.
    """
    await _mk_user(db, "erin")
    found = await ops.get_user_by_username(db, "erin")
    assert found is not None and found.username == "erin"
    assert await ops.get_user_by_username(db, "nobody") is None


async def test_get_user_by_api_key(db):
    """API-key lookup resolves the owning user and rejects an unknown key.

    Security-relevant: a bogus key must return None (→ 401), never a fallback
    or the first user in the table.
    """
    user = await _mk_user(db, "frank")
    found = await ops.get_user_by_api_key(db, user.api_key)
    assert found is not None and found.id == user.id
    assert await ops.get_user_by_api_key(db, "not-a-real-key") is None


async def test_list_users_ordered_by_username(db):
    """list_users returns users sorted by username, not insertion order.

    The admin UI renders this list verbatim, so the ORDER BY is part of the
    contract. Inserting z/a/m and asserting the output equals its own sorted
    form catches an accidentally-dropped ``order_by``.
    """
    await _mk_user(db, "zoe")
    await _mk_user(db, "amy")
    await _mk_user(db, "mike")
    users = await ops.list_users(db)
    names = [u.username for u in users]
    assert names == sorted(names)
    assert {"zoe", "amy", "mike"}.issubset(set(names))


async def test_update_user_sets_fields(db):
    """update_user applies arbitrary keyword fields and returns the fresh row.

    Covers role escalation via the admin UI (``role="admin"``), so the returned
    object must reflect the committed values, not the pre-update state.
    """
    user = await _mk_user(db, "gary")
    updated = await ops.update_user(db, user.id, email="new@example.com", role="admin")
    assert updated is not None
    assert updated.email == "new@example.com"
    assert updated.role == "admin"


async def test_update_user_missing_returns_none(db):
    """Updating a non-existent user returns None instead of raising."""
    # update_user uses ``db.get(User, user_id)`` directly (no _sid), so a missing
    # *string* id returns None cleanly.
    assert await ops.update_user(db, str(uuid.uuid4()), email="x") is None


async def test_update_user_accepts_uuid_object(db):
    """update_user should accept a uuid.UUID id like its sibling read ops do.

    Documents the asymmetry: ``get_user_by_id`` coerces via ``_sid`` but
    ``update_user`` does not, so any route that hands it a path-param ``UUID``
    500s. Marked strict-xfail so fixing the source flags this test for cleanup.
    """
    user = await _mk_user(db, "uuidupd")
    updated = await ops.update_user(db, uuid.UUID(user.id), email="z@example.com")
    assert updated is not None and updated.email == "z@example.com"


# ── Groups & memberships & pool access ─────────────────────────────────────


async def test_create_and_list_groups_ordered(db):
    """Groups are created with an optional description and listed name-sorted."""
    admin = await _mk_user(db, "ga", role="admin")
    await ops.create_group(db, name="zulu", created_by=admin.id)
    await ops.create_group(db, name="alpha", created_by=admin.id, description="first")
    groups = await ops.list_groups(db)
    names = [g.name for g in groups]
    assert names == sorted(names)


async def test_add_and_remove_user_from_group(db):
    """Group membership can be created with a role and then revoked.

    Membership is the mechanism behind pool authorization, so revocation must
    actually delete the row — a stale membership would leave a removed user
    with lingering access to every pool the group can reach.
    """
    admin = await _mk_user(db, "ga2", role="admin")
    member = await _mk_user(db, "member1")
    group = await ops.create_group(db, name="devs", created_by=admin.id)

    membership = await ops.add_user_to_group(db, member.id, group.id, role_in_group="lead")
    assert membership.role_in_group == "lead"
    assert membership.user_id == member.id

    # remove (uses _sid coercion internally; pass UUID to exercise it)
    await ops.remove_user_from_group(db, uuid.UUID(member.id), uuid.UUID(group.id))
    # access via the group should now be gone — verify through a pool check below
    assert await ops.check_user_pool_access(db, member.id, uuid.uuid4()) is False


async def test_add_user_to_group_defaults_to_member_role(db):
    """Omitting role_in_group stores the least-privileged default, 'member'."""
    admin = await _mk_user(db, "ga2b", role="admin")
    member = await _mk_user(db, "member2")
    group = await ops.create_group(db, name="defgrp", created_by=admin.id)
    membership = await ops.add_user_to_group(db, member.id, group.id)
    assert membership.role_in_group == "member"


async def test_set_group_pool_access_inserts_then_upserts(db):
    """set_group_pool_access upserts: a re-grant updates rather than duplicates.

    ``GroupPoolAccess`` is keyed on (group_id, pool_id). If the second call
    INSERTed instead of UPDATEing, either the PK constraint would blow up or —
    worse — two rows with conflicting permissions would exist and the effective
    permission would depend on row order. The explicit COUNT is the real
    assertion; the permission check alone would pass either way.
    """
    admin = await _mk_user(db, "ga3", role="admin")
    group = await ops.create_group(db, name="qa", created_by=admin.id)
    pool = await ops.create_pool(db, name="qa-pool", created_by=admin.id)

    access = await ops.set_group_pool_access(db, group.id, pool.id, permission="submit")
    assert access.permission == "submit"

    # second call updates the same row instead of inserting a duplicate PK
    access2 = await ops.set_group_pool_access(db, group.id, pool.id, permission="admin")
    assert access2.permission == "admin"
    assert access2.group_id == group.id and access2.pool_id == pool.id

    # the upsert must NOT have created a second row for the same (group, pool)
    from sqlalchemy import func, select

    from nexus_server.db.models import GroupPoolAccess

    count = (
        await db.execute(
            select(func.count())
            .select_from(GroupPoolAccess)
            .where(
                GroupPoolAccess.group_id == group.id,
                GroupPoolAccess.pool_id == pool.id,
            )
        )
    ).scalar_one()
    assert count == 1
    # default permission when not specified is "submit"
    grp2 = await ops.create_group(db, name="qa2", created_by=admin.id)
    default_access = await ops.set_group_pool_access(db, grp2.id, pool.id)
    assert default_access.permission == "submit"


async def test_check_user_pool_access_admin_bypass(db):
    """role='admin' grants pool access without any group grant.

    Security-sensitive shortcut: admins skip the join entirely. Pinning it here
    means an attempt to tighten the query cannot accidentally lock admins out
    of pools they never explicitly joined.
    """
    admin = await _mk_user(db, "superadmin", role="admin")
    pool = await ops.create_pool(db, name="any-pool", created_by=admin.id)
    # admin has no group membership at all, but role bypasses the check
    assert await ops.check_user_pool_access(db, admin.id, pool.id) is True


async def test_check_user_pool_access_granted_via_group(db):
    """A non-admin gains pool access transitively: user → group → pool grant.

    This is the whole point of the groups feature; both hops must be present
    for the check to succeed.
    """
    admin = await _mk_user(db, "ga4", role="admin")
    user = await _mk_user(db, "grantee")
    group = await ops.create_group(db, name="grant-grp", created_by=admin.id)
    pool = await ops.create_pool(db, name="grant-pool", created_by=admin.id)

    await ops.add_user_to_group(db, user.id, group.id)
    await ops.set_group_pool_access(db, group.id, pool.id)

    assert await ops.check_user_pool_access(db, user.id, pool.id) is True


async def test_check_user_pool_access_accepts_uuid_object(db):
    """The authorization check should tolerate UUID ids on BOTH parameters.

    Half-coerced arguments are the dangerous shape: ``pool_id`` is normalised in
    the join filter but ``user_id`` is not, so the function's behaviour depends
    on which type the caller happens to hold. Strict-xfail until both coerce.
    """
    admin = await _mk_user(db, "ga4b", role="admin")
    user = await _mk_user(db, "grantee2")
    group = await ops.create_group(db, name="grant-grp2", created_by=admin.id)
    pool = await ops.create_pool(db, name="grant-pool2", created_by=admin.id)
    await ops.add_user_to_group(db, user.id, group.id)
    await ops.set_group_pool_access(db, group.id, pool.id)
    assert await ops.check_user_pool_access(db, uuid.UUID(user.id), uuid.UUID(pool.id)) is True


async def test_check_user_pool_access_denied_without_grant(db):
    """Group membership alone is not access — the group needs a pool grant.

    Guards against a query that joins on membership but forgets the
    ``GroupPoolAccess`` predicate, which would grant every grouped user access
    to every pool.
    """
    admin = await _mk_user(db, "ga5", role="admin")
    user = await _mk_user(db, "outsider")
    pool = await ops.create_pool(db, name="locked-pool", created_by=admin.id)
    # in a group but the group has no access to this pool
    group = await ops.create_group(db, name="nopool-grp", created_by=admin.id)
    await ops.add_user_to_group(db, user.id, group.id)
    assert await ops.check_user_pool_access(db, user.id, pool.id) is False


async def test_check_user_pool_access_unknown_user(db):
    """An unknown user id denies access (fail closed, no exception)."""
    # missing user → False (uses string ids; db.get on a missing str returns None)
    assert await ops.check_user_pool_access(db, str(uuid.uuid4()), str(uuid.uuid4())) is False


# ── Nodes ──────────────────────────────────────────────────────────────────


async def test_create_node_generates_api_key(db):
    """create_node mints the per-node api_key the agent authenticates with.

    Without this key the agent cannot open its WebSocket, so a node created
    without one would register successfully yet never come online.
    """
    node = await ops.create_node(db, hostname="h1", os_type="linux", status="online")
    assert node.id is not None
    assert node.api_key
    assert node.hostname == "h1"


async def test_get_node_by_id_and_api_key(db):
    """Both node lookups handle str/UUID ids and return None for unknown keys.

    ``get_node_by_api_key`` is the agent's authentication path — the bogus-key
    case must return None so the WebSocket handshake is refused.
    """
    node = await ops.create_node(db, hostname="h2", os_type="macos")
    assert (await ops.get_node_by_id(db, node.id)).hostname == "h2"
    assert (await ops.get_node_by_id(db, uuid.UUID(node.id))).id == node.id
    assert (await ops.get_node_by_api_key(db, node.api_key)).id == node.id
    assert await ops.get_node_by_id(db, uuid.uuid4()) is None
    assert await ops.get_node_by_api_key(db, "bogus") is None


async def test_list_nodes_filters_by_os_type_and_status(db):
    """os_type and status filters work alone and compose with each other (AND).

    The scheduler relies on ``list_nodes(status="online")`` to shortlist
    candidates; if a filter were ignored, offline or wrong-OS nodes would be
    handed work they cannot run.
    """
    await ops.create_node(db, hostname="lin-on", os_type="linux", status="online")
    await ops.create_node(db, hostname="lin-off", os_type="linux", status="offline")
    await ops.create_node(db, hostname="mac-on", os_type="macos", status="online")

    linux = await ops.list_nodes(db, os_type="linux")
    assert {n.hostname for n in linux} == {"lin-on", "lin-off"}

    online = await ops.list_nodes(db, status="online")
    assert {n.hostname for n in online} == {"lin-on", "mac-on"}

    linux_online = await ops.list_nodes(db, os_type="linux", status="online")
    assert {n.hostname for n in linux_online} == {"lin-on"}


async def test_list_nodes_ordered_by_hostname(db):
    """Nodes come back hostname-sorted so the UI listing is stable."""
    await ops.create_node(db, hostname="zeta", os_type="linux")
    await ops.create_node(db, hostname="alpha", os_type="linux")
    nodes = await ops.list_nodes(db)
    hostnames = [n.hostname for n in nodes]
    assert hostnames == sorted(hostnames)


async def test_list_nodes_filter_by_pool(db, sample_node, sample_pool):
    """pool_id restricts the listing to members, accepting a str or UUID pool id.

    Asserting the empty result *before* the add proves the filter is actually
    joining on membership rather than returning all nodes.
    """
    # node not yet in the pool
    assert await ops.list_nodes(db, pool_id=sample_pool.id) == []
    await ops.add_node_to_pool(db, sample_pool.id, sample_node.id)
    in_pool = await ops.list_nodes(db, pool_id=sample_pool.id)
    assert [n.id for n in in_pool] == [sample_node.id]
    # UUID form of the pool id works too
    in_pool2 = await ops.list_nodes(db, pool_id=uuid.UUID(sample_pool.id))
    assert [n.id for n in in_pool2] == [sample_node.id]


async def test_update_node(db, sample_node):
    """update_node persists arbitrary fields; an unknown id returns None.

    Status transitions (online/offline/busy/maintenance) all flow through here
    from the WebSocket heartbeat handler, so silent no-ops would strand nodes
    in a stale state.
    """
    updated = await ops.update_node(db, sample_node.id, status="offline", cpu_cores=4)
    assert updated.status == "offline"
    assert updated.cpu_cores == 4
    assert await ops.update_node(db, uuid.uuid4(), status="x") is None


async def test_delete_node(db):
    """delete_node returns True once and False on a repeat (idempotent DELETE).

    The boolean is what the deregister route turns into 204 vs 404, so the
    second-call False is the contract that keeps double-deletes from 500-ing.
    """
    node = await ops.create_node(db, hostname="todelete", os_type="linux")
    assert await ops.delete_node(db, node.id) is True
    assert await ops.get_node_by_id(db, node.id) is None
    # deleting again returns False
    assert await ops.delete_node(db, node.id) is False


# ── Pools ──────────────────────────────────────────────────────────────────


async def test_create_get_list_pools(db, admin_user):
    """Pool create/get/list round-trips, tolerates UUID ids and sorts by name."""
    p1 = await ops.create_pool(db, name="zpool", created_by=admin_user.id)
    await ops.create_pool(db, name="apool", created_by=admin_user.id, description="a")
    assert (await ops.get_pool_by_id(db, p1.id)).name == "zpool"
    assert (await ops.get_pool_by_id(db, uuid.UUID(p1.id))).id == p1.id
    assert await ops.get_pool_by_id(db, uuid.uuid4()) is None
    names = [p.name for p in await ops.list_pools(db)]
    assert names == sorted(names)


async def test_pool_node_membership_add_remove_and_get(db, sample_node, sample_pool):
    """A node can be added to a pool, read back, and removed with UUID ids.

    ``get_pool_nodes`` (unlike ``list_nodes(pool_id=...)``) returns members in
    every status, which is what lets the scheduler prefer an online node over a
    busy one within a pool.
    """
    await ops.add_node_to_pool(db, sample_pool.id, sample_node.id)
    nodes = await ops.get_pool_nodes(db, sample_pool.id)
    assert [n.id for n in nodes] == [sample_node.id]

    await ops.remove_node_from_pool(db, uuid.UUID(sample_pool.id), uuid.UUID(sample_node.id))
    assert await ops.get_pool_nodes(db, sample_pool.id) == []


# ── Jobs ───────────────────────────────────────────────────────────────────


async def test_create_job_coerces_ids(db, regular_user, sample_pool, sample_node):
    """create_job ``_sid``-coerces every FK it is handed and defaults status.

    All four id arguments are passed as ``uuid.UUID`` on purpose — this is
    exactly the shape the submit route produces from parsed request bodies. The
    assertions compare against the stored *string* ids, so a missing coercion
    shows up as a mismatch (or a SQLite bind crash) rather than passing by
    accident.
    """
    job = await ops.create_job(
        db, name="build", submitted_by=uuid.UUID(regular_user.id),
        steps_config=[{"step": "noop"}],
        target_pool_id=uuid.UUID(sample_pool.id),
        target_node_id=uuid.UUID(sample_node.id),
        priority=5, storage_target="minio",
    )
    assert job.id is not None
    # _sid coerced the UUIDs back into the stored string ids
    assert job.submitted_by == regular_user.id
    assert job.target_pool_id == sample_pool.id
    assert job.target_node_id == sample_node.id
    assert job.priority == 5
    assert job.status == "pending"
    assert job.steps_config == [{"step": "noop"}]


async def test_get_job_by_id(db, regular_user):
    """get_job_by_id accepts str/UUID ids and returns None when absent."""
    job = await ops.create_job(db, name="j", submitted_by=regular_user.id, steps_config=[])
    assert (await ops.get_job_by_id(db, job.id)).name == "j"
    assert (await ops.get_job_by_id(db, uuid.UUID(job.id))).id == job.id
    assert await ops.get_job_by_id(db, uuid.uuid4()) is None


async def test_list_jobs_filters_status_user_pool(db, regular_user, admin_user, sample_pool):
    """status / submitted_by / pool_id each narrow the job listing independently.

    The ``submitted_by`` filter is the per-user "my jobs" view — the final
    assertion that the admin's job is absent from ``mine`` is the privacy-facing
    half of the contract, not just a listing detail.
    """
    await ops.create_job(db, name="a", submitted_by=regular_user.id, steps_config=[])
    j_pool = await ops.create_job(
        db, name="b", submitted_by=regular_user.id, steps_config=[],
        target_pool_id=sample_pool.id,
    )
    j_admin = await ops.create_job(db, name="c", submitted_by=admin_user.id, steps_config=[])
    await ops.update_job(db, j_admin.id, status="running")

    running = await ops.list_jobs(db, status="running")
    assert [j.id for j in running] == [j_admin.id]

    mine = await ops.list_jobs(db, submitted_by=regular_user.id)
    assert {j.name for j in mine} == {"a", "b"}

    by_pool = await ops.list_jobs(db, pool_id=sample_pool.id)
    assert [j.id for j in by_pool] == [j_pool.id]

    # a pool with no jobs yields nothing
    other_pool = await ops.create_pool(db, name="empty-pool", created_by=admin_user.id)
    assert await ops.list_jobs(db, pool_id=other_pool.id) == []
    # admin's job is not in regular_user's listing
    assert j_admin.id not in {j.id for j in mine}


async def test_list_jobs_submitted_by_accepts_uuid_object(db, regular_user):
    """The submitted_by filter should accept a uuid.UUID like pool_id does.

    Worse failure mode than the other ``_sid`` gaps: this one does not crash, it
    returns an empty list. A caller holding a ``UUID`` would see "you have no
    jobs" with no error anywhere. Strict-xfail until ``list_jobs`` coerces.
    """
    await ops.create_job(db, name="mine", submitted_by=regular_user.id, steps_config=[])
    found = await ops.list_jobs(db, submitted_by=uuid.UUID(regular_user.id))
    assert [j.name for j in found] == ["mine"]


async def test_list_jobs_ordered_desc_with_limit_offset(db, regular_user):
    """Jobs page newest-first with working limit/offset.

    The dashboard depends on both the DESC ordering and stable pagination;
    asserting exact names across two pages catches an off-by-one in the offset
    as well as a flipped sort direction.
    """
    created = []
    for i in range(5):
        created.append(
            await ops.create_job(db, name=f"job{i}", submitted_by=regular_user.id, steps_config=[])
        )
    # newest first
    page1 = await ops.list_jobs(db, limit=2, offset=0)
    assert [j.name for j in page1] == ["job4", "job3"]
    page2 = await ops.list_jobs(db, limit=2, offset=2)
    assert [j.name for j in page2] == ["job2", "job1"]


async def test_update_job_and_missing(db, regular_user):
    """update_job writes status/current_step; an unknown id returns None.

    ``current_step`` is the runner's resume checkpoint, so a lost write would
    make a restarted job replay from the wrong index.
    """
    job = await ops.create_job(db, name="upd", submitted_by=regular_user.id, steps_config=[])
    updated = await ops.update_job(db, job.id, status="completed", current_step=3)
    assert updated.status == "completed"
    assert updated.current_step == 3
    assert await ops.update_job(db, uuid.uuid4(), status="x") is None


async def test_append_job_log_accumulates(db, regular_user):
    """append_job_log concatenates (never overwrites) and no-ops on a missing job.

    The per-job terminal log is built by repeated appends from the runner as
    each step finishes. If the op replaced instead of appended, only the last
    step's output would survive. The missing-job call must stay silent because
    it can race with a job deletion.
    """
    job = await ops.create_job(db, name="log", submitted_by=regular_user.id, steps_config=[])
    assert job.log_text is None
    await ops.append_job_log(db, job.id, "line1\n")
    await ops.append_job_log(db, job.id, "line2\n")
    refreshed = await ops.get_job_by_id(db, job.id)
    assert refreshed.log_text == "line1\nline2\n"
    # appending to a missing job is a no-op (no exception)
    await ops.append_job_log(db, uuid.uuid4(), "ignored")


async def test_get_active_jobs(db, regular_user):
    """get_active_jobs returns exactly pending/queued/running, excluding terminal.

    This set defines what ``resume_active_jobs`` picks up after a server
    restart. Including a completed job would re-run finished work; omitting
    ``pending`` would strand never-started jobs forever.
    """
    j_pending = await ops.create_job(db, name="p", submitted_by=regular_user.id, steps_config=[])
    j_queued = await ops.create_job(db, name="q", submitted_by=regular_user.id, steps_config=[])
    await ops.update_job(db, j_queued.id, status="queued")
    j_running = await ops.create_job(db, name="r", submitted_by=regular_user.id, steps_config=[])
    await ops.update_job(db, j_running.id, status="running")
    j_done = await ops.create_job(db, name="d", submitted_by=regular_user.id, steps_config=[])
    await ops.update_job(db, j_done.id, status="completed")

    active_ids = {j.id for j in await ops.get_active_jobs(db)}
    assert active_ids == {j_pending.id, j_queued.id, j_running.id}
    assert j_done.id not in active_ids


# ── Step runs ──────────────────────────────────────────────────────────────


async def test_create_and_update_step_run(db, regular_user):
    """Step runs start 'pending' and round-trip their JSON input/output params.

    ``input_params`` / ``output_params`` are JSON columns; asserting dict
    equality after the round trip catches a serialisation regression that would
    otherwise only show up as a broken job-detail page.
    """
    job = await ops.create_job(db, name="sj", submitted_by=regular_user.id, steps_config=[])
    sr = await ops.create_step_run(
        db, job_id=job.id, step_index=0, step_name="compile", input_params={"x": 1},
    )
    assert sr.status == "pending"
    assert sr.input_params == {"x": 1}

    updated = await ops.update_step_run(
        db, sr.id, status="completed", output_params={"y": 2},
    )
    assert updated.status == "completed"
    assert updated.output_params == {"y": 2}
    assert await ops.update_step_run(db, uuid.uuid4(), status="x") is None


async def test_get_step_runs_for_job_ordered_by_index(db, regular_user):
    """Step runs come back ordered by step_index regardless of insertion order.

    Deliberately inserted 2/0/1 so a missing ``order_by`` would surface. The job
    detail UI renders this sequence as the execution timeline.
    """
    job = await ops.create_job(db, name="oj", submitted_by=regular_user.id, steps_config=[])
    # insert out of order
    await ops.create_step_run(db, job_id=job.id, step_index=2, step_name="c")
    await ops.create_step_run(db, job_id=job.id, step_index=0, step_name="a")
    await ops.create_step_run(db, job_id=job.id, step_index=1, step_name="b")

    runs = await ops.get_step_runs_for_job(db, job.id)
    assert [r.step_index for r in runs] == [0, 1, 2]
    assert [r.step_name for r in runs] == ["a", "b", "c"]
    # UUID job id works as well
    runs2 = await ops.get_step_runs_for_job(db, uuid.UUID(job.id))
    assert len(runs2) == 3


async def test_get_latest_step_run_returns_a_run_at_index(db, regular_user):
    """get_latest_step_run returns *some* run at the given index, or None when
    there is none. (See xfail below re: which one it picks.)

    Scoped deliberately to the single-run case, where "latest" is unambiguous,
    so this test stays green while the ordering bug below is still open.
    """
    job = await ops.create_job(db, name="lj0", submitted_by=regular_user.id, steps_config=[])
    sr = await ops.create_step_run(db, job_id=job.id, step_index=1, step_name="loop")
    latest = await ops.get_latest_step_run(db, job.id, 1)
    assert latest is not None and latest.id == sr.id
    # UUID job id works (job filter is _sid-coerced)
    assert (await ops.get_latest_step_run(db, uuid.UUID(job.id), 1)).id == sr.id
    # no step run at this index
    assert await ops.get_latest_step_run(db, job.id, 99) is None


@pytest.mark.xfail(
    reason="SOURCE BUG: get_latest_step_run() orders by StepRun.id.desc(), but id "
    "is a random uuid4 string, not a monotonic/creation-ordered value. The "
    "docstring promises the 'most recently created' run (for loop steps that "
    "reuse a step_index), but uuid-desc ordering does not honor insertion order. "
    "Asserting the contract across many sequentially-created runs makes the failure "
    "deterministic (id-desc ordering essentially never matches creation order for "
    "all of them).",
    strict=True,
)
async def test_get_latest_step_run_picks_newest_at_index(db, regular_user):
    """The 'latest' run at a reused step_index must be the newest one.

    Matters because loop/jump steps re-execute the same ``step_index``: the WS
    agent handler resolves incoming results against "the latest run at index N",
    so picking the wrong row attributes an agent's output to a previous
    iteration.

    The loop below is a determinism trick, not flakiness: it keeps inserting
    runs until the most-recently-created one is provably NOT the
    lexicographically-largest uuid4, at which point the buggy ``id DESC``
    ordering is guaranteed to return the wrong row. Terminates almost surely on
    the first or second insert (P(newest is also max id) halves each time).
    """
    job = await ops.create_job(db, name="lj", submitted_by=regular_user.id, steps_config=[])
    # Create runs at the same step_index (as a loop step would) until the
    # last-created run is NOT the lexicographically-largest id. Under the buggy
    # id-desc ordering the function returns the max-id run, so once that run is
    # not the newest, the "most recently created" contract is deterministically
    # violated.
    created = []
    while True:
        sr = await ops.create_step_run(db, job_id=job.id, step_index=1, step_name="loop")
        created.append(sr)
        max_id_run = max(created, key=lambda r: r.id)
        if max_id_run.id != created[-1].id:
            break
    last = created[-1]
    latest = await ops.get_latest_step_run(db, job.id, 1)
    assert latest is not None
    # Contract: the most recently created run should win. (Fails: id-desc
    # ordering returns max_id_run, which we ensured is not `last`.)
    assert latest.id == last.id


# ── Credentials ────────────────────────────────────────────────────────────


async def test_credential_crud(db, regular_user):
    """Full credential lifecycle: create, read by id/name, update, delete.

    Pins the security-relevant defaults (``allowed_groups=[]`` and
    ``is_shared=False``) — a credential must start private, so a schema change
    that flipped either default would silently expose every new secret to other
    users. Also pins that ``update_credential`` stamps ``updated_at`` and that
    delete is idempotent (True then False), which the route maps to 204/404.
    """
    cred = await ops.create_credential(
        db, name="ssh-key", credential_type="ssh", encrypted_fields=b"secret",
        owner_id=regular_user.id, description="my key",
    )
    assert cred.id is not None
    assert cred.allowed_groups == []
    assert cred.is_shared is False

    assert (await ops.get_credential_by_id(db, cred.id)).name == "ssh-key"
    assert (await ops.get_credential_by_id(db, uuid.UUID(cred.id))).id == cred.id
    assert (await ops.get_credential_by_name(db, "ssh-key")).id == cred.id
    assert await ops.get_credential_by_name(db, "missing") is None

    creds = await ops.list_credentials(db)
    assert any(c.name == "ssh-key" for c in creds)

    updated = await ops.update_credential(db, cred.id, description="rotated", is_shared=True)
    assert updated.description == "rotated"
    assert updated.is_shared is True
    assert updated.updated_at is not None  # set by op
    assert await ops.update_credential(db, str(uuid.uuid4()), description="x") is None

    assert await ops.delete_credential(db, cred.id) is True
    assert await ops.get_credential_by_id(db, cred.id) is None
    assert await ops.delete_credential(db, cred.id) is False


async def test_update_credential_accepts_uuid_object(db, regular_user):
    """Credential write ops should accept UUID ids like the read op already does.

    The credentials route declares ``cred_id: UUID`` and passes it straight
    through, so this gap 500s the real DELETE endpoint (see the matching
    xfails in tests/integration/test_credentials_routes.py).
    """
    cred = await ops.create_credential(
        db, name="uuid-cred", credential_type="ssh", encrypted_fields=b"s",
        owner_id=regular_user.id,
    )
    updated = await ops.update_credential(db, uuid.UUID(cred.id), description="x")
    assert updated is not None and updated.description == "x"


# ── Storage backends ───────────────────────────────────────────────────────


async def _mk_backend(db, regular_user, name, **kw):
    """Persist a storage backend plus the credential it must reference.

    Args:
        db: Test session.
        regular_user: Owner of the generated credential.
        name: Backend name (also used to make the credential name unique).
        **kw: Extra ``create_storage_backend`` kwargs — ``is_default``,
            ``is_active``, ``priority``.

    Returns:
        The persisted ``StorageBackend``.

    Side effects:
        INSERTs two rows (one credential, one backend). The credential's
        encrypted blob is a dummy — nothing in these tests decrypts it.
    """
    cred = await ops.create_credential(
        db, name=f"cred-{name}", credential_type="s3", encrypted_fields=b"x",
        owner_id=regular_user.id,
    )
    return await ops.create_storage_backend(
        db, name=name, backend_type="minio", credential_id=cred.id, **kw,
    )


async def test_storage_backend_create_and_get(db, regular_user):
    """Backend lookup by id accepts str/UUID and returns None when unknown."""
    b = await _mk_backend(db, regular_user, "primary", is_default=True)
    assert (await ops.get_storage_backend_by_id(db, b.id)).name == "primary"
    assert (await ops.get_storage_backend_by_id(db, uuid.UUID(b.id))).id == b.id
    assert await ops.get_storage_backend_by_id(db, uuid.uuid4()) is None


async def test_get_default_storage_backend_requires_default_and_active(db, regular_user):
    """The default backend must satisfy BOTH is_default AND is_active.

    Each flag is exercised in isolation first so a query that dropped either
    predicate would fail: a default-but-disabled backend would otherwise be
    handed out for artifact uploads and every upload would fail at connect
    time.
    """
    # default but inactive — should NOT be returned
    await _mk_backend(db, regular_user, "inactive-default", is_default=True, is_active=False)
    assert await ops.get_default_storage_backend(db) is None

    # active but NOT marked default — also must NOT be returned
    await _mk_backend(db, regular_user, "active-nondefault", is_default=False, is_active=True)
    assert await ops.get_default_storage_backend(db) is None

    # the real default: both flags true
    good = await _mk_backend(db, regular_user, "live-default", is_default=True, is_active=True)
    found = await ops.get_default_storage_backend(db)
    assert found is not None and found.id == good.id


async def test_list_storage_backends_ordered_by_priority_then_name(db, regular_user):
    """Backends sort by priority ascending, then name — the failover order.

    Two backends share priority 5 so the secondary name sort is actually
    exercised; the priority-1 entry proves the primary key of the sort wins.
    """
    await _mk_backend(db, regular_user, "bravo", priority=5)
    await _mk_backend(db, regular_user, "alpha", priority=5)
    await _mk_backend(db, regular_user, "first", priority=1)
    listed = await ops.list_storage_backends(db)
    pairs = [(b.priority, b.name) for b in listed]
    assert pairs == sorted(pairs)
    # priority-1 backend comes before any priority-5
    assert listed[0].name == "first"


# ── Artifacts ──────────────────────────────────────────────────────────────


async def test_artifact_create_list_get(db, regular_user):
    """Artifacts are fetchable by id and listed per job in creation order.

    The oldest-first ordering is deliberate (unlike jobs, which are newest
    first) so the artifact list mirrors the order the job produced them. An
    unknown job id yields an empty list rather than an error, which is what
    lets the job-detail page render before any artifact exists.
    """
    job = await ops.create_job(db, name="aj", submitted_by=regular_user.id, steps_config=[])
    backend = await _mk_backend(db, regular_user, "art-store")

    a1 = await ops.create_artifact(
        db, job_id=job.id, filename="out.bin", storage_backend_id=backend.id,
        storage_key="k1", size_bytes=100,
    )
    a2 = await ops.create_artifact(
        db, job_id=job.id, filename="log.txt", storage_backend_id=backend.id,
        storage_key="k2", size_bytes=200,
    )

    assert (await ops.get_artifact_by_id(db, a1.id)).filename == "out.bin"
    assert (await ops.get_artifact_by_id(db, uuid.UUID(a2.id))).id == a2.id
    assert await ops.get_artifact_by_id(db, uuid.uuid4()) is None

    listed = await ops.list_artifacts_for_job(db, job.id)
    assert {a.id for a in listed} == {a1.id, a2.id}
    # ordered by created_at ascending — a1 created first
    assert listed[0].id == a1.id
    # empty for an unknown job
    assert await ops.list_artifacts_for_job(db, uuid.uuid4()) == []


# ── Storage transfers ──────────────────────────────────────────────────────


async def test_transfer_create_update_and_list_ordering(db, regular_user):
    """Transfers default to 'pending', update in place, and sort started-first.

    The ordering assertion is the interesting one: ``list_transfers`` sorts by
    ``started_at`` DESC with NULLs last, so a never-started (pending) transfer
    must sink below one that has begun. Without the explicit NULLS LAST the
    pending rows would float to the top on some backends and the UI's "recent
    activity" list would be wrong.
    """
    job = await ops.create_job(db, name="tj", submitted_by=regular_user.id, steps_config=[])
    src = await _mk_backend(db, regular_user, "src")
    dst = await _mk_backend(db, regular_user, "dst")
    artifact = await ops.create_artifact(
        db, job_id=job.id, filename="f", storage_backend_id=src.id, storage_key="sk",
    )

    t_pending = await ops.create_transfer(
        db, artifact_id=artifact.id, source_backend_id=src.id, dest_backend_id=dst.id,
    )
    assert t_pending.status == "pending"

    # one transfer with an explicit started_at so ordering is deterministic
    # AI Note: a hard-coded aware datetime (not utcnow()) keeps the comparison
    # against the NULL-started row independent of wall-clock timing.
    from datetime import datetime, timezone

    t_started = await ops.create_transfer(
        db, artifact_id=artifact.id, source_backend_id=src.id, dest_backend_id=dst.id,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    updated = await ops.update_transfer(db, t_started.id, status="completed", bytes_transferred=42)
    assert updated.status == "completed"
    assert updated.bytes_transferred == 42
    assert await ops.update_transfer(db, uuid.uuid4(), status="x") is None

    # filter by status
    completed = await ops.list_transfers(db, status="completed")
    assert [t.id for t in completed] == [t_started.id]

    # ordering: started_at desc, nulls last → the started one precedes the null one
    all_transfers = await ops.list_transfers(db)
    ids = [t.id for t in all_transfers]
    assert ids.index(t_started.id) < ids.index(t_pending.id)


# ── Saved templates ────────────────────────────────────────────────────────


async def test_template_create_list_delete(db, admin_user):
    """Templates list name-sorted and delete idempotently (True, then False).

    Saved templates are reusable ``steps_config`` blobs for the job builder.
    The repeat-delete returning False (rather than raising) is what keeps a
    double-click on the UI delete button from 500-ing.
    """
    t = await ops.create_template(
        db, name="ztpl", steps_config=[{"s": 1}], created_by=admin_user.id,
        description="d",
    )
    await ops.create_template(db, name="atpl", steps_config=[], created_by=admin_user.id)

    names = [x.name for x in await ops.list_templates(db)]
    assert names == sorted(names)

    assert await ops.delete_template(db, t.id) is True
    remaining = [x.name for x in await ops.list_templates(db)]
    assert "ztpl" not in remaining
    assert await ops.delete_template(db, t.id) is False
    assert await ops.delete_template(db, uuid.uuid4()) is False


# ── Audit log ──────────────────────────────────────────────────────────────


async def test_create_audit_entry(db, regular_user):
    """Audit entries persist their JSON details and allow a null actor.

    The system/anonymous case (``action`` only, no ``user_id``) must work —
    boot and other unattributed events are recorded through the same op, and a
    NOT NULL constraint on ``user_id`` would make them silently unloggable.
    ``timestamp`` is server-assigned so entries cannot be backdated by a caller.
    """
    entry = await ops.create_audit_entry(
        db, action="login", user_id=regular_user.id,
        target_type="user", target_id=regular_user.id,
        details={"ip": "1.2.3.4"},
    )
    assert entry.id is not None
    assert entry.action == "login"
    assert entry.details == {"ip": "1.2.3.4"}
    assert entry.timestamp is not None

    # minimal entry: only an action, no user
    minimal = await ops.create_audit_entry(db, action="system.boot")
    assert minimal.user_id is None
    assert minimal.action == "system.boot"
