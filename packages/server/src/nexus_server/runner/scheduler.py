"""Job scheduler — assigns jobs to appropriate nodes.

Placement policy for node-bound (``REQUIRES_NODE``) steps. Given a step name
and optional targeting hints, this module answers a single question: *which
registered node should run this step right now?*

Responsibilities
----------------
- Resolve targeting precedence: explicit node pin > pool membership > any
  online node.
- Filter candidates by liveness (``Node.status``) and OS compatibility (the
  step class's ``SUPPORTED_OS``, plus an optional per-step ``target_os`` pin).
- Return ``None`` when nothing matches, so the caller can fail the step with a
  descriptive error instead of hanging.

Fits between :mod:`nexus_server.runner.runner` (the only caller — see
``JobRunner._execute_remote_step``) and ``nexus_server.db.ops`` /
``nexus_common.steps.registry`` (its data sources). It performs no writes and
holds no state; placement is recomputed per step, which is what makes
per-step ``target_node_id`` / ``target_pool_id`` / ``target_os`` overrides work
inside a single job.

AI Note: there is deliberately no capability/software gate here. Node
"capabilities" were removed from the model — whether a node actually has gem5,
git, etc. is the operator's problem, surfaced as a step failure in the job log
rather than as an unschedulable job.
"""

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

    Matching considers: node status, OS support, and an optional target_os
    override (per-step pin to a specific OS family). Whether the node actually
    has the required software (gem5, git, …) is the operator's responsibility —
    a misconfigured node simply fails the step, captured in the per-job log.

    Args:
        db: Async session used for the node/pool lookups (read-only here).
        step_name: Registry key of the step about to run. Used to look up the
            step class and read its ``SUPPORTED_OS`` declaration.
        target_pool_id: Restrict candidates to members of this pool. Ignored
            when ``target_node_id`` is given.
        target_node_id: Hard pin to one node. If that node is missing or fails
            the match test, this returns ``None`` rather than falling back to
            another node — a pin is a constraint, not a preference.
        target_os: Per-step OS pin (``"linux"``, ``"macos"``, ``"windows"``).
            Narrows beyond the step class's own ``SUPPORTED_OS``.

    Returns:
        A matching :class:`~nexus_server.db.models.Node`, or ``None`` if no
        node satisfies the constraints. The caller turns ``None`` into a step
        failure with a human-readable "No available node" message.

    Raises:
        KeyError: propagated from ``get_step`` if ``step_name`` is not in the
            step registry. Submission-time validation normally catches this
            first, so hitting it here means a job was persisted with a step
            that the server no longer knows about.
    """
    step_cls = get_step(step_name)

    # Direct node targeting
    # AI Note: an explicit node pin never falls back. Returning None on a
    # mismatch (offline / wrong OS) is intentional — silently rerouting a
    # pinned step to a different machine would break jobs that pin a node to
    # reuse files it left on disk from an earlier step.
    if target_node_id:
        node = await ops.get_node_by_id(db, target_node_id)
        if node and _node_matches_step(node, step_cls, target_os):
            return node
        return None

    # Pool-based selection
    # AI Note: get_pool_nodes() does NOT filter on status, whereas the
    # no-pool branch asks list_nodes() for status="online". Both paths are
    # re-filtered by _node_matches_step below, which is what makes the pool
    # branch able to return a "busy" node while the unpooled branch cannot.
    if target_pool_id:
        nodes = await ops.get_pool_nodes(db, target_pool_id)
    else:
        nodes = await ops.list_nodes(db, status="online")

    # Filter to compatible nodes, prefer least busy
    candidates = [n for n in nodes if _node_matches_step(n, step_cls, target_os)]
    if not candidates:
        return None

    # Prefer online nodes, then sort by status (online > busy)
    # AI Note: "least busy" is aspirational — there is no load metric yet.
    # Selection is first-match on an idle node, else first busy node, using
    # whatever order the DB query returned (hostname order for list_nodes).
    # This means concurrent jobs will pile onto the same node; adding real
    # load-aware placement is a change isolated to these two lines.
    online = [n for n in candidates if n.status == "online"]
    return online[0] if online else candidates[0]


def _node_matches_step(node: Node, step_cls: type, target_os: str | None = None) -> bool:
    """Check if a node can run a given step type.

    Pure predicate — no I/O, no DB access — so it is cheap to call once per
    candidate node inside the selection loop.

    Args:
        node: Candidate node row.
        step_cls: The step class from the registry (needs ``SUPPORTED_OS``).
        target_os: Optional per-step OS pin, applied *in addition to* the step
            class's own OS support list.

    Returns:
        ``True`` if the node is alive and OS-compatible with the step.

    AI Note: "busy" counts as schedulable. Nexus does not serialize work per
    node — a node already running a step can still be handed another one, and
    the agent runs them concurrently. If per-node exclusivity is ever needed,
    this is the gate to tighten (and the ``online``-preference in
    ``find_node_for_step`` becomes a hard filter).
    """
    if node.status not in ("online", "busy"):
        return False

    # Per-step OS pin (e.g. "this gem5 sim must run on linux")
    if target_os and node.os_type != target_os:
        return False

    # Check OS support declared by the step class
    # AI Note: os_type is compared as an exact string ("linux"/"macos"/
    # "windows") reported by the agent at registration. A node that has never
    # registered has os_type=None and therefore fails here — that is the
    # desired behavior, since we know nothing about it.
    if node.os_type not in step_cls.SUPPORTED_OS:
        return False

    return True
