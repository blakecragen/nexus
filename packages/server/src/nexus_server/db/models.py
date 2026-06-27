"""SQLAlchemy ORM models for the Nexus database."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ── Users & Groups ──────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    api_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    group_memberships: Mapped[list[UserGroupMembership]] = relationship(back_populates="user")
    jobs: Mapped[list[Job]] = relationship(back_populates="submitted_by_user")


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    memberships: Mapped[list[UserGroupMembership]] = relationship(back_populates="group")
    pool_access: Mapped[list[GroupPoolAccess]] = relationship(back_populates="group")


class UserGroupMembership(Base):
    __tablename__ = "user_group_memberships"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("groups.id"), primary_key=True
    )
    role_in_group: Mapped[str] = mapped_column(String(16), default="member")

    user: Mapped[User] = relationship(back_populates="group_memberships")
    group: Mapped[Group] = relationship(back_populates="memberships")


# ── Nodes & Pools ───────────────────────────────────────────────────────


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    os_type: Mapped[str] = mapped_column(String(16), nullable=False)
    os_version: Mapped[str | None] = mapped_column(String(64))
    arch: Mapped[str | None] = mapped_column(String(32))
    cpu_model: Mapped[str | None] = mapped_column(String(128))
    cpu_cores: Mapped[int | None] = mapped_column(Integer)
    ram_mb: Mapped[int | None] = mapped_column(Integer)
    gpu_info: Mapped[str | None] = mapped_column(String(255))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    status: Mapped[str] = mapped_column(String(16), default="offline")
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    api_key: Mapped[str | None] = mapped_column(String(64), unique=True)

    pool_memberships: Mapped[list[PoolNodeMembership]] = relationship(back_populates="node")


class Pool(Base):
    __tablename__ = "pools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    node_memberships: Mapped[list[PoolNodeMembership]] = relationship(back_populates="pool")
    group_access: Mapped[list[GroupPoolAccess]] = relationship(back_populates="pool")


class PoolNodeMembership(Base):
    __tablename__ = "pool_node_memberships"

    pool_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pools.id"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("nodes.id"), primary_key=True
    )

    pool: Mapped[Pool] = relationship(back_populates="node_memberships")
    node: Mapped[Node] = relationship(back_populates="pool_memberships")


class GroupPoolAccess(Base):
    __tablename__ = "group_pool_access"

    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("groups.id"), primary_key=True
    )
    pool_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pools.id"), primary_key=True
    )
    permission: Mapped[str] = mapped_column(String(16), default="submit")

    group: Mapped[Group] = relationship(back_populates="pool_access")
    pool: Mapped[Pool] = relationship(back_populates="group_access")


# ── Credentials ─────────────────────────────────────────────────────────


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False)
    encrypted_fields: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_groups: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Storage ─────────────────────────────────────────────────────────────


class StorageBackend(Base):
    __tablename__ = "storage_backends"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    backend_type: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("credentials.id")
    )
    capacity_bytes: Mapped[int | None] = mapped_column(BigInteger)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    artifacts: Mapped[list[Artifact]] = relationship(back_populates="storage_backend")


# ── Jobs & Steps ────────────────────────────────────────────────────────


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    target_pool_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pools.id")
    )
    target_node_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("nodes.id")
    )
    priority: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    steps_config: Mapped[list] = mapped_column(JSON, nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    context_data: Mapped[dict] = mapped_column(JSON, default=dict)
    storage_target: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    submitted_by_user: Mapped[User] = relationship(back_populates="jobs")
    step_runs: Mapped[list[StepRun]] = relationship(back_populates="job", order_by="StepRun.step_index")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="job")


class StepRun(Base):
    __tablename__ = "step_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"))
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    node_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("nodes.id"))
    input_params: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[dict | None] = mapped_column(JSON)
    output_params: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[Job] = relationship(back_populates="step_runs")


# ── Artifacts ───────────────────────────────────────────────────────────


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"))
    step_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("step_runs.id")
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_backend_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("storage_backends.id")
    )
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="artifacts")
    storage_backend: Mapped[StorageBackend] = relationship(back_populates="artifacts")


# ── Storage Transfers ───────────────────────────────────────────────────


class StorageTransfer(Base):
    __tablename__ = "storage_transfers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    artifact_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifacts.id"))
    source_backend_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("storage_backends.id")
    )
    dest_backend_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("storage_backends.id")
    )
    status: Mapped[str] = mapped_column(String(16), default="pending")
    bytes_transferred: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id")
    )


# ── Saved Templates ────────────────────────────────────────────────────


class SavedTemplate(Base):
    __tablename__ = "saved_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    steps_config: Mapped[list] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ── Audit Log ───────────────────────────────────────────────────────────


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict | None] = mapped_column(JSON)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
