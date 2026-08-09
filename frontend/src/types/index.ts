/**
 * The frontend's view of the Nexus wire format — every REST response body and
 * every WebSocket event, as TypeScript types.
 *
 * Role in the system: a single vocabulary shared by `@/api/client` (which
 * annotates each endpoint's return type from here), `@/stores` (which stores
 * these shapes), and every page that renders them. Type-only module: it emits
 * no JavaScript and must never gain runtime code.
 *
 * AI Note: these are HAND-MIRRORED from the server's Pydantic schemas in
 * `packages/common/src/nexus_common/models/schemas.py` and the agent protocol in
 * `packages/common/src/nexus_common/agent_protocol.py`. Nothing generates or
 * validates them — `request<T>` casts the decoded JSON without checking it. A
 * server field rename therefore produces `undefined` at runtime while the
 * compiler stays happy. Any schema change on the server needs a matching edit
 * here in the same commit.
 *
 * AI Note: all timestamp fields are ISO-8601 strings, not `Date`. The server
 * serialises them as UTC (see the UTCDateTime serializer); `formatRelativeTime`
 * in `@/lib/utils` depends on that being timezone-aware.
 */

/** A server-generated UUID. Only a string alias — no format validation anywhere. */
export type UUID = string;

/**
 * Authorization tiers. `admin` can manage users and all resources; `manager`
 * manages jobs/nodes; `user` submits and views. Enforcement is entirely
 * server-side — the UI does not hide routes by role.
 */
export type UserRole = "admin" | "manager" | "user";
/**
 * Agent lifecycle state. `busy` means running a job; `maintenance` means the
 * node is connected but excluded from scheduling (a drain switch).
 */
export type NodeStatus = "online" | "offline" | "busy" | "maintenance";
/**
 * Job lifecycle. `pending` → accepted but not yet placed; `queued` → assigned
 * and waiting on an agent; then `running` → one terminal state.
 * `completed`/`failed`/`cancelled` are terminal.
 */
export type JobStatus = "pending" | "queued" | "running" | "completed" | "failed" | "cancelled";
/**
 * Per-step lifecycle. Note it differs from `JobStatus`: success is `success`
 * (not `completed`), and `skipped` exists for steps bypassed by flow control.
 * Do not treat the two unions as interchangeable.
 */
export type StepStatus = "pending" | "running" | "success" | "failed" | "cancelled" | "skipped";
/** Artifact copy-between-backends lifecycle; polled via `api.listTransfers` (no WS event). */
export type TransferStatus = "pending" | "in_progress" | "completed" | "failed";
/** Agent host platform. Gates which steps can run on a node (`StepSchemaInfo.supported_os`). */
export type OSType = "macos" | "linux" | "windows";

/** Authenticated user, from GET /api/auth/me. Drives the sidebar footer and role display. */
export interface UserInfo {
  id: UUID;
  username: string;
  email: string | null;
  role: UserRole;
  is_active: boolean;
}

/**
 * JWT pair from POST /api/auth/login or /api/auth/refresh.
 *
 * AI Note: no `expires_in` is exposed, so the client cannot pre-emptively
 * refresh. Expiry is discovered reactively as a 401, which `@/api/client` turns
 * into a logout + redirect.
 */
export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

/**
 * A registered agent node. Hardware/OS fields are self-reported by the agent at
 * registration; `last_heartbeat` is null until the agent first connects and is
 * what the Nodes page renders as "last seen".
 *
 * AI Note: there is deliberately no `capabilities` field — that concept was
 * removed and the scheduler does not gate on it. Placement is by pool/node
 * targeting plus `supported_os`.
 */
export interface NodeInfo {
  id: UUID;
  hostname: string;
  display_name: string | null;
  os_type: OSType;
  os_version: string;
  arch: string;
  cpu_model: string;
  cpu_cores: number;
  ram_mb: number;
  gpu_info: string | null;
  agent_version: string;
  ip_address: string;
  status: NodeStatus;
  tags: string[];
  last_heartbeat: string | null;
  registered_at: string;
}

/** A named group of nodes usable as a job target. `node_count` is computed server-side. */
export interface PoolInfo {
  id: UUID;
  name: string;
  description: string | null;
  node_count: number;
  created_at: string;
}

/**
 * One step in a job definition, as composed by the Job Builder and sent to
 * POST /api/jobs.
 *
 * @property step   the registered step type name (must match a `StepSchemaInfo.name`).
 * @property params values for that step's declared fields; validated server-side
 *   at submit time against the step schema, including that any referenced
 *   upstream output key is actually produced by an earlier step.
 * @property on_fail `"stop"` (default) aborts the job; `"continue"` runs the
 *   next step anyway.
 */
export interface StepConfig {
  step: string;
  params: Record<string, unknown>;
  on_fail?: "stop" | "continue";
}

/**
 * Job summary row, from GET /api/jobs.
 *
 * `current_step` is a 0-based index into the job's steps and advances via
 * `job.status` WebSocket events. Exactly one of `target_pool_id` /
 * `target_node_id` is non-null.
 */
export interface JobInfo {
  id: UUID;
  name: string;
  submitted_by: UUID;
  target_pool_id: UUID | null;
  target_node_id: UUID | null;
  priority: number;
  status: JobStatus;
  current_step: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

/**
 * Execution record for one step of one job.
 *
 * `input_params` are the resolved values actually used (after substituting
 * earlier steps' outputs); `output_params` are the step's OUTPUT_KEYS, which
 * later steps can reference. `node_id` is null until the step is dispatched.
 */
export interface StepRunInfo {
  id: UUID;
  job_id: UUID;
  step_index: number;
  step_name: string;
  status: StepStatus;
  node_id: UUID | null;
  input_params: Record<string, unknown> | null;
  output_params: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/**
 * GET /api/jobs/{id} — everything `@/pages/JobDetail` needs in one round trip.
 *
 * `context_data` is the accumulated key/value state threaded between steps.
 * `has_log` / `has_results` are optional booleans the page uses to decide
 * whether to offer the log view and the results tree / Download button; treat a
 * missing value as false (older servers omit them).
 */
export interface JobDetail {
  job: JobInfo;
  steps: StepRunInfo[];
  context_data: Record<string, unknown>;
  has_log?: boolean;
  has_results?: boolean;
}

/**
 * One parameter of a step type, used to generate its form control.
 * `field_type` is a server-supplied string (e.g. "string", "int", "bool") that
 * the Job Builder switches on to pick an input widget.
 */
export interface FieldSchema {
  name: string;
  required: boolean;
  description: string | null;
  default: unknown;
  examples: string[];
  field_type: string;
}

/**
 * Introspection payload for one registered step type, from GET /api/steps.
 * This is what makes the Job Builder generic: the frontend ships no per-step
 * knowledge and renders entirely from this schema.
 *
 * @property requires_node steps that need an agent (vs. server-side flow control).
 * @property supported_os  restricts which nodes can run it.
 * @property output_keys   context keys this step publishes for later steps.
 * @property rules         cross-field constraints (e.g. mutually-exclusive params).
 * @property os_variants   per-OS parameter overrides.
 */
export interface StepSchemaInfo {
  name: string;
  description: string;
  requires_node: boolean;
  supported_os: string[];
  output_keys: string[];
  fields: FieldSchema[];
  rules: { rule_type: string; fields: string[]; description?: string }[];
  os_variants: Record<string, Record<string, unknown>>;
}

/**
 * Credential metadata. `is_shared` makes it usable by users other than `owner_id`.
 *
 * AI Note: intentionally contains NO secret material — secrets are encrypted at
 * rest server-side and never returned. Adding a secret field here would leak it
 * into browser memory and any state dump.
 */
export interface CredentialInfo {
  id: UUID;
  name: string;
  credential_type: string;
  description: string | null;
  is_shared: boolean;
  owner_id: UUID;
  created_at: string;
  updated_at: string | null;
}

/** A credential kind and its field contract, from GET /api/credentials/types. Drives the dynamic "add credential" form. */
export interface CredentialTypeInfo {
  credential_type: string;
  required_fields: string[];
  optional_fields: string[];
  description: string;
}

/**
 * A configured artifact store. `config` is backend-type specific (endpoint,
 * bucket, path, ...) and untyped here. `is_default` marks where new artifacts
 * land; `priority` orders candidates. `capacity_bytes` is null when unknown/unbounded.
 */
export interface StorageBackendInfo {
  id: UUID;
  name: string;
  backend_type: string;
  config: Record<string, unknown>;
  credential_id: UUID;
  capacity_bytes: number | null;
  used_bytes: number;
  is_default: boolean;
  is_active: boolean;
  priority: number;
  created_at: string;
}

/**
 * A stored file produced by a job. `storage_key` is the backend-relative
 * location; `step_run_id` is null for job-level outputs.
 */
export interface ArtifactInfo {
  id: UUID;
  job_id: UUID;
  step_run_id: UUID | null;
  filename: string;
  storage_backend_id: UUID;
  storage_backend_name: string | null;
  storage_key: string;
  content_type: string | null;
  size_bytes: number;
  created_at: string;
}

/** An in-flight or finished artifact copy between two backends. `bytes_transferred` drives the progress bar. */
export interface TransferInfo {
  id: UUID;
  artifact_id: UUID;
  source_backend_id: UUID;
  dest_backend_id: UUID;
  status: TransferStatus;
  bytes_transferred: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

/**
 * A saved, reusable step sequence.
 *
 * AI Note: no endpoint wrapper for templates exists in `@/api/client` yet — this
 * type is declared ahead of the feature. Do not assume the UI reads it.
 */
export interface TemplateInfo {
  id: UUID;
  name: string;
  description: string | null;
  steps: StepConfig[];
  created_by: UUID;
  created_at: string;
}

// WebSocket message types
//
// AI Note: the four interfaces below form the discriminated union `WsMessage`,
// narrowed on the literal `type` field by the switch in `handleWsMessage`
// (`@/stores`). They mirror the broadcast payloads in
// `packages/server/src/nexus_server/api/routes/ws.py`. Adding a variant here
// without adding a case there means the event is silently dropped; adding a
// case without the type breaks the exhaustiveness of the narrowing.

/** Node came online/offline/busy/into maintenance. Patches `useNodesStore`. */
export interface WsNodeStatus {
  type: "node.status";
  node_id: string;
  status: NodeStatus;
  hostname?: string;
  last_heartbeat?: string;
}

/** Job progressed — new status and/or a new `current_step`. Patches `useJobsStore`. */
export interface WsJobStatus {
  type: "job.status";
  job_id: string;
  status: JobStatus;
  current_step: number;
  step_name?: string;
}

/**
 * One line of streamed step output. Appended to `useLiveLogsStore` under
 * `${job_id}:${step_index}`.
 *
 * AI Note: highest-volume message by far — one per output line of a running
 * step. Anything added to the `step.log` path in the dispatcher runs at that rate.
 */
export interface WsStepLog {
  type: "step.log";
  job_id: string;
  step_index: number;
  stream: "stdout" | "stderr";
  line: string;
}

/**
 * Job reached a terminal state.
 *
 * AI Note: `status` is a bare `string` here, not `JobStatus`, unlike
 * `WsJobStatus.status`. The dispatcher passes it straight into
 * `updateJobStatus`, which casts. Tightening it to `JobStatus` would be an
 * improvement but changes the union's shape — check the WS tests first.
 */
export interface WsJobCompleted {
  type: "job.completed";
  job_id: string;
  status: string;
  completed_at?: string;
}

/** Every message the dashboard socket can deliver; discriminated on `type`. */
export type WsMessage = WsNodeStatus | WsJobStatus | WsStepLog | WsJobCompleted;
