"""Enumerations shared across Nexus packages."""

from __future__ import annotations

from enum import Enum


class OSType(str, Enum):
    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"


class UserRole(str, Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    USER = "user"


class GroupRole(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"


class PoolPermission(str, Enum):
    SUBMIT = "submit"
    MANAGE = "manage"


class NodeStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    MAINTENANCE = "maintenance"


class JobStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class StepResult(str, Enum):
    """Result returned by FlowStep.check()."""
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class TransferStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class CredentialType(str, Enum):
    S3 = "s3"
    GDRIVE = "gdrive"
    GIT_PAT = "git_pat"
    GIT_SSH = "git_ssh"
    SMB = "smb"
    BASIC = "basic"
