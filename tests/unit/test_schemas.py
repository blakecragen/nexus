"""Unit tests for shared Pydantic schemas and enums.

Covers:
- UTCDateTime PlainSerializer (_iso_utc): naive -> UTC ISO with +00:00 offset,
  aware datetimes preserve their own offset.
- Round-trip model_validate / model_dump(mode="json") for key models.
- Schema defaults (StepConfig.on_fail, JobSubmit.priority, etc.).
- Enum wire values matching the JSON strings clients expect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from nexus_common.models.enums import (
    CredentialType,
    JobStatus,
    NodeStatus,
    OSType,
    StepResult,
    StepStatus,
    TransferStatus,
    UserRole,
)
from nexus_common.models.schemas import (
    CredentialCreate,
    JobInfo,
    JobSubmit,
    NodeInfo,
    StepConfig,
    UserInfo,
    _iso_utc,
)


# ── _iso_utc / UTCDateTime serializer ────────────────────────────────────

def test_iso_utc_naive_assumed_utc():
    """A naive datetime is treated as UTC and gets a +00:00 offset."""
    dt = datetime(2026, 6, 30, 12, 0, 0)  # no tzinfo
    out = _iso_utc(dt)
    assert out == "2026-06-30T12:00:00+00:00"
    assert out.endswith("+00:00")


def test_iso_utc_aware_utc_preserved():
    """An aware UTC datetime serializes with +00:00 and unchanged wall time."""
    dt = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    out = _iso_utc(dt)
    assert out == "2026-06-30T12:00:00+00:00"


def test_iso_utc_aware_nonutc_offset_preserved():
    """A non-UTC aware datetime keeps its own offset (not converted to UTC)."""
    tz = timezone(timedelta(hours=-7))  # e.g. PDT
    dt = datetime(2026, 6, 30, 5, 0, 0, tzinfo=tz)
    out = _iso_utc(dt)
    # Offset must be preserved exactly, wall-clock time untouched.
    assert out == "2026-06-30T05:00:00-07:00"
    assert out.endswith("-07:00")


def test_utcdatetime_field_serializes_naive_as_utc_string():
    """Through a model, a naive datetime field becomes a UTC ISO string in JSON."""
    node = NodeInfo(
        id=uuid4(),
        hostname="agent-1",
        os_type=OSType.LINUX,
        os_version="22.04",
        arch="x86_64",
        cpu_model="Xeon",
        cpu_cores=8,
        ram_mb=16384,
        agent_version="1.0.0",
        ip_address="10.0.0.5",
        status=NodeStatus.ONLINE,
        tags=["gpu"],
        registered_at=datetime(2026, 1, 2, 3, 4, 5),  # naive
    )
    data = node.model_dump(mode="json")
    assert isinstance(data["registered_at"], str)
    assert data["registered_at"] == "2026-01-02T03:04:05+00:00"
    assert data["registered_at"].endswith("+00:00")


def test_utcdatetime_optional_none_serializes_none():
    """An optional UTCDateTime field left None stays None (no offset string)."""
    node = NodeInfo(
        id=uuid4(),
        hostname="agent-2",
        os_type=OSType.MACOS,
        os_version="14",
        arch="arm64",
        cpu_model="M3",
        cpu_cores=10,
        ram_mb=32768,
        agent_version="1.0.0",
        ip_address="10.0.0.6",
        status=NodeStatus.OFFLINE,
        tags=[],
        registered_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )
    data = node.model_dump(mode="json")
    assert data["last_heartbeat"] is None


def test_utcdatetime_field_preserves_nonutc_offset_in_json():
    """Through a model, an aware non-UTC datetime keeps its offset (no UTC conversion)."""
    tz = timezone(timedelta(hours=-7))
    node = NodeInfo(
        id=uuid4(),
        hostname="agent-tz",
        os_type=OSType.LINUX,
        os_version="22.04",
        arch="x86_64",
        cpu_model="Xeon",
        cpu_cores=8,
        ram_mb=16384,
        agent_version="1.0.0",
        ip_address="10.0.0.8",
        status=NodeStatus.ONLINE,
        tags=[],
        # Both fields aware non-UTC; the serializer must not shift the wall clock.
        last_heartbeat=datetime(2026, 6, 30, 5, 0, 0, tzinfo=tz),
        registered_at=datetime(2026, 6, 30, 4, 0, 0, tzinfo=tz),
    )
    data = node.model_dump(mode="json")
    assert data["registered_at"] == "2026-06-30T04:00:00-07:00"
    assert data["last_heartbeat"] == "2026-06-30T05:00:00-07:00"


def test_utcdatetime_python_mode_keeps_datetime():
    """when_used='json' means model_dump() (python mode) keeps a datetime, not a str."""
    node = NodeInfo(
        id=uuid4(),
        hostname="agent-3",
        os_type=OSType.WINDOWS,
        os_version="11",
        arch="x86_64",
        cpu_model="Ryzen",
        cpu_cores=16,
        ram_mb=65536,
        agent_version="1.0.0",
        ip_address="10.0.0.7",
        status=NodeStatus.BUSY,
        tags=[],
        registered_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    data = node.model_dump()  # python mode, not json
    assert isinstance(data["registered_at"], datetime)


# ── Enum wire values ─────────────────────────────────────────────────────

def test_enum_string_values_match_wire_format():
    """Enum .value strings are the lowercase tokens clients send/receive."""
    assert JobStatus.RUNNING == "running"
    assert JobStatus.RUNNING.value == "running"
    assert JobStatus.COMPLETED.value == "completed"
    assert NodeStatus.ONLINE.value == "online"
    assert StepStatus.SUCCESS.value == "success"
    assert StepStatus.SKIPPED.value == "skipped"
    assert StepResult.RUNNING.value == "running"
    assert TransferStatus.IN_PROGRESS.value == "in_progress"
    assert OSType.MACOS.value == "macos"
    assert UserRole.ADMIN.value == "admin"
    assert CredentialType.GIT_PAT.value == "git_pat"


def test_enum_str_subclass():
    """Enums are str subclasses so they serialize as their value in JSON."""
    assert isinstance(JobStatus.FAILED, str)
    assert isinstance(OSType.LINUX, str)


def test_enum_round_trip_from_string():
    """Constructing an enum from its wire string yields the member."""
    assert JobStatus("queued") is JobStatus.QUEUED
    assert NodeStatus("maintenance") is NodeStatus.MAINTENANCE
    assert CredentialType("s3") is CredentialType.S3


def test_enum_invalid_string_raises():
    """An unknown wire string is rejected."""
    with pytest.raises(ValueError):
        JobStatus("not-a-status")


# ── StepConfig defaults ──────────────────────────────────────────────────

def test_stepconfig_defaults():
    """StepConfig with only `step` set gets empty params and on_fail='stop'."""
    sc = StepConfig(step="git_clone")
    assert sc.step == "git_clone"
    assert sc.params == {}
    assert sc.on_fail == "stop"
    assert sc.target_node_id is None
    assert sc.target_pool_id is None
    assert sc.target_os is None


def test_stepconfig_params_are_independent_instances():
    """The default-factory dict is not shared across instances."""
    a = StepConfig(step="a")
    b = StepConfig(step="b")
    a.params["x"] = 1
    assert b.params == {}


def test_stepconfig_round_trip():
    """StepConfig survives model_dump(json) -> model_validate unchanged."""
    node_id = uuid4()
    sc = StepConfig(
        step="run_cmd",
        params={"cmd": "echo hi"},
        on_fail="continue",
        target_node_id=node_id,
        target_os="linux",
    )
    dumped = sc.model_dump(mode="json")
    assert dumped["target_node_id"] == str(node_id)  # UUID -> str in json mode
    assert dumped["on_fail"] == "continue"
    restored = StepConfig.model_validate(dumped)
    assert restored == sc


# ── JobSubmit defaults ───────────────────────────────────────────────────

def test_jobsubmit_defaults():
    """JobSubmit defaults: priority=1 (normal), no targets, no storage override."""
    js = JobSubmit(name="nightly", steps=[StepConfig(step="git_clone")])
    assert js.priority == 1
    assert js.target_pool_id is None
    assert js.target_node_id is None
    assert js.storage_target is None
    assert len(js.steps) == 1
    assert isinstance(js.steps[0], StepConfig)


def test_jobsubmit_parses_nested_step_dicts():
    """Nested step dicts are coerced into StepConfig objects."""
    js = JobSubmit.model_validate(
        {
            "name": "build",
            "steps": [{"step": "git_clone", "params": {"url": "x"}}],
            "priority": 0,
        }
    )
    assert js.priority == 0
    assert isinstance(js.steps[0], StepConfig)
    assert js.steps[0].on_fail == "stop"  # default applied to nested dict


def test_jobsubmit_round_trip():
    """JobSubmit with multiple steps survives model_dump(json) -> model_validate.

    This is exactly the path a submitted job takes (API JSON body -> model -> DB
    JSON -> model), so equality after the round trip guards the whole pipeline.
    """
    js = JobSubmit(
        name="job",
        steps=[StepConfig(step="s1"), StepConfig(step="s2", on_fail="continue")],
        priority=2,
    )
    dumped = js.model_dump(mode="json")
    restored = JobSubmit.model_validate(dumped)
    assert restored == js


# ── UserInfo round-trip ──────────────────────────────────────────────────

def test_userinfo_round_trip_and_enum_serialization():
    """UserInfo serializes UUID -> str and UserRole -> its wire string, and parses back.

    The frontend reads ``role`` as a plain string ('manager'), so an enum leaking
    as 'UserRole.MANAGER' would break role checks in the UI.
    """
    uid = uuid4()
    u = UserInfo(id=uid, username="alice", email="a@b.com", role=UserRole.MANAGER, is_active=True)
    dumped = u.model_dump(mode="json")
    assert dumped["id"] == str(uid)
    assert dumped["role"] == "manager"  # enum serialized to wire string
    assert dumped["email"] == "a@b.com"
    restored = UserInfo.model_validate(dumped)
    assert restored == u
    assert isinstance(restored.id, UUID)
    assert restored.role is UserRole.MANAGER


def test_userinfo_optional_email_defaults_none():
    """email is optional and serializes as JSON null when unset.

    The key must still be present in the payload (not omitted) so the frontend can
    rely on a stable object shape.
    """
    u = UserInfo(id=uuid4(), username="bob", role=UserRole.USER, is_active=False)
    assert u.email is None
    assert u.model_dump(mode="json")["email"] is None


# ── JobInfo round-trip with datetimes ────────────────────────────────────

def test_jobinfo_round_trip_with_datetimes():
    """JobInfo serializes status enum + UTCDateTime fields, and round-trips."""
    jid, uid = uuid4(), uuid4()
    job = JobInfo(
        id=jid,
        name="my-job",
        submitted_by=uid,
        priority=1,
        status=JobStatus.RUNNING,
        current_step=2,
        created_at=datetime(2026, 6, 30, 8, 0, 0),  # naive
        started_at=datetime(2026, 6, 30, 8, 0, 5, tzinfo=timezone.utc),  # aware
    )
    dumped = job.model_dump(mode="json")
    assert dumped["status"] == "running"
    assert dumped["created_at"] == "2026-06-30T08:00:00+00:00"
    assert dumped["started_at"] == "2026-06-30T08:00:05+00:00"
    assert dumped["completed_at"] is None
    assert dumped["error"] is None
    # Round-trip: ISO strings parse back into datetimes / UUIDs / enum.
    restored = JobInfo.model_validate(dumped)
    assert restored.status is JobStatus.RUNNING
    assert restored.id == jid
    assert restored.created_at.tzinfo is not None  # parsed back as aware
    # The naive created_at was assumed UTC, so the parsed instant matches that UTC time.
    assert restored.created_at == datetime(2026, 6, 30, 8, 0, 0, tzinfo=timezone.utc)
    # The aware started_at instant is preserved exactly through the round-trip.
    assert restored.started_at == datetime(2026, 6, 30, 8, 0, 5, tzinfo=timezone.utc)


def test_nodeinfo_round_trip_with_enums_and_datetimes():
    """NodeInfo (two enums + naive registered_at + None heartbeat) round-trips."""
    nid = uuid4()
    node = NodeInfo(
        id=nid,
        hostname="agent-rt",
        os_type=OSType.LINUX,
        os_version="22.04",
        arch="x86_64",
        cpu_model="Xeon",
        cpu_cores=8,
        ram_mb=16384,
        agent_version="1.0.0",
        ip_address="10.0.0.9",
        status=NodeStatus.ONLINE,
        tags=["gpu", "fast"],
        registered_at=datetime(2026, 1, 2, 3, 4, 5),  # naive -> assumed UTC
    )
    dumped = node.model_dump(mode="json")
    assert dumped["id"] == str(nid)
    assert dumped["os_type"] == "linux"
    assert dumped["status"] == "online"
    assert dumped["registered_at"] == "2026-01-02T03:04:05+00:00"
    assert dumped["last_heartbeat"] is None
    restored = NodeInfo.model_validate(dumped)
    assert restored.id == nid
    assert restored.os_type is OSType.LINUX
    assert restored.status is NodeStatus.ONLINE
    assert restored.tags == ["gpu", "fast"]
    assert restored.last_heartbeat is None
    assert restored.registered_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_jobinfo_requires_status():
    """status has no default -> omitting it is a validation error."""
    with pytest.raises(ValueError):
        JobInfo(
            id=uuid4(),
            name="x",
            submitted_by=uuid4(),
            priority=1,
            current_step=0,
            created_at=datetime(2026, 1, 1),
        )


# ── CredentialCreate defaults / round-trip ───────────────────────────────

def test_credentialcreate_defaults():
    """CredentialCreate defaults to private (is_shared False) with no group grants.

    Security-relevant default: a credential created without explicit sharing must
    not become visible to other users.
    """
    cc = CredentialCreate(
        name="my-s3",
        credential_type=CredentialType.S3,
        fields={"access_key": "AK", "secret_key": "SK"},
    )
    assert cc.is_shared is False
    assert cc.allowed_groups == []
    assert cc.description is None
    assert cc.credential_type is CredentialType.S3


def test_credentialcreate_round_trip():
    """A shared credential with group grants survives the JSON round trip.

    allowed_groups is a list of UUIDs; json mode must stringify them and
    model_validate must parse them back into UUID objects for DB comparison.
    """
    gid = uuid4()
    cc = CredentialCreate(
        name="shared-pat",
        credential_type=CredentialType.GIT_PAT,
        fields={"token": "ghp_xxx"},
        description="org token",
        is_shared=True,
        allowed_groups=[gid],
    )
    dumped = cc.model_dump(mode="json")
    assert dumped["credential_type"] == "git_pat"
    assert dumped["is_shared"] is True
    assert dumped["allowed_groups"] == [str(gid)]
    restored = CredentialCreate.model_validate(dumped)
    assert restored == cc
    assert restored.allowed_groups[0] == gid
