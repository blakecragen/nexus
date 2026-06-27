"""Pool management routes — CRUD, node membership."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from nexus_common.models.schemas import NodeInfo, PoolCreate, PoolInfo
from nexus_server.api.deps import AdminUser, CurrentUser, DbSession
from nexus_server.db import ops

router = APIRouter()


def _pool_to_info(pool, node_count: int = 0) -> PoolInfo:
    return PoolInfo(
        id=pool.id, name=pool.name, description=pool.description,
        node_count=node_count, created_at=pool.created_at,
    )


def _node_to_info(node) -> NodeInfo:
    return NodeInfo(
        id=node.id, hostname=node.hostname, display_name=node.display_name,
        os_type=node.os_type, os_version=node.os_version or "", arch=node.arch or "",
        cpu_model=node.cpu_model or "", cpu_cores=node.cpu_cores or 0,
        ram_mb=node.ram_mb or 0, gpu_info=node.gpu_info,
        agent_version=node.agent_version or "", ip_address=node.ip_address or "",
        status=node.status, capabilities=node.capabilities or {},
        tags=node.tags or [], last_heartbeat=node.last_heartbeat,
        registered_at=node.registered_at,
    )


@router.get("", response_model=list[PoolInfo])
async def list_pools(db: DbSession, user: CurrentUser):
    """List all pools with node counts."""
    pools = await ops.list_pools(db)
    result = []
    for pool in pools:
        nodes = await ops.get_pool_nodes(db, pool.id)
        result.append(_pool_to_info(pool, node_count=len(nodes)))
    return result


@router.post("", response_model=PoolInfo, status_code=status.HTTP_201_CREATED)
async def create_pool(body: PoolCreate, db: DbSession, user: CurrentUser):
    """Create a new pool."""
    pool = await ops.create_pool(db, name=body.name, created_by=user.id, description=body.description)
    return _pool_to_info(pool)


@router.get("/{pool_id}")
async def get_pool(pool_id: UUID, db: DbSession, user: CurrentUser):
    """Get pool detail including member nodes."""
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
    """Update pool name/description."""
    pool = await ops.get_pool_by_id(db, pool_id)
    if not pool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pool not found")
    # Update fields directly
    pool.name = body.name
    if body.description is not None:
        pool.description = body.description
    await db.commit()
    await db.refresh(pool)
    nodes = await ops.get_pool_nodes(db, pool_id)
    return _pool_to_info(pool, node_count=len(nodes))


@router.delete("/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pool(pool_id: UUID, db: DbSession, admin: AdminUser):
    """Delete a pool (admin only)."""
    pool = await ops.get_pool_by_id(db, pool_id)
    if not pool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pool not found")
    await db.delete(pool)
    await db.commit()


@router.post("/{pool_id}/nodes", status_code=status.HTTP_201_CREATED)
async def add_nodes_to_pool(pool_id: UUID, node_ids: list[UUID], db: DbSession, user: CurrentUser):
    """Add one or more nodes to a pool."""
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
    """Remove a node from a pool."""
    await ops.remove_node_from_pool(db, pool_id, node_id)
