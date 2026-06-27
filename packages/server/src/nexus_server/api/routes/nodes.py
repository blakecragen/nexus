"""Node management routes — list, detail, register, deregister, maintenance."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from nexus_common.models.schemas import NodeInfo, NodeRegistration
from nexus_server.api.deps import AdminUser, CurrentUser, DbSession
from nexus_server.db import ops

router = APIRouter()


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


@router.get("", response_model=list[NodeInfo])
async def list_nodes(
    db: DbSession,
    user: CurrentUser,
    os_type: str | None = None,
    node_status: str | None = None,
    pool_id: UUID | None = None,
):
    """List all nodes, optionally filtered by os_type, status, or pool membership."""
    nodes = await ops.list_nodes(db, os_type=os_type, status=node_status, pool_id=pool_id)
    return [_node_to_info(n) for n in nodes]


@router.get("/{node_id}", response_model=NodeInfo)
async def get_node(node_id: UUID, db: DbSession, user: CurrentUser):
    """Get detailed information about a single node."""
    node = await ops.get_node_by_id(db, node_id)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return _node_to_info(node)


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_node(body: NodeRegistration, db: DbSession, admin: AdminUser):
    """Register a new node (admin only). Returns the node info with its API key."""
    node = await ops.create_node(
        db,
        hostname=body.hostname, display_name=body.display_name,
        os_type=body.os_type.value, os_version=body.os_version, arch=body.arch,
        cpu_model=body.cpu_model, cpu_cores=body.cpu_cores, ram_mb=body.ram_mb,
        gpu_info=body.gpu_info, agent_version=body.agent_version,
        ip_address=body.ip_address, capabilities=body.capabilities,
        tags=body.tags,
    )
    return {
        "node": _node_to_info(node),
        "api_key": node.api_key,
    }


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deregister_node(node_id: UUID, db: DbSession, admin: AdminUser):
    """Deregister a node (admin only)."""
    deleted = await ops.delete_node(db, node_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")


@router.put("/{node_id}/maintenance")
async def toggle_maintenance(node_id: UUID, enable: bool, db: DbSession, admin: AdminUser):
    """Toggle maintenance mode on a node (admin only)."""
    new_status = "maintenance" if enable else "offline"
    node = await ops.update_node(db, node_id, status=new_status)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return _node_to_info(node)
