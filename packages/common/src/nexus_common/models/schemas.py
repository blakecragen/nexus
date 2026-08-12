"""Pydantic schemas shared by the Nexus API, agents, and CLI.

The request/response contract for the whole HTTP surface. FastAPI routes in
``nexus_server.api.routes.*`` declare these as body models and
``response_model=``; the CLI client and the React frontend consume the same
shapes. Everything here is a pure data model — no DB access, no business logic —
so the ``nexus-common`` package stays importable from the agent, which has no
server dependencies.

Naming convention, used consistently below:
    ``*Create`` / ``*Submit`` / ``*Request`` — inbound bodies. UUIDs are inputs
        the caller already knows; server-assigned fields are absent.
    ``*Info`` / ``*Detail``                  — outbound projections of a DB row.
        Always carry ``id`` plus server-managed timestamps.

Two cross-cutting rules:
    - Identifiers are typed ``UUID`` here (HTTP boundary), but the agent
      WebSocket protocol in ``nexus_common.agent_protocol`` uses ``str`` for the
      same ids. Conversion happens at the server edge.
    - Any datetime shown to a client uses the ``UTCDateTime`` alias, never bare
      ``datetime``. See the note on ``_iso_utc`` for why.

AI Note: These models mirror ``nexus_server.db.models`` by hand — there is no
generated mapping. A column added to a DB model does not appear in the API until
it is added to the matching ``*Info`` here, and a field renamed here silently
breaks the frontend, which has its own hand-written TypeScript types.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, PlainSerializer

from nexus_common.models.enums import (
    CredentialType,
    JobStatus,
    NodeStatus,
    OSType,
    StepStatus,
    TransferStatus,
    UserRole,
)


def _iso_utc(dt: datetime) -> str:
    """Serialize a datetime as timezone-aware ISO 8601. Naive values are assumed
    to be UTC (SQLite returns naive datetimes), so clients never misread them as
    local time.

    Args:
        dt: Value to serialize. May be naive or already timezone-aware.

    Returns:
        ISO 8601 string that always carries an offset (e.g. "...+00:00").

    AI Note: This exists to fix a real bug — SQLite drops tzinfo on round-trip, so
    UTC timestamps came back naive, and browsers parsed them as *local* time. That
    produced nonsense relative labels like "-17990s ago" for events in the recent
    past. Assuming UTC is correct here only because every writer in the codebase
    stores ``datetime.now(timezone.utc)``; a writer that stamps local time would
    be silently mislabeled by this function.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# Use in place of bare `datetime` for any field serialized to clients.
#
# AI Note: ``when_used="json"`` matters — the serializer only runs for JSON output,
# so Python-side consumers (tests, internal calls using model_dump()) still get real
# datetime objects. Fields typed as plain ``datetime`` bypass this entirely and will
# reintroduce the naive-timestamp bug, so prefer this alias for anything client-facing.
UTCDateTime = Annotated[datetime, PlainSerializer(_iso_utc, return_type=str, when_used="json")]


# ── Auth ────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    """Credentials posted to ``POST /api/auth/login``.

    Attributes:
        username: Account name; the login identifier (email is not used for auth).
        password: Plaintext password, checked against the stored hash. Never logged
            and never echoed back in any response model.
    """
    username: str
    password: str


class TokenResponse(BaseModel):
    """JWT pair returned by login and refresh.

    Attributes:
        access_token: Short-lived bearer token sent as ``Authorization: Bearer ...``
            on every subsequent request.
        refresh_token: Longer-lived token exchanged at ``POST /api/auth/refresh``
            for a new access token.
        token_type: Always "bearer"; present so standard OAuth2 clients work.
    """
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserInfo(BaseModel):
    """Public projection of a user account.

    Deliberately omits the password hash and every other secret — this is the only
    user shape returned by the API.

    Attributes:
        id: User UUID.
        username: Login identifier.
        email: Contact address; optional because accounts can be created without one.
        role: Global ``UserRole`` gating admin-only routes.
        is_active: False disables login without deleting the account or orphaning
            the jobs it submitted.
    """
    id: UUID
    username: str
    email: str | None = None
    role: UserRole
    is_active: bool


# ── Nodes ───────────────────────────────────────────────────────────────

class NodeRegistration(BaseModel):
    """Inventory reported when provisioning a compute node over HTTP.

    Mirrors ``agent_protocol.AgentRegister`` (the WebSocket equivalent), with two
    differences: ``os_type`` is validated against the ``OSType`` enum here, and
    there is no ``node_id`` because the server assigns it.

    Attributes:
        hostname: Reported host name.
        display_name: Operator-chosen label; falls back to hostname in the UI.
        os_type: OS family — gates step eligibility and OS_VARIANTS resolution.
        os_version, arch, cpu_model, cpu_cores, ram_mb, gpu_info: Static hardware
            inventory, refreshed on each agent reconnect.
        agent_version: Agent build version, for spotting protocol skew.
        ip_address: Self-reported address; informational, the server never dials out.
        tags: Free-form labels. Descriptive only — they do not gate scheduling.
    """
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
    tags: list[str] = Field(default_factory=list)


class NodeInfo(BaseModel):
    """Outbound projection of a ``Node`` row.

    ``NodeRegistration`` plus server-managed fields. Note the node's API key is
    absent: it is returned exactly once, by the provisioning route, and never again.

    Attributes:
        id: Node UUID — the value the agent embeds in its WebSocket URL.
        status: Current ``NodeStatus``; only ONLINE nodes receive work.
        last_heartbeat: Server-side receipt time of the newest heartbeat. None until
            an agent has ever connected. The UI derives staleness from this.
        registered_at: When the node was provisioned.
    """
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
    tags: list[str]
    last_heartbeat: UTCDateTime | None = None
    registered_at: UTCDateTime


# ── Pools ───────────────────────────────────────────────────────────────

class PoolCreate(BaseModel):
    """Body for creating or updating a pool (a named group of nodes).

    Attributes:
        name: Human-readable pool name. Also the handle used in the ``.nexus`` DSL
            (``# pool: <name>``), which the CLI resolves to ``target_pool_id``.
        description: Optional free text.
    """
    name: str
    description: str | None = None


class PoolInfo(BaseModel):
    """Outbound projection of a ``Pool`` row.

    Attributes:
        id: Pool UUID; the value jobs and steps target.
        name: Pool name.
        description: Optional free text.
        node_count: Members in the pool, computed per request rather than stored.
            Counts membership only — it does not tell you how many are online.
        created_at: Creation timestamp.
    """
    id: UUID
    name: str
    description: str | None = None
    node_count: int = 0
    created_at: UTCDateTime


# ── Jobs ────────────────────────────────────────────────────────────────

class StepConfig(BaseModel):
    """One step in a job's plan: which step type, its params, and its targeting.

    Produced by the UI builder or by ``nexus_common.parser`` from a ``.nexus`` file,
    validated at submit time against the step class in ``STEP_REGISTRY``, then
    persisted verbatim in ``Job.steps_config``.

    Attributes:
        step: Registry name of the ``FlowStep`` (e.g. "run_command"). Rejected with
            400 at submit time if unknown.
        params: Step-specific parameters, validated against the step's
            ``PARAMS_SCHEMA``. Values may contain ``${var}`` placeholders resolved
            from upstream step outputs at dispatch time.
        on_fail: "stop" ends the job when this step fails; "continue" moves to the
            next step. A job whose steps all use "continue" can finish COMPLETED
            with failed steps inside it.
        target_node_id: Pin this step to one node, overriding the job-level target.
        target_pool_id: Restrict this step to one pool, overriding the job-level target.
        target_os: Require an OS family ("macos"/"linux"/"windows").

    AI Note: The three ``target_*`` fields are per-step overrides that take
    precedence over the job-level targets on the Job row. They are also accepted
    *inside* the DSL's parameter body and lifted out by the parser (see
    ``parser._STEP_KEYWORDS``), which is why they are step attributes rather than
    params — the step's own ``PARAMS_SCHEMA`` would reject them as unknown keys.
    """
    step: str
    params: dict = Field(default_factory=dict)
    on_fail: str = "stop"  # "stop" or "continue"
    target_node_id: UUID | None = None  # pin this step to a specific node
    target_pool_id: UUID | None = None  # restrict scheduling to a specific pool
    target_os: str | None = None  # require a specific OS family (macos / linux / windows)


class JobSubmit(BaseModel):
    """Body for ``POST /api/jobs`` — a complete job plan.

    The server validates every step against the accumulated context of upstream
    ``OUTPUT_KEYS`` before persisting anything, so an invalid plan is rejected as a
    whole rather than failing partway through execution.

    Attributes:
        name: Display name for the job.
        steps: Ordered plan. Executed sequentially; flow steps may jump within it.
        target_pool_id: Default pool for steps that do not override it.
        target_node_id: Default node for steps that do not override it.
        priority: 0=high, 1=normal, 2=low — lower number wins in the queue.
        storage_target: Named storage backend override for this job's artifacts;
            None uses the configured default backend.
    """
    name: str
    steps: list[StepConfig]
    target_pool_id: UUID | None = None
    target_node_id: UUID | None = None
    priority: int = 1  # 0=high, 1=normal, 2=low
    storage_target: str | None = None  # override default storage backend


class JobInfo(BaseModel):
    """Outbound summary of a ``Job`` row — the shape used in list views.

    Attributes:
        id: Job UUID.
        name: Display name.
        submitted_by: UUID of the submitting user.
        target_pool_id, target_node_id: Job-level scheduling targets, which
            individual steps may override.
        priority: 0=high, 1=normal, 2=low.
        status: Current ``JobStatus``.
        current_step: Index of the step being executed. With flow/jump steps this
            can move backwards, so it is a position, not a progress counter.
        error: Failure reason when status is FAILED.
        created_at: When the job was submitted.
        started_at: When the first step was dispatched; None while queued.
        completed_at: When the job reached a terminal state.
    """
    id: UUID
    name: str
    submitted_by: UUID
    target_pool_id: UUID | None = None
    target_node_id: UUID | None = None
    priority: int
    status: JobStatus
    current_step: int
    error: str | None = None
    created_at: UTCDateTime
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None


class StepRunInfo(BaseModel):
    """Outbound projection of one ``StepRun`` row — a single *attempt* at a step.

    Attributes:
        id: StepRun UUID.
        job_id: Owning job.
        step_index: Position in the job plan.
        step_name: Registry name of the step type that ran.
        status: Terminal or in-flight ``StepStatus`` for this attempt.
        node_id: Node that executed it; None for control-plane steps and for
            attempts that never found a node.
        input_params: Fully resolved params actually sent to the agent (context
            merged, OS variants applied) — not the raw plan values.
        output_params: Values harvested from the step's ``OUTPUT_KEYS`` and merged
            into the job context.
        error: Failure reason when status is FAILED.
        started_at, finished_at: Execution window for this attempt.

    AI Note: (job_id, step_index) does not identify a row. Loop/jump steps produce
    one StepRun per attempt at the same index, so clients rendering per-step state
    must group by index and take the newest row.
    """
    id: UUID
    job_id: UUID
    step_index: int
    step_name: str
    status: StepStatus
    node_id: UUID | None = None
    input_params: dict | None = None
    output_params: dict | None = None
    error: str | None = None
    started_at: UTCDateTime | None = None
    finished_at: UTCDateTime | None = None


class JobDetail(BaseModel):
    """Full job view returned by ``GET /api/jobs/{id}`` — the job detail page.

    Attributes:
        job: Summary row.
        steps: Every StepRun attempt for this job, including repeats at the same index.
        steps_config: The *submitted plan* — the job's ``steps_config`` column,
            which is what "Duplicate" pre-fills the Job Builder from and what
            ``POST /api/jobs/{id}/requeue`` re-submits.
        context_data: Accumulated outputs merged from completed steps; the values
            that ``${var}`` references resolve against.
        has_log: Whether a terminal transcript exists at ``/api/jobs/{id}/log``.
            A boolean rather than the text itself so list/detail payloads stay small.
        has_results: Whether a results tarball exists to download.

    AI Note: ``steps`` and ``steps_config`` are easy to confuse and are not
    parallel lists. ``steps_config`` is the plan — one entry per position, fixed
    at submission. ``steps`` is the execution record — one entry per *attempt*,
    so a loop produces several entries at the same ``step_index`` and a job that
    never started produces none at all. Never zip them.

    AI Note: ``steps_config`` is exposed on the single-job detail view only. It
    is deliberately absent from ``JobInfo`` (see ``_job_to_info`` in
    ``routes/jobs.py``) because the list endpoint returns up to 50 rows and the
    plan is unbounded JSON.
    """
    job: JobInfo
    steps: list[StepRunInfo]
    steps_config: list[StepConfig] = Field(default_factory=list)
    context_data: dict = Field(default_factory=dict)
    has_log: bool = False
    has_results: bool = False


# ── Steps Schema ────────────────────────────────────────────────────────
#
# Serialized form of a FlowStep class, produced by FlowStep.to_schema() and served
# by /api/steps. This is how the frontend renders a step's parameter form without
# knowing anything about the Python step classes.

class FieldSchema(BaseModel):
    """One parameter of a step, derived from its ``PARAMS_SCHEMA`` Pydantic model.

    Attributes:
        name: Parameter key.
        required: Whether the field has no default.
        description: Help text from the Pydantic ``Field(description=...)``.
        default: Default value, coerced to a JSON-safe scalar or its ``str()`` form
            by ``to_schema()``; always None for required fields.
        examples: Example values, stringified so the form renderer needs no type
            switch even when the underlying examples are lists or dicts.
        field_type: Coarse UI type ("string"/"integer"/"number"/"boolean"/"list"/
            "object"), not the real Python annotation — it only picks a widget.
    """
    name: str
    required: bool
    description: str | None = None
    default: object = None
    examples: list[str] = Field(default_factory=list)
    field_type: str = "string"


class InputRuleSchema(BaseModel):
    """Serialized ``InputRule`` so the UI can mirror server-side validation.

    Attributes:
        rule_type: Discriminator — "required", "optional", "context_satisfiable",
            or "at_least_one".
        fields: Parameter names the rule covers; multi-element for "at_least_one".
        description: Human-readable explanation of the constraint.

    AI Note: This is a *hint* for the form UI. The server re-runs the real rules in
    ``FlowStep.validate_params`` at submit time, so a client that ignores or
    misinterprets these still cannot submit an invalid job.
    """
    rule_type: str
    fields: list[str]
    description: str | None = None


class StepSchemaInfo(BaseModel):
    """Complete published description of one registered step type.

    Returned by ``GET /api/steps`` and ``GET /api/steps/{name}``, built by
    ``FlowStep.to_schema()``.

    Attributes:
        name: Registry name (the string used in ``StepConfig.step``).
        description: The step class's ``DESCRIPTION``.
        requires_node: False for control-plane steps (sleep, jump) that run on the
            server instead of being dispatched to an agent.
        supported_os: OS families this step can run on.
        output_keys: Keys this step contributes to the job context on success —
            what downstream steps may reference.
        fields: Parameter schemas for form rendering.
        rules: Validation rules for client-side pre-checks.
        os_variants: Per-OS default params merged before dispatch; explicit params win.

    AI Note: ``to_schema()`` also emits a ``large_output`` key that this model does
    not declare, so it is dropped when the route constructs ``StepSchemaInfo(**schema)``.
    Add the field here if the UI ever needs that hint.
    """
    name: str
    description: str
    requires_node: bool
    supported_os: list[str]
    output_keys: list[str]
    fields: list[FieldSchema]
    rules: list[InputRuleSchema]
    os_variants: dict[str, dict] = Field(default_factory=dict)


# ── Credentials ─────────────────────────────────────────────────────────

class CredentialCreate(BaseModel):
    """Body for storing a new credential.

    Attributes:
        name: Unique handle steps reference via their ``credential_name`` param;
            the runner resolves it and inlines the decrypted config into
            ``ExecuteStepCommand.credential_config``.
        credential_type: Selects the validation/encryption strategy and the
            required field set.
        fields: Raw secret material. Encrypted before it touches the DB and never
            returned by any read route — ``CredentialInfo`` has no ``fields``.
        description: Optional free text.
        is_shared: If True, visible beyond the owner subject to ``allowed_groups``.
        allowed_groups: Group UUIDs granted access when shared.

    AI Note: This is the only place plaintext secrets legitimately enter the API.
    Do not add ``fields`` to any ``*Info`` model, and do not log request bodies on
    the credential routes.
    """
    name: str
    credential_type: CredentialType
    fields: dict  # raw fields (encrypted before storage)
    description: str | None = None
    is_shared: bool = False
    allowed_groups: list[UUID] = Field(default_factory=list)


class CredentialInfo(BaseModel):
    """Outbound projection of a credential — metadata only, never the secret.

    Attributes:
        id: Credential UUID.
        name: Handle used by steps.
        credential_type: Which kind of secret this is.
        description: Optional free text.
        is_shared: Whether non-owners may use it.
        owner_id: Creating user.
        created_at, updated_at: Lifecycle timestamps; ``updated_at`` is None until
            the credential is first modified.
    """
    id: UUID
    name: str
    credential_type: CredentialType
    description: str | None = None
    is_shared: bool
    owner_id: UUID
    created_at: UTCDateTime
    updated_at: UTCDateTime | None = None


class CredentialTypeInfo(BaseModel):
    """Describes what a given credential type expects, so the UI can build its form.

    Attributes:
        credential_type: The type being described.
        required_fields: Keys that must appear in ``CredentialCreate.fields``.
        optional_fields: Keys that may appear.
        description: Human-readable explanation of the type.
    """
    credential_type: CredentialType
    required_fields: list[str]
    optional_fields: list[str] = Field(default_factory=list)
    description: str


# ── Storage ─────────────────────────────────────────────────────────────

class StorageBackendCreate(BaseModel):
    """Body for registering a place artifacts can be stored.

    Attributes:
        backend_type: Driver key — "minio", "gdrive", "nas", or "s3". A plain string
            rather than an enum, so it is validated by the backend manager at use
            time, not by Pydantic here.
        config: Driver-specific settings (endpoint, bucket, path, ...). Shape depends
            entirely on ``backend_type``.
        credential_id: Credential used to authenticate against the backend.
        capacity_bytes: Declared capacity, used together with ``FlowStep.LARGE_OUTPUT``
            to prefer roomy backends. None means unbounded/unknown.
        is_default: Whether this backend receives artifacts when a job specifies no
            ``storage_target``.
        priority: Selection order among eligible backends; lower is preferred.
    """
    name: str
    backend_type: str  # "minio", "gdrive", "nas", "s3"
    config: dict  # backend-specific config
    credential_id: UUID
    capacity_bytes: int | None = None
    is_default: bool = False
    priority: int = 10


class StorageBackendInfo(BaseModel):
    """Outbound projection of a storage backend.

    Attributes:
        used_bytes: Bytes currently attributed to this backend, tracked by the
            server as artifacts are written — not measured from the remote store,
            so it can drift if files are added or removed out of band.
        is_active: False parks the backend: existing artifacts stay readable but no
            new writes are routed to it.
        (Remaining fields mirror ``StorageBackendCreate``.)

    AI Note: ``config`` is echoed back as stored. Anything secret belongs in the
    linked credential, not in ``config``, or it will be exposed by this response.
    """
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
    created_at: UTCDateTime


class TransferRequest(BaseModel):
    """Body for copying an artifact from its current backend to another.

    Attributes:
        artifact_id: Artifact to move; its current backend is the implicit source.
        dest_backend_id: Destination backend.
        delete_source: If True, drop the original *after* the copy reaches
            COMPLETED. A failed transfer must leave the source intact.
    """
    artifact_id: UUID
    dest_backend_id: UUID
    delete_source: bool = False


class TransferInfo(BaseModel):
    """Outbound projection of an in-flight or finished ``StorageTransfer``.

    Attributes:
        status: Current ``TransferStatus``.
        bytes_transferred: Progress counter; meaningful while IN_PROGRESS and on a
            partial failure, where it shows how far the copy got.
        error: Failure reason when status is FAILED.
        started_at, completed_at: Transfer window.
    """
    id: UUID
    artifact_id: UUID
    source_backend_id: UUID
    dest_backend_id: UUID
    status: TransferStatus
    bytes_transferred: int = 0
    error: str | None = None
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None


# ── Artifacts ───────────────────────────────────────────────────────────

class ArtifactInfo(BaseModel):
    """A file produced by a job, plus where it currently lives.

    Attributes:
        id: Artifact UUID.
        job_id: Producing job.
        step_run_id: Producing step attempt, when attributable.
        filename: Original name shown in the UI.
        storage_backend_id: Backend currently holding the bytes. Changes when a
            transfer completes.
        storage_backend_name: Denormalized backend name so the UI can label the row
            without a second lookup.
        storage_key: Path/key within that backend. Backend-relative — meaningless
            without ``storage_backend_id``.
        content_type: MIME type when known.
        size_bytes: File size.
        created_at: Upload time.
    """
    id: UUID
    job_id: UUID
    step_run_id: UUID | None = None
    filename: str
    storage_backend_id: UUID
    storage_backend_name: str | None = None
    storage_key: str
    content_type: str | None = None
    size_bytes: int
    created_at: UTCDateTime


# ── Templates ───────────────────────────────────────────────────────────

class TemplateCreate(BaseModel):
    """Body for saving a reusable job plan.

    Attributes:
        name: Template name.
        description: Optional free text.
        steps: The saved plan, the same ``StepConfig`` list a job submission uses.
    """
    name: str
    description: str | None = None
    steps: list[StepConfig]


class TemplateInfo(BaseModel):
    """Outbound projection of a saved template.

    Attributes:
        id: Template UUID.
        name: Template name.
        description: Optional free text.
        steps: Saved plan, ready to be posted as a ``JobSubmit``.
        created_by: Author's user UUID.
        created_at: Creation timestamp.

    AI Note: Steps are stored as-is and are *not* re-validated when the template is
    loaded. A template saved before a step was renamed or had a required param added
    will only fail at job-submit time, not at template-read time.
    """
    id: UUID
    name: str
    description: str | None = None
    steps: list[StepConfig]
    created_by: UUID
    created_at: UTCDateTime
