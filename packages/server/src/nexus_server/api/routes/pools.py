"""Pool management routes — CRUD, node membership.

Role in the system
------------------
Mounted at ``/api/pools`` by ``nexus_server.main.create_app``. A *pool* is a
named group of nodes. Pools serve two purposes:

1. **Scheduling** — a job or an individual step can carry a
   ``target_pool_id``; ``runner/scheduler.py`` then restricts placement to
   members of that pool (see ``ops.get_pool_nodes``).
2. **Authorization** — group-to-pool grants (``GroupPoolAccess``) are the
   basis for ``ops.check_user_pool_access`` and
   ``deps.require_pool_access``.

Neighbouring modules
--------------------
- ``nexus_server.db.ops`` for all persistence, including the
  ``PoolNodeMembership`` join table.
- ``nexus_server.api.routes.nodes`` owns node identity; this module only owns
  the membership edges.
- Frontend ``frontend/src/pages/Pools.tsx`` is the primary consumer.

AI Note: the permission model here is looser than elsewhere in the API. Only
``DELETE /{pool_id}`` requires admin; create, update and membership changes
accept any authenticated user (``CurrentUser``), and none of them consult
``check_user_pool_access``. Any logged-in user can therefore move nodes between
pools, which indirectly affects which pools other users can schedule onto.
Treat that as a known gap, not as an oversight to silently "fix" — several
routes and the UI assume this behaviour today.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from nexus_common.models.schemas import NodeInfo, PoolCreate, PoolInfo
from nexus_server.api.deps import AdminUser, CurrentUser, DbSession
from nexus_server.db import ops

router = APIRouter()


def _pool_to_info(pool, node_count: int = 0) -> PoolInfo:
    """Project a :class:`~nexus_server.db.models.Pool` ORM row to the public API shape.

    Args:
        pool: A ``Pool`` ORM instance.
        node_count: Number of member nodes. Passed in rather than derived
            because membership lives in a separate table and the caller has
            usually already loaded the node list.

    Returns:
        PoolInfo: The serializable view of the pool.

    AI Note: ``node_count`` defaults to 0, so a caller that forgets to pass it
    reports an empty pool rather than raising — see :func:`create_pool`, where
    0 is correct because a brand-new pool genuinely has no members.
    """
    return PoolInfo(
        id=pool.id, name=pool.name, description=pool.description,
        node_count=node_count, created_at=pool.created_at,
    )


def _node_to_info(node) -> NodeInfo:
    """Project a :class:`~nexus_server.db.models.Node` ORM row to the public API shape.

    Coalesces nullable hardware columns so the response always satisfies
    ``NodeInfo``'s non-optional fields.

    Args:
        node: A ``Node`` ORM instance.

    Returns:
        NodeInfo: The serializable view of the node, without ``api_key``.

    AI Note: byte-for-byte duplicate of ``routes/nodes.py::_node_to_info``.
    Keep them in sync — a field added to ``NodeInfo`` will make pool detail
    responses fail Pydantic validation if only the nodes copy is updated.
    """
    return NodeInfo(
        id=node.id, hostname=node.hostname, display_name=node.display_name,
        os_type=node.os_type, os_version=node.os_version or "", arch=node.arch or "",
        cpu_model=node.cpu_model or "", cpu_cores=node.cpu_cores or 0,
        ram_mb=node.ram_mb or 0, gpu_info=node.gpu_info,
        agent_version=node.agent_version or "", ip_address=node.ip_address or "",
        status=node.status,
        tags=node.tags or [], last_heartbeat=node.last_heartbeat,
        registered_at=node.registered_at,
    )


@router.get("", response_model=list[PoolInfo])
async def list_pools(db: DbSession, user: CurrentUser):
    """List all pools with node counts.

    Args:
        db: Request-scoped DB session.
        user: Any authenticated user. No pool ACL filtering is applied — every
            user sees every pool.

    Returns:
        list[PoolInfo]: All pools, each annotated with its member count.
    """
    pools = await ops.list_pools(db)
    result = []
    # AI Note: N+1 query — one get_pool_nodes round-trip per pool, purely to
    # compute node_count. Fine at cluster scale (tens of pools); would need a
    # grouped COUNT if pool counts ever grow large.
    for pool in pools:
        nodes = await ops.get_pool_nodes(db, pool.id)
        result.append(_pool_to_info(pool, node_count=len(nodes)))
    return result


@router.post("", response_model=PoolInfo, status_code=status.HTTP_201_CREATED)
async def create_pool(body: PoolCreate, db: DbSession, user: CurrentUser):
    """Create a new pool.

    Args:
        body: Name and optional description.
        db: Request-scoped DB session (a row is committed).
        user: Any authenticated user; their ID is recorded as ``created_by``.

    Returns:
        PoolInfo: The new pool, always with ``node_count=0``.
    """
    # AI Note: pool names are not enforced unique at this layer. Whether a
    # duplicate name is rejected depends entirely on the DB constraint in
    # db/models.py; if that constraint is absent, two same-named pools can
    # coexist and only their UUIDs distinguish them in the UI.
    pool = await ops.create_pool(db, name=body.name, created_by=user.id, description=body.description)
    return _pool_to_info(pool)


@router.get("/{pool_id}")
async def get_pool(pool_id: UUID, db: DbSession, user: CurrentUser):
    """Get pool detail including member nodes.

    Args:
        pool_id: Pool UUID from the path.
        db: Request-scoped DB session.
        user: Any authenticated user.

    Returns:
        dict: ``{"pool": PoolInfo, "nodes": list[NodeInfo]}``.

    Raises:
        HTTPException: 404 if the pool does not exist.
    """
    # AI Note: this route has no response_model, so the returned shape is
    # whatever this dict contains — FastAPI will not validate or filter it. The
    # frontend's PoolDetail type mirrors it by hand, so changing a key here is
    # a silent breaking change for the UI.
    pool = await ops.get_pool_by_id(db, pool_id)
    if not pool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pool not found")
    nodes = await ops.get_pool_nodes(db, pool_id)
    return {
        "pool": _pool_to_info(pool, node_count=len(nodes)),
        "nodes": [_node_to_info(n) for n in nodes],
    }


@router.put("/{pool_id}", response_model=PoolInfo)
async def update_pool(pool_id: UUID, body: PoolCreate, db: DbSession, user: CurrentUser):
    """Update pool name/description.

    Args:
        pool_id: Pool UUID from the path.
        body: New name and (optionally) description. Reuses ``PoolCreate``, so
            ``name`` is always required even for a description-only edit.
        db: Request-scoped DB session (row is mutated and committed).
        user: Any authenticated user.

    Returns:
        PoolInfo: The updated pool with a refreshed member count.

    Raises:
        HTTPException: 404 if the pool does not exist.

    Note:
        A description can be cleared by sending ``""``; sending ``null`` leaves
        the existing description unchanged.
    """
    # AI Note: asymmetric null handling — `name` is always overwritten, but
    # `description` is only overwritten when non-None.
    pool = await ops.get_pool_by_id(db, pool_id)
    if not pool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pool not found")
    # Update fields directly
    # AI Note: mutates the ORM object rather than going through ops.*, so this
    # is the one pool write that bypasses the ops layer. Any invariant added to
    # ops (validation, audit logging) will not apply here.
    pool.name = body.name
    if body.description is not None:
        pool.description = body.description
    await db.commit()
    await db.refresh(pool)
    nodes = await ops.get_pool_nodes(db, pool_id)
    return _pool_to_info(pool, node_count=len(nodes))


@router.delete("/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pool(pool_id: UUID, db: DbSession, admin: AdminUser):
    """Delete a pool (admin only).

    Args:
        pool_id: Pool UUID from the path.
        db: Request-scoped DB session (row is deleted and committed).
        admin: Enforces admin role — the only pool route that does.

    Raises:
        HTTPException: 404 if the pool does not exist.

    Note:
        Member nodes are not deleted — only the pool and its memberships.
    """
    # AI Note: jobs that recorded this target_pool_id keep a dangling
    # reference; the scheduler then finds no candidate nodes and the step fails
    # with a placement error rather than a foreign-key error.
    pool = await ops.get_pool_by_id(db, pool_id)
    if not pool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pool not found")
    await db.delete(pool)
    await db.commit()


@router.post("/{pool_id}/nodes", status_code=status.HTTP_201_CREATED)
async def add_nodes_to_pool(pool_id: UUID, node_ids: list[UUID], db: DbSession, user: CurrentUser):
    """Add one or more nodes to a pool.

    Args:
        pool_id: Pool UUID from the path.
        node_ids: JSON array of node UUIDs in the request body.
        db: Request-scoped DB session (one membership row committed per node).
        user: Any authenticated user.

    Returns:
        dict: ``{"added": list[str]}`` — the node IDs that were linked, as
        strings.

    Raises:
        HTTPException: 404 if the pool, or any one of the nodes, does not
            exist.

    Note:
        Not atomic. Nodes are linked one at a time, so a 404 for a later node
        leaves the earlier ones linked. Retrying the same request is safe.
    """
    # AI Note: each node is committed by ops.add_node_to_pool as it is
    # processed, which is why a mid-list 404 leaves partial state behind.
    #
    # AI Note: no duplicate check — adding a node already in the pool inserts a
    # second PoolNodeMembership row unless the DB has a unique constraint,
    # which would inflate node_count and return the node twice from
    # get_pool_nodes.
    pool = await ops.get_pool_by_id(db, pool_id)
    if not pool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pool not found")
    added = []
    for nid in node_ids:
        node = await ops.get_node_by_id(db, nid)
        if not node:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Node {nid} not found",
            )
        await ops.add_node_to_pool(db, pool_id, nid)
        added.append(str(nid))
    return {"added": added}


@router.delete("/{pool_id}/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_node_from_pool(pool_id: UUID, node_id: UUID, db: DbSession, user: CurrentUser):
    """Remove a node from a pool.

    Deletes the ``PoolNodeMembership`` edge only; neither the pool nor the node
    is affected.

    Args:
        pool_id: Pool UUID from the path.
        node_id: Node UUID from the path.
        db: Request-scoped DB session (membership row deleted and committed).
        user: Any authenticated user.

    Note:
        Idempotent — removing a node that is not a member still returns 204.
    """
    # AI Note: the underlying DELETE matches zero rows when the pool, node, or
    # membership does not exist, and the route still returns 204. Callers
    # cannot distinguish "removed" from "was never a member", which is
    # deliberate so repeated UI clicks are harmless.
    await ops.remove_node_from_pool(db, pool_id, node_id)
