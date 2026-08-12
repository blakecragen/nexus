/**
 * Typed HTTP client for the Nexus server REST API.
 *
 * Role in the system: the single place the frontend talks to the FastAPI server
 * over HTTP. Every page (`@/pages/*`) and every zustand store (`@/stores`) goes
 * through the `api` object exported below; nothing else should call `fetch`
 * against `/api` directly. The live push channel is separate — see
 * `@/hooks/useWebSocket` for the `/ws/dashboard` socket.
 *
 * Responsibilities:
 * - Own the in-memory + localStorage access token (`setToken` / `getToken`).
 * - Attach `Authorization: Bearer <token>` to every request.
 * - Normalise errors into `Error(detail)` so callers can just `catch (e) { e.message }`.
 * - Globally handle 401 by clearing the token and hard-redirecting to `/login`.
 *
 * Neighbours:
 * - Response shapes come from `@/types` (kept in sync by hand with the server's
 *   Pydantic schemas in `packages/common/src/nexus_common/models/schemas.py`).
 * - `@/stores` `useAuthStore.login` calls `api.login` then `setToken`.
 *
 * AI Note: requests use relative paths under `/api`. In dev, `vite.config.ts`
 * proxies `/api` → `http://localhost:8000`; in production the built assets are
 * served from the same origin as the API. There is intentionally no configurable
 * absolute base URL — cross-origin would break the cookie-free Bearer flow's
 * same-origin assumptions and the `/ws` proxy.
 */
import type { TokenResponse } from "@/types";

/** Path prefix every REST call is mounted under (proxied to the FastAPI server). */
const API_BASE = "/api";

/**
 * In-memory copy of the JWT access token, seeded from localStorage at module
 * load so a page refresh keeps the session.
 *
 * AI Note: this is read at module-evaluation time. Tests that stub localStorage
 * must do so before importing this module (or re-import it), otherwise the
 * initial value is captured from the real/previous storage.
 */
let accessToken: string | null = localStorage.getItem("nexus_token");

/**
 * Set (or clear) the access token used for all subsequent requests.
 *
 * Side effects: updates the module-level `accessToken` AND persists to
 * `localStorage["nexus_token"]`; passing `null` removes the key (logout).
 * Callers: `useAuthStore.login` / `useAuthStore.logout` in `@/stores`, and the
 * 401 handler in `request` below.
 */
export function setToken(token: string | null) {
  accessToken = token;
  if (token) {
    localStorage.setItem("nexus_token", token);
  } else {
    localStorage.removeItem("nexus_token");
  }
}

/**
 * Current access token, or null when logged out.
 *
 * Used by the hand-rolled fetch helpers below and by `@/hooks/useWebSocket`,
 * which appends it as a `?token=` query param (the browser WebSocket API cannot
 * send custom headers).
 */
export function getToken(): string | null {
  return accessToken;
}

/**
 * Core JSON request helper — every entry in `api` that returns JSON funnels here.
 *
 * Behaviour:
 * - Sets `Content-Type: application/json` and merges caller-supplied headers.
 * - Adds `Authorization: Bearer <token>` when logged in.
 * - 401 → clears the token and hard-navigates to `/login`, then throws.
 * - Other non-2xx → throws `Error(body.detail)` (falling back to `HTTP <status>`).
 * - 204 → resolves to `undefined` (cast to `T`; callers use `request<void>`).
 *
 * @typeParam T - the expected decoded JSON shape; NOT validated at runtime.
 */
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

  // AI Note: 401 is handled globally with a full page navigation rather than a
  // react-router redirect. That is deliberate: `request` is called from stores
  // and event handlers with no router context, and the hard reload also wipes
  // any stale in-memory store state from the expired session. Consequence: the
  // thrown "Unauthorized" is usually never observed, since the page is already
  // tearing down. There is no automatic refresh-token retry here even though a
  // refresh token is stored under `nexus_refresh` — expiry means re-login.
  if (res.status === 401) {
    setToken(null);
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    // AI Note: FastAPI error bodies are `{detail: ...}` but `detail` may be a
    // string OR a validation-error array/object; `body.detail ||` keeps this
    // from producing "undefined" but a non-string detail stringifies poorly.
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

/**
 * The Nexus REST surface, grouped by resource.
 *
 * Every member is a thin, typed wrapper around one server endpoint. Four of them
 * bypass `request` because they need non-JSON handling (`provisionNode`,
 * `reconnectNode`, `getJobLog`, `downloadJobResults`) — each is documented
 * inline with the reason.
 *
 * AI Note: return types are declared via inline `import("@/types")` rather than
 * top-level imports. This is a style choice in this file, not a constraint; it
 * keeps the import block short. Do not "clean it up" without checking nothing
 * relies on `@/types` staying type-only in this module's import graph.
 */
export const api = {
  // ── Auth ──────────────────────────────────────────────────────────────────
  /**
   * POST /api/auth/login — exchange username/password for access + refresh JWTs.
   * Does NOT store the token; the caller (`useAuthStore.login`) calls `setToken`.
   */
  login: (username: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  /**
   * POST /api/auth/refresh — trade a refresh token for a fresh token pair.
   *
   * AI Note: nothing in the UI currently calls this — `request`'s 401 handler
   * logs the user out instead of refreshing. It is kept so a future silent-refresh
   * interceptor has an endpoint wrapper ready.
   */
  refresh: (refreshToken: string) =>
    request<TokenResponse>("/auth/refresh", {
      method: "POST",
      body: JSON.stringify({ refresh_token: refreshToken }),
    }),
  /** GET /api/auth/me — the authenticated user (id, username, role, is_active). Used by Layout's auth gate. */
  getMe: () => request<import("@/types").UserInfo>("/auth/me"),
  /** POST /api/auth/register — create a user. Admin-only server-side; surfaced on the Admin page. */
  register: (data: { username: string; password: string; email?: string; role?: string }) =>
    request<import("@/types").UserInfo>("/auth/register", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  // ── Nodes ─────────────────────────────────────────────────────────────────
  /**
   * GET /api/nodes — all registered agent nodes.
   * @param params optional query filters (e.g. `{status: "online"}`), serialised
   *   with URLSearchParams; omitted entirely when undefined.
   */
  listNodes: (params?: Record<string, string>) => {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return request<import("@/types").NodeInfo[]>(`/nodes${qs}`);
  },
  /** GET /api/nodes/{id} — one node's full record. */
  getNode: (id: string) => request<import("@/types").NodeInfo>(`/nodes/${id}`),
  /**
   * POST /api/nodes — register a node record without touching the machine.
   *
   * AI Note: the response includes `api_key` — the agent's WebSocket credential.
   * It is returned exactly once, at creation, and is not retrievable later, so
   * the UI must show it to the operator immediately.
   */
  createNode: (data: Record<string, unknown>) =>
    request<import("@/types").NodeInfo & { api_key: string }>("/nodes", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  /**
   * POST /api/nodes/provision — SSH into a host, install the agent, and register it.
   *
   * Returns the node plus `api_key`, `ws_url`, `mode` and the `log` lines from
   * the remote installer. On failure the thrown Error carries a `log?: string[]`
   * property so the Nodes page can render the installer output in the error dialog.
   *
   * AI Note: this cannot use `request<T>` because a FAILED provision is the case
   * we care most about — the server returns 502 with `{detail: {error, log}}` and
   * `request` would collapse that into a bare message, discarding the install log.
   * Long-running: expect tens of seconds while SSH + install run.
   */
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
      // AI Note: `detail` is either a plain string (FastAPI HTTPException) or an
      // object `{error, log}` (this endpoint's structured failure). Both shapes
      // must be handled or the operator sees "[object Object]".
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
  /** DELETE /api/nodes/{id} — deregister a node. 204 → resolves undefined. */
  deleteNode: (id: string) => request<void>(`/nodes/${id}`, { method: "DELETE" }),
  /**
   * POST /api/nodes/{id}/reconnect — re-run agent setup on an existing node
   * (restart the service / repoint it at the server) without re-registering.
   *
   * Same 502-with-log contract as `provisionNode`; the thrown Error carries `log`.
   * Response adds `online` so the UI can tell whether the agent came back.
   */
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
  /**
   * PUT /api/nodes/{id}/maintenance — toggle maintenance mode.
   * A node in maintenance stays connected but the scheduler will not assign it
   * new jobs, so this is the safe way to drain a machine.
   */
  setMaintenance: (id: string, enabled: boolean) =>
    request<import("@/types").NodeInfo>(`/nodes/${id}/maintenance`, {
      method: "PUT",
      body: JSON.stringify({ maintenance: enabled }),
    }),

  // ── Pools ─────────────────────────────────────────────────────────────────
  /** GET /api/pools — all pools with their `node_count`. */
  listPools: () => request<import("@/types").PoolInfo[]>("/pools"),
  /** GET /api/pools/{id} — one pool plus the full node records of its members. */
  getPool: (id: string) =>
    request<{ pool: import("@/types").PoolInfo; nodes: import("@/types").NodeInfo[] }>(`/pools/${id}`),
  /** POST /api/pools — create a pool. Jobs target either a pool or a single node. */
  createPool: (data: { name: string; description?: string }) =>
    request<import("@/types").PoolInfo>("/pools", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  /** DELETE /api/pools/{id} — delete a pool (members are unassigned, not deleted). */
  deletePool: (id: string) => request<void>(`/pools/${id}`, { method: "DELETE" }),
  /** POST /api/pools/{poolId}/nodes — add a node to a pool's membership. */
  addNodeToPool: (poolId: string, nodeId: string) =>
    request<void>(`/pools/${poolId}/nodes`, {
      method: "POST",
      body: JSON.stringify({ node_id: nodeId }),
    }),
  /** DELETE /api/pools/{poolId}/nodes/{nodeId} — remove a node from a pool. */
  removeNodeFromPool: (poolId: string, nodeId: string) =>
    request<void>(`/pools/${poolId}/nodes/${nodeId}`, { method: "DELETE" }),

  // ── Jobs ──────────────────────────────────────────────────────────────────
  /**
   * GET /api/jobs — job list, newest first (server-ordered).
   * @param params optional filters such as `{status: "running"}`.
   */
  listJobs: (params?: Record<string, string>) => {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return request<import("@/types").JobInfo[]>(`/jobs${qs}`);
  },
  /** GET /api/jobs/{id} — job + per-step runs + accumulated context, plus `has_log` / `has_results` flags the detail page uses to decide which tabs to show. */
  getJob: (id: string) => request<import("@/types").JobDetail>(`/jobs/${id}`),
  /**
   * GET /api/jobs/{id}/log — the job's full terminal transcript as plain text.
   *
   * AI Note: hand-rolled because the endpoint returns `text/plain`, so
   * `request<T>` (which unconditionally calls `res.json()`) would throw. Also
   * note no `Content-Type` request header is set — sending one on a GET with no
   * body is pointless and some proxies dislike it.
   */
  getJobLog: async (id: string): Promise<string> => {
    // Plain-text endpoint — can't use request<T> (which does res.json()).
    const headers: Record<string, string> = {};
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetch(`${API_BASE}/jobs/${id}/log`, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
  },
  /**
   * GET /api/jobs/{id}/results/manifest — listing of the results tarball
   * (total `archive_bytes` plus each entry's path/size/is_dir), so the
   * ResultsTree component can render a file tree without downloading the archive.
   */
  getJobResultsManifest: (id: string) =>
    request<{
      archive_bytes: number;
      entries: { path: string; size: number; is_dir: boolean }[];
    }>(`/jobs/${id}/results/manifest`),
  /**
   * GET /api/jobs/{id}/results/download — fetch the results tarball and trigger
   * a browser save as `job_<id>_results.tar.gz`.
   *
   * Side effects: creates a detached `<a>` and clicks it; revokes the object URL.
   *
   * AI Note: a plain `<a href>` link cannot be used because the endpoint requires
   * the `Authorization` header and the browser will not attach it to navigations.
   * Hence: authenticated fetch → Blob → object URL → synthetic click.
   *
   * AI Note: `revokeObjectURL` runs immediately after `click()`. That works
   * because the click starts the download synchronously, but it is the fragile
   * part of this function — if a browser ever defers the download, the URL will
   * already be dead. Buffers the whole archive in memory, so very large result
   * sets can spike RAM.
   */
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
  /**
   * POST /api/jobs — submit a job for scheduling.
   *
   * @param data.steps ordered step configs from the Job Builder; the server
   *   validates each step's params against its registered schema at submit time
   *   (including OUTPUT_KEYS chaining between steps) and rejects the whole job.
   * @param data.target_pool_id / data.target_node_id — pick exactly one; a pool
   *   lets the scheduler choose any eligible member.
   * @param data.priority higher runs first among queued jobs.
   */
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
  /**
   * POST /api/jobs/{id}/requeue — re-submit this job's plan as a brand new job.
   *
   * Takes no body: name, steps, pool/node pin, priority and storage target are
   * all copied verbatim server-side. Returns the NEW job (a different id) —
   * never the original, which is left untouched.
   *
   * Rejects with 400 if the stored plan no longer validates against the current
   * step registry (a step type since renamed or removed), which is the same
   * error shape POST /api/jobs produces.
   */
  requeueJob: (id: string) =>
    request<import("@/types").JobInfo>(`/jobs/${id}/requeue`, { method: "POST" }),
  /** POST /api/jobs/{id}/cancel — request cancellation; returns the updated job. Only meaningful for pending/queued/running jobs. */
  cancelJob: (id: string) =>
    request<import("@/types").JobInfo>(`/jobs/${id}/cancel`, { method: "POST" }),
  /** DELETE /api/jobs/{id} — permanently remove a job and its step runs. */
  deleteJob: (id: string) => request<void>(`/jobs/${id}`, { method: "DELETE" }),

  // ── Steps ─────────────────────────────────────────────────────────────────
  /** GET /api/steps — every registered step type's schema. Drives the Job Builder palette and its param forms. */
  listSteps: () => request<import("@/types").StepSchemaInfo[]>("/steps"),
  /** GET /api/steps/{name} — one step's schema (fields, rules, OS variants, output keys). */
  getStep: (name: string) => request<import("@/types").StepSchemaInfo>(`/steps/${name}`),

  // ── Credentials ───────────────────────────────────────────────────────────
  /**
   * GET /api/credentials — credential metadata only.
   *
   * AI Note: secret values are encrypted at rest server-side and are never
   * returned by this endpoint — `CredentialInfo` has no secret field by design.
   * Do not add one.
   */
  listCredentials: () => request<import("@/types").CredentialInfo[]>("/credentials"),
  /** POST /api/credentials — store a new credential; the request body is the only time the secret crosses the wire from the browser. */
  createCredential: (data: Record<string, unknown>) =>
    request<import("@/types").CredentialInfo>("/credentials", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  /** DELETE /api/credentials/{id} — delete a credential. Storage backends referencing it will start failing health checks. */
  deleteCredential: (id: string) =>
    request<void>(`/credentials/${id}`, { method: "DELETE" }),
  /** POST /api/credentials/{id}/test — server-side connectivity probe; `{success, error?}` rather than an HTTP error, so a failed probe is a 200. */
  testCredential: (id: string) =>
    request<{ success: boolean; error?: string }>(`/credentials/${id}/test`, { method: "POST" }),
  /**
   * GET /api/credentials/types — the credential kinds the server supports and
   * their required/optional fields; drives the dynamic "add credential" form.
   *
   * AI Note: this path is a sibling of `/credentials/{id}`. The server must
   * declare the `/types` route before the `{id}` route or "types" gets parsed as
   * an id — worth remembering if these routes are ever reorganised.
   */
  listCredentialTypes: () => request<import("@/types").CredentialTypeInfo[]>("/credentials/types"),

  // ── Storage ───────────────────────────────────────────────────────────────
  /** GET /api/storage/backends — configured artifact stores (local/S3/MinIO/...) with capacity and usage. */
  listBackends: () => request<import("@/types").StorageBackendInfo[]>("/storage/backends"),
  /** POST /api/storage/backends — register a backend; `credential_id` must point at an existing credential. */
  createBackend: (data: Record<string, unknown>) =>
    request<import("@/types").StorageBackendInfo>("/storage/backends", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  /** DELETE /api/storage/backends/{id} — remove a backend. Artifacts already stored there become unreachable. */
  deleteBackend: (id: string) =>
    request<void>(`/storage/backends/${id}`, { method: "DELETE" }),
  /** GET /api/storage/backends/{id}/health — live reachability probe; returns `{healthy}` (a 200 even when unhealthy). */
  checkBackendHealth: (id: string) =>
    request<{ healthy: boolean }>(`/storage/backends/${id}/health`),
  /** POST /api/storage/transfer — start copying an artifact to another backend; returns the transfer record to poll via `listTransfers`. */
  transferArtifact: (data: { artifact_id: string; dest_backend_id: string }) =>
    request<import("@/types").TransferInfo>("/storage/transfer", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  /** GET /api/storage/transfers — all transfers with status and bytes moved. The Storage page polls this; there is no WS event for transfers. */
  listTransfers: () => request<import("@/types").TransferInfo[]>("/storage/transfers"),

  // ── Artifacts ─────────────────────────────────────────────────────────────
  /**
   * GET /api/artifacts?job_id=... — files a job produced.
   *
   * AI Note: `jobId` is interpolated straight into the query string without
   * encodeURIComponent. Safe today because ids are server-generated UUIDs, but
   * it would break for any free-form identifier.
   */
  listArtifacts: (jobId: string) =>
    request<import("@/types").ArtifactInfo[]>(`/artifacts?job_id=${jobId}`),
};
