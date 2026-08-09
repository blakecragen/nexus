"""Enumerations shared across Nexus packages.

Central vocabulary for every status, role, and type string in the system. The DB
models (``nexus_server.db.models``), the API schemas
(``nexus_common.models.schemas``), the scheduler, the agent, and the React
frontend all key off these exact strings.

Every enum here subclasses ``str``, which is load-bearing rather than stylistic:

    - SQLAlchemy columns store the plain string value, so a row written by one
      package reads back identically in another.
    - Pydantic serializes members to their value in JSON, so the frontend's
      TypeScript unions can be written as the same literals.
    - Comparisons against bare strings (``node.status == "online"``) work
      throughout the codebase, and much of the server does exactly that instead
      of importing the enum.

AI Note: The member *values* are persisted data and a wire format. Renaming a
value orphans every existing DB row and breaks the frontend's hardcoded status
strings at the same time; there is no migration or translation layer. Adding new
members is safe as long as consumers treat unknown values as a display-only
fallback. The member *names* are internal and safe to change (though pointless).
"""

from __future__ import annotations

from enum import Enum


class OSType(str, Enum):
    """Operating-system family of a node.

    Drives two things: which nodes are eligible for a step (``FlowStep.SUPPORTED_OS``
    and a step's ``target_os``), and which branch of ``FlowStep.OS_VARIANTS`` supplies
    default params before dispatch. The agent detects its own value at startup and
    reports it in ``AgentRegister.os_type``.

    AI Note: These values are matched by string against ``OS_VARIANTS`` keys and
    ``SUPPORTED_OS`` entries, which are declared as bare strings in step classes.
    A new OS member is inert until steps opt into it.
    """
    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"


class UserRole(str, Enum):
    """Global authorization level of a user account.

    Checked by the API dependencies in ``nexus_server.api.deps`` to gate
    administrative routes. Distinct from ``GroupRole``, which is scoped to a
    single group.

    Members:
        ADMIN: Full control, including user and node administration.
        MANAGER: Elevated operational rights below full admin.
        USER: Default role for a newly registered account.
    """
    ADMIN = "admin"
    MANAGER = "manager"
    USER = "user"


class GroupRole(str, Enum):
    """A user's role *within* a single group (``UserGroupMembership.role``).

    Orthogonal to ``UserRole``: a global USER can be the ADMIN of a group, and a
    global ADMIN needs no group membership to act.

    Members:
        ADMIN: May manage the group's membership and its pool grants.
        MEMBER: Inherits the group's pool access without being able to change it.
    """
    ADMIN = "admin"
    MEMBER = "member"


class PoolPermission(str, Enum):
    """What a group is allowed to do with a pool (``GroupPoolAccess.permission``).

    Members:
        SUBMIT: May target jobs at the pool.
        MANAGE: May also change the pool itself (membership, settings).

    AI Note: MANAGE is intended to imply SUBMIT, but that is not encoded here —
    nothing in this enum expresses an ordering. Any permission check must treat
    MANAGE as satisfying a SUBMIT requirement explicitly.
    """
    SUBMIT = "submit"
    MANAGE = "manage"


class NodeStatus(str, Enum):
    """Lifecycle state of a compute node.

    Set by the WebSocket layer (ONLINE on register/heartbeat, OFFLINE on
    disconnect or missed-heartbeat sweep) and by operators (MAINTENANCE). The
    scheduler in ``nexus_server.runner`` only places work on ONLINE nodes.

    Members:
        ONLINE: Agent socket is connected and heartbeating; eligible for work.
        OFFLINE: No live agent — set on disconnect or when heartbeats lapse.
        BUSY: Node is saturated. Advisory; the current scheduler does not use it
            as a hard gate, so treat it as a display state.
        MAINTENANCE: Operator has parked the node; must not receive new work.
    """
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    MAINTENANCE = "maintenance"


class JobStatus(str, Enum):
    """Lifecycle state of a whole job.

    Normal path: PENDING -> QUEUED -> RUNNING -> COMPLETED. A job may leave
    RUNNING for FAILED (a step failed with ``on_fail="stop"``) or CANCELLED
    (operator action). COMPLETED / FAILED / CANCELLED are terminal and stamp
    ``completed_at``.

    Members:
        PENDING: Row created, not yet handed to the runner.
        QUEUED: Accepted by the runner, waiting for a suitable node.
        RUNNING: At least one step has been dispatched.
        COMPLETED: Every step finished without a fatal failure.
        FAILED: A step failed fatally, or the runner itself errored.
        CANCELLED: Terminated by request rather than by outcome.

    AI Note: COMPLETED does not mean every step succeeded — steps configured with
    ``on_fail="continue"`` can fail while the job still completes. Per-step truth
    lives in ``StepStatus`` on the StepRun rows.
    """
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    """Persisted state of one ``StepRun`` row (one *attempt* at one step).

    Note this is the storage-level status and is deliberately not the same type
    as ``StepResult``, which is the in-memory value ``FlowStep.check()`` returns.
    SUCCESS/FAILED overlap; the rest do not.

    Members:
        PENDING: Row created, not yet dispatched.
        RUNNING: Agent reported ``step.started`` (or a local step began).
        SUCCESS: Terminal — outputs were merged into the job context.
        FAILED: Terminal — see the row's ``error`` field.
        CANCELLED: Terminal — cancelled before finishing.
        SKIPPED: Terminal — never executed (e.g. jumped over by a flow step).

    AI Note: (job_id, step_index) is *not* unique across StepRun rows. Loop and
    jump steps re-enter the same index, and the runner creates a fresh row per
    attempt, so anything resolving a step must take the latest row for that index.
    """
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class StepResult(str, Enum):
    """Result returned by FlowStep.check().

    The polling contract between a step implementation and its executor (the
    agent's ``StepExecutor`` for remote steps, ``JobRunner._execute_local_step``
    for control-plane ones): both loop on ``check(state)`` and only stop on
    SUCCESS or FAILED.

    Members:
        RUNNING: Not finished — the executor sleeps and polls again.
        SUCCESS: Finished; the executor harvests ``OUTPUT_KEYS`` from the state dict.
        FAILED: Finished badly; the executor reads ``state["error"]`` for the reason.

    AI Note: There is no timeout member. A ``check()`` that never stops returning
    RUNNING pins the step forever, so step implementations must enforce their own
    deadlines and flip themselves to FAILED.
    """
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class TransferStatus(str, Enum):
    """State of a ``StorageTransfer`` — an artifact copy between storage backends.

    Members:
        PENDING: Requested, not started.
        IN_PROGRESS: Bytes are moving; ``bytes_transferred`` advances.
        COMPLETED: All bytes landed at the destination. Only after this may a
            ``delete_source`` transfer drop the original.
        FAILED: Aborted; the source is left untouched.
    """
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class CredentialType(str, Enum):
    """Kind of secret a stored credential holds.

    Selects which strategy in ``nexus_server.credentials`` validates the field
    set, how it is encrypted at rest, and what shape gets handed to a step as
    ``ExecuteStepCommand.credential_config``.

    Members:
        S3: S3/MinIO-compatible object storage keys.
        GDRIVE: Google Drive service-account credentials.
        GIT_PAT: Git personal access token (HTTPS auth).
        GIT_SSH: Git SSH private key.
        SMB: Windows/CIFS share credentials.
        BASIC: Generic username/password pair.

    AI Note: Each member must have a matching strategy registered server-side;
    adding a value here without one makes credentials of that type unusable at
    creation time rather than at use time.
    """
    S3 = "s3"
    GDRIVE = "gdrive"
    GIT_PAT = "git_pat"
    GIT_SSH = "git_ssh"
    SMB = "smb"
    BASIC = "basic"
