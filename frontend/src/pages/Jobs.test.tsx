/**
 * Tests for the Jobs page (src/pages/Jobs.tsx).
 *
 * The page renders a table of jobs from useJobsStore, supports a status filter
 * that re-fetches with params, shows an empty/loading state, derives a status
 * badge per row, formats priority/step/duration columns, navigates to the job
 * detail route on row click, and exposes cancel/delete actions (guarded by a
 * confirmation dialog) that call the api client, plus a re-run action on
 * terminal rows that deliberately has no dialog. We mock only the data
 * boundaries (the store + the api client) and assert real rendered output and
 * real side effects with resilient role/text selectors.
 *
 * Neighbouring pieces: `useJobsStore` lives in src/stores (covered by
 * stores/index.test.ts), `api.cancelJob`/`api.deleteJob`/`api.requeueJob` hit
 * /api/jobs/{id}/cancel, /api/jobs/{id} and /api/jobs/{id}/requeue on the
 * server, and row clicks hand off to the JobDetail route (stubbed here by
 * `DetailProbe`).
 */
import { vi, describe, it, expect, beforeEach } from "vitest";
import {
  renderWithRouter,
  screen,
  within,
  waitFor,
  makeJob,
} from "../test/test-utils";
import { Routes, Route, MemoryRouter, useParams } from "react-router-dom";
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { JobInfo } from "@/types";

// ── Mock the store boundary ──────────────────────────────────────────────────
// useJobsStore is called both as a hook (returning state) and via .getState().
// Mutable state shared across the mock and the tests, populated per test.

/** Shape of the fake jobs-store state the page reads from. */
type StoreState = { jobs: JobInfo[]; isLoading: boolean; fetch: ReturnType<typeof vi.fn> };
/** Spy for the store's `fetch`; asserted for both mount and post-action refetches. */
const fetchMock = vi.fn().mockResolvedValue(undefined);
/**
 * Single mutable state object shared by the mock and every test.
 *
 * AI Note: tests mutate this object's *fields* (`storeState.jobs = [...]`) and
 * never reassign the binding. That is required — the hoisted `vi.mock` factory
 * closes over this exact reference, so replacing it would leave the mock
 * pointing at a stale object.
 */
const storeState: StoreState = { jobs: [], isLoading: false, fetch: fetchMock };

// The vi.mock factory is hoisted; reference only hoist-safe values inside it.
//
// AI Note: the mocked hook exposes BOTH call styles the page uses — invoked as
// a hook (`useJobsStore()`) and statically (`useJobsStore.getState()` inside
// event handlers, where hooks are illegal). Dropping `.getState` makes the
// cancel/delete refetch throw "not a function" rather than fail an assertion.
vi.mock("@/stores", () => {
  const hook = vi.fn(() => storeState) as unknown as {
    (): StoreState;
    getState: () => StoreState;
  };
  hook.getState = () => storeState;
  return { useJobsStore: hook };
});

// api is imported by the page for cancel/delete; stub it (true external boundary).
/** Spies standing in for the destructive REST calls; nothing reaches the network. */
const cancelJobMock = vi.fn().mockResolvedValue(undefined);
const deleteJobMock = vi.fn().mockResolvedValue(undefined);
/** Re-run is additive, not destructive — it resolves with the NEW job. */
const requeueJobMock = vi.fn().mockResolvedValue({ id: "new-job-id" });
vi.mock("@/api/client", () => ({
  api: {
    cancelJob: (...args: unknown[]) => cancelJobMock(...args),
    deleteJob: (...args: unknown[]) => deleteJobMock(...args),
    requeueJob: (...args: unknown[]) => requeueJobMock(...args),
  },
}));

import Jobs from "./Jobs";

beforeEach(() => {
  fetchMock.mockClear();
  cancelJobMock.mockClear();
  deleteJobMock.mockClear();
  requeueJobMock.mockClear();
  requeueJobMock.mockResolvedValue({ id: "new-job-id" });
  // Re-arm the resolved values: individual tests use mockRejectedValueOnce, and
  // mockClear() does not restore an implementation replaced by a *Once variant
  // that never got consumed.
  cancelJobMock.mockResolvedValue(undefined);
  deleteJobMock.mockResolvedValue(undefined);
  storeState.jobs = [];
  storeState.isLoading = false;
  storeState.fetch = fetchMock;
});

describe("Jobs page", () => {
  // ── Rendering: list / rows ────────────────────────────────────────────────
  /**
   * One `<tr>` per job plus the header. The explicit row count catches
   * duplicate rendering (e.g. a bad `key` or a doubled `.map`) that a
   * name-only assertion would miss.
   */
  it("renders a row per job with its name", () => {
    storeState.jobs = [
      makeJob({ name: "build-firmware", status: "completed" }),
      makeJob({ name: "run-sim", status: "running" }),
    ];

    renderWithRouter(<Jobs />);

    expect(screen.getByText("build-firmware")).toBeInTheDocument();
    expect(screen.getByText("run-sim")).toBeInTheDocument();
    // Two data rows in addition to the header row.
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });

  /** No jobs -> an explicit "no jobs found" row, never a silently blank table. */
  it("shows the empty state when there are no jobs", () => {
    storeState.jobs = [];
    renderWithRouter(<Jobs />);
    expect(screen.getByText(/no jobs found/i)).toBeInTheDocument();
    // No data rows: only the header row + the empty-state row.
    expect(screen.getAllByRole("row")).toHaveLength(2);
  });

  /**
   * Loading and empty are distinct states. Regression guarded: flashing "No
   * jobs found" during the initial fetch, which reads as "your jobs are gone"
   * on every page load.
   */
  it("renders a loading spinner instead of rows or empty state while loading", () => {
    storeState.isLoading = true;
    storeState.jobs = [];
    renderWithRouter(<Jobs />);
    // No empty-state text while loading.
    expect(screen.queryByText(/no jobs found/i)).not.toBeInTheDocument();
    // Header row + a single spinner row, no job rows.
    expect(screen.getAllByRole("row")).toHaveLength(2);
  });

  // ── Initial fetch + filtering ─────────────────────────────────────────────
  /**
   * Mount fetches once with an empty param object (default filter "all").
   * Regression guarded: sending `{status: "all"}` to the API, which the server
   * would treat as a literal status and return nothing.
   */
  it("triggers an initial fetch with no status filter on mount", () => {
    renderWithRouter(<Jobs />);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // Default filter is "all" -> no status param passed.
    expect(fetchMock).toHaveBeenCalledWith({});
  });

  /**
   * Filtering is server-side: changing the select re-runs the effect with the
   * chosen status. Regression guarded: filtering only the already-loaded page
   * of jobs, which would hide matching jobs the client hasn't fetched.
   */
  it("re-fetches with the selected status when the filter changes", async () => {
    storeState.jobs = [makeJob({ name: "j", status: "running" })];
    const { user } = renderWithRouter(<Jobs />);

    // Initial mount fetch.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fetchMock.mockClear();

    const select = screen.getByRole("combobox");
    await user.selectOptions(select, "running");

    // Changing the filter re-runs the effect with the chosen status.
    expect(fetchMock).toHaveBeenCalledWith({ status: "running" });
  });

  /**
   * The reverse transition: going back to "all" must drop the param entirely.
   * Regression guarded: a sticky filter that leaves the user unable to see the
   * full list again without a reload.
   */
  it("drops the status param again when the filter returns to 'all'", async () => {
    const { user } = renderWithRouter(<Jobs />);
    const select = screen.getByRole("combobox");

    await user.selectOptions(select, "failed");
    expect(fetchMock).toHaveBeenLastCalledWith({ status: "failed" });

    await user.selectOptions(select, "all");
    expect(fetchMock).toHaveBeenLastCalledWith({});
  });

  // ── Status badge ──────────────────────────────────────────────────────────
  /** Each row's badge carries that row's own status text (no cross-row bleed). */
  it("renders a status badge whose text reflects each job's status", () => {
    storeState.jobs = [
      makeJob({ name: "ok-job", status: "completed" }),
      makeJob({ name: "bad-job", status: "failed" }),
    ];

    renderWithRouter(<Jobs />);

    const okRow = screen.getByText("ok-job").closest("tr")!;
    const badRow = screen.getByText("bad-job").closest("tr")!;
    expect(within(okRow).getByText("completed")).toBeInTheDocument();
    expect(within(badRow).getByText("failed")).toBeInTheDocument();
  });

  /**
   * Colour encoding is the at-a-glance signal operators scan for, so the
   * mapping is asserted (loosely, by hue substring) rather than left to visual
   * review. Regression guarded: a failed job rendering in the same colour as a
   * successful one.
   *
   * AI Note: matching on `/green/` instead of the full class string is
   * deliberate — it survives Tailwind shade tweaks (green-500 -> green-600)
   * while still catching an actual mis-mapping.
   */
  it("colors the status badge per status (completed->green, failed->red, running->blue)", () => {
    storeState.jobs = [
      makeJob({ name: "green-job", status: "completed" }),
      makeJob({ name: "red-job", status: "failed" }),
      makeJob({ name: "blue-job", status: "running" }),
    ];
    renderWithRouter(<Jobs />);
    expect(screen.getByText("completed").className).toMatch(/green/);
    expect(screen.getByText("failed").className).toMatch(/red/);
    expect(screen.getByText("running").className).toMatch(/blue/);
  });

  /**
   * Statuses with no colour entry fall through to the neutral scheme rather
   * than borrowing another status's colour. Regression guarded: a cancelled job
   * showing green (read as "succeeded") because of a default-case mistake.
   */
  it("falls back to neutral styling for the cancelled status", () => {
    // 'cancelled' maps to the secondary/muted scheme (no color class).
    storeState.jobs = [makeJob({ name: "cancel-job", status: "cancelled" })];
    renderWithRouter(<Jobs />);
    const badge = screen.getByText("cancelled");
    expect(badge.className).not.toMatch(/(green|red|blue|yellow)/);
    expect(badge.className).toMatch(/secondary|muted/);
  });

  // ── Column formatting ─────────────────────────────────────────────────────
  /** Priority renders as its raw number in the row. */
  it("renders the job priority in its column", () => {
    storeState.jobs = [makeJob({ name: "prio-job", status: "pending", priority: 7 })];
    renderWithRouter(<Jobs />);
    const row = screen.getByText("prio-job").closest("tr")!;
    expect(within(row).getByText("7")).toBeInTheDocument();
  });

  /**
   * `current_step` is -1 as a sentinel for "not started".
   *
   * AI Note: -1 is a real sentinel, not a bug — the server initialises jobs
   * with it. The page must render "-" for it; printing "Step -1" would be
   * nonsense to users, so both the positive and sentinel branches are pinned.
   */
  it("shows 'Step N' for an active step and '-' for current_step < 0", () => {
    storeState.jobs = [
      makeJob({ name: "stepped", status: "running", current_step: 2 }),
      makeJob({ name: "no-step", status: "pending", current_step: -1 }),
    ];
    renderWithRouter(<Jobs />);
    const steppedRow = screen.getByText("stepped").closest("tr")!;
    const noStepRow = screen.getByText("no-step").closest("tr")!;
    expect(within(steppedRow).getByText("Step 2")).toBeInTheDocument();
    // The current-step column renders "-" (so does duration for this pending
    // job that never started), so assert at least one "-" is present.
    expect(within(noStepRow).getAllByText("-").length).toBeGreaterThanOrEqual(1);
    expect(within(noStepRow).queryByText(/^Step /)).not.toBeInTheDocument();
  });

  /**
   * A null `started_at` must produce the "-" placeholder, not "NaN" or a
   * duration measured from the epoch.
   */
  it("renders the duration placeholder for jobs that never started", () => {
    storeState.jobs = [
      makeJob({ name: "not-started", status: "pending", started_at: null, current_step: 0 }),
    ];
    renderWithRouter(<Jobs />);
    const row = screen.getByText("not-started").closest("tr")!;
    // formatDuration(null, ...) returns "-".
    expect(within(row).getAllByText("-").length).toBeGreaterThanOrEqual(1);
  });

  /**
   * Duration is computed from started_at/completed_at and rendered in "Nm Ns"
   * form. Fixed ISO timestamps (not `Date.now()`) keep this deterministic
   * across timezones and slow CI.
   */
  it("formats a finished job's duration in m/s form", () => {
    // 90s between start and completion -> "1m 30s".
    const start = "2026-06-30T10:00:00.000Z";
    const end = "2026-06-30T10:01:30.000Z";
    storeState.jobs = [
      makeJob({
        name: "timed-job",
        status: "completed",
        started_at: start,
        completed_at: end,
      }),
    ];
    renderWithRouter(<Jobs />);
    const row = screen.getByText("timed-job").closest("tr")!;
    expect(within(row).getByText("1m 30s")).toBeInTheDocument();
  });

  // ── Navigation ────────────────────────────────────────────────────────────
  /**
   * Clicking anywhere on a row opens that job. The `DetailProbe` echoes the
   * `:id` param, so this proves the *correct* job is opened rather than merely
   * that some navigation happened.
   */
  it("navigates to the job detail route when a row is clicked", async () => {
    const job = makeJob({ name: "click-me", status: "completed" });
    storeState.jobs = [job];

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/jobs"]}>
        <Routes>
          <Route path="/jobs" element={<Jobs />} />
          <Route
            path="/jobs/:id"
            element={<DetailProbe />}
          />
        </Routes>
      </MemoryRouter>
    );

    const row = screen.getByText("click-me").closest("tr")!;
    await user.click(row);

    // The detail route renders with the clicked job's id in the URL.
    const probe = await screen.findByTestId("detail-probe");
    expect(probe).toHaveTextContent(`detail:${job.id}`);
  });

  /**
   * The row-level click handler and the per-row action buttons overlap, so the
   * buttons must call `stopPropagation`. Regression guarded: clicking Cancel
   * both opening the confirm dialog *and* navigating away from it — the user
   * would never see the confirmation, and the pending action would be lost.
   */
  it("does NOT navigate when an action button is clicked (stopPropagation)", async () => {
    const job = makeJob({ name: "no-nav", status: "running" });
    storeState.jobs = [job];

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/jobs"]}>
        <Routes>
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/jobs/:id" element={<DetailProbe />} />
        </Routes>
      </MemoryRouter>
    );

    const cancelBtn = screen.getByTitle("Cancel");
    await user.click(cancelBtn);

    // Still on the list (detail probe never rendered); the confirm dialog opens.
    expect(screen.queryByTestId("detail-probe")).not.toBeInTheDocument();
    expect(screen.getByText("Cancel Job?")).toBeInTheDocument();
  });

  // ── New Job link ──────────────────────────────────────────────────────────
  /**
   * The entry point to the job builder is a real `<a href>` (not a click
   * handler), so it supports middle-click/open-in-new-tab. Regression guarded:
   * a broken href stranding users with no way to create a job.
   */
  it("exposes a 'New Job' link pointing at the builder route", () => {
    renderWithRouter(<Jobs />);
    const link = screen.getByRole("link", { name: /new job/i });
    expect(link).toHaveAttribute("href", "/jobs/new");
  });

  // ── Action buttons: conditional rendering ─────────────────────────────────
  /**
   * In-flight jobs offer Cancel and must NOT offer Delete. Regression guarded:
   * deleting a running job would orphan work on an agent with no way to stop it.
   */
  it("shows a Cancel action for running jobs and no Delete action", () => {
    storeState.jobs = [makeJob({ name: "running-job", status: "running" })];
    renderWithRouter(<Jobs />);
    const row = screen.getByText("running-job").closest("tr")!;
    expect(within(row).getByTitle("Cancel")).toBeInTheDocument();
    expect(within(row).queryByTitle("Delete")).not.toBeInTheDocument();
  });

  /** The mirror case: terminal jobs offer Delete and no (meaningless) Cancel. */
  it("shows a Delete action for completed jobs and no Cancel action", () => {
    storeState.jobs = [makeJob({ name: "done-job", status: "completed" })];
    renderWithRouter(<Jobs />);
    const row = screen.getByText("done-job").closest("tr")!;
    expect(within(row).getByTitle("Delete")).toBeInTheDocument();
    expect(within(row).queryByTitle("Cancel")).not.toBeInTheDocument();
  });

  // ── Cancel flow (confirmation dialog -> api.cancelJob -> refetch) ──────────
  /**
   * End-to-end cancel: confirm -> `api.cancelJob(id)` -> refetch -> dialog
   * closes. The refetch matters because the store holds a stale status until
   * the server confirms; without it the row keeps showing "running".
   */
  it("cancels a job through the confirmation dialog and refetches", async () => {
    const job = makeJob({ name: "cancel-flow", status: "running" });
    storeState.jobs = [job];
    const { user } = renderWithRouter(<Jobs />);

    fetchMock.mockClear(); // ignore the mount fetch

    await user.click(screen.getByTitle("Cancel"));
    // Dialog appears; confirm with the destructive button (not "Keep").
    expect(screen.getByText("Cancel Job?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel Job" }));

    await waitFor(() => expect(cancelJobMock).toHaveBeenCalledWith(job.id));
    // Refetch after the action.
    expect(fetchMock).toHaveBeenCalled();
    expect(deleteJobMock).not.toHaveBeenCalled();
    // Dialog closes.
    await waitFor(() =>
      expect(screen.queryByText("Cancel Job?")).not.toBeInTheDocument()
    );
  });

  /**
   * The confirmation must actually guard the action: dismissing via "Keep"
   * calls nothing. Regression guarded: a mis-wired dialog that cancels the job
   * on *either* button — an irreversible action taken without consent.
   */
  it("dismisses the cancel dialog via 'Keep' without calling the api", async () => {
    storeState.jobs = [makeJob({ name: "keep-flow", status: "running" })];
    const { user } = renderWithRouter(<Jobs />);

    await user.click(screen.getByTitle("Cancel"));
    expect(screen.getByText("Cancel Job?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Keep" }));

    expect(screen.queryByText("Cancel Job?")).not.toBeInTheDocument();
    expect(cancelJobMock).not.toHaveBeenCalled();
  });

  // ── Delete flow ───────────────────────────────────────────────────────────
  /**
   * The delete twin of the cancel flow, including the post-action refetch and a
   * cross-check that the *other* destructive endpoint was not also invoked.
   */
  it("deletes a job through the confirmation dialog and refetches", async () => {
    const job = makeJob({ name: "delete-flow", status: "completed" });
    storeState.jobs = [job];
    const { user } = renderWithRouter(<Jobs />);

    fetchMock.mockClear();

    await user.click(screen.getByTitle("Delete"));
    expect(screen.getByText("Delete Job?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete Job" }));

    await waitFor(() => expect(deleteJobMock).toHaveBeenCalledWith(job.id));
    expect(fetchMock).toHaveBeenCalled();
    expect(cancelJobMock).not.toHaveBeenCalled();
  });

  /**
   * A rejected API call must still run the `finally` cleanup that closes the
   * dialog. Regression guarded: a server error leaving a modal permanently
   * stuck open over the UI with no way to dismiss it.
   */
  it("closes the dialog even when the api call rejects (error path)", async () => {
    deleteJobMock.mockRejectedValueOnce(new Error("server boom"));
    const job = makeJob({ name: "delete-fail", status: "failed" });
    storeState.jobs = [job];
    const { user } = renderWithRouter(<Jobs />);

    await user.click(screen.getByTitle("Delete"));
    await user.click(screen.getByRole("button", { name: "Delete Job" }));

    await waitFor(() => expect(deleteJobMock).toHaveBeenCalledWith(job.id));
    // Despite the rejection the finally block closes the dialog.
    await waitFor(() =>
      expect(screen.queryByText("Delete Job?")).not.toBeInTheDocument()
    );
  });

  // ── Re-run flow (no dialog — the action is additive) ──────────────────────
  /**
   * Re-run belongs to the same terminal-row bucket as Delete. Regression
   * guarded: offering it on a running job, which would silently start a second
   * concurrent copy of work already in flight.
   */
  it("shows Re-run on terminal rows and not on in-flight ones", () => {
    storeState.jobs = [
      makeJob({ name: "done-job", status: "completed" }),
      makeJob({ name: "live-job", status: "running" }),
    ];
    renderWithRouter(<Jobs />);

    const done = screen.getByText("done-job").closest("tr")!;
    const live = screen.getByText("live-job").closest("tr")!;
    expect(
      within(done).getByTitle("Re-run with the same parameters")
    ).toBeInTheDocument();
    expect(
      within(live).queryByTitle("Re-run with the same parameters")
    ).not.toBeInTheDocument();
  });

  /**
   * The whole point of the row button: one click, no confirmation, then a
   * refetch so the new pending job shows up.
   *
   * Regression guarded (two ways): a dialog creeping in to match cancel/delete
   * — this action is trivially undone by cancelling the copy, so a modal would
   * be pure friction; and a missing refetch, which leaves the list looking
   * exactly as it did before and reads as a dead button.
   */
  it("re-runs a job with no confirmation dialog and refetches the list", async () => {
    const job = makeJob({ name: "rerun-flow", status: "failed" });
    storeState.jobs = [job];
    const { user } = renderWithRouter(<Jobs />);

    fetchMock.mockClear(); // ignore the mount fetch

    await user.click(screen.getByTitle("Re-run with the same parameters"));

    await waitFor(() => expect(requeueJobMock).toHaveBeenCalledWith(job.id));
    expect(fetchMock).toHaveBeenCalled();
    // No modal was ever shown, and nothing destructive fired.
    expect(screen.queryByText("Delete Job?")).not.toBeInTheDocument();
    expect(screen.queryByText("Cancel Job?")).not.toBeInTheDocument();
    expect(deleteJobMock).not.toHaveBeenCalled();
    expect(cancelJobMock).not.toHaveBeenCalled();
  });

  /**
   * The refetch is intentionally unfiltered (`getState().fetch()` with no
   * params). Regression guarded: passing the active filter through would hide
   * the freshly created `pending` job whenever the user is looking at, say,
   * "Failed" — the copy exists but never appears.
   */
  it("refetches without the active status filter after a re-run", async () => {
    storeState.jobs = [makeJob({ name: "rerun-filter", status: "failed" })];
    const { user } = renderWithRouter(<Jobs />);

    fetchMock.mockClear();
    await user.click(screen.getByTitle("Re-run with the same parameters"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith();
  });

  /**
   * Re-run stays on the list rather than following the copy. Regression
   * guarded: a `navigate` to the new job would break re-running several jobs in
   * a row, which is the common case after a node or daemon outage.
   */
  it("stays on the jobs list after a re-run", async () => {
    storeState.jobs = [makeJob({ name: "rerun-stay", status: "cancelled" })];
    const { user } = renderWithRouter(<Jobs />);

    await user.click(screen.getByTitle("Re-run with the same parameters"));

    await waitFor(() => expect(requeueJobMock).toHaveBeenCalled());
    expect(screen.getByText("rerun-stay")).toBeInTheDocument();
    expect(screen.queryByTestId("detail-probe")).not.toBeInTheDocument();
  });

  /**
   * A rejected requeue must clear `actionLoading` in its `finally`, or the
   * row's buttons stay disabled until a full reload.
   */
  it("re-enables the row's actions when the re-run rejects", async () => {
    requeueJobMock.mockRejectedValueOnce(new Error("plan no longer valid"));
    storeState.jobs = [makeJob({ name: "rerun-fail", status: "failed" })];
    const { user } = renderWithRouter(<Jobs />);

    const button = screen.getByTitle("Re-run with the same parameters");
    await user.click(button);

    await waitFor(() => expect(requeueJobMock).toHaveBeenCalled());
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  /**
   * The Actions cell sits inside a row whose own click navigates to the detail
   * page. Regression guarded: dropping the `stopPropagation` wrapper, which
   * would re-run the job AND yank the user off the list mid-action.
   */
  it("does not navigate to the detail page when Re-run is clicked", async () => {
    storeState.jobs = [makeJob({ name: "rerun-nonav", status: "completed" })];

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/jobs"]}>
        <Routes>
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/jobs/:id" element={<DetailProbe />} />
        </Routes>
      </MemoryRouter>
    );

    await user.click(screen.getByTitle("Re-run with the same parameters"));

    await waitFor(() => expect(requeueJobMock).toHaveBeenCalled());
    expect(screen.queryByTestId("detail-probe")).not.toBeInTheDocument();
  });
});

// A tiny route probe that echoes the :id param so navigation can be asserted
// without pulling in the real (heavy) JobDetail page.
/**
 * Stand-in for the JobDetail route. Renders the matched `:id` into a testid so
 * navigation assertions can verify *which* job was opened.
 *
 * Using a probe instead of the real page keeps these tests free of JobDetail's
 * own store subscriptions, WebSocket connection and log-polling API calls.
 */
function DetailProbe() {
  const { id } = useParams();
  return <div data-testid="detail-probe">detail:{id}</div>;
}
