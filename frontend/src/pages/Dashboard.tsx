import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Server, Play, Layers, HardDrive } from "lucide-react";
import { useNodesStore, useJobsStore, usePoolsStore, useStorageStore } from "@/stores";
import { cn, formatRelativeTime } from "@/lib/utils";
import type { JobStatus } from "@/types";

const statusColors: Record<JobStatus, string> = {
  completed: "bg-green-100 text-green-800",
  running: "bg-blue-100 text-blue-800",
  failed: "bg-red-100 text-red-800",
  pending: "bg-yellow-100 text-yellow-800",
  queued: "bg-yellow-100 text-yellow-800",
  cancelled: "bg-secondary text-muted-foreground",
};

interface StatCardProps {
  label: string;
  value: number;
  icon: React.ComponentType<{ className?: string }>;
}

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

  useEffect(() => {
    fetchNodes();
    fetchJobs();
    fetchPools();
    fetchBackends();
  }, [fetchNodes, fetchJobs, fetchPools, fetchBackends]);

  const onlineNodes = nodes.filter((n) => n.status === "online" || n.status === "busy").length;
  const activeJobs = jobs.filter((j) => j.status === "running").length;

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
