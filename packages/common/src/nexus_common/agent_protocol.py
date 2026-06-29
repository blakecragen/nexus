"""WebSocket protocol messages between the Nexus server and agents.

All messages are JSON-encoded Pydantic models. The `type` field discriminates
message types on both sides.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Agent → Server Messages ─────────────────────────────────────────────


class AgentRegister(BaseModel):
    """Sent once on connection to identify the agent."""
    type: Literal["register"] = "register"
    node_id: str
    hostname: str
    os_type: str
    os_version: str
    arch: str
    cpu_model: str
    cpu_cores: int
    ram_mb: int
    gpu_info: str | None = None
    agent_version: str
    ip_address: str
    tags: list[str] = Field(default_factory=list)


class AgentHeartbeat(BaseModel):
    """Sent periodically to signal liveness."""
    type: Literal["heartbeat"] = "heartbeat"
    node_id: str
    timestamp: datetime
    load_avg: float | None = None
    memory_used_pct: float | None = None
    active_steps: int = 0


class StepStarted(BaseModel):
    """Agent confirms step execution has begun."""
    type: Literal["step.started"] = "step.started"
    job_id: str
    step_index: int
    state: dict[str, Any]  # startup() return value — persisted for crash recovery


class StepLog(BaseModel):
    """Streaming stdout/stderr from step execution."""
    type: Literal["step.log"] = "step.log"
    job_id: str
    step_index: int
    stream: Literal["stdout", "stderr"]
    line: str
    timestamp: datetime


class StepProgress(BaseModel):
    """Optional progress update from a step."""
    type: Literal["step.progress"] = "step.progress"
    job_id: str
    step_index: int
    percent: float
    message: str = ""


class StepCompleted(BaseModel):
    """Step finished successfully."""
    type: Literal["step.completed"] = "step.completed"
    job_id: str
    step_index: int
    outputs: dict[str, Any]  # merged into job context
    # Captured terminal output for the per-job log (optional; agent fills these).
    command: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None


class StepFailed(BaseModel):
    """Step execution failed."""
    type: Literal["step.failed"] = "step.failed"
    job_id: str
    step_index: int
    error: str
    exit_code: int | None = None
    # Captured terminal output for the per-job log (optional; agent fills these).
    command: str | None = None
    stdout: str | None = None
    stderr: str | None = None


# ── Server → Agent Messages ─────────────────────────────────────────────


class ExecuteStepCommand(BaseModel):
    """Server instructs agent to execute a step."""
    type: Literal["execute_step"] = "execute_step"
    job_id: str
    step_index: int
    step_name: str
    params: dict[str, Any]  # already resolved (context merged + OS variants applied)
    artifacts: list[str] = Field(default_factory=list)  # S3 keys to pre-fetch
    credential_config: dict[str, Any] | None = None  # decrypted credential for this step


class CancelStepCommand(BaseModel):
    """Server instructs agent to cancel a running step."""
    type: Literal["cancel_step"] = "cancel_step"
    job_id: str
    step_index: int


class ServerAck(BaseModel):
    """Server acknowledges agent registration or heartbeat."""
    type: Literal["ack"] = "ack"
    message: str = "ok"


# ── Dashboard → Server / Server → Dashboard Messages ────────────────────


class DashboardNodeStatus(BaseModel):
    """Broadcast to dashboard WebSocket clients on node status change."""
    type: Literal["node.status"] = "node.status"
    node_id: str
    status: str
    hostname: str | None = None
    last_heartbeat: datetime | None = None


class DashboardJobStatus(BaseModel):
    """Broadcast to dashboard on job status change."""
    type: Literal["job.status"] = "job.status"
    job_id: str
    status: str
    current_step: int = 0
    step_name: str | None = None


class DashboardStepLog(BaseModel):
    """Broadcast to dashboard for live log streaming."""
    type: Literal["step.log"] = "step.log"
    job_id: str
    step_index: int
    stream: str
    line: str


class DashboardJobCompleted(BaseModel):
    """Broadcast when a job reaches terminal state."""
    type: Literal["job.completed"] = "job.completed"
    job_id: str
    status: str
    completed_at: datetime | None = None


# ── Type unions for message parsing ──────────────────────────────────────

AgentMessage = (
    AgentRegister | AgentHeartbeat |
    StepStarted | StepLog | StepProgress | StepCompleted | StepFailed
)

ServerCommand = ExecuteStepCommand | CancelStepCommand | ServerAck

DashboardEvent = (
    DashboardNodeStatus | DashboardJobStatus |
    DashboardStepLog | DashboardJobCompleted
)
