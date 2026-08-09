/**
 * Tests for the Storage page (src/pages/Storage.tsx).
 *
 * Job artifacts are persisted to pluggable backends (MinIO, S3, a NAS mount,
 * Google Drive), and this page is the operator UI for registering, health
 * checking and removing them. Four areas are covered here:
 *
 *  1. The backend grid  — `useStorageStore` (GET /api/storage/backends), with a
 *                         loading frame, an empty state, the type pill and the
 *                         Default star.
 *  2. `CapacityBar`     — the used/total meter, its warn/critical thresholds and
 *                         its "unknown capacity" branch.
 *  3. Health + delete   — the on-demand probe (GET .../health) and the
 *                         `confirm()`-gated DELETE.
 *  4. Add Backend       — the modal that POSTs a free-form JSON `config`, plus
 *                         the transfers table below it.
 *
 * What is real vs stubbed: the components and the real `cn` / `formatBytes`
 * helpers run for real. Two boundaries are replaced — `@/stores` (backends +
 * credentials) and `@/api/client` — so nothing here touches the network.
 *
 * AI Note: nothing on this page auto-refreshes. Health results, backend usage
 * and the transfer table are point-in-time snapshots taken on mount or on
 * explicit user action; there is no polling and no WebSocket channel for storage
 * events. Several tests below pin that (a green dot persisting, a transfers table
 * that never reloads) as deliberate behaviour rather than a bug.
 *
 * Neighbouring pieces: the credentials this page's picker offers are managed on
 * `Admin.tsx` (Admin.test.tsx), and the backend chosen here is what
 * `JobDetail.tsx` downloads results from.
 */
import { vi, describe, it, expect, beforeEach } from "vitest";
import {
  act,
  renderWithRouter,
  screen,
  within,
  waitFor,
  makeBackend,
  makeCredential,
} from "../test/test-utils";
import type { CredentialInfo, StorageBackendInfo, TransferInfo } from "@/types";

// ── Mock the store + api boundaries ─────────────────────────────────────────
// Both stores are consumed as bare destructuring calls, but the mocks honour the
// selector form too so they behave like zustand for any future caller.
//
// AI Note: `useStorageStore` additionally needs a `getState()` — the page calls
// `useStorageStore.getState().fetch()` from its delete and create handlers
// (outside the render, where the hook is unavailable). A mock without it throws
// "getState is not a function" the moment a mutation succeeds.
//
// AI Note: this must live in `vi.hoisted` — `vi.mock` factories are hoisted above
// every `const`, so a factory closing over a normally-declared binding throws
// "Cannot access 'x' before initialization" at import time.
const h = vi.hoisted(() => {
  const storageFetch = vi.fn().mockResolvedValue(undefined);
  const credFetch = vi.fn().mockResolvedValue(undefined);
  const storageState: {
    backends: unknown[];
    isLoading: boolean;
    fetch: typeof storageFetch;
  } = { backends: [], isLoading: false, fetch: storageFetch };
  const credentialsState: { credentials: unknown[]; fetch: typeof credFetch } = {
    credentials: [],
    fetch: credFetch,
  };
  const useStorageStore = Object.assign(
    vi.fn((selector?: (s: typeof storageState) => unknown) =>
      selector ? selector(storageState) : storageState
    ),
    { getState: () => storageState }
  );
  const useCredentialsStore = vi.fn(
    (selector?: (s: typeof credentialsState) => unknown) =>
      selector ? selector(credentialsState) : credentialsState
  );
  const api = {
    listTransfers: vi.fn(),
    checkBackendHealth: vi.fn(),
    createBackend: vi.fn(),
    deleteBackend: vi.fn(),
    transferArtifact: vi.fn(),
  };
  return {
    storageFetch,
    credFetch,
    storageState,
    credentialsState,
    useStorageStore,
    useCredentialsStore,
    api,
  };
});

vi.mock("@/stores", () => ({
  useStorageStore: h.useStorageStore,
  useCredentialsStore: h.useCredentialsStore,
}));
vi.mock("@/api/client", () => ({ api: h.api }));

import Storage from "./Storage";

/** Narrowed aliases so tests can seed typed fixtures into the hoisted state. */
const storageState = h.storageState as {
  backends: StorageBackendInfo[];
  isLoading: boolean;
  fetch: typeof h.storageFetch;
};
const credentialsState = h.credentialsState as {
  credentials: CredentialInfo[];
  fetch: typeof h.credFetch;
};

// ── Local fixtures ──────────────────────────────────────────────────────────

let _seq = 0;
/** Distinct, UUID-shaped id. Never assert on a literal — capture it from the fixture. */
const tid = () => `20000000-0000-0000-0000-${String(++_seq).padStart(12, "0")}`;

/** A completed transfer with no error. `TransferInfo` has no factory in test-utils. */
function makeTransfer(overrides: Partial<TransferInfo> = {}): TransferInfo {
  return {
    id: tid(),
    artifact_id: "aaaaaaaa-1111-2222-3333-444444444444",
    source_backend_id: "bbbbbbbb-1111-2222-3333-444444444444",
    dest_backend_id: "cccccccc-1111-2222-3333-444444444444",
    status: "completed",
    bytes_transferred: 1024,
    error: null,
    started_at: new Date().toISOString(),
    completed_at: new Date().toISOString(),
    ...overrides,
  };
}

/** One gibibyte, so capacity fixtures render as clean "N GB" strings via `formatBytes`. */
const GB = 1024 ** 3;

// ── Query helpers ───────────────────────────────────────────────────────────

/**
 * The card element for a backend, located from its name heading.
 *
 * `.bg-card` is the card's own class; the only other `.bg-card` on the page (the
 * transfers table wrapper, and the empty state) is never an ancestor of a
 * backend name, so this resolves unambiguously.
 */
const cardFor = (name: string) =>
  screen.getByRole("heading", { name, level: 3 }).closest(".bg-card") as HTMLElement;

/**
 * The form control that a visible `<label>` describes.
 *
 * Needed because the Add Backend dialog's labels are plain siblings of their
 * inputs (no `htmlFor`/`id` pair), so `getByLabelText` cannot resolve them. The
 * two checkbox labels DO wrap their input and are queried with `getByLabelText`.
 *
 * AI Note: this is an accessibility gap in the page, not a test convenience —
 * screen readers cannot associate these labels either.
 */
function inputFor(label: string | RegExp): HTMLElement {
  const el = screen.getByText(label, { selector: "label" });
  return el.parentElement!.querySelector("input, textarea, select") as HTMLElement;
}

/** Open the Add Backend modal. */
async function openAddDialog(user: ReturnType<typeof renderWithRouter>["user"]) {
  await user.click(screen.getByRole("button", { name: /add backend/i }));
  await screen.findByRole("heading", { name: "Add Storage Backend" });
}

/**
 * Render the page and drain the mount effect before returning.
 *
 * Every test uses this instead of calling `renderWithRouter` directly, because
 * the mount effect fires `api.listTransfers()` and then calls `setTransfers` /
 * `setTransfersLoading` when it resolves. In a test whose body is otherwise
 * synchronous that resolution lands on the microtask queue AFTER the test
 * returns, and React logs "An update to Storage inside a test was not wrapped in
 * act(...)". Awaiting an empty async `act` here drains the queue while React is
 * still inside act scope.
 *
 * AI Note: this does NOT make a pending promise settle — a test that stubs
 * `listTransfers` with a never-resolving promise still sees the loading frame,
 * which is exactly what the transfers-spinner test relies on.
 */
async function renderStorage() {
  const view = renderWithRouter(<Storage />);
  await act(async () => {});
  return view;
}

beforeEach(() => {
  storageState.backends = [];
  storageState.isLoading = false;
  storageState.fetch = h.storageFetch;
  credentialsState.credentials = [];
  credentialsState.fetch = h.credFetch;

  h.storageFetch.mockResolvedValue(undefined);
  h.credFetch.mockResolvedValue(undefined);
  // Re-declare every api implementation per test so a test that overrides one
  // (e.g. to reject) cannot leak that into the next.
  h.api.listTransfers.mockResolvedValue([]);
  h.api.checkBackendHealth.mockResolvedValue({ healthy: true });
  h.api.createBackend.mockResolvedValue(makeBackend());
  h.api.deleteBackend.mockResolvedValue(undefined);
  h.api.transferArtifact.mockResolvedValue(makeTransfer());

  // Deleting a backend goes through the browser's blocking confirm(); default to
  // "the operator said yes" and override per test.
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

// ── Backend grid ────────────────────────────────────────────────────────────

/** The card grid: mount fetches, loading/empty/populated frames, card content. */
describe("Storage page — backend grid", () => {
  /** Mount loads all three data sources: backends, credentials and transfers. */
  it("fetches backends, credentials and transfers once on mount", async () => {
    await renderStorage();

    expect(h.storageFetch).toHaveBeenCalledTimes(1);
    expect(h.credFetch).toHaveBeenCalledTimes(1);
    expect(h.api.listTransfers).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByText(/no transfers recorded/i)).toBeInTheDocument());
  });

  /** The header offers the one creation entry point. */
  it("renders the Storage heading and an Add Backend button", async () => {
    await renderStorage();

    expect(screen.getByRole("heading", { name: "Storage", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add backend/i })).toBeInTheDocument();
  });

  /**
   * Loading suppresses both the grid and the empty state. Regression guarded:
   * flashing "No storage backends configured." on every page load, which reads
   * as a lost storage configuration.
   */
  it("renders a spinner instead of cards or the empty state while loading", async () => {
    storageState.isLoading = true;
    storageState.backends = [];
    await renderStorage();

    expect(screen.queryByText(/no storage backends configured/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Test" })).not.toBeInTheDocument();
  });

  /** Zero backends shows an explicit empty state. */
  it("shows the empty state when no backends are configured", async () => {
    storageState.backends = [];
    await renderStorage();

    expect(screen.getByText(/no storage backends configured/i)).toBeInTheDocument();
  });

  /** One card per backend, each with its own Test and Delete actions. */
  it("renders one card per backend with Test and Delete actions", async () => {
    storageState.backends = [
      makeBackend({ name: "minio-primary" }),
      makeBackend({ name: "nas-archive", backend_type: "nas", is_default: false }),
    ];
    await renderStorage();

    expect(screen.getByRole("heading", { name: "minio-primary", level: 3 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "nas-archive", level: 3 })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Test" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(2);
  });

  /**
   * Only the default backend wears the star. Regression guarded: showing
   * "Default" on every card, which hides where new artifacts actually land.
   */
  it("marks only the default backend with the Default badge", async () => {
    storageState.backends = [
      makeBackend({ name: "minio-primary", is_default: true }),
      makeBackend({ name: "nas-archive", is_default: false }),
    ];
    await renderStorage();

    expect(within(cardFor("minio-primary")).getByText("Default")).toBeInTheDocument();
    expect(within(cardFor("nas-archive")).queryByText("Default")).not.toBeInTheDocument();
  });

  /** The scheduling priority is surfaced per card. */
  it("shows the backend's scheduling priority", async () => {
    storageState.backends = [makeBackend({ name: "minio-primary", priority: 42 })];
    await renderStorage();

    expect(within(cardFor("minio-primary")).getByText("Priority: 42")).toBeInTheDocument();
  });
});

// ── Type badge ──────────────────────────────────────────────────────────────

/**
 * `typeBadge` maps `backend_type` to an icon + colour.
 *
 * Colour assertions match on hue substrings rather than exact Tailwind classes so
 * shade tweaks don't break the suite while genuine mis-mappings still do.
 */
describe("Storage page — backend type badge", () => {
  /** Each known type gets its own hue, so backends are distinguishable at a glance. */
  it("colors each known backend type differently", async () => {
    storageState.backends = [
      makeBackend({ name: "m", backend_type: "minio" }),
      makeBackend({ name: "n", backend_type: "nas" }),
      makeBackend({ name: "s", backend_type: "s3" }),
      makeBackend({ name: "g", backend_type: "gdrive" }),
    ];
    await renderStorage();

    expect(screen.getByText("minio").className).toMatch(/orange/);
    expect(screen.getByText("nas").className).toMatch(/purple/);
    expect(screen.getByText("s3").className).toMatch(/blue/);
    expect(screen.getByText("gdrive").className).toMatch(/green/);
  });

  /** The lookup is case-insensitive, so a differently-cased server value still styles. */
  it("matches the type map case-insensitively", async () => {
    storageState.backends = [makeBackend({ name: "mixed", backend_type: "MinIO" })];
    await renderStorage();

    expect(screen.getByText("MinIO").className).toMatch(/orange/);
  });

  /**
   * An unknown type must not crash the grid — a backend type added server-side
   * without a matching key falls back to neutral styling and still shows its
   * label.
   */
  it("falls back to neutral styling for an unknown backend type", async () => {
    storageState.backends = [makeBackend({ name: "future", backend_type: "azure" })];
    await renderStorage();

    const badge = screen.getByText("azure");
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/bg-secondary/);
    // The neutral fallback still renders an icon rather than an empty pill.
    expect(badge.querySelector("svg")).not.toBeNull();
  });
});

// ── Capacity meter ──────────────────────────────────────────────────────────

/**
 * `CapacityBar` has two modes: a bare text line when capacity is unknown, and a
 * used/total line plus a thresholded bar when it is known (yellow > 70%,
 * red > 90%).
 */
describe("Storage page — capacity meter", () => {
  /** The known-capacity mode shows formatted used/total plus a percentage. */
  it("renders used/total and a percentage when capacity is known", async () => {
    storageState.backends = [
      makeBackend({ name: "minio-primary", used_bytes: 0, capacity_bytes: 1024 ** 4 }),
    ];
    await renderStorage();

    const card = cardFor("minio-primary");
    expect(within(card).getByText("0 B / 1 TB")).toBeInTheDocument();
    expect(within(card).getByText("0%")).toBeInTheDocument();
  });

  /** Comfortably-used backends read green. */
  it("colors the bar green below the 70% warning threshold", async () => {
    storageState.backends = [
      makeBackend({ name: "roomy", used_bytes: 50 * GB, capacity_bytes: 100 * GB }),
    ];
    await renderStorage();

    const card = cardFor("roomy");
    expect(within(card).getByText("50 GB / 100 GB")).toBeInTheDocument();
    expect(within(card).getByText("50%")).toBeInTheDocument();
    expect(card.querySelector(".bg-green-500")).not.toBeNull();
  });

  /** Above 70% the bar warns, so a filling backend is noticed before it fails. */
  it("colors the bar yellow above the 70% warning threshold", async () => {
    storageState.backends = [
      makeBackend({ name: "filling", used_bytes: 75 * GB, capacity_bytes: 100 * GB }),
    ];
    await renderStorage();

    const card = cardFor("filling");
    expect(within(card).getByText("75%")).toBeInTheDocument();
    expect(card.querySelector(".bg-yellow-500")).not.toBeNull();
    expect(card.querySelector(".bg-green-500")).toBeNull();
  });

  /** Above 90% it escalates to critical. */
  it("colors the bar red above the 90% critical threshold", async () => {
    storageState.backends = [
      makeBackend({ name: "critical", used_bytes: 95 * GB, capacity_bytes: 100 * GB }),
    ];
    await renderStorage();

    const card = cardFor("critical");
    expect(within(card).getByText("95%")).toBeInTheDocument();
    expect(card.querySelector(".bg-red-500")).not.toBeNull();
  });

  /**
   * Exactly 70% is still green (`> 70`, not `>=`). Boundary pinned so a change
   * to the comparison is a visible, deliberate one.
   */
  it("treats exactly 70% as still-green (boundary)", async () => {
    storageState.backends = [
      makeBackend({ name: "edge", used_bytes: 70 * GB, capacity_bytes: 100 * GB }),
    ];
    await renderStorage();

    const card = cardFor("edge");
    expect(within(card).getByText("70%")).toBeInTheDocument();
    expect(card.querySelector(".bg-green-500")).not.toBeNull();
  });

  /**
   * An over-provisioned backend is clamped to 100%, so the bar can never
   * overflow its track. Regression guarded: a width style above 100% spilling
   * across the card.
   */
  it("clamps the meter at 100% for an over-capacity backend", async () => {
    storageState.backends = [
      makeBackend({ name: "over", used_bytes: 200 * GB, capacity_bytes: 100 * GB }),
    ];
    await renderStorage();

    const card = cardFor("over");
    expect(within(card).getByText("100%")).toBeInTheDocument();
    expect(card.querySelector(".bg-red-500")).not.toBeNull();
  });

  /** A null capacity switches to the text-only mode; a percentage would be meaningless. */
  it("shows 'Unknown capacity' and no bar when capacity_bytes is null", async () => {
    storageState.backends = [
      makeBackend({ name: "unbounded", used_bytes: 5 * GB, capacity_bytes: null }),
    ];
    await renderStorage();

    const card = cardFor("unbounded");
    expect(within(card).getByText("Used: 5 GB / Unknown capacity")).toBeInTheDocument();
    expect(card.querySelector(".bg-green-500")).toBeNull();
    expect(within(card).queryByText(/%$/)).not.toBeInTheDocument();
  });

  /**
   * An explicit 0 capacity takes the same branch (the check is falsy, not
   * null-only). Deliberate: dividing by zero would put `Infinity`/`NaN` into the
   * bar's width style.
   */
  it("treats a zero capacity as unknown rather than dividing by zero", async () => {
    storageState.backends = [
      makeBackend({ name: "zero-cap", used_bytes: 1024, capacity_bytes: 0 }),
    ];
    await renderStorage();

    const card = cardFor("zero-cap");
    expect(within(card).getByText("Used: 1 KB / Unknown capacity")).toBeInTheDocument();
    expect(within(card).queryByText(/NaN|Infinity/)).not.toBeInTheDocument();
  });
});

// ── Health probe ────────────────────────────────────────────────────────────

/** The on-demand reachability probe and the dot it lights. */
describe("Storage page — health probe", () => {
  /**
   * An untested backend shows NO dot. Pinned because the card checks
   * `health !== undefined` rather than truthiness — collapsing that to a
   * truthiness check would make every untested backend look unhealthy.
   */
  it("shows no health dot before the backend has been tested", async () => {
    storageState.backends = [makeBackend({ name: "minio-primary" })];
    await renderStorage();

    const card = cardFor("minio-primary");
    expect(card.querySelector('[title="Healthy"]')).toBeNull();
    expect(card.querySelector('[title="Unhealthy"]')).toBeNull();
  });

  /** A healthy probe lights a green dot with an accessible title. */
  it("shows a green Healthy dot after a successful probe", async () => {
    const backend = makeBackend({ name: "minio-primary" });
    storageState.backends = [backend];
    h.api.checkBackendHealth.mockResolvedValue({ healthy: true });
    const { user } = await renderStorage();

    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => expect(h.api.checkBackendHealth).toHaveBeenCalledWith(backend.id));
    await waitFor(() => {
      const dot = cardFor("minio-primary").querySelector('[title="Healthy"]');
      expect(dot).not.toBeNull();
      expect(dot!.className).toMatch(/bg-green-500/);
    });
  });

  /** An unhealthy probe (still a 200) lights a red dot. */
  it("shows a red Unhealthy dot when the probe reports the backend is down", async () => {
    storageState.backends = [makeBackend({ name: "minio-primary" })];
    h.api.checkBackendHealth.mockResolvedValue({ healthy: false });
    const { user } = await renderStorage();

    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => {
      const dot = cardFor("minio-primary").querySelector('[title="Unhealthy"]');
      expect(dot).not.toBeNull();
      expect(dot!.className).toMatch(/bg-red-500/);
    });
  });

  /**
   * Documents ACTUAL behaviour: a THROWN probe is recorded as `healthy: false`.
   *
   * POSSIBLE BUG (reported, not fixed): "the Nexus server could not run the
   * check" is displayed identically to "the backend is down". Deliberate for an
   * ops dashboard, but a red dot is therefore not diagnostic on its own.
   */
  it("shows a red dot when the probe request itself fails (failure conflation)", async () => {
    storageState.backends = [makeBackend({ name: "minio-primary" })];
    h.api.checkBackendHealth.mockRejectedValue(new Error("HTTP 500"));
    const { user } = await renderStorage();

    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() =>
      expect(cardFor("minio-primary").querySelector('[title="Unhealthy"]')).not.toBeNull()
    );
    expect(screen.queryByText(/http 500/i)).not.toBeInTheDocument();
  });

  /**
   * Health results are keyed by id, so testing one card must not paint a dot on
   * its neighbours. Regression guarded: a single shared health flag.
   */
  it("records the health result only against the tested backend", async () => {
    storageState.backends = [
      makeBackend({ name: "tested" }),
      makeBackend({ name: "untested", is_default: false }),
    ];
    const { user } = await renderStorage();

    await user.click(within(cardFor("tested")).getByRole("button", { name: "Test" }));

    await waitFor(() =>
      expect(cardFor("tested").querySelector('[title="Healthy"]')).not.toBeNull()
    );
    expect(cardFor("untested").querySelector('[title="Healthy"]')).toBeNull();
  });

  /**
   * Only the in-flight row is disabled, so an operator can probe several
   * backends at once. Regression guarded: a page-wide "busy" flag.
   */
  it("disables only the tested card's button while its probe is in flight", async () => {
    storageState.backends = [
      makeBackend({ name: "slow" }),
      makeBackend({ name: "other", is_default: false }),
    ];
    h.api.checkBackendHealth.mockReturnValue(new Promise(() => {}));
    const { user } = await renderStorage();

    await user.click(within(cardFor("slow")).getByRole("button", { name: "Test" }));

    expect(within(cardFor("slow")).getByRole("button", { name: "Test" })).toBeDisabled();
    expect(within(cardFor("other")).getByRole("button", { name: "Test" })).toBeEnabled();
  });

  /**
   * Documents a real gap: results are never cleared, so a green dot outlives the
   * backend going down. Re-clicking Test is the only refresh.
   *
   * POSSIBLE BUG (reported, not fixed): a stale green dot on a dead backend is
   * indistinguishable from a fresh one — there is no timestamp and no expiry.
   */
  it("keeps a stale health dot after the backend list is refreshed (known gap)", async () => {
    const backend = makeBackend({ name: "minio-primary" });
    storageState.backends = [backend];
    const { user, rerender } = await renderStorage();

    await user.click(screen.getByRole("button", { name: "Test" }));
    await waitFor(() =>
      expect(cardFor("minio-primary").querySelector('[title="Healthy"]')).not.toBeNull()
    );

    // A later refetch replaced the store object; the recorded health survives.
    storageState.backends = [{ ...backend, used_bytes: 10 * GB }];
    rerender(<Storage />);

    expect(cardFor("minio-primary").querySelector('[title="Healthy"]')).not.toBeNull();
  });
});

// ── Delete backend ──────────────────────────────────────────────────────────

/**
 * Deleting a backend.
 *
 * AI Note: this uses the browser's blocking `window.confirm` rather than the
 * custom modal used on Jobs/Nodes/Pools — it is the odd one out on this page.
 * Deleting a backend only drops the registration; artifacts stored on it become
 * undownloadable.
 */
describe("Storage page — delete backend", () => {
  /** Confirming deletes the right backend and reloads the grid. */
  it("deletes the backend and refetches the list once confirmed", async () => {
    const backend = makeBackend({ name: "minio-primary" });
    storageState.backends = [backend];
    const { user } = await renderStorage();
    expect(h.storageFetch).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(h.api.deleteBackend).toHaveBeenCalledWith(backend.id));
    await waitFor(() => expect(h.storageFetch).toHaveBeenCalledTimes(2));
  });

  /** The prompt states that the action is irreversible. */
  it("warns that the deletion cannot be undone", async () => {
    storageState.backends = [makeBackend({ name: "minio-primary" })];
    const { user } = await renderStorage();

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(window.confirm).toHaveBeenCalledWith(
      "Delete this storage backend? This cannot be undone."
    );
  });

  /**
   * Declining the prompt is a hard stop. Regression guarded: a confirm() whose
   * return value is ignored, which would drop a backend on a stray click.
   */
  it("deletes nothing when the confirm prompt is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    storageState.backends = [makeBackend({ name: "minio-primary" })];
    const { user } = await renderStorage();

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(h.api.deleteBackend).not.toHaveBeenCalled();
    expect(h.storageFetch).toHaveBeenCalledTimes(1);
  });

  /** Only the row being deleted is disabled, so the rest of the grid stays usable. */
  it("disables only the deleting card's button while the request is in flight", async () => {
    storageState.backends = [
      makeBackend({ name: "slow" }),
      makeBackend({ name: "other", is_default: false }),
    ];
    h.api.deleteBackend.mockReturnValue(new Promise(() => {}));
    const { user } = await renderStorage();

    await user.click(within(cardFor("slow")).getByRole("button", { name: "Delete" }));

    expect(within(cardFor("slow")).getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(within(cardFor("other")).getByRole("button", { name: "Delete" })).toBeEnabled();
  });

  /**
   * Documents ACTUAL behaviour on failure: the error is swallowed (the api client
   * handles 401 globally) and the button is simply re-enabled.
   *
   * POSSIBLE BUG (reported, not fixed): a 409 "backend still in use" produces no
   * message at all — the card just stays put, which reads as an unresponsive
   * button.
   */
  it("re-enables the button and shows no message when the delete fails", async () => {
    storageState.backends = [makeBackend({ name: "minio-primary" })];
    h.api.deleteBackend.mockRejectedValue(new Error("backend in use"));
    const { user } = await renderStorage();

    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete" })).toBeEnabled()
    );
    expect(screen.queryByText(/backend in use/i)).not.toBeInTheDocument();
    // No refetch on the failure path (the store fetch is only awaited on success).
    expect(h.storageFetch).toHaveBeenCalledTimes(1);
  });
});

// ── Add Backend dialog ──────────────────────────────────────────────────────

/** The registration modal: its controls, its payload encodings and its errors. */
describe("Storage page — add backend dialog", () => {
  /** The modal opens with every documented control. */
  it("opens with name, type, config, credential, capacity and default controls", async () => {
    const { user } = await renderStorage();

    await openAddDialog(user);

    expect(inputFor("Name")).toBeInTheDocument();
    expect(inputFor("Type")).toHaveValue("minio");
    expect(inputFor(/^Config/)).toHaveValue("{}");
    expect(inputFor("Credential")).toHaveValue("");
    expect(inputFor(/^Capacity/)).toHaveValue(null);
    expect(screen.getByLabelText(/set as default backend/i)).not.toBeChecked();
  });

  /** The four creatable types are the authoritative list of what the UI supports. */
  it("offers exactly the four supported backend types", async () => {
    const { user } = await renderStorage();
    await openAddDialog(user);

    const options = within(inputFor("Type")).getAllByRole("option");
    expect(options.map((o) => (o as HTMLOptionElement).value)).toEqual([
      "minio",
      "nas",
      "s3",
      "gdrive",
    ]);
  });

  /** The credential picker is populated from the credentials store, labelled by type. */
  it("lists the stored credentials in the picker with their type", async () => {
    credentialsState.credentials = [
      makeCredential({ name: "prod-s3", credential_type: "s3" }),
      makeCredential({ name: "nas-login", credential_type: "ssh" }),
    ];
    const { user } = await renderStorage();
    await openAddDialog(user);

    const select = inputFor("Credential");
    expect(within(select).getByRole("option", { name: "None" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "prod-s3 (s3)" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "nas-login (ssh)" })).toBeInTheDocument();
  });

  /**
   * The default payload encodings: `credential_id` omitted (undefined) when
   * "None" is selected, `capacity_bytes` explicit `null` when blank. Two
   * different "absent" encodings the server treats the same way — do not
   * normalise one to the other without checking the API schema.
   */
  it("creates a backend with the default encodings for an omitted credential and capacity", async () => {
    const { user } = await renderStorage();
    await openAddDialog(user);

    await user.type(inputFor("Name"), "minio-primary");
    await user.click(screen.getByRole("button", { name: /create backend/i }));

    await waitFor(() => expect(h.api.createBackend).toHaveBeenCalledTimes(1));
    const payload = h.api.createBackend.mock.calls[0][0];
    expect(payload).toMatchObject({
      name: "minio-primary",
      backend_type: "minio",
      config: {},
      is_default: false,
    });
    expect(payload.credential_id).toBeUndefined();
    expect(payload.capacity_bytes).toBeNull();
  });

  /** The full path: a parsed JSON config, a chosen credential, a capacity and the default flag. */
  it("passes the parsed config, chosen credential, capacity and default flag through", async () => {
    const cred = makeCredential({ name: "prod-s3", credential_type: "s3" });
    credentialsState.credentials = [cred];
    const { user } = await renderStorage();
    await openAddDialog(user);

    await user.type(inputFor("Name"), "s3-archive");
    await user.selectOptions(inputFor("Type"), "s3");
    await user.clear(inputFor(/^Config/));
    // `{{` is userEvent's escape for a literal `{` — a bare `{` starts a key
    // descriptor like `{Enter}` and throws a parse error.
    await user.type(inputFor(/^Config/), '{{"bucket": "artifacts"}');
    await user.selectOptions(inputFor("Credential"), cred.id);
    await user.type(inputFor(/^Capacity/), "2048");
    await user.click(screen.getByLabelText(/set as default backend/i));
    await user.click(screen.getByRole("button", { name: /create backend/i }));

    await waitFor(() => expect(h.api.createBackend).toHaveBeenCalledTimes(1));
    expect(h.api.createBackend.mock.calls[0][0]).toEqual({
      name: "s3-archive",
      backend_type: "s3",
      config: { bucket: "artifacts" },
      credential_id: cred.id,
      capacity_bytes: 2048,
      is_default: true,
    });
  });

  /** On success the grid reloads, the modal closes and the form is reset for next time. */
  it("refetches the grid, closes the modal and resets the form on success", async () => {
    const { user } = await renderStorage();
    await openAddDialog(user);

    await user.type(inputFor("Name"), "minio-primary");
    await user.clear(inputFor(/^Config/));
    await user.type(inputFor(/^Config/), '{{"endpoint": "localhost:9000"}');
    await user.click(screen.getByRole("button", { name: /create backend/i }));

    await waitFor(() => expect(h.storageFetch).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Add Storage Backend" })).not.toBeInTheDocument()
    );

    // Reopening shows a pristine form, not the previous submission.
    await openAddDialog(user);
    expect(inputFor("Name")).toHaveValue("");
    expect(inputFor(/^Config/)).toHaveValue("{}");
  });

  /**
   * The config blob is validated client-side only as "is it JSON". A parse
   * failure must be reported and must not reach the server.
   *
   * AI Note: this early return manually clears `formSubmitting` because it sits
   * before the try/finally — hence the "still enabled" assertion, which is what
   * catches a regression that leaves the button disabled forever.
   */
  it("rejects an unparseable config, sends nothing and leaves the button usable", async () => {
    const { user } = await renderStorage();
    await openAddDialog(user);

    await user.type(inputFor("Name"), "broken");
    await user.clear(inputFor(/^Config/));
    await user.type(inputFor(/^Config/), "{{not json");
    await user.click(screen.getByRole("button", { name: /create backend/i }));

    expect(await screen.findByText("Config must be valid JSON")).toBeInTheDocument();
    expect(h.api.createBackend).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /create backend/i })).toBeEnabled();
  });

  /** A rejected create surfaces the server's message and keeps the operator's input. */
  it("shows the server error inline and preserves the form when creation fails", async () => {
    h.api.createBackend.mockRejectedValue(new Error("Backend name already exists"));
    const { user } = await renderStorage();
    await openAddDialog(user);

    await user.type(inputFor("Name"), "minio-primary");
    await user.click(screen.getByRole("button", { name: /create backend/i }));

    expect(await screen.findByText("Backend name already exists")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Add Storage Backend" })).toBeInTheDocument();
    expect(inputFor("Name")).toHaveValue("minio-primary");
    expect(h.storageFetch).toHaveBeenCalledTimes(1);
  });

  /** Cancel dismisses without registering anything. */
  it("closes the modal without creating when Cancel is clicked", async () => {
    const { user } = await renderStorage();
    await openAddDialog(user);

    await user.type(inputFor("Name"), "temp");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("heading", { name: "Add Storage Backend" })).not.toBeInTheDocument();
    expect(h.api.createBackend).not.toHaveBeenCalled();
  });

  /** The X in the modal header closes it too. */
  it("closes the modal via the header close button", async () => {
    const { user } = await renderStorage();
    await openAddDialog(user);

    const header = screen.getByRole("heading", { name: "Add Storage Backend" }).parentElement!;
    await user.click(within(header).getByRole("button"));

    expect(screen.queryByRole("heading", { name: "Add Storage Backend" })).not.toBeInTheDocument();
  });

  /**
   * Documents a deliberate inconsistency with the Pools page: this modal's
   * backdrop DOES close on click, so a stray click discards a half-typed
   * registration (the form state survives, but the modal is dismissed).
   */
  it("closes the modal when the backdrop is clicked (unlike the Pools dialog)", async () => {
    const { user } = await renderStorage();
    await openAddDialog(user);

    await user.type(inputFor("Name"), "half-typed");
    await user.click(document.querySelector('[class*="bg-black/50"]') as HTMLElement);

    expect(screen.queryByRole("heading", { name: "Add Storage Backend" })).not.toBeInTheDocument();
    // The form fields live on the page, not in the modal, so the input survives.
    await openAddDialog(user);
    expect(inputFor("Name")).toHaveValue("half-typed");
  });
});

// ── Transfers table ─────────────────────────────────────────────────────────

/**
 * The "Recent Transfers" table.
 *
 * AI Note: transfers are fetched with a raw promise chain (not through a store)
 * and the `.catch(() => {})` deliberately swallows failures, so a broken
 * transfers endpoint must not blank the backend cards.
 */
describe("Storage page — transfers table", () => {
  /** While the transfers request is outstanding, neither rows nor the empty copy show. */
  it("renders a spinner row while transfers load", async () => {
    h.api.listTransfers.mockReturnValue(new Promise(() => {}));
    await renderStorage();

    expect(screen.getByRole("heading", { name: "Recent Transfers" })).toBeInTheDocument();
    expect(screen.queryByText(/no transfers recorded/i)).not.toBeInTheDocument();
  });

  /** Zero transfers shows an explicit empty row. */
  it("shows 'No transfers recorded.' when there are none", async () => {
    h.api.listTransfers.mockResolvedValue([]);
    await renderStorage();

    expect(await screen.findByText(/no transfers recorded/i)).toBeInTheDocument();
  });

  /** A populated row shows truncated ids, a status pill, the bytes moved and no error. */
  it("renders a row per transfer with truncated ids, status, progress and a dash for no error", async () => {
    h.api.listTransfers.mockResolvedValue([
      makeTransfer({
        artifact_id: "abcdef01-2222-3333-4444-555555555555",
        source_backend_id: "11112222-3333-4444-5555-666666666666",
        dest_backend_id: "99998888-7777-6666-5555-444444444444",
        status: "completed",
        bytes_transferred: 2048,
        error: null,
      }),
    ]);
    await renderStorage();

    expect(await screen.findByText("abcdef01...")).toBeInTheDocument();
    expect(screen.getByText("11112222 -> 99998888")).toBeInTheDocument();
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    expect(screen.getByText("-")).toBeInTheDocument();
  });

  /**
   * `in_progress` is displayed as "in progress" via a single `replace("_", " ")`.
   * Pinned because a future status with two underscores would only have the first
   * one replaced.
   */
  it("renders in_progress as 'in progress' with its own colour", async () => {
    h.api.listTransfers.mockResolvedValue([makeTransfer({ status: "in_progress" })]);
    await renderStorage();

    const badge = await screen.findByText("in progress");
    expect(badge.className).toMatch(/blue/);
  });

  /** Each status gets a distinct hue so an operator can scan the table. */
  it("colors each transfer status differently", async () => {
    h.api.listTransfers.mockResolvedValue([
      makeTransfer({ status: "pending" }),
      makeTransfer({ status: "completed" }),
      makeTransfer({ status: "failed" }),
    ]);
    await renderStorage();

    expect((await screen.findByText("pending")).className).toMatch(/yellow/);
    expect(screen.getByText("completed").className).toMatch(/green/);
    expect(screen.getByText("failed").className).toMatch(/red/);
  });

  /** A failed transfer surfaces its error text so the cause is visible in the table. */
  it("renders the error message for a failed transfer", async () => {
    h.api.listTransfers.mockResolvedValue([
      makeTransfer({ status: "failed", error: "connection reset", bytes_transferred: 0 }),
    ]);
    await renderStorage();

    expect(await screen.findByText("connection reset")).toBeInTheDocument();
    // formatBytes(0) has its own branch and must not render "NaN".
    expect(screen.getByText("0 B")).toBeInTheDocument();
  });

  /**
   * Documents ACTUAL behaviour: a failed transfers fetch is swallowed, so the
   * table shows the same "No transfers recorded." as a genuinely empty history —
   * while the backend cards still render, which is the point of the swallow.
   *
   * POSSIBLE BUG (reported, not fixed): "the endpoint 500'd" is
   * indistinguishable from "no transfers have ever run".
   */
  it("silently shows the empty state when the transfers request fails", async () => {
    h.api.listTransfers.mockRejectedValue(new Error("HTTP 500"));
    storageState.backends = [makeBackend({ name: "minio-primary" })];
    await renderStorage();

    expect(await screen.findByText(/no transfers recorded/i)).toBeInTheDocument();
    expect(screen.queryByText(/http 500/i)).not.toBeInTheDocument();
    // The backend cards are unaffected — the whole reason the failure is swallowed.
    expect(screen.getByRole("heading", { name: "minio-primary", level: 3 })).toBeInTheDocument();
  });

  /**
   * Documents a real gap: the transfers table is READ-ONLY. There is no control
   * anywhere on this page that starts a transfer, so `api.transferArtifact`
   * (POST /api/storage/transfer) is unreachable from the UI.
   *
   * POSSIBLE BUG (reported, not fixed): the backend team found that
   * POST /api/storage/transfer always 404s because a str-keyed backend registry
   * is looked up with a UUID. That defect is currently INVISIBLE to users
   * precisely because this page never calls it — this test pins the absence, so
   * whoever adds a "Copy to backend" button is forced to notice.
   */
  it("offers no control that starts a transfer, so the transfer endpoint is unreachable (known gap)", async () => {
    storageState.backends = [
      makeBackend({ name: "minio-primary" }),
      makeBackend({ name: "nas-archive", backend_type: "nas", is_default: false }),
    ];
    h.api.listTransfers.mockResolvedValue([makeTransfer()]);
    await renderStorage();
    await screen.findByText("completed");

    expect(
      screen.queryByRole("button", { name: /transfer|copy to|move to|migrate/i })
    ).not.toBeInTheDocument();
    expect(h.api.transferArtifact).not.toHaveBeenCalled();
  });

  /**
   * The table is a mount-time snapshot: nothing re-polls it, so a transfer that
   * completes while the page is open is never reflected. Pinned as deliberate
   * (there is no WS channel for storage events) rather than left implicit.
   */
  it("never re-fetches transfers after mount (snapshot, not a live feed)", async () => {
    h.api.listTransfers.mockResolvedValue([makeTransfer()]);
    storageState.backends = [makeBackend({ name: "minio-primary" })];
    const { user } = await renderStorage();
    await screen.findByText("completed");

    // A mutation refreshes the backend grid but deliberately not the transfers.
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(h.storageFetch).toHaveBeenCalledTimes(2));

    expect(h.api.listTransfers).toHaveBeenCalledTimes(1);
  });
});
