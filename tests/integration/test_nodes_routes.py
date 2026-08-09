"""Integration tests for the node management routes (api/routes/nodes.py).

Covers the hermetic, no-SSH routes end-to-end through the FastAPI app:
list/detail/register/deregister/maintenance, including authz (some routes are
admin-only) and 404/422 error paths. The provision + reconnect routes SSH out
to real devices; we DO NOT run them for real — the one provision test
monkeypatches the provisioner so no network/SSH happens, and reconnect's
input-validation guards are exercised before any SSH would occur.

SAFETY — read before adding a test here
    ``POST /api/nodes/provision`` and ``POST /api/nodes/{id}/reconnect`` open a
    real SSH connection and install/restart the agent on a remote host. Any new
    test that reaches those handlers MUST monkeypatch
    ``nexus_server.services.provisioner.provision`` (and
    ``callback_candidates``, which probes local network interfaces). Forgetting
    to do so turns a unit-test run into an outbound connection attempt against
    whatever address the payload names.

    Both routes also poll for the agent to come back online in a
    ``10 x asyncio.sleep(2)`` loop. Tests that let that loop run must patch
    ``nodes_mod.asyncio.sleep`` or they add ~20s of wall time each.

Authorization split
    Read routes (list, detail) accept any authenticated user. Every mutating
    route (register, deregister, maintenance, provision, reconnect) is
    admin-only, and each has a matching 403 test.

Secret-handling invariant
    ``api_key`` is a node's agent credential. It is returned exactly once — in
    the registration/provision response — and must never appear in the list or
    detail serialisations. Several tests assert its absence explicitly.

Provision vs. reconnect (easy to conflate)
    * provision: creates a NEW node row, and DELETES it again if the SSH
      install fails, so a failed attempt leaves no orphan.
    * reconnect: reuses an EXISTING node's id and api_key (preserving its
      identity and history) and leaves the row in place even on failure.
"""

from __future__ import annotations

import uuid

import pytest

from nexus_server.db import ops


# ── helpers ──────────────────────────────────────────────────────────────────


def _registration_payload(**overrides) -> dict:
    """A valid NodeRegistration body. Override individual fields as needed.

    Every field the schema requires is present, so a 422 from a test using this
    helper means the override introduced the problem (which is how the
    invalid-os_type and missing-field tests work).

    Args:
        **overrides: Field replacements — most commonly ``hostname`` (keep it
            unique per test) or ``os_type``.

    Returns:
        A JSON-able dict suitable for ``POST /api/nodes``.
    """
    body = {
        "hostname": "reg-node.test",
        "display_name": "Registered Node",
        "os_type": "linux",
        "os_version": "Ubuntu 24.04",
        "arch": "x86_64",
        "cpu_model": "Xeon",
        "cpu_cores": 16,
        "ram_mb": 32768,
        "gpu_info": None,
        "agent_version": "0.1.0",
        "ip_address": "10.1.2.3",
        "tags": ["gpu", "fast"],
    }
    body.update(overrides)
    return body


# ── GET /api/nodes (list) ────────────────────────────────────────────────────


async def test_list_nodes_returns_existing(auth_client, sample_node):
    """A regular (non-admin) user can list nodes; the sample node is present."""
    resp = auth_client.get("/api/nodes")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    ids = {n["id"] for n in body}
    assert str(sample_node.id) in ids


async def test_list_nodes_requires_auth(client):
    """Unauthenticated requests are rejected."""
    resp = client.get("/api/nodes")
    assert resp.status_code in (401, 403)


async def test_list_nodes_filter_by_status(auth_client, db):
    """The node_status query param filters the result server-side."""
    await ops.create_node(
        db, hostname="online-a.test", os_type="linux", os_version="x",
        arch="x86_64", cpu_model="c", cpu_cores=1, ram_mb=512,
        agent_version="0.1.0", ip_address="10.0.0.5", status="online",
    )
    await ops.create_node(
        db, hostname="offline-b.test", os_type="linux", os_version="x",
        arch="x86_64", cpu_model="c", cpu_cores=1, ram_mb=512,
        agent_version="0.1.0", ip_address="10.0.0.6", status="offline",
    )

    resp = auth_client.get("/api/nodes", params={"node_status": "offline"})
    assert resp.status_code == 200
    statuses = {n["status"] for n in resp.json()}
    hostnames = {n["hostname"] for n in resp.json()}
    assert statuses == {"offline"}
    assert "offline-b.test" in hostnames
    assert "online-a.test" not in hostnames


async def test_list_nodes_filter_by_os_type(auth_client, db):
    """The os_type query param filters by OS family."""
    await ops.create_node(
        db, hostname="mac.test", os_type="macos", os_version="14",
        arch="arm64", cpu_model="M3", cpu_cores=8, ram_mb=16384,
        agent_version="0.1.0", ip_address="10.0.0.7", status="online",
    )
    await ops.create_node(
        db, hostname="win.test", os_type="windows", os_version="11",
        arch="x86_64", cpu_model="c", cpu_cores=4, ram_mb=8192,
        agent_version="0.1.0", ip_address="10.0.0.8", status="online",
    )

    resp = auth_client.get("/api/nodes", params={"os_type": "macos"})
    assert resp.status_code == 200
    os_types = {n["os_type"] for n in resp.json()}
    assert os_types == {"macos"}


async def test_list_nodes_filter_no_match_returns_empty(auth_client, sample_node):
    """A filter that matches no node returns an empty list, not all nodes.

    Guards the classic filter bug where an unmatched value is ignored and the
    unfiltered set is returned — which in the UI would look like the filter
    silently doing nothing.
    """
    resp = auth_client.get("/api/nodes", params={"node_status": "draining"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_nodes_never_leaks_api_key(auth_client, sample_node):
    """The list view must not expose api_key for any node.

    A node's api_key authenticates its agent WebSocket. Leaking it in a
    list any authenticated user can read would let that user impersonate the
    node and report fabricated step results.
    """
    resp = auth_client.get("/api/nodes")
    assert resp.status_code == 200
    assert all("api_key" not in n for n in resp.json())


async def test_list_nodes_filter_by_pool_membership(auth_client, db, sample_pool):
    """The pool_id query param restricts to members of that pool."""
    in_pool = await ops.create_node(
        db, hostname="member.test", os_type="linux", os_version="x",
        arch="x86_64", cpu_model="c", cpu_cores=1, ram_mb=512,
        agent_version="0.1.0", ip_address="10.0.0.9", status="online",
    )
    await ops.create_node(
        db, hostname="outsider.test", os_type="linux", os_version="x",
        arch="x86_64", cpu_model="c", cpu_cores=1, ram_mb=512,
        agent_version="0.1.0", ip_address="10.0.0.10", status="online",
    )
    await ops.add_node_to_pool(db, sample_pool.id, in_pool.id)

    resp = auth_client.get("/api/nodes", params={"pool_id": str(sample_pool.id)})
    assert resp.status_code == 200
    hostnames = {n["hostname"] for n in resp.json()}
    assert hostnames == {"member.test"}


# ── GET /api/nodes/{id} (detail) ─────────────────────────────────────────────


async def test_get_node_detail_200(auth_client, sample_node):
    """Fetching an existing node returns its full NodeInfo."""
    resp = auth_client.get(f"/api/nodes/{sample_node.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(sample_node.id)
    assert body["hostname"] == "node-1.test"
    assert body["cpu_cores"] == 8
    assert body["ram_mb"] == 16384
    # api_key must never be exposed via the detail view.
    assert "api_key" not in body


async def test_get_node_detail_404(auth_client):
    """Fetching a non-existent node returns 404 with the documented detail."""
    missing = uuid.uuid4()
    resp = auth_client.get(f"/api/nodes/{missing}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Node not found"


async def test_get_node_detail_invalid_uuid_422(auth_client):
    """A malformed node id fails path validation (not a 404)."""
    resp = auth_client.get("/api/nodes/not-a-uuid")
    assert resp.status_code == 422


# ── POST /api/nodes (register) ───────────────────────────────────────────────


async def test_register_node_returns_api_key(admin_client, db):
    """Admin registration creates a node and returns its api_key + info.

    The registration response is the ONLY time the api_key is disclosed (the
    operator copies it into the agent's config), so this test pins both halves
    of the contract: the key is present at the top level, and absent from the
    nested node object that every other endpoint serialises. The DB comparison
    proves the returned key is the real persisted secret rather than a
    placeholder.
    """
    resp = admin_client.post("/api/nodes", json=_registration_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert "api_key" in body and body["api_key"]
    assert body["node"]["hostname"] == "reg-node.test"
    assert body["node"]["os_type"] == "linux"
    assert body["node"]["tags"] == ["gpu", "fast"]
    assert body["node"]["cpu_cores"] == 16
    assert body["node"]["ram_mb"] == 32768
    # The api_key must live on the nested node object too (no leak there).
    assert "api_key" not in body["node"]
    # The returned api_key is the REAL persisted secret for this node.
    node_id = body["node"]["id"]
    persisted = await ops.get_node_by_id(db, uuid.UUID(node_id))
    assert persisted is not None
    assert persisted.api_key == body["api_key"]
    # And it must be a real, fetchable node via the API.
    follow = admin_client.get(f"/api/nodes/{node_id}")
    assert follow.status_code == 200


async def test_register_node_appears_in_list(admin_client):
    """A registered node shows up in the subsequent list response."""
    resp = admin_client.post(
        "/api/nodes", json=_registration_payload(hostname="listed.test")
    )
    assert resp.status_code == 201
    new_id = resp.json()["node"]["id"]
    listing = admin_client.get("/api/nodes")
    assert listing.status_code == 200
    assert new_id in {n["id"] for n in listing.json()}


async def test_register_node_requires_admin(auth_client):
    """A regular user cannot register a node (admin-only route)."""
    resp = auth_client.post("/api/nodes", json=_registration_payload())
    assert resp.status_code == 403


async def test_register_node_requires_auth(client):
    """Unauthenticated registration is rejected."""
    resp = client.post("/api/nodes", json=_registration_payload())
    assert resp.status_code in (401, 403)


async def test_register_node_validation_error(admin_client):
    """Missing required fields yields a 422 validation error."""
    resp = admin_client.post("/api/nodes", json={"hostname": "incomplete.test"})
    assert resp.status_code == 422


async def test_register_node_invalid_os_type(admin_client):
    """An os_type outside the OSType enum is rejected.

    The scheduler matches a step's supported OSes against this field, so an
    arbitrary string would create a node that silently matches nothing (or
    everything, depending on the comparison) forever after.
    """
    resp = admin_client.post(
        "/api/nodes", json=_registration_payload(os_type="solaris")
    )
    assert resp.status_code == 422


# ── DELETE /api/nodes/{id} (deregister) ──────────────────────────────────────


async def test_deregister_node_204(admin_client, sample_node):
    """Admin can delete a node; it returns 204 and the node is gone.

    The follow-up 404 matters because the delete op returns a bool the route
    turns into a status code — a handler that returned 204 without deleting
    would pass on the status assertion alone.
    """
    resp = admin_client.delete(f"/api/nodes/{sample_node.id}")
    assert resp.status_code == 204
    # The node should no longer be fetchable.
    follow = admin_client.get(f"/api/nodes/{sample_node.id}")
    assert follow.status_code == 404


async def test_deregister_node_404(admin_client):
    """Deleting a non-existent node returns 404."""
    resp = admin_client.delete(f"/api/nodes/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Node not found"


async def test_deregister_node_requires_admin(auth_client, sample_node):
    """A regular user cannot deregister a node."""
    resp = auth_client.delete(f"/api/nodes/{sample_node.id}")
    assert resp.status_code == 403


# ── PUT /api/nodes/{id}/maintenance (toggle) ─────────────────────────────────


async def test_maintenance_enable_sets_status(admin_client, sample_node):
    """enable=true moves the node into maintenance status (and persists).

    Maintenance takes a node out of scheduling rotation (the scheduler only
    accepts ``online``/``busy``), so this is how an operator safely drains a
    host before working on it. The durability check uses a second request — a
    non-committed change would still look right in the first response.
    """
    resp = admin_client.put(
        f"/api/nodes/{sample_node.id}/maintenance", params={"enable": True}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "maintenance"
    # The change is durable: a fresh GET (new request/session) still sees it.
    follow = admin_client.get(f"/api/nodes/{sample_node.id}")
    assert follow.status_code == 200
    assert follow.json()["status"] == "maintenance"


async def test_maintenance_disable_sets_offline(admin_client, sample_node):
    """enable=false takes the node out of maintenance into offline.

    ``offline`` — not ``online`` — is the deliberate exit state: the server
    cannot know the agent is healthy, so the node stays out of rotation until
    the agent's own heartbeat marks it online again. Flipping straight to
    online would schedule work onto a host with no live socket.
    """
    # First put it into maintenance.
    admin_client.put(
        f"/api/nodes/{sample_node.id}/maintenance", params={"enable": True}
    )
    resp = admin_client.put(
        f"/api/nodes/{sample_node.id}/maintenance", params={"enable": False}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "offline"


async def test_maintenance_404(admin_client):
    """Toggling maintenance on a missing node returns 404."""
    resp = admin_client.put(
        f"/api/nodes/{uuid.uuid4()}/maintenance", params={"enable": True}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Node not found"


async def test_maintenance_requires_admin(auth_client, sample_node):
    """A regular user cannot toggle maintenance."""
    resp = auth_client.put(
        f"/api/nodes/{sample_node.id}/maintenance", params={"enable": True}
    )
    assert resp.status_code == 403


async def test_maintenance_requires_enable_param(admin_client, sample_node):
    """The enable query param is required (422 when absent)."""
    resp = admin_client.put(f"/api/nodes/{sample_node.id}/maintenance")
    assert resp.status_code == 422


# ── POST /api/nodes/provision (SSH — monkeypatched, NO real network) ──────────


async def test_provision_requires_admin(auth_client):
    """A regular user cannot provision (admin-only), even before any SSH.

    Safe to run unpatched precisely because the 403 fires in the dependency
    layer — the handler body (and therefore ``provisioner.provision``) is never
    reached, so no outbound connection is attempted.
    """
    resp = auth_client.post(
        "/api/nodes/provision",
        json={"ssh_host": "1.2.3.4", "ssh_user": "ubuntu", "ssh_password": "pw"},
    )
    assert resp.status_code == 403


async def test_provision_requires_credentials(admin_client):
    """Without ssh_password and without use_server_key, provision is rejected
    BEFORE any SSH is attempted (422).

    Fail-fast guard: with no credential the SSH layer would fall back to
    whatever agent/key material the server process happens to have, producing a
    slow, confusing failure (or worse, an unintended successful login). Also
    safe to run unpatched for the same reason.
    """
    resp = admin_client.post(
        "/api/nodes/provision",
        json={"ssh_host": "1.2.3.4", "ssh_user": "ubuntu"},
    )
    assert resp.status_code == 422
    assert "ssh_password" in resp.json()["detail"]


async def test_provision_success_monkeypatched(admin_client, monkeypatch, db):
    """Happy-path provision with the provisioner stubbed out — NO real SSH.

    We patch provisioner.provision (the only outbound boundary) to return a
    successful result, and stub callback_candidates so no host networking is
    touched. The agent never actually connects, so online=False and the
    not-online note is appended — exercising the route's real result handling
    while the node is persisted and returned with its api_key.

    The ``captured`` assertions are the substantive part: they prove the route
    minted the node FIRST and handed that node's real id + api_key down to the
    provisioner, which is what lets the installed agent authenticate back. If
    the route generated the credentials after (or independently of) the DB row,
    the agent would connect with a key the server does not recognise.
    """
    from nexus_server.services import provisioner

    captured = {}

    def fake_provision(**kwargs):
        """Stand in for the SSH install; record the identity the route passed."""
        # Capture what the route passed so we can assert it wired identity through.
        captured.update(kwargs)
        return {
            "ok": True,
            "ws_url": "ws://10.0.0.1:8000/ws/agent",
            "ws_host": "10.0.0.1",
            "mode": "background",
            "log": ["cloned repo", "started agent"],
        }

    # AI Note: all three patches are required for this test to be hermetic.
    #   1. provision          → the SSH/install boundary.
    #   2. callback_candidates→ enumerates local interfaces to guess the URL the
    #      agent should dial back on; unpatched it inspects the real host.
    #   3. asyncio.sleep      → the route polls "is the agent online yet?" ten
    #      times with a 2s sleep. Patched to a no-op the loop still runs all ten
    #      iterations (exercising the real logic) but costs no wall time.
    #      Patching the module attribute (``nodes_mod.asyncio``) rather than the
    #      global asyncio keeps the no-op scoped to this route.
    monkeypatch.setattr(provisioner, "provision", fake_provision)
    monkeypatch.setattr(provisioner, "callback_candidates", lambda: ["10.0.0.1"])
    # Don't actually sleep through the 10x2s online-poll loop.
    import nexus_server.api.routes.nodes as nodes_mod

    async def _no_sleep(_seconds):
        """No-op replacement for ``asyncio.sleep`` inside the poll loop."""
        return None

    monkeypatch.setattr(nodes_mod.asyncio, "sleep", _no_sleep)

    resp = admin_client.post(
        "/api/nodes/provision",
        json={
            "ssh_host": "10.9.9.9",
            "ssh_user": "ubuntu",
            "ssh_password": "secret",
            "display_name": "Provisioned",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["api_key"]
    assert body["mode"] == "background"
    # AI Note: online=False is CORRECT here, not a bug. No real agent exists to
    # dial back, so the poll loop exhausts and the route appends its advisory
    # note. This is the "install succeeded but the agent has not phoned home"
    # path, which is exactly the case operators most need reported clearly.
    assert body["online"] is False
    assert body["ws_url"] == "ws://10.0.0.1:8000/ws/agent"
    # The not-online note is appended to the install log (after the real lines).
    assert body["log"][:2] == ["cloned repo", "started agent"]
    assert any("NOT connected back" in line for line in body["log"])
    assert body["node"]["hostname"] == "10.9.9.9"
    assert body["node"]["display_name"] == "Provisioned"

    # The route actually minted and persisted the node, and handed the real
    # node id + api_key to the provisioner (not a tautology — these are the
    # route's own values, verified against the DB).
    node_id = body["node"]["id"]
    persisted = await ops.get_node_by_id(db, uuid.UUID(node_id))
    assert persisted is not None
    assert persisted.api_key == body["api_key"]
    assert captured["node_id"] == node_id
    assert captured["api_key"] == body["api_key"]
    assert captured["host"] == "10.9.9.9"


async def test_provision_failure_deregisters(admin_client, monkeypatch, db):
    """When provisioning fails, the route deletes the node (no orphan) and
    returns 502. Provisioner is stubbed — no real SSH.

    Compensating-transaction guarantee: the node row is created BEFORE the SSH
    install (the installer needs its id and api_key), so a failed install must
    roll it back. Otherwise every failed attempt would leave a permanently
    offline phantom node in the UI and in scheduler candidate queries. Contrast
    with reconnect, which must NOT delete on failure.

    No ``asyncio.sleep`` patch is needed here because the failure path returns
    before the online-poll loop.
    """
    from nexus_server.services import provisioner

    def fake_provision(**kwargs):
        """Simulate an SSH failure result (the route maps ok=False to 502)."""
        return {"ok": False, "error": "ssh connect failed", "log": ["timeout"]}

    monkeypatch.setattr(provisioner, "provision", fake_provision)
    monkeypatch.setattr(provisioner, "callback_candidates", lambda: ["10.0.0.1"])

    before = await ops.list_nodes(db)
    resp = admin_client.post(
        "/api/nodes/provision",
        json={"ssh_host": "10.9.9.9", "ssh_user": "ubuntu", "ssh_password": "x"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "ssh connect failed"
    assert resp.json()["detail"]["log"] == ["timeout"]

    # No orphan node should be left behind — neither in count nor by hostname.
    after = await ops.list_nodes(db)
    assert len(after) == len(before)
    assert "10.9.9.9" not in {n.hostname for n in after}


# ── POST /api/nodes/{id}/reconnect (SSH guards — NO real network) ─────────────


async def test_reconnect_requires_admin(auth_client, sample_node):
    """A regular user cannot reconnect a node."""
    resp = auth_client.post(
        f"/api/nodes/{sample_node.id}/reconnect",
        json={"ssh_user": "ubuntu", "ssh_password": "pw"},
    )
    assert resp.status_code == 403


async def test_reconnect_404_for_missing_node(admin_client):
    """Reconnecting a non-existent node returns 404 before any SSH."""
    resp = admin_client.post(
        f"/api/nodes/{uuid.uuid4()}/reconnect",
        json={"ssh_user": "ubuntu", "ssh_password": "pw"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Node not found"


async def test_reconnect_requires_credentials(admin_client, sample_node):
    """Without ssh_password / use_server_key, reconnect is rejected (422)
    before any SSH is attempted."""
    resp = admin_client.post(
        f"/api/nodes/{sample_node.id}/reconnect",
        json={"ssh_user": "ubuntu"},
    )
    assert resp.status_code == 422
    assert "ssh_password" in resp.json()["detail"]


async def test_reconnect_no_ssh_host_known(admin_client, db):
    """When the node has no usable IP and no ssh_host is supplied, reconnect
    422s before any SSH (the IP placeholder '0.0.0.0' is treated as unknown).

    ``0.0.0.0`` is what gets stored when a node registers without a resolvable
    address. Treating it as a real target would SSH to the wildcard address —
    a nonsensical connection attempt — so the route rejects it up front and
    asks the operator for an explicit host.
    """
    node = await ops.create_node(
        db, hostname="no-ip.test", os_type="linux", os_version="x",
        arch="x86_64", cpu_model="c", cpu_cores=1, ram_mb=512,
        agent_version="0.1.0", ip_address="0.0.0.0", status="offline",
    )
    resp = admin_client.post(
        f"/api/nodes/{node.id}/reconnect",
        json={"ssh_user": "ubuntu", "ssh_password": "pw"},
    )
    assert resp.status_code == 422
    assert "No SSH host" in resp.json()["detail"]


async def test_reconnect_success_uses_last_known_ip(admin_client, monkeypatch, sample_node):
    """Reconnect defaults ssh_host to the node's last-known IP, reuses the node's
    existing identity (UUID + api_key), and does NOT delete the node on success.
    Provisioner is stubbed — NO real SSH.

    Identity preservation is the whole point of reconnect versus provision: the
    node keeps its id, so its job history, pool memberships and existing agent
    credential all survive a reinstall. Re-minting the api_key would silently
    invalidate any config already deployed on the host.
    """
    from nexus_server.services import provisioner

    captured = {}

    def fake_provision(**kwargs):
        """Stub the SSH reinstall and capture the identity passed to it."""
        captured.update(kwargs)
        return {
            "ok": True,
            "ws_url": "ws://10.0.0.1:8000/ws/agent",
            "ws_host": "10.0.0.1",
            "mode": "service",
            "log": ["reinstalled agent"],
        }

    monkeypatch.setattr(provisioner, "provision", fake_provision)
    monkeypatch.setattr(provisioner, "callback_candidates", lambda: ["10.0.0.1"])
    import nexus_server.api.routes.nodes as nodes_mod

    async def _no_sleep(_seconds):
        """No-op replacement for the online-poll loop's sleep."""
        return None

    monkeypatch.setattr(nodes_mod.asyncio, "sleep", _no_sleep)

    resp = admin_client.post(
        f"/api/nodes/{sample_node.id}/reconnect",
        json={"ssh_user": "ubuntu", "ssh_password": "pw"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "service"
    # sample_node is already "online", so the poll loop sees it online on the
    # first iteration: online=True and NO not-online note is appended.
    # AI Note: this asymmetry with the provision test (online=False there) is
    # entirely due to the sample_node fixture's status="online". It is what
    # lets us assert the *positive* branch of the poll loop — the note is only
    # appended when the loop exhausts.
    assert body["online"] is True
    assert body["log"] == ["reinstalled agent"]
    assert not any("NOT connected back" in line for line in body["log"])
    # Reconnect does not return a new api_key (reuses existing identity).
    assert "api_key" not in body
    # It SSHed to the node's last-known IP (10.0.0.1) using the node's own id.
    assert captured["host"] == "10.0.0.1"
    assert captured["node_id"] == str(sample_node.id)
    assert captured["api_key"] == sample_node.api_key
    # The node is NOT deleted by reconnect — it still exists (fresh GET).
    still_there = admin_client.get(f"/api/nodes/{sample_node.id}")
    assert still_there.status_code == 200


async def test_reconnect_failure_keeps_node(admin_client, monkeypatch, db, sample_node):
    """A failed reconnect returns 502 but leaves the node in place (unlike
    provision, which deregisters on failure).

    The divergence is deliberate: provision's node row is brand new and worth
    nothing if the install fails, whereas reconnect's node predates the attempt
    and carries history. Deleting it on a transient SSH error would destroy
    real data over a retryable failure.
    """
    from nexus_server.services import provisioner

    def fake_provision(**kwargs):
        """Simulate an SSH refusal during reinstall."""
        return {"ok": False, "error": "ssh refused", "log": ["denied"]}

    monkeypatch.setattr(provisioner, "provision", fake_provision)
    monkeypatch.setattr(provisioner, "callback_candidates", lambda: ["10.0.0.1"])

    resp = admin_client.post(
        f"/api/nodes/{sample_node.id}/reconnect",
        json={"ssh_user": "ubuntu", "ssh_password": "pw"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "ssh refused"
    # The node survives a failed reconnect.
    assert await ops.get_node_by_id(db, sample_node.id) is not None
