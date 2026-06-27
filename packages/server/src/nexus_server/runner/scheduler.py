"""Job scheduler — assigns jobs to appropriate nodes."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_common.steps.registry import get_step
from nexus_server.db import ops
from nexus_server.db.models import Node


async def find_node_for_step(
    db: AsyncSession,
    step_name: str,
    target_pool_id: UUID | None = None,
    target_node_id: UUID | None = None,
    target_os: str | None = None,
) -> Node | None:
    """Find a suitable online node for executing a step.

    Priority:
    1. If target_node_id is specified, use that node (if online and matches)
    2. If target_pool_id is specified, find a matching node in the pool
    3. Otherwise, find any matching online node

    Matching considers: node status, OS support, required capabilities, and
    an optional target_os override (per-step pin to a specific OS family).
    """
    step_cls = get_step(step_name)

    # Direct node targeting
    if target_node_id:
        node = await ops.get_node_by_id(db, target_node_id)
        if node and _node_matches_step(node, step_cls, target_os):
            return node
        return None

    # Pool-based selection
    if target_pool_id:
        nodes = await ops.get_pool_nodes(db, target_pool_id)
    else:
        nodes = await ops.list_nodes(db, status="online")

    # Filter to compatible nodes, prefer least busy
    candidates = [n for n in nodes if _node_matches_step(n, step_cls, target_os)]
    if not candidates:
        return None

    # Prefer online nodes, then sort by status (online > busy)
    online = [n for n in candidates if n.status == "online"]
    return online[0] if online else candidates[0]


def _node_matches_step(node: Node, step_cls: type, target_os: str | None = None) -> bool:
    """Check if a node can run a given step type."""
    if node.status not in ("online", "busy"):
        return False

    # Per-step OS pin (e.g. "this gem5 sim must run on linux")
    if target_os and node.os_type != target_os:
        return False

    # Check OS support declared by the step class
    if node.os_type not in step_cls.SUPPORTED_OS:
        return False

    # Check required capabilities
    node_caps = node.capabilities or {}
    for cap in step_cls.REQUIRED_CAPABILITIES:
        if not node_caps.get(cap):
            return False

    return True
