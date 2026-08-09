/**
 * Application shell + authentication gate for every signed-in page.
 *
 * Role in the system: `@/App` mounts this at `/` as the parent of all
 * authenticated routes, so it is the one component guaranteed to be alive for
 * the whole session. That makes it the right home for three cross-cutting
 * concerns:
 *
 *  1. Auth gate — fetches the current user via `useAuthStore.fetchUser()`
 *     (GET /api/auth/me) and redirects to `/login` if there is no session.
 *  2. Live data feed — opens the single `/ws/dashboard` WebSocket via
 *     `useWebSocket` and pipes every message into `handleWsMessage`, which
 *     fans updates out to the nodes/jobs/live-log stores. Because the socket
 *     lives here and not in individual pages, node and job status stay fresh
 *     no matter which page is open, and navigating does not churn the socket.
 *  3. Chrome — the fixed sidebar (logo, nav, current user, logout) plus the
 *     scrollable `<Outlet />` region the child route renders into.
 *
 * Neighbours: `@/stores` (auth state + WS dispatcher), `@/hooks/useWebSocket`,
 * `@/lib/utils` (`cn`), lucide-react icons, react-router.
 */
import { useEffect } from "react";
import { Outlet, NavLink, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  Server,
  Layers,
  Play,
  PlusCircle,
  HardDrive,
  Shield,
  LogOut,
} from "lucide-react";
import { useAuthStore, handleWsMessage } from "@/stores";
import { useWebSocket } from "@/hooks/useWebSocket";
import { cn } from "@/lib/utils";

/**
 * Sidebar navigation entries, rendered in this order.
 *
 * Each item is `{to, icon, label, end?}` and must correspond to a route
 * declared in `@/App` — adding an entry here does NOT create a route.
 *
 * AI Note: `end: true` on the Dashboard entry is required. Without it,
 * NavLink's prefix matching would mark "/" active on every page, since every
 * path starts with "/". The other entries deliberately omit `end`, so e.g.
 * "/jobs" stays highlighted while viewing "/jobs/:id".
 *
 * AI Note: this list is not role-filtered — the "Admin" link is shown to every
 * signed-in user. Authorization is enforced server-side on the admin endpoints,
 * so a non-admin who clicks through sees errors rather than a blocked route.
 */
const navItems = [
  { to: "/", icon: LayoutDashboard, label: "Dashboard", end: true },
  { to: "/nodes", icon: Server, label: "Nodes" },
  { to: "/pools", icon: Layers, label: "Pools" },
  { to: "/jobs", icon: Play, label: "Jobs" },
  { to: "/jobs/new", icon: PlusCircle, label: "Job Builder" },
  { to: "/storage", icon: HardDrive, label: "Storage" },
  { to: "/admin", icon: Shield, label: "Admin" },
];

/**
 * Layout — the authenticated shell.
 *
 * Props: none (it is a react-router layout route; children arrive via `<Outlet />`).
 *
 * State read from `useAuthStore`: `user`, `isLoading`, plus the `fetchUser` and
 * `logout` actions.
 *
 * Render states, in order:
 * - `isLoading` (initial, before /auth/me resolves) → a centred "Loading..." screen.
 *   This gate is what stops a logged-in user from flashing the login page on refresh.
 * - no `user` → renders `null` while the redirect effect navigates to `/login`.
 * - otherwise → sidebar + `<Outlet />`.
 *
 * What the user sees: a fixed 256px dark sidebar with the NEXUS wordmark, the
 * nav list (active item highlighted), and a footer showing their username and
 * role with a logout icon-button. The main region scrolls independently.
 *
 * Interactions: clicking a nav item routes without a reload; clicking logout
 * clears tokens (`useAuthStore.logout`) and replaces history with `/login` so
 * Back cannot return to the authenticated view.
 *
 * Side effects: one GET /api/auth/me on mount, and one open WebSocket to
 * `/ws/dashboard` for the component's lifetime.
 */
export function Layout() {
  const { user, isLoading, fetchUser, logout } = useAuthStore();
  const navigate = useNavigate();

  // Connect WebSocket for live updates
  //
  // AI Note: `handleWsMessage` is a stable module-level function, which matters:
  // `useWebSocket` re-creates its socket whenever the handler identity changes,
  // so passing an inline arrow here would tear down and reopen the socket on
  // every Layout re-render.
  useWebSocket(handleWsMessage);

  useEffect(() => {
    fetchUser();
  }, [fetchUser]);

  // AI Note: the redirect is a separate effect gated on `!isLoading` so it only
  // fires once /auth/me has actually answered. Dropping the isLoading guard
  // would bounce every page load to /login before the session check completes.
  useEffect(() => {
    if (!isLoading && !user) {
      navigate("/login", { replace: true });
    }
  }, [isLoading, user, navigate]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-screen bg-background">
        <div className="text-muted-foreground text-lg">Loading...</div>
      </div>
    );
  }

  // AI Note: render nothing (rather than the shell or a redirect element) for
  // the one frame between "no user" and the effect above navigating away. This
  // avoids mounting child routes that would immediately fire authenticated
  // requests and trip the 401 handler in @/api/client.
  if (!user) {
    return null;
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside className="w-64 bg-sidebar text-sidebar-foreground flex flex-col shrink-0 border-r border-border">
        {/* Logo */}
        <div className="px-6 py-5 border-b border-border">
          <h1 className="text-xl font-bold tracking-widest text-primary">NEXUS</h1>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {/* AI Note: `icon: Icon` renames the lowercase field to a capitalised
              local so JSX treats it as a component and not an HTML tag. */}
          {navItems.map(({ to, icon: Icon, label, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 px-3 py-2.5 rounded-md text-sm font-medium transition-colors",
                  isActive
                    ? "bg-primary/15 text-sidebar-active"
                    : "text-sidebar-foreground hover:bg-primary/10 hover:text-foreground"
                )
              }
            >
              <Icon className="h-4 w-4 shrink-0" />
              {label}
            </NavLink>
          ))}
        </nav>

        {/* User info + logout */}
        <div className="px-4 py-4 border-t border-border">
          <div className="flex items-center justify-between">
            <div className="min-w-0">
              <p className="text-sm font-medium truncate text-foreground">{user.username}</p>
              <p className="text-xs text-muted-foreground capitalize">{user.role}</p>
            </div>
            <button
              onClick={() => {
                // AI Note: navigate AFTER logout so the Layout unmounts with an
                // already-cleared token; otherwise the in-flight render could
                // fire an authenticated request with a token we just discarded.
                logout();
                navigate("/login", { replace: true });
              }}
              className="p-2 rounded-md text-muted-foreground hover:text-foreground hover:bg-primary/10 transition-colors"
              title="Log out"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 bg-background overflow-y-auto p-6">
        <Outlet />
      </main>
    </div>
  );
}
