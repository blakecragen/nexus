/**
 * Tests for the Pools page (src/pages/Pools.tsx).
 *
 * A pool is a named group of nodes that a job can target instead of naming a
 * machine, so this page is effectively the scheduler's routing configuration UI.
 * It has four moving parts, and this file is organised around them:
 *
 *  1. The card grid      — driven by `usePoolsStore` (GET /api/pools), with a
 *                          loading frame, an empty state and a count badge.
 *  2. Create Pool        — a modal POSTing /api/pools, the one mutation on the
 *                          page that surfaces its error to the operator.
 *  3. The slide-over     — fetches membership itself (GET /api/pools/{id}) into
 *                          local state, and owns add/remove member plus the
 *                          admin-only delete.
 *  4. The sync effect    — re-resolves the selected pool against the refreshed
 *                          store array, and closes the panel when it is gone.
 *
 * What is real vs stubbed: the components, the real `cn` helper, and React's own
 * effect/refetch ordering all run for real. Two boundaries are replaced —
 * `@/stores` (pools list, node inventory, current user) and `@/api/client` — so
 * nothing here touches the network.
 *
 * AI Note: pool membership is NOT pushed over the dashboard WebSocket, so every
 * mutation must explicitly refetch BOTH the member list (`api.getPool`) and the
 * pool list (the store's `fetch`). Several tests below assert those call counts
 * rather than just the visible result, because a missing refresh shows up as a
 * stale `node_count` badge long after the test that should have caught it.
 *
 * Neighbouring pieces: the node inventory this page reads is rendered by
 * `Nodes.tsx` (Nodes.test.tsx), pool access grants are administered on
 * `Admin.tsx` (Admin.test.tsx), and the store itself is covered by
 * src/stores/index.test.ts.
 */
import { vi, describe, it, expect, beforeEach } from "vitest";
import {
  renderWithRouter,
  screen,
  within,
  waitFor,
  makeNode,
  makePool,
  makeUser,
} from "../test/test-utils";
import type { NodeInfo, PoolInfo, UserInfo } from "@/types";

// ── Mock the store + api boundaries ─────────────────────────────────────────
// The page consumes the three stores with DIFFERENT call conventions, and the
// mocks mirror that exactly: `usePoolsStore()` is a bare destructuring call,
// while `useNodesStore` and `useAuthStore` are always read through a selector
// (`(s) => s.fetch`, `(s) => s.nodes`, `(s) => s.user`).
//
// AI Note: this must live in `vi.hoisted` — `vi.mock` factories are hoisted
// above every `const`, so a factory closing over a normally-declared binding
// throws "Cannot access 'x' before initialization" at import time.
const h = vi.hoisted(() => {
  const poolsFetch = vi.fn().mockResolvedValue(undefined);
  const nodesFetch = vi.fn().mockResolvedValue(undefined);
  const poolsState: { pools: unknown[]; isLoading: boolean; fetch: typeof poolsFetch } = {
    pools: [],
    isLoading: false,
    fetch: poolsFetch,
  };
  const nodesState: { nodes: unknown[]; fetch: typeof nodesFetch } = {
    nodes: [],
    fetch: nodesFetch,
  };
  const authState: { user: unknown } = { user: null };
  const usePoolsStore = vi.fn((selector?: (s: typeof poolsState) => unknown) =>
    selector ? selector(poolsState) : poolsState
  );
  const useNodesStore = vi.fn((selector?: (s: typeof nodesState) => unknown) =>
    selector ? selector(nodesState) : nodesState
  );
  const useAuthStore = vi.fn((selector?: (s: typeof authState) => unknown) =>
    selector ? selector(authState) : authState
  );
  const api = {
    createPool: vi.fn(),
    deletePool: vi.fn(),
    getPool: vi.fn(),
    addNodeToPool: vi.fn(),
    removeNodeFromPool: vi.fn(),
  };
  return {
    poolsFetch,
    nodesFetch,
    poolsState,
    nodesState,
    authState,
    usePoolsStore,
    useNodesStore,
    useAuthStore,
    api,
  };
});

vi.mock("@/stores", () => ({
  usePoolsStore: h.usePoolsStore,
  useNodesStore: h.useNodesStore,
  useAuthStore: h.useAuthStore,
}));
vi.mock("@/api/client", () => ({ api: h.api }));

import PoolsPage from "./Pools";

/** Narrowed aliases so tests can seed typed fixtures into the hoisted state. */
const poolsState = h.poolsState as {
  pools: PoolInfo[];
  isLoading: boolean;
  fetch: typeof h.poolsFetch;
};
const nodesState = h.nodesState as { nodes: NodeInfo[]; fetch: typeof h.nodesFetch };
const authState = h.authState as { user: UserInfo | null };

// ── Query helpers ───────────────────────────────────────────────────────────

/**
 * The Create Pool modal's overlay.
 *
 * Needed because the modal's submit button and the page header button share the
 * accessible name "Create Pool" — an unscoped `getByRole` finds both and throws.
 */
const createDialog = () =>
  screen
    .getByRole("heading", { name: "Create Pool" })
    .closest("[class*='bg-black/40']") as HTMLElement;

/** The Create Pool modal's submit button (scoped away from the header button). */
const createSubmit = () =>
  within(createDialog()).getByRole("button", { name: "Create Pool" });

/** The slide-over's `<h2>` for a pool (the card renders the same name as an `<h3>`). */
const panelHeading = (name: string) =>
  screen.getByRole("heading", { name, level: 2 });

/** The `<dd>` next to a `<dt>` in the slide-over's Details list. */
const detailValue = (label: string) =>
  screen.getByText(label).nextElementSibling as HTMLElement;

/** Open the slide-over for a pool by clicking its card. */
async function openPool(user: ReturnType<typeof renderWithRouter>["user"], name: string) {
  await user.click(screen.getByRole("button", { name: new RegExp(name) }));
  await waitFor(() => expect(panelHeading(name)).toBeInTheDocument());
}

beforeEach(() => {
  poolsState.pools = [];
  poolsState.isLoading = false;
  poolsState.fetch = h.poolsFetch;
  nodesState.nodes = [];
  nodesState.fetch = h.nodesFetch;
  // Default to a non-admin so admin-gated UI must be opted into explicitly by
  // any test that expects it — a missing gate then shows up as a failure.
  authState.user = makeUser({ role: "user" });

  h.poolsFetch.mockResolvedValue(undefined);
  h.nodesFetch.mockResolvedValue(undefined);
  // Re-declare every api implementation per test so a test that overrides one
  // (e.g. to reject) cannot leak that into the next.
  h.api.createPool.mockResolvedValue(makePool());
  h.api.deletePool.mockResolvedValue(undefined);
  h.api.getPool.mockResolvedValue({ pool: makePool(), nodes: [] });
  h.api.addNodeToPool.mockResolvedValue(undefined);
  h.api.removeNodeFromPool.mockResolvedValue(undefined);

  // The page's failure handling is console-only in several places; silence it so
  // an expected failure does not look like a broken test run.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

// ── Card grid ───────────────────────────────────────────────────────────────

/** The pool list: mount fetches, loading/empty/populated frames, card content. */
describe("Pools page — card grid", () => {
  /** The page loads BOTH pools and the node inventory, the latter for the member picker. */
  it("fetches pools and the node inventory once on mount", () => {
    renderWithRouter(<PoolsPage />);

    expect(h.poolsFetch).toHaveBeenCalledTimes(1);
    expect(h.nodesFetch).toHaveBeenCalledTimes(1);
  });

  /**
   * Loading suppresses both the grid and the empty state. Regression guarded:
   * flashing "No pools created yet" on every page load, which reads as a wiped
   * scheduler configuration.
   */
  it("renders a spinner instead of cards or the empty state while loading", () => {
    poolsState.isLoading = true;
    poolsState.pools = [];
    renderWithRouter(<PoolsPage />);

    expect(screen.queryByText(/no pools created yet/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /create your first pool/i })).not.toBeInTheDocument();
  });

  /**
   * Documents a real gap: the page has NO error state. The pools store leaves
   * `isLoading` true when its request rejects (it does not catch), so a failed
   * GET /api/pools renders an indefinite spinner with nothing explaining why.
   *
   * POSSIBLE BUG (reported, not fixed): a server outage is visually identical to
   * a slow load, forever. Pinned as current behaviour — if an error banner is
   * added, this test fails and should be replaced with assertions on it.
   */
  it("renders no error message when the pool list never loads (known gap)", () => {
    poolsState.isLoading = true;
    renderWithRouter(<PoolsPage />);

    expect(screen.queryByText(/error|failed|retry/i)).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Pools" })).toBeInTheDocument();
  });

  /** Zero pools shows an explicit empty state plus a first-run shortcut. */
  it("shows the empty state and a first-pool shortcut when there are no pools", () => {
    poolsState.pools = [];
    renderWithRouter(<PoolsPage />);

    expect(screen.getByText(/no pools created yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create your first pool/i })).toBeInTheDocument();
  });

  /** One card per pool; the explicit count catches duplicate rendering. */
  it("renders one card per pool with its name", () => {
    poolsState.pools = [makePool({ name: "gpu-cluster" }), makePool({ name: "build-farm" })];
    renderWithRouter(<PoolsPage />);

    expect(screen.getByRole("heading", { name: "gpu-cluster", level: 3 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "build-farm", level: 3 })).toBeInTheDocument();
    expect(screen.queryByText(/no pools created yet/i)).not.toBeInTheDocument();
  });

  /** The header badge reports the number of pools (not nodes). */
  it("shows the pool count badge in the header", () => {
    poolsState.pools = [makePool({ name: "a" }), makePool({ name: "b" }), makePool({ name: "c" })];
    renderWithRouter(<PoolsPage />);

    const header = screen.getByRole("heading", { name: "Pools" }).parentElement!;
    expect(within(header).getByText("3")).toBeInTheDocument();
  });

  /**
   * The member pill singularises correctly. Regression guarded: "1 nodes",
   * which is the kind of detail that makes an ops console look untrustworthy.
   */
  it("singularises the member pill for a one-node pool and pluralises otherwise", () => {
    poolsState.pools = [
      makePool({ name: "solo", node_count: 1 }),
      makePool({ name: "empty", node_count: 0 }),
      makePool({ name: "big", node_count: 7 }),
    ];
    renderWithRouter(<PoolsPage />);

    expect(screen.getByText("1 node")).toBeInTheDocument();
    expect(screen.getByText("0 nodes")).toBeInTheDocument();
    expect(screen.getByText("7 nodes")).toBeInTheDocument();
  });

  /** A description is shown when present and the block is omitted when null. */
  it("renders the description only when the pool has one", () => {
    poolsState.pools = [
      makePool({ name: "described", description: "All the GPU boxes" }),
      makePool({ name: "bare", description: null }),
    ];
    renderWithRouter(<PoolsPage />);

    expect(screen.getByText("All the GPU boxes")).toBeInTheDocument();
    // The bare card's accessible name is just the name + count + created line.
    expect(screen.getByRole("button", { name: /bare/ }).textContent).not.toMatch(/All the GPU/);
  });

  /** The creation date is rendered through `toLocaleDateString`, matching the runtime locale. */
  it("renders the pool's creation date on the card", () => {
    const created = "2026-03-04T10:00:00Z";
    poolsState.pools = [makePool({ name: "dated", created_at: created })];
    renderWithRouter(<PoolsPage />);

    expect(
      screen.getByText(`Created ${new Date(created).toLocaleDateString()}`)
    ).toBeInTheDocument();
  });

  /**
   * The whole card is a `<button>`, so it is keyboard-focusable and activates the
   * same way as a click. Regression guarded: a clickable `<div>` that keyboard
   * users cannot reach.
   */
  it("exposes each card as a focusable button", async () => {
    poolsState.pools = [makePool({ name: "gpu-cluster" })];
    const { user } = renderWithRouter(<PoolsPage />);

    const card = screen.getByRole("button", { name: /gpu-cluster/ });
    await user.tab();
    // The header "Create Pool" button comes first in DOM order; the card follows.
    await user.tab();
    expect(card).toHaveFocus();
  });
});

// ── Create Pool ─────────────────────────────────────────────────────────────

/** The create modal: validation, payload shape, error surfacing, dismissal. */
describe("Pools page — create pool", () => {
  /** The header button opens the modal. */
  it("opens the create modal from the header button", async () => {
    const { user } = renderWithRouter(<PoolsPage />);

    await user.click(screen.getByRole("button", { name: "Create Pool" }));

    expect(screen.getByRole("heading", { name: "Create Pool" })).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toBeInTheDocument();
    expect(screen.getByLabelText("Description")).toBeInTheDocument();
  });

  /** ...and so does the empty-state shortcut, which is the first-run path. */
  it("opens the create modal from the empty-state shortcut", async () => {
    poolsState.pools = [];
    const { user } = renderWithRouter(<PoolsPage />);

    await user.click(screen.getByRole("button", { name: /create your first pool/i }));

    expect(screen.getByRole("heading", { name: "Create Pool" })).toBeInTheDocument();
  });

  /** Submitting is impossible with no name — a pool must be nameable to be targeted. */
  it("disables the submit button while the name is empty", async () => {
    const { user } = renderWithRouter(<PoolsPage />);

    await user.click(screen.getByRole("button", { name: "Create Pool" }));

    expect(createSubmit()).toBeDisabled();
  });

  /** Whitespace is not a name; the guard trims before deciding. */
  it("keeps the submit button disabled for a whitespace-only name", async () => {
    const { user } = renderWithRouter(<PoolsPage />);

    await user.click(screen.getByRole("button", { name: "Create Pool" }));
    await user.type(screen.getByLabelText("Name"), "   ");

    expect(createSubmit()).toBeDisabled();
  });

  /** The happy path: POST the trimmed values, refresh the grid, close the modal. */
  it("creates the pool with trimmed values, refreshes the list and closes", async () => {
    const { user } = renderWithRouter(<PoolsPage />);
    expect(h.poolsFetch).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Create Pool" }));
    await user.type(screen.getByLabelText("Name"), "  gpu-cluster  ");
    await user.type(screen.getByLabelText("Description"), "  All the GPU boxes  ");
    await user.click(createSubmit());

    await waitFor(() =>
      expect(h.api.createPool).toHaveBeenCalledWith({
        name: "gpu-cluster",
        description: "All the GPU boxes",
      })
    );
    await waitFor(() => expect(h.poolsFetch).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("heading", { name: "Create Pool" })).not.toBeInTheDocument();
  });

  /**
   * A blank description is sent as `undefined` (field omitted) so the server
   * stores NULL — which is what makes the card's "render only if truthy" check
   * behave.
   */
  it("omits the description entirely when it is left blank", async () => {
    const { user } = renderWithRouter(<PoolsPage />);

    await user.click(screen.getByRole("button", { name: "Create Pool" }));
    await user.type(screen.getByLabelText("Name"), "bare");
    await user.click(createSubmit());

    await waitFor(() => expect(h.api.createPool).toHaveBeenCalled());
    expect(h.api.createPool.mock.calls[0][0].description).toBeUndefined();
  });

  /**
   * A duplicate pool name is a normal, recoverable 4xx the operator must see.
   * This is the only mutation on the page that surfaces its error rather than
   * only console.error-ing.
   */
  it("shows the server error inline and keeps the modal open on failure", async () => {
    h.api.createPool.mockRejectedValue(new Error("Pool name already exists"));
    const { user } = renderWithRouter(<PoolsPage />);

    await user.click(screen.getByRole("button", { name: "Create Pool" }));
    await user.type(screen.getByLabelText("Name"), "gpu-cluster");
    await user.click(createSubmit());

    expect(await screen.findByText("Pool name already exists")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Create Pool" })).toBeInTheDocument();
    // The list is not refreshed on failure.
    expect(h.poolsFetch).toHaveBeenCalledTimes(1);
  });

  /** A rejected create leaves the operator's input intact for a retry. */
  it("preserves the typed name after a failed create", async () => {
    h.api.createPool.mockRejectedValue(new Error("nope"));
    const { user } = renderWithRouter(<PoolsPage />);

    await user.click(screen.getByRole("button", { name: "Create Pool" }));
    await user.type(screen.getByLabelText("Name"), "gpu-cluster");
    await user.click(createSubmit());

    await waitFor(() => expect(screen.getByText("nope")).toBeInTheDocument());
    expect(screen.getByLabelText("Name")).toHaveValue("gpu-cluster");
  });

  /** Cancel dismisses without creating anything. */
  it("closes the modal without creating when Cancel is clicked", async () => {
    const { user } = renderWithRouter(<PoolsPage />);

    await user.click(screen.getByRole("button", { name: "Create Pool" }));
    await user.type(screen.getByLabelText("Name"), "temp");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("heading", { name: "Create Pool" })).not.toBeInTheDocument();
    expect(h.api.createPool).not.toHaveBeenCalled();
  });

  /**
   * Documents a deliberate inconsistency: this modal's backdrop has NO click
   * handler, unlike the Storage page's Add Backend dialog, so a stray click
   * cannot discard a half-typed form here.
   */
  it("does not close when the modal backdrop is clicked (unlike the Storage dialog)", async () => {
    const { user } = renderWithRouter(<PoolsPage />);

    await user.click(screen.getByRole("button", { name: "Create Pool" }));
    await user.type(screen.getByLabelText("Name"), "half-typed");
    await user.click(createDialog());

    expect(screen.getByRole("heading", { name: "Create Pool" })).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("half-typed");
  });
});

// ── Detail slide-over ───────────────────────────────────────────────────────

/** The membership panel: its own fetch, its metadata, and its loading/empty frames. */
describe("Pools page — detail slide-over", () => {
  /** Clicking a card opens the panel and fetches that pool's membership. */
  it("opens the slide-over and fetches the pool's membership", async () => {
    const pool = makePool({ name: "gpu-cluster" });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({ pool, nodes: [] });
    const { user } = renderWithRouter(<PoolsPage />);

    await openPool(user, "gpu-cluster");

    expect(h.api.getPool).toHaveBeenCalledWith(pool.id);
    expect(screen.getByText("Member Nodes")).toBeInTheDocument();
  });

  /** The Details list surfaces the created date and the list endpoint's node count. */
  it("shows the created date and node count in the Details list", async () => {
    const created = "2026-01-15T08:00:00Z";
    const pool = makePool({ name: "gpu-cluster", node_count: 4, created_at: created });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({ pool, nodes: [] });
    const { user } = renderWithRouter(<PoolsPage />);

    await openPool(user, "gpu-cluster");

    expect(detailValue("Created")).toHaveTextContent(new Date(created).toLocaleDateString());
    expect(detailValue("Node Count")).toHaveTextContent("4");
  });

  /** The description block is rendered only when the pool has one. */
  it("renders the Description section only for a pool that has one", async () => {
    const withDesc = makePool({ name: "described", description: "All the GPU boxes" });
    poolsState.pools = [withDesc];
    h.api.getPool.mockResolvedValue({ pool: withDesc, nodes: [] });
    const { user, unmount } = renderWithRouter(<PoolsPage />);
    await openPool(user, "described");
    expect(screen.getByText("Description")).toBeInTheDocument();
    unmount();

    const bare = makePool({ name: "bare", description: null });
    poolsState.pools = [bare];
    h.api.getPool.mockResolvedValue({ pool: bare, nodes: [] });
    const second = renderWithRouter(<PoolsPage />);
    await openPool(second.user, "bare");

    expect(screen.queryByText("Description")).not.toBeInTheDocument();
  });

  /** While membership is in flight, neither rows nor the empty state are shown. */
  it("shows a spinner instead of the membership empty state while the detail fetch is pending", async () => {
    const pool = makePool({ name: "gpu-cluster" });
    poolsState.pools = [pool];
    h.api.getPool.mockReturnValue(new Promise(() => {}));
    const { user } = renderWithRouter(<PoolsPage />);

    await openPool(user, "gpu-cluster");

    expect(screen.queryByText(/no nodes in this pool/i)).not.toBeInTheDocument();
  });

  /** An empty pool gets its own copy rather than an unexplained blank area. */
  it("shows 'No nodes in this pool' for an empty pool", async () => {
    const pool = makePool({ name: "empty" });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({ pool, nodes: [] });
    const { user } = renderWithRouter(<PoolsPage />);

    await openPool(user, "empty");

    expect(await screen.findByText(/no nodes in this pool/i)).toBeInTheDocument();
  });

  /** Member rows show the node's identity and its status/OS/arch summary. */
  it("renders a row per member node with its status, OS and arch", async () => {
    const pool = makePool({ name: "gpu-cluster", node_count: 2 });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({
      pool,
      nodes: [
        makeNode({ display_name: "Alpha", status: "online", os_type: "linux", arch: "x86_64" }),
        makeNode({ display_name: "Beta", status: "offline", os_type: "macos", arch: "arm64" }),
      ],
    });
    const { user } = renderWithRouter(<PoolsPage />);

    await openPool(user, "gpu-cluster");

    expect(await screen.findByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("online -- linux -- x86_64")).toBeInTheDocument();
    expect(screen.getByText("offline -- macos -- arm64")).toBeInTheDocument();
  });

  /** `display_name` is optional, so a nameless node must fall back to its hostname. */
  it("falls back to the hostname for a member node with no display name", async () => {
    const pool = makePool({ name: "gpu-cluster" });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({
      pool,
      nodes: [makeNode({ display_name: "", hostname: "bare-host.test" })],
    });
    const { user } = renderWithRouter(<PoolsPage />);

    await openPool(user, "gpu-cluster");

    expect(await screen.findByText("bare-host.test")).toBeInTheDocument();
  });

  /**
   * The status dot colours must differ per status. This map is a third copy of
   * the one in Nodes.tsx, so a drift between them is exactly what this pins.
   */
  it("colors the member status dot per status (online green, offline gray)", async () => {
    const pool = makePool({ name: "gpu-cluster" });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({
      pool,
      nodes: [
        makeNode({ display_name: "Alpha", status: "online" }),
        makeNode({ display_name: "Beta", status: "offline" }),
      ],
    });
    const { user } = renderWithRouter(<PoolsPage />);

    await openPool(user, "gpu-cluster");

    const online = await screen.findByText(/^online --/);
    const offline = screen.getByText(/^offline --/);
    expect(online.querySelector(".bg-green-500")).not.toBeNull();
    expect(offline.querySelector(".bg-gray-400")).not.toBeNull();
  });

  /**
   * Documents ACTUAL behaviour when membership cannot be loaded: the failure is
   * logged to the console and the panel renders as if the pool were empty.
   *
   * POSSIBLE BUG (reported, not fixed): "the request failed" and "this pool has
   * no members" look identical, so an operator can conclude a pool was emptied
   * when the request merely 500'd.
   */
  it("renders the empty membership state when the detail fetch fails (logged only)", async () => {
    const pool = makePool({ name: "gpu-cluster", node_count: 3 });
    poolsState.pools = [pool];
    h.api.getPool.mockRejectedValue(new Error("HTTP 500"));
    const { user } = renderWithRouter(<PoolsPage />);

    await openPool(user, "gpu-cluster");

    expect(await screen.findByText(/no nodes in this pool/i)).toBeInTheDocument();
    expect(console.error).toHaveBeenCalled();
    expect(screen.queryByText(/http 500/i)).not.toBeInTheDocument();
  });

  /**
   * The panel must be closable, or it covers the grid until a reload.
   *
   * AI Note: the close button is located by DOM position (the only button in the
   * panel header) because the X icon has no accessible name. Fragile by
   * construction — if the header gains another button, fix the traversal here,
   * and ideally give the button an aria-label in the page instead.
   */
  it("closes the slide-over via its close button", async () => {
    const pool = makePool({ name: "gpu-cluster" });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({ pool, nodes: [] });
    const { user } = renderWithRouter(<PoolsPage />);
    await openPool(user, "gpu-cluster");

    const heading = panelHeading("gpu-cluster");
    await user.click(within(heading.parentElement!.parentElement!).getByRole("button"));

    expect(screen.queryByText("Member Nodes")).not.toBeInTheDocument();
  });
});

// ── Add node to pool ────────────────────────────────────────────────────────

/** The searchable picker that POSTs /api/pools/{id}/nodes. */
describe("Pools page — add node to pool", () => {
  /** Seed one pool with `members` already joined and `inventory` in the node store. */
  async function setup(members: NodeInfo[], inventory: NodeInfo[]) {
    const pool = makePool({ name: "gpu-cluster", node_count: members.length });
    poolsState.pools = [pool];
    nodesState.nodes = inventory;
    h.api.getPool.mockResolvedValue({ pool, nodes: members });
    const { user } = renderWithRouter(<PoolsPage />);
    await openPool(user, "gpu-cluster");
    return { user, pool };
  }

  /** The picker is closed by default and opens with a search box. */
  it("opens a searchable picker listing the cluster inventory", async () => {
    const alpha = makeNode({ display_name: "Alpha" });
    const { user } = await setup([], [alpha]);

    expect(screen.queryByPlaceholderText("Search nodes...")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add Node" }));

    expect(screen.getByPlaceholderText("Search nodes...")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Alpha" })).toBeInTheDocument();
  });

  /**
   * Already-joined nodes are filtered out. Regression guarded: offering a node
   * that is already a member, whose "add" is a confusing no-op.
   */
  it("hides nodes that are already members of the pool", async () => {
    const alpha = makeNode({ display_name: "Alpha" });
    const beta = makeNode({ display_name: "Beta" });
    const { user } = await setup([alpha], [alpha, beta]);
    await screen.findByText("Alpha");

    await user.click(screen.getByRole("button", { name: "Add Node" }));

    expect(screen.getByRole("button", { name: "Beta" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Alpha" })).not.toBeInTheDocument();
  });

  /** When every node has joined, the picker says so instead of rendering an empty list. */
  it("shows 'No available nodes' when the whole inventory has already joined", async () => {
    const alpha = makeNode({ display_name: "Alpha" });
    const { user } = await setup([alpha], [alpha]);
    await screen.findByText("Alpha");

    await user.click(screen.getByRole("button", { name: "Add Node" }));

    expect(screen.getByText(/no available nodes/i)).toBeInTheDocument();
  });

  /**
   * The search matches hostname OR display name, even though only the display
   * name is rendered — operators often know the real hostname.
   */
  it("filters candidates by hostname even though the display name is what is shown", async () => {
    const { user } = await setup(
      [],
      [
        makeNode({ display_name: "Alpha", hostname: "beta-box.test" }),
        makeNode({ display_name: "Gamma", hostname: "gamma.test" }),
      ]
    );

    await user.click(screen.getByRole("button", { name: "Add Node" }));
    await user.type(screen.getByPlaceholderText("Search nodes..."), "beta-box");

    expect(screen.getByRole("button", { name: "Alpha" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Gamma" })).not.toBeInTheDocument();
  });

  /** Search also matches the display name, case-insensitively. */
  it("filters candidates by display name, case-insensitively", async () => {
    const { user } = await setup(
      [],
      [makeNode({ display_name: "Alpha" }), makeNode({ display_name: "Gamma" })]
    );

    await user.click(screen.getByRole("button", { name: "Add Node" }));
    await user.type(screen.getByPlaceholderText("Search nodes..."), "ALPH");

    expect(screen.getByRole("button", { name: "Alpha" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Gamma" })).not.toBeInTheDocument();
  });

  /**
   * The happy path. Both refreshes matter: `getPool` updates the member list and
   * the pools store `fetch` updates the card's `node_count` badge.
   */
  it("adds the node and refreshes both the member list and the pool list", async () => {
    const beta = makeNode({ display_name: "Beta" });
    const { user, pool } = await setup([], [beta]);
    expect(h.api.getPool).toHaveBeenCalledTimes(1);
    expect(h.poolsFetch).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Add Node" }));
    await user.click(screen.getByRole("button", { name: "Beta" }));

    await waitFor(() => expect(h.api.addNodeToPool).toHaveBeenCalledWith(pool.id, beta.id));
    await waitFor(() => expect(h.api.getPool).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(h.poolsFetch).toHaveBeenCalledTimes(2));
  });

  /**
   * The picker stays open after an add so several nodes can be added in a row.
   * Pinned because the just-added node vanishing from the list is the ONLY
   * feedback that the add worked.
   */
  it("keeps the picker open after a successful add", async () => {
    const { user } = await setup(
      [],
      [makeNode({ display_name: "Beta" }), makeNode({ display_name: "Gamma" })]
    );

    await user.click(screen.getByRole("button", { name: "Add Node" }));
    await user.click(screen.getByRole("button", { name: "Beta" }));

    await waitFor(() => expect(h.api.addNodeToPool).toHaveBeenCalled());
    expect(screen.getByPlaceholderText("Search nodes...")).toBeInTheDocument();
  });

  /**
   * Documents ACTUAL behaviour on failure: the error is console-only, so the
   * spinner clears and nothing visibly changes.
   *
   * POSSIBLE BUG (reported, not fixed): a failed add reads as "nothing
   * happened" — the operator has no way to distinguish it from a mis-click.
   */
  it("logs and silently does nothing visible when the add fails", async () => {
    h.api.addNodeToPool.mockRejectedValue(new Error("HTTP 409"));
    const { user } = await setup([], [makeNode({ display_name: "Beta" })]);

    await user.click(screen.getByRole("button", { name: "Add Node" }));
    await user.click(screen.getByRole("button", { name: "Beta" }));

    await waitFor(() => expect(console.error).toHaveBeenCalled());
    expect(screen.queryByText(/409|failed/i)).not.toBeInTheDocument();
    // The candidate is still listed, because no refresh happened.
    expect(screen.getByRole("button", { name: "Beta" })).toBeInTheDocument();
  });
});

// ── Remove node from pool ───────────────────────────────────────────────────

/** The per-member remove control (DELETE /api/pools/{id}/nodes/{nodeId}). */
describe("Pools page — remove node from pool", () => {
  async function setup(members: NodeInfo[]) {
    const pool = makePool({ name: "gpu-cluster", node_count: members.length });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({ pool, nodes: members });
    const { user } = renderWithRouter(<PoolsPage />);
    await openPool(user, "gpu-cluster");
    await screen.findByText(members[0].display_name!);
    return { user, pool };
  }

  /** Each member row has its own remove control. */
  it("renders a remove control per member row", async () => {
    await setup([
      makeNode({ display_name: "Alpha" }),
      makeNode({ display_name: "Beta" }),
    ]);

    expect(screen.getAllByRole("button", { name: "Remove from pool" })).toHaveLength(2);
  });

  /** Removal DELETEs by (poolId, nodeId) and refreshes both views. */
  it("removes the node and refreshes both the member list and the pool list", async () => {
    const alpha = makeNode({ display_name: "Alpha" });
    const { user, pool } = await setup([alpha]);

    await user.click(screen.getByRole("button", { name: "Remove from pool" }));

    await waitFor(() =>
      expect(h.api.removeNodeFromPool).toHaveBeenCalledWith(pool.id, alpha.id)
    );
    await waitFor(() => expect(h.api.getPool).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(h.poolsFetch).toHaveBeenCalledTimes(2));
  });

  /**
   * Removal is deliberately unconfirmed — it is cheap and reversible via the Add
   * Node picker, unlike pool deletion which does confirm.
   */
  it("removes a member without any confirmation prompt", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = await setup([makeNode({ display_name: "Alpha" })]);

    await user.click(screen.getByRole("button", { name: "Remove from pool" }));

    await waitFor(() => expect(h.api.removeNodeFromPool).toHaveBeenCalled());
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: /remove/i })).not.toBeInTheDocument();
  });

  /**
   * Documents ACTUAL behaviour on failure: console-only, so the member stays
   * listed and the operator gets no explanation.
   */
  it("logs and leaves the member in place when the removal fails", async () => {
    h.api.removeNodeFromPool.mockRejectedValue(new Error("HTTP 500"));
    const { user } = await setup([makeNode({ display_name: "Alpha" })]);

    await user.click(screen.getByRole("button", { name: "Remove from pool" }));

    await waitFor(() => expect(console.error).toHaveBeenCalled());
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    // No refresh on the failure path.
    expect(h.api.getPool).toHaveBeenCalledTimes(1);
  });
});

// ── Delete pool ─────────────────────────────────────────────────────────────

/**
 * Pool deletion: admin-gated and confirm-gated.
 *
 * AI Note: the role check is a UI affordance only — the server independently
 * enforces it on DELETE /api/pools/{id}. These tests should not be read as
 * proving authorization.
 */
describe("Pools page — delete pool", () => {
  async function setup(role: UserInfo["role"]) {
    authState.user = makeUser({ role });
    const pool = makePool({ name: "gpu-cluster" });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({ pool, nodes: [] });
    const { user, ...view } = renderWithRouter(<PoolsPage />);
    await openPool(user, "gpu-cluster");
    return { user, pool, ...view };
  }

  /** Admins get the destructive action inside the panel. */
  it("shows the Delete Pool action to an admin", async () => {
    await setup("admin");

    expect(screen.getByRole("button", { name: /delete pool/i })).toBeInTheDocument();
  });

  /**
   * Non-admins do not. The Member Nodes assertion is deliberate: it proves the
   * panel really opened, so the missing button is a role gate and not a failed
   * open.
   */
  it("hides the Delete Pool action from a non-admin", async () => {
    await setup("user");

    expect(screen.getByText("Member Nodes")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /delete pool/i })).not.toBeInTheDocument();
  });

  /** ...including a manager, who can create pools but not delete them. */
  it("hides the Delete Pool action from a manager", async () => {
    await setup("manager");

    expect(screen.queryByRole("button", { name: /delete pool/i })).not.toBeInTheDocument();
  });

  /** The confirmation names the target and states that member nodes survive. */
  it("opens a confirmation naming the pool and promising the nodes survive", async () => {
    const { user } = await setup("admin");

    await user.click(screen.getByRole("button", { name: /delete pool/i }));

    expect(screen.getByRole("heading", { name: "Delete Pool" })).toBeInTheDocument();
    expect(screen.getByText(/will not delete the nodes/i)).toBeInTheDocument();
    expect(h.api.deletePool).not.toHaveBeenCalled();
  });

  /** Declining the confirmation deletes nothing and keeps the panel open. */
  it("deletes nothing and keeps the slide-over open when the confirmation is cancelled", async () => {
    const { user } = await setup("admin");

    await user.click(screen.getByRole("button", { name: /delete pool/i }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("heading", { name: "Delete Pool" })).not.toBeInTheDocument();
    expect(screen.getByText("Member Nodes")).toBeInTheDocument();
    expect(h.api.deletePool).not.toHaveBeenCalled();
  });

  /** Confirming deletes, refreshes the grid, and closes the panel. */
  it("deletes the pool, refreshes the grid and closes the slide-over", async () => {
    const { user, pool } = await setup("admin");

    await user.click(screen.getByRole("button", { name: /delete pool/i }));
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(h.api.deletePool).toHaveBeenCalledWith(pool.id));
    await waitFor(() => expect(h.poolsFetch).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("Member Nodes")).not.toBeInTheDocument());
  });

  /**
   * Documents ACTUAL behaviour on failure: `handleDeletePool` catches, so neither
   * `onRefresh` nor `onClose` runs and the panel simply stays put.
   *
   * POSSIBLE BUG (reported, not fixed): the failure is console-only. The panel
   * staying open is the only hint, which is easy to read as an unresponsive
   * button.
   */
  it("keeps the slide-over open and logs when the delete fails", async () => {
    h.api.deletePool.mockRejectedValue(new Error("HTTP 409"));
    const { user } = await setup("admin");

    await user.click(screen.getByRole("button", { name: /delete pool/i }));
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(console.error).toHaveBeenCalled());
    expect(screen.getByText("Member Nodes")).toBeInTheDocument();
    expect(h.poolsFetch).toHaveBeenCalledTimes(1);
  });
});

// ── Selected-pool synchronisation ───────────────────────────────────────────

/**
 * `selectedPool` is a SNAPSHOT of a store object, so an effect re-resolves it
 * against the refreshed array after every refetch. Identical pattern to the
 * selected-node effect in Nodes.tsx.
 */
describe("Pools page — selected pool stays in sync with the store", () => {
  /** A refreshed pool object must be picked up, or the panel shows stale metadata. */
  it("re-resolves the open pool against the refreshed list", async () => {
    const pool = makePool({ name: "gpu-cluster", node_count: 0 });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({ pool, nodes: [] });
    const { user, rerender } = renderWithRouter(<PoolsPage />);
    await openPool(user, "gpu-cluster");
    expect(detailValue("Node Count")).toHaveTextContent("0");

    // A refetch replaced the store object with an updated copy (same id).
    poolsState.pools = [{ ...pool, node_count: 5 }];
    rerender(<PoolsPage />);

    await waitFor(() => expect(detailValue("Node Count")).toHaveTextContent("5"));
  });

  /**
   * A pool deleted elsewhere disappears from the refreshed list, and the effect
   * closes the panel. Regression guarded: a slide-over left open over a pool
   * that no longer exists, whose every action 404s.
   */
  it("closes the slide-over when the open pool vanishes from the refreshed list", async () => {
    const pool = makePool({ name: "gpu-cluster" });
    poolsState.pools = [pool];
    h.api.getPool.mockResolvedValue({ pool, nodes: [] });
    const { user, rerender } = renderWithRouter(<PoolsPage />);
    await openPool(user, "gpu-cluster");

    poolsState.pools = [];
    rerender(<PoolsPage />);

    await waitFor(() => expect(screen.queryByText("Member Nodes")).not.toBeInTheDocument());
    expect(screen.getByText(/no pools created yet/i)).toBeInTheDocument();
  });
});
