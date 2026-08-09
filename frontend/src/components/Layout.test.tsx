/**
 * Tests for the authenticated app shell (src/components/Layout.tsx).
 *
 * `Layout` is the parent route of every signed-in page, so it owns three
 * cross-cutting concerns that this file covers one at a time:
 *
 *  1. Auth gate      — GET /api/auth/me on mount via `useAuthStore.fetchUser`,
 *                      a "Loading..." screen until it answers, and a
 *                      `replace` redirect to /login when there is no session.
 *  2. Live data feed — a single `useWebSocket(handleWsMessage)` subscription
 *                      whose handler identity must stay stable (an unstable
 *                      handler re-dials the dashboard socket every render).
 *  3. Chrome         — the sidebar (NEXUS wordmark, seven nav links with
 *                      active-route highlighting, username/role footer, logout
 *                      button) plus the `<main>` region the child route renders
 *                      into through `<Outlet />`.
 *
 * What is real vs stubbed: the component, react-router's real `NavLink`
 * matching (which is what `aria-current` and the active classes come from) and
 * the real `cn` helper all run for real. Only `@/stores` (the auth state + WS
 * dispatcher) and `@/hooks/useWebSocket` are replaced, so no test here opens a
 * socket or touches the network.
 *
 * Neighbouring pieces: the route table that mounts this component lives in
 * `@/App` (see App.test.tsx), the auth actions it calls are covered by
 * src/stores/index.test.ts, and the socket it opens is covered by
 * src/hooks/useWebSocket.test.tsx.
 */
import { vi, describe, it, expect, beforeEach } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  MemoryRouter,
  Routes,
  Route,
  useLocation,
  useNavigationType,
} from "react-router-dom";
import type { ReactElement } from "react";
import type { UserInfo } from "@/types";
import { makeUser } from "../test/test-utils";

// ── Mock the two boundaries Layout depends on ────────────────────────────────
// `useAuthStore` is consumed as a bare destructuring call
// (`const {user, isLoading, fetchUser, logout} = useAuthStore()`), but the mock
// also honours the selector form so it behaves like zustand for any future
// caller. `useWebSocket` is stubbed to a spy so we can assert *what* Layout
// subscribes with without ever constructing a real WebSocket.
//
// AI Note: this must live in `vi.hoisted` — `vi.mock` factories are hoisted
// above every `const`, so a factory closing over a normally-declared binding
// throws "Cannot access 'x' before initialization" at import time.
const h = vi.hoisted(() => {
  const fetchUser = vi.fn().mockResolvedValue(undefined);
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
  /** Stand-in for the module-level WS dispatcher; identity is what matters. */
  const handleWsMessage = vi.fn();
  const useWebSocket = vi.fn(() => ({ current: null }));
  return { fetchUser, logout, authState, useAuthStore, handleWsMessage, useWebSocket };
});

vi.mock("@/stores", () => ({
  useAuthStore: h.useAuthStore,
  handleWsMessage: h.handleWsMessage,
}));

vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: h.useWebSocket }));

import { Layout } from "./Layout";

/** Narrowed alias so tests can seed a typed user into the hoisted state. */
const authState = h.authState as {
  user: UserInfo | null;
  isLoading: boolean;
  fetchUser: typeof h.fetchUser;
  logout: typeof h.logout;
};

beforeEach(() => {
  h.fetchUser.mockClear();
  h.fetchUser.mockResolvedValue(undefined);
  h.logout.mockClear();
  h.useWebSocket.mockClear();
  h.useAuthStore.mockClear();
  // Default: the pre-`/auth/me` state the store really starts in.
  authState.user = null;
  authState.isLoading = true;
  authState.fetchUser = h.fetchUser;
  authState.logout = h.logout;
});

// ── Harness ──────────────────────────────────────────────────────────────────

/**
 * Probe standing in for the (unauthenticated) login route.
 *
 * It echoes the router's navigation TYPE as well as the path, which is how the
 * `replace: true` contract is asserted without reaching into history internals:
 * a push would report "PUSH", so "REPLACE" proves Back cannot return to the
 * authenticated view.
 */
function LoginProbe() {
  const navType = useNavigationType();
  const location = useLocation();
  return (
    <div data-testid="login-probe">{`login:${navType}:${location.pathname}`}</div>
  );
}

/**
 * The route tree used by every test: `Layout` as the parent of a handful of
 * trivial outlet probes, plus a sibling `/login` route so redirects land
 * somewhere observable.
 *
 * Real child pages are deliberately not used — they would drag in their own
 * stores, fetches and WebSocket subscriptions and turn shell tests into
 * page tests.
 */
function tree(route: string): ReactElement {
  return (
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<div data-testid="outlet">outlet:index</div>} />
          <Route path="nodes" element={<div data-testid="outlet">outlet:nodes</div>} />
          <Route path="jobs" element={<div data-testid="outlet">outlet:jobs</div>} />
          <Route path="jobs/new" element={<div data-testid="outlet">outlet:jobs-new</div>} />
          <Route path="jobs/:id" element={<div data-testid="outlet">outlet:job-detail</div>} />
          <Route path="admin" element={<div data-testid="outlet">outlet:admin</div>} />
        </Route>
        <Route path="/login" element={<LoginProbe />} />
      </Routes>
    </MemoryRouter>
  );
}

/** Render the shell at `route`; `rerenderLayout()` re-renders without resetting the router. */
function renderLayout(route = "/") {
  const user = userEvent.setup();
  const view = render(tree(route));
  return { user, rerenderLayout: () => view.rerender(tree(route)), ...view };
}

/** The sidebar `<nav>`; every nav-link assertion is scoped to it. */
const nav = () => screen.getByRole("navigation");
/** One nav link by its visible label (labels double as the accessible name). */
const navLink = (label: string | RegExp) =>
  within(nav()).getByRole("link", { name: label });

// ── Loading gate ─────────────────────────────────────────────────────────────

/**
 * The pre-`/auth/me` frame. `isLoading` starts `true` in the real store, so
 * this is what every page load renders first.
 */
describe("Layout — loading gate", () => {
  /**
   * While the session check is outstanding the shell must show a neutral
   * loading screen. Regression guarded: rendering the sidebar (or worse, the
   * child route) before we know who the user is, which fires authenticated
   * requests on behalf of a possibly-anonymous visitor.
   */
  it("renders a Loading screen and no chrome while the session check is pending", () => {
    authState.isLoading = true;
    authState.user = null;

    renderLayout("/");

    expect(screen.getByText("Loading...")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "NEXUS" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("outlet")).not.toBeInTheDocument();
  });

  /**
   * The redirect effect is gated on `!isLoading`. Regression guarded: dropping
   * that guard bounces EVERY page load to /login before /auth/me answers, so a
   * signed-in user who refreshes is logged out.
   */
  it("does not redirect to /login while the session check is still pending", () => {
    authState.isLoading = true;
    authState.user = null;

    renderLayout("/nodes");

    expect(screen.queryByTestId("login-probe")).not.toBeInTheDocument();
    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });

  /**
   * One GET /api/auth/me per mount — no more. Regression guarded: an unstable
   * `fetchUser` dependency turning the session check into a request loop.
   */
  it("calls fetchUser exactly once on mount", () => {
    renderLayout("/");
    expect(h.fetchUser).toHaveBeenCalledTimes(1);
  });

  /**
   * Re-renders must not re-check the session. `fetchUser` is a stable zustand
   * action, and the effect lists it as its only dependency.
   */
  it("does not call fetchUser again on a re-render", () => {
    const { rerenderLayout } = renderLayout("/");
    expect(h.fetchUser).toHaveBeenCalledTimes(1);

    rerenderLayout();
    rerenderLayout();

    expect(h.fetchUser).toHaveBeenCalledTimes(1);
  });

  /**
   * The loading screen resolves into the real shell once the store reports a
   * user. Pins the whole gate transition, not just its endpoints.
   */
  it("swaps the loading screen for the sidebar once the user resolves", () => {
    authState.isLoading = true;
    const { rerenderLayout } = renderLayout("/");
    expect(screen.getByText("Loading...")).toBeInTheDocument();

    authState.isLoading = false;
    authState.user = makeUser({ username: "alice" });
    rerenderLayout();

    expect(screen.queryByText("Loading...")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "NEXUS" })).toBeInTheDocument();
    expect(screen.getByTestId("outlet")).toHaveTextContent("outlet:index");
  });
});

// ── Unauthenticated redirect ────────────────────────────────────────────────

/** The gate's negative branch: no session -> nothing rendered, bounce to /login. */
describe("Layout — unauthenticated redirect", () => {
  /**
   * With the check finished and no user, the shell must navigate to /login and
   * render nothing in the meantime. Regression guarded: mounting the child
   * route for an anonymous visitor, whose requests all 401.
   */
  it("redirects to /login and renders no chrome when there is no user", async () => {
    authState.isLoading = false;
    authState.user = null;

    renderLayout("/nodes");

    expect(await screen.findByTestId("login-probe")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.queryByTestId("outlet")).not.toBeInTheDocument();
  });

  /**
   * The redirect uses `replace: true`.
   *
   * Regression guarded (security-relevant): a push would leave the
   * authenticated URL in history, so Back re-enters the shell — which briefly
   * renders it and fires authenticated requests before bouncing out again.
   */
  it("replaces history rather than pushing when redirecting to /login", async () => {
    authState.isLoading = false;
    authState.user = null;

    renderLayout("/admin");

    const probe = await screen.findByTestId("login-probe");
    expect(probe).toHaveTextContent("login:REPLACE:/login");
  });

  /**
   * Session expiry mid-session (a 401 clears `user`) must eject the user from
   * the shell immediately rather than leaving a dead UI whose every action
   * fails.
   */
  it("redirects when the user disappears after the shell has already rendered", async () => {
    authState.isLoading = false;
    authState.user = makeUser({ username: "alice" });
    const { rerenderLayout } = renderLayout("/jobs");
    expect(screen.getByRole("navigation")).toBeInTheDocument();

    // Session expires: the store clears `user`.
    authState.user = null;
    rerenderLayout();

    expect(await screen.findByTestId("login-probe")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
});

// ── Sidebar chrome ──────────────────────────────────────────────────────────

/** The signed-in shell: wordmark, nav list, user footer, outlet region. */
describe("Layout — sidebar chrome", () => {
  beforeEach(() => {
    authState.isLoading = false;
    authState.user = makeUser({ username: "alice", role: "admin" });
  });

  /** The product wordmark is a real heading, not decorative text. */
  it("renders the NEXUS wordmark as a heading", () => {
    renderLayout("/");
    expect(screen.getByRole("heading", { name: "NEXUS" })).toBeInTheDocument();
  });

  /**
   * The full nav contract: seven items, in declaration order, each pointing at
   * a route that `@/App` actually declares. Regression guarded: a nav entry
   * whose `to` has drifted from the route table, producing a link that
   * silently redirects to the dashboard via the catch-all.
   */
  it("renders every nav item once, in order, with the right href", () => {
    renderLayout("/");

    const links = within(nav()).getAllByRole("link");
    expect(links).toHaveLength(7);
    expect(links.map((a) => a.textContent)).toEqual([
      "Dashboard",
      "Nodes",
      "Pools",
      "Jobs",
      "Job Builder",
      "Storage",
      "Admin",
    ]);
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/",
      "/nodes",
      "/pools",
      "/jobs",
      "/jobs/new",
      "/storage",
      "/admin",
    ]);
  });

  /**
   * Each nav entry renders its lucide icon. Regression guarded: the
   * `icon: Icon` rename in the `.map` being lost, which makes React emit a
   * literal `<icon>` HTML element and drops the whole icon column.
   */
  it("renders an icon inside every nav link", () => {
    renderLayout("/");
    for (const link of within(nav()).getAllByRole("link")) {
      expect(link.querySelector("svg")).not.toBeNull();
    }
  });

  /** The signed-in identity is visible so operators know which account they are acting as. */
  it("shows the current username and role in the sidebar footer", () => {
    authState.user = makeUser({ username: "carol", role: "manager" });
    renderLayout("/");

    expect(screen.getByText("carol")).toBeInTheDocument();
    // The role is rendered verbatim; only CSS (`capitalize`) titlecases it.
    expect(screen.getByText("manager")).toBeInTheDocument();
  });

  /**
   * A long username must not be dropped from the DOM by the truncation styling
   * — it is clipped visually but remains present (and thus copyable/testable).
   */
  it("keeps a very long username in the DOM (truncation is visual only)", () => {
    const longName = "a-really-long-service-account-name-that-overflows";
    authState.user = makeUser({ username: longName });
    renderLayout("/");
    expect(screen.getByText(longName)).toBeInTheDocument();
  });

  /** The logout control is a button (keyboard reachable) labelled by its title. */
  it("renders a labelled logout button", () => {
    renderLayout("/");
    expect(screen.getByRole("button", { name: /log out/i })).toBeInTheDocument();
  });

  /**
   * The child route renders inside `<main>`, not in the sidebar. Regression
   * guarded: an `<Outlet />` moved into the `<aside>` would put page content
   * inside the 256px fixed rail.
   */
  it("renders the child route into the main region via Outlet", () => {
    renderLayout("/nodes");
    const main = screen.getByRole("main");
    expect(within(main).getByTestId("outlet")).toHaveTextContent("outlet:nodes");
  });

  /**
   * The shell survives route changes: navigating swaps only the outlet.
   * Regression guarded: a remount would re-run `fetchUser` and re-dial the
   * WebSocket on every navigation.
   */
  it("keeps the same shell mounted when the child route changes", async () => {
    const { user } = renderLayout("/");
    expect(screen.getByTestId("outlet")).toHaveTextContent("outlet:index");

    await user.click(navLink("Nodes"));

    expect(screen.getByTestId("outlet")).toHaveTextContent("outlet:nodes");
    // Still one session check and one socket subscription-worth of mounts.
    expect(h.fetchUser).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("heading", { name: "NEXUS" })).toBeInTheDocument();
  });
});

// ── Active-route highlighting ───────────────────────────────────────────────

/**
 * `NavLink` active state. The visible signal is a Tailwind class pair, and the
 * accessible signal is `aria-current="page"`; both are asserted because screen
 * readers rely on the latter.
 */
describe("Layout — active route highlighting", () => {
  beforeEach(() => {
    authState.isLoading = false;
    authState.user = makeUser();
  });

  /** On the dashboard, exactly the Dashboard entry is marked current. */
  it("marks only the Dashboard entry active on the index route", () => {
    renderLayout("/");

    expect(navLink("Dashboard")).toHaveAttribute("aria-current", "page");
    for (const label of ["Nodes", "Pools", "Jobs", "Job Builder", "Storage", "Admin"]) {
      expect(navLink(label)).not.toHaveAttribute("aria-current");
    }
  });

  /**
   * The `end: true` flag on the Dashboard entry.
   *
   * Regression guarded: without it, NavLink prefix-matches "/" against every
   * path, so Dashboard would render highlighted on all seven pages and the nav
   * would stop telling the user where they are.
   */
  it("does NOT mark Dashboard active on a non-index route (end:true)", () => {
    renderLayout("/nodes");

    expect(navLink("Dashboard")).not.toHaveAttribute("aria-current");
    expect(navLink("Nodes")).toHaveAttribute("aria-current", "page");
  });

  /**
   * The other entries deliberately omit `end`, so a section stays highlighted
   * on its detail pages. Regression guarded: adding `end` everywhere would
   * un-highlight "Jobs" as soon as the user opens a job.
   */
  it("keeps Jobs active on a job detail route (prefix matching)", () => {
    renderLayout("/jobs/00000000-0000-0000-0000-000000000001");

    expect(navLink("Jobs")).toHaveAttribute("aria-current", "page");
    expect(navLink("Job Builder")).not.toHaveAttribute("aria-current");
    expect(navLink("Dashboard")).not.toHaveAttribute("aria-current");
  });

  /**
   * Documents ACTUAL behaviour on /jobs/new: because the "Jobs" entry has no
   * `end`, "/jobs" prefix-matches and BOTH "Jobs" and "Job Builder" light up.
   *
   * This is a (minor, cosmetic) source defect — two nav items claim to be the
   * current page and two elements carry `aria-current="page"`, which screen
   * readers announce twice. Pinned here so the fix (an `end` on the Jobs entry,
   * or an explicit `isActive` override) is a deliberate, visible change.
   */
  it("marks BOTH Jobs and Job Builder active on /jobs/new (current behaviour)", () => {
    renderLayout("/jobs/new");

    expect(navLink("Job Builder")).toHaveAttribute("aria-current", "page");
    expect(navLink("Jobs")).toHaveAttribute("aria-current", "page");
    expect(
      within(nav())
        .getAllByRole("link")
        .filter((a) => a.getAttribute("aria-current") === "page")
    ).toHaveLength(2);
  });

  /**
   * The visual half of the signal. Asserted loosely (by the semantic class
   * name, not the full string) so Tailwind tweaks do not break the test while a
   * genuine mis-mapping still does.
   */
  it("applies the active class set to the current entry and the idle set to the rest", () => {
    renderLayout("/admin");

    const active = navLink("Admin");
    expect(active.className).toMatch(/text-sidebar-active/);
    expect(active.className).toMatch(/bg-primary\/15/);

    const idle = navLink("Storage");
    expect(idle.className).not.toMatch(/text-sidebar-active/);
    expect(idle.className).toMatch(/hover:bg-primary\/10/);
  });

  /** Highlighting follows in-app navigation, not just the initial URL. */
  it("moves the highlight when the user clicks a different nav item", async () => {
    const { user } = renderLayout("/");
    expect(navLink("Dashboard")).toHaveAttribute("aria-current", "page");

    await user.click(navLink("Jobs"));

    expect(navLink("Jobs")).toHaveAttribute("aria-current", "page");
    expect(navLink("Dashboard")).not.toHaveAttribute("aria-current");
  });
});

// ── Role gating (or the deliberate absence of it) ───────────────────────────

/**
 * The nav list is intentionally NOT role-filtered: authorisation is enforced
 * server-side on the /api/admin/* endpoints. These tests pin that decision so a
 * future "hide Admin for non-admins" change is made knowingly (and so nobody
 * mistakes the visible link for a client-side permission grant).
 */
describe("Layout — role gating of nav items", () => {
  beforeEach(() => {
    authState.isLoading = false;
  });

  it("shows the Admin link to a plain user (authorization is server-side)", () => {
    authState.user = makeUser({ username: "bob", role: "user" });
    renderLayout("/");

    expect(navLink("Admin")).toHaveAttribute("href", "/admin");
    expect(within(nav()).getAllByRole("link")).toHaveLength(7);
  });

  it("shows the Admin link to a manager", () => {
    authState.user = makeUser({ username: "mia", role: "manager" });
    renderLayout("/");

    expect(navLink("Admin")).toBeInTheDocument();
    expect(within(nav()).getAllByRole("link")).toHaveLength(7);
  });

  /** The nav is identical for every role — no hidden entries appear for admins. */
  it("renders the identical nav list for user, manager and admin", () => {
    const labelsFor = (role: UserInfo["role"]) => {
      authState.user = makeUser({ role });
      const view = render(tree("/"));
      const labels = within(screen.getByRole("navigation"))
        .getAllByRole("link")
        .map((a) => a.textContent);
      view.unmount();
      return labels;
    };

    const asUser = labelsFor("user");
    expect(labelsFor("manager")).toEqual(asUser);
    expect(labelsFor("admin")).toEqual(asUser);
  });

  /**
   * A deactivated account that still holds a valid token renders the normal
   * shell — the client draws no distinction. Documents that `is_active` is a
   * server-side concern, so a regression on the server would not be masked by
   * client-side chrome changes.
   */
  it("renders the normal shell for an inactive user (no client-side gate)", () => {
    authState.user = makeUser({ username: "ghost", is_active: false });
    renderLayout("/");

    expect(screen.getByText("ghost")).toBeInTheDocument();
    expect(within(nav()).getAllByRole("link")).toHaveLength(7);
  });
});

// ── Logout ──────────────────────────────────────────────────────────────────

/** The logout button: clear the session, then leave the shell. */
describe("Layout — logout", () => {
  beforeEach(() => {
    authState.isLoading = false;
    authState.user = makeUser({ username: "alice" });
  });

  /** One click, one `logout()` call, and the user ends up on /login. */
  it("calls logout and navigates to /login when the logout button is clicked", async () => {
    const { user } = renderLayout("/jobs");

    await user.click(screen.getByRole("button", { name: /log out/i }));

    expect(h.logout).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("login-probe")).toBeInTheDocument();
  });

  /**
   * The post-logout navigation also uses `replace: true`.
   *
   * Regression guarded (security-relevant): a push leaves the authenticated URL
   * one Back press away, so a shared machine can be walked back into the
   * dashboard shell after a logout.
   */
  it("replaces history on logout so Back cannot re-enter the shell", async () => {
    const { user } = renderLayout("/admin");

    await user.click(screen.getByRole("button", { name: /log out/i }));

    expect(await screen.findByTestId("login-probe")).toHaveTextContent(
      "login:REPLACE:/login"
    );
  });

  /**
   * Ordering: `logout()` must run BEFORE the navigation, so the shell unmounts
   * with the token already cleared. Regression guarded: navigating first leaves
   * an in-flight render able to fire an authenticated request with a token that
   * is about to be discarded.
   */
  it("clears the session before navigating away (ordering)", async () => {
    /** Records whether the shell was still on-screen when logout() ran. */
    let shellStillMountedAtLogout: boolean | null = null;
    h.logout.mockImplementation(() => {
      shellStillMountedAtLogout = screen.queryByRole("navigation") !== null;
    });

    const { user } = renderLayout("/");
    await user.click(screen.getByRole("button", { name: /log out/i }));

    expect(shellStillMountedAtLogout).toBe(true);
    await waitFor(() =>
      expect(screen.queryByRole("navigation")).not.toBeInTheDocument()
    );
  });

  /** The shell (and the child route) are gone after logging out. */
  it("tears down the sidebar and outlet after logging out", async () => {
    const { user } = renderLayout("/nodes");
    expect(screen.getByTestId("outlet")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /log out/i }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "NEXUS" })).not.toBeInTheDocument()
    );
    expect(screen.queryByTestId("outlet")).not.toBeInTheDocument();
  });

  /** Logout is not a data refresh: it must not re-run the session check. */
  it("does not re-run fetchUser when logging out", async () => {
    const { user } = renderLayout("/");
    expect(h.fetchUser).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /log out/i }));

    expect(h.fetchUser).toHaveBeenCalledTimes(1);
  });
});

// ── Live connection (WebSocket subscription) ────────────────────────────────

/**
 * Layout owns the single `/ws/dashboard` subscription for the whole session.
 * These tests pin *how* it subscribes; the socket's own behaviour (backoff,
 * parsing, teardown) is covered by src/hooks/useWebSocket.test.tsx.
 */
describe("Layout — live connection", () => {
  /**
   * The handler must be the module-level `handleWsMessage`, by reference.
   *
   * Regression guarded: passing an inline arrow (or a locally-defined function)
   * changes identity every render, and `useWebSocket` memoizes `connect` on
   * that identity — so the dashboard socket would be torn down and re-dialled
   * on every single re-render.
   */
  it("subscribes with the module-level handleWsMessage dispatcher", () => {
    authState.isLoading = false;
    authState.user = makeUser();

    renderLayout("/");

    expect(h.useWebSocket).toHaveBeenCalled();
    expect(h.useWebSocket).toHaveBeenCalledWith(h.handleWsMessage);
  });

  /** The same reference on every render — the stability half of the contract. */
  it("passes an identical handler reference across re-renders", () => {
    authState.isLoading = false;
    authState.user = makeUser();
    const { rerenderLayout } = renderLayout("/");

    rerenderLayout();
    rerenderLayout();

    const handlers = h.useWebSocket.mock.calls.map((c) => c[0]);
    expect(handlers.length).toBeGreaterThan(1);
    for (const handler of handlers) {
      expect(handler).toBe(h.handleWsMessage);
    }
  });

  /**
   * `useWebSocket` is called above the early returns, so the socket opens even
   * during the loading frame. Regression guarded: moving the call below the
   * `if (isLoading)` return breaks the rules of hooks and crashes the shell the
   * moment the user resolves.
   */
  it("opens the socket even while the session check is still loading", () => {
    authState.isLoading = true;
    authState.user = null;

    renderLayout("/");

    expect(screen.getByText("Loading...")).toBeInTheDocument();
    expect(h.useWebSocket).toHaveBeenCalledWith(h.handleWsMessage);
  });

  /** ...and also on the unauthenticated frame, for the same rules-of-hooks reason. */
  it("opens the socket even when there is no authenticated user", () => {
    authState.isLoading = false;
    authState.user = null;

    renderLayout("/");

    expect(h.useWebSocket).toHaveBeenCalledWith(h.handleWsMessage);
  });

  /**
   * Documents a real gap: the shell surfaces NO connection-status indicator, so
   * a dropped `/ws/dashboard` socket (which `useWebSocket` retries silently
   * forever) is invisible — stale node/job badges look like live ones.
   *
   * Pinned as current behaviour: if an indicator is ever added, this test fails
   * and should be replaced with real assertions on it.
   */
  it("renders no connection-status indicator in the sidebar (known gap)", () => {
    authState.isLoading = false;
    authState.user = makeUser();
    renderLayout("/");

    const sidebar = screen.getByRole("complementary");
    expect(
      within(sidebar).queryByText(/connect|disconnect|reconnect|offline|live/i)
    ).not.toBeInTheDocument();
  });
});
