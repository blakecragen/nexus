/**
 * Tests for the Job Detail page (src/pages/JobDetail.tsx).
 *
 * `JobDetail` is the deepest read view in the app: it stitches four data
 * sources into one screen, so this file covers each of them in turn.
 *
 *  1. Job + steps    — GET /api/jobs/{id}: the header (name, status pill,
 *                      submitter, timings), the error banner, the step
 *                      timeline (icons, durations, selection), and the 3s poll
 *                      that runs only while the job is active.
 *  2. Live logs      — `step.log` WebSocket frames, dispatched through the REAL
 *                      `handleWsMessage`/`useLiveLogsStore` pair, keyed
 *                      `${jobId}:${stepIndex}`.
 *  3. Terminal log   — GET /api/jobs/{id}/log (plain text) behind the "Full
 *                      Terminal Log" tab, plus its client-side .txt download.
 *  4. Results        — GET /api/jobs/{id}/results/manifest turned into the
 *                      expandable `ResultsTree`, and the authenticated tarball
 *                      Download button.
 *
 * What is real vs stubbed: the page, its tree machinery (`buildTree`,
 * `TreeRow`, `ResultsTree`), the status/duration/byte formatters, the real
 * zustand live-log store and real react-router are all exercised for real. Only
 * `@/api/client` is mocked — every network call this page makes goes through it.
 *
 * AI Note: using the real store means the log buffer is a module singleton that
 * survives between tests; `beforeEach` resets it with
 * `useLiveLogsStore.setState({logs: {}})`. Without that, a line pushed by one
 * test shows up in the next one's log pane.
 *
 * Neighbouring pieces: the socket that delivers those `step.log` frames is
 * owned by `<Layout />` (see Layout.test.tsx) and covered by
 * src/hooks/useWebSocket.test.tsx; the store mutators are covered by
 * src/stores/index.test.ts.
 */
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, within, act, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import type { JobDetail as JobDetailType, JobInfo, StepRunInfo, ArtifactInfo } from "@/types";
import { makeJob, makeStepRun } from "../test/test-utils";
import { useLiveLogsStore, handleWsMessage } from "@/stores";

// ── Mock the single boundary: the REST client ───────────────────────────────
//
// AI Note: `@/stores` (imported for real, below) also imports `setToken` from
// this module, so the mock must export it too or the store module fails to
// evaluate with "does not provide an export named 'setToken'".
const h = vi.hoisted(() => ({
  getJob: vi.fn(),
  listArtifacts: vi.fn(),
  getJobLog: vi.fn(),
  getJobResultsManifest: vi.fn(),
  downloadJobResults: vi.fn(),
  cancelJob: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  api: {
    getJob: h.getJob,
    listArtifacts: h.listArtifacts,
    getJobLog: h.getJobLog,
    getJobResultsManifest: h.getJobResultsManifest,
    downloadJobResults: h.downloadJobResults,
    cancelJob: h.cancelJob,
  },
  setToken: vi.fn(),
  getToken: vi.fn(() => null),
}));

import JobDetail from "./JobDetail";

// ── Fixtures ────────────────────────────────────────────────────────────────

/** A fixed job id, so log keys (`${jobId}:${stepIndex}`) are easy to construct. */
const JOB_ID = "00000000-0000-0000-0000-0000000000ff";

/**
 * Build a `GET /api/jobs/{id}` payload.
 *
 * Steps default to two runs belonging to THIS job — `makeStepRun` otherwise
 * invents a fresh `job_id`, which would be a lie in a detail payload.
 */
function makeDetail(overrides: Partial<JobDetailType> = {}): JobDetailType {
  const job = makeJob({
    id: JOB_ID,
    name: "nightly-build",
    submitted_by: "abcdef12-3456-7890-abcd-ef1234567890",
    status: "running",
    started_at: new Date().toISOString(),
    ...overrides.job,
  });
  return {
    job,
    steps: [
      makeStepRun({ job_id: JOB_ID, step_index: 0, step_name: "shell_run", status: "success" }),
      makeStepRun({ job_id: JOB_ID, step_index: 1, step_name: "gem5_run", status: "running" }),
    ],
    context_data: { build_sha: "deadbeef" },
    ...overrides,
  };
}

/** A finished job with no live step, for the terminal-state paths. */
const completedDetail = () =>
  makeDetail({
    job: makeJob({
      id: JOB_ID,
      name: "nightly-build",
      status: "completed",
      started_at: new Date(Date.now() - 60_000).toISOString(),
      completed_at: new Date().toISOString(),
    }),
    steps: [
      makeStepRun({ job_id: JOB_ID, step_index: 0, step_name: "shell_run", status: "success" }),
    ],
  });

/** The manifest used by every results test: one dir, a nested dir, three files. */
const manifestFixture = {
  archive_bytes: 2048,
  entries: [
    { path: "m5out/", size: 0, is_dir: true },
    { path: "m5out/stats.txt", size: 1024, is_dir: false },
    { path: "m5out/config.json", size: 512, is_dir: false },
    // No explicit entry for "m5out/nested" — the tree must materialise it.
    { path: "m5out/nested/inner.log", size: 256, is_dir: false },
    { path: "README.txt", size: 128, is_dir: false },
  ],
};

beforeEach(() => {
  h.getJob.mockReset().mockResolvedValue(makeDetail());
  h.listArtifacts.mockReset().mockResolvedValue([]);
  h.getJobLog.mockReset().mockResolvedValue("");
  h.getJobResultsManifest.mockReset().mockResolvedValue(manifestFixture);
  h.downloadJobResults.mockReset().mockResolvedValue(undefined);
  h.cancelJob.mockReset().mockResolvedValue(makeJob({ id: JOB_ID, status: "cancelled" }));
  // The live-log buffer is a module singleton; start every test with it empty.
  useLiveLogsStore.setState({ logs: {} });
});

afterEach(() => {
  vi.useRealTimers();
});

// ── Harness ─────────────────────────────────────────────────────────────────

/**
 * Render the page behind a `/jobs/:id` route (so `useParams` resolves) with a
 * sibling `/jobs` probe, which is where the back arrow navigates to.
 */
function renderDetail(jobId: string = JOB_ID) {
  const user = userEvent.setup();
  const view = render(
    <MemoryRouter initialEntries={[`/jobs/${jobId}`]}>
      <Routes>
        <Route path="/jobs/:id" element={<JobDetail />} />
        <Route path="/jobs" element={<div data-testid="jobs-list-probe">jobs list</div>} />
      </Routes>
    </MemoryRouter>
  );
  return { user, ...view };
}

/** Render and wait for the first payload to land. */
async function renderLoaded(jobId: string = JOB_ID) {
  const view = renderDetail(jobId);
  await screen.findByRole("heading", { level: 1 });
  return view;
}

// ── Query helpers ───────────────────────────────────────────────────────────

/** Any spinner currently on screen (the page has no role for its loaders). */
const spinners = () => document.querySelectorAll("svg.animate-spin");

/** A tab button in the right-hand detail panel. */
const tab = (label: string) => screen.getByRole("button", { name: label });

/** The timeline button for the step whose name is `name`. */
const stepButton = (name: string): HTMLElement => screen.getByText(name).closest("button")!;

/** All timeline buttons, in render order (they are the ones holding a status icon + duration). */
const timelineButtons = (): HTMLElement[] =>
  Array.from(document.querySelectorAll<HTMLElement>("button")).filter((b) =>
    /^\d/.test(b.textContent ?? "")
  );

/** A promise plus its settle functions, for asserting in-flight UI. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Push one live log line the way the dashboard socket would. */
function pushLogLine(
  jobId: string,
  stepIndex: number,
  line: string,
  stream: "stdout" | "stderr" = "stdout"
) {
  act(() => {
    handleWsMessage({ type: "step.log", job_id: jobId, step_index: stepIndex, stream, line });
  });
}

// ── Loading, error and empty states ─────────────────────────────────────────

/** The three ways the page can start: pending, loaded, or failed. */
describe("JobDetail — load states", () => {
  /** Until GET /api/jobs/{id} answers, the page is a bare spinner. */
  it("renders only a spinner while the job request is in flight", () => {
    // Both requests are left pending so nothing settles after the assertions
    // (a late resolution would be a state update outside act()).
    h.getJob.mockReturnValue(deferred<JobDetailType>().promise);
    h.listArtifacts.mockReturnValue(deferred<ArtifactInfo[]>().promise);

    renderDetail();

    expect(spinners().length).toBeGreaterThan(0);
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
    expect(screen.queryByText("Steps")).not.toBeInTheDocument();
  });

  /** One GET for the job and one for its artifacts, per mount. */
  it("requests the job and its artifacts exactly once on mount", async () => {
    await renderLoaded();

    expect(h.getJob).toHaveBeenCalledTimes(1);
    expect(h.getJob).toHaveBeenCalledWith(JOB_ID);
    expect(h.listArtifacts).toHaveBeenCalledTimes(1);
    expect(h.listArtifacts).toHaveBeenCalledWith(JOB_ID);
  });

  /**
   * Documents a known rough edge, called out in the source: a failed job fetch
   * leaves `detail` null, which renders the SAME spinner as "still loading".
   * A deleted or forbidden job id therefore shows an indefinite spinner rather
   * than a 404 / error state.
   *
   * If a real not-found state is ever added, this test should fail and be
   * replaced with assertions on it.
   */
  it("shows an indefinite spinner (not an error state) when the job cannot be fetched", async () => {
    h.getJob.mockRejectedValue(new Error("Job not found"));

    renderDetail("00000000-0000-0000-0000-000000000bad");

    await waitFor(() => expect(h.getJob).toHaveBeenCalled());
    expect(spinners().length).toBeGreaterThan(0);
    expect(screen.queryByText(/not found/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
  });

  /** Artifacts are optional: their failure must never block the job itself. */
  it("still renders the job when the artifacts request fails", async () => {
    h.listArtifacts.mockRejectedValue(new Error("boom"));

    await renderLoaded();

    expect(screen.getByRole("heading", { name: "nightly-build" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  /** The artifacts section is omitted entirely when a job produced none. */
  it("omits the artifacts table when the job has no artifacts", async () => {
    await renderLoaded();

    expect(screen.queryByText("Artifacts")).not.toBeInTheDocument();
  });

  /** A job with no step runs says so rather than rendering an empty column. */
  it("renders a placeholder when the job has no steps", async () => {
    h.getJob.mockResolvedValue(makeDetail({ steps: [] }));

    await renderLoaded();

    expect(screen.getByText("No steps to display.")).toBeInTheDocument();
    expect(timelineButtons()).toHaveLength(0);
  });
});

// ── Header ──────────────────────────────────────────────────────────────────

/** Job identity, status pill, timing metadata, error banner, back arrow. */
describe("JobDetail — header", () => {
  it("renders the job name as the page heading with its status pill", async () => {
    await renderLoaded();

    expect(screen.getByRole("heading", { name: "nightly-build", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("running").className).toMatch(/bg-blue-100/);
  });

  /** Terminal statuses get their own colour from the status map. */
  it("colours the status pill per job status", async () => {
    h.getJob.mockResolvedValue(
      makeDetail({ job: makeJob({ id: JOB_ID, name: "nightly-build", status: "failed" }) })
    );

    await renderLoaded();

    expect(screen.getByText("failed").className).toMatch(/bg-red-100/);
  });

  /** `submitted_by` is a raw user UUID, shown as a compact 8-char identifier. */
  it("shows the submitter's id truncated to eight characters", async () => {
    await renderLoaded();

    expect(screen.getByText("abcdef12")).toBeInTheDocument();
    expect(screen.queryByText(/abcdef12-3456/)).not.toBeInTheDocument();
  });

  /** Created is always shown; Started/Completed only once the job reached them. */
  it("shows Created and Started but not Completed for a running job", async () => {
    await renderLoaded();

    expect(screen.getByText(/^Created /)).toBeInTheDocument();
    expect(screen.getByText(/^Started /)).toBeInTheDocument();
    expect(screen.queryByText(/^Completed /)).not.toBeInTheDocument();
  });

  it("shows Completed once the job has finished", async () => {
    h.getJob.mockResolvedValue(completedDetail());

    await renderLoaded();

    expect(screen.getByText(/^Completed /)).toBeInTheDocument();
  });

  /** A pending job has neither Started nor Completed timestamps. */
  it("omits Started for a job that has not begun", async () => {
    h.getJob.mockResolvedValue(
      makeDetail({
        job: makeJob({ id: JOB_ID, name: "nightly-build", status: "pending", started_at: null }),
      })
    );

    await renderLoaded();

    expect(screen.queryByText(/^Started /)).not.toBeInTheDocument();
  });

  /** A job-level failure message gets its own banner above the columns. */
  it("renders the error banner when the job carries an error", async () => {
    h.getJob.mockResolvedValue(
      makeDetail({
        job: makeJob({
          id: JOB_ID,
          name: "nightly-build",
          status: "failed",
          error: "step 1 exited with code 2",
        }),
      })
    );

    await renderLoaded();

    expect(screen.getByText("Error:")).toBeInTheDocument();
    expect(screen.getByText(/step 1 exited with code 2/)).toBeInTheDocument();
  });

  it("renders no error banner for a healthy job", async () => {
    await renderLoaded();

    expect(screen.queryByText("Error:")).not.toBeInTheDocument();
  });

  /** The back arrow returns to the job list. */
  it("navigates back to the job list when the back arrow is clicked", async () => {
    const { user } = await renderLoaded();
    // The arrow button is the first button in the header and has no label.
    const back = screen.getAllByRole("button")[0]!;

    await user.click(back);

    expect(await screen.findByTestId("jobs-list-probe")).toBeInTheDocument();
  });
});

// ── Step timeline ───────────────────────────────────────────────────────────

/** The left column: one row per step run, with status, duration and selection. */
describe("JobDetail — step timeline", () => {
  it("renders one row per step with its 0-based index and name", async () => {
    await renderLoaded();

    const rows = timelineButtons();
    expect(rows).toHaveLength(2);
    expect(rows[0]!.textContent).toMatch(/^0shell_run/);
    expect(rows[1]!.textContent).toMatch(/^1gem5_run/);
  });

  /** Status icons are colour-coded per `StepStatus` (a different enum from JobStatus). */
  it("renders a status icon per step, colour-coded by step status", async () => {
    h.getJob.mockResolvedValue(
      makeDetail({
        steps: [
          makeStepRun({ job_id: JOB_ID, step_index: 0, step_name: "ok_step", status: "success" }),
          makeStepRun({ job_id: JOB_ID, step_index: 1, step_name: "bad_step", status: "failed" }),
          makeStepRun({ job_id: JOB_ID, step_index: 2, step_name: "skip_step", status: "skipped" }),
          makeStepRun({ job_id: JOB_ID, step_index: 3, step_name: "wait_step", status: "pending" }),
        ],
      })
    );

    await renderLoaded();

    expect(stepButton("ok_step").querySelector("svg.text-green-500")).not.toBeNull();
    expect(stepButton("bad_step").querySelector("svg.text-red-500")).not.toBeNull();
    expect(stepButton("skip_step").querySelector("svg.text-muted-foreground")).not.toBeNull();
    expect(stepButton("wait_step").querySelector("svg.text-yellow-500")).not.toBeNull();
  });

  /** A running step is animated so it reads as live. */
  it("animates the row of a running step", async () => {
    await renderLoaded();

    expect(stepButton("gem5_run").className).toMatch(/animate-pulse/);
    expect(stepButton("shell_run").className).not.toMatch(/animate-pulse/);
  });

  /** A step that never started shows "-" rather than a bogus duration. */
  it("renders '-' as the duration of a step that never started", async () => {
    h.getJob.mockResolvedValue(
      makeDetail({
        steps: [makeStepRun({ job_id: JOB_ID, step_name: "waiting", started_at: null })],
      })
    );

    await renderLoaded();

    expect(within(stepButton("waiting")).getByText("-")).toBeInTheDocument();
  });

  /** Sub-minute durations are seconds; over a minute switches to "Xm Ys". */
  it("formats finished step durations in seconds and minutes", async () => {
    const start = "2026-01-01T00:00:00Z";
    h.getJob.mockResolvedValue(
      makeDetail({
        steps: [
          makeStepRun({
            job_id: JOB_ID,
            step_name: "quick",
            started_at: start,
            finished_at: "2026-01-01T00:00:12Z",
          }),
          makeStepRun({
            job_id: JOB_ID,
            step_name: "slow",
            started_at: start,
            finished_at: "2026-01-01T00:03:04Z",
          }),
          makeStepRun({
            job_id: JOB_ID,
            step_name: "very_slow",
            started_at: start,
            finished_at: "2026-01-01T01:20:00Z",
          }),
        ],
      })
    );

    await renderLoaded();

    expect(within(stepButton("quick")).getByText("12s")).toBeInTheDocument();
    expect(within(stepButton("slow")).getByText("3m 4s")).toBeInTheDocument();
    expect(within(stepButton("very_slow")).getByText("1h 20m")).toBeInTheDocument();
  });

  /** A step error is previewed inline, clipped to 60 characters. */
  it("previews a step error truncated to 60 characters", async () => {
    const long = "E".repeat(80);
    h.getJob.mockResolvedValue(
      makeDetail({
        steps: [
          makeStepRun({
            job_id: JOB_ID,
            step_name: "boom",
            status: "failed",
            error: long,
            started_at: "2026-01-01T00:00:00Z",
            finished_at: "2026-01-01T00:00:01Z",
          }),
        ],
      })
    );

    await renderLoaded();

    expect(screen.getByText("E".repeat(60))).toBeInTheDocument();
    expect(screen.queryByText(long)).not.toBeInTheDocument();
  });

  /**
   * On the initial load the running step is pre-selected, so the operator lands
   * on the step that is actually producing output.
   */
  it("pre-selects the running step on the initial load", async () => {
    await renderLoaded();

    expect(stepButton("gem5_run").className).toMatch(/bg-primary\/5/);
    expect(stepButton("shell_run").className).not.toMatch(/bg-primary\/5/);
  });

  /** With nothing running, selection falls back to the first step. */
  it("selects the first step when no step is running", async () => {
    h.getJob.mockResolvedValue(completedDetail());

    await renderLoaded();

    expect(stepButton("shell_run").className).toMatch(/bg-primary\/5/);
  });

  /** Clicking a row moves the selection (and thus the right-hand panel). */
  it("selects a step when its row is clicked", async () => {
    const { user } = await renderLoaded();

    await user.click(stepButton("shell_run"));

    expect(stepButton("shell_run").className).toMatch(/bg-primary\/5/);
    expect(stepButton("gem5_run").className).not.toMatch(/bg-primary\/5/);
  });

  /**
   * Switching steps also forces the Logs tab back on — Params/Outputs are
   * per-step, so leaving the user on another tab would look like the click did
   * nothing.
   */
  it("returns to the Logs tab when a different step is selected", async () => {
    const { user } = await renderLoaded();
    await user.click(tab("Params"));
    expect(screen.queryByText(/No log output yet/)).not.toBeInTheDocument();

    await user.click(stepButton("shell_run"));

    expect(screen.getByText(/No log output yet/)).toBeInTheDocument();
  });
});

// ── Tab strip ───────────────────────────────────────────────────────────────

/** The right-hand panel's tabs and their bodies. */
describe("JobDetail — detail tabs", () => {
  /** Five tabs by default; Results is conditional on `has_results`. */
  it("renders the five always-present tabs and omits Results by default", async () => {
    await renderLoaded();

    for (const label of ["Logs", "Params", "Outputs", "Context", "Full Terminal Log"]) {
      expect(tab(label)).toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: "Results" })).not.toBeInTheDocument();
  });

  it("appends the Results tab when the job reports has_results", async () => {
    h.getJob.mockResolvedValue(makeDetail({ has_results: true }));

    await renderLoaded();

    expect(tab("Results")).toBeInTheDocument();
  });

  /** Logs is the landing tab, with a placeholder until lines arrive. */
  it("opens on the Logs tab with its empty-state copy", async () => {
    await renderLoaded();

    expect(tab("Logs").className).toMatch(/border-primary/);
    expect(
      screen.getByText("No log output yet. Logs appear in real time while a step runs.")
    ).toBeInTheDocument();
  });

  /** Params shows the selected step's resolved input params. */
  it("shows the selected step's input params on the Params tab", async () => {
    h.getJob.mockResolvedValue(
      makeDetail({
        steps: [
          makeStepRun({
            job_id: JOB_ID,
            step_name: "shell_run",
            input_params: { command: "make test" },
          }),
        ],
      })
    );
    const { user } = await renderLoaded();

    await user.click(tab("Params"));

    expect(screen.getByText(/"command": "make test"/)).toBeInTheDocument();
  });

  /** A step with no params gets an explicit note, not an empty block. */
  it("shows a placeholder note when the step has no input params", async () => {
    const { user } = await renderLoaded();

    await user.click(tab("Params"));

    expect(screen.getByText(/"note": "No input parameters"/)).toBeInTheDocument();
  });

  it("shows the selected step's output params on the Outputs tab", async () => {
    h.getJob.mockResolvedValue(
      makeDetail({
        steps: [
          makeStepRun({
            job_id: JOB_ID,
            step_name: "shell_run",
            output_params: { exit_code: 0 },
          }),
        ],
      })
    );
    const { user } = await renderLoaded();

    await user.click(tab("Outputs"));

    expect(screen.getByText(/"exit_code": 0/)).toBeInTheDocument();
  });

  it("shows a placeholder note when the step has no output params", async () => {
    const { user } = await renderLoaded();

    await user.click(tab("Outputs"));

    expect(screen.getByText(/"note": "No output parameters"/)).toBeInTheDocument();
  });

  /** Context is job-level, not per-step: the accumulated key/value state. */
  it("shows the job's accumulated context on the Context tab", async () => {
    const { user } = await renderLoaded();

    await user.click(tab("Context"));

    expect(screen.getByText(/"build_sha": "deadbeef"/)).toBeInTheDocument();
  });

  /** The active tab is the only one with the underline. */
  it("moves the active-tab underline as tabs are clicked", async () => {
    const { user } = await renderLoaded();

    await user.click(tab("Context"));

    expect(tab("Context").className).toMatch(/border-primary/);
    expect(tab("Logs").className).toMatch(/border-transparent/);
  });
});

// ── Live logs over the WebSocket ────────────────────────────────────────────

/**
 * The Logs pane is fed exclusively by `step.log` frames (the 3s poll never
 * carries log lines), so these tests dispatch real frames through the real
 * `handleWsMessage`.
 */
describe("JobDetail — live log stream", () => {
  it("renders a log line pushed for the selected step", async () => {
    await renderLoaded();

    pushLogLine(JOB_ID, 1, "compiling gem5...");

    expect(screen.getByText("compiling gem5...")).toBeInTheDocument();
    expect(screen.queryByText(/No log output yet/)).not.toBeInTheDocument();
  });

  it("renders lines in arrival order", async () => {
    await renderLoaded();

    pushLogLine(JOB_ID, 1, "first");
    pushLogLine(JOB_ID, 1, "second");
    pushLogLine(JOB_ID, 1, "third");

    const pane = screen.getByText("first").parentElement!;
    expect(Array.from(pane.children).map((c) => c.textContent)).toEqual([
      "first",
      "second",
      "third",
    ]);
  });

  /** stdout is green, stderr red — the only way the two streams are told apart. */
  it("tints stderr lines differently from stdout lines", async () => {
    await renderLoaded();

    pushLogLine(JOB_ID, 1, "out line", "stdout");
    pushLogLine(JOB_ID, 1, "err line", "stderr");

    expect(screen.getByText("out line").className).toMatch(/text-green-400/);
    expect(screen.getByText("err line").className).toMatch(/text-red-400/);
  });

  /**
   * Buffers are keyed `${jobId}:${stepIndex}`, so another step's output stays
   * hidden until that step is selected. Regression guarded: a mismatch between
   * this key and `appendLog`'s makes live logs silently vanish.
   */
  it("keeps each step's log buffer separate", async () => {
    const { user } = await renderLoaded(); // step 1 selected

    pushLogLine(JOB_ID, 0, "step zero output");
    expect(screen.queryByText("step zero output")).not.toBeInTheDocument();

    await user.click(stepButton("shell_run"));

    expect(screen.getByText("step zero output")).toBeInTheDocument();
  });

  /** Frames for a different job must never leak into this page. */
  it("ignores log lines addressed to another job", async () => {
    await renderLoaded();

    pushLogLine("11111111-1111-1111-1111-111111111111", 1, "someone else's output");

    expect(screen.queryByText("someone else's output")).not.toBeInTheDocument();
    expect(screen.getByText(/No log output yet/)).toBeInTheDocument();
  });

  /** Lines keep streaming into the pane only while the Logs tab is open. */
  it("shows lines that arrived while another tab was open once Logs is reopened", async () => {
    const { user } = await renderLoaded();
    await user.click(tab("Context"));

    pushLogLine(JOB_ID, 1, "arrived off-screen");
    await user.click(tab("Logs"));

    expect(screen.getByText("arrived off-screen")).toBeInTheDocument();
  });
});

// ── Polling ─────────────────────────────────────────────────────────────────

/**
 * The 3s poll: it refreshes step status/timing while the job is active and must
 * stop the moment the job reaches a terminal state.
 */
describe("JobDetail — polling while active", () => {
  it("refetches the job every 3s while it is running and renders the update", async () => {
    vi.useFakeTimers();
    h.getJob.mockResolvedValueOnce(makeDetail()).mockResolvedValue(completedDetail());

    renderDetail();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("running")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(h.getJob).toHaveBeenCalledTimes(2);
    expect(screen.getByText("completed")).toBeInTheDocument();
  });

  /** A finished job is not polled at all — no wasted requests. */
  it("does not poll a job that is already in a terminal state", async () => {
    vi.useFakeTimers();
    h.getJob.mockResolvedValue(completedDetail());

    renderDetail();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(12_000);
    });

    expect(h.getJob).toHaveBeenCalledTimes(1);
  });

  /** Polling stops once the status becomes terminal, without needing a remount. */
  it("stops polling after the job transitions to a terminal state", async () => {
    vi.useFakeTimers();
    h.getJob.mockResolvedValueOnce(makeDetail()).mockResolvedValue(completedDetail());

    renderDetail();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(h.getJob).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });

    expect(h.getJob).toHaveBeenCalledTimes(2);
  });

  /** A failed poll is swallowed: the last good payload stays on screen. */
  it("keeps the last good payload when a poll fails", async () => {
    vi.useFakeTimers();
    h.getJob.mockResolvedValueOnce(makeDetail()).mockRejectedValue(new Error("network blip"));

    renderDetail();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(screen.getByRole("heading", { name: "nightly-build" })).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  /** Each poll also refreshes the artifacts table. */
  it("refreshes the artifacts list on every poll tick", async () => {
    vi.useFakeTimers();
    const artifact: ArtifactInfo = {
      id: "11111111-1111-1111-1111-111111111111",
      job_id: JOB_ID,
      step_run_id: null,
      filename: "results.tar.gz",
      storage_backend_id: "22222222-2222-2222-2222-222222222222",
      storage_backend_name: "minio-1",
      storage_key: "jobs/x/results.tar.gz",
      content_type: "application/gzip",
      size_bytes: 2048,
      created_at: new Date().toISOString(),
    };
    h.listArtifacts.mockResolvedValueOnce([]).mockResolvedValue([artifact]);

    renderDetail();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.queryByText("Artifacts")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(screen.getByText("Artifacts")).toBeInTheDocument();
    expect(screen.getByText("results.tar.gz")).toBeInTheDocument();
    expect(screen.getByText("minio-1")).toBeInTheDocument();
    expect(screen.getByText("2 KB")).toBeInTheDocument();
  });
});

// ── Cancel ──────────────────────────────────────────────────────────────────

/** The Cancel Job button: visibility, the POST, and its in-flight state. */
describe("JobDetail — cancelling a job", () => {
  it("offers Cancel Job while the job is running", async () => {
    await renderLoaded();

    expect(screen.getByRole("button", { name: /cancel job/i })).toBeInTheDocument();
  });

  it("offers Cancel Job while the job is still queued", async () => {
    h.getJob.mockResolvedValue(
      makeDetail({ job: makeJob({ id: JOB_ID, name: "nightly-build", status: "queued" }) })
    );

    await renderLoaded();

    expect(screen.getByRole("button", { name: /cancel job/i })).toBeInTheDocument();
  });

  /** A finished job cannot be cancelled, so the button is not rendered. */
  it("hides Cancel Job once the job has completed", async () => {
    h.getJob.mockResolvedValue(completedDetail());

    await renderLoaded();

    expect(screen.queryByRole("button", { name: /cancel job/i })).not.toBeInTheDocument();
  });

  /**
   * Cancelling POSTs, then immediately refetches so the header updates without
   * waiting for the next poll tick.
   */
  it("posts the cancellation and refetches the job", async () => {
    h.getJob
      .mockResolvedValueOnce(makeDetail())
      .mockResolvedValue(
        makeDetail({
          job: makeJob({ id: JOB_ID, name: "nightly-build", status: "cancelled" }),
        })
      );
    const { user } = await renderLoaded();

    await user.click(screen.getByRole("button", { name: /cancel job/i }));

    expect(h.cancelJob).toHaveBeenCalledWith(JOB_ID);
    expect(await screen.findByText("cancelled")).toBeInTheDocument();
    // Terminal now, so the button goes away.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /cancel job/i })).not.toBeInTheDocument()
    );
  });

  /** The button is disabled (with a spinner) while the POST is in flight. */
  it("disables the Cancel button and shows a spinner while cancelling", async () => {
    const gate = deferred<JobInfo>();
    h.cancelJob.mockReturnValue(gate.promise);
    const { user } = await renderLoaded();

    await user.click(screen.getByRole("button", { name: /cancel job/i }));

    const button = screen.getByRole("button", { name: /cancel job/i });
    expect(button).toBeDisabled();
    expect(button.querySelector("svg.animate-spin")).not.toBeNull();

    await act(async () => {
      gate.resolve(makeJob({ id: JOB_ID, status: "cancelled" }));
      await gate.promise;
    });
  });

  /**
   * A rejected cancellation (already finished, insufficient rights, ...) leaves
   * the page usable and the button clickable again rather than stuck.
   */
  it("re-enables the Cancel button when the cancellation fails", async () => {
    h.cancelJob.mockRejectedValue(new Error("job already finished"));
    const { user } = await renderLoaded();

    await user.click(screen.getByRole("button", { name: /cancel job/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /cancel job/i })).toBeEnabled()
    );
    expect(screen.getByText("running")).toBeInTheDocument();
    // The refetch is skipped when the cancel POST itself failed.
    expect(h.getJob).toHaveBeenCalledTimes(1);
  });
});

// ── Full terminal log ───────────────────────────────────────────────────────

/**
 * The persisted per-job transcript (`Job.log_text`), fetched lazily when its
 * tab is opened and refreshed while the job is active.
 */
describe("JobDetail — full terminal log tab", () => {
  /** Lazy: nothing is fetched until the tab is actually opened. */
  it("does not request the terminal log until its tab is opened", async () => {
    const { user } = await renderLoaded();
    expect(h.getJobLog).not.toHaveBeenCalled();

    await user.click(tab("Full Terminal Log"));

    await waitFor(() => expect(h.getJobLog).toHaveBeenCalledWith(JOB_ID));
  });

  it("renders the fetched transcript", async () => {
    h.getJobLog.mockResolvedValue("$ make test\nall tests passed\n");
    const { user } = await renderLoaded();

    await user.click(tab("Full Terminal Log"));

    expect(await screen.findByText(/all tests passed/)).toBeInTheDocument();
    expect(
      screen.getByText("Every command run for this job and its full stdout/stderr.")
    ).toBeInTheDocument();
  });

  /** An empty transcript gets a placeholder, and the download is disabled. */
  it("shows a placeholder and disables the download when there is no transcript", async () => {
    h.getJobLog.mockResolvedValue("");
    const { user } = await renderLoaded();

    await user.click(tab("Full Terminal Log"));

    expect(await screen.findByText("No terminal output captured yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /download \.txt/i })).toBeDisabled();
  });

  /**
   * The .txt download is entirely client-side (the text is already in memory):
   * a Blob URL on a synthetic `<a>` named after the job.
   */
  it("downloads the transcript client-side as job_<id>.txt", async () => {
    h.getJobLog.mockResolvedValue("log body");
    const createObjectURL = vi.fn(() => "blob:fake");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
    let downloadName: string | null = null;
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        downloadName = this.download;
      });

    const { user } = await renderLoaded();
    await user.click(tab("Full Terminal Log"));
    const button = await screen.findByRole("button", { name: /download \.txt/i });
    await waitFor(() => expect(button).toBeEnabled());

    await user.click(button);

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(downloadName).toBe(`job_${JOB_ID}.txt`);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:fake");

    vi.unstubAllGlobals();
  });

  /** While the job is active the transcript is re-fetched on the same 3s cadence. */
  it("refreshes the transcript every 3s while the job is active", async () => {
    vi.useFakeTimers();
    h.getJobLog.mockResolvedValueOnce("first pass").mockResolvedValue("second pass");

    renderDetail();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    fireEvent.click(tab("Full Terminal Log"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("first pass")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(screen.getByText("second pass")).toBeInTheDocument();
  });

  /** A completed job's transcript is fetched once and never polled. */
  it("fetches the transcript only once for a finished job", async () => {
    vi.useFakeTimers();
    h.getJob.mockResolvedValue(completedDetail());
    h.getJobLog.mockResolvedValue("done");

    renderDetail();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    fireEvent.click(tab("Full Terminal Log"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });

    expect(h.getJobLog).toHaveBeenCalledTimes(1);
  });

  /** A failed transcript fetch degrades to the placeholder, not a crash. */
  it("falls back to the placeholder when the transcript request fails", async () => {
    h.getJobLog.mockRejectedValue(new Error("HTTP 500"));
    const { user } = await renderLoaded();

    await user.click(tab("Full Terminal Log"));

    expect(await screen.findByText("No terminal output captured yet.")).toBeInTheDocument();
  });
});

// ── Results tree ────────────────────────────────────────────────────────────

/**
 * The Results tab: the archive summary strip, the Download button, and the
 * tree `buildTree` derives from the flat tarball manifest.
 */
describe("JobDetail — results tree", () => {
  /** Open the Results tab of a job that has a results tarball. */
  async function openResults(manifest: unknown = manifestFixture) {
    h.getJob.mockResolvedValue(makeDetail({ has_results: true }));
    h.getJobResultsManifest.mockResolvedValue(manifest);
    const view = await renderLoaded();
    await view.user.click(tab("Results"));
    return view;
  }

  /** The manifest is fetched lazily, only when the tab opens. */
  it("does not request the manifest until the Results tab is opened", async () => {
    h.getJob.mockResolvedValue(makeDetail({ has_results: true }));
    const { user } = await renderLoaded();
    expect(h.getJobResultsManifest).not.toHaveBeenCalled();

    await user.click(tab("Results"));

    await waitFor(() => expect(h.getJobResultsManifest).toHaveBeenCalledWith(JOB_ID));
  });

  /** Until the manifest lands, the tab is its own loading state. */
  it("shows a 'Reading archive' spinner until the manifest arrives", async () => {
    h.getJob.mockResolvedValue(makeDetail({ has_results: true }));
    h.getJobResultsManifest.mockReturnValue(deferred<unknown>().promise);
    const { user } = await renderLoaded();

    await user.click(tab("Results"));

    expect(screen.getByText(/Reading archive/)).toBeInTheDocument();
  });

  /** The summary strip counts FILES only, alongside the compressed archive size. */
  it("summarises the archive as a file count plus compressed size", async () => {
    await openResults();

    expect(await screen.findByText("results.tar.gz")).toBeInTheDocument();
    expect(screen.getByText(/4 files · 2 KB compressed/)).toBeInTheDocument();
  });

  /** Top-level entries are expanded on mount (depth < 1). */
  it("renders top-level entries expanded", async () => {
    await openResults();

    expect(await screen.findByText("m5out")).toBeInTheDocument();
    expect(screen.getByText("README.txt")).toBeInTheDocument();
    // m5out's own children are visible because it starts open.
    expect(screen.getByText("stats.txt")).toBeInTheDocument();
    expect(screen.getByText("config.json")).toBeInTheDocument();
  });

  /** Directories sort before files, then alphabetically. */
  it("sorts directories before files", async () => {
    await openResults();
    await screen.findByText("m5out");

    const text = document.body.textContent!;
    expect(text.indexOf("m5out")).toBeLessThan(text.indexOf("README.txt"));
    expect(text.indexOf("nested")).toBeLessThan(text.indexOf("config.json"));
  });

  /** Nested directories start collapsed and toggle on click. */
  it("expands and collapses a nested directory on click", async () => {
    const { user } = await openResults();
    const nested = await screen.findByText("nested");
    expect(screen.queryByText("inner.log")).not.toBeInTheDocument();

    await user.click(nested);
    expect(screen.getByText("inner.log")).toBeInTheDocument();

    await user.click(screen.getByText("nested"));
    expect(screen.queryByText("inner.log")).not.toBeInTheDocument();
  });

  /**
   * A directory row reports its child count and the AGGREGATE size of its
   * descendants (1024 + 512 + 256 = 1792 bytes for m5out).
   */
  it("shows each directory's child count and aggregate size", async () => {
    await openResults();
    await screen.findByText("m5out");

    expect(screen.getByText(/3 items · 1.8 KB/)).toBeInTheDocument();
    // Singular for a directory holding exactly one entry.
    expect(screen.getByText(/1 item · 256 B/)).toBeInTheDocument();
  });

  /** File rows show their own (uncompressed) size. */
  it("shows each file's size", async () => {
    await openResults();
    await screen.findByText("README.txt");

    expect(screen.getByText("128 B")).toBeInTheDocument();
    expect(screen.getByText("1 KB")).toBeInTheDocument(); // stats.txt
  });

  /** gem5's primary output gets a highlighted row and a "stats" chip. */
  it("highlights stats.txt with a stats chip", async () => {
    await openResults();

    expect(await screen.findByText("stats")).toBeInTheDocument();
    expect(screen.getByText("stats.txt").className).toMatch(/text-emerald-300/);
  });

  /**
   * A file whose parent directory has no manifest entry still nests correctly —
   * `ensureDir` materialises the missing ancestors.
   */
  it("materialises a directory that only exists implicitly in the manifest", async () => {
    const { user } = await openResults({
      archive_bytes: 100,
      entries: [{ path: "a/b/c.txt", size: 10, is_dir: false }],
    });

    const a = await screen.findByText("a");
    expect(a).toBeInTheDocument();
    await user.click(screen.getByText("b"));
    expect(screen.getByText("c.txt")).toBeInTheDocument();
  });

  /** "m5out/" and "m5out" must resolve to one node, not a phantom pair. */
  it("treats a trailing-slash directory entry and its implied path as one node", async () => {
    await openResults();
    await screen.findByText("m5out");

    expect(screen.getAllByText("m5out")).toHaveLength(1);
  });

  /** An entry that is nothing but slashes is skipped entirely. */
  it("skips manifest entries that are only slashes", async () => {
    await openResults({ archive_bytes: 0, entries: [{ path: "///", size: 0, is_dir: true }] });

    expect(await screen.findByText("Archive is empty.")).toBeInTheDocument();
  });

  /** A results tarball with no entries says so. */
  it("reports an empty archive", async () => {
    await openResults({ archive_bytes: 0, entries: [] });

    expect(await screen.findByText("Archive is empty.")).toBeInTheDocument();
    expect(screen.getByText(/0 files · 0 B compressed/)).toBeInTheDocument();
  });

  /** A failed manifest fetch leaves the tab in its loading state (no error UI). */
  it("stays on the 'Reading archive' state when the manifest request fails", async () => {
    h.getJob.mockResolvedValue(makeDetail({ has_results: true }));
    h.getJobResultsManifest.mockRejectedValue(new Error("HTTP 404"));
    const { user } = await renderLoaded();

    await user.click(tab("Results"));

    await waitFor(() => expect(h.getJobResultsManifest).toHaveBeenCalled());
    expect(screen.getByText(/Reading archive/)).toBeInTheDocument();
  });

  /** The manifest is fetched once per (tab, id) — never polled. */
  it("does not poll the manifest while the job is still running", async () => {
    vi.useFakeTimers();
    h.getJob.mockResolvedValue(makeDetail({ has_results: true }));
    renderDetail();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    fireEvent.click(tab("Results"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });

    expect(h.getJobResultsManifest).toHaveBeenCalledTimes(1);
  });
});

// ── Results download button ─────────────────────────────────────────────────

/**
 * The Download button hands off to `api.downloadJobResults`, which needs the
 * Bearer header and therefore cannot be a plain `<a href>`.
 */
describe("JobDetail — results download", () => {
  async function openResults() {
    h.getJob.mockResolvedValue(makeDetail({ has_results: true }));
    const view = await renderLoaded();
    await view.user.click(tab("Results"));
    await screen.findByText("results.tar.gz");
    return view;
  }

  it("requests the tarball for this job when Download is clicked", async () => {
    const { user } = await openResults();

    await user.click(screen.getByRole("button", { name: /download/i }));

    expect(h.downloadJobResults).toHaveBeenCalledWith(JOB_ID);
  });

  /** In flight: the button is disabled and its icon becomes a spinner. */
  it("disables the Download button and shows a spinner while downloading", async () => {
    const gate = deferred<void>();
    h.downloadJobResults.mockReturnValue(gate.promise);
    const { user } = await openResults();

    await user.click(screen.getByRole("button", { name: /download/i }));

    const button = screen.getByRole("button", { name: /download/i });
    expect(button).toBeDisabled();
    expect(button.querySelector("svg.animate-spin")).not.toBeNull();

    await act(async () => {
      gate.resolve();
      await gate.promise;
    });
    expect(screen.getByRole("button", { name: /download/i })).toBeEnabled();
  });

  /** A failed download re-enables the button (the api client surfaces the error). */
  it("re-enables the Download button when the download fails", async () => {
    h.downloadJobResults.mockRejectedValue(new Error("HTTP 500"));
    const { user } = await openResults();

    await user.click(screen.getByRole("button", { name: /download/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /download/i })).toBeEnabled()
    );
  });
});

// ── Artifacts table ─────────────────────────────────────────────────────────

/** The bottom section: files a job stored in a backend. */
describe("JobDetail — artifacts table", () => {
  const artifact = (overrides: Partial<ArtifactInfo> = {}): ArtifactInfo => ({
    id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    job_id: JOB_ID,
    step_run_id: null,
    filename: "stats.txt",
    storage_backend_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    storage_backend_name: "minio-1",
    storage_key: "jobs/x/stats.txt",
    content_type: "text/plain",
    size_bytes: 1024,
    created_at: new Date().toISOString(),
    ...overrides,
  });

  it("renders one row per artifact with its type, size and backend", async () => {
    h.listArtifacts.mockResolvedValue([artifact()]);

    await renderLoaded();

    const row = (await screen.findByText("stats.txt")).closest("tr")!;
    expect(within(row).getByText("text/plain")).toBeInTheDocument();
    expect(within(row).getByText("1 KB")).toBeInTheDocument();
    expect(within(row).getByText("minio-1")).toBeInTheDocument();
  });

  /** With no backend name, the backend id is shown truncated to 8 chars. */
  it("falls back to a truncated backend id when the backend has no name", async () => {
    h.listArtifacts.mockResolvedValue([artifact({ storage_backend_name: null })]);

    await renderLoaded();

    expect(await screen.findByText("bbbbbbbb")).toBeInTheDocument();
  });

  /** A missing content type renders as a dash rather than "null". */
  it("renders a dash for an artifact with no content type", async () => {
    h.listArtifacts.mockResolvedValue([artifact({ content_type: null })]);

    await renderLoaded();

    const row = (await screen.findByText("stats.txt")).closest("tr")!;
    expect(within(row).getByText("-")).toBeInTheDocument();
  });

  /**
   * Artifacts are downloaded through a plain `<a href>`, unlike the results
   * tarball — documented in the source as only working if that endpoint does
   * not require the Bearer header.
   */
  it("links each artifact to its download endpoint", async () => {
    h.listArtifacts.mockResolvedValue([artifact()]);

    await renderLoaded();

    const link = await screen.findByRole("link", { name: "Download" });
    expect(link).toHaveAttribute(
      "href",
      "/api/artifacts/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/download"
    );
  });
});
