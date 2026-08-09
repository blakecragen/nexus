"""Node management routes — list, detail, register, provision, deregister, maintenance.

Role in the system
------------------
Mounted at ``/api/nodes`` by ``nexus_server.main.create_app``. A *node* is a
machine that runs the ``nexus-agent`` process and executes job steps. This
module is the control plane for a node's identity and lifecycle:

- **Read** (``GET ""`` / ``GET /{node_id}``): any authenticated user.
- **Write** (register, provision, reconnect, delete, maintenance): admin only.

Two ways a node comes into existence:

1. *Manual registration* (``POST ""``) mints a row plus an ``api_key`` and
   returns it. The operator then installs and starts the agent themselves,
   passing that key on the command line.
2. *Provisioning* (``POST /provision``) does both: it mints the row and then
   SSHes to the machine to clone, install and start the agent. ``POST
   /{node_id}/reconnect`` re-runs only the second half against an existing row.

Neighbouring modules
--------------------
- ``nexus_server.db.ops`` — all persistence (``create_node``, ``update_node``,
  ``delete_node``, ``list_nodes``).
- ``nexus_server.services.provisioner`` — the blocking, paramiko-based SSH
  installer plus callback-address discovery.
- ``nexus_server.api.routes.ws`` — once the agent dials back, the WebSocket
  handler flips ``status`` to ``online`` and overwrites the placeholder
  hardware fields written here.
- Frontend ``frontend/src/pages/Nodes.tsx`` is the primary consumer.

AI Note: this module never touches ``node.api_key`` except to hand it to the
provisioner or return it at creation time. The key is the agent's only
credential (see ``routes/ws.py``), so any new endpoint that echoes a node row
must not leak it — that is exactly why ``_node_to_info`` exists and why
``NodeInfo`` has no ``api_key`` field.
"""

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
    """Register a node AND set it up on the device over SSH.

    Request body for ``POST /api/nodes/provision``. Combines the SSH
    credentials needed to reach the device with the knobs that control how the
    agent is installed there.

    Attributes:
        ssh_host: Hostname or IP to SSH to. Also used as the node's
            ``hostname`` until the agent reports its real one.
        ssh_user: SSH login user on the device.
        ssh_password: Password auth. Required unless ``use_server_key``.
            Never persisted — it is passed straight to paramiko and dropped.
        use_server_key: Authenticate with the server's own SSH keys/agent
            instead of a password.
        display_name: Friendly label shown in the UI; defaults to ``ssh_host``.
        tags: Free-form labels used by the scheduler to target steps.
        service: Install an auto-start service (systemd/launchd) instead of
            launching the agent as a background process that dies on reboot.
        install_python: Allow the provisioner to install a Python >= 3.11 on
            the device if none is found. Set False on locked-down hosts.
        ws_host: Override the callback address the agent dials back to. When
            omitted the server auto-detects candidates via
            ``provisioner.callback_candidates()``.
        ws_port: Port of the server's WebSocket endpoint on the callback host.
        branch: Git branch of the agent repo to install.
        repo_url: Git remote to clone the agent from.

    AI Note: ``ws_host`` is the field to reach for when a node installs fine
    but never comes online — the auto-detected address is frequently wrong
    behind VPNs or on multi-homed servers.
    """
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
    stored, so they must be supplied again.

    Request body for ``POST /api/nodes/{node_id}/reconnect``. Deliberately
    mirrors :class:`NodeProvision` minus the fields that describe identity
    (``display_name``, ``tags``) — those already exist on the node row and are
    left untouched.

    Attributes:
        ssh_user: SSH login user on the device.
        ssh_password: Password auth. Required unless ``use_server_key``.
        use_server_key: Authenticate with the server's own SSH keys/agent.
        ssh_host: Where to SSH. Defaults to the node's last-known
            ``ip_address`` when that is a real address.
        ws_host: Override the auto-detected callback address.
        ws_port: Port of the server's WebSocket endpoint.
        branch: Git branch of the agent repo to (re)install.
        repo_url: Git remote to clone the agent from.
        install_python: Allow installing Python >= 3.11 on the device.
        service: Reinstall as an auto-start service rather than a background
            process.

    AI Note: the defaults for ``service`` and ``install_python`` differ in
    spirit from provisioning — reconnect defaults ``service`` to False, so a
    node originally provisioned as a service is reconnected as a plain
    background process unless the caller passes ``service=true``.
    """
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
    """Project a :class:`~nexus_server.db.models.Node` ORM row to the public API shape.

    Coalesces nullable columns to empty strings / zeros so the response always
    satisfies ``NodeInfo``'s non-optional fields — freshly provisioned rows
    carry placeholder or NULL hardware data until the agent registers.

    Args:
        node: A ``Node`` ORM instance (not type-annotated because the same
            helper is duplicated in ``routes/pools.py``).

    Returns:
        NodeInfo: The serializable view of the node.

    AI Note: this projection is the reason ``api_key`` never leaks to
    non-admin API consumers — ``NodeInfo`` simply has no such field. Adding
    one here would expose every node's agent credential to any authenticated
    user via ``GET /api/nodes``.

    AI Note: an identical copy of this function lives in ``routes/pools.py``.
    If you add or rename a ``NodeInfo`` field, update both or pool detail
    responses will start failing validation.
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


@router.get("", response_model=list[NodeInfo])
async def list_nodes(
    db: DbSession,
    user: CurrentUser,
    os_type: str | None = None,
    node_status: str | None = None,
    pool_id: UUID | None = None,
):
    """List all nodes, optionally filtered by os_type, status, or pool membership.

    Args:
        db: Request-scoped DB session.
        user: Any authenticated user (no pool ACL is applied — node visibility
            is global).
        os_type: Filter by OS family, e.g. ``"linux"`` / ``"macos"``.
        node_status: Filter by lifecycle status (``online``, ``offline``,
            ``maintenance``). Named ``node_status`` rather than ``status``
            because ``status`` is already bound to the imported FastAPI
            status-code module in this file.
        pool_id: Restrict to nodes belonging to this pool (adds a join on the
            membership table).

    Returns:
        list[NodeInfo]: Matching nodes, api_key excluded.
    """
    nodes = await ops.list_nodes(db, os_type=os_type, status=node_status, pool_id=pool_id)
    return [_node_to_info(n) for n in nodes]


@router.get("/{node_id}", response_model=NodeInfo)
async def get_node(node_id: UUID, db: DbSession, user: CurrentUser):
    """Get detailed information about a single node.

    Args:
        node_id: Node UUID from the path.
        db: Request-scoped DB session.
        user: Any authenticated user.

    Returns:
        NodeInfo: The node's public view.

    Raises:
        HTTPException: 404 if no node with that ID exists.
    """
    node = await ops.get_node_by_id(db, node_id)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return _node_to_info(node)


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_node(body: NodeRegistration, db: DbSession, admin: AdminUser):
    """Register a new node (admin only). Returns the node info with its API key.

    Creates the DB row and mints a UUID plus an ``api_key``. No SSH is
    performed — the caller is responsible for installing and starting the
    agent on the device with the returned key (see ``POST /provision`` for the
    automated path).

    Args:
        body: Full hardware/identity description supplied by the caller.
        db: Request-scoped DB session (a row is committed).
        admin: Enforces admin role.

    Returns:
        dict: ``{"node": NodeInfo, "api_key": str}``. The API key is shown only
        in this response and is never returned again.
    """
    # AI Note: this is the ONLY response in the API that contains `api_key`,
    # and it is returned exactly once — the plaintext key is stored on the row
    # but never re-exposed through NodeInfo. If the operator loses it, the node
    # must be deleted and re-registered, or reconnected via
    # POST /{node_id}/reconnect (which reuses the stored key server-side).
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
    reconnect.

    Side effects: opens an SSH connection to the device, clones/installs the
    agent repo and starts the process there. Reads (but does not write) the
    node row while polling.

    Args:
        node_id: Node UUID as a string — this is baked into the agent's
            WebSocket URL on the device, so it must match the DB row exactly.
        api_key: The node's agent credential, written into the device's agent
            config. Never logged.
        body: A :class:`NodeProvision` or :class:`NodeReconnect`; only the
            fields common to both are read (duck-typed on purpose so one
            helper serves both endpoints).
        ssh_host: Resolved SSH target — the caller decides whether that came
            from the request or the node's last-known IP.

    Returns:
        dict: ``{"result": <provisioner dict>, "online": bool, "fresh": Node|None}``.
        ``result`` carries ``ok``, ``log``, and on success ``ws_url``/``ws_host``/
        ``mode``. ``fresh`` is the re-read node row from the last poll (None if
        provisioning failed or the row vanished).

    AI Note: ``provisioner.provision`` is blocking paramiko I/O, so it is
    pushed onto a worker thread with ``asyncio.to_thread``. Calling it directly
    would stall the entire event loop — including every other request and every
    agent WebSocket — for the minutes an install can take.
    """
    # AI Note: an explicit ws_host wins outright; otherwise the provisioner is
    # given an ordered candidate list (mDNS hostname first, then IPv4s) and
    # tries each until the agent's dial-back succeeds. Preferring the hostname
    # is what lets a node survive the server's IP changing.
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
    # AI Note: this is a real correctness constraint, not a style choice. The
    # request's session has already loaded (or will cache) the Node in its
    # identity map; the agent's status flip is committed by the WebSocket
    # handler on a *different* session, so only a brand-new session sees it.
    # The import is function-local to avoid an import cycle at module load.
    from nexus_server.db.session import get_session_factory
    session_factory = get_session_factory()
    online = False
    fresh = None
    # AI Note: fixed budget of 10 polls x 2s = ~20s max wait, deliberately
    # shorter than a typical HTTP client timeout. Timing out here is NOT an
    # error — the agent keeps retrying its dial-back, so the caller gets
    # online=False plus the advisory note from _not_online_note() and the node
    # may still come up moments later.
    for _ in range(10):
        await asyncio.sleep(2)
        async with session_factory() as poll_db:
            fresh = await ops.get_node_by_id(poll_db, UUID(node_id))
        if fresh and fresh.status == "online":
            online = True
            break
    return {"result": result, "online": online, "fresh": fresh}


def _not_online_note(result: dict) -> str:
    """Build the human-readable advisory appended to the log when the agent never dialed back.

    Args:
        result: The provisioner result dict; only ``ws_host`` is read.

    Returns:
        str: A diagnostic sentence naming the callback address that failed and
        the usual causes, suitable for display in the UI's provisioning log.

    AI Note: this string is surfaced verbatim to operators in the Nodes page,
    so keep it actionable — it is the main troubleshooting hint for the most
    common provisioning failure mode (install succeeds, WebSocket never
    connects).
    """
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

    Args:
        body: SSH target, credentials, and install options.
        db: Request-scoped DB session (creates, and on failure deletes, a row).
        admin: Enforces admin role.

    Returns:
        dict: ``{"node", "api_key", "ws_url", "mode", "online", "log"}``. The
        ``api_key`` is returned once here so the operator can re-install by
        hand later. ``online`` is False when the install succeeded but the
        agent's dial-back has not landed within the poll window; ``log`` then
        carries an extra diagnostic line.

    Raises:
        HTTPException: 422 when neither ``ssh_password`` nor ``use_server_key``
            is supplied; 502 if the SSH provisioning itself failed (detail
            contains the provisioner's error and full log).

    Note:
        This endpoint is long-running: an SSH install plus up to ~20 seconds of
        polling for the agent to connect back. Clients should use a generous
        request timeout.
    """
    # AI Note: a client-side timeout does NOT roll back the install already
    # performed on the device — only an explicit provisioner failure triggers
    # the compensating delete below.
    #
    # AI Note: security-sensitive precondition. Without it, paramiko would fall
    # back to whatever keys the server process happens to have loaded, so a
    # caller who supplies no credential could unintentionally provision using
    # the server's identity. `use_server_key` makes that opt-in and explicit.
    if not body.use_server_key and not body.ssh_password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide ssh_password, or set use_server_key to use the server's SSH keys.",
        )

    name = body.display_name or body.ssh_host
    # Register (mint UUID + api_key). Hardware fields are placeholders — the
    # agent reports real specs once it connects.
    # AI Note: ordering matters — the row must exist before provisioning
    # because the node UUID and api_key are baked into the agent config written
    # on the device. The placeholders (os_type="linux", cpu_model="pending",
    # ip_address="0.0.0.0") are overwritten by the "register" message handled
    # in routes/ws.py. Treat a node still showing "pending" as one that has
    # never successfully connected.
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
        # AI Note: compensating delete. Unlike /reconnect, a failed provision
        # rolls back the row it just created so a retry with corrected
        # credentials does not accumulate duplicate/orphan nodes.
        await ops.delete_node(db, node.id)  # leave no orphan
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": result.get("error", "provisioning failed"), "log": result.get("log", [])},
        )

    log = result.get("log", [])
    if not outcome["online"]:
        log = log + [_not_online_note(result)]

    return {
        # AI Note: prefer the freshly re-read row — if the agent connected
        # during polling it has already replaced the placeholder hardware
        # fields, so `fresh` shows real specs while `node` still shows
        # "pending".
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

    Args:
        node_id: Existing node UUID from the path.
        body: SSH credentials plus optional overrides for host/callback.
        db: Request-scoped DB session (read-only here; the WS handler is what
            flips status to online).
        admin: Enforces admin role.

    Returns:
        dict: ``{"node", "ws_url", "mode", "online", "log"}``. Note there is no
        ``api_key`` in this response — the existing key is reused on the device
        and is never re-disclosed.

    Raises:
        HTTPException: 404 if the node does not exist; 422 if no SSH credential
            was supplied or no SSH host can be determined; 502 if the SSH setup
            failed.
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
    # AI Note: "0.0.0.0" is the placeholder written by /provision before the
    # agent ever registers, so it is excluded alongside None/"" — SSHing to it
    # would hang rather than fail fast.
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
    """Deregister a node (admin only).

    Permanently removes the node row (and, via cascade, its pool memberships).
    The agent process on the device is NOT stopped — it will keep dialing back
    and be rejected by the WebSocket handler with code 4003 once its row is
    gone.

    Args:
        node_id: Node UUID from the path.
        db: Request-scoped DB session (row is deleted and committed).
        admin: Enforces admin role.

    Raises:
        HTTPException: 404 if the node does not exist.
    """
    # AI Note: no guard against deleting a node that is mid-job. In-flight
    # steps dispatched to it will never report back and the job stalls until
    # its own timeout — check for running work before deregistering a busy
    # node.
    deleted = await ops.delete_node(db, node_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")


@router.put("/{node_id}/maintenance")
async def toggle_maintenance(node_id: UUID, enable: bool, db: DbSession, admin: AdminUser):
    """Toggle maintenance mode on a node (admin only).

    Maintenance is expressed purely as a ``status`` value: the scheduler
    (``runner/scheduler.py``) only ever places steps on nodes whose status is
    ``"online"``, so setting ``"maintenance"`` drains the node of new work
    without deleting it or stopping its agent.

    Args:
        node_id: Node UUID from the path.
        enable: Query parameter — True sets ``maintenance``, False sets
            ``offline``.
        db: Request-scoped DB session (status column is updated).
        admin: Enforces admin role.

    Returns:
        NodeInfo: The node with its new status.

    Raises:
        HTTPException: 404 if the node does not exist.

    Note:
        Disabling maintenance sets the node to ``offline``, not ``online``. A
        connected agent's next heartbeat restores ``online`` automatically.
    """
    # AI Note: only the agent's WebSocket connect/heartbeat in routes/ws.py is
    # allowed to assert that a node is actually reachable, which is why this
    # writes "offline" rather than "online" when disabling maintenance.
    #
    # AI Note: this is a blunt write. Toggling maintenance ON for an *online*
    # node overwrites the live status until the next heartbeat — that is what
    # makes the drain take effect immediately.
    new_status = "maintenance" if enable else "offline"
    node = await ops.update_node(db, node_id, status=new_status)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return _node_to_info(node)
