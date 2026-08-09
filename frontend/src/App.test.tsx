/**
 * Tests for the root component and route table (src/App.tsx).
 *
 * What this file is responsible for: the WIRING, not the pages. `App` declares
 * a `BrowserRouter` with exactly three kinds of entry:
 *
 *  1. `/login`  — rendered bare, outside the shell, for visitors with no session.
 *  2. `/`       — the `Layout` shell (the auth gate) as the parent of eight child
 *                 routes, each of which renders into its `<Outlet />`.
 *  3. `*`       — a catch-all that redirects to `/` (NOT to `/login`).
 *
 * So the questions asked here are: does each URL mount the right page, does an
 * anonymous visitor get bounced to the login form, is an unknown URL handled
 * gracefully, and does the `/admin` route gate on role.
 *
 * What is real vs stubbed:
 * - REAL: `App` itself, react-router's `BrowserRouter`/`Routes` matching and
 *   ranking (the thing under test), and `@/components/Layout` — the auth gate is
 *   half of the routing contract, so stubbing it would delete the redirect tests.
 * - STUBBED: all nine page modules (`@/pages/*`) are replaced with one-line
 *   probes, so a routing failure never hides behind a page's own fetches, and
 *   `@/stores` + `@/hooks/useWebSocket` so nothing here touches the network or
 *   opens a socket.
 *
 * AI Note: `App` renders its OWN `BrowserRouter`, so the shared
 * `renderWithRouter` helper from `../test/test-utils` cannot be used — nesting
 * routers makes the inner one win and silently ignore `initialEntries`. The
 * current URL is instead set with `history.pushState` before `render` (see
 * `renderApp`), which also means `window.location.pathname` is a legitimate
 * assertion target here — unlike in the MemoryRouter-based page tests.
 *
 * Neighbouring pieces: the shell's chrome, nav highlighting and logout are
 * covered by src/components/Layout.test.tsx (deliberately not repeated here);
 * each page's internals live in src/pages/*.test.tsx.
 */
import { vi, describe, it, expect, beforeEach } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UserInfo } from "@/types";
import { makeUser } from "./test/test-utils";

// ── Store / socket boundaries ────────────────────────────────────────────────
// `Layout` is the only real consumer of these: it destructures `useAuthStore()`
// bare, passes the module-level `handleWsMessage` to `useWebSocket`, and calls
// `fetchUser()` on mount. Mocking the store lets each test declare a session
// state (pending / anonymous / signed-in as a given role) synchronously.
//
// AI Note: must live in `vi.hoisted` — `vi.mock` factories are hoisted above
// every `const`, so a factory closing over a normally-declared binding throws
// "Cannot access 'x' before initialization" at import time.
const h = vi.hoisted(() => {
  const fetchUser = vi.fn(() => Promise.resolve());
  const logout = vi.fn();
  const authState: {
    user: unknown;
    isLoading: boolean;
    fetchUser: typeof fetchUser;
    logout: typeof logout;
  } = { user: null, isLoading: true, fetchUser, logout };
  const useAuthStore = vi.fn((selector?: (s: typeof authState) => unknown) =>
    selector ? selector(authState) : authState
  );
  const handleWsMessage = vi.fn();
  const useWebSocket = vi.fn(() => ({ current: null }));
  return { fetchUser, logout, authState, useAuthStore, handleWsMessage, useWebSocket };
});

vi.mock("@/stores", () => ({
  useAuthStore: h.useAuthStore,
  handleWsMessage: h.handleWsMessage,
}));

vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: h.useWebSocket }));

// ── Page stubs ───────────────────────────────────────────────────────────────
// One probe per route target. Every probe uses a `page-*` test id so
// `mountedPages()` can assert on "which page is on screen" generically, and each
// renders its own name as text so a mis-wired route fails with a readable diff
// instead of an empty DOM.
//
// AI Note: the JSX lives inline in each factory on purpose. A shared
// `makeProbe()` helper declared with `const` would be in its temporal dead zone
// when the hoisted factories run. JSX itself is safe because it compiles to a
// call into the auto-imported jsx runtime, which is a real (hoisted) import.
vi.mock("@/pages/Login", () => ({
  default: () => <div data-testid="page-login">page:login</div>,
}));
vi.mock("@/pages/Dashboard", () => ({
  default: () => <div data-testid="page-dashboard">page:dashboard</div>,
}));
vi.mock("@/pages/Nodes", () => ({
  default: () => <div data-testid="page-nodes">page:nodes</div>,
}));
vi.mock("@/pages/Pools", () => ({
  default: () => <div data-testid="page-pools">page:pools</div>,
}));
vi.mock("@/pages/Jobs", () => ({
  default: () => <div data-testid="page-jobs">page:jobs</div>,
}));
vi.mock("@/pages/JobBuilder", () => ({
  default: () => <div data-testid="page-job-builder">page:job-builder</div>,
}));
vi.mock("@/pages/Storage", () => ({
  default: () => <div data-testid="page-storage">page:storage</div>,
}));
vi.mock("@/pages/Admin", () => ({
  default: () => <div data-testid="page-admin">page:admin</div>,
}));

// JobDetail is the only parameterised route, so its probe echoes `useParams().id`
// — that is what proves the route pattern really is `jobs/:id` and not, say,
// `jobs/:jobId` (which would compile fine and leave the page fetching nothing).
//
// AI Note: an ASYNC factory with `await import(...)` is the safe way to reach a
// real module from inside a mock; a top-level `import { useParams }` would be in
// its TDZ when the factory runs.
vi.mock("@/pages/JobDetail", async () => {
  const { useParams } = await import("react-router-dom");
  return {
    default: () => {
      const { id } = useParams();
      return <div data-testid="page-job-detail">{`page:job-detail:${id ?? "none"}`}</div>;
    },
  };
});

import App from "./App";

/** Narrowed alias so tests can seed a typed user into the hoisted store state. */
const authState = h.authState as {
  user: UserInfo | null;
  isLoading: boolean;
  fetchUser: typeof h.fetchUser;
  logout: typeof h.logout;
};

// ── Harness ──────────────────────────────────────────────────────────────────

/**
 * Render `<App />` with the browser URL set to `route`.
 *
 * `pushState` is used rather than assigning `location.href` because jsdom
 * refuses real navigations; `BrowserRouter` reads `window.location` on mount, so
 * the push must happen BEFORE `render`.
 */
function renderApp(route = "/") {
  window.history.pushState({}, "", route);
  const user = userEvent.setup();
  return { user, ...render(<App />) };
}

/** Every page probe currently on screen (normally exactly one, sometimes none). */
const mountedPages = () => screen.queryAllByTestId(/^page-/);

/** The authenticated shell's sidebar nav, or null when the shell is not mounted. */
const nav = () => screen.queryByRole("navigation");

/** Sign in as `role` with the session check already finished. */
function signIn(role: UserInfo["role"] = "user", overrides: Partial<UserInfo> = {}) {
  authState.isLoading = false;
  authState.user = makeUser({ role, ...overrides });
}

/** Finish the session check with no session — the anonymous visitor case. */
function signOut() {
  authState.isLoading = false;
  authState.user = null;
}

/** Every path the route table declares behind the `Layout` auth gate. */
const PROTECTED_PATHS = [
  "/",
  "/nodes",
  "/pools",
  "/jobs",
  "/jobs/new",
  "/jobs/00000000-0000-0000-0000-000000000001",
  "/storage",
  "/admin",
] as const;

beforeEach(() => {
  h.fetchUser.mockClear();
  h.logout.mockClear();
  h.useWebSocket.mockClear();
  h.useAuthStore.mockClear();
  authState.user = null;
  authState.isLoading = true;
  authState.fetchUser = h.fetchUser;
  authState.logout = h.logout;
  // Reset jsdom's URL so a redirect performed by one test cannot become the
  // starting point of the next.
  window.history.pushState({}, "", "/");
});

// ── Authenticated route table ───────────────────────────────────────────────

/**
 * The happy path: a signed-in user deep-linking to each declared URL. One test
 * per route, because a copy-paste slip in the table (two routes pointing at the
 * same component) is exactly the failure a single parameterised test would
 * flatten into an unreadable diff.
 */
describe("App — route table for a signed-in user", () => {
  beforeEach(() => signIn("admin"));

  /** The index route: `/` renders the Dashboard inside the shell's <main>. */
  it("renders the Dashboard at the index route inside the shell", () => {
    renderApp("/");
    expect(within(screen.getByRole("main")).getByTestId("page-dashboard")).toBeInTheDocument();
    expect(nav()).toBeInTheDocument();
  });

  it("renders the Nodes page at /nodes", () => {
    renderApp("/nodes");
    expect(screen.getByTestId("page-nodes")).toHaveTextContent("page:nodes");
  });

  it("renders the Pools page at /pools", () => {
    renderApp("/pools");
    expect(screen.getByTestId("page-pools")).toHaveTextContent("page:pools");
  });

  it("renders the Jobs list at /jobs", () => {
    renderApp("/jobs");
    expect(screen.getByTestId("page-jobs")).toHaveTextContent("page:jobs");
  });

  it("renders the Job Builder at /jobs/new", () => {
    renderApp("/jobs/new");
    expect(screen.getByTestId("page-job-builder")).toHaveTextContent("page:job-builder");
  });

  it("renders the Storage page at /storage", () => {
    renderApp("/storage");
    expect(screen.getByTestId("page-storage")).toHaveTextContent("page:storage");
  });

  it("renders the Admin page at /admin", () => {
    renderApp("/admin");
    expect(screen.getByTestId("page-admin")).toHaveTextContent("page:admin");
  });

  /**
   * Every authenticated route renders inside `Layout`, never bare. Regression
   * guarded: a child route accidentally declared as a SIBLING of `/` would still
   * render its page — but with no sidebar, no session check and no WebSocket.
   */
  it("nests every authenticated page inside the Layout shell", () => {
    // Mounted one URL at a time (and unmounted again) so each iteration is an
    // independent page load rather than a navigation.
    for (const path of PROTECTED_PATHS) {
      const view = renderApp(path);
      const main = screen.getByRole("main");
      expect(within(main).getAllByTestId(/^page-/)).toHaveLength(1);
      expect(screen.getByRole("navigation")).toBeInTheDocument();
      view.unmount();
    }
  });

  /**
   * Exactly one page is mounted per URL. Regression guarded: the index route
   * losing its `index` flag and becoming a path-less always-match, which would
   * render the Dashboard underneath every other page.
   */
  it("mounts exactly one page at a time and never leaves the Dashboard behind", () => {
    renderApp("/nodes");
    expect(mountedPages()).toHaveLength(1);
    expect(screen.queryByTestId("page-dashboard")).not.toBeInTheDocument();
  });
});

// ── Route ranking and URL parsing ───────────────────────────────────────────

/**
 * The subtle half of the table: which of two overlapping patterns wins, and how
 * odd-but-valid URLs are parsed.
 */
describe("App — route ranking and URL shapes", () => {
  beforeEach(() => signIn());

  /**
   * `jobs/:id` renders JobDetail with the id it was given, verbatim.
   * Regression guarded: renaming the param (`:jobId`) — the route still matches,
   * so only an assertion on the resolved param catches it.
   */
  it("passes the URL segment to JobDetail as the `id` param", () => {
    renderApp("/jobs/00000000-0000-0000-0000-000000000042");
    expect(screen.getByTestId("page-job-detail")).toHaveTextContent(
      "page:job-detail:00000000-0000-0000-0000-000000000042"
    );
  });

  /** Ids are not validated at the routing layer — any single non-empty segment matches. */
  it("matches jobs/:id for a non-UUID id and forwards it unchanged", () => {
    renderApp("/jobs/not-a-uuid_123");
    expect(screen.getByTestId("page-job-detail")).toHaveTextContent(
      "page:job-detail:not-a-uuid_123"
    );
  });

  /**
   * The static-beats-dynamic guarantee.
   *
   * Regression guarded: react-router v6 ranks `jobs/new` above `jobs/:id`
   * regardless of declaration order, so "new" is never swallowed as a job id.
   * If a future refactor moves these into a `<Route path="jobs">` parent with a
   * splat, or reorders v5-style, this test is what notices.
   */
  it("prefers the static jobs/new route over the dynamic jobs/:id route", () => {
    renderApp("/jobs/new");
    expect(screen.getByTestId("page-job-builder")).toBeInTheDocument();
    expect(screen.queryByTestId("page-job-detail")).not.toBeInTheDocument();
  });

  /** A trailing slash is insignificant: `/nodes/` is the same route as `/nodes`. */
  it("treats a trailing slash as the same route", () => {
    renderApp("/nodes/");
    expect(screen.getByTestId("page-nodes")).toBeInTheDocument();
  });

  /** A query string does not affect matching and is preserved for the page to read. */
  it("ignores the query string when matching and leaves it on the URL", () => {
    renderApp("/nodes?status=online&page=2");
    expect(screen.getByTestId("page-nodes")).toBeInTheDocument();
    expect(window.location.search).toBe("?status=online&page=2");
  });

  /**
   * Documents ACTUAL behaviour: react-router's `caseSensitive` defaults to
   * false, so `/NODES` matches the `nodes` route rather than falling through to
   * the catch-all. Pinned so that turning case-sensitivity on becomes a visible,
   * deliberate change (it would start redirecting those URLs to the dashboard).
   */
  it("matches paths case-insensitively (/NODES renders Nodes)", () => {
    renderApp("/NODES");
    expect(screen.getByTestId("page-nodes")).toBeInTheDocument();
  });
});

// ── The /login route ────────────────────────────────────────────────────────

/** `/login` is the one route outside the shell — no chrome, no session check. */
describe("App — the /login route", () => {
  it("renders the login page for an anonymous visitor", () => {
    signOut();
    renderApp("/login");
    expect(screen.getByTestId("page-login")).toHaveTextContent("page:login");
  });

  /**
   * No sidebar, no wordmark. Regression guarded: moving `/login` under the `/`
   * layout route would wrap the login form in the authenticated chrome AND make
   * the gate redirect it to itself forever.
   */
  it("renders the login page bare, outside the Layout shell", () => {
    signOut();
    renderApp("/login");
    expect(nav()).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "NEXUS" })).not.toBeInTheDocument();
    expect(screen.queryByRole("main")).not.toBeInTheDocument();
  });

  /**
   * `Layout` never mounts on `/login`, so no GET /api/auth/me is fired for a
   * visitor who has no session to check.
   */
  it("does not run the session check on /login", () => {
    signOut();
    renderApp("/login");
    expect(h.fetchUser).not.toHaveBeenCalled();
  });

  /**
   * Documents ACTUAL behaviour: an already-authenticated user who navigates to
   * `/login` is shown the login form again — the route table has no
   * "already signed in, go to the dashboard" guard. Harmless (the page's own
   * submit handler redirects to `/` on success) but worth pinning: if such a
   * guard is ever added, this test fails and should be replaced.
   */
  it("still renders the login form for an already-authenticated user", () => {
    signIn("admin");
    renderApp("/login");
    expect(screen.getByTestId("page-login")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/login");
  });
});

// ── Unauthenticated access to protected routes ──────────────────────────────

/**
 * The auth gate as seen from the route table: no session means no page, on any
 * protected URL. (The gate's own mechanics are covered in Layout.test.tsx; what
 * matters here is that every declared route really sits behind it.)
 */
describe("App — unauthenticated access is redirected to /login", () => {
  beforeEach(() => signOut());

  it.each(PROTECTED_PATHS)("redirects %s to /login when there is no session", async (path) => {
    renderApp(path);

    expect(await screen.findByTestId("page-login")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/login");
  });

  /**
   * The protected page must never mount, not even for one frame. Regression
   * guarded (security-relevant): rendering the child route before the redirect
   * lands fires authenticated requests on behalf of an anonymous visitor.
   */
  it("never mounts the protected page while redirecting", async () => {
    renderApp("/admin");
    expect(screen.queryByTestId("page-admin")).not.toBeInTheDocument();

    await screen.findByTestId("page-login");
    expect(screen.queryByTestId("page-admin")).not.toBeInTheDocument();
    expect(mountedPages()).toHaveLength(1);
  });

  /**
   * While the session check is still outstanding nothing is decided yet: no page
   * and no redirect, just the shell's loading frame. Regression guarded: a
   * missing `isLoading` guard logs out every user who refreshes the tab.
   */
  it("shows the loading frame and no page while the session check is pending", () => {
    authState.isLoading = true;
    authState.user = null;

    renderApp("/nodes");

    expect(screen.getByText("Loading...")).toBeInTheDocument();
    expect(mountedPages()).toHaveLength(0);
    expect(window.location.pathname).toBe("/nodes");
  });

  /**
   * A session that resolves AFTER the loading frame renders the requested page —
   * i.e. deep links survive the async session check instead of being redirected.
   */
  it("renders the deep-linked page once a pending session check resolves", async () => {
    authState.isLoading = true;
    authState.user = null;
    const { rerender } = renderApp("/storage");
    expect(screen.getByText("Loading...")).toBeInTheDocument();

    signIn("user");
    rerender(<App />);

    expect(await screen.findByTestId("page-storage")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/storage");
  });
});

// ── Catch-all ───────────────────────────────────────────────────────────────

/**
 * `<Route path="*" element={<Navigate to="/" replace />} />`.
 *
 * The destination is `/`, NOT `/login`: an unknown URL for a signed-in user
 * should land on the dashboard, and the gate bounces anonymous visitors onward
 * from there. Redirecting straight to `/login` would log-out-loop authenticated
 * users who mistype a URL.
 */
describe("App — unknown routes (catch-all)", () => {
  it("redirects an unknown path to the dashboard for a signed-in user", async () => {
    signIn();
    renderApp("/totally/made/up/path");

    expect(await screen.findByTestId("page-dashboard")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });

  /**
   * `jobs/:id` matches exactly one segment, so an extra segment is NOT a job
   * detail URL — it falls through to the catch-all rather than rendering
   * JobDetail with a truncated id.
   */
  it("sends an over-long job URL to the catch-all rather than JobDetail", async () => {
    signIn();
    renderApp("/jobs/abc/extra");

    expect(await screen.findByTestId("page-dashboard")).toBeInTheDocument();
    expect(screen.queryByTestId("page-job-detail")).not.toBeInTheDocument();
  });

  /** `/login` has no children either — `/login/reset` is an unknown route. */
  it("sends a sub-path of /login to the catch-all", async () => {
    signIn();
    renderApp("/login/forgot-password");

    expect(await screen.findByTestId("page-dashboard")).toBeInTheDocument();
    expect(screen.queryByTestId("page-login")).not.toBeInTheDocument();
  });

  /**
   * The two redirects compose: unknown URL → `/` → (no session) → `/login`.
   * This is the chain that the "catch-all must not target /login" decision
   * depends on, so it is asserted end to end.
   */
  it("chains through the dashboard to /login when there is no session", async () => {
    signOut();
    renderApp("/nope");

    expect(await screen.findByTestId("page-login")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/login");
  });

  /**
   * The redirect REPLACES the unknown URL instead of pushing over it, so Back
   * does not walk the user straight back into the 404 and forward again.
   * Asserted via history length, which a push would grow.
   */
  it("replaces history rather than pushing when redirecting an unknown path", async () => {
    signIn();
    window.history.pushState({}, "", "/");
    const lengthBefore = window.history.length;

    renderApp("/unknown-page");
    await screen.findByTestId("page-dashboard");

    // renderApp's own pushState adds one entry; the Navigate must not add another.
    expect(window.history.length).toBe(lengthBefore + 1);
  });
});

// ── Role gating of /admin ───────────────────────────────────────────────────

/**
 * The `/admin` route is gated on AUTHENTICATION only — not on role. That is a
 * deliberate decision (authorization is enforced server-side on /api/admin/*),
 * and these tests pin it so a future client-side gate is added knowingly rather
 * than assumed to be already there.
 */
describe("App — role gating of the /admin route", () => {
  it("allows an admin onto /admin", () => {
    signIn("admin");
    renderApp("/admin");
    expect(screen.getByTestId("page-admin")).toBeInTheDocument();
  });

  /** No client-side gate: a plain user reaches the page and sees API errors instead. */
  it("also allows a plain user onto /admin (authorization is server-side)", () => {
    signIn("user");
    renderApp("/admin");
    expect(screen.getByTestId("page-admin")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/admin");
  });

  it("also allows a manager onto /admin", () => {
    signIn("manager");
    renderApp("/admin");
    expect(screen.getByTestId("page-admin")).toBeInTheDocument();
  });

  /** A deactivated-but-authenticated account is likewise not gated client-side. */
  it("also allows an inactive account onto /admin", () => {
    signIn("admin", { is_active: false });
    renderApp("/admin");
    expect(screen.getByTestId("page-admin")).toBeInTheDocument();
  });

  /** The gate that DOES exist: no session means no /admin. */
  it("denies /admin to an anonymous visitor", async () => {
    signOut();
    renderApp("/admin");

    expect(await screen.findByTestId("page-login")).toBeInTheDocument();
    expect(screen.queryByTestId("page-admin")).not.toBeInTheDocument();
  });
});

// ── In-app navigation ───────────────────────────────────────────────────────

/**
 * The routes are also reached by clicking, not just by deep link. These tests
 * use the real sidebar links (rendered by the real `Layout`) to prove the nav
 * `to` targets and the route table agree — a drifted `to` would land on the
 * catch-all and silently show the dashboard.
 */
describe("App — navigating between routes", () => {
  beforeEach(() => signIn("admin"));

  it("swaps the page and updates the URL when a nav link is clicked", async () => {
    const { user } = renderApp("/");
    expect(screen.getByTestId("page-dashboard")).toBeInTheDocument();

    await user.click(within(screen.getByRole("navigation")).getByRole("link", { name: "Nodes" }));

    expect(screen.getByTestId("page-nodes")).toBeInTheDocument();
    expect(screen.queryByTestId("page-dashboard")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/nodes");
  });

  /**
   * Every sidebar link resolves to a real route, so none of them bounce off the
   * catch-all. Regression guarded: a nav entry pointing at a path the table does
   * not declare — which looks like a working link but always shows the dashboard.
   */
  it("resolves every sidebar link to a declared route (no catch-all fallbacks)", async () => {
    const { user } = renderApp("/");
    const expected: Array<[string, string, string]> = [
      ["Nodes", "/nodes", "page-nodes"],
      ["Pools", "/pools", "page-pools"],
      ["Jobs", "/jobs", "page-jobs"],
      ["Job Builder", "/jobs/new", "page-job-builder"],
      ["Storage", "/storage", "page-storage"],
      ["Admin", "/admin", "page-admin"],
      ["Dashboard", "/", "page-dashboard"],
    ];

    for (const [label, path, testid] of expected) {
      await user.click(within(screen.getByRole("navigation")).getByRole("link", { name: label }));
      expect(window.location.pathname).toBe(path);
      expect(screen.getByTestId(testid)).toBeInTheDocument();
    }
  });

  /**
   * The shell persists across navigations: only the outlet changes, so the
   * session check does not re-run. Regression guarded: declaring the pages as
   * siblings of `Layout` (rather than children) would remount the shell — and
   * re-fire /auth/me and the WebSocket — on every click.
   */
  it("keeps the shell mounted across navigations (one session check, one socket)", async () => {
    const { user } = renderApp("/");
    expect(h.fetchUser).toHaveBeenCalledTimes(1);
    const socketCallsAfterMount = h.useWebSocket.mock.calls.length;

    const navRegion = () => within(screen.getByRole("navigation"));
    await user.click(navRegion().getByRole("link", { name: "Jobs" }));
    await user.click(navRegion().getByRole("link", { name: "Storage" }));

    expect(h.fetchUser).toHaveBeenCalledTimes(1);
    expect(h.useWebSocket).toHaveBeenCalledWith(h.handleWsMessage);
    // Re-called on re-render, but always with the same stable handler — and the
    // shell was never remounted, which is what would reopen the socket.
    expect(h.useWebSocket.mock.calls.length).toBeGreaterThanOrEqual(socketCallsAfterMount);
    expect(screen.getByRole("heading", { name: "NEXUS" })).toBeInTheDocument();
  });

  /**
   * Browser Back works because `App` uses `BrowserRouter` (real history), not a
   * memory router. Regression guarded: swapping in `HashRouter`/`MemoryRouter`
   * would break deep links and the Back button for the whole app.
   */
  it("responds to the browser Back button", async () => {
    const { user } = renderApp("/");
    await user.click(within(screen.getByRole("navigation")).getByRole("link", { name: "Pools" }));
    expect(screen.getByTestId("page-pools")).toBeInTheDocument();

    window.history.back();

    await waitFor(() => expect(screen.getByTestId("page-dashboard")).toBeInTheDocument());
    expect(window.location.pathname).toBe("/");
  });

  /**
   * Losing the session mid-visit (a 401 clears `user`) ejects the user from
   * whatever route they were on, rather than leaving a shell whose every request
   * fails.
   */
  it("redirects to /login when the session disappears mid-visit", async () => {
    const { rerender } = renderApp("/jobs");
    expect(screen.getByTestId("page-jobs")).toBeInTheDocument();

    authState.user = null;
    rerender(<App />);

    expect(await screen.findByTestId("page-login")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/login");
    expect(screen.queryByTestId("page-jobs")).not.toBeInTheDocument();
  });
});
