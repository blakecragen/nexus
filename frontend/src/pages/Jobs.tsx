import { useEffect, useState, useCallback } from "react";
import { useNavigate, Link } from "react-router-dom";
import {
  Plus,
  Loader2,
  XCircle,
  Trash2,
  ChevronDown,
} from "lucide-react";
import { useJobsStore } from "@/stores";
import { api } from "@/api/client";
import { cn, formatRelativeTime } from "@/lib/utils";
import type { JobStatus } from "@/types";

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "pending", label: "Pending" },
  { value: "cancelled", label: "Cancelled" },
];

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

export default function Jobs() {
  const navigate = useNavigate();
  const { jobs, isLoading, fetch } = useJobsStore();
  const [statusFilter, setStatusFilter] = useState("all");
  const [confirmAction, setConfirmAction] = useState<{
    type: "cancel" | "delete";
    jobId: string;
  } | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  useEffect(() => {
    const params: Record<string, string> = {};
    if (statusFilter !== "all") params.status = statusFilter;
    fetch(params);
  }, [fetch, statusFilter]);

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
                    {job.current_step >= 0 ? `Step ${job.current_step}` : "-"}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {formatRelativeTime(job.created_at)}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {formatDuration(job.started_at, job.completed_at)}
                  </td>
                  <td className="px-4 py-3 text-right">
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
                      )}
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Confirmation Dialog */}
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
