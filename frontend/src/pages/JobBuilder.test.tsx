/**
 * Tests for the Job Builder page (src/pages/JobBuilder.tsx).
 *
 * `JobBuilder` is the only place a job is created, so this file walks the whole
 * composition flow the way an operator does:
 *
 *  1. Palette      — GET /api/steps via `useStepsStore`, grouped into categories
 *                    by step-name prefix, searchable by name or description,
 *                    with loading / no-match states.
 *  2. Canvas       — click (or native-drag) a palette entry to append a step,
 *                    select a card to edit it, remove a card, reorder cards.
 *  3. Param editor — the form is generated entirely from the schema's `fields`
 *                    array (text / number / checkbox), so the tests drive the
 *                    real inputs rather than poking state.
 *  4. Submit       — local validation ("name required", "at least one step"),
 *                    the exact POST payload (empty params stripped, `false`
 *                    kept, exactly one target field, numeric priority), the
 *                    server-error banner, and the navigation to /jobs/{id}.
 *
 * What is real vs stubbed: the page, its helpers (`categorize`, `groupSteps`,
 * `buildDefaultParams`, `ParamEditor`, `FieldInput`) and react-router's real
 * navigation all run for real. Three boundaries are replaced:
 *   - `@/stores` — the three data stores, so loading/empty/populated states are
 *     set directly instead of being faked through the network.
 *   - `@/api/client` — only `submitJob` is ever called from this page.
 *   - `@dnd-kit/*` — see the note on the mock below. dnd-kit's pointer and
 *     keyboard sensors need real layout boxes (jsdom reports every rect as
 *     0x0), so the drag *gestures* cannot be simulated. The stub captures
 *     dnd-kit's own callbacks and the tests fire them, then assert the
 *     user-visible outcome: the order and 1-based numbering of the cards.
 *
 * Neighbouring pieces: the step schemas that drive every form here come from
 * the server's step registry (packages/common/.../steps/registry.py); the
 * submit-time dependency/OUTPUT_KEYS validation is server-side and only ever
 * observed by this page as a message in the error banner.
 */
import { vi, describe, it, expect, beforeEach } from "vitest";
import type { ReactNode } from "react";
import { render, screen, within, act, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route, useParams } from "react-router-dom";
import type { NodeInfo, PoolInfo, StepSchemaInfo, JobInfo } from "@/types";
import { makeStepSchema, makePool, makeNode, makeJob } from "../test/test-utils";

// ── Mocked boundaries ────────────────────────────────────────────────────────
//
// AI Note: everything shared with the (hoisted) `vi.mock` factories has to live
// in `vi.hoisted`. `vi.mock` calls are lifted above every `const`, so a factory
// closing over a normally-declared binding throws "Cannot access 'x' before
// initialization" while the module graph is being built.
const h = vi.hoisted(() => {
  const stepsFetch = vi.fn().mockResolvedValue(undefined);
  const poolsFetch = vi.fn().mockResolvedValue(undefined);
  const nodesFetch = vi.fn().mockResolvedValue(undefined);

  const stepsState: { steps: unknown[]; isLoading: boolean; fetch: typeof stepsFetch } = {
    steps: [],
    isLoading: false,
    fetch: stepsFetch,
  };
  const poolsState: { pools: unknown[]; isLoading: boolean; fetch: typeof poolsFetch } = {
    pools: [],
    isLoading: false,
    fetch: poolsFetch,
  };
  const nodesState: { nodes: unknown[]; isLoading: boolean; fetch: typeof nodesFetch } = {
    nodes: [],
    isLoading: false,
    fetch: nodesFetch,
  };

  const submitJob = vi.fn();

  /**
   * Handles captured from the stubbed `<DndContext>` on every render, plus the
   * id of the card the stubbed `useSortable` should report as being dragged.
   */
  const dnd: {
    onDragStart: ((event: unknown) => void) | null;
    onDragEnd: ((event: unknown) => void) | null;
  } = { onDragStart: null, onDragEnd: null };

  return {
    stepsFetch,
    poolsFetch,
    nodesFetch,
    stepsState,
    poolsState,
    nodesState,
    submitJob,
    dnd,
  };
});

// The page consumes all three stores as bare object destructures
// (`const stepsStore = useStepsStore()`), so a plain state object is enough;
// the selector form is honoured anyway so the mock behaves like zustand.
vi.mock("@/stores", () => ({
  useStepsStore: vi.fn(() => h.stepsState),
  usePoolsStore: vi.fn(() => h.poolsState),
  useNodesStore: vi.fn(() => h.nodesState),
}));

vi.mock("@/api/client", () => ({ api: { submitJob: h.submitJob } }));

// dnd-kit stubs. `DndContext` records the page's drag callbacks so tests can
// commit a reorder; `useSortable` tags each card root with `data-step-id`,
// which is the only way a test can learn the client-side ids `uniqueId()`
// generates (they are deliberately not rendered anywhere).
vi.mock("@dnd-kit/core", () => ({
  DndContext: ({
    children,
    onDragStart,
    onDragEnd,
  }: {
    children: ReactNode;
    onDragStart?: (event: unknown) => void;
    onDragEnd?: (event: unknown) => void;
  }) => {
    h.dnd.onDragStart = onDragStart ?? null;
    h.dnd.onDragEnd = onDragEnd ?? null;
    return <>{children}</>;
  },
  DragOverlay: ({ children }: { children?: ReactNode }) =>
    children ? <div data-testid="drag-overlay">{children}</div> : null,
  closestCenter: () => null,
  KeyboardSensor: function KeyboardSensor() {},
  PointerSensor: function PointerSensor() {},
  useSensor: vi.fn(() => ({})),
  useSensors: vi.fn(() => []),
}));

vi.mock("@dnd-kit/sortable", () => ({
  SortableContext: ({ children }: { children: ReactNode }) => <>{children}</>,
  sortableKeyboardCoordinates: () => null,
  verticalListSortingStrategy: "vertical",
  useSortable: ({ id }: { id: string }) => ({
    attributes: {},
    listeners: {},
    setNodeRef: (el: HTMLElement | null) => {
      if (el) el.setAttribute("data-step-id", id);
    },
    transform: null,
    transition: undefined,
    isDragging: false,
  }),
  /** Same semantics as the real helper: move `from` to `to`, non-mutating. */
  arrayMove: <T,>(arr: T[], from: number, to: number): T[] => {
    const copy = arr.slice();
    copy.splice(to, 0, ...copy.splice(from, 1));
    return copy;
  },
}));

import JobBuilder from "./JobBuilder";

/** Narrowed aliases so tests can seed typed fixtures into the hoisted state. */
const stepsState = h.stepsState as { steps: StepSchemaInfo[]; isLoading: boolean; fetch: typeof h.stepsFetch };
const poolsState = h.poolsState as { pools: PoolInfo[]; isLoading: boolean; fetch: typeof h.poolsFetch };
const nodesState = h.nodesState as { nodes: NodeInfo[]; isLoading: boolean; fetch: typeof h.nodesFetch };

// ── Fixtures ────────────────────────────────────────────────────────────────

/**
 * A three-field shell step: a required text field, a number field with a
 * schema default, and a boolean. Exercises all three `FieldInput` branches
 * from one card.
 */
const shellSchema = makeStepSchema({
  name: "shell_run",
  description: "Run a shell command on the target node",
  fields: [
    {
      name: "command",
      required: true,
      description: "Command to run",
      default: null,
      examples: ["echo hi"],
      field_type: "string",
    },
    {
      name: "timeout",
      required: false,
      description: "Seconds before giving up",
      default: 30,
      examples: ["60"],
      field_type: "integer",
    },
    {
      name: "capture",
      required: false,
      description: "Capture stdout",
      default: null,
      examples: [],
      field_type: "boolean",
    },
  ],
});

/** A server-side step (`requires_node: false`) with no configurable params. */
const flowSchema = makeStepSchema({
  name: "flow_wait",
  description: "Pause the pipeline for a while",
  requires_node: false,
  supported_os: ["linux"],
  fields: [],
  rules: [],
});

/** A second node step, in its own palette category. */
const gem5Schema = makeStepSchema({
  name: "gem5_run",
  description: "Launch a gem5 simulation",
  fields: [
    {
      name: "binary",
      required: true,
      description: "Workload binary",
      default: null,
      examples: ["/bin/hello"],
      field_type: "string",
    },
  ],
});

/** Prefix "run" is not in STEP_CATEGORIES, so this lands in "Other". */
const otherSchema = makeStepSchema({
  name: "run_command",
  description: "Legacy command runner",
  fields: [],
  rules: [],
});

const poolA = makePool({ name: "pool-a", node_count: 3 });
const poolB = makePool({ name: "pool-b", node_count: 0 });
const onlineNode = makeNode({ display_name: "Node 1", status: "online" });
const offlineNode = makeNode({ display_name: "Node 2", hostname: "node-2.test", status: "offline" });
const busyNode = makeNode({ display_name: "Node 3", hostname: "node-3.test", status: "busy" });

beforeEach(() => {
  stepsState.steps = [shellSchema, flowSchema, gem5Schema, otherSchema];
  stepsState.isLoading = false;
  stepsState.fetch = h.stepsFetch.mockResolvedValue(undefined);
  poolsState.pools = [poolA, poolB];
  poolsState.isLoading = false;
  poolsState.fetch = h.poolsFetch.mockResolvedValue(undefined);
  nodesState.nodes = [onlineNode, offlineNode, busyNode];
  nodesState.isLoading = false;
  nodesState.fetch = h.nodesFetch.mockResolvedValue(undefined);
  h.submitJob.mockReset();
  h.submitJob.mockResolvedValue(makeJob({ name: "submitted" }));
  h.dnd.onDragStart = null;
  h.dnd.onDragEnd = null;
});

// ── Harness ─────────────────────────────────────────────────────────────────

/**
 * Probe standing in for the job detail route, so the post-submit navigation is
 * observable without reaching into router internals.
 */
function JobDetailProbe() {
  const { id } = useParams<{ id: string }>();
  return <div data-testid="job-detail-probe">{`detail:${id}`}</div>;
}

/** Render the builder at /jobs/new with a real router and a detail probe. */
function renderBuilder() {
  const user = userEvent.setup();
  const view = render(
    <MemoryRouter initialEntries={["/jobs/new"]}>
      <Routes>
        <Route path="/jobs/new" element={<JobBuilder />} />
        <Route path="/jobs/:id" element={<JobDetailProbe />} />
      </Routes>
    </MemoryRouter>
  );
  return { user, ...view };
}

// ── Query helpers ───────────────────────────────────────────────────────────

/**
 * A palette entry by step name.
 *
 * Palette entries are real `<button draggable>` elements whose first child is
 * the name; canvas cards are `<div role="button">`. Matching on `draggable`
 * keeps the two apart without depending on styling.
 */
function paletteItem(name: string): HTMLElement {
  const found = screen
    .getAllByRole("button")
    .find(
      (b) => b.getAttribute("draggable") === "true" && b.firstElementChild?.textContent === name
    );
  if (!found) throw new Error(`No palette item named "${name}"`);
  return found;
}

/** Every canvas card, in DOM (= pipeline) order. Tagged by the useSortable stub. */
const cards = () => Array.from(document.querySelectorAll<HTMLElement>("[data-step-id]"));

/** The client-side step ids of the canvas cards, in pipeline order. */
const cardIds = () => cards().map((c) => c.getAttribute("data-step-id")!);

/**
 * `{position, step}` for each canvas card, in pipeline order.
 *
 * Read positionally from the card's children (grip button, position badge,
 * content column, remove button) rather than by class name, so Tailwind tweaks
 * do not break the tests.
 */
function canvasRows(): Array<{ position: string; step: string }> {
  return cards().map((c) => ({
    position: c.children[1]!.textContent!.trim(),
    step: c.children[2]!.firstElementChild!.textContent!.trim(),
  }));
}

/** The `<select>` that owns an option with this label (the selects have no accessible name). */
const selectWithOption = (optionLabel: string): HTMLSelectElement =>
  screen.getByRole("option", { name: optionLabel }).closest("select")!;

const jobNameInput = () => screen.getByPlaceholderText("e.g. nightly-build-check");
const searchInput = () => screen.getByPlaceholderText("Filter steps...");
const submitButton = () => screen.getByRole("button", { name: /submit job/i });
const categoryHeadings = () =>
  screen.queryAllByRole("heading", { level: 3 }).map((el) => el.textContent);

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

// ── Mount + palette data ────────────────────────────────────────────────────

/** The three GETs the page issues on mount, and the palette's three states. */
describe("JobBuilder — mount and palette data", () => {
  /**
   * One fetch per store per mount. Regression guarded: listing the store
   * objects as effect deps (they get a new identity on every store update)
   * turns this into an infinite request loop — the reason the page disables
   * exhaustive-deps there.
   */
  it("fetches steps, pools and nodes exactly once on mount", () => {
    renderBuilder();

    expect(h.stepsFetch).toHaveBeenCalledTimes(1);
    expect(h.poolsFetch).toHaveBeenCalledTimes(1);
    expect(h.nodesFetch).toHaveBeenCalledTimes(1);
  });

  /** Interacting with the page must not re-issue the catalogue fetches. */
  it("does not refetch the catalogues when the user types in the search box", async () => {
    const { user } = renderBuilder();

    await user.type(searchInput(), "shell");

    expect(h.stepsFetch).toHaveBeenCalledTimes(1);
    expect(h.poolsFetch).toHaveBeenCalledTimes(1);
    expect(h.nodesFetch).toHaveBeenCalledTimes(1);
  });

  /** The static chrome is present before any data arrives. */
  it("renders the page heading and palette header", () => {
    renderBuilder();

    expect(screen.getByRole("heading", { name: "Job Builder", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /step palette/i })).toBeInTheDocument();
    expect(
      screen.getByText("Drag steps from the palette or click to add. Reorder by dragging.")
    ).toBeInTheDocument();
  });

  /** While GET /api/steps is outstanding the palette shows a loading line, not "no matches". */
  it("shows a loading line and no empty-state while the step catalogue loads", () => {
    stepsState.isLoading = true;
    stepsState.steps = [];

    renderBuilder();

    expect(screen.getByText("Loading steps...")).toBeInTheDocument();
    expect(screen.queryByText("No steps match your search.")).not.toBeInTheDocument();
  });

  /** Every schema the server advertises becomes a clickable palette entry. */
  it("renders one palette entry per step schema, with its description", () => {
    renderBuilder();

    expect(paletteItem("shell_run")).toBeInTheDocument();
    expect(paletteItem("flow_wait")).toBeInTheDocument();
    expect(paletteItem("gem5_run")).toBeInTheDocument();
    expect(paletteItem("run_command")).toBeInTheDocument();
    expect(screen.getByText("Run a shell command on the target node")).toBeInTheDocument();
  });

  /** Each entry advertises the OSes its step supports. */
  it("renders an OS chip for every supported_os of a palette entry", () => {
    renderBuilder();

    const item = paletteItem("shell_run");
    expect(within(item).getByText("macos")).toBeInTheDocument();
    expect(within(item).getByText("linux")).toBeInTheDocument();
    expect(within(item).getByText("windows")).toBeInTheDocument();
    // flow_wait declares linux only.
    expect(within(paletteItem("flow_wait")).getAllByText(/^(macos|linux|windows)$/)).toHaveLength(1);
  });

  /**
   * Category grouping and ordering: prefix -> heading via STEP_CATEGORIES, with
   * the "Other" bucket forced last. Regression guarded: losing the insertion
   * -order sort in `groupSteps` scatters the palette.
   */
  it("groups entries into prefix categories and renders Other last", () => {
    renderBuilder();

    expect(categoryHeadings()).toEqual(["Flow Control", "gem5", "Shell", "Other"]);
    // The unprefixed "run_command" is what lands in Other.
    const other = screen.getByRole("heading", { name: "Other" }).parentElement!;
    expect(within(other).getByText("run_command")).toBeInTheDocument();
  });

  /** An empty (but loaded) catalogue reuses the no-match copy rather than a blank column. */
  it("shows the no-match message when the server advertises no steps at all", () => {
    stepsState.steps = [];

    renderBuilder();

    expect(screen.getByText("No steps match your search.")).toBeInTheDocument();
    expect(categoryHeadings()).toEqual([]);
  });
});

// ── Palette search ──────────────────────────────────────────────────────────

/** The filter box matches name OR description, case-insensitively. */
describe("JobBuilder — palette search", () => {
  it("filters palette entries by step name", async () => {
    const { user } = renderBuilder();

    await user.type(searchInput(), "gem5");

    expect(paletteItem("gem5_run")).toBeInTheDocument();
    expect(screen.queryByText("shell_run")).not.toBeInTheDocument();
    expect(categoryHeadings()).toEqual(["gem5"]);
  });

  /** Descriptions are searched too, so "simulation"-style queries work. */
  it("filters palette entries by description text", async () => {
    const { user } = renderBuilder();

    await user.type(searchInput(), "pause the pipeline");

    expect(paletteItem("flow_wait")).toBeInTheDocument();
    expect(screen.queryByText("gem5_run")).not.toBeInTheDocument();
  });

  it("matches case-insensitively", async () => {
    const { user } = renderBuilder();

    await user.type(searchInput(), "SHELL");

    expect(paletteItem("shell_run")).toBeInTheDocument();
  });

  /** Whitespace-only input is treated as "no filter", not as "match nothing". */
  it("treats a whitespace-only query as no filter", async () => {
    const { user } = renderBuilder();

    await user.type(searchInput(), "   ");

    expect(categoryHeadings()).toEqual(["Flow Control", "gem5", "Shell", "Other"]);
  });

  it("shows the no-match message for a query nothing satisfies", async () => {
    const { user } = renderBuilder();

    await user.type(searchInput(), "nosuchstep");

    expect(screen.getByText("No steps match your search.")).toBeInTheDocument();
    expect(categoryHeadings()).toEqual([]);
  });

  /**
   * A hidden-by-search step must still resolve its schema on the canvas —
   * `schemaMap` is built from the unfiltered list on purpose.
   */
  it("keeps an already-added card fully functional after the search hides its palette entry", async () => {
    const { user } = renderBuilder();

    await user.click(paletteItem("flow_wait"));
    await user.type(searchInput(), "gem5");

    expect(canvasRows()).toEqual([{ position: "1", step: "flow_wait" }]);
    // The schema still resolves: the "no node" chip and the editor heading need it.
    expect(screen.getByText("no node")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Configure: flow_wait" })).toBeInTheDocument();
  });
});

// ── Adding steps ────────────────────────────────────────────────────────────

/** Click-to-add and drag-to-add, both of which append to the end of the canvas. */
describe("JobBuilder — adding steps", () => {
  /** The empty canvas invites a drag rather than rendering an empty list. */
  it("renders the empty-canvas prompt when no steps have been added", () => {
    renderBuilder();

    expect(screen.getByText("Drag steps here to build your job")).toBeInTheDocument();
    expect(screen.getByText("or click a step in the palette to add it")).toBeInTheDocument();
    expect(cards()).toHaveLength(0);
  });

  it("appends a card and drops the empty-canvas prompt when a palette entry is clicked", async () => {
    const { user } = renderBuilder();

    await user.click(paletteItem("shell_run"));

    expect(canvasRows()).toEqual([{ position: "1", step: "shell_run" }]);
    expect(screen.queryByText("Drag steps here to build your job")).not.toBeInTheDocument();
  });

  /** The freshly added step is selected, so the user lands straight in its form. */
  it("selects the newly added step and opens its parameter form", async () => {
    const { user } = renderBuilder();

    await user.click(paletteItem("gem5_run"));

    const heading = screen.getByRole("heading", { name: "Configure: gem5_run" });
    // The editor repeats the schema description under its heading (the palette
    // entry shows the same text, hence the scoped query).
    expect(within(heading.parentElement!).getByText("Launch a gem5 simulation")).toBeInTheDocument();
  });

  /** Steps accumulate in click order and are numbered 1..n. */
  it("appends steps in click order with 1-based position badges", async () => {
    const { user } = renderBuilder();

    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("flow_wait"));
    await user.click(paletteItem("gem5_run"));

    expect(canvasRows()).toEqual([
      { position: "1", step: "shell_run" },
      { position: "2", step: "flow_wait" },
      { position: "3", step: "gem5_run" },
    ]);
  });

  /**
   * The same step type can appear twice with independent params — `uniqueId()`
   * is what keeps the two cards distinct.
   */
  it("allows the same step type twice, as two independently-keyed cards", async () => {
    const { user } = renderBuilder();

    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("shell_run"));

    expect(canvasRows()).toHaveLength(2);
    const [first, second] = cardIds();
    expect(first).not.toEqual(second);
  });

  /** Steps declaring `requires_node: false` are chipped as server-side. */
  it("marks a requires_node:false step with the 'no node' chip and omits it otherwise", async () => {
    const { user } = renderBuilder();

    await user.click(paletteItem("flow_wait"));
    await user.click(paletteItem("shell_run"));

    const [flowCard, shellCard] = cards();
    expect(within(flowCard!).getByText("no node")).toBeInTheDocument();
    expect(within(shellCard!).queryByText("no node")).not.toBeInTheDocument();
  });

  /** Native palette -> canvas drag carries the step name under a private MIME type. */
  it("puts the step name on the dataTransfer when a palette entry starts a native drag", () => {
    renderBuilder();
    const setData = vi.fn();

    fireEvent.dragStart(paletteItem("gem5_run"), {
      dataTransfer: { setData, effectAllowed: "none" },
    });

    expect(setData).toHaveBeenCalledWith("application/nexus-step", "gem5_run");
  });

  /** Dropping a palette item on the canvas appends it, exactly like a click. */
  it("appends the dropped step when the canvas receives a nexus-step drop", () => {
    renderBuilder();

    fireEvent.drop(screen.getByText("Drag steps here to build your job"), {
      dataTransfer: {
        getData: (type: string) => (type === "application/nexus-step" ? "shell_run" : ""),
        types: ["application/nexus-step"],
      },
    });

    expect(canvasRows()).toEqual([{ position: "1", step: "shell_run" }]);
  });

  /** A drop carrying an unknown step name is ignored rather than adding a broken card. */
  it("ignores a drop naming a step the server does not advertise", () => {
    renderBuilder();

    fireEvent.drop(screen.getByText("Drag steps here to build your job"), {
      dataTransfer: {
        getData: () => "not_a_real_step",
        types: ["application/nexus-step"],
      },
    });

    expect(cards()).toHaveLength(0);
    expect(screen.getByText("Drag steps here to build your job")).toBeInTheDocument();
  });

  /** A drop with no payload at all (e.g. dragged text) is ignored. */
  it("ignores a drop with no nexus-step payload", () => {
    renderBuilder();

    fireEvent.drop(screen.getByText("Drag steps here to build your job"), {
      dataTransfer: { getData: () => "", types: ["text/plain"] },
    });

    expect(cards()).toHaveLength(0);
  });

  /**
   * `dragOver` only calls `preventDefault` for our own MIME type — which is
   * what makes the canvas a drop target for palette items and a no-op for
   * files or arbitrary text. `fireEvent` returns false when the default was
   * prevented, so that is the observable signal.
   */
  it("accepts a dragover carrying application/nexus-step and rejects other types", () => {
    renderBuilder();
    const canvas = screen.getByText("Drag steps here to build your job");

    const accepted = fireEvent.dragOver(canvas, {
      dataTransfer: { types: ["application/nexus-step"], dropEffect: "none" },
    });
    const ignored = fireEvent.dragOver(canvas, {
      dataTransfer: { types: ["Files"], dropEffect: "none" },
    });

    expect(accepted).toBe(false); // preventDefault() -> drop can fire
    expect(ignored).toBe(true); // left alone -> no drop
  });
});

// ── Selecting and removing steps ────────────────────────────────────────────

/** Card selection drives the right-hand editor; removal must never orphan it. */
describe("JobBuilder — selecting and removing steps", () => {
  it("switches the parameter form when another card is clicked", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("gem5_run"));
    expect(screen.getByRole("heading", { name: "Configure: gem5_run" })).toBeInTheDocument();

    await user.click(cards()[0]!);

    expect(screen.getByRole("heading", { name: "Configure: shell_run" })).toBeInTheDocument();
  });

  /** The card root is keyboard-activatable (Enter), since it is a div with role=button. */
  it("selects a card with the Enter key", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("gem5_run"));

    cards()[0]!.focus();
    await user.keyboard("{Enter}");

    expect(screen.getByRole("heading", { name: "Configure: shell_run" })).toBeInTheDocument();
  });

  /** The visual selection ring follows the selected card. */
  it("rings only the selected card", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("gem5_run"));

    const [shellCard, gem5Card] = cards();
    expect(gem5Card!.className).toMatch(/ring-2/);
    expect(shellCard!.className).not.toMatch(/ring-2/);
  });

  it("removes only the clicked card and renumbers the rest", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("flow_wait"));
    await user.click(paletteItem("gem5_run"));

    await user.click(within(cards()[1]!).getByRole("button", { name: "Remove step" }));

    expect(canvasRows()).toEqual([
      { position: "1", step: "shell_run" },
      { position: "2", step: "gem5_run" },
    ]);
  });

  /**
   * Removing the card being edited clears the selection. Regression guarded:
   * keeping a dead id would leave the right panel showing a step that is no
   * longer in the pipeline.
   */
  it("clears the parameter form when the selected card is removed", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("gem5_run"));

    await user.click(within(cards()[1]!).getByRole("button", { name: "Remove step" }));

    expect(screen.queryByRole("heading", { name: /^Configure:/ })).not.toBeInTheDocument();
    expect(
      screen.getByText("Select a step in the canvas to configure its parameters")
    ).toBeInTheDocument();
  });

  /** Removing an unselected card leaves the editor where it was. */
  it("keeps the current selection when a different card is removed", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("gem5_run"));

    await user.click(within(cards()[0]!).getByRole("button", { name: "Remove step" }));

    expect(screen.getByRole("heading", { name: "Configure: gem5_run" })).toBeInTheDocument();
  });

  /** Emptying the canvas restores both the drop prompt and the "add steps" hint. */
  it("returns to the empty state after the last card is removed", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));

    await user.click(within(cards()[0]!).getByRole("button", { name: "Remove step" }));

    expect(screen.getByText("Drag steps here to build your job")).toBeInTheDocument();
    expect(screen.getByText("Add steps to get started")).toBeInTheDocument();
  });

  /** Clicking the X must not also select the card underneath it. */
  it("does not select the card when its remove button is clicked", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("gem5_run"));
    await user.click(paletteItem("flow_wait")); // flow_wait selected

    await user.click(within(cards()[0]!).getByRole("button", { name: "Remove step" }));

    expect(screen.getByRole("heading", { name: "Configure: flow_wait" })).toBeInTheDocument();
  });
});

// ── Reordering ──────────────────────────────────────────────────────────────

/**
 * Reorder outcomes, driven through the callbacks the real `<DndContext>` would
 * invoke (see the dnd-kit note in the file header).
 */
describe("JobBuilder — reordering the canvas", () => {
  /** Add three steps and hand back their ids in pipeline order. */
  async function threeSteps() {
    const view = renderBuilder();
    await view.user.click(paletteItem("shell_run"));
    await view.user.click(paletteItem("flow_wait"));
    await view.user.click(paletteItem("gem5_run"));
    return { ...view, ids: cardIds() };
  }

  it("moves a card down when it is dropped on a later card", async () => {
    const { ids } = await threeSteps();

    act(() => h.dnd.onDragEnd!({ active: { id: ids[0] }, over: { id: ids[2] } }));

    expect(canvasRows()).toEqual([
      { position: "1", step: "flow_wait" },
      { position: "2", step: "gem5_run" },
      { position: "3", step: "shell_run" },
    ]);
  });

  it("moves a card up when it is dropped on an earlier card", async () => {
    const { ids } = await threeSteps();

    act(() => h.dnd.onDragEnd!({ active: { id: ids[2] }, over: { id: ids[0] } }));

    expect(canvasRows().map((r) => r.step)).toEqual(["gem5_run", "shell_run", "flow_wait"]);
  });

  /** Dropping a card on itself is a no-op. */
  it("leaves the order untouched when a card is dropped on itself", async () => {
    const { ids } = await threeSteps();

    act(() => h.dnd.onDragEnd!({ active: { id: ids[1] }, over: { id: ids[1] } }));

    expect(canvasRows().map((r) => r.step)).toEqual(["shell_run", "flow_wait", "gem5_run"]);
  });

  /** A drag cancelled outside any droppable (`over: null`) must not reorder. */
  it("leaves the order untouched when the drag ends outside the list", async () => {
    const { ids } = await threeSteps();

    act(() => h.dnd.onDragEnd!({ active: { id: ids[0] }, over: null }));

    expect(canvasRows().map((r) => r.step)).toEqual(["shell_run", "flow_wait", "gem5_run"]);
  });

  /**
   * The `oldIndex/newIndex === -1` guard: a stale id (e.g. a card removed
   * mid-drag) returns the previous list rather than corrupting it.
   */
  it("leaves the order untouched when the dragged id is no longer in the list", async () => {
    const { ids } = await threeSteps();

    act(() => h.dnd.onDragEnd!({ active: { id: "step-gone-999" }, over: { id: ids[0] } }));

    expect(canvasRows().map((r) => r.step)).toEqual(["shell_run", "flow_wait", "gem5_run"]);
  });

  /** Reordering does not disturb which card is being edited. */
  it("keeps the selected step selected across a reorder", async () => {
    const { ids } = await threeSteps(); // gem5_run is selected (added last)

    act(() => h.dnd.onDragEnd!({ active: { id: ids[2] }, over: { id: ids[0] } }));

    expect(screen.getByRole("heading", { name: "Configure: gem5_run" })).toBeInTheDocument();
    expect(cards()[0]!.className).toMatch(/ring-2/);
  });

  /** The follow-the-cursor overlay appears for the dragged card and is torn down after the drop. */
  it("renders a drag overlay card while a step is being dragged", async () => {
    const { ids } = await threeSteps();
    expect(screen.queryByTestId("drag-overlay")).not.toBeInTheDocument();

    act(() => h.dnd.onDragStart!({ active: { id: ids[1] } }));

    const overlay = screen.getByTestId("drag-overlay");
    expect(within(overlay).getByText("flow_wait")).toBeInTheDocument();
    expect(within(overlay).getByText("2")).toBeInTheDocument();

    act(() => h.dnd.onDragEnd!({ active: { id: ids[1] }, over: { id: ids[0] } }));

    expect(screen.queryByTestId("drag-overlay")).not.toBeInTheDocument();
  });
});

// ── Parameter editing ───────────────────────────────────────────────────────

/** The schema-driven form: one control per field, plus the card's summary line. */
describe("JobBuilder — parameter editing", () => {
  /** Add a shell step and select it. */
  async function withShellStep() {
    const view = renderBuilder();
    await view.user.click(paletteItem("shell_run"));
    return view;
  }

  /** Field defaults come from the schema; text/boolean fall back to ""/false. */
  it("seeds each control from the schema default", async () => {
    await withShellStep();

    expect(screen.getByPlaceholderText("e.g. echo hi")).toHaveValue("");
    expect(screen.getByRole("spinbutton")).toHaveValue(30);
    expect(screen.getByRole("checkbox")).not.toBeChecked();
  });

  /** Required fields are flagged with an asterisk (labelling only — no client gate). */
  it("marks required fields with an asterisk and optional ones without", async () => {
    await withShellStep();

    expect(screen.getByText("command *")).toBeInTheDocument();
    expect(screen.getByText("timeout")).toBeInTheDocument();
    expect(screen.getByText("capture")).toBeInTheDocument();
  });

  /** Descriptions and the first example (as a placeholder) are surfaced. */
  it("renders each field's description and example placeholder", async () => {
    await withShellStep();

    expect(screen.getByText("Command to run")).toBeInTheDocument();
    expect(screen.getByText("Seconds before giving up")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("e.g. echo hi")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("e.g. 60")).toBeInTheDocument();
  });

  it("stores typed text and mirrors it into the card's param summary", async () => {
    const { user } = await withShellStep();

    await user.type(screen.getByPlaceholderText("e.g. echo hi"), "ls -la");

    expect(screen.getByPlaceholderText("e.g. echo hi")).toHaveValue("ls -la");
    expect(within(cards()[0]!).getByText("command=ls -la, timeout=30")).toBeInTheDocument();
  });

  /** Long values are clipped at 30 chars in the summary (a preview, not a source of truth). */
  it("truncates a long value at 30 characters in the card summary", async () => {
    const { user } = await withShellStep();

    await user.type(screen.getByPlaceholderText("e.g. echo hi"), "a".repeat(40));

    expect(within(cards()[0]!).getByText(`command=${"a".repeat(30)}..., timeout=30`)).toBeInTheDocument();
  });

  it("toggles a boolean field through its checkbox", async () => {
    const { user } = await withShellStep();

    await user.click(screen.getByRole("checkbox"));

    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(within(cards()[0]!).getByText("timeout=30, capture=true")).toBeInTheDocument();
  });

  /** A `false` boolean is treated as "unset" by the summary line (but is still submitted). */
  it("omits an unchecked boolean from the card summary", async () => {
    await withShellStep();

    expect(within(cards()[0]!).getByText("timeout=30")).toBeInTheDocument();
  });

  /** Numeric fields coerce through `Number`, so the payload gets a number not a string. */
  it("coerces a numeric field to a number", async () => {
    const { user } = await withShellStep();
    const numberInput = screen.getByRole("spinbutton");

    await user.clear(numberInput);
    await user.type(numberInput, "90");

    expect(numberInput).toHaveValue(90);
    expect(within(cards()[0]!).getByText("timeout=90")).toBeInTheDocument();
  });

  /**
   * Clearing a numeric field leaves it empty rather than snapping to 0 — the
   * value is stripped at submit time so the server applies its own default.
   */
  it("keeps a cleared numeric field empty instead of falling back to 0", async () => {
    const { user } = await withShellStep();
    const numberInput = screen.getByRole("spinbutton");

    await user.clear(numberInput);

    expect(numberInput).toHaveValue(null);
    expect(within(cards()[0]!).queryByText(/timeout=/)).not.toBeInTheDocument();
  });

  /** A step declaring no fields says so instead of rendering an empty form. */
  it("tells the user when a step has no configurable parameters", async () => {
    const { user } = renderBuilder();

    await user.click(paletteItem("flow_wait"));

    expect(screen.getByText("This step has no configurable parameters.")).toBeInTheDocument();
  });

  /** Params are per-card: editing one instance must not bleed into the other. */
  it("keeps params independent between two cards of the same step type", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("shell_run"));

    // Second card is selected; give it a distinct command.
    await user.type(screen.getByPlaceholderText("e.g. echo hi"), "second");
    await user.click(cards()[0]!);

    expect(screen.getByPlaceholderText("e.g. echo hi")).toHaveValue("");
    expect(within(cards()[1]!).getByText("command=second, timeout=30")).toBeInTheDocument();
  });

  /**
   * A card whose schema the server no longer advertises still renders, but the
   * editor falls back to the placeholder rather than crashing. Documents the
   * `schemaMap.get(...) === undefined` path.
   */
  it("falls back to the placeholder when the selected step's schema disappears", async () => {
    const { user, rerender } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    expect(screen.getByRole("heading", { name: "Configure: shell_run" })).toBeInTheDocument();

    // The catalogue reloads without shell_run.
    stepsState.steps = [flowSchema, gem5Schema];
    rerender(
      <MemoryRouter initialEntries={["/jobs/new"]}>
        <Routes>
          <Route path="/jobs/new" element={<JobBuilder />} />
          <Route path="/jobs/:id" element={<JobDetailProbe />} />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.queryByRole("heading", { name: /^Configure:/ })).not.toBeInTheDocument();
    expect(
      screen.getByText("Select a step in the canvas to configure its parameters")
    ).toBeInTheDocument();
  });
});

// ── Target and priority ─────────────────────────────────────────────────────

/** The submission form's target selector and priority dropdown. */
describe("JobBuilder — target and priority controls", () => {
  /** Pool targeting is the default mode. */
  it("starts in Pool mode with a pool dropdown listing every pool and its node count", () => {
    renderBuilder();

    const select = selectWithOption("-- Select Pool --");
    expect(within(select).getByRole("option", { name: "pool-a (3 nodes)" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "pool-b (0 nodes)" })).toBeInTheDocument();
  });

  /** Switching to Node mode swaps the dropdown entirely. */
  it("swaps to a node dropdown when Node mode is selected", async () => {
    const { user } = renderBuilder();

    await user.click(screen.getByRole("button", { name: "Node" }));

    expect(screen.getByRole("option", { name: "-- Select Node --" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "-- Select Pool --" })).not.toBeInTheDocument();
  });

  /**
   * Only `online` nodes are offered. Documents current behaviour: `busy` nodes
   * are valid scheduler targets but are filtered out here, which can make a
   * healthy cluster look like it has none available.
   */
  it("offers only online nodes, excluding offline and busy ones", async () => {
    const { user } = renderBuilder();

    await user.click(screen.getByRole("button", { name: "Node" }));

    const select = selectWithOption("-- Select Node --");
    expect(within(select).getByRole("option", { name: "Node 1 (linux / x86_64)" })).toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: /Node 2/ })).not.toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: /Node 3/ })).not.toBeInTheDocument();
  });

  /** Mode is a toggle: the active button is highlighted. */
  it("highlights the active target mode button", async () => {
    const { user } = renderBuilder();
    const pool = screen.getByRole("button", { name: "Pool" });
    const node = screen.getByRole("button", { name: "Node" });
    expect(pool.className).toMatch(/border-indigo-300/);
    expect(node.className).not.toMatch(/border-indigo-300/);

    await user.click(node);

    expect(node.className).toMatch(/border-indigo-300/);
    expect(pool.className).not.toMatch(/border-indigo-300/);
  });

  /** Priority defaults to Normal and offers the three documented levels. */
  it("defaults the priority dropdown to Normal", () => {
    renderBuilder();

    const select = selectWithOption("Normal");
    expect(select).toHaveValue("normal");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "High",
      "Normal",
      "Low",
    ]);
  });
});

// ── Submission ──────────────────────────────────────────────────────────────

/** Local validation, the POST payload, the error banner and the navigation. */
describe("JobBuilder — submitting a job", () => {
  it("blocks submission with a validation message when the name is empty", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));

    await user.click(submitButton());

    expect(screen.getByText("Job name is required.")).toBeInTheDocument();
    expect(h.submitJob).not.toHaveBeenCalled();
  });

  /** A whitespace-only name is treated as empty (the payload sends the trimmed value). */
  it("treats a whitespace-only name as missing", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.type(jobNameInput(), "   ");

    await user.click(submitButton());

    expect(screen.getByText("Job name is required.")).toBeInTheDocument();
    expect(h.submitJob).not.toHaveBeenCalled();
  });

  /** Name checked before steps, so an empty canvas with a name reports the step error. */
  it("blocks submission when the canvas is empty", async () => {
    const { user } = renderBuilder();
    await user.type(jobNameInput(), "nightly");

    await user.click(submitButton());

    expect(screen.getByText("Add at least one step.")).toBeInTheDocument();
    expect(h.submitJob).not.toHaveBeenCalled();
  });

  /**
   * The happy path, end to end: trimmed name, ordered steps, numeric priority,
   * both target fields undefined when nothing is picked, then navigation to the
   * created job.
   */
  it("posts the assembled pipeline and navigates to the new job", async () => {
    h.submitJob.mockResolvedValue(makeJob({ id: "job-42" as JobInfo["id"], name: "nightly" }));
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.type(screen.getByPlaceholderText("e.g. echo hi"), "make test");
    await user.click(paletteItem("flow_wait"));
    await user.type(jobNameInput(), "  nightly  ");

    await user.click(submitButton());

    expect(h.submitJob).toHaveBeenCalledTimes(1);
    expect(h.submitJob).toHaveBeenCalledWith({
      name: "nightly",
      steps: [
        { step: "shell_run", params: { command: "make test", timeout: 30, capture: false } },
        { step: "flow_wait", params: {} },
      ],
      target_pool_id: undefined,
      target_node_id: undefined,
      priority: 5,
    });
    expect(await screen.findByTestId("job-detail-probe")).toHaveTextContent("detail:job-42");
  });

  /**
   * Empty strings are stripped so the server applies its own defaults, while an
   * explicitly-off boolean IS sent — it is a meaningful value.
   */
  it("strips empty params but keeps an explicit false", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.type(jobNameInput(), "j");

    await user.click(submitButton());

    expect(h.submitJob).toHaveBeenCalledWith(
      expect.objectContaining({
        steps: [{ step: "shell_run", params: { timeout: 30, capture: false } }],
      })
    );
  });

  it("sends target_pool_id (and no node) when a pool is selected", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("flow_wait"));
    await user.type(jobNameInput(), "j");
    await user.selectOptions(selectWithOption("-- Select Pool --"), poolA.id);

    await user.click(submitButton());

    expect(h.submitJob).toHaveBeenCalledWith(
      expect.objectContaining({ target_pool_id: poolA.id, target_node_id: undefined })
    );
  });

  it("sends target_node_id (and no pool) when a node is selected", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("flow_wait"));
    await user.type(jobNameInput(), "j");
    await user.click(screen.getByRole("button", { name: "Node" }));
    await user.selectOptions(selectWithOption("-- Select Node --"), onlineNode.id);

    await user.click(submitButton());

    expect(h.submitJob).toHaveBeenCalledWith(
      expect.objectContaining({ target_node_id: onlineNode.id, target_pool_id: undefined })
    );
  });

  /**
   * Switching mode after picking a pool drops the pool from the payload — the
   * two target fields are mutually exclusive by construction.
   */
  it("drops a previously-picked pool when the user switches to Node mode", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("flow_wait"));
    await user.type(jobNameInput(), "j");
    await user.selectOptions(selectWithOption("-- Select Pool --"), poolA.id);
    await user.click(screen.getByRole("button", { name: "Node" }));

    await user.click(submitButton());

    expect(h.submitJob).toHaveBeenCalledWith(
      expect.objectContaining({ target_pool_id: undefined, target_node_id: undefined })
    );
  });

  it("maps the High priority label to 10", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("flow_wait"));
    await user.type(jobNameInput(), "j");
    await user.selectOptions(selectWithOption("High"), "high");

    await user.click(submitButton());

    expect(h.submitJob).toHaveBeenCalledWith(expect.objectContaining({ priority: 10 }));
  });

  it("maps the Low priority label to 1", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("flow_wait"));
    await user.type(jobNameInput(), "j");
    await user.selectOptions(selectWithOption("Low"), "low");

    await user.click(submitButton());

    expect(h.submitJob).toHaveBeenCalledWith(expect.objectContaining({ priority: 1 }));
  });

  /** Step order in the payload is the canvas order, including after a reorder. */
  it("submits the steps in the (reordered) canvas order", async () => {
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.click(paletteItem("flow_wait"));
    const ids = cardIds();
    act(() => h.dnd.onDragEnd!({ active: { id: ids[1] }, over: { id: ids[0] } }));
    await user.type(jobNameInput(), "j");

    await user.click(submitButton());

    expect(h.submitJob.mock.calls[0]![0].steps.map((s: { step: string }) => s.step)).toEqual([
      "flow_wait",
      "shell_run",
    ]);
  });

  /**
   * A server rejection (this is where the OUTPUT_KEYS chain errors surface)
   * is shown verbatim and the user stays on the builder with their work intact.
   */
  it("shows the server's error message and keeps the pipeline when submission fails", async () => {
    h.submitJob.mockRejectedValue(new Error("step 2 references unknown output key 'sha'"));
    const { user } = renderBuilder();
    await user.click(paletteItem("shell_run"));
    await user.type(jobNameInput(), "nightly");

    await user.click(submitButton());

    expect(
      await screen.findByText("step 2 references unknown output key 'sha'")
    ).toBeInTheDocument();
    expect(screen.queryByTestId("job-detail-probe")).not.toBeInTheDocument();
    expect(canvasRows()).toEqual([{ position: "1", step: "shell_run" }]);
  });

  /** A non-Error rejection still produces a readable banner. */
  it("falls back to a generic message when the rejection is not an Error", async () => {
    h.submitJob.mockRejectedValue("kaboom");
    const { user } = renderBuilder();
    await user.click(paletteItem("flow_wait"));
    await user.type(jobNameInput(), "nightly");

    await user.click(submitButton());

    expect(await screen.findByText("Submission failed")).toBeInTheDocument();
  });

  /** The button is disabled and relabelled while the POST is in flight. */
  it("disables the submit button and relabels it while submitting", async () => {
    const gate = deferred<JobInfo>();
    h.submitJob.mockReturnValue(gate.promise);
    const { user } = renderBuilder();
    await user.click(paletteItem("flow_wait"));
    await user.type(jobNameInput(), "nightly");

    await user.click(submitButton());

    const inFlight = screen.getByRole("button", { name: /submitting/i });
    expect(inFlight).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^Submit Job$/ })).not.toBeInTheDocument();

    await act(async () => {
      gate.resolve(makeJob({ id: "job-99" as JobInfo["id"] }));
      await gate.promise;
    });
    expect(await screen.findByTestId("job-detail-probe")).toHaveTextContent("detail:job-99");
  });

  /** After a failure the button becomes usable again (no stuck spinner). */
  it("re-enables the submit button after a failed submission", async () => {
    h.submitJob.mockRejectedValue(new Error("nope"));
    const { user } = renderBuilder();
    await user.click(paletteItem("flow_wait"));
    await user.type(jobNameInput(), "nightly");

    await user.click(submitButton());

    await waitFor(() => expect(submitButton()).toBeEnabled());
    expect(submitButton()).toHaveTextContent("Submit Job");
  });

  /** A stale error banner is cleared at the start of the next attempt. */
  it("clears a previous error banner on the next submission attempt", async () => {
    const { user } = renderBuilder();
    await user.click(submitButton());
    expect(screen.getByText("Job name is required.")).toBeInTheDocument();

    await user.type(jobNameInput(), "nightly");
    await user.click(submitButton());

    expect(screen.queryByText("Job name is required.")).not.toBeInTheDocument();
    expect(screen.getByText("Add at least one step.")).toBeInTheDocument();
  });

});
