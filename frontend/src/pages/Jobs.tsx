/**
 * Jobs.tsx — the job list / management table (route `/jobs`).
 *
 * Role in the system:
 *   Primary operator view for everything that has been submitted to the
 *   cluster. Renders one row per job with live-updating status, and exposes the
 *   only two destructive job actions in the UI: cancel (in-flight jobs) and
 *   delete (terminal jobs). Terminal rows also get a non-destructive re-run.
 *
 * Data flow:
 *   - Reads/refreshes `useJobsStore` (`frontend/src/stores/index.ts`) ->
 *     GET /api/jobs (optionally `?status=`).
 *   - Mutations go straight through `api` (`frontend/src/api/client.ts`):
 *     POST /api/jobs/{id}/cancel, POST /api/jobs/{id}/requeue and
 *     DELETE /api/jobs/{id}, each followed by a store refetch.
 *   - Live status changes arrive out-of-band: `<Layout />` owns the
 *     `/ws/dashboard` socket and `handleWsMessage` calls
 *     `useJobsStore.updateJobStatus()`, mutating rows already on screen.
 *
 * Neighbours: links to `JobBuilder.tsx` (`/jobs/new`) and navigates to
 * `JobDetail.tsx` (`/jobs/:id`) on row click.
 */
import { useEffect, useState, useCallback } from "react";
import { useNavigate, Link } from "react-router-dom";
import {
  Plus,
  Loader2,
  XCircle,
  Trash2,
  ChevronDown,
  RotateCcw,
} from "lucide-react";
import { useJobsStore } from "@/stores";
import { api } from "@/api/client";
import { cn, formatRelativeTime } from "@/lib/utils";
import type { JobStatus } from "@/types";

/**
 * Options for the status dropdown. `value` is sent verbatim as the `status`
 * query parameter to GET /api/jobs, so these strings must match the server's
 * `JobStatus` enum values exactly — the sentinel `"all"` is the one exception
 * and is stripped before the request is built.
 *
 * AI Note: there is no `queued` entry even though `queued` is a real backend
 * status (it is rendered in the table and coloured by {@link statusBadge}).
 * Queued jobs are therefore only visible under "All". Add an option here if
 * operators need to filter on it.
 */
const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "pending", label: "Pending" },
  { value: "cancelled", label: "Cancelled" },
];

/**
 * Renders the coloured status pill for a job row.
 *
 * Returns JSX (not a component) so it can be dropped inline in a `<td>`.
 * Unknown/new statuses fall back to the neutral secondary style via the `??`
 * guard, so a backend enum addition degrades gracefully instead of rendering
 * an unstyled pill.
 *
 * AI Note: this colour map is a near-duplicate of `statusColors` in
 * `Dashboard.tsx` but uses the `-700` text shade instead of `-800`. Keep both
 * in sync when statuses change; they are intentionally separate today because
 * the two tables have different visual density.
 */
function statusBadge(status: JobStatus) {
  const colors: Record<JobStatus, string> = {
    running: "bg-blue-100 text-blue-700",
    completed: "bg-green-100 text-green-700",
    failed: "bg-red-100 text-red-700",
    pending: "bg-yellow-100 text-yellow-700",
    queued: "bg-yellow-100 text-yellow-700",
    cancelled: "bg-secondary text-muted-foreground",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize",
        colors[status] ?? "bg-secondary text-muted-foreground"
      )}
    >
      {status}
    </span>
  );
}

/**
 * Formats a job's elapsed wall-clock time for the Duration column.
 *
 * @param start ISO-8601 UTC timestamp of `started_at`, or null if the job has
 *   not been dispatched to a node yet.
 * @param end   ISO-8601 UTC timestamp of `completed_at`, or null while running.
 * @returns `"-"` when the job never started, else `"12s"` / `"3m 4s"` /
 *   `"1h 20m"` depending on magnitude.
 *
 * AI Note: when `end` is null the duration is measured against `Date.now()`,
 * i.e. a *running* job's duration is computed at render time and only advances
 * when the component re-renders (there is no interval tick here). Rows
 * therefore appear to "jump" forward when a WebSocket message or refetch
 * triggers a re-render, which is deliberate — a per-second timer over the whole
 * table was not worth the churn.
 *
 * AI Note: `Math.max(0, ...)` clamps negative values. Clock skew between the
 * server (which stamps `started_at`) and the browser can otherwise produce a
 * negative duration; this is the same class of bug that produced "-17990s ago"
 * before timestamps were normalised to UTC.
 */
function formatDuration(start: string | null, end: string | null): string {
  if (!start) return "-";
  const from = new Date(start).getTime();
  const to = end ? new Date(end).getTime() : Date.now();
  const secs = Math.max(0, Math.floor((to - from) / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const remSecs = secs % 60;
  if (mins < 60) return `${mins}m ${remSecs}s`;
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  return `${hrs}h ${remMins}m`;
}

/**
 * Job list page.
 *
 * What the user sees: a header with a "New Job" button, a status filter
 * dropdown, and a seven-column table (Name, Status, Priority, Current Step,
 * Created, Duration, Actions). Rows are clickable and route to the job detail
 * page. Per-row action buttons open a modal confirmation before cancelling or
 * deleting.
 *
 * State:
 *   - `statusFilter`: drives the server-side `?status=` query, not client-side
 *     filtering — changing it refetches.
 *   - `confirmAction`: `{ type, jobId }` for the modal, or null when closed.
 *     Doubles as the modal's visibility flag.
 *   - `actionLoading`: id of the job whose mutation is in flight; used to
 *     disable both the row button and the modal's confirm button.
 *
 * Side effects: GET /api/jobs on mount and on every filter change;
 * POST /api/jobs/{id}/cancel and DELETE /api/jobs/{id} from the modal, and
 * POST /api/jobs/{id}/requeue straight from the row (no modal — it is additive).
 *
 * Props: none — routed component.
 */
export default function Jobs() {
  const navigate = useNavigate();
  // AI Note: this subscribes to the whole jobs store (not a selector slice), so
  // the table re-renders on every WebSocket-driven `updateJobStatus` call. That
  // is intentional — it is how running rows update live without polling.
  const { jobs, isLoading, fetch } = useJobsStore();
  const [statusFilter, setStatusFilter] = useState("all");
  const [confirmAction, setConfirmAction] = useState<{
    type: "cancel" | "delete";
    jobId: string;
  } | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  // Refetch whenever the filter changes. The "all" sentinel is dropped so the
  // request goes out with no query string at all.
  useEffect(() => {
    const params: Record<string, string> = {};
    if (statusFilter !== "all") params.status = statusFilter;
    fetch(params);
  }, [fetch, statusFilter]);

  /**
   * Cancels a job: POST /api/jobs/{id}/cancel, then reloads the list.
   *
   * AI Note: the refetch deliberately calls `useJobsStore.getState().fetch()`
   * with NO params rather than the `fetch` captured from the hook. That means
   * the reload ignores the active status filter, so a job cancelled while
   * "Running" is selected reappears in a list that now contains every status.
   * It also keeps the `useCallback` dependency array empty (stable identity).
   *
   * AI Note: errors are swallowed on purpose. `api.request()` already
   * force-redirects to /login on 401; for other failures the subsequent refetch
   * simply shows the unchanged job, and the modal closes either way via
   * `finally`. There is no toast system on this page yet.
   */
  const handleCancel = useCallback(async (jobId: string) => {
    setActionLoading(jobId);
    try {
      await api.cancelJob(jobId);
      await useJobsStore.getState().fetch();
    } catch {
      // error handled by api client
    } finally {
      setActionLoading(null);
      setConfirmAction(null);
    }
  }, []);

  /**
   * Permanently deletes a job: DELETE /api/jobs/{id}, then reloads the list.
   *
   * Only reachable from rows in a terminal state (completed / failed /
   * cancelled) — the server also rejects deleting an in-flight job, so the
   * conditional render of the trash button is a UX affordance, not the
   * authoritative guard.
   *
   * Shares the same swallow-errors + filter-losing refetch caveats documented
   * on {@link handleCancel}.
   */
  const handleDelete = useCallback(async (jobId: string) => {
    setActionLoading(jobId);
    try {
      await api.deleteJob(jobId);
      await useJobsStore.getState().fetch();
    } catch {
      // error handled by api client
    } finally {
      setActionLoading(null);
      setConfirmAction(null);
    }
  }, []);

  /**
   * Re-runs a job: POST /api/jobs/{id}/requeue, then reloads the list so the
   * copy appears. Stays on the list rather than navigating to the new job —
   * the common use is re-running several jobs in a row.
   *
   * AI Note: no confirmation modal, unlike cancel and delete. Those are
   * destructive and irreversible; this only adds a job, and the mistake is
   * undone by cancelling the copy. Do not route it through `confirmAction` for
   * symmetry's sake — that would put a modal in front of the cheapest action
   * on the page.
   *
   * Shares the swallow-errors + filter-losing refetch caveats documented on
   * {@link handleCancel}. Here the filter loss is actually load-bearing: the
   * new job is `pending`, so under a "Completed" or "Failed" filter it would
   * not come back in a filtered refetch and the click would look like a no-op.
   */
  const handleRerun = useCallback(async (jobId: string) => {
    setActionLoading(jobId);
    try {
      await api.requeueJob(jobId);
      await useJobsStore.getState().fetch();
    } catch {
      // error handled by api client
    } finally {
      setActionLoading(null);
    }
  }, []);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Jobs</h1>
        <Link
          to="/jobs/new"
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          <Plus className="h-4 w-4" />
          New Job
        </Link>
      </div>

      {/* Filter Row */}
      <div className="flex items-center gap-3">
        <div className="relative">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="appearance-none rounded-lg border border-border bg-background px-4 py-2 pr-10 text-sm font-medium focus:outline-none focus:ring-2 focus:ring-ring"
          >
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <ChevronDown className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        </div>
      </div>

      {/* Table */}
      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/50">
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Name
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Status
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Priority
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Current Step
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Created
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Duration
              </th>
              <th className="px-4 py-3 text-right font-medium text-muted-foreground">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {isLoading ? (
              <tr>
                <td colSpan={7} className="px-4 py-12 text-center">
                  <Loader2 className="mx-auto h-6 w-6 animate-spin text-muted-foreground" />
                </td>
              </tr>
            ) : jobs.length === 0 ? (
              <tr>
                <td
                  colSpan={7}
                  className="px-4 py-12 text-center text-muted-foreground"
                >
                  No jobs found.
                </td>
              </tr>
            ) : (
              jobs.map((job) => (
                <tr
                  key={job.id}
                  onClick={() => navigate(`/jobs/${job.id}`)}
                  className="cursor-pointer hover:bg-muted/50 transition-colors"
                >
                  <td className="px-4 py-3 font-medium">{job.name}</td>
                  <td className="px-4 py-3">{statusBadge(job.status)}</td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {job.priority}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {/* AI Note: `current_step` is -1 (not null) for jobs that
                        have not begun executing, so the guard is `>= 0` —
                        step 0 is a real, running first step. */}
                    {job.current_step >= 0 ? `Step ${job.current_step}` : "-"}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {formatRelativeTime(job.created_at)}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {formatDuration(job.started_at, job.completed_at)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {/* AI Note: this wrapper stops click propagation so the
                        action buttons do not also trigger the row's
                        navigate-to-detail handler. Any new control added to
                        the Actions cell must stay inside this div. */}
                    <div
                      className="inline-flex items-center gap-1"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {(job.status === "running" || job.status === "pending" || job.status === "queued") && (
                        <button
                          onClick={() =>
                            setConfirmAction({ type: "cancel", jobId: job.id })
                          }
                          disabled={actionLoading === job.id}
                          className="rounded-md p-1.5 text-muted-foreground hover:bg-red-50 hover:text-red-600 transition-colors disabled:opacity-50"
                          title="Cancel"
                        >
                          <XCircle className="h-4 w-4" />
                        </button>
                      )}
                      {(job.status === "completed" || job.status === "failed" || job.status === "cancelled") && (
                        <>
                          <button
                            onClick={() => handleRerun(job.id)}
                            disabled={actionLoading === job.id}
                            className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors disabled:opacity-50"
                            title="Re-run with the same parameters"
                          >
                            <RotateCcw className="h-4 w-4" />
                          </button>
                          <button
                            onClick={() =>
                              setConfirmAction({ type: "delete", jobId: job.id })
                            }
                            disabled={actionLoading === job.id}
                            className="rounded-md p-1.5 text-muted-foreground hover:bg-red-50 hover:text-red-600 transition-colors disabled:opacity-50"
                            title="Delete"
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Confirmation Dialog — a hand-rolled modal (no portal): the fixed
          overlay is a sibling of the table inside the page container, and the
          backdrop div is click-to-dismiss. z-50 must stay above the app
          sidebar/header rendered by <Layout />. */}
      {confirmAction && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="fixed inset-0 bg-black/50"
            onClick={() => setConfirmAction(null)}
          />
          <div className="relative z-10 w-full max-w-sm rounded-xl border border-border bg-card p-6 shadow-xl">
            <h3 className="text-lg font-semibold">
              {confirmAction.type === "cancel" ? "Cancel Job?" : "Delete Job?"}
            </h3>
            <p className="mt-2 text-sm text-muted-foreground">
              {confirmAction.type === "cancel"
                ? "This will cancel the running job. This action cannot be undone."
                : "This will permanently delete the job and its data."}
            </p>
            <div className="mt-6 flex items-center justify-end gap-3">
              <button
                onClick={() => setConfirmAction(null)}
                className="rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors"
              >
                Keep
              </button>
              <button
                onClick={() =>
                  confirmAction.type === "cancel"
                    ? handleCancel(confirmAction.jobId)
                    : handleDelete(confirmAction.jobId)
                }
                disabled={actionLoading === confirmAction.jobId}
                className="inline-flex items-center gap-2 rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700 transition-colors disabled:opacity-50"
              >
                {actionLoading === confirmAction.jobId && (
                  <Loader2 className="h-4 w-4 animate-spin" />
                )}
                {confirmAction.type === "cancel" ? "Cancel Job" : "Delete Job"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
