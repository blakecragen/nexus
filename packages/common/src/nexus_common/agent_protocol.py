"""WebSocket protocol messages between the Nexus server and agents.

All messages are JSON-encoded Pydantic models. The `type` field discriminates
message types on both sides.

Role in the system
------------------
This module is the *only* definition of the agent<->server wire format. Both
ends import it from ``nexus-common``:

    - Server side: ``nexus_server.api.routes.ws`` accepts the agent socket,
      parses inbound frames with these models in ``_handle_agent_message()``,
      and emits ``ExecuteStepCommand`` / ``CancelStepCommand`` from
      ``nexus_server.runner.runner``.
    - Agent side: ``nexus_agent.connection.AgentConnection`` sends
      ``AgentRegister`` / ``AgentHeartbeat`` and forwards server commands to
      ``nexus_agent.executor.StepExecutor``, which emits the ``step.*`` family.
    - Dashboard side: the server fans ``Dashboard*`` events out to browser
      WebSocket clients; the React app in ``frontend/src/hooks/useWebSocket``
      switches on the same ``type`` strings.

Conventions
-----------
    - Every model carries a ``Literal`` ``type`` field with a default, so
      ``Model().model_dump(mode="json")`` is a complete, self-describing frame.
    - IDs cross the wire as ``str``, never ``UUID``. The agent has no DB and
      SQLite bindings choke on ``UUID`` objects, so both ends stringify at the
      boundary and convert on the server only when touching the DB.
    - Send frames as ``model_dump(mode="json")`` dicts, not ``model_dump_json()``
      strings: ``WebSocket.send_json`` encodes once, so passing a pre-encoded
      string delivers a quoted JSON string that the peer cannot ``.get()`` into.

AI Note: These models are a versionless ABI. There is no handshake that
negotiates protocol versions, so adding a *required* field to any message
immediately breaks every already-deployed agent (and, for server->agent
messages, every older server). Add new fields as optional with defaults, and
bump ``nexus_common.__version__`` so ``agent_version`` on the Node row reveals
the skew.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Agent → Server Messages ─────────────────────────────────────────────


class AgentRegister(BaseModel):
    """Sent once on connection to identify the agent.

    First frame on every (re)connect. The server treats this as an upsert of the
    hardware/OS inventory for an already-provisioned node: ``ws._handle_agent_message``
    writes every field onto the ``Node`` row and replies with ``ServerAck(message=
    "registered")``. It does *not* create nodes — provisioning happens over HTTP
    (``POST /api/nodes``), and the resulting node id + API key are what authorize
    the socket in the first place.

    Attributes:
        node_id: Server-assigned node UUID (as a string) from provisioning. Must
            match the id embedded in the WebSocket URL or the server rejects it.
        hostname: Reported host name; the dashboard's primary label for the node.
        os_type: "macos" / "linux" / "windows" — drives ``FlowStep.OS_VARIANTS``
            resolution and OS-targeted scheduling.
        os_version, arch, cpu_model, cpu_cores, ram_mb, gpu_info: Static
            inventory shown in the UI; refreshed on each reconnect.
        agent_version: ``nexus_common.__version__`` of the agent build, used to
            spot protocol skew after a partial rollout.
        ip_address: Address the agent believes it has; informational only, the
            server never dials back to it (the agent always initiates).
        tags: Free-form labels. Purely descriptive today — node *capabilities*
            were removed from the scheduler, so tags do not gate placement.
    """
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
    """Sent periodically to signal liveness.

    The server stamps ``Node.last_heartbeat`` and forces ``status="online"`` on
    receipt, then replies ``ServerAck(message="heartbeat_ok")``. A node that stops
    heartbeating is swept to "offline" by the server's liveness check, which makes
    it ineligible for scheduling.

    Attributes:
        node_id: Node UUID string; must match the socket's node.
        timestamp: Agent-local send time. Informational — the server uses its own
            ``datetime.now(timezone.utc)`` for ``last_heartbeat`` so clock skew on
            the agent cannot make a node look permanently fresh or stale.
        load_avg: 1-minute load average, or None if the platform can't report it.
        memory_used_pct: Percent RAM in use, or None if unavailable.
        active_steps: How many steps this agent is currently running. Lets the
            dashboard show real utilization.
    """
    type: Literal["heartbeat"] = "heartbeat"
    node_id: str
    timestamp: datetime
    load_avg: float | None = None
    memory_used_pct: float | None = None
    active_steps: int = 0


class StepStarted(BaseModel):
    """Agent confirms step execution has begun.

    Sent immediately after ``FlowStep.startup()`` returns, before any polling or
    subprocess supervision. The server writes ``state`` onto the newest
    ``StepRun`` row for this (job_id, step_index) and marks it "running".

    Attributes:
        job_id: Job UUID string.
        step_index: Position in the job's step list. Not unique on its own — loop
            /jump steps can revisit the same index, producing several StepRun rows,
            so the server resolves against the *latest* run for that index.
        state: The dict returned by ``startup()``. Persisted verbatim for crash
            recovery: if the server restarts mid-step it can resume by calling
            ``check(state)`` without re-running ``startup()``. Must therefore be
            JSON-serializable and contain everything ``check()`` needs.
    """
    type: Literal["step.started"] = "step.started"
    job_id: str
    step_index: int
    state: dict[str, Any]  # startup() return value — persisted for crash recovery


class StepLog(BaseModel):
    """Streaming stdout/stderr from step execution.

    Emitted line-by-line while a subprocess-backed step runs. The server does not
    persist these individually (the full transcript arrives with
    ``StepCompleted`` / ``StepFailed``); it re-broadcasts them to dashboard
    clients for live tailing, so dropping one only costs a missing live line.

    Attributes:
        job_id: Job UUID string.
        step_index: Step position this line belongs to.
        stream: Which pipe produced the line ("stdout" or "stderr").
        line: One output line, already newline-stripped.
        timestamp: When the agent read the line.
    """
    type: Literal["step.log"] = "step.log"
    job_id: str
    step_index: int
    stream: Literal["stdout", "stderr"]
    line: str
    timestamp: datetime


class StepProgress(BaseModel):
    """Optional progress update from a step.

    Purely advisory; steps are not required to emit it. The server forwards it to
    dashboards without persisting.

    Attributes:
        job_id: Job UUID string.
        step_index: Step position.
        percent: Completion estimate, 0-100. Not validated or clamped.
        message: Optional human-readable status line shown next to the bar.
    """
    type: Literal["step.progress"] = "step.progress"
    job_id: str
    step_index: int
    percent: float
    message: str = ""


class StepCompleted(BaseModel):
    """Step finished successfully.

    Terminal success frame. The server hands this to ``JobRunner.on_step_completed``,
    which owns the DB writes: it merges ``outputs`` into the job's context (so later
    steps can reference them), appends the captured terminal text to the per-job log,
    marks the StepRun "success", and advances the job to the next step.

    Attributes:
        job_id: Job UUID string.
        step_index: Step position that finished.
        outputs: Values merged into the job context, keyed by the step class's
            declared ``OUTPUT_KEYS``. The agent builds this by pulling those keys
            out of the step's state dict (steps write values directly into state,
            not under an "outputs" sub-dict), falling back to an explicit
            ``state["outputs"]`` when the step provides one.
        command: Command line the step ran, if any — recorded in the job log.
        stdout: Full captured stdout for the job log (separate from the live
            ``StepLog`` stream, which is not persisted).
        stderr: Full captured stderr for the job log.
        exit_code: Process exit status; 0 or None for steps with no subprocess.
    """
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
    """Step execution failed.

    Terminal failure frame, routed to ``JobRunner.on_step_failed``. Whether the
    job dies here depends on the step's ``on_fail`` setting ("stop" ends the job,
    "continue" advances to the next step) — the agent does not know or decide that.

    Attributes:
        job_id: Job UUID string.
        step_index: Step position that failed.
        error: Human-readable failure reason, surfaced on the Job/StepRun row and
            in the UI. This is the only mandatory diagnostic, so make it specific.
        exit_code: Process exit status when a subprocess produced the failure.
        command: Command line that failed, if any — recorded in the job log.
        stdout: Captured stdout up to the failure.
        stderr: Captured stderr up to the failure; usually where the real cause is.
    """
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
    """Server instructs agent to execute a step.

    Emitted by ``JobRunner._execute_remote_step`` once a node has been selected.
    Everything the agent needs is inlined — the agent never queries the server's
    DB — so the payload is fully resolved before it is sent.

    Attributes:
        job_id: Job UUID string.
        step_index: Position in the job's step list; echoed back on every
            ``step.*`` reply and used as the correlation key ("{job_id}:{index}")
            on both sides.
        step_name: Registry key (see ``nexus_common.steps.registry``). The agent
            looks this up in its own ``STEP_REGISTRY``, so a step that exists only
            on the server produces a KeyError on the agent, not a routing error.
        params: Fully resolved parameters — job context merged in and the
            server-side ``resolve_for_os`` pass already applied. The agent applies
            ``resolve_for_os`` again against its own detected OS, which is
            idempotent because explicit params always beat OS defaults.
        artifacts: Storage keys the agent should pre-fetch before running.
        credential_config: Decrypted credential fields for this step, resolved by
            the server from the step's ``credential_name`` param.

    AI Note: ``credential_config`` carries plaintext secrets. It is only ever
    safe because the agent socket is authenticated with the node API key and is
    expected to run over wss:// off-LAN. Do not log this frame verbatim, and do
    not echo it back in any ``step.*`` reply.
    """
    type: Literal["execute_step"] = "execute_step"
    job_id: str
    step_index: int
    step_name: str
    params: dict[str, Any]  # already resolved (context merged + OS variants applied)
    artifacts: list[str] = Field(default_factory=list)  # S3 keys to pre-fetch
    credential_config: dict[str, Any] | None = None  # decrypted credential for this step


class CancelStepCommand(BaseModel):
    """Server instructs agent to cancel a running step.

    Best-effort: the agent looks up "{job_id}:{step_index}" among its running
    steps and calls ``FlowStep.cancel(state)``. Cancelling a step that already
    finished, or that this agent never ran, is a silent no-op rather than an
    error — the command can legitimately race step completion.

    Attributes:
        job_id: Job UUID string.
        step_index: Step position to cancel.
    """
    type: Literal["cancel_step"] = "cancel_step"
    job_id: str
    step_index: int


class ServerAck(BaseModel):
    """Server acknowledges agent registration or heartbeat.

    Attributes:
        message: Which thing was acknowledged — "registered" or "heartbeat_ok".

    AI Note: This is a liveness signal, not a delivery receipt. Agents do not
    block waiting for an ack, and step commands are never acked, so a missing
    ack cannot be used to infer that a step was lost.
    """
    type: Literal["ack"] = "ack"
    message: str = "ok"


# ── Dashboard → Server / Server → Dashboard Messages ────────────────────
#
# These are fan-out only: the server broadcasts them to every connected browser
# client. They are UI notifications, not state — a dashboard that misses one (or
# connects late) recovers by re-fetching over REST. Never make correctness depend
# on a dashboard event being delivered.


class DashboardNodeStatus(BaseModel):
    """Broadcast to dashboard WebSocket clients on node status change.

    Sent when an agent connects, registers, heartbeats, or its socket drops.

    Attributes:
        node_id: Node UUID string the row should update.
        status: New ``NodeStatus`` value as a plain string ("online"/"offline"/...).
            Typed loosely so the dashboard contract does not break when the enum grows.
        hostname: Included when known, so a client that has not yet loaded the node
            list can still render a label.
        last_heartbeat: Server-side receipt time of the latest heartbeat.
    """
    type: Literal["node.status"] = "node.status"
    node_id: str
    status: str
    hostname: str | None = None
    last_heartbeat: datetime | None = None


class DashboardJobStatus(BaseModel):
    """Broadcast to dashboard on job status change.

    Attributes:
        job_id: Job UUID string.
        status: Current ``JobStatus`` value as a plain string.
        current_step: Index of the step now executing; drives the progress
            indicator on the jobs list.
        step_name: Name of that step when the emitter knows it. The ``step.started``
            path sends None because it only has the index, so the UI must tolerate
            a missing name rather than blanking a previously shown one.
    """
    type: Literal["job.status"] = "job.status"
    job_id: str
    status: str
    current_step: int = 0
    step_name: str | None = None


class DashboardStepLog(BaseModel):
    """Broadcast to dashboard for live log streaming.

    Attributes:
        job_id: Job UUID string.
        step_index: Step position that produced the line.
        stream: "stdout" or "stderr" — untyped here (unlike ``StepLog``) because
            it is only used for display styling.
        line: One output line.

    AI Note: The ``type`` value "step.log" is deliberately identical to the
    agent-side ``StepLog``. The server relays agent log frames straight through
    to dashboards without re-wrapping, so the two models describe the same bytes
    minus the timestamp. Renaming either discriminator breaks live log tailing.
    """
    type: Literal["step.log"] = "step.log"
    job_id: str
    step_index: int
    stream: str
    line: str


class DashboardJobCompleted(BaseModel):
    """Broadcast when a job reaches terminal state.

    "Completed" here means *finished*, not *succeeded* — read ``status`` to tell
    completed / failed / cancelled apart.

    Attributes:
        job_id: Job UUID string.
        status: Terminal ``JobStatus`` value as a plain string.
        completed_at: When the job reached its terminal state.
    """
    type: Literal["job.completed"] = "job.completed"
    job_id: str
    status: str
    completed_at: datetime | None = None


# ── Type unions for message parsing ──────────────────────────────────────
#
# AI Note: These unions document intent and give call sites a type to annotate
# against; they are NOT used as Pydantic discriminated unions at runtime. Both
# ends dispatch by reading the raw ``type`` string and constructing the specific
# model (``ws._handle_agent_message`` does exactly this). Adding a message class
# therefore requires editing the relevant handler's if/elif chain too — appending
# it to a union here has no runtime effect on its own.

AgentMessage = (
    AgentRegister | AgentHeartbeat |
    StepStarted | StepLog | StepProgress | StepCompleted | StepFailed
)
"""Every frame an agent may send to the server."""

ServerCommand = ExecuteStepCommand | CancelStepCommand | ServerAck
"""Every frame the server may send down an agent socket."""

DashboardEvent = (
    DashboardNodeStatus | DashboardJobStatus |
    DashboardStepLog | DashboardJobCompleted
)
"""Every frame the server may broadcast to browser dashboard clients."""
