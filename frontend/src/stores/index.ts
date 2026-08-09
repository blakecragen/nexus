/**
 * Global client state for the Nexus dashboard — a set of small zustand stores
 * plus the WebSocket message dispatcher that keeps them live.
 *
 * Role in the system: this is the seam between `@/api/client` (HTTP reads) and
 * the pages that render cluster data. Each store owns one resource collection,
 * exposes an async `fetch()` that calls the matching `api.*` method, and (where
 * the server pushes updates) a synchronous mutator the WebSocket feed calls.
 *
 * Data flow:
 *
 *   pages ──fetch()──> api.* ──HTTP──> server
 *   server ──/ws/dashboard──> useWebSocket (Layout) ──> handleWsMessage ──> stores ──> re-render
 *
 * AI Note: these stores are MODULE SINGLETONS, not React context. Two important
 * consequences: (1) `handleWsMessage` can mutate them from outside React via
 * `useXStore.getState()`, which is exactly why the dispatcher at the bottom of
 * this file works; (2) state survives navigation but NOT a page reload, and it
 * is shared by every component — including across a logout, so any new store
 * holding user-scoped data should be cleared in `useAuthStore.logout`.
 *
 * AI Note: none of the `fetch()` actions catch errors (except `fetchUser`). A
 * rejected request leaves `isLoading` stuck at `true` and rejects the promise
 * into the caller's effect. Pages are expected to `.catch()` themselves.
 */
import { create } from "zustand";
import type {
  UserInfo,
  NodeInfo,
  PoolInfo,
  JobInfo,
  StepSchemaInfo,
  StorageBackendInfo,
  CredentialInfo,
  WsMessage,
} from "@/types";
import { api, setToken } from "@/api/client";

// ── Auth Store ─────────────────────────────────────────────────────────

/**
 * Auth state: who is signed in, and whether we know yet.
 *
 * `isLoading` starts `true` so `@/components/Layout` can show a loading screen
 * instead of flashing the login page before /auth/me answers.
 */
interface AuthState {
  user: UserInfo | null;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  fetchUser: () => Promise<void>;
}

/**
 * `useAuthStore` — session state consumed by `Layout` (gate + sidebar footer)
 * and `@/pages/Login`.
 *
 * Actions:
 * - `login(username, password)` → POST /api/auth/login, persists the access
 *   token via `setToken` and the refresh token to localStorage, then GETs
 *   /api/auth/me and stores the user. Rejects on bad credentials; the Login
 *   page catches and renders the message.
 * - `logout()` → clears both tokens and `user`. Synchronous, no server call —
 *   JWTs are stateless so there is nothing to revoke.
 * - `fetchUser()` → GET /api/auth/me; on ANY failure sets `user: null` rather
 *   than rejecting, so the Layout gate always resolves to a decision.
 *
 * AI Note: `login` deliberately does NOT set `isLoading`. It is only ever called
 * from the Login page (which owns its own submitting state), and flipping
 * `isLoading` here would make Layout render its loading screen mid-transition.
 *
 * AI Note: `login` awaits `getMe()` after `setToken`. If that second call fails
 * the tokens are already persisted but `user` stays null — the user is "logged
 * in" per localStorage yet sees the login page until a refresh.
 */
export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isLoading: true,
  login: async (username, password) => {
    const tokens = await api.login(username, password);
    setToken(tokens.access_token);
    // AI Note: the refresh token is stored under a different key than the access
    // token (which `setToken` owns as "nexus_token"). Nothing consumes it yet —
    // `@/api/client` has no silent-refresh path — but `logout` must keep clearing
    // it or a stale refresh token outlives the session.
    localStorage.setItem("nexus_refresh", tokens.refresh_token);
    const user = await api.getMe();
    set({ user });
  },
  logout: () => {
    setToken(null);
    localStorage.removeItem("nexus_refresh");
    set({ user: null });
  },
  fetchUser: async () => {
    try {
      const user = await api.getMe();
      set({ user, isLoading: false });
    } catch {
      // AI Note: swallowing the error is the point — a 401 here just means "not
      // logged in", and `request` has already cleared the token and started a
      // redirect to /login. Setting `isLoading: false` in BOTH branches is what
      // guarantees Layout escapes its loading screen.
      set({ user: null, isLoading: false });
    }
  },
}));

// ── Nodes Store ────────────────────────────────────────────────────────

/** Registered agent nodes, plus the WS-driven status patch. */
interface NodesState {
  nodes: NodeInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
  updateNodeStatus: (nodeId: string, status: string) => void;
}

/**
 * `useNodesStore` — the node list shown on `@/pages/Nodes` and summarised on the
 * Dashboard.
 *
 * - `fetch()` → GET /api/nodes (full refresh; replaces the array).
 * - `updateNodeStatus(nodeId, status)` → applied by `handleWsMessage` on a
 *   `node.status` event so online/offline/busy badges flip without polling.
 *
 * AI Note: `updateNodeStatus` only PATCHES nodes already in the array — a node
 * registered after the last `fetch()` is dropped on the floor. Pages that need
 * to see brand-new nodes must re-`fetch()`; the WS feed alone will not surface
 * them. Same reasoning applies to deletions.
 *
 * AI Note: the `status as NodeInfo["status"]` cast is unchecked. The action
 * takes a plain `string` because it is called from the WS dispatcher, and a
 * server-side enum change would silently store an invalid status here.
 */
export const useNodesStore = create<NodesState>((set) => ({
  nodes: [],
  isLoading: false,
  fetch: async () => {
    set({ isLoading: true });
    const nodes = await api.listNodes();
    set({ nodes, isLoading: false });
  },
  updateNodeStatus: (nodeId, status) => {
    set((state) => ({
      nodes: state.nodes.map((n) =>
        n.id === nodeId ? { ...n, status: status as NodeInfo["status"] } : n
      ),
    }));
  },
}));

// ── Pools Store ────────────────────────────────────────────────────────

/** Node pools (scheduling targets). Read-only in the store; CRUD goes straight through `api`. */
interface PoolsState {
  pools: PoolInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
}

/**
 * `usePoolsStore` — pool list for `@/pages/Pools` and the Job Builder's target
 * selector. `fetch()` → GET /api/pools.
 *
 * No mutators: pool create/delete/membership calls go directly to `api` from the
 * page, which then re-`fetch()`es. There are no pool WebSocket events.
 */
export const usePoolsStore = create<PoolsState>((set) => ({
  pools: [],
  isLoading: false,
  fetch: async () => {
    set({ isLoading: true });
    const pools = await api.listPools();
    set({ pools, isLoading: false });
  },
}));

// ── Jobs Store ─────────────────────────────────────────────────────────

/** Job list, plus the WS-driven status/progress patch. */
interface JobsState {
  jobs: JobInfo[];
  isLoading: boolean;
  fetch: (params?: Record<string, string>) => Promise<void>;
  updateJobStatus: (jobId: string, status: string, currentStep?: number) => void;
}

/**
 * `useJobsStore` — the job table on `@/pages/Jobs` and the Dashboard's recent
 * activity.
 *
 * - `fetch(params?)` → GET /api/jobs with optional filters (e.g. `{status}`).
 * - `updateJobStatus(jobId, status, currentStep?)` → called by `handleWsMessage`
 *   for both `job.status` and `job.completed`, so a running job's badge and
 *   step counter advance live.
 *
 * AI Note: `currentStep ?? j.current_step` uses `??` (not `||`) on purpose —
 * step index 0 is a legitimate value and `||` would discard it, freezing the
 * progress display on the first step. `job.completed` events carry no step, so
 * the fallback preserves the last known one.
 *
 * AI Note: like the nodes store, this only patches jobs already loaded; a job
 * submitted in another tab never appears until the next `fetch()`.
 */
export const useJobsStore = create<JobsState>((set) => ({
  jobs: [],
  isLoading: false,
  fetch: async (params) => {
    set({ isLoading: true });
    const jobs = await api.listJobs(params);
    set({ jobs, isLoading: false });
  },
  updateJobStatus: (jobId, status, currentStep) => {
    set((state) => ({
      jobs: state.jobs.map((j) =>
        j.id === jobId
          ? { ...j, status: status as JobInfo["status"], current_step: currentStep ?? j.current_step }
          : j
      ),
    }));
  },
}));

// ── Steps Store ────────────────────────────────────────────────────────

/** Registered step-type schemas (the Job Builder's palette). */
interface StepsState {
  steps: StepSchemaInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
}

/**
 * `useStepsStore` — the catalogue of step types the server knows how to run.
 * `fetch()` → GET /api/steps.
 *
 * `@/pages/JobBuilder` reads this to build the draggable step palette and to
 * generate each step's parameter form from `fields` / `rules` / `os_variants`.
 * Effectively static for a given server build, so one fetch per page visit is fine.
 */
export const useStepsStore = create<StepsState>((set) => ({
  steps: [],
  isLoading: false,
  fetch: async () => {
    set({ isLoading: true });
    const steps = await api.listSteps();
    set({ steps, isLoading: false });
  },
}));

// ── Storage Store ──────────────────────────────────────────────────────

/** Configured artifact storage backends. */
interface StorageState {
  backends: StorageBackendInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
}

/**
 * `useStorageStore` — storage backends for `@/pages/Storage`.
 * `fetch()` → GET /api/storage/backends.
 *
 * Health (`api.checkBackendHealth`) and transfers (`api.listTransfers`) are
 * intentionally NOT kept here: they are live probes/polls owned by the page, and
 * caching them in a singleton would show stale health after navigation.
 */
export const useStorageStore = create<StorageState>((set) => ({
  backends: [],
  isLoading: false,
  fetch: async () => {
    set({ isLoading: true });
    const backends = await api.listBackends();
    set({ backends, isLoading: false });
  },
}));

// ── Credentials Store ──────────────────────────────────────────────────

/** Stored credentials — metadata only, never secret material. */
interface CredentialsState {
  credentials: CredentialInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
}

/**
 * `useCredentialsStore` — credential list for `@/pages/Admin` and the storage
 * backend form. `fetch()` → GET /api/credentials.
 *
 * AI Note: `CredentialInfo` carries no secret fields — the server encrypts
 * secrets at rest and never returns them. Do not extend this store to hold
 * plaintext secrets; it is a module singleton readable from any component and
 * it survives navigation.
 */
export const useCredentialsStore = create<CredentialsState>((set) => ({
  credentials: [],
  isLoading: false,
  fetch: async () => {
    set({ isLoading: true });
    const credentials = await api.listCredentials();
    set({ credentials, isLoading: false });
  },
}));

// ── Live Logs Store (for job detail page) ──────────────────────────────

/** One streamed output line, tagged with the pipe it came from. */
interface LogLine {
  stream: "stdout" | "stderr";
  line: string;
}

/**
 * Buffer of log lines pushed over the WebSocket while a job runs.
 *
 * Keys are `${jobId}:${stepIndex}` so each step's output is separate; the Job
 * Detail page renders the entry for the step the user has expanded.
 */
interface LiveLogsState {
  logs: Record<string, LogLine[]>; // keyed by `${jobId}:${stepIndex}`
  appendLog: (jobId: string, stepIndex: number, stream: "stdout" | "stderr", line: string) => void;
  clearLogs: (jobId: string) => void;
}

/**
 * `useLiveLogsStore` — in-memory tail of running-job output, fed exclusively by
 * `step.log` WebSocket events and read by `@/pages/JobDetail`.
 *
 * - `appendLog(jobId, stepIndex, stream, line)` — push one line onto that step's
 *   buffer, creating it on first use.
 * - `clearLogs(jobId)` — drop every buffer belonging to a job (called when the
 *   detail page mounts/switches jobs, so a re-visit does not show doubled output).
 *
 * AI Note: this is ephemeral and unbounded. It is NOT the source of truth — the
 * durable transcript is `Job.log_text`, fetched with `api.getJobLog`. Lines that
 * arrive while the page is closed are simply never buffered, which is fine
 * because the detail page falls back to the server-side log. A very chatty job
 * left open for a long time will grow this map without limit; there is no cap
 * or ring-buffer today.
 *
 * AI Note: `appendLog` copies both the outer map and the target array on every
 * single line (`[...(state.logs[key] || []), ...]`). That immutability is what
 * makes zustand notify subscribers, but it makes appends O(n) — a step emitting
 * tens of thousands of lines will visibly degrade.
 */
export const useLiveLogsStore = create<LiveLogsState>((set) => ({
  logs: {},
  appendLog: (jobId, stepIndex, stream, line) => {
    const key = `${jobId}:${stepIndex}`;
    set((state) => ({
      logs: {
        ...state.logs,
        [key]: [...(state.logs[key] || []), { stream, line }],
      },
    }));
  },
  clearLogs: (jobId) => {
    set((state) => {
      const logs = { ...state.logs };
      for (const key of Object.keys(logs)) {
        // AI Note: matches on a bare `startsWith(jobId)` with no ":" delimiter,
        // so `clearLogs("job-1")` also wipes "job-10:0" and "job-1a:0". Harmless
        // with server-generated UUIDs (no UUID is a prefix of another), but it
        // breaks immediately for any human-readable job id. The frontend test
        // suite documents this as a known source bug — do not "fix" it here
        // without updating stores/index.test.ts, which asserts current behaviour.
        if (key.startsWith(jobId)) delete logs[key];
      }
      return { logs };
    });
  },
}));

// ── WebSocket message dispatcher ───────────────────────────────────────

/**
 * Fan a single `/ws/dashboard` message out to the store that owns it.
 *
 * Wired up once, in `@/components/Layout`: `useWebSocket(handleWsMessage)`.
 * Message shapes are the discriminated union `WsMessage` in `@/types`, mirroring
 * the server broadcasts in `packages/server/src/nexus_server/api/routes/ws.py`.
 *
 * Routing:
 * - `node.status`    → `useNodesStore.updateNodeStatus`
 * - `job.status`     → `useJobsStore.updateJobStatus` (with `current_step`)
 * - `step.log`       → `useLiveLogsStore.appendLog`
 * - `job.completed`  → `useJobsStore.updateJobStatus` (terminal status, no step)
 *
 * AI Note: must stay a MODULE-LEVEL function with a stable identity. It is the
 * `onMessage` argument to `useWebSocket`, which lists it as a `useCallback`
 * dependency — an inline or re-created handler would tear down and reopen the
 * dashboard socket on every Layout render.
 *
 * AI Note: uses `useXStore.getState()` rather than hooks because this runs
 * outside React (in a WebSocket event handler). Calling the hooks here would
 * violate the rules of hooks.
 *
 * AI Note: unknown `msg.type` values fall through silently — no default case,
 * no logging. That is intentional so a newer server can add event types without
 * breaking an older dashboard, but it does mean a typo'd type is invisible.
 */
export function handleWsMessage(msg: WsMessage) {
  switch (msg.type) {
    case "node.status":
      useNodesStore.getState().updateNodeStatus(msg.node_id, msg.status);
      break;
    case "job.status":
      useJobsStore.getState().updateJobStatus(msg.job_id, msg.status, msg.current_step);
      break;
    case "step.log":
      useLiveLogsStore.getState().appendLog(msg.job_id, msg.step_index, msg.stream, msg.line);
      break;
    case "job.completed":
      useJobsStore.getState().updateJobStatus(msg.job_id, msg.status);
      break;
  }
}
