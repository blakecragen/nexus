import type { TokenResponse } from "@/types";

const API_BASE = "/api";

let accessToken: string | null = localStorage.getItem("nexus_token");

export function setToken(token: string | null) {
  accessToken = token;
  if (token) {
    localStorage.setItem("nexus_token", token);
  } else {
    localStorage.removeItem("nexus_token");
  }
}

export function getToken(): string | null {
  return accessToken;
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };

  if (accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (res.status === 401) {
    setToken(null);
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  // Auth
  login: (username: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  refresh: (refreshToken: string) =>
    request<TokenResponse>("/auth/refresh", {
      method: "POST",
      body: JSON.stringify({ refresh_token: refreshToken }),
    }),
  getMe: () => request<import("@/types").UserInfo>("/auth/me"),
  register: (data: { username: string; password: string; email?: string; role?: string }) =>
    request<import("@/types").UserInfo>("/auth/register", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  // Nodes
  listNodes: (params?: Record<string, string>) => {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return request<import("@/types").NodeInfo[]>(`/nodes${qs}`);
  },
  getNode: (id: string) => request<import("@/types").NodeInfo>(`/nodes/${id}`),
  createNode: (data: Record<string, unknown>) =>
    request<import("@/types").NodeInfo & { api_key: string }>("/nodes", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  provisionNode: async (data: Record<string, unknown>) => {
    // Custom fetch: provisioning can fail with a 502 whose body carries the
    // install log, which we want to surface in the UI.
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetch(`${API_BASE}/nodes/provision`, {
      method: "POST",
      headers,
      body: JSON.stringify(data),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = body?.detail;
      const err = new Error(
        typeof detail === "string" ? detail : detail?.error || `HTTP ${res.status}`
      ) as Error & { log?: string[] };
      err.log = (detail && typeof detail === "object" && detail.log) || [];
      throw err;
    }
    return body as import("@/types").NodeInfo & {
      api_key: string;
      ws_url: string;
      mode: string;
      log: string[];
    };
  },
  deleteNode: (id: string) => request<void>(`/nodes/${id}`, { method: "DELETE" }),
  reconnectNode: async (id: string, data: Record<string, unknown>) => {
    // Like provisionNode: a 502 body carries the setup log we want to surface.
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetch(`${API_BASE}/nodes/${id}/reconnect`, {
      method: "POST",
      headers,
      body: JSON.stringify(data),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = body?.detail;
      const err = new Error(
        typeof detail === "string" ? detail : detail?.error || `HTTP ${res.status}`
      ) as Error & { log?: string[] };
      err.log = (detail && typeof detail === "object" && detail.log) || [];
      throw err;
    }
    return body as import("@/types").NodeInfo & {
      ws_url: string;
      mode: string;
      online: boolean;
      log: string[];
    };
  },
  setMaintenance: (id: string, enabled: boolean) =>
    request<import("@/types").NodeInfo>(`/nodes/${id}/maintenance`, {
      method: "PUT",
      body: JSON.stringify({ maintenance: enabled }),
    }),

  // Pools
  listPools: () => request<import("@/types").PoolInfo[]>("/pools"),
  getPool: (id: string) =>
    request<{ pool: import("@/types").PoolInfo; nodes: import("@/types").NodeInfo[] }>(`/pools/${id}`),
  createPool: (data: { name: string; description?: string }) =>
    request<import("@/types").PoolInfo>("/pools", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  deletePool: (id: string) => request<void>(`/pools/${id}`, { method: "DELETE" }),
  addNodeToPool: (poolId: string, nodeId: string) =>
    request<void>(`/pools/${poolId}/nodes`, {
      method: "POST",
      body: JSON.stringify({ node_id: nodeId }),
    }),
  removeNodeFromPool: (poolId: string, nodeId: string) =>
    request<void>(`/pools/${poolId}/nodes/${nodeId}`, { method: "DELETE" }),

  // Jobs
  listJobs: (params?: Record<string, string>) => {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return request<import("@/types").JobInfo[]>(`/jobs${qs}`);
  },
  getJob: (id: string) => request<import("@/types").JobDetail>(`/jobs/${id}`),
  getJobLog: async (id: string): Promise<string> => {
    // Plain-text endpoint — can't use request<T> (which does res.json()).
    const headers: Record<string, string> = {};
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetch(`${API_BASE}/jobs/${id}/log`, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
  },
  getJobResultsManifest: (id: string) =>
    request<{
      archive_bytes: number;
      entries: { path: string; size: number; is_dir: boolean }[];
    }>(`/jobs/${id}/results/manifest`),
  downloadJobResults: async (id: string): Promise<void> => {
    // Authenticated download → Blob → client-side save (an <a href> wouldn't
    // carry the Bearer token).
    const headers: Record<string, string> = {};
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetch(`${API_BASE}/jobs/${id}/results/download`, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `job_${id}_results.tar.gz`;
    a.click();
    URL.revokeObjectURL(url);
  },
  submitJob: (data: {
    name: string;
    steps: import("@/types").StepConfig[];
    target_pool_id?: string;
    target_node_id?: string;
    priority?: number;
  }) =>
    request<import("@/types").JobInfo>("/jobs", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  cancelJob: (id: string) =>
    request<import("@/types").JobInfo>(`/jobs/${id}/cancel`, { method: "POST" }),
  deleteJob: (id: string) => request<void>(`/jobs/${id}`, { method: "DELETE" }),

  // Steps
  listSteps: () => request<import("@/types").StepSchemaInfo[]>("/steps"),
  getStep: (name: string) => request<import("@/types").StepSchemaInfo>(`/steps/${name}`),

  // Credentials
  listCredentials: () => request<import("@/types").CredentialInfo[]>("/credentials"),
  createCredential: (data: Record<string, unknown>) =>
    request<import("@/types").CredentialInfo>("/credentials", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  deleteCredential: (id: string) =>
    request<void>(`/credentials/${id}`, { method: "DELETE" }),
  testCredential: (id: string) =>
    request<{ success: boolean; error?: string }>(`/credentials/${id}/test`, { method: "POST" }),
  listCredentialTypes: () => request<import("@/types").CredentialTypeInfo[]>("/credentials/types"),

  // Storage
  listBackends: () => request<import("@/types").StorageBackendInfo[]>("/storage/backends"),
  createBackend: (data: Record<string, unknown>) =>
    request<import("@/types").StorageBackendInfo>("/storage/backends", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  deleteBackend: (id: string) =>
    request<void>(`/storage/backends/${id}`, { method: "DELETE" }),
  checkBackendHealth: (id: string) =>
    request<{ healthy: boolean }>(`/storage/backends/${id}/health`),
  transferArtifact: (data: { artifact_id: string; dest_backend_id: string }) =>
    request<import("@/types").TransferInfo>("/storage/transfer", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  listTransfers: () => request<import("@/types").TransferInfo[]>("/storage/transfers"),

  // Artifacts
  listArtifacts: (jobId: string) =>
    request<import("@/types").ArtifactInfo[]>(`/artifacts?job_id=${jobId}`),
};
