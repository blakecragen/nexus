import { useEffect, useState, useRef, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  CheckCircle,
  XCircle,
  Loader2,
  Clock,
  SkipForward,
  Ban,
  Download,
  ChevronRight,
  Folder,
  FolderOpen,
  FileText,
  FileCode,
  FileBarChart,
  File as FileIcon,
  Package,
} from "lucide-react";
import { api } from "@/api/client";
import { useLiveLogsStore } from "@/stores";
import { cn, formatBytes, formatRelativeTime } from "@/lib/utils";
import type { JobDetail as JobDetailType, StepRunInfo, StepStatus, JobStatus, ArtifactInfo } from "@/types";

type DetailTab = "logs" | "params" | "outputs" | "context" | "full-log" | "results";

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

function stepStatusIcon(status: StepStatus) {
  switch (status) {
    case "success":
      return <CheckCircle className="h-5 w-5 text-green-500" />;
    case "failed":
      return <XCircle className="h-5 w-5 text-red-500" />;
    case "running":
      return <Loader2 className="h-5 w-5 animate-spin text-blue-500" />;
    case "cancelled":
      return <Ban className="h-5 w-5 text-muted-foreground" />;
    case "skipped":
      return <SkipForward className="h-5 w-5 text-muted-foreground" />;
    case "pending":
    default:
      return <Clock className="h-5 w-5 text-yellow-500" />;
  }
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

function JsonBlock({ data }: { data: unknown }) {
  return (
    <pre className="overflow-auto rounded-lg bg-gray-900 p-4 text-xs text-muted-foreground font-mono leading-relaxed max-h-[500px]">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

// ── Results file tree ──────────────────────────────────────────────────────

type ManifestEntry = { path: string; size: number; is_dir: boolean };
type TreeNode = {
  name: string;
  path: string;
  isDir: boolean;
  size: number;       // own size (files) or aggregate (dirs)
  children: TreeNode[];
};

/** Build a nested tree from the flat tarball manifest. */
function buildTree(entries: ManifestEntry[]): TreeNode {
  const root: TreeNode = { name: "", path: "", isDir: true, size: 0, children: [] };
  const dirOf = new Map<string, TreeNode>([["", root]]);

  const ensureDir = (path: string): TreeNode => {
    if (dirOf.has(path)) return dirOf.get(path)!;
    const slash = path.lastIndexOf("/");
    const parent = ensureDir(slash === -1 ? "" : path.slice(0, slash));
    const node: TreeNode = {
      name: path.slice(slash + 1),
      path,
      isDir: true,
      size: 0,
      children: [],
    };
    parent.children.push(node);
    dirOf.set(path, node);
    return node;
  };

  for (const e of entries) {
    const clean = e.path.replace(/\/+$/, "");
    if (!clean) continue;
    if (e.is_dir) {
      ensureDir(clean);
    } else {
      const slash = clean.lastIndexOf("/");
      const parent = ensureDir(slash === -1 ? "" : clean.slice(0, slash));
      parent.children.push({
        name: clean.slice(slash + 1),
        path: clean,
        isDir: false,
        size: e.size,
        children: [],
      });
    }
  }

  // Aggregate dir sizes + sort (dirs first, then alpha).
  const finalize = (node: TreeNode): number => {
    if (!node.isDir) return node.size;
    let total = 0;
    for (const c of node.children) total += finalize(c);
    node.size = total;
    node.children.sort((a, b) =>
      a.isDir === b.isDir ? a.name.localeCompare(b.name) : a.isDir ? -1 : 1
    );
    return total;
  };
  finalize(root);
  return root;
}

function fileGlyph(name: string) {
  const lower = name.toLowerCase();
  if (lower === "stats.txt") return { Icon: FileBarChart, className: "text-emerald-400" };
  if (lower.endsWith(".json") || lower.endsWith(".ini") || lower.endsWith(".dot"))
    return { Icon: FileCode, className: "text-sky-400" };
  if (lower.endsWith(".txt") || lower.endsWith(".bib") || lower.endsWith(".log"))
    return { Icon: FileText, className: "text-zinc-400" };
  return { Icon: FileIcon, className: "text-zinc-500" };
}

function TreeRow({ node, depth }: { node: TreeNode; depth: number }) {
  const [open, setOpen] = useState(depth < 1); // top-level dir expanded by default
  const pad = { paddingLeft: `${depth * 16 + 12}px` };
  const isStats = !node.isDir && node.name.toLowerCase() === "stats.txt";

  if (node.isDir) {
    const FolderGlyph = open ? FolderOpen : Folder;
    return (
      <div>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          style={pad}
          className="group flex w-full items-center gap-2 py-1.5 pr-3 text-left font-mono text-xs hover:bg-white/[0.04] transition-colors"
        >
          <ChevronRight
            className={cn(
              "h-3.5 w-3.5 shrink-0 text-zinc-500 transition-transform duration-150",
              open && "rotate-90"
            )}
          />
          <FolderGlyph className="h-3.5 w-3.5 shrink-0 text-amber-400/80" />
          <span className="truncate text-zinc-200">{node.name}</span>
          <span className="ml-auto shrink-0 tabular-nums text-[11px] text-zinc-600">
            {node.children.length} item{node.children.length === 1 ? "" : "s"} · {formatBytes(node.size)}
          </span>
        </button>
        {open && (
          <div className="relative">
            {/* indent guide line */}
            <span
              className="pointer-events-none absolute top-0 bottom-0 w-px bg-white/[0.06]"
              style={{ left: `${depth * 16 + 19}px` }}
            />
            {node.children.map((c) => (
              <TreeRow key={c.path} node={c} depth={depth + 1} />
            ))}
          </div>
        )}
      </div>
    );
  }

  const { Icon, className } = fileGlyph(node.name);
  return (
    <div
      style={pad}
      className={cn(
        "flex items-center gap-2 py-1.5 pr-3 font-mono text-xs",
        isStats ? "bg-emerald-500/[0.06]" : "hover:bg-white/[0.04] transition-colors"
      )}
    >
      <span className="h-3.5 w-3.5 shrink-0" />
      <Icon className={cn("h-3.5 w-3.5 shrink-0", className)} />
      <span className={cn("truncate", isStats ? "text-emerald-300" : "text-zinc-300")}>
        {node.name}
      </span>
      {isStats && (
        <span className="shrink-0 rounded bg-emerald-500/15 px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-emerald-400">
          stats
        </span>
      )}
      <span className="ml-auto shrink-0 tabular-nums text-[11px] text-zinc-500">
        {formatBytes(node.size)}
      </span>
    </div>
  );
}

function ResultsTree({
  manifest,
  downloading,
  onDownload,
}: {
  manifest: { archive_bytes: number; entries: ManifestEntry[] } | null;
  downloading: boolean;
  onDownload: () => void;
}) {
  if (!manifest) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Reading archive…
      </div>
    );
  }

  const tree = buildTree(manifest.entries);
  const fileCount = manifest.entries.filter((e) => !e.is_dir).length;

  return (
    <div className="space-y-3">
      {/* Header strip */}
      <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-card/40 px-4 py-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
            <Package className="h-4.5 w-4.5 text-primary" />
          </div>
          <div className="leading-tight">
            <div className="text-sm font-medium">results.tar.gz</div>
            <div className="text-xs text-muted-foreground tabular-nums">
              {fileCount} file{fileCount === 1 ? "" : "s"} · {formatBytes(manifest.archive_bytes)} compressed
            </div>
          </div>
        </div>
        <button
          type="button"
          onClick={onDownload}
          disabled={downloading}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
        >
          {downloading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
          Download
        </button>
      </div>

      {/* Tree */}
      <div className="overflow-hidden rounded-lg border border-border bg-gray-900/60">
        <div className="border-b border-white/[0.06] px-3 py-2 font-mono text-[11px] uppercase tracking-wider text-zinc-500">
          Archive contents
        </div>
        <div className="max-h-[440px] overflow-auto py-1">
          {tree.children.length === 0 ? (
            <div className="px-4 py-8 text-center text-xs text-muted-foreground">Archive is empty.</div>
          ) : (
            tree.children.map((c) => <TreeRow key={c.path} node={c} depth={0} />)
          )}
        </div>
      </div>
    </div>
  );
}


export default function JobDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [detail, setDetail] = useState<JobDetailType | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactInfo[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [selectedStep, setSelectedStep] = useState<number>(0);
  const [activeTab, setActiveTab] = useState<DetailTab>("logs");
  const [cancelling, setCancelling] = useState(false);
  const [fullLog, setFullLog] = useState<string>("");
  const [resultsManifest, setResultsManifest] = useState<
    { archive_bytes: number; entries: { path: string; size: number; is_dir: boolean }[] } | null
  >(null);
  const [downloadingResults, setDownloadingResults] = useState(false);

  const logs = useLiveLogsStore((s) => s.logs);
  const logContainerRef = useRef<HTMLDivElement>(null);

  // Fetch job detail
  useEffect(() => {
    if (!id) return;
    setIsLoading(true);
    api.getJob(id)
      .then((jobDetail) => {
        setDetail(jobDetail);
        // Select the running step by default if any
        const runningIdx = jobDetail.steps.findIndex((s) => s.status === "running");
        if (runningIdx >= 0) setSelectedStep(runningIdx);
      })
      .catch(() => {
        // leave detail null; the page shows the loader/empty state
      })
      .finally(() => setIsLoading(false));
    // Artifacts are optional — never let them block rendering the job.
    api.listArtifacts(id).then(setArtifacts).catch(() => setArtifacts([]));
  }, [id]);

  // Poll for updates while job is active
  useEffect(() => {
    if (!id || !detail) return;
    const isActive = ["pending", "queued", "running"].includes(detail.job.status);
    if (!isActive) return;

    const interval = setInterval(async () => {
      try {
        const updated = await api.getJob(id);
        setDetail(updated);
      } catch {
        // ignore polling errors
      }
      api.listArtifacts(id).then(setArtifacts).catch(() => {});
    }, 3000);

    return () => clearInterval(interval);
  }, [id, detail?.job.status]);

  // Auto-scroll logs
  const logKey = id ? `${id}:${selectedStep}` : "";
  const currentLogs = logs[logKey] || [];

  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [currentLogs.length]);

  // Fetch the persisted per-job terminal log when its tab is open (and refresh
  // while the job is still active).
  useEffect(() => {
    if (activeTab !== "full-log" || !id) return;
    let cancelled = false;
    const load = () => api.getJobLog(id).then((t) => { if (!cancelled) setFullLog(t); }).catch(() => {});
    load();
    const active = ["pending", "queued", "running"].includes(detail?.job.status ?? "");
    const interval = active ? setInterval(load, 3000) : undefined;
    return () => { cancelled = true; if (interval) clearInterval(interval); };
  }, [activeTab, id, detail?.job.status]);

  // Fetch the results manifest (file list inside the tarball) when its tab opens.
  useEffect(() => {
    if (activeTab !== "results" || !id || !detail?.has_results) return;
    let cancelled = false;
    api.getJobResultsManifest(id)
      .then((m) => { if (!cancelled) setResultsManifest(m); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [activeTab, id, detail?.has_results]);

  const handleDownloadResults = useCallback(async () => {
    if (!id) return;
    setDownloadingResults(true);
    try {
      await api.downloadJobResults(id);
    } catch {
      /* surfaced by api client */
    } finally {
      setDownloadingResults(false);
    }
  }, [id]);

  const handleCancel = useCallback(async () => {
    if (!id) return;
    setCancelling(true);
    try {
      await api.cancelJob(id);
      const updated = await api.getJob(id);
      setDetail(updated);
    } catch {
      // handled by api client
    } finally {
      setCancelling(false);
    }
  }, [id]);

  if (isLoading || !detail) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const { job, steps, context_data } = detail;
  const currentStepData: StepRunInfo | undefined = steps[selectedStep];
  const isActive = ["pending", "queued", "running"].includes(job.status);

  const tabs: Array<{ key: DetailTab; label: string }> = [
    { key: "logs", label: "Logs" },
    { key: "params", label: "Params" },
    { key: "outputs", label: "Outputs" },
    { key: "context", label: "Context" },
    { key: "full-log", label: "Full Terminal Log" },
    ...(detail.has_results ? [{ key: "results" as DetailTab, label: "Results" }] : []),
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate("/jobs")}
              className="rounded-md p-1 hover:bg-muted transition-colors"
            >
              <ArrowLeft className="h-5 w-5" />
            </button>
            <h1 className="text-2xl font-bold tracking-tight">{job.name}</h1>
            {statusBadge(job.status)}
          </div>
          <div className="flex items-center gap-4 text-sm text-muted-foreground pl-9">
            <span>
              Submitted by{" "}
              <span className="font-medium text-foreground">
                {job.submitted_by.slice(0, 8)}
              </span>
            </span>
            <span>Created {formatRelativeTime(job.created_at)}</span>
            {job.started_at && (
              <span>Started {formatRelativeTime(job.started_at)}</span>
            )}
            {job.completed_at && (
              <span>Completed {formatRelativeTime(job.completed_at)}</span>
            )}
          </div>
        </div>
        {isActive && (
          <button
            onClick={handleCancel}
            disabled={cancelling}
            className="inline-flex items-center gap-2 rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700 transition-colors disabled:opacity-50"
          >
            {cancelling && <Loader2 className="h-4 w-4 animate-spin" />}
            Cancel Job
          </button>
        )}
      </div>

      {/* Error Banner */}
      {job.error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <strong>Error:</strong> {job.error}
        </div>
      )}

      {/* Two-column layout */}
      <div className="flex gap-6">
        {/* Left: Step Timeline */}
        <div className="flex-1 space-y-1">
          <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide mb-3">
            Steps
          </h2>
          <div className="space-y-0">
            {steps.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4">
                No steps to display.
              </p>
            ) : (
              steps.map((step, idx) => {
                const isSelected = idx === selectedStep;
                const isRunning = step.status === "running";
                return (
                  <button
                    key={step.id}
                    onClick={() => {
                      setSelectedStep(idx);
                      setActiveTab("logs");
                    }}
                    className={cn(
                      "w-full flex items-center gap-3 rounded-lg px-4 py-3 text-left transition-all",
                      isSelected
                        ? "bg-primary/5 border border-primary/20"
                        : "hover:bg-muted/50 border border-transparent",
                      isRunning && "animate-pulse"
                    )}
                  >
                    {/* Timeline connector */}
                    <div className="flex flex-col items-center gap-1">
                      <div className="flex h-8 w-8 items-center justify-center rounded-full bg-muted text-xs font-bold">
                        {idx}
                      </div>
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-sm truncate">
                          {step.step_name}
                        </span>
                        {stepStatusIcon(step.status)}
                      </div>
                      <div className="text-xs text-muted-foreground mt-0.5">
                        {formatDuration(step.started_at, step.finished_at)}
                        {step.error && (
                          <span className="ml-2 text-red-500 truncate">
                            {step.error.slice(0, 60)}
                          </span>
                        )}
                      </div>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Right: Details Panel */}
        <div className="w-96 flex-shrink-0 space-y-4">
          {/* Tabs */}
          <div className="flex border-b border-border">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={cn(
                  "px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px",
                  activeTab === tab.key
                    ? "border-primary text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                )}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Tab Content */}
          <div className="min-h-[400px]">
            {activeTab === "logs" && (
              <div
                ref={logContainerRef}
                className="h-[500px] overflow-auto rounded-lg bg-gray-900 p-4 font-mono text-xs leading-relaxed"
              >
                {currentLogs.length === 0 ? (
                  <span className="text-muted-foreground">
                    No log output yet. Logs appear in real time while a step
                    runs.
                  </span>
                ) : (
                  currentLogs.map((entry, i) => (
                    <div
                      key={i}
                      className={
                        entry.stream === "stderr"
                          ? "text-red-400"
                          : "text-green-400"
                      }
                    >
                      {entry.line}
                    </div>
                  ))
                )}
              </div>
            )}

            {activeTab === "params" && (
              <JsonBlock
                data={currentStepData?.input_params ?? { note: "No input parameters" }}
              />
            )}

            {activeTab === "outputs" && (
              <JsonBlock
                data={currentStepData?.output_params ?? { note: "No output parameters" }}
              />
            )}

            {activeTab === "context" && (
              <JsonBlock data={context_data} />
            )}

            {activeTab === "full-log" && (
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">
                    Every command run for this job and its full stdout/stderr.
                  </span>
                  <button
                    type="button"
                    onClick={() => {
                      const blob = new Blob([fullLog], { type: "text/plain" });
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement("a");
                      a.href = url;
                      a.download = `job_${job.id}.txt`;
                      a.click();
                      URL.revokeObjectURL(url);
                    }}
                    disabled={!fullLog}
                    className="inline-flex items-center gap-2 rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:bg-muted transition-colors disabled:opacity-50"
                  >
                    <Download className="h-3.5 w-3.5" />
                    Download .txt
                  </button>
                </div>
                <pre className="h-[500px] overflow-auto rounded-lg bg-gray-900 p-4 font-mono text-xs leading-relaxed text-green-400 whitespace-pre-wrap">
                  {fullLog || "No terminal output captured yet."}
                </pre>
              </div>
            )}

            {activeTab === "results" && (
              <ResultsTree
                manifest={resultsManifest}
                downloading={downloadingResults}
                onDownload={handleDownloadResults}
              />
            )}
          </div>
        </div>
      </div>

      {/* Artifacts Section */}
      {artifacts.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide">
            Artifacts
          </h2>
          <div className="overflow-hidden rounded-xl border border-border bg-card">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/50">
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">
                    Filename
                  </th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">
                    Type
                  </th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">
                    Size
                  </th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">
                    Storage
                  </th>
                  <th className="px-4 py-2 text-right font-medium text-muted-foreground">
                    Download
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {artifacts.map((art) => (
                  <tr key={art.id}>
                    <td className="px-4 py-2 font-medium">{art.filename}</td>
                    <td className="px-4 py-2 text-muted-foreground">
                      {art.content_type || "-"}
                    </td>
                    <td className="px-4 py-2 text-muted-foreground">
                      {formatBytes(art.size_bytes)}
                    </td>
                    <td className="px-4 py-2 text-muted-foreground">
                      {art.storage_backend_name || art.storage_backend_id.slice(0, 8)}
                    </td>
                    <td className="px-4 py-2 text-right">
                      <a
                        href={`/api/artifacts/${art.id}/download`}
                        className="inline-flex items-center gap-1 rounded-md p-1.5 text-muted-foreground hover:text-primary transition-colors"
                        title="Download"
                      >
                        <Download className="h-4 w-4" />
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
