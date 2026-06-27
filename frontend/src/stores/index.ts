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

interface AuthState {
  user: UserInfo | null;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  fetchUser: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isLoading: true,
  login: async (username, password) => {
    const tokens = await api.login(username, password);
    setToken(tokens.access_token);
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
      set({ user: null, isLoading: false });
    }
  },
}));

// ── Nodes Store ────────────────────────────────────────────────────────

interface NodesState {
  nodes: NodeInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
  updateNodeStatus: (nodeId: string, status: string) => void;
}

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

interface PoolsState {
  pools: PoolInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
}

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

interface JobsState {
  jobs: JobInfo[];
  isLoading: boolean;
  fetch: (params?: Record<string, string>) => Promise<void>;
  updateJobStatus: (jobId: string, status: string, currentStep?: number) => void;
}

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

interface StepsState {
  steps: StepSchemaInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
}

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

interface StorageState {
  backends: StorageBackendInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
}

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

interface CredentialsState {
  credentials: CredentialInfo[];
  isLoading: boolean;
  fetch: () => Promise<void>;
}

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

interface LogLine {
  stream: "stdout" | "stderr";
  line: string;
}

interface LiveLogsState {
  logs: Record<string, LogLine[]>; // keyed by `${jobId}:${stepIndex}`
  appendLog: (jobId: string, stepIndex: number, stream: "stdout" | "stderr", line: string) => void;
  clearLogs: (jobId: string) => void;
}

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
        if (key.startsWith(jobId)) delete logs[key];
      }
      return { logs };
    });
  },
}));

// ── WebSocket message dispatcher ───────────────────────────────────────

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
