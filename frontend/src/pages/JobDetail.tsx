/**
 * JobDetail.tsx — single-job inspection view (route `/jobs/:id`).
 *
 * Role in the system:
 *   The deepest read view in the app. It combines four different data sources
 *   into one screen: the job + its step runs, the live log stream, the
 *   persisted terminal log, and the result tarball's contents.
 *
 * Data flow:
 *   - GET /api/jobs/{id}                    -> job, steps, context_data,
 *                                              has_results (polled while active)
 *   - GET /api/artifacts?job_id={id}        -> the artifacts table at the bottom
 *   - GET /api/jobs/{id}/log                -> plain-text "Full Terminal Log" tab
 *   - GET /api/jobs/{id}/results/manifest   -> the Results file tree
 *   - GET /api/jobs/{id}/results/download   -> authenticated tarball download
 *   - POST /api/jobs/{id}/cancel            -> Cancel Job button
 *   - `useLiveLogsStore` supplies the per-step live log lines, which are
 *     pushed in by `handleWsMessage` from the `/ws/dashboard` socket that
 *     `<Layout />` owns (message type `step.log`).
 *
 * AI Note: this page uses BOTH polling and WebSockets, and they cover
 * different things. Step status/timing comes from the 3s poll; log *lines*
 * only ever arrive over the socket. If the socket is down, the timeline still
 * advances but the Logs tab stays empty — that asymmetry explains most
 * "the logs are blank but the job is running" reports.
 *
 * File layout: badge/format helpers -> results-tree machinery (buildTree,
 * fileGlyph, TreeRow, ResultsTree) -> the exported page component.
 */
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

/** Keys for the right-hand detail panel's tab strip. `"results"` is only ever
 * present when the job reported `has_results`, so `activeTab` can hold a value
 * that has no visible tab if the flag flips off mid-session. */
type DetailTab = "logs" | "params" | "outputs" | "context" | "full-log" | "results";

/**
 * Job-level status pill shown next to the job title.
 *
 * Third copy of the status colour map (see also `Jobs.tsx` and
 * `Dashboard.tsx`); keep them consistent when the backend `JobStatus` enum
 * changes. Unknown statuses fall back to neutral styling.
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
 * Per-step icon for the timeline.
 *
 * @param status the `StepStatus` from a `StepRunInfo`.
 *
 * AI Note: `StepStatus` is a *different* enum from `JobStatus` — a step is
 * `success`/`failed`/`running`/`cancelled`/`skipped`/`pending`, note "success"
 * rather than "completed". Do not reuse the job status maps here.
 *
 * AI Note: `pending` and the `default` branch share the clock icon, so any
 * unrecognised status renders as "not started yet" rather than blowing up.
 */
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

/**
 * Elapsed-time formatter for a single step run.
 *
 * @param start `started_at` ISO string, or null if the step never ran.
 * @param end `finished_at` ISO string, or null while it is still running.
 * @returns `"-"`, `"12s"`, `"3m 4s"` or `"1h 20m"`.
 *
 * AI Note: byte-for-byte the same logic as `formatDuration` in `Jobs.tsx` —
 * duplicated rather than shared. Same caveats apply: a running step's duration
 * is computed at render time (it only advances when the 3s poll re-renders the
 * page) and `Math.max(0, ...)` clamps clock skew between server and browser.
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
 * Pretty-printed, scrollable JSON viewer used by the Params, Outputs and
 * Context tabs.
 *
 * @param data any JSON-serialisable value. `undefined` renders as an empty
 *   block, which is why callers pass a `{ note: ... }` placeholder instead.
 *
 * AI Note: `JSON.stringify` throws on circular structures. Everything shown
 * here comes from the API as parsed JSON, so that cannot happen today — but do
 * not repurpose this component for arbitrary client-side objects.
 */
function JsonBlock({ data }: { data: unknown }) {
  return (
    <pre className="overflow-auto rounded-lg bg-gray-900 p-4 text-xs text-muted-foreground font-mono leading-relaxed max-h-[500px]">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

// ── Results file tree ──────────────────────────────────────────────────────
//
// The server returns the results tarball's table of contents as a FLAT list of
// entries (`GET /api/jobs/{id}/results/manifest`). Everything below turns that
// flat list into an expandable directory tree without ever downloading the
// archive itself.

/** One row of the flat tarball manifest as returned by the server. `path` is
 * archive-relative and may or may not have a trailing slash for directories. */
type ManifestEntry = { path: string; size: number; is_dir: boolean };
/** A node in the tree built by {@link buildTree}. */
type TreeNode = {
  name: string;
  path: string;
  isDir: boolean;
  size: number;       // own size (files) or aggregate (dirs)
  children: TreeNode[];
};

/**
 * Build a nested tree from the flat tarball manifest.
 *
 * @param entries the manifest rows, in whatever order the archive listed them.
 * @returns a synthetic root node (`name`/`path` are `""`); callers render
 *   `root.children`, never the root itself.
 *
 * AI Note: entry order is NOT assumed. `ensureDir` recursively materialises
 * missing ancestors, so a file can appear before (or entirely without) its
 * parent directory entry — which real tarballs frequently do.
 *
 * AI Note: `dirOf` is what keeps this O(n) and prevents duplicate directory
 * nodes; it is seeded with `"" -> root` so the recursion terminates at the top
 * level.
 */
function buildTree(entries: ManifestEntry[]): TreeNode {
  const root: TreeNode = { name: "", path: "", isDir: true, size: 0, children: [] };
  const dirOf = new Map<string, TreeNode>([["", root]]);

  /** Return the node for `path`, creating it (and every missing ancestor) on
   * demand. Memoised through `dirOf` so each directory exists exactly once. */
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
    // AI Note: tar directory entries conventionally end in "/" ("m5out/"),
    // file entries do not. Stripping trailing slashes is what makes "m5out/"
    // and "m5out" resolve to the same node instead of creating a phantom
    // empty-named child. An entry that is nothing but slashes is skipped.
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
  //
  // AI Note: `finalize` MUTATES each directory node's `size` in place, turning
  // it from 0 into the recursive sum of its descendants, and returns that sum
  // so the parent can accumulate. It must run exactly once per tree; calling
  // it twice is harmless only because directory sizes are recomputed from
  // scratch, but file sizes are returned untouched.
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

/**
 * Picks an icon + tint for a file row based on its name.
 *
 * @param name the bare filename (no directory part).
 *
 * AI Note: `stats.txt` is special-cased because it is gem5's primary output
 * file — the one users open first. It gets an emerald chart icon here and a
 * highlighted row + "stats" chip in {@link TreeRow}. The `.txt` branch below
 * would otherwise swallow it, so this check must stay first.
 */
function fileGlyph(name: string) {
  const lower = name.toLowerCase();
  if (lower === "stats.txt") return { Icon: FileBarChart, className: "text-emerald-400" };
  if (lower.endsWith(".json") || lower.endsWith(".ini") || lower.endsWith(".dot"))
    return { Icon: FileCode, className: "text-sky-400" };
  if (lower.endsWith(".txt") || lower.endsWith(".bib") || lower.endsWith(".log"))
    return { Icon: FileText, className: "text-zinc-400" };
  return { Icon: FileIcon, className: "text-zinc-500" };
}

/**
 * One row of the results tree — recursive: directories render their children
 * through nested `TreeRow`s.
 *
 * What the user sees: directories are clickable disclosure rows showing a
 * chevron, folder glyph, child count and aggregate size; files are static rows
 * with a type icon and size. `stats.txt` gets a highlighted background and a
 * "stats" chip.
 *
 * @param node the tree node to render.
 * @param depth 0 for top-level entries; drives the left padding and the indent
 *   guide line.
 *
 * AI Note: `useState(depth < 1)` means only the first level is expanded on
 * mount. Because expansion state lives in each row (not lifted), collapsing a
 * parent and re-expanding it preserves the children's own open/closed state —
 * they never unmount, they are just hidden by the conditional render... except
 * they ARE unmounted (`{open && ...}`), so state does reset. Do not rely on it.
 *
 * AI Note: this component is purely a viewer — there is no per-file download.
 * The whole archive is fetched via the Download button in {@link ResultsTree}.
 */
function TreeRow({ node, depth }: { node: TreeNode; depth: number }) {
  const [open, setOpen] = useState(depth < 1); // top-level dir expanded by default
  // AI Note: 16px per depth level + 12px base gutter; the indent guide line in
  // the expanded branch below uses `depth * 16 + 19` to sit under the chevron.
  // Changing one without the other visibly misaligns the tree.
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

/**
 * The "Results" tab body: an archive summary strip with a Download button,
 * plus the expandable file tree.
 *
 * @param manifest the parsed manifest, or null while it is still loading — the
 *   null case renders a "Reading archive…" spinner, so this component doubles
 *   as its own loading state.
 * @param downloading true while the tarball download is in flight; disables the
 *   button and swaps in a spinner.
 * @param onDownload triggers the authenticated blob download in the parent.
 *
 * AI Note: {@link buildTree} runs on every render (no `useMemo`). Manifests are
 * small (tens to hundreds of entries) and the parent re-renders rarely, so this
 * is fine — but it would need memoising if manifests ever got large.
 *
 * AI Note: `fileCount` counts only non-directory entries, while
 * `archive_bytes` is the *compressed* size of the whole tarball. The per-file
 * sizes in the tree are uncompressed, so they will not sum to `archive_bytes`.
 */
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


/**
 * Job detail page.
 *
 * What the user sees:
 *   - Header: back arrow, job name, status pill, submitter/created/started/
 *     completed metadata, and a "Cancel Job" button while the job is active.
 *   - An error banner if the job failed with a message.
 *   - Left column: the step timeline. Each step shows its index, name, status
 *     icon, duration and a truncated error. Clicking one selects it and jumps
 *     back to the Logs tab.
 *   - Right column: a tab strip over the selected step — Logs (live), Params,
 *     Outputs, Context, Full Terminal Log, and Results (only when the job has
 *     a results tarball).
 *   - Bottom: the artifacts table, when any exist.
 *
 * Key state:
 *   - `detail`: the whole GET /api/jobs/{id} payload; null until loaded, which
 *     also drives the full-page spinner.
 *   - `selectedStep`: index into `detail.steps`, and half of the live-log key.
 *   - `activeTab`: which right-hand panel is shown; several effects key off it
 *     so data is only fetched when its tab is opened.
 *
 * Props: none — routed component reading `:id` from the URL.
 */
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
  //
  // AI Note: a failed fetch deliberately leaves `detail` null, which renders
  // the same spinner as "still loading" — a deleted or forbidden job id shows
  // an indefinite spinner rather than a 404 state. Known rough edge.
  useEffect(() => {
    if (!id) return;
    setIsLoading(true);
    api.getJob(id)
      .then((jobDetail) => {
        setDetail(jobDetail);
        // Select the running step by default if any
        //
        // AI Note: this only runs on the initial load (effect is keyed on
        // `id`), not on every poll — otherwise the selection would jump out
        // from under the user each time the job advanced a step.
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
  //
  // AI Note: 3000 ms is the refresh cadence for step status/timings. The
  // dependency array is `[id, detail?.job.status]` — NOT `[id, detail]` — so
  // the interval is only torn down and recreated when the *status* changes,
  // not on every polled payload. Depending on `detail` would restart the timer
  // every 3s and can drift into a tight loop; do not "fix" the exhaustive-deps
  // warning by adding `detail` here.
  //
  // AI Note: the interval stops as soon as the job reaches a terminal state,
  // so the final payload is whatever the last in-flight poll returned. The
  // status transition itself is what re-runs this effect and clears the timer.
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
  //
  // AI Note: `logKey` must match the key format used by
  // `useLiveLogsStore.appendLog` — `${jobId}:${stepIndex}` (see
  // frontend/src/stores/index.ts). Change one and live logs silently stop
  // appearing, because the lookup just misses.
  const logKey = id ? `${id}:${selectedStep}` : "";
  const currentLogs = logs[logKey] || [];

  // AI Note: keyed on `currentLogs.length`, not the array identity — this
  // pins the log pane to the bottom as lines stream in. It also means the view
  // snaps back down even if the user has scrolled up to read history, which is
  // a known annoyance during long-running steps.
  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [currentLogs.length]);

  // Fetch the persisted per-job terminal log when its tab is open (and refresh
  // while the job is still active).
  //
  // AI Note: this is the server-side `Job.log_text` (GET /api/jobs/{id}/log,
  // plain text), a *different* source from the WebSocket-driven per-step live
  // logs above. It survives page reloads and socket drops; the live logs do
  // not. Both can legitimately show different content.
  //
  // AI Note: the `cancelled` flag is the classic stale-response guard — the
  // 3s interval means a response can land after the tab has been switched or
  // the component unmounted, and writing state then would either overwrite
  // fresher data or warn. Both the immediate `load()` and the interval share
  // the flag, and the cleanup clears the interval too.
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
  //
  // AI Note: fetched lazily and exactly once per (tab, id, has_results) — the
  // manifest is not polled, because results only exist after the job has
  // finished uploading its tarball. `has_results` is the gate: without it the
  // endpoint would 404.
  useEffect(() => {
    if (activeTab !== "results" || !id || !detail?.has_results) return;
    let cancelled = false;
    api.getJobResultsManifest(id)
      .then((m) => { if (!cancelled) setResultsManifest(m); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [activeTab, id, detail?.has_results]);

  /**
   * Downloads the whole results tarball.
   *
   * AI Note: delegates to `api.downloadJobResults`, which fetches the bytes
   * with the Bearer token, wraps them in a Blob and clicks a synthetic `<a>`.
   * A plain `<a href>` cannot be used because the endpoint requires the
   * Authorization header. That also means the whole archive is buffered in
   * memory before the save dialog appears — large result sets will spike RAM.
   */
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

  /**
   * Cancels the job (POST /api/jobs/{id}/cancel) and immediately refetches the
   * detail so the header status and the Cancel button's visibility update
   * without waiting for the next poll tick.
   *
   * AI Note: cancellation is asynchronous on the backend — the server marks
   * the job and signals the agent, so the refetched status may still be
   * `running` for a moment. The 3s poll (which keeps running until the status
   * is terminal) is what eventually shows `cancelled`.
   */
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
  // May be undefined if the step list shrank (or is empty) while a later index
  // was selected; the tab bodies all null-guard with `?.` and a placeholder.
  const currentStepData: StepRunInfo | undefined = steps[selectedStep];
  const isActive = ["pending", "queued", "running"].includes(job.status);

  // AI Note: the Results tab is conditionally appended based on
  // `detail.has_results`, so tab *positions* shift between jobs. Anything
  // keying off index rather than `tab.key` will break.
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
              {/* AI Note: `submitted_by` is a user UUID, not a username — it is
                  truncated to 8 chars purely as a compact identifier. There is
                  currently no user lookup to resolve it to a display name. */}
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
                      // AI Note: switching steps also forces the Logs tab.
                      // Params/Outputs are per-step, so leaving the user on
                      // (say) Results after clicking a different step would
                      // look like the click did nothing.
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
                      // AI Note: the terminal log is already in memory, so the
                      // download is done entirely client-side via a Blob URL
                      // rather than re-hitting the API (which would need the
                      // Bearer header anyway). `revokeObjectURL` runs
                      // synchronously after `click()`; this works because the
                      // browser has already captured the blob by then — do not
                      // "fix" it by awaiting anything in between.
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
                      {/* AI Note: unlike the results tarball, artifacts are
                          downloaded with a plain <a href>. That only works if
                          this endpoint accepts cookie/session auth or is
                          unauthenticated — it cannot carry the Bearer token
                          the rest of the client uses. If artifact downloads
                          start 401ing, this is why. */}
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
