"""Pydantic schemas shared by the Nexus API, agents, and CLI."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from nexus_common.models.enums import (
    CredentialType,
    JobStatus,
    NodeStatus,
    OSType,
    StepStatus,
    TransferStatus,
    UserRole,
)


# ── Auth ────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserInfo(BaseModel):
    id: UUID
    username: str
    email: str | None = None
    role: UserRole
    is_active: bool


# ── Nodes ───────────────────────────────────────────────────────────────

class NodeRegistration(BaseModel):
    hostname: str
    display_name: str | None = None
    os_type: OSType
    os_version: str
    arch: str
    cpu_model: str
    cpu_cores: int
    ram_mb: int
    gpu_info: str | None = None
    agent_version: str
    ip_address: str
    capabilities: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class NodeInfo(BaseModel):
    id: UUID
    hostname: str
    display_name: str | None = None
    os_type: OSType
    os_version: str
    arch: str
    cpu_model: str
    cpu_cores: int
    ram_mb: int
    gpu_info: str | None = None
    agent_version: str
    ip_address: str
    status: NodeStatus
    capabilities: dict
    tags: list[str]
    last_heartbeat: datetime | None = None
    registered_at: datetime


# ── Pools ───────────────────────────────────────────────────────────────

class PoolCreate(BaseModel):
    name: str
    description: str | None = None


class PoolInfo(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    node_count: int = 0
    created_at: datetime


# ── Jobs ────────────────────────────────────────────────────────────────

class StepConfig(BaseModel):
    step: str
    params: dict = Field(default_factory=dict)
    on_fail: str = "stop"  # "stop" or "continue"
    target_node_id: UUID | None = None  # pin this step to a specific node
    target_pool_id: UUID | None = None  # restrict scheduling to a specific pool
    target_os: str | None = None  # require a specific OS family (macos / linux / windows)


class JobSubmit(BaseModel):
    name: str
    steps: list[StepConfig]
    target_pool_id: UUID | None = None
    target_node_id: UUID | None = None
    priority: int = 1  # 0=high, 1=normal, 2=low
    storage_target: str | None = None  # override default storage backend


class JobInfo(BaseModel):
    id: UUID
    name: str
    submitted_by: UUID
    target_pool_id: UUID | None = None
    target_node_id: UUID | None = None
    priority: int
    status: JobStatus
    current_step: int
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class StepRunInfo(BaseModel):
    id: UUID
    job_id: UUID
    step_index: int
    step_name: str
    status: StepStatus
    node_id: UUID | None = None
    input_params: dict | None = None
    output_params: dict | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobDetail(BaseModel):
    job: JobInfo
    steps: list[StepRunInfo]
    context_data: dict = Field(default_factory=dict)


# ── Steps Schema ────────────────────────────────────────────────────────

class FieldSchema(BaseModel):
    name: str
    required: bool
    description: str | None = None
    default: object = None
    examples: list[str] = Field(default_factory=list)
    field_type: str = "string"


class InputRuleSchema(BaseModel):
    rule_type: str
    fields: list[str]
    description: str | None = None


class StepSchemaInfo(BaseModel):
    name: str
    description: str
    requires_node: bool
    supported_os: list[str]
    required_capabilities: list[str]
    output_keys: list[str]
    fields: list[FieldSchema]
    rules: list[InputRuleSchema]
    os_variants: dict[str, dict] = Field(default_factory=dict)


# ── Credentials ─────────────────────────────────────────────────────────

class CredentialCreate(BaseModel):
    name: str
    credential_type: CredentialType
    fields: dict  # raw fields (encrypted before storage)
    description: str | None = None
    is_shared: bool = False
    allowed_groups: list[UUID] = Field(default_factory=list)


class CredentialInfo(BaseModel):
    id: UUID
    name: str
    credential_type: CredentialType
    description: str | None = None
    is_shared: bool
    owner_id: UUID
    created_at: datetime
    updated_at: datetime | None = None


class CredentialTypeInfo(BaseModel):
    credential_type: CredentialType
    required_fields: list[str]
    optional_fields: list[str] = Field(default_factory=list)
    description: str


# ── Storage ─────────────────────────────────────────────────────────────

class StorageBackendCreate(BaseModel):
    name: str
    backend_type: str  # "minio", "gdrive", "nas", "s3"
    config: dict  # backend-specific config
    credential_id: UUID
    capacity_bytes: int | None = None
    is_default: bool = False
    priority: int = 10


class StorageBackendInfo(BaseModel):
    id: UUID
    name: str
    backend_type: str
    config: dict
    credential_id: UUID
    capacity_bytes: int | None = None
    used_bytes: int = 0
    is_default: bool
    is_active: bool
    priority: int
    created_at: datetime


class TransferRequest(BaseModel):
    artifact_id: UUID
    dest_backend_id: UUID
    delete_source: bool = False


class TransferInfo(BaseModel):
    id: UUID
    artifact_id: UUID
    source_backend_id: UUID
    dest_backend_id: UUID
    status: TransferStatus
    bytes_transferred: int = 0
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


# ── Artifacts ───────────────────────────────────────────────────────────

class ArtifactInfo(BaseModel):
    id: UUID
    job_id: UUID
    step_run_id: UUID | None = None
    filename: str
    storage_backend_id: UUID
    storage_backend_name: str | None = None
    storage_key: str
    content_type: str | None = None
    size_bytes: int
    created_at: datetime


# ── Templates ───────────────────────────────────────────────────────────

class TemplateCreate(BaseModel):
    name: str
    description: str | None = None
    steps: list[StepConfig]


class TemplateInfo(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    steps: list[StepConfig]
    created_by: UUID
    created_at: datetime
