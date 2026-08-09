/**
 * Tests for the Dashboard page (src/pages/Dashboard.tsx).
 *
 * Dashboard pulls from four zustand stores (nodes, jobs, pools, storage) using
 * selector functions (s => s.x), fires each store's fetch() on mount, and renders
 * summary stat cards plus a "Recent Jobs" table (top 10 by created_at desc).
 *
 * We mock the store boundary so each hook applies the page's selector to seeded
 * state, then assert the real rendered output with resilient role/text selectors.
 *
 * What is real vs stubbed: the page component, its derived counts/sorting, the
 * Tailwind class output and react-router navigation are all real. Only `@/stores`
 * is replaced — which also means `@/api/client` is never reached, so no test here
 * can hit the network.
 */
import { vi, describe, it, expect, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useParams } from "react-router-dom";
import type { ReactElement } from "react";
import userEvent from "@testing-library/user-event";
import { renderWithRouter, screen, within, makeNode, makeJob, makePool, makeBackend } from "../test/test-utils";
import type { NodeInfo, JobInfo, PoolInfo, StorageBackendInfo } from "@/types";
import { formatRelativeTime } from "@/lib/utils";

// ── Mock the store boundary ──────────────────────────────────────────────────
// The page calls each store as a hook with a selector: useXStore((s) => s.field).
// Our mock applies that selector to the current seeded state so selectors like
// (s) => s.nodes and (s) => s.fetch both resolve correctly.

/** Per-store `fetch` spies; the page must call each exactly once on mount. */
const fetchNodes = vi.fn().mockResolvedValue(undefined);
const fetchJobs = vi.fn().mockResolvedValue(undefined);
const fetchPools = vi.fn().mockResolvedValue(undefined);
const fetchBackends = vi.fn().mockResolvedValue(undefined);

/**
 * Mutable state backing each mocked store, re-seeded in `beforeEach`.
 *
 * AI Note: these are reassigned (not mutated) in `beforeEach`, and `makeStore`
 * below closes over a getter rather than the value — so the mock always reads
 * the *current* object. Capturing the value directly would freeze every test on
 * the first seed.
 */
let nodesState: { nodes: NodeInfo[]; isLoading: boolean; fetch: typeof fetchNodes };
let jobsState: { jobs: JobInfo[]; isLoading: boolean; fetch: typeof fetchJobs };
let poolsState: { pools: PoolInfo[]; isLoading: boolean; fetch: typeof fetchPools };
let storageState: { backends: StorageBackendInfo[]; isLoading: boolean; fetch: typeof fetchBackends };

/**
 * Build a stand-in for a zustand hook.
 *
 * Real zustand hooks are callable either bare (`useStore()`) or with a selector
 * (`useStore(s => s.jobs)`). Dashboard uses the selector form for both data and
 * actions, so the mock must apply the selector to live state rather than return
 * a fixed object.
 *
 * @param getState lazily resolves the current seeded state for this store.
 */
function makeStore<T>(getState: () => T) {
  // Mimic a zustand hook: invoked with a selector, returns selector(state).
  return vi.fn((selector?: (s: T) => unknown) =>
    selector ? selector(getState()) : getState()
  );
}

vi.mock("@/stores", () => ({
  useNodesStore: makeStore(() => nodesState),
  useJobsStore: makeStore(() => jobsState),
  usePoolsStore: makeStore(() => poolsState),
  useStorageStore: makeStore(() => storageState),
}));

import Dashboard from "./Dashboard";

// ── Local multi-route render helper ──────────────────────────────────────────
// test-utils' renderWithRouter only supports a single route; here we mount the
// Dashboard at "/" alongside extra routes so we can assert real navigation.

/**
 * Render `ui` at "/" inside a MemoryRouter together with additional routes.
 *
 * Needed for the row-click test: proving navigation actually landed on
 * `/jobs/:id` requires a second route to render into. The shared
 * `renderWithRouter` only mounts one component, so it can only show that the
 * dashboard disappeared — not where the user ended up.
 *
 * @param ui           component mounted at "/"
 * @param extraRoutes  additional `{path, element}` routes to register
 * @returns `{user, ...renderResult}` with a per-render `userEvent` instance
 */
function renderWithRouterRoutes(
  ui: ReactElement,
  extraRoutes: { path: string; element: ReactElement }[]
) {
  const user = userEvent.setup();
  const view = render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={ui} />
        {extraRoutes.map((r) => (
          <Route key={r.path} path={r.path} element={r.element} />
        ))}
      </Routes>
    </MemoryRouter>
  );
  return { user, ...view };
}

// Sentinel page that echoes the :id it was navigated to.
/**
 * Minimal stand-in for the real JobDetail page: renders the `:id` route param
 * into a testid so a test can assert *which* job was navigated to, without
 * pulling in JobDetail's own stores, WebSocket usage and API calls.
 */
function JobDetailSentinel() {
  const { id } = useParams();
  return <div data-testid="job-detail-sentinel">job:{id}</div>;
}

beforeEach(() => {
  fetchNodes.mockClear();
  fetchJobs.mockClear();
  fetchPools.mockClear();
  fetchBackends.mockClear();
  nodesState = { nodes: [], isLoading: false, fetch: fetchNodes };
  jobsState = { jobs: [], isLoading: false, fetch: fetchJobs };
  poolsState = { pools: [], isLoading: false, fetch: fetchPools };
  storageState = { backends: [], isLoading: false, fetch: fetchBackends };
});

// Helper: locate the stat card by its label, then read its numeric value.
/**
 * Read the big number out of the stat card identified by its label text.
 *
 * Walks from the label up to the enclosing `.bg-card` container and then picks
 * the first purely-numeric text node inside it. This scoping matters: several
 * cards can legitimately show the same number, so a global `getByText("2")`
 * would be ambiguous or match the wrong card.
 *
 * AI Note: the `?? labelEl.parentElement!.parentElement!` fallback exists
 * because the card wrapper's class list is a styling detail. If the Dashboard
 * markup is restructured, this helper degrades to a two-level walk instead of
 * throwing — which can silently read from the wrong element, so re-check it
 * when card markup changes.
 *
 * @param label exact text or pattern identifying the card (e.g. /nodes online/i)
 * @returns the card's numeric value as rendered text
 */
function statValue(label: string | RegExp): string {
  const labelEl = screen.getByText(label);
  const card = labelEl.closest("div.bg-card") ?? labelEl.parentElement!.parentElement!;
  // The value is the only large number paragraph in the card.
  const value = within(card as HTMLElement).getAllByText(/^\d+$/)[0];
  return value.textContent ?? "";
}

describe("Dashboard page", () => {
  /** Smoke check that the page mounts and is identifiable by its heading. */
  it("renders the page heading", () => {
    renderWithRouter(<Dashboard />);
    expect(screen.getByRole("heading", { name: /^dashboard$/i })).toBeInTheDocument();
  });

  /**
   * The mount effect must load all four data sources — and only once.
   * Regression guarded: a missing dependency array would refetch on every
   * render (four request storms per keystroke elsewhere in the tree), while a
   * dropped call leaves a permanently empty stat card.
   */
  it("fetches all four data sources exactly once on mount", () => {
    renderWithRouter(<Dashboard />);
    // The mount effect should fire each store's fetch once — not zero, not many.
    expect(fetchNodes).toHaveBeenCalledTimes(1);
    expect(fetchJobs).toHaveBeenCalledTimes(1);
    expect(fetchPools).toHaveBeenCalledTimes(1);
    expect(fetchBackends).toHaveBeenCalledTimes(1);
  });

  /** All four summary cards are present, independent of their values. */
  it("renders all four stat card labels", () => {
    renderWithRouter(<Dashboard />);
    expect(screen.getByText(/nodes online/i)).toBeInTheDocument();
    expect(screen.getByText(/active jobs/i)).toBeInTheDocument();
    expect(screen.getByText(/total pools/i)).toBeInTheDocument();
    expect(screen.getByText(/storage backends/i)).toBeInTheDocument();
  });

  /**
   * "Nodes Online" counts both `online` and `busy` — a busy node is still
   * reachable and schedulable. Regression guarded: counting only `online` would
   * make a fully-utilised cluster look like it had zero capacity.
   */
  it("counts online + busy nodes for 'Nodes Online'", () => {
    nodesState.nodes = [
      makeNode({ status: "online" }),
      makeNode({ status: "busy" }),
      makeNode({ status: "offline" }),
    ];
    renderWithRouter(<Dashboard />);
    // online + busy = 2 (offline excluded).
    expect(statValue(/nodes online/i)).toBe("2");
  });

  /**
   * "Active Jobs" is strictly `running`. Regression guarded: including
   * pending/queued would conflate backlog with actual execution, which is the
   * number operators use to judge cluster load.
   */
  it("counts only running jobs for 'Active Jobs'", () => {
    jobsState.jobs = [
      makeJob({ status: "running" }),
      makeJob({ status: "running" }),
      makeJob({ status: "completed" }),
      makeJob({ status: "pending" }),
    ];
    renderWithRouter(<Dashboard />);
    expect(statValue(/active jobs/i)).toBe("2");
  });

  /** Pools card is a plain length of the pools store. */
  it("shows the total pool count", () => {
    poolsState.pools = [makePool(), makePool(), makePool()];
    renderWithRouter(<Dashboard />);
    expect(statValue(/total pools/i)).toBe("3");
  });

  /** Storage card is a plain length of the backends store. */
  it("shows the storage backend count", () => {
    storageState.backends = [makeBackend(), makeBackend()];
    renderWithRouter(<Dashboard />);
    expect(statValue(/storage backends/i)).toBe("2");
  });

  /**
   * Empty stores render "0", not a blank or "NaN". Regression guarded: a
   * first-run dashboard (before any fetch resolves) showing broken values.
   */
  it("renders zeros across stat cards when all stores are empty", () => {
    renderWithRouter(<Dashboard />);
    expect(statValue(/nodes online/i)).toBe("0");
    expect(statValue(/active jobs/i)).toBe("0");
    expect(statValue(/total pools/i)).toBe("0");
    expect(statValue(/storage backends/i)).toBe("0");
  });

  /** The Recent Jobs section exists regardless of whether there are jobs. */
  it("renders a Recent Jobs section", () => {
    renderWithRouter(<Dashboard />);
    expect(screen.getByRole("heading", { name: /recent jobs/i })).toBeInTheDocument();
  });

  /** Zero jobs produces an explicit empty state, not a bare table. */
  it("shows the empty state when there are no jobs", () => {
    jobsState.jobs = [];
    renderWithRouter(<Dashboard />);
    expect(screen.getByText(/no jobs found/i)).toBeInTheDocument();
  });

  /** Each job contributes one row carrying its name and its status text. */
  it("renders a row per recent job with its name and status", () => {
    jobsState.jobs = [
      makeJob({ name: "build-firmware", status: "completed" }),
      makeJob({ name: "run-sim", status: "running" }),
    ];
    renderWithRouter(<Dashboard />);

    const buildRow = screen.getByText("build-firmware").closest("tr")!;
    const simRow = screen.getByText("run-sim").closest("tr")!;
    expect(within(buildRow).getByText("completed")).toBeInTheDocument();
    expect(within(simRow).getByText("running")).toBeInTheDocument();
  });

  /**
   * The table is capped at 10 rows. Regression guarded: a cluster with
   * thousands of jobs rendering all of them into the dashboard would make the
   * landing page unusably slow.
   */
  it("limits the Recent Jobs table to the 10 newest jobs", () => {
    // 12 jobs with increasing created_at; only the 10 newest should render.
    const base = Date.parse("2026-06-01T00:00:00Z");
    jobsState.jobs = Array.from({ length: 12 }, (_, i) =>
      makeJob({
        name: `job-${i}`,
        created_at: new Date(base + i * 60_000).toISOString(),
      })
    );
    renderWithRouter(<Dashboard />);

    // The two oldest (job-0, job-1) are dropped; the newest (job-11) is shown.
    expect(screen.queryByText("job-0")).not.toBeInTheDocument();
    expect(screen.queryByText("job-1")).not.toBeInTheDocument();
    expect(screen.getByText("job-11")).toBeInTheDocument();
    // 10 data rows + 1 header row.
    expect(screen.getAllByRole("row")).toHaveLength(11);
  });

  /**
   * Sorting is done by the page, not assumed from the API. The fixtures are
   * deliberately supplied oldest-first so a missing sort would fail here.
   * Regression guarded: "Recent Jobs" showing the *oldest* jobs — which also
   * silently breaks the 10-row cap above (it would truncate the wrong end).
   */
  it("orders recent jobs newest-first by created_at", () => {
    const older = makeJob({ name: "older-job", created_at: "2026-01-01T00:00:00Z" });
    const newer = makeJob({ name: "newer-job", created_at: "2026-06-01T00:00:00Z" });
    // Provide them out of order to prove the page sorts.
    jobsState.jobs = [older, newer];
    renderWithRouter(<Dashboard />);

    const rows = screen.getAllByRole("row");
    // rows[0] is the header; rows[1] is the first data row.
    expect(within(rows[1]).getByText("newer-job")).toBeInTheDocument();
    expect(within(rows[2]).getByText("older-job")).toBeInTheDocument();
  });

  /**
   * Row click navigates to that job's detail route. Asserting the id (via the
   * sentinel) rather than merely "the dashboard went away" guards against the
   * classic bug of navigating with a stale/looped-over index and opening the
   * wrong job.
   */
  it("navigates to the specific job detail route when a recent job row is clicked", async () => {
    // Use a real router with both the dashboard ("/") and a job-detail route so
    // we can prove the click lands on /jobs/:id (not merely that content vanished).
    const job = makeJob({ name: "click-me", status: "completed" });
    jobsState.jobs = [job];

    const { user } = renderWithRouterRoutes(<Dashboard />, [
      // Sentinel route renders the navigated-to job id so we can assert on it.
      {
        path: "/jobs/:id",
        element: <JobDetailSentinel />,
      },
    ]);

    const row = screen.getByText("click-me").closest("tr")!;
    await user.click(row);

    // Landed on the job-detail route for exactly this job's id.
    expect(await screen.findByTestId("job-detail-sentinel")).toHaveTextContent(
      `job:${job.id}`
    );
    // And the Dashboard content is gone.
    expect(screen.queryByText("click-me")).not.toBeInTheDocument();
  });

  /** Priority column is rendered verbatim per row. */
  it("renders the job priority in its row", () => {
    jobsState.jobs = [makeJob({ name: "prio-job", priority: 7 })];
    renderWithRouter(<Dashboard />);
    const row = screen.getByText("prio-job").closest("tr")!;
    expect(within(row).getByText("7")).toBeInTheDocument();
  });

  /**
   * The created-at column goes through `formatRelativeTime`.
   *
   * AI Note: the expectation calls the same formatter rather than hard-coding
   * "2h ago". That keeps the test from flaking on unit boundaries and from
   * having to be edited whenever the formatter's wording changes — it asserts
   * the wiring (column uses the shared formatter), while utils.test.ts asserts
   * the formatting rules themselves.
   */
  it("renders the relative created time in the job's row", () => {
    // A timestamp ~2h in the past should surface as "2h ago" via formatRelativeTime.
    const created = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    jobsState.jobs = [makeJob({ name: "timed-job", created_at: created })];
    renderWithRouter(<Dashboard />);

    const row = screen.getByText("timed-job").closest("tr")!;
    // Assert against the SUT's own formatter rather than a hard-coded string.
    expect(within(row).getByText(formatRelativeTime(created))).toBeInTheDocument();
  });

  /**
   * Statuses outside the common set still render their literal text.
   * Regression guarded: a `statusColors[status]` lookup miss blanking the cell
   * (or throwing) for less common states.
   */
  it("renders non-default job statuses (queued, cancelled) by their text", () => {
    // statusColors maps these; the row should still show the raw status text.
    jobsState.jobs = [
      makeJob({ name: "queued-job", status: "queued" }),
      makeJob({ name: "cancelled-job", status: "cancelled" }),
    ];
    renderWithRouter(<Dashboard />);

    const queuedRow = screen.getByText("queued-job").closest("tr")!;
    const cancelledRow = screen.getByText("cancelled-job").closest("tr")!;
    expect(within(queuedRow).getByText("queued")).toBeInTheDocument();
    expect(within(cancelledRow).getByText("cancelled")).toBeInTheDocument();
  });

  /**
   * Exhaustive negative case for the Active Jobs filter: every non-running
   * status at once must still yield 0. Complements the positive test above by
   * catching an over-broad predicate (e.g. `status !== "completed"`).
   */
  it("excludes non-running jobs (pending/queued/completed/failed/cancelled) from Active Jobs", () => {
    // Only 'running' counts as active; every other status must be excluded.
    jobsState.jobs = [
      makeJob({ status: "pending" }),
      makeJob({ status: "queued" }),
      makeJob({ status: "completed" }),
      makeJob({ status: "failed" }),
      makeJob({ status: "cancelled" }),
    ];
    renderWithRouter(<Dashboard />);
    expect(statValue(/active jobs/i)).toBe("0");
  });

  /**
   * Mixed-population version of the Nodes Online rule, proving the count scales
   * (4 of 6) rather than accidentally returning a boolean-ish 0/1.
   */
  it("excludes offline (and only offline) nodes from Nodes Online", () => {
    // online + busy are counted; offline is not. Two offline among four online/busy.
    nodesState.nodes = [
      makeNode({ status: "online" }),
      makeNode({ status: "busy" }),
      makeNode({ status: "online" }),
      makeNode({ status: "busy" }),
      makeNode({ status: "offline" }),
      makeNode({ status: "offline" }),
    ];
    renderWithRouter(<Dashboard />);
    expect(statValue(/nodes online/i)).toBe("4");
  });

  /**
   * Structural check on the empty state: exactly header + placeholder, so the
   * placeholder can't be mistaken for a data row by row-count assertions
   * elsewhere (and no phantom row is rendered alongside it).
   */
  it("does not render a job table row for the empty state placeholder", () => {
    // When there are no jobs, only the header row plus the 'No jobs found' row exist.
    jobsState.jobs = [];
    renderWithRouter(<Dashboard />);
    const rows = screen.getAllByRole("row");
    // header row + single placeholder row.
    expect(rows).toHaveLength(2);
    expect(within(rows[1]).getByText(/no jobs found/i)).toBeInTheDocument();
  });
});
