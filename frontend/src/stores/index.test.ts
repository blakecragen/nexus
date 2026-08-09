/**
 * Tests for the zustand stores + handleWsMessage dispatcher (src/stores/index.ts).
 *
 * The api client is a true external boundary here (it does network I/O), so we
 * mock "@/api/client" and assert the stores' state transitions after calling
 * their actions. handleWsMessage is tested for routing each WsMessage variant
 * to the correct store mutation.
 *
 * Stores are module singletons, so each test resets the relevant store's state
 * via store.setState(...) in beforeEach.
 *
 * Role in the system: these stores are the single source of truth for every
 * page. Two write paths feed them — user-initiated `fetch()` calls (REST) and
 * `handleWsMessage`, which is driven by frames arriving on the dashboard
 * WebSocket (see hooks/useWebSocket.ts and the server's ws.py). Both paths are
 * covered here; the pages themselves mock the stores away.
 */
import { describe, it, expect, beforeEach, vi, type Mock } from "vitest";
import type { NodeInfo } from "@/types";
import {
  makeUser,
  makeNode,
  makePool,
  makeJob,
  makeStepSchema,
  makeBackend,
  makeCredential,
} from "../test/test-utils";

// ── Mock the api client (external boundary) ──────────────────────────────────
// AI Note: `setToken` is mocked too, which means these tests assert that the
// store *delegates* token storage to the client rather than that the token is
// actually persisted. The real localStorage round-trip is covered in
// api/client.test.ts.
vi.mock("@/api/client", () => ({
  setToken: vi.fn(),
  getToken: vi.fn(),
  api: {
    login: vi.fn(),
    getMe: vi.fn(),
    listNodes: vi.fn(),
    listPools: vi.fn(),
    listJobs: vi.fn(),
    listSteps: vi.fn(),
    listBackends: vi.fn(),
    listCredentials: vi.fn(),
  },
}));

import { api, setToken } from "@/api/client";
import {
  useAuthStore,
  useNodesStore,
  usePoolsStore,
  useJobsStore,
  useStepsStore,
  useStorageStore,
  useCredentialsStore,
  useLiveLogsStore,
  handleWsMessage,
} from "./index";

// Typed handles to the mocked api fns for readability.
/** The mocked `api` object, indexable so tests can reach each method as a Mock. */
const mockApi = api as unknown as Record<string, Mock>;
/** The mocked `setToken`, used to assert auth-token delegation. */
const mockSetToken = setToken as unknown as Mock;

beforeEach(() => {
  vi.clearAllMocks();
  // Reset every singleton store to a known baseline.
  //
  // AI Note: zustand stores are module-level singletons shared by every test in
  // this file, so state leaks between tests unless reset here. `setState` merges
  // (it does not replace), which is why only the data fields are listed — the
  // action functions on each store are preserved.
  useAuthStore.setState({ user: null, isLoading: true });
  useNodesStore.setState({ nodes: [], isLoading: false });
  usePoolsStore.setState({ pools: [], isLoading: false });
  useJobsStore.setState({ jobs: [], isLoading: false });
  useStepsStore.setState({ steps: [], isLoading: false });
  useStorageStore.setState({ backends: [], isLoading: false });
  useCredentialsStore.setState({ credentials: [], isLoading: false });
  useLiveLogsStore.setState({ logs: {} });
});

// ── Auth store ───────────────────────────────────────────────────────────────

/**
 * Session lifecycle: login (token exchange + profile load), logout (full
 * teardown) and fetchUser (session restore on page load).
 */
describe("useAuthStore", () => {
  /**
   * Login is a three-step sequence: exchange credentials, hand the access token
   * to the api client, persist the refresh token, then load the profile.
   *
   * Regression guarded: skipping `setToken` would leave every subsequent
   * request unauthenticated even though the UI believes the user is signed in.
   * The "nexus_refresh" key is a persisted identifier — renaming it invalidates
   * every existing session.
   */
  it("login stores tokens and sets the user", async () => {
    const user = makeUser({ username: "bob" });
    mockApi.login.mockResolvedValue({
      access_token: "access-abc",
      refresh_token: "refresh-xyz",
      token_type: "bearer",
    });
    mockApi.getMe.mockResolvedValue(user);

    await useAuthStore.getState().login("bob", "secret");

    expect(mockApi.login).toHaveBeenCalledWith("bob", "secret");
    expect(mockSetToken).toHaveBeenCalledWith("access-abc");
    expect(localStorage.getItem("nexus_refresh")).toBe("refresh-xyz");
    expect(useAuthStore.getState().user).toEqual(user);
  });

  /**
   * A failed credential exchange must short-circuit: no profile fetch, no user
   * set, and the error propagates so the Login page can display it.
   *
   * Regression guarded (security-relevant): swallowing the rejection would let
   * the app treat a rejected login as a partial success.
   */
  it("login propagates an error from the credentials call and does not set a user", async () => {
    mockApi.login.mockRejectedValue(new Error("bad credentials"));

    await expect(useAuthStore.getState().login("bob", "wrong")).rejects.toThrow(
      "bad credentials"
    );
    expect(mockApi.getMe).not.toHaveBeenCalled();
    expect(useAuthStore.getState().user).toBeNull();
  });

  /**
   * Logout must clear all three pieces of session state — access token, refresh
   * token and cached user. Regression guarded: leaving the refresh token behind
   * means the next page load silently signs the user back in, which is wrong on
   * a shared machine.
   */
  it("logout clears token, refresh storage, and user", () => {
    useAuthStore.setState({ user: makeUser(), isLoading: false });
    localStorage.setItem("nexus_refresh", "refresh-xyz");

    useAuthStore.getState().logout();

    expect(mockSetToken).toHaveBeenCalledWith(null);
    expect(localStorage.getItem("nexus_refresh")).toBeNull();
    expect(useAuthStore.getState().user).toBeNull();
  });

  /**
   * Session restore on app boot: a valid token yields the user and clears the
   * initial `isLoading: true`, which is what un-gates the authenticated routes.
   */
  it("fetchUser sets the user and clears loading on success", async () => {
    const user = makeUser({ username: "carol" });
    mockApi.getMe.mockResolvedValue(user);

    await useAuthStore.getState().fetchUser();

    expect(useAuthStore.getState().user).toEqual(user);
    expect(useAuthStore.getState().isLoading).toBe(false);
  });

  /**
   * An expired token must resolve (not reject) with user cleared and loading
   * finished.
   *
   * AI Note: `fetchUser` deliberately swallows its error — it runs at app boot,
   * where an unhandled rejection would leave `isLoading` true forever and hang
   * the app on a spinner instead of redirecting to /login.
   */
  it("fetchUser clears the user (no throw) when the request fails", async () => {
    useAuthStore.setState({ user: makeUser(), isLoading: true });
    mockApi.getMe.mockRejectedValue(new Error("401"));

    await expect(useAuthStore.getState().fetchUser()).resolves.toBeUndefined();

    expect(useAuthStore.getState().user).toBeNull();
    expect(useAuthStore.getState().isLoading).toBe(false);
  });
});

// ── Nodes store ──────────────────────────────────────────────────────────────

/**
 * Node list state plus `updateNodeStatus`, the mutation driven by live
 * `node.status` WebSocket frames.
 */
describe("useNodesStore", () => {
  /** Happy path: results land in `nodes` and the loading flag is cleared. */
  it("fetch loads nodes and clears loading", async () => {
    const nodes = [makeNode(), makeNode()];
    mockApi.listNodes.mockResolvedValue(nodes);

    await useNodesStore.getState().fetch();

    expect(useNodesStore.getState().nodes).toEqual(nodes);
    expect(useNodesStore.getState().isLoading).toBe(false);
  });

  /**
   * `isLoading` must be set *before* the await, otherwise pages never render a
   * spinner. Gating the mocked api call on a manually-resolved promise is what
   * makes the intermediate state observable.
   */
  it("fetch flips isLoading to true while the request is in flight", async () => {
    // Gate the api call on a promise we resolve manually so we can observe the
    // intermediate state. This proves the store sets isLoading=true *before*
    // awaiting, not just that it ends up false.
    let resolve!: (v: NodeInfo[]) => void;
    mockApi.listNodes.mockReturnValue(new Promise<NodeInfo[]>((r) => (resolve = r)));

    const done = useNodesStore.getState().fetch();
    expect(useNodesStore.getState().isLoading).toBe(true);
    expect(useNodesStore.getState().nodes).toEqual([]);

    resolve([makeNode()]);
    await done;
    expect(useNodesStore.getState().isLoading).toBe(false);
    expect(useNodesStore.getState().nodes).toHaveLength(1);
  });

  /**
   * Characterisation test for a rough edge: `fetch` has no try/catch, so an API
   * failure rejects and leaves `isLoading` stuck at true (spinner forever).
   *
   * AI Note: this asserts the CURRENT behaviour, not the desired one. If error
   * handling is added to the store this test will fail — that failure means
   * "update the expectation", not "revert the fix". The same missing-catch
   * pattern applies to the other stores' `fetch` actions.
   */
  it("fetch rejects (and leaves isLoading stuck true) when the api throws", async () => {
    // The store does not wrap fetch in try/catch, so a rejection propagates and
    // isLoading is never reset. Documenting the real behavior here.
    mockApi.listNodes.mockRejectedValue(new Error("network down"));

    await expect(useNodesStore.getState().fetch()).rejects.toThrow("network down");
    expect(useNodesStore.getState().isLoading).toBe(true);
    expect(useNodesStore.getState().nodes).toEqual([]);
  });

  /** A targeted status update must not disturb sibling nodes. */
  it("updateNodeStatus mutates only the matching node", () => {
    const a = makeNode({ status: "online" });
    const b = makeNode({ status: "online" });
    useNodesStore.setState({ nodes: [a, b] });

    useNodesStore.getState().updateNodeStatus(b.id, "offline");

    const { nodes } = useNodesStore.getState();
    expect(nodes.find((n) => n.id === a.id)!.status).toBe("online");
    expect(nodes.find((n) => n.id === b.id)!.status).toBe("offline");
  });

  /**
   * Unknown ids are ignored silently. This matters because WebSocket frames can
   * reference a node that was added after the last list fetch; the update must
   * not throw or insert a half-populated placeholder row.
   */
  it("updateNodeStatus is a no-op for an unknown node id", () => {
    const a = makeNode({ status: "online" });
    useNodesStore.setState({ nodes: [a] });

    useNodesStore.getState().updateNodeStatus("does-not-exist", "offline");

    expect(useNodesStore.getState().nodes[0].status).toBe("online");
  });

  /**
   * Immutability is a functional requirement, not style: zustand's subscribers
   * re-render on reference change. An in-place mutation would update the data
   * but leave the UI showing the old status until some unrelated render.
   */
  it("updateNodeStatus produces a fresh array and a fresh node object (immutable update)", () => {
    const a = makeNode({ status: "online" });
    const before = [a];
    useNodesStore.setState({ nodes: before });

    useNodesStore.getState().updateNodeStatus(a.id, "offline");

    const after = useNodesStore.getState().nodes;
    // zustand-driven re-renders depend on new references, not in-place mutation.
    expect(after).not.toBe(before);
    expect(after[0]).not.toBe(a);
    expect(a.status).toBe("online"); // original untouched
  });
});

// ── Pools store ──────────────────────────────────────────────────────────────

/** Pools store — same fetch shape as the others; smoke coverage only. */
describe("usePoolsStore", () => {
  /** Results land in `pools` and loading clears. */
  it("fetch loads pools and clears loading", async () => {
    const pools = [makePool()];
    mockApi.listPools.mockResolvedValue(pools);

    await usePoolsStore.getState().fetch();

    expect(usePoolsStore.getState().pools).toEqual(pools);
    expect(usePoolsStore.getState().isLoading).toBe(false);
  });
});

// ── Jobs store ───────────────────────────────────────────────────────────────

/**
 * Jobs store: parameterised fetch (server-side filtering) plus
 * `updateJobStatus`, driven by live `job.status` / `job.completed` frames.
 */
describe("useJobsStore", () => {
  /** Filter params are forwarded verbatim — filtering happens server-side. */
  it("fetch loads jobs and forwards params to the api", async () => {
    const jobs = [makeJob()];
    mockApi.listJobs.mockResolvedValue(jobs);

    await useJobsStore.getState().fetch({ status: "running" });

    expect(mockApi.listJobs).toHaveBeenCalledWith({ status: "running" });
    expect(useJobsStore.getState().jobs).toEqual(jobs);
    expect(useJobsStore.getState().isLoading).toBe(false);
  });

  /**
   * The no-params call passes `undefined` through, which the api client turns
   * into a bare "/api/jobs" (no query string). Regression guarded: substituting
   * an empty object here would still work, but substituting a default filter
   * would silently hide jobs.
   */
  it("fetch with no params forwards undefined to the api", async () => {
    mockApi.listJobs.mockResolvedValue([]);

    await useJobsStore.getState().fetch();

    expect(mockApi.listJobs).toHaveBeenCalledWith(undefined);
    expect(useJobsStore.getState().jobs).toEqual([]);
    expect(useJobsStore.getState().isLoading).toBe(false);
  });

  /** Status and step advance together for the targeted job only. */
  it("updateJobStatus updates status and current_step on the matching job", () => {
    const a = makeJob({ status: "pending", current_step: 0 });
    const b = makeJob({ status: "queued", current_step: 1 });
    useJobsStore.setState({ jobs: [a, b] });

    useJobsStore.getState().updateJobStatus(b.id, "running", 3);

    const { jobs } = useJobsStore.getState();
    const updated = jobs.find((j) => j.id === b.id)!;
    expect(updated.status).toBe("running");
    expect(updated.current_step).toBe(3);
    expect(jobs.find((j) => j.id === a.id)!.status).toBe("pending");
  });

  /**
   * `current_step` is optional and must be preserved when omitted — terminal
   * frames (`job.completed`) carry a status but no step.
   *
   * Regression guarded: defaulting the missing argument to 0/undefined would
   * reset a finished job's progress display back to the first step.
   */
  it("updateJobStatus keeps the existing current_step when none is supplied", () => {
    const a = makeJob({ status: "running", current_step: 2 });
    useJobsStore.setState({ jobs: [a] });

    useJobsStore.getState().updateJobStatus(a.id, "completed");

    const updated = useJobsStore.getState().jobs[0];
    expect(updated.status).toBe("completed");
    expect(updated.current_step).toBe(2);
  });
});

// ── Steps / Storage / Credentials stores ─────────────────────────────────────

/** Step-schema catalogue used by the job builder; smoke coverage of fetch. */
describe("useStepsStore", () => {
  /** Schemas land in `steps` and loading clears. */
  it("fetch loads step schemas and clears loading", async () => {
    const steps = [makeStepSchema()];
    mockApi.listSteps.mockResolvedValue(steps);

    await useStepsStore.getState().fetch();

    expect(useStepsStore.getState().steps).toEqual(steps);
    expect(useStepsStore.getState().isLoading).toBe(false);
  });
});

/** Storage-backend list (MinIO/S3 targets); smoke coverage of fetch. */
describe("useStorageStore", () => {
  /** Backends land in `backends` and loading clears. */
  it("fetch loads backends and clears loading", async () => {
    const backends = [makeBackend()];
    mockApi.listBackends.mockResolvedValue(backends);

    await useStorageStore.getState().fetch();

    expect(useStorageStore.getState().backends).toEqual(backends);
    expect(useStorageStore.getState().isLoading).toBe(false);
  });
});

/** Credential metadata list (secrets themselves stay server-side, encrypted). */
describe("useCredentialsStore", () => {
  /** Credential records land in `credentials` and loading clears. */
  it("fetch loads credentials and clears loading", async () => {
    const credentials = [makeCredential()];
    mockApi.listCredentials.mockResolvedValue(credentials);

    await useCredentialsStore.getState().fetch();

    expect(useCredentialsStore.getState().credentials).toEqual(credentials);
    expect(useCredentialsStore.getState().isLoading).toBe(false);
  });
});

// ── Live logs store ──────────────────────────────────────────────────────────

/**
 * In-memory buffer for streamed step output, keyed `${jobId}:${stepIndex}`.
 *
 * Fed exclusively by `step.log` WebSocket frames and read by the JobDetail
 * log viewer. The composite key is what keeps concurrent steps of the same job
 * (and different jobs) from interleaving into one another's panes.
 */
describe("useLiveLogsStore", () => {
  /** Pins the key format; the log viewer looks lines up by this exact string. */
  it("appendLog keys lines by `${jobId}:${stepIndex}`", () => {
    useLiveLogsStore.getState().appendLog("job-1", 0, "stdout", "hello");

    expect(useLiveLogsStore.getState().logs["job-1:0"]).toEqual([
      { stream: "stdout", line: "hello" },
    ]);
  });

  /**
   * Lines accumulate in arrival order and stdout/stderr share one buffer so the
   * viewer can interleave them faithfully. Regression guarded: an overwrite
   * instead of an append would show only the most recent line.
   */
  it("appendLog appends to the existing buffer for the same key", () => {
    const store = useLiveLogsStore.getState();
    store.appendLog("job-1", 2, "stdout", "first");
    store.appendLog("job-1", 2, "stderr", "second");

    expect(useLiveLogsStore.getState().logs["job-1:2"]).toEqual([
      { stream: "stdout", line: "first" },
      { stream: "stderr", line: "second" },
    ]);
  });

  /** Steps of the same job must stay in separate buffers (no cross-talk). */
  it("appendLog keeps different steps of the same job in separate keys", () => {
    const store = useLiveLogsStore.getState();
    store.appendLog("job-1", 0, "stdout", "step0");
    store.appendLog("job-1", 1, "stdout", "step1");

    const { logs } = useLiveLogsStore.getState();
    expect(logs["job-1:0"]).toEqual([{ stream: "stdout", line: "step0" }]);
    expect(logs["job-1:1"]).toEqual([{ stream: "stdout", line: "step1" }]);
  });

  /**
   * `clearLogs(jobId)` drops every step buffer for that job while leaving other
   * jobs alone — used when a job is re-run so stale output isn't shown.
   *
   * AI Note: the fixture ids ("aaa"/"bbb") are chosen so neither is a prefix of
   * the other. That sidesteps the prefix bug documented by the `it.fails` case
   * immediately below; using "job-1"/"job-10" here would make this test fail
   * for reasons unrelated to what it is checking.
   */
  it("clearLogs removes every key for the given job and leaves others intact", () => {
    // Use job ids that are NOT prefixes of one another to avoid the
    // startsWith() ambiguity (see clearLogs note below).
    useLiveLogsStore.setState({
      logs: {
        "aaa:0": [{ stream: "stdout", line: "x" }],
        "aaa:1": [{ stream: "stderr", line: "y" }],
        "bbb:0": [{ stream: "stdout", line: "z" }],
      },
    });

    useLiveLogsStore.getState().clearLogs("aaa");

    const { logs } = useLiveLogsStore.getState();
    expect(logs["aaa:0"]).toBeUndefined();
    expect(logs["aaa:1"]).toBeUndefined();
    expect(logs["bbb:0"]).toEqual([{ stream: "stdout", line: "z" }]);
  });

  // SOURCE BUG: clearLogs uses key.startsWith(jobId) without the ":" delimiter,
  // so clearLogs("job-1") also wipes logs for "job-10", "job-1a", etc. The
  // correct check would be key.startsWith(`${jobId}:`). This test asserts the
  // *correct* behavior and is expected to FAIL until the source is fixed.
  // (Marked .fails so it documents the bug without masking it or breaking green.)
  //
  // AI Note: `it.fails` inverts the result — the suite goes RED once the source
  // bug is fixed. When fixing clearLogs in src/stores/index.ts, change this back
  // to a plain `it(...)` in the same commit.
  /**
   * Documents a known defect: `clearLogs` matches keys by bare prefix, so
   * clearing job "job-1" also destroys the live log buffer of the unrelated job
   * "job-10". Real UUID job ids make a collision unlikely but not impossible,
   * and it silently blanks a running job's log pane.
   */
  it.fails(
    "clearLogs should NOT delete logs for jobs whose id merely shares a prefix",
    () => {
      useLiveLogsStore.setState({
        logs: {
          "job-1:0": [{ stream: "stdout", line: "target" }],
          "job-10:0": [{ stream: "stdout", line: "bystander" }],
        },
      });

      useLiveLogsStore.getState().clearLogs("job-1");

      const { logs } = useLiveLogsStore.getState();
      expect(logs["job-1:0"]).toBeUndefined();
      // BUG: this currently gets deleted too because "job-10:0".startsWith("job-1").
      expect(logs["job-10:0"]).toEqual([{ stream: "stdout", line: "bystander" }]);
    }
  );
});

// ── handleWsMessage dispatcher ───────────────────────────────────────────────

/**
 * The WebSocket-frame router. `useWebSocket` hands it every parsed frame from
 * /ws/dashboard, and it translates each `type` into the matching store
 * mutation. This is the entire live-update mechanism: an unrouted frame type
 * means that class of update silently stops appearing in the UI.
 */
describe("handleWsMessage", () => {
  /** `node.status` -> nodes store (drives the status dot going red/green live). */
  it("routes node.status to updateNodeStatus", () => {
    const node = makeNode({ status: "online" });
    useNodesStore.setState({ nodes: [node] });

    handleWsMessage({ type: "node.status", node_id: node.id, status: "offline" });

    expect(useNodesStore.getState().nodes[0].status).toBe("offline");
  });

  /** `job.status` -> jobs store, carrying step progress as the job advances. */
  it("routes job.status to updateJobStatus with current_step", () => {
    const job = makeJob({ status: "queued", current_step: 0 });
    useJobsStore.setState({ jobs: [job] });

    handleWsMessage({
      type: "job.status",
      job_id: job.id,
      status: "running",
      current_step: 2,
    });

    const updated = useJobsStore.getState().jobs[0];
    expect(updated.status).toBe("running");
    expect(updated.current_step).toBe(2);
  });

  /** `step.log` -> live-logs buffer under the composite job:step key. */
  it("routes step.log to liveLogs appendLog", () => {
    handleWsMessage({
      type: "step.log",
      job_id: "job-9",
      step_index: 1,
      stream: "stderr",
      line: "boom",
    });

    expect(useLiveLogsStore.getState().logs["job-9:1"]).toEqual([
      { stream: "stderr", line: "boom" },
    ]);
  });

  /**
   * `job.completed` carries no step index, so the job's final `current_step`
   * must survive the transition — the detail view uses it to show which step
   * the job ended on.
   */
  it("routes job.completed to updateJobStatus (status only, step preserved)", () => {
    const job = makeJob({ status: "running", current_step: 4 });
    useJobsStore.setState({ jobs: [job] });

    handleWsMessage({ type: "job.completed", job_id: job.id, status: "completed" });

    const updated = useJobsStore.getState().jobs[0];
    expect(updated.status).toBe("completed");
    expect(updated.current_step).toBe(4);
  });

  /**
   * Forward compatibility: an unrecognised frame type must be ignored, not
   * throw. A newer server emitting a new message type should degrade to "no
   * live update" rather than crashing the dispatcher and killing every
   * subsequent frame.
   */
  it("ignores an unknown message type without mutating any store", () => {
    const node = makeNode({ status: "online" });
    useNodesStore.setState({ nodes: [node] });

    // Cast through unknown: an unrecognized type should hit no switch case.
    handleWsMessage({ type: "totally.unknown" } as unknown as Parameters<
      typeof handleWsMessage
    >[0]);

    expect(useNodesStore.getState().nodes[0].status).toBe("online");
  });
});
