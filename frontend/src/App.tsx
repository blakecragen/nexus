/**
 * Root component and route table for the Nexus dashboard SPA.
 *
 * Role in the system: `main.tsx` renders this; everything the user can navigate
 * to is declared here. There are exactly two top-level branches:
 *
 *  1. `/login` — rendered bare (no `Layout`), because the user has no session
 *     yet and must not see the sidebar/chrome.
 *  2. `/` — rendered inside `@/components/Layout`, which is the auth gate. Layout
 *     fetches the current user, redirects to `/login` when unauthenticated, and
 *     opens the dashboard WebSocket. Every child route below therefore may
 *     assume an authenticated user and a live WS feed.
 *
 * Page modules (`@/pages/*`) each fetch their own data via `@/api/client` and/or
 * read from the zustand stores in `@/stores`.
 *
 * Uses `BrowserRouter` (HTML5 history), so the server hosting the built assets
 * must fall back to `index.html` for unknown paths; in dev the Vite server does
 * this automatically.
 */
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Layout } from "@/components/Layout";
import LoginPage from "@/pages/Login";
import Dashboard from "@/pages/Dashboard";
import Nodes from "@/pages/Nodes";
import Pools from "@/pages/Pools";
import Jobs from "@/pages/Jobs";
import JobBuilder from "@/pages/JobBuilder";
import JobDetail from "@/pages/JobDetail";
import Storage from "@/pages/Storage";
import Admin from "@/pages/Admin";

/**
 * App — declares the router and the full route table.
 *
 * Props: none. State: none (all state lives in `@/stores` or in each page).
 *
 * Routes:
 * - `/login`            → Login form (unauthenticated, no chrome).
 * - `/`                 → Layout shell; renders child routes into its `<Outlet />`.
 *   - index             → Dashboard (cluster overview: node/job counts, activity).
 *   - `nodes`           → Node list + provisioning / maintenance actions.
 *   - `pools`           → Pool CRUD and pool membership editing.
 *   - `jobs`            → Job list with status filters.
 *   - `jobs/new`        → JobBuilder (drag-and-drop step composer, submits a job).
 *   - `jobs/:id`        → JobDetail (per-step status, live logs, results tree).
 *   - `storage`         → Storage backends, health checks, transfers.
 *   - `admin`           → User + credential administration.
 * - `*`                 → Redirect to `/`.
 *
 * AI Note: route ORDER matters here. The catch-all `*` must stay last, and it
 * redirects to `/` rather than to `/login` — an unknown URL for a logged-in user
 * should land on the dashboard, and Layout will bounce them to `/login` if they
 * are not authenticated. Redirecting straight to `/login` here would log-out-loop
 * authenticated users who mistype a URL.
 *
 * AI Note: `jobs/new` is declared before `jobs/:id`, but react-router v6 ranks
 * static segments above dynamic ones regardless of order, so "new" is never
 * swallowed as a job id. Do not "fix" this by reordering — the ranking is what
 * guarantees it.
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="nodes" element={<Nodes />} />
          <Route path="pools" element={<Pools />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="jobs/new" element={<JobBuilder />} />
          <Route path="jobs/:id" element={<JobDetail />} />
          <Route path="storage" element={<Storage />} />
          <Route path="admin" element={<Admin />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
