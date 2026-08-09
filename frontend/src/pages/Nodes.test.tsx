/**
 * Tests for the Nodes page (src/pages/Nodes.tsx).
 *
 * The page renders a table of nodes from useNodesStore. Each row shows the
 * node's hostname (or display name), an OS icon, a StatusBadge (colored dot +
 * label), arch, CPU, RAM (formatted by formatRam), IP and last-heartbeat. The
 * high-value targets are the StatusBadge / formatRam helpers and the list
 * rendering, so we mock the two store boundaries (nodes + auth) and the api
 * client, then assert real rendered output with resilient role/text selectors.
 *
 * Interactive admin panels (provision/reconnect/delete dialogs that POST to the
 * api) are largely out of scope; we cover the stable open/close of the detail
 * slide-over and the Add-Node dialog, and otherwise leave the network-heavy
 * SSH flows untested (see notes).
 *
 * Neighbouring pieces: the real provisioning path this page triggers is
 * api.provisionNode -> POST /api/nodes/provision, which the server implements
 * by running the same SSH install flow as nexus_deploy.py. Those long-running
 * flows are exercised manually / by backend tests, not here.
 */
import { vi, describe, it, expect, beforeEach } from "vitest";
import { renderWithRouter, screen, within, makeNode } from "../test/test-utils";
import type { NodeInfo, UserInfo } from "@/types";

// ── Mock the store boundaries ────────────────────────────────────────────────
// useNodesStore is used as an object-destructure hook: { nodes, isLoading, fetch }.
// useAuthStore is used with a selector: useAuthStore((s) => s.user).
// Mutable state lives in vi.hoisted so the (hoisted) vi.mock factory can close
// over it without the "cannot access before initialization" error.

/**
 * Hoisted mock state + fake store hooks.
 *
 * AI Note: this must live inside `vi.hoisted`. `vi.mock` factories are hoisted
 * above all `const` declarations, so a factory referencing a normally-declared
 * const throws "Cannot access 'x' before initialization" at import time. The
 * two hooks intentionally use *different* call conventions because the page
 * consumes them differently: nodes is destructured from a bare call, auth is
 * read through a selector.
 */
const h = vi.hoisted(() => {
  const fetchMock = vi.fn().mockResolvedValue(undefined);
  const nodesState: { nodes: unknown[]; isLoading: boolean; fetch: typeof fetchMock } = {
    nodes: [],
    isLoading: false,
    fetch: fetchMock,
  };
  const authState: { user: unknown } = { user: null };
  const useNodesStore = vi.fn(() => nodesState);
  const useAuthStore = vi.fn((selector?: (s: typeof authState) => unknown) =>
    selector ? selector(authState) : authState
  );
  return { fetchMock, nodesState, authState, useNodesStore, useAuthStore };
});

/** Narrowed aliases so tests can seed typed fixtures into the hoisted state. */
const { fetchMock, useNodesStore, useAuthStore } = h;
const nodesState = h.nodesState as { nodes: NodeInfo[]; isLoading: boolean; fetch: typeof fetchMock };
const authState = h.authState as { user: UserInfo | null };

vi.mock("@/stores", () => ({
  useNodesStore: h.useNodesStore,
  useAuthStore: h.useAuthStore,
}));

// api is imported for provision/reconnect/delete/maintenance; stub the whole
// surface so nothing escapes to the network and dialogs can be exercised.
//
// AI Note: every method is stubbed even though most tests never trigger them.
// The page imports `api` at module scope, and an incomplete stub would make an
// accidental call fail with "not a function" instead of being harmlessly
// recorded — and, worse, a real call here would attempt SSH provisioning.
vi.mock("@/api/client", () => ({
  api: {
    provisionNode: vi.fn().mockResolvedValue({}),
    createNode: vi.fn().mockResolvedValue({}),
    reconnectNode: vi.fn().mockResolvedValue({ online: true, log: [] }),
    deleteNode: vi.fn().mockResolvedValue(undefined),
    setMaintenance: vi.fn().mockResolvedValue(undefined),
  },
}));

import NodesPage from "./Nodes";

beforeEach(() => {
  fetchMock.mockClear();
  useNodesStore.mockClear();
  useAuthStore.mockClear();
  nodesState.nodes = [];
  nodesState.isLoading = false;
  nodesState.fetch = fetchMock;
  // Default to logged-out so admin-gated UI must be opted into explicitly by
  // any test that expects it — a missing gate then shows up as a failure.
  authState.user = null;
});

/** Table rendering, mount fetch, loading/empty states and the count badge. */
describe("Nodes page — list rendering", () => {
  /** One row per node plus the header; the explicit count catches duplicates. */
  it("renders a row per node showing its display name", () => {
    nodesState.nodes = [
      makeNode({ display_name: "Build Box", hostname: "build.test" }),
      makeNode({ display_name: "Sim Box", hostname: "sim.test" }),
    ];

    renderWithRouter(<NodesPage />);

    expect(screen.getByText("Build Box")).toBeInTheDocument();
    expect(screen.getByText("Sim Box")).toBeInTheDocument();
    // Two data rows in addition to the header row.
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });

  /**
   * `display_name` is optional (nexus_deploy.py defaults it to the host, but
   * API-registered nodes can leave it blank), so an empty value must fall back
   * to the hostname. Regression guarded: a nameless, unclickable blank row.
   */
  it("falls back to the hostname when there is no display name", () => {
    nodesState.nodes = [makeNode({ display_name: "", hostname: "bare-host.test" })];
    renderWithRouter(<NodesPage />);
    expect(screen.getByText("bare-host.test")).toBeInTheDocument();
  });

  /** Mount must populate the store; without it the page renders permanently empty. */
  it("triggers an initial fetch on mount", () => {
    renderWithRouter(<NodesPage />);
    expect(fetchMock).toHaveBeenCalled();
  });

  /** Zero nodes shows an explicit empty state rather than a bare table. */
  it("shows the empty state when there are no nodes", () => {
    nodesState.nodes = [];
    renderWithRouter(<NodesPage />);
    expect(screen.getByText(/no nodes found/i)).toBeInTheDocument();
  });

  /**
   * Loading suppresses both the table and the empty state. Regression guarded:
   * flashing "No nodes found" on every page load, which reads as a cluster
   * outage.
   */
  it("renders a loading spinner instead of rows while loading", () => {
    nodesState.isLoading = true;
    nodesState.nodes = [];
    renderWithRouter(<NodesPage />);
    // No empty-state text while loading, and no table at all.
    expect(screen.queryByText(/no nodes found/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  /** The header badge reflects the total when no filter is applied. */
  it("shows the node count badge reflecting the total list size", () => {
    nodesState.nodes = [
      makeNode({ display_name: "A" }),
      makeNode({ display_name: "B" }),
      makeNode({ display_name: "C" }),
    ];
    renderWithRouter(<NodesPage />);
    // The badge lives in the header next to the "Nodes" title.
    const heading = screen.getByRole("heading", { name: "Nodes" });
    const headerRow = heading.parentElement!;
    expect(within(headerRow).getByText("3")).toBeInTheDocument();
  });

  /**
   * The badge counts *visible* (filtered) nodes, not the raw list. Regression
   * guarded: a badge showing 3 above a 2-row table, which reads as "a node is
   * missing" rather than "a filter is active".
   */
  it("updates the count badge to reflect the filtered list size", async () => {
    nodesState.nodes = [
      makeNode({ display_name: "Linux Box", os_type: "linux" }),
      makeNode({ display_name: "Mac Box", os_type: "macos" }),
      makeNode({ display_name: "Other Mac", os_type: "macos" }),
    ];
    const { user } = renderWithRouter(<NodesPage />);

    const heading = screen.getByRole("heading", { name: "Nodes" });
    const headerRow = heading.parentElement!;
    expect(within(headerRow).getByText("3")).toBeInTheDocument();

    // Narrow to macOS -> 2 of the 3 nodes remain, badge must follow.
    await user.click(screen.getByRole("button", { name: /all os/i }));
    await user.click(screen.getByRole("button", { name: "macOS" }));

    expect(within(headerRow).getByText("2")).toBeInTheDocument();
    expect(within(headerRow).queryByText("3")).not.toBeInTheDocument();
  });

  /** First-run onboarding CTA: admin + genuinely zero nodes. */
  it("offers admins a 'register your first node' CTA only when truly empty", () => {
    authState.user = { id: "u1", username: "root", email: "r@x.io", role: "admin", is_active: true };
    nodesState.nodes = [];
    renderWithRouter(<NodesPage />);
    // Empty + admin + zero nodes -> the first-run CTA is shown.
    expect(screen.getByRole("button", { name: /register your first node/i })).toBeInTheDocument();
  });

  /**
   * Authorization gate: non-admins never see the registration CTA. Regression
   * guarded: offering a privileged action that would 403, and advertising an
   * admin capability to ordinary users.
   */
  it("hides the 'register your first node' CTA for non-admins", () => {
    authState.user = { id: "u1", username: "bob", email: "b@x.io", role: "user", is_active: true };
    nodesState.nodes = [];
    renderWithRouter(<NodesPage />);
    expect(screen.getByText(/no nodes found/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /register your first node/i })
    ).not.toBeInTheDocument();
  });

  /**
   * "Empty because filtered" must not be confused with "empty because new".
   *
   * AI Note: the CTA is gated on `nodes.length === 0` (the unfiltered list),
   * not on the filtered rows — this test is what pins that distinction. Without
   * it, an admin who filters to Windows on a Linux-only cluster would be
   * prompted to register their "first" node.
   */
  it("shows the empty state when a filter excludes every node (non-empty list)", async () => {
    // The list is non-empty but the active filter leaves zero rows: empty state
    // appears WITHOUT the admin first-run CTA (which is gated on nodes.length===0).
    authState.user = { id: "u1", username: "root", email: "r@x.io", role: "admin", is_active: true };
    nodesState.nodes = [makeNode({ display_name: "Only Linux", os_type: "linux" })];
    const { user } = renderWithRouter(<NodesPage />);

    await user.click(screen.getByRole("button", { name: /all os/i }));
    await user.click(screen.getByRole("button", { name: "Windows" }));

    expect(screen.getByText(/no nodes found/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /register your first node/i })
    ).not.toBeInTheDocument();
  });
});

/**
 * StatusBadge: the coloured dot + label that conveys node health at a glance.
 *
 * Colour assertions match on hue substrings (/green/, /gray/) rather than exact
 * Tailwind classes so shade tweaks don't break the suite while genuine
 * mis-mappings still do.
 */
describe("Nodes page — StatusBadge helper", () => {
  /** Each row shows its own status label. */
  it("renders the status label per row", () => {
    nodesState.nodes = [
      makeNode({ display_name: "On", status: "online" }),
      makeNode({ display_name: "Off", status: "offline" }),
    ];

    renderWithRouter(<NodesPage />);

    const onRow = screen.getByText("On").closest("tr")!;
    const offRow = screen.getByText("Off").closest("tr")!;
    expect(within(onRow).getByText("online")).toBeInTheDocument();
    expect(within(offRow).getByText("offline")).toBeInTheDocument();
  });

  /**
   * The primary health distinction must be visually different. The final
   * `not.toEqual` catches a mapping collapse where both statuses resolve to the
   * same class string despite the hue assertions passing.
   */
  it("colors online and offline differently (online green, offline gray)", () => {
    nodesState.nodes = [
      makeNode({ display_name: "On", status: "online" }),
      makeNode({ display_name: "Off", status: "offline" }),
    ];

    renderWithRouter(<NodesPage />);

    const onlineLabel = screen.getByText("online");
    const offlineLabel = screen.getByText("offline");
    // STATUS_TEXT_COLORS maps online -> green, offline -> gray.
    expect(onlineLabel.className).toMatch(/green/);
    expect(offlineLabel.className).toMatch(/gray/);
    expect(onlineLabel.className).not.toEqual(offlineLabel.className);
  });

  /**
   * The two intermediate states have distinct hues too. Regression guarded: a
   * node under maintenance looking identical to a healthy busy node, which
   * would mislead an operator deciding where to schedule work.
   */
  it("colors busy and maintenance with their own schemes", () => {
    nodesState.nodes = [
      makeNode({ display_name: "Busy", status: "busy" }),
      makeNode({ display_name: "Maint", status: "maintenance" }),
    ];

    renderWithRouter(<NodesPage />);

    expect(screen.getByText("busy").className).toMatch(/yellow/);
    expect(screen.getByText("maintenance").className).toMatch(/orange/);
  });

  /**
   * The dot is a separate element from the label (different colour map:
   * STATUS_COLORS vs STATUS_TEXT_COLORS), so its presence is asserted
   * independently via a class query.
   */
  it("renders a colored status dot alongside the label", () => {
    nodesState.nodes = [makeNode({ display_name: "On", status: "online" })];
    renderWithRouter(<NodesPage />);
    const row = screen.getByText("On").closest("tr")!;
    // The badge dot uses bg-green-500 for online (STATUS_COLORS).
    expect(row.querySelector(".bg-green-500")).not.toBeNull();
  });
});

/**
 * formatRam: converts the agent-reported `ram_mb` integer into a display
 * string. Exercised through the rendered row because the helper is private to
 * the page module.
 */
describe("Nodes page — formatRam helper", () => {
  /** Multi-gigabyte values render as whole GB. */
  it("formats >= 1024 MB as whole GB", () => {
    nodesState.nodes = [makeNode({ display_name: "Big", ram_mb: 16384 })];
    renderWithRouter(<NodesPage />);
    const row = screen.getByText("Big").closest("tr")!;
    expect(within(row).getByText("16 GB")).toBeInTheDocument();
  });

  /**
   * Characterisation of a deliberately lossy choice: GB values are rendered
   * with `toFixed(0)`, so 1.5 GB displays as "2 GB".
   *
   * AI Note: this looks wrong but is intended — node RAM is effectively always
   * a power-of-two GB figure, and the whole-number form keeps the column
   * narrow. Changing to one decimal will fail here; update the expectation
   * deliberately rather than assuming a bug.
   */
  it("rounds GB to a whole number (no decimals)", () => {
    // 1536 MB = 1.5 GB -> toFixed(0) rounds to "2 GB".
    nodesState.nodes = [makeNode({ display_name: "Mid", ram_mb: 1536 })];
    renderWithRouter(<NodesPage />);
    const row = screen.getByText("Mid").closest("tr")!;
    expect(within(row).getByText("2 GB")).toBeInTheDocument();
  });

  /** Below the boundary the raw MB value is shown. */
  it("formats sub-1024 MB values in MB", () => {
    nodesState.nodes = [makeNode({ display_name: "Small", ram_mb: 512 })];
    renderWithRouter(<NodesPage />);
    const row = screen.getByText("Small").closest("tr")!;
    expect(within(row).getByText("512 MB")).toBeInTheDocument();
  });

  /** Exactly 1024 belongs to the GB branch (`>=`, not `>`). */
  it("treats exactly 1024 MB as 1 GB (boundary)", () => {
    nodesState.nodes = [makeNode({ display_name: "Edge", ram_mb: 1024 })];
    renderWithRouter(<NodesPage />);
    const row = screen.getByText("Edge").closest("tr")!;
    expect(within(row).getByText("1 GB")).toBeInTheDocument();
  });

  /**
   * Zero is a real value: nexus_deploy.py registers placeholder nodes before
   * the agent reports its true specs. The row must render "0 MB" rather than
   * crashing or printing NaN.
   */
  it("renders 0 MB (sub-1024 path) rather than crashing", () => {
    nodesState.nodes = [makeNode({ display_name: "Zero", ram_mb: 0 })];
    renderWithRouter(<NodesPage />);
    const row = screen.getByText("Zero").closest("tr")!;
    expect(within(row).getByText("0 MB")).toBeInTheDocument();
  });
});

/** Remaining per-row columns: hardware specs, heartbeat and OS labelling. */
describe("Nodes page — row content", () => {
  /**
   * Hardware columns are scoped to the owning row, so a regression that renders
   * one node's specs against another node would fail here.
   */
  it("renders arch, CPU (with core count) and IP for a node", () => {
    nodesState.nodes = [
      makeNode({
        display_name: "Spec Box",
        arch: "arm64",
        cpu_model: "Apple M2",
        cpu_cores: 10,
        ip_address: "192.168.5.5",
      }),
    ];

    renderWithRouter(<NodesPage />);
    const row = screen.getByText("Spec Box").closest("tr")!;
    expect(within(row).getByText("arm64")).toBeInTheDocument();
    expect(within(row).getByText("Apple M2")).toBeInTheDocument();
    expect(within(row).getByText("(10c)")).toBeInTheDocument();
    expect(within(row).getByText("192.168.5.5")).toBeInTheDocument();
  });

  /**
   * A node that has never checked in has `last_heartbeat: null` and must show
   * "--". Regression guarded: passing null through the relative-time formatter
   * and rendering "Invalid Date" or a 1970 timestamp.
   */
  it("shows a placeholder when there is no last heartbeat", () => {
    nodesState.nodes = [makeNode({ display_name: "Cold", last_heartbeat: null })];
    renderWithRouter(<NodesPage />);
    const row = screen.getByText("Cold").closest("tr")!;
    expect(within(row).getByText("--")).toBeInTheDocument();
  });

  /**
   * Raw `os_type` values are mapped to display labels ("macos" -> "macOS").
   * Regression guarded: a missing map entry leaking the internal enum value
   * into the UI.
   */
  it("renders an OS label in the row for each os_type", () => {
    nodesState.nodes = [
      makeNode({ display_name: "Lin", os_type: "linux" }),
      makeNode({ display_name: "Mac", os_type: "macos" }),
      makeNode({ display_name: "Win", os_type: "windows" }),
    ];
    renderWithRouter(<NodesPage />);
    expect(within(screen.getByText("Lin").closest("tr")!).getByText("Linux")).toBeInTheDocument();
    expect(within(screen.getByText("Mac").closest("tr")!).getByText("macOS")).toBeInTheDocument();
    expect(within(screen.getByText("Win").closest("tr")!).getByText("Windows")).toBeInTheDocument();
  });
});

/**
 * Client-side filtering via the OS and Status dropdowns.
 *
 * AI Note: unlike the Jobs page (which re-fetches with a `status` param), the
 * Nodes filters operate purely on the already-loaded list — hence no `fetch`
 * assertions here. Cluster node counts are small enough that this is fine, but
 * it does mean the filters can only see what the last fetch returned.
 */
describe("Nodes page — filters", () => {
  /** Selecting an OS hides non-matching rows and keeps matching ones. */
  it("filters the list by OS via the OS dropdown", async () => {
    nodesState.nodes = [
      makeNode({ display_name: "Linux Box", os_type: "linux" }),
      makeNode({ display_name: "Mac Box", os_type: "macos" }),
    ];

    const { user } = renderWithRouter(<NodesPage />);
    // Both visible initially.
    expect(screen.getByText("Linux Box")).toBeInTheDocument();
    expect(screen.getByText("Mac Box")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /all os/i }));
    await user.click(screen.getByRole("button", { name: "macOS" }));

    expect(screen.queryByText("Linux Box")).not.toBeInTheDocument();
    expect(screen.getByText("Mac Box")).toBeInTheDocument();
  });

  /** The status dropdown is an independent filter over the same list. */
  it("filters the list by status via the Status dropdown", async () => {
    nodesState.nodes = [
      makeNode({ display_name: "Up", status: "online" }),
      makeNode({ display_name: "Down", status: "offline" }),
    ];

    const { user } = renderWithRouter(<NodesPage />);

    await user.click(screen.getByRole("button", { name: /all status/i }));
    await user.click(screen.getByRole("button", { name: "Offline" }));

    expect(screen.queryByText("Up")).not.toBeInTheDocument();
    expect(screen.getByText("Down")).toBeInTheDocument();
  });
});

/**
 * Role-gated controls.
 *
 * AI Note: these assertions are UI-affordance checks only. The server
 * independently enforces the admin role on /api/nodes writes; hiding a button
 * is never the security boundary, and these tests should not be read as
 * proving authorization.
 */
describe("Nodes page — admin controls", () => {
  /** Non-admins get no Add Node entry point. */
  it("hides the Add Node button for non-admin users", () => {
    authState.user = { id: "u1", username: "bob", email: "b@x.io", role: "user", is_active: true };
    nodesState.nodes = [makeNode({ display_name: "N" })];
    renderWithRouter(<NodesPage />);
    expect(screen.queryByRole("button", { name: /add node/i })).not.toBeInTheDocument();
  });

  /** Admins do — the positive half of the gate, so it can't be hidden for everyone. */
  it("shows the Add Node button for admins", () => {
    authState.user = { id: "u1", username: "root", email: "r@x.io", role: "admin", is_active: true };
    nodesState.nodes = [makeNode({ display_name: "N" })];
    renderWithRouter(<NodesPage />);
    expect(screen.getByRole("button", { name: /add node/i })).toBeInTheDocument();
  });

  /**
   * The Add Node dialog opens and is dismissible without submitting.
   * Regression guarded: a modal that cannot be closed, blocking the page until
   * a reload. The provisioning submit itself is deliberately not exercised (it
   * drives the SSH install flow server-side).
   */
  it("opens and closes the Add Node dialog (admin)", async () => {
    authState.user = { id: "u1", username: "root", email: "r@x.io", role: "admin", is_active: true };
    const { user } = renderWithRouter(<NodesPage />);

    await user.click(screen.getByRole("button", { name: /add node/i }));
    // Dialog heading appears.
    expect(screen.getByRole("heading", { name: /add node/i })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.queryByRole("heading", { name: /add node/i })).not.toBeInTheDocument();
  });
});

/** The row-click slide-over panel: node details plus per-node actions. */
describe("Nodes page — detail slide-over", () => {
  /**
   * Clicking a row opens the panel. "Specifications" is used as the marker
   * because it appears only inside the panel, never in the table.
   */
  it("opens the detail panel when a row is clicked", async () => {
    const node = makeNode({
      display_name: "Detail Box",
      ram_mb: 8192,
      cpu_model: "Xeon",
      cpu_cores: 4,
    });
    nodesState.nodes = [node];

    const { user } = renderWithRouter(<NodesPage />);
    await user.click(screen.getByText("Detail Box").closest("tr")!);

    // The slide-over surfaces a Specifications section unique to the panel.
    expect(screen.getByText(/specifications/i)).toBeInTheDocument();
    // RAM is formatted by formatRam inside the panel as well (also in the row),
    // so there are at least two matches.
    expect(screen.getAllByText("8 GB").length).toBeGreaterThanOrEqual(2);
  });

  /** An online node offers "Enable Maintenance" (drain it from scheduling). */
  it("offers a maintenance toggle in the detail panel", async () => {
    const node = makeNode({ display_name: "Maint Box", status: "online" });
    nodesState.nodes = [node];

    const { user } = renderWithRouter(<NodesPage />);
    await user.click(screen.getByText("Maint Box").closest("tr")!);

    expect(screen.getByRole("button", { name: /enable maintenance/i })).toBeInTheDocument();
  });

  /**
   * The toggle label must reflect current state, and the opposite label must be
   * absent. Regression guarded: a stuck "Enable" label on an already-drained
   * node, so the operator cannot tell whether clicking will drain or restore it.
   */
  it("flips the maintenance toggle label when the node is already in maintenance", async () => {
    const node = makeNode({ display_name: "Already Maint", status: "maintenance" });
    nodesState.nodes = [node];

    const { user } = renderWithRouter(<NodesPage />);
    await user.click(screen.getByText("Already Maint").closest("tr")!);

    expect(screen.getByRole("button", { name: /disable maintenance/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /enable maintenance/i })).not.toBeInTheDocument();
  });

  /** Admins see the destructive Delete Node action inside the panel. */
  it("shows admin-only Delete Node action in the panel for admins", async () => {
    authState.user = { id: "u1", username: "root", email: "r@x.io", role: "admin", is_active: true };
    nodesState.nodes = [makeNode({ display_name: "Adm Box", status: "online" })];

    const { user } = renderWithRouter(<NodesPage />);
    await user.click(screen.getByText("Adm Box").closest("tr")!);

    expect(screen.getByRole("button", { name: /delete node/i })).toBeInTheDocument();
  });

  /**
   * Non-admins get the panel but not the destructive action. The maintenance
   * assertion is deliberate: it proves the panel really rendered, so the
   * missing Delete button is a role gate and not just a failed open.
   */
  it("hides the Delete Node action in the panel for non-admins", async () => {
    authState.user = { id: "u1", username: "bob", email: "b@x.io", role: "user", is_active: true };
    nodesState.nodes = [makeNode({ display_name: "User Box", status: "online" })];

    const { user } = renderWithRouter(<NodesPage />);
    await user.click(screen.getByText("User Box").closest("tr")!);

    // The panel still opens (maintenance toggle present) but no destructive action.
    expect(screen.getByRole("button", { name: /enable maintenance/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /delete node/i })).not.toBeInTheDocument();
  });

  /**
   * "Bring Online" triggers the SSH reconnect flow, so it is gated on both
   * admin role and the node actually being offline.
   */
  it("offers 'Bring Online' for an offline node only to admins", async () => {
    authState.user = { id: "u1", username: "root", email: "r@x.io", role: "admin", is_active: true };
    nodesState.nodes = [makeNode({ display_name: "Down Box", status: "offline" })];

    const { user } = renderWithRouter(<NodesPage />);
    await user.click(screen.getByText("Down Box").closest("tr")!);

    expect(screen.getByRole("button", { name: /bring online/i })).toBeInTheDocument();
  });

  /**
   * The status half of that gate: an already-online node must not offer it.
   * Regression guarded: re-running the agent install against a healthy node,
   * which restarts its agent and interrupts running work.
   */
  it("does not offer 'Bring Online' for an online node", async () => {
    authState.user = { id: "u1", username: "root", email: "r@x.io", role: "admin", is_active: true };
    nodesState.nodes = [makeNode({ display_name: "Up Box", status: "online" })];

    const { user } = renderWithRouter(<NodesPage />);
    await user.click(screen.getByText("Up Box").closest("tr")!);

    expect(screen.queryByRole("button", { name: /bring online/i })).not.toBeInTheDocument();
  });

  /**
   * The panel must be closable, or it covers the table until a reload.
   *
   * AI Note: the close button is located by DOM position (the only button in
   * the panel-header container) because the X icon has no accessible name. This
   * selector is fragile by construction — if the header markup gains another
   * button or another wrapper level, fix the traversal here, and ideally give
   * the close button an aria-label in the page instead.
   */
  it("closes the detail panel via its close button", async () => {
    nodesState.nodes = [makeNode({ display_name: "Close Box", status: "online" })];

    const { user } = renderWithRouter(<NodesPage />);
    await user.click(screen.getByText("Close Box").closest("tr")!);

    // Panel open: a Specifications section is visible.
    const specs = screen.getByText(/specifications/i);
    expect(specs).toBeInTheDocument();

    // The panel header has the node name as an h2; its sibling button is the X close.
    const panelHeading = screen.getByRole("heading", { name: "Close Box" });
    const closeButton = within(panelHeading.parentElement!.parentElement!).getByRole("button");
    await user.click(closeButton);

    expect(screen.queryByText(/specifications/i)).not.toBeInTheDocument();
  });
});
