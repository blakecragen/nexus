/**
 * Dashboard.tsx — the cluster "at a glance" landing page (route `/`).
 *
 * Role in the system:
 *   Index route inside the `<Layout />` shell (see `App.tsx`). It is the first
 *   authenticated screen a user sees and is deliberately read-only: it fans out
 *   four list requests, renders four counters and the 10 newest jobs, and links
 *   into the detail pages. It owns no mutations.
 *
 * Data sources (all Zustand stores in `frontend/src/stores/index.ts`, each
 * backed by `frontend/src/api/client.ts`):
 *   - `useNodesStore.fetch()`   -> GET /api/nodes
 *   - `useJobsStore.fetch()`    -> GET /api/jobs
 *   - `usePoolsStore.fetch()`   -> GET /api/pools
 *   - `useStorageStore.fetch()` -> GET /api/storage/backends
 *
 * Live updates: this page does not open its own socket. `<Layout />` runs
 * `useWebSocket(handleWsMessage)`, and `handleWsMessage` patches
 * `useNodesStore`/`useJobsStore` in place, so the "Nodes Online" and
 * "Active Jobs" counters re-derive automatically as `node.status` /
 * `job.status` frames arrive from `/ws/dashboard`.
 *
 * Neighbours: navigates to `JobDetail.tsx` (`/jobs/:id`) on row click.
 */
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Server, Play, Layers, HardDrive } from "lucide-react";
import { useNodesStore, useJobsStore, usePoolsStore, useStorageStore } from "@/stores";
import { cn, formatRelativeTime } from "@/lib/utils";
import type { JobStatus } from "@/types";

/**
 * Tailwind class pairs for the job-status pill, keyed by the `JobStatus` union
 * from `@/types` (which mirrors the server-side `JobStatus` enum).
 *
 * AI Note: `pending` and `queued` intentionally share the same yellow — the
 * server distinguishes "accepted but not yet scheduled" from "assigned to a
 * node and waiting", but the dashboard treats both as "not started yet".
 * Adding a status to the backend enum without adding a key here silently falls
 * back to the neutral `bg-secondary` style at the call site rather than
 * crashing, so keep this map in sync when the enum grows.
 */
const statusColors: Record<JobStatus, string> = {
  completed: "bg-green-100 text-green-800",
  running: "bg-blue-100 text-blue-800",
  failed: "bg-red-100 text-red-800",
  pending: "bg-yellow-100 text-yellow-800",
  queued: "bg-yellow-100 text-yellow-800",
  cancelled: "bg-secondary text-muted-foreground",
};

/** Props for {@link StatCard}. `icon` is a lucide-react icon component (not an
 * element), so the card controls its own sizing via `className`. */
interface StatCardProps {
  label: string;
  value: number;
  icon: React.ComponentType<{ className?: string }>;
}

/**
 * One of the four summary tiles in the top grid.
 *
 * What the user sees: a bordered card with a muted caption, a large number,
 * and a tinted square holding the icon on the right.
 *
 * Pure presentational component — no state, no fetching, no click handling.
 * All values are computed by {@link Dashboard} and passed down.
 */
function StatCard({ label, value, icon: Icon }: StatCardProps) {
  return (
    <div className="bg-card border border-border rounded-xl shadow-sm p-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-muted-foreground">{label}</p>
          <p className="text-3xl font-bold text-foreground mt-1">{value}</p>
        </div>
        <div className="p-3 bg-secondary rounded-lg">
          <Icon className="h-6 w-6 text-secondary-foreground" />
        </div>
      </div>
    </div>
  );
}

/**
 * Cluster overview page.
 *
 * What the user sees:
 *   1. Four stat cards — Nodes Online, Active Jobs, Total Pools, Storage
 *      Backends.
 *   2. A "Recent Jobs" table (name / status pill / priority / relative created
 *      time). Clicking any row navigates to `/jobs/:id`.
 *
 * Side effects: on mount it fires all four store `fetch()` calls in parallel
 * (four independent GETs; no ordering guarantee, each store flips its own
 * `isLoading`). This page renders zeros rather than a spinner while they land.
 *
 * Props: none — routed component.
 */
export default function Dashboard() {
  const navigate = useNavigate();

  const nodes = useNodesStore((s) => s.nodes);
  const fetchNodes = useNodesStore((s) => s.fetch);

  const jobs = useJobsStore((s) => s.jobs);
  const fetchJobs = useJobsStore((s) => s.fetch);

  const pools = usePoolsStore((s) => s.pools);
  const fetchPools = usePoolsStore((s) => s.fetch);

  const backends = useStorageStore((s) => s.backends);
  const fetchBackends = useStorageStore((s) => s.fetch);

  // AI Note: the dependency array lists the store `fetch` actions, which
  // Zustand keeps referentially stable for the life of the store. That is what
  // makes this an effective mount-only effect — if any of these stores is ever
  // rewritten to recreate its actions per render, this turns into an infinite
  // fetch loop.
  useEffect(() => {
    fetchNodes();
    fetchJobs();
    fetchPools();
    fetchBackends();
  }, [fetchNodes, fetchJobs, fetchPools, fetchBackends]);

  // AI Note: "online" for the counter includes `busy` — a node executing a job
  // is still healthy and connected. Counting only `status === "online"` would
  // make the tile drop toward zero exactly when the cluster is busiest.
  const onlineNodes = nodes.filter((n) => n.status === "online" || n.status === "busy").length;
  const activeJobs = jobs.filter((j) => j.status === "running").length;

  // AI Note: `[...jobs]` copies before sorting because `Array.prototype.sort`
  // mutates in place and `jobs` is the Zustand store array — sorting it
  // directly would mutate shared state without notifying subscribers.
  // `created_at` is a UTC ISO string from the server (see the UTCDateTime
  // serializer); `new Date(...)` parses it to epoch ms for a newest-first sort.
  const recentJobs = [...jobs]
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .slice(0, 10);

  return (
    <div className="space-y-8">
      <h2 className="text-2xl font-bold text-foreground">Dashboard</h2>

      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="Nodes Online" value={onlineNodes} icon={Server} />
        <StatCard label="Active Jobs" value={activeJobs} icon={Play} />
        <StatCard label="Total Pools" value={pools.length} icon={Layers} />
        <StatCard label="Storage Backends" value={backends.length} icon={HardDrive} />
      </div>

      {/* Recent Jobs */}
      <div>
        <h3 className="text-lg font-semibold text-foreground mb-4">Recent Jobs</h3>
        <div className="bg-card border border-border rounded-xl shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40">
                <th className="text-left px-4 py-3 font-medium text-muted-foreground">Name</th>
                <th className="text-left px-4 py-3 font-medium text-muted-foreground">Status</th>
                <th className="text-left px-4 py-3 font-medium text-muted-foreground">Priority</th>
                <th className="text-left px-4 py-3 font-medium text-muted-foreground">Created</th>
              </tr>
            </thead>
            <tbody>
              {recentJobs.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">
                    No jobs found
                  </td>
                </tr>
              ) : (
                recentJobs.map((job) => (
                  <tr
                    key={job.id}
                    onClick={() => navigate(`/jobs/${job.id}`)}
                    className="border-b border-border last:border-b-0 hover:bg-muted/30 cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-3 font-medium text-foreground">{job.name}</td>
                    <td className="px-4 py-3">
                      <span
                        className={cn(
                          "inline-block px-2.5 py-0.5 rounded-full text-xs font-medium capitalize",
                          statusColors[job.status] ?? "bg-secondary text-muted-foreground"
                        )}
                      >
                        {job.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">{job.priority}</td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {formatRelativeTime(job.created_at)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
