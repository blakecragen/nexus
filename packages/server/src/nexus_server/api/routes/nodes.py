"""Node management routes — list, detail, register, provision, deregister, maintenance."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from nexus_common.models.schemas import NodeInfo, NodeRegistration
from nexus_server.api.deps import AdminUser, CurrentUser, DbSession
from nexus_server.db import ops
from nexus_server.services import provisioner

router = APIRouter()


class NodeProvision(BaseModel):
    """Register a node AND set it up on the device over SSH."""
    ssh_host: str
    ssh_user: str
    ssh_password: str | None = None
    use_server_key: bool = False
    display_name: str | None = None
    tags: list[str] = []
    service: bool = False
    install_python: bool = True
    ws_host: str | None = None          # override the auto-detected callback address
    ws_port: int = 8000
    branch: str = "main"
    repo_url: str = provisioner.GITHUB_URL_DEFAULT


class NodeReconnect(BaseModel):
    """Bring an existing (offline) node back online by re-running setup over SSH,
    reusing the node's existing identity (UUID + api_key). Re-picks a reachable
    callback address, so it survives the server's IP changing. SSH creds aren't
    stored, so they must be supplied again."""
    ssh_user: str
    ssh_password: str | None = None
    use_server_key: bool = False
    ssh_host: str | None = None   # defaults to the node's last-known IP
    ws_host: str | None = None
    ws_port: int = 8000
    branch: str = "main"
    repo_url: str = provisioner.GITHUB_URL_DEFAULT
    install_python: bool = True
    service: bool = False


def _node_to_info(node) -> NodeInfo:
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
        ip_address=body.ip_address,
        tags=body.tags,
    )
    return {
        "node": _node_to_info(node),
        "api_key": node.api_key,
    }


async def _provision_and_poll(
    *, node_id: str, api_key: str, body, ssh_host: str,
) -> dict:
    """Run the SSH setup for a node (new or existing) and poll until the agent
    reports online. Returns {result, online, fresh_node}. Shared by provision +
    reconnect."""
    server_ips = [body.ws_host] if body.ws_host else provisioner.callback_candidates()
    result = await asyncio.to_thread(
        provisioner.provision,
        host=ssh_host, user=body.ssh_user, password=body.ssh_password,
        use_server_key=body.use_server_key, node_id=node_id, api_key=api_key,
        server_ips=server_ips, ws_port=body.ws_port, repo_url=body.repo_url,
        branch=body.branch, service=body.service, install_python=body.install_python,
    )
    if not result.get("ok"):
        return {"result": result, "online": False, "fresh": None}

    # Poll with FRESH sessions — the WS handler updates status in a different
    # session, so reusing the request's db would never see "online".
    from nexus_server.db.session import get_session_factory
    session_factory = get_session_factory()
    online = False
    fresh = None
    for _ in range(10):
        await asyncio.sleep(2)
        async with session_factory() as poll_db:
            fresh = await ops.get_node_by_id(poll_db, UUID(node_id))
        if fresh and fresh.status == "online":
            online = True
            break
    return {"result": result, "online": online, "fresh": fresh}


def _not_online_note(result: dict) -> str:
    return (
        "Installed and started, but the agent has NOT connected back yet. The "
        f"WebSocket to {result.get('ws_host')} isn't completing — usually a "
        "VPN/firewall or asymmetric routing between server and device. The agent "
        "keeps retrying; it'll appear online once the path clears, or retry with a "
        "reachable ws_host."
    )


@router.post("/provision", status_code=status.HTTP_201_CREATED)
async def provision_node(body: NodeProvision, db: DbSession, admin: AdminUser):
    """Register a node AND set it up on the device over SSH (admin only).

    SSHes to the device, clones the agent from GitHub, installs it, and starts it
    (background by default, or an auto-start service). If provisioning fails the
    node is deregistered so no orphan is left. Can take a few minutes when Python
    must be installed on the device.
    """
    if not body.use_server_key and not body.ssh_password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide ssh_password, or set use_server_key to use the server's SSH keys.",
        )

    name = body.display_name or body.ssh_host
    # Register (mint UUID + api_key). Hardware fields are placeholders — the
    # agent reports real specs once it connects.
    node = await ops.create_node(
        db, hostname=body.ssh_host, display_name=name, os_type="linux",
        os_version="unknown", arch="unknown", cpu_model="pending", cpu_cores=1,
        ram_mb=1024, gpu_info=None, agent_version="0.1.0", ip_address="0.0.0.0",
        tags=body.tags,
    )

    outcome = await _provision_and_poll(
        node_id=str(node.id), api_key=node.api_key, body=body, ssh_host=body.ssh_host,
    )
    result = outcome["result"]
    if not result.get("ok"):
        await ops.delete_node(db, node.id)  # leave no orphan
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": result.get("error", "provisioning failed"), "log": result.get("log", [])},
        )

    log = result.get("log", [])
    if not outcome["online"]:
        log = log + [_not_online_note(result)]

    return {
        "node": _node_to_info(outcome["fresh"] or node),
        "api_key": node.api_key,
        "ws_url": result.get("ws_url"),
        "mode": result.get("mode"),
        "online": outcome["online"],
        "log": log,
    }


@router.post("/{node_id}/reconnect")
async def reconnect_node(node_id: UUID, body: NodeReconnect, db: DbSession, admin: AdminUser):
    """Bring an existing offline node back online (admin only).

    Reuses the node's identity (UUID + api_key) and re-runs setup over SSH so the
    agent re-picks a reachable callback address (the server's IP can change). The
    node is NOT deleted if this fails — it just stays offline.
    """
    node = await ops.get_node_by_id(db, node_id)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    if not body.use_server_key and not body.ssh_password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide ssh_password, or set use_server_key to use the server's SSH keys.",
        )

    # Prefer an explicit ssh_host; else the node's last-known IP (if real).
    ssh_host = body.ssh_host or (node.ip_address if node.ip_address not in (None, "", "0.0.0.0") else None)
    if not ssh_host:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No SSH host known for this node — provide ssh_host.",
        )

    outcome = await _provision_and_poll(
        node_id=str(node.id), api_key=node.api_key, body=body, ssh_host=ssh_host,
    )
    result = outcome["result"]
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": result.get("error", "reconnect failed"), "log": result.get("log", [])},
        )

    log = result.get("log", [])
    if not outcome["online"]:
        log = log + [_not_online_note(result)]

    return {
        "node": _node_to_info(outcome["fresh"] or node),
        "ws_url": result.get("ws_url"),
        "mode": result.get("mode"),
        "online": outcome["online"],
        "log": log,
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
