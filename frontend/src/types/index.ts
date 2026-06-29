export type UUID = string;

export type UserRole = "admin" | "manager" | "user";
export type NodeStatus = "online" | "offline" | "busy" | "maintenance";
export type JobStatus = "pending" | "queued" | "running" | "completed" | "failed" | "cancelled";
export type StepStatus = "pending" | "running" | "success" | "failed" | "cancelled" | "skipped";
export type TransferStatus = "pending" | "in_progress" | "completed" | "failed";
export type OSType = "macos" | "linux" | "windows";

export interface UserInfo {
  id: UUID;
  username: string;
  email: string | null;
  role: UserRole;
  is_active: boolean;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

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

export interface PoolInfo {
  id: UUID;
  name: string;
  description: string | null;
  node_count: number;
  created_at: string;
}

export interface StepConfig {
  step: string;
  params: Record<string, unknown>;
  on_fail?: "stop" | "continue";
}

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

export interface JobDetail {
  job: JobInfo;
  steps: StepRunInfo[];
  context_data: Record<string, unknown>;
  has_log?: boolean;
  has_results?: boolean;
}

export interface FieldSchema {
  name: string;
  required: boolean;
  description: string | null;
  default: unknown;
  examples: string[];
  field_type: string;
}

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

export interface CredentialTypeInfo {
  credential_type: string;
  required_fields: string[];
  optional_fields: string[];
  description: string;
}

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

export interface TemplateInfo {
  id: UUID;
  name: string;
  description: string | null;
  steps: StepConfig[];
  created_by: UUID;
  created_at: string;
}

// WebSocket message types
export interface WsNodeStatus {
  type: "node.status";
  node_id: string;
  status: NodeStatus;
  hostname?: string;
  last_heartbeat?: string;
}

export interface WsJobStatus {
  type: "job.status";
  job_id: string;
  status: JobStatus;
  current_step: number;
  step_name?: string;
}

export interface WsStepLog {
  type: "step.log";
  job_id: string;
  step_index: number;
  stream: "stdout" | "stderr";
  line: string;
}

export interface WsJobCompleted {
  type: "job.completed";
  job_id: string;
  status: string;
  completed_at?: string;
}

export type WsMessage = WsNodeStatus | WsJobStatus | WsStepLog | WsJobCompleted;
