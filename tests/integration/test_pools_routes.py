"""Integration tests for the pool management routes.

SUT: packages/server/src/nexus_server/api/routes/pools.py (mounted at /api/pools).

Covers list / create / get / delete, node add+remove membership, and the
authorization split: create is allowed for any authenticated user, but delete is
admin-only (``AdminUser`` dependency). Membership is asserted via the
``node_count`` field and the ``nodes`` array returned by GET /api/pools/{id}.

What a pool is
    A named group of nodes that a job can target instead of naming a specific
    machine (``target_pool_id``). The scheduler narrows its candidate search to
    pool members, and ``GroupPoolAccess`` grants are expressed per pool, so
    pools are both a routing and an authorization boundary.

Authorization split (deliberate, not an oversight)
    list / get / create / add-node use ``CurrentUser`` — any authenticated user.
    Only DELETE requires ``AdminUser``. Users are expected to organise their own
    pools; destroying one (which other users' queued jobs may target) is the
    privileged operation.

Client-fixture caveat
    ``auth_client`` and ``admin_client`` are the SAME TestClient with different
    default headers, so a single test must use only one of them. See
    ``test_delete_pool_as_regular_user_forbidden`` for the full explanation.

Nothing here is stubbed — real routes, real ops, real in-memory DB.
"""

from __future__ import annotations

import uuid

import pytest


# ── list ──────────────────────────────────────────────────────────────────


def test_list_pools_empty(admin_client):
    """With no pools created, the list endpoint returns an empty array."""
    resp = admin_client.get("/api/pools")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_pools_returns_created_pool_with_node_count(admin_client):
    """A created pool shows up in the list with node_count == 0."""
    admin_client.post("/api/pools", json={"name": "alpha", "description": "first"})

    resp = admin_client.get("/api/pools")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "alpha"
    assert body[0]["description"] == "first"
    assert body[0]["node_count"] == 0


def test_list_pools_ordered_by_name(admin_client):
    """list_pools orders pools by name regardless of insertion order.

    Inserted zeta/alpha/mid so a dropped ORDER BY would surface immediately.
    The pool picker in the job builder renders this list verbatim.
    """
    admin_client.post("/api/pools", json={"name": "zeta"})
    admin_client.post("/api/pools", json={"name": "alpha"})
    admin_client.post("/api/pools", json={"name": "mid"})

    resp = admin_client.get("/api/pools")
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert names == ["alpha", "mid", "zeta"]


def test_list_pools_requires_auth(client):
    """Unauthenticated requests are rejected."""
    resp = client.get("/api/pools")
    assert resp.status_code == 401


# ── create ────────────────────────────────────────────────────────────────


def test_create_pool_returns_201_and_pool_info(admin_client):
    """Creating a pool returns 201 with the persisted pool's info."""
    resp = admin_client.post(
        "/api/pools", json={"name": "gpu-pool", "description": "has gpus"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "gpu-pool"
    assert body["description"] == "has gpus"
    assert body["node_count"] == 0
    # A real UUID id should have been assigned.
    uuid.UUID(body["id"])
    # created_at must be serialized (PoolInfo requires it).
    assert body["created_at"]


def test_create_pool_allowed_for_regular_user(auth_client):
    """Create uses CurrentUser (not AdminUser): a regular user may create.

    Pins the intentional authorization asymmetry with DELETE. If someone
    "tightens" create to AdminUser this test fails, forcing the change to be a
    deliberate product decision rather than a drive-by edit.
    """
    resp = auth_client.post("/api/pools", json={"name": "user-pool"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "user-pool"


def test_create_pool_without_description_defaults_none(admin_client):
    """description is optional and defaults to null."""
    resp = admin_client.post("/api/pools", json={"name": "no-desc"})
    assert resp.status_code == 201
    assert resp.json()["description"] is None


def test_create_pool_missing_name_is_422(admin_client):
    """A body without the required name field is a validation error."""
    resp = admin_client.post("/api/pools", json={"description": "orphan"})
    assert resp.status_code == 422


def test_create_pool_requires_auth(client):
    """Unauthenticated create is rejected."""
    resp = client.post("/api/pools", json={"name": "nope"})
    assert resp.status_code == 401


# ── get ───────────────────────────────────────────────────────────────────


def test_get_pool_returns_pool_and_empty_nodes(admin_client):
    """GET on a fresh pool returns its info plus an empty nodes list."""
    created = admin_client.post("/api/pools", json={"name": "detail-pool"}).json()

    resp = admin_client.get(f"/api/pools/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pool"]["id"] == created["id"]
    assert body["pool"]["name"] == "detail-pool"
    assert body["pool"]["node_count"] == 0
    assert body["nodes"] == []


def test_get_unknown_pool_is_404(admin_client):
    """An unknown pool id yields 404 with the documented detail."""
    resp = admin_client.get(f"/api/pools/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Pool not found"


def test_get_pool_invalid_uuid_is_422(admin_client):
    """A non-UUID path segment fails path validation."""
    resp = admin_client.get("/api/pools/not-a-uuid")
    assert resp.status_code == 422


# ── update (PUT) ───────────────────────────────────────────────────────────


def test_update_pool_changes_name_and_description(admin_client):
    """PUT replaces name and description and echoes the updated PoolInfo."""
    created = admin_client.post(
        "/api/pools", json={"name": "before", "description": "old"}
    ).json()

    resp = admin_client.put(
        f"/api/pools/{created['id']}",
        json={"name": "after", "description": "new"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["name"] == "after"
    assert body["description"] == "new"

    # Change is persisted: a subsequent GET reflects it.
    detail = admin_client.get(f"/api/pools/{created['id']}").json()
    assert detail["pool"]["name"] == "after"
    assert detail["pool"]["description"] == "new"


def test_update_pool_omitting_description_preserves_existing(admin_client):
    """When description is null the route leaves the stored description intact.

    The route only overwrites description when body.description is not None,
    so a rename without a description must not blank out the old value.

    ``PoolCreate`` is reused as the update body and requires ``name`` but not
    ``description``, so a plain "replace all fields" PUT would silently erase
    the description on every rename. This null-means-unchanged behaviour is the
    deliberate workaround.
    """
    created = admin_client.post(
        "/api/pools", json={"name": "keep-desc", "description": "stays"}
    ).json()

    resp = admin_client.put(f"/api/pools/{created['id']}", json={"name": "renamed"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "renamed"
    assert body["description"] == "stays"


def test_update_unknown_pool_is_404(admin_client):
    """Updating a non-existent pool yields 404 with the documented detail."""
    resp = admin_client.put(
        f"/api/pools/{uuid.uuid4()}", json={"name": "ghost"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Pool not found"


def test_update_pool_missing_name_is_422(admin_client):
    """name is required on update too (PoolCreate schema)."""
    created = admin_client.post("/api/pools", json={"name": "needs-name"}).json()
    resp = admin_client.put(
        f"/api/pools/{created['id']}", json={"description": "no name"}
    )
    assert resp.status_code == 422


# ── delete (authorization) ────────────────────────────────────────────────


def test_delete_pool_as_admin_returns_204_and_removes(admin_client):
    """Admin can delete a pool; subsequent GET is 404."""
    created = admin_client.post("/api/pools", json={"name": "doomed"}).json()

    resp = admin_client.delete(f"/api/pools/{created['id']}")
    assert resp.status_code == 204

    assert admin_client.get(f"/api/pools/{created['id']}").status_code == 404


def test_delete_pool_as_regular_user_forbidden(auth_client):
    """delete_pool depends on AdminUser → a regular user gets 403.

    NOTE: ``auth_client`` and ``admin_client`` are the *same* underlying
    TestClient (both built from the ``client`` fixture), so we must not request
    both in one test — the last-applied Authorization header would win. A
    regular user is allowed to create and GET pools, just not delete them.
    """
    created = auth_client.post("/api/pools", json={"name": "protected"}).json()

    resp = auth_client.delete(f"/api/pools/{created['id']}")
    assert resp.status_code == 403
    assert resp.json()["detail"]  # an explanatory detail is present
    # Pool must still exist (delete was blocked before touching the DB).
    assert auth_client.get(f"/api/pools/{created['id']}").status_code == 200


def test_delete_unknown_pool_is_404(admin_client):
    """Deleting a non-existent pool yields 404."""
    resp = admin_client.delete(f"/api/pools/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Pool not found"


# ── node membership ───────────────────────────────────────────────────────


def test_add_empty_node_list_is_noop_201(admin_client):
    """Posting an empty node list adds nothing and returns added: [].

    This exercises the route without tripping the add_node_to_pool bug (the
    loop body never runs), confirming the 201 + empty-added contract.

    Worth keeping even after the bug is fixed: the UI can submit an empty
    selection, and that must be a harmless no-op rather than an error.
    """
    pool = admin_client.post("/api/pools", json={"name": "empty-list"}).json()

    resp = admin_client.post(f"/api/pools/{pool['id']}/nodes", json=[])
    assert resp.status_code == 201
    assert resp.json() == {"added": []}
    # Nothing was added.
    assert admin_client.get(f"/api/pools/{pool['id']}").json()["pool"]["node_count"] == 0


def test_add_validates_node_before_adding(admin_client):
    """A bogus node id 404s before any membership is created.

    The route validates each node via get_node_by_id before calling
    add_node_to_pool, so an unknown node aborts with a node-specific 404 and
    leaves the pool empty. (A single bogus id never reaches the buggy add.)

    Validate-then-mutate ordering matters for the multi-node case: a batch
    containing one bad id must not leave the pool half-populated. With a single
    bogus id here, the ``node_count == 0`` assertion is what proves nothing was
    written before the abort.
    """
    pool = admin_client.post("/api/pools", json={"name": "validate"}).json()
    bogus = uuid.uuid4()

    resp = admin_client.post(f"/api/pools/{pool['id']}/nodes", json=[str(bogus)])
    assert resp.status_code == 404
    assert resp.json()["detail"] == f"Node {bogus} not found"
    # No partial membership was written.
    assert admin_client.get(f"/api/pools/{pool['id']}").json()["pool"]["node_count"] == 0


def test_add_node_requires_auth(client, sample_node):
    """Unauthenticated add is rejected before any DB work."""
    pool_id = uuid.uuid4()
    resp = client.post(f"/api/pools/{pool_id}/nodes", json=[str(sample_node.id)])
    assert resp.status_code == 401


def test_add_node_to_pool_reflects_in_count_and_membership(admin_client, sample_node):
    """Adding a node bumps node_count and lists the node in GET detail."""
    pool = admin_client.post("/api/pools", json={"name": "members"}).json()

    resp = admin_client.post(
        f"/api/pools/{pool['id']}/nodes", json=[str(sample_node.id)]
    )
    assert resp.status_code == 201
    assert resp.json() == {"added": [str(sample_node.id)]}

    detail = admin_client.get(f"/api/pools/{pool['id']}").json()
    assert detail["pool"]["node_count"] == 1
    node_ids = [n["id"] for n in detail["nodes"]]
    assert str(sample_node.id) in node_ids
    # node_count in the list endpoint should match too.
    listed = admin_client.get("/api/pools").json()
    assert listed[0]["node_count"] == 1


def test_add_node_to_unknown_pool_is_404(admin_client, sample_node):
    """Adding to a non-existent pool yields 404 (pool checked first)."""
    resp = admin_client.post(
        f"/api/pools/{uuid.uuid4()}/nodes", json=[str(sample_node.id)]
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Pool not found"


def test_add_unknown_node_is_404(admin_client):
    """Adding a node id that does not exist yields a node-specific 404."""
    pool = admin_client.post("/api/pools", json={"name": "empty-add"}).json()
    bogus = uuid.uuid4()

    resp = admin_client.post(f"/api/pools/{pool['id']}/nodes", json=[str(bogus)])
    assert resp.status_code == 404
    assert resp.json()["detail"] == f"Node {bogus} not found"


def test_remove_node_from_pool_drops_membership(admin_client, sample_node):
    """Removing a node returns 204 and drops node_count back to 0.

    Depends on a successful add first, so it shares the add_node_to_pool bug.
    """
    pool = admin_client.post("/api/pools", json={"name": "removable"}).json()
    admin_client.post(f"/api/pools/{pool['id']}/nodes", json=[str(sample_node.id)])
    assert admin_client.get(f"/api/pools/{pool['id']}").json()["pool"]["node_count"] == 1

    resp = admin_client.delete(f"/api/pools/{pool['id']}/nodes/{sample_node.id}")
    assert resp.status_code == 204

    detail = admin_client.get(f"/api/pools/{pool['id']}").json()
    assert detail["pool"]["node_count"] == 0
    assert detail["nodes"] == []


def test_remove_node_not_in_pool_is_idempotent_204(admin_client, sample_node):
    """Removing a node that was never a member is a no-op 204 (DELETE is idempotent).

    Notably this test is NOT marked with ``_ADD_NODE_BUG``: the remove path
    coerces its ids correctly, so it works even though add does not. That
    asymmetry is itself the evidence the bug is isolated to
    ``add_node_to_pool``.
    """
    pool = admin_client.post("/api/pools", json={"name": "noop-remove"}).json()

    resp = admin_client.delete(f"/api/pools/{pool['id']}/nodes/{sample_node.id}")
    assert resp.status_code == 204
    assert admin_client.get(f"/api/pools/{pool['id']}").json()["pool"]["node_count"] == 0
