/**
 * Nodes.tsx — the node inventory and lifecycle-management page (route `/nodes`).
 *
 * Role in the system:
 *   The operator's view of every agent machine known to the cluster, plus the
 *   admin workflows for getting machines *into* the cluster. It is the only
 *   place in the UI that can:
 *     - provision a brand-new node over SSH (server clones + installs the
 *       agent on the target box),
 *     - register a node "credentials only" and hand back a one-time API key,
 *     - reconnect an offline node over SSH,
 *     - toggle maintenance mode, and delete a node.
 *
 * Data flow:
 *   - `useNodesStore` (`frontend/src/stores/index.ts`) -> GET /api/nodes.
 *   - Mutations via `api` (`frontend/src/api/client.ts`):
 *       POST   /api/nodes                  (register-only)
 *       POST   /api/nodes/provision        (SSH install)
 *       POST   /api/nodes/{id}/reconnect   (SSH restart)
 *       PUT    /api/nodes/{id}/maintenance
 *       DELETE /api/nodes/{id}
 *   - `useAuthStore` gates every destructive/provisioning control behind
 *     `role === "admin"`.
 *
 * Live updates: `<Layout />` owns the `/ws/dashboard` socket; `handleWsMessage`
 * calls `useNodesStore.updateNodeStatus()` on `node.status` frames, so the
 * status dot flips without a refetch. Note that only `status` is patched live —
 * heartbeat/spec columns need a `fetchNodes()` to refresh.
 *
 * File layout: constants -> small presentational helpers -> dialogs
 * (Delete / Reconnect / Register) -> slide-over detail panel -> the exported
 * page component at the bottom.
 */
import { useEffect, useState, useCallback } from "react";
import {
  Server,
  Monitor,
  Terminal,
  Cpu,
  MemoryStick,
  Wifi,
  X,
  Trash2,
  Wrench,
  ChevronDown,
  Loader2,
  Tag,
  Plus,
  Copy,
  Check,
  KeyRound,
  Power,
} from "lucide-react";
import { useNodesStore, useAuthStore } from "@/stores";
import { api } from "@/api/client";
import { formatRelativeTime, cn } from "@/lib/utils";
import type { NodeInfo, OSType, NodeStatus } from "@/types";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Options for the OS filter dropdown. Values (other than the `"all"` sentinel)
 * must match the server's `OSType` enum exactly — they are compared directly
 * against `node.os_type` during client-side filtering.
 */
const OS_OPTIONS: { value: OSType | "all"; label: string }[] = [
  { value: "all", label: "All OS" },
  { value: "macos", label: "macOS" },
  { value: "linux", label: "Linux" },
  { value: "windows", label: "Windows" },
];

/**
 * Options for the status filter dropdown.
 *
 * AI Note: node filtering on this page is entirely client-side (unlike
 * `Jobs.tsx`, which pushes `?status=` to the server). GET /api/nodes returns
 * the full inventory and {@link NodesPage} narrows it in memory, so the count
 * badge reflects the filtered subset, not the cluster total.
 */
const STATUS_OPTIONS: { value: NodeStatus | "all"; label: string }[] = [
  { value: "all", label: "All Status" },
  { value: "online", label: "Online" },
  { value: "offline", label: "Offline" },
  { value: "busy", label: "Busy" },
  { value: "maintenance", label: "Maintenance" },
];

/** Dot colour per node status, used by {@link StatusBadge}. Keys must cover the
 * whole `NodeStatus` union or the dot renders unstyled. */
const STATUS_COLORS: Record<NodeStatus, string> = {
  online: "bg-green-500",
  offline: "bg-gray-400",
  busy: "bg-yellow-500",
  maintenance: "bg-orange-500",
};

/** Label colour per node status (dark-mode aware). Parallel to
 * {@link STATUS_COLORS}; both are indexed by the same key in
 * {@link StatusBadge}, so they must be kept in lockstep. */
const STATUS_TEXT_COLORS: Record<NodeStatus, string> = {
  online: "text-green-600 dark:text-green-400",
  offline: "text-gray-500 dark:text-gray-400",
  busy: "text-yellow-600 dark:text-yellow-400",
  maintenance: "text-orange-600 dark:text-orange-400",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Icon + label for a node's operating system, used in the OS table column.
 *
 * @param os the node's `os_type` as reported by the agent at registration.
 *
 * AI Note: the switch has no `default` branch — it relies on `OSType` being an
 * exhaustive union so TypeScript flags a missing case when a new OS is added.
 * At runtime an unexpected value renders nothing (empty cell) rather than
 * throwing. macOS and Windows deliberately share the `Monitor` glyph; only the
 * label distinguishes them.
 */
function OsIcon({ os }: { os: OSType }) {
  switch (os) {
    case "macos":
      return (
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground" title="macOS">
          <Monitor className="h-3.5 w-3.5" />
          <span>macOS</span>
        </span>
      );
    case "linux":
      return (
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground" title="Linux">
          <Terminal className="h-3.5 w-3.5" />
          <span>Linux</span>
        </span>
      );
    case "windows":
      return (
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground" title="Windows">
          <Monitor className="h-3.5 w-3.5" />
          <span>Windows</span>
        </span>
      );
  }
}

/**
 * Coloured dot + capitalised status word. Used in both the table row and the
 * slide-over detail panel so they can never drift visually.
 *
 * Purely presentational: reads {@link STATUS_COLORS} / {@link STATUS_TEXT_COLORS}
 * and nothing else.
 */
function StatusBadge({ status }: { status: NodeStatus }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn("h-2 w-2 rounded-full", STATUS_COLORS[status])} />
      <span className={cn("text-xs font-medium capitalize", STATUS_TEXT_COLORS[status])}>
        {status}
      </span>
    </span>
  );
}

/**
 * Renders `node.ram_mb` (always megabytes on the wire) as a human string.
 *
 * @param mb total RAM in MB as reported by the agent.
 * @returns e.g. `"512 MB"` or `"16 GB"`.
 *
 * AI Note: uses the binary 1024 divisor and `toFixed(0)`, so 16384 MB shows as
 * "16 GB" but 12500 MB rounds to "12 GB". This is a deliberately lossy display
 * format — never parse it back into a number.
 */
function formatRam(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(0)} GB`;
  return `${mb} MB`;
}

// ---------------------------------------------------------------------------
// Filter Dropdown (lightweight, no Radix dependency required)
// ---------------------------------------------------------------------------

/**
 * Generic single-select dropdown used for the OS and Status filters.
 *
 * Deliberately hand-rolled (no Radix/headless-ui) to keep the bundle small; it
 * is a controlled component, so the parent owns the selected value.
 *
 * @typeParam T the union of allowed values (e.g. `OSType | "all"`).
 * @param options list rendered in the menu; `value` is returned to `onChange`.
 * @param value currently selected value; the matching option's label is shown
 *   on the trigger button, falling back to "Select" if no option matches.
 * @param onChange called with the chosen value, then the menu auto-closes.
 *
 * AI Note: the invisible `fixed inset-0 z-10` div is the click-outside catcher.
 * It must stay *below* the menu (`z-20`) and *above* the rest of the page, or
 * either the menu becomes unclickable or outside clicks stop dismissing it.
 * This is why there is no document-level mousedown listener here.
 *
 * AI Note: no keyboard/ARIA support (no Escape-to-close, no roving focus, no
 * `role="listbox"`). Known gap — do not assume this is accessible.
 */
function FilterDropdown<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  const [open, setOpen] = useState(false);
  const selected = options.find((o) => o.value === value);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-background px-3 py-1.5 text-sm font-medium hover:bg-muted transition-colors"
      >
        {selected?.label ?? "Select"}
        <ChevronDown className="h-3.5 w-3.5 opacity-50" />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute left-0 z-20 mt-1 min-w-[140px] rounded-lg border border-border bg-background shadow-lg">
            {options.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => {
                  onChange(opt.value);
                  setOpen(false);
                }}
                className={cn(
                  "w-full px-3 py-1.5 text-left text-sm hover:bg-muted transition-colors first:rounded-t-lg last:rounded-b-lg",
                  opt.value === value && "bg-muted font-medium"
                )}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Delete Confirmation Dialog
// ---------------------------------------------------------------------------

/**
 * Modal confirmation for node deletion.
 *
 * Stateless — the parent ({@link NodeDetailPanel}) decides when to mount it and
 * supplies both callbacks. `onConfirm` is what actually issues
 * DELETE /api/nodes/{id}; this component performs no I/O and shows no spinner,
 * so the parent is responsible for closing it.
 *
 * @param hostname shown in the prompt so the operator can double-check the
 *   target before destroying it.
 */
function DeleteDialog({
  hostname,
  onConfirm,
  onCancel,
}: {
  hostname: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-sm rounded-xl border border-border bg-background p-6 shadow-xl">
        <h3 className="text-lg font-semibold">Delete Node</h3>
        <p className="mt-2 text-sm text-muted-foreground">
          Are you sure you want to delete <span className="font-medium text-foreground">{hostname}</span>?
          This action cannot be undone.
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700 transition-colors"
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Reconnect (Bring Online) Dialog
// ---------------------------------------------------------------------------

/**
 * "Bring Node Online" dialog — SSHes into an offline node and restarts its
 * agent via POST /api/nodes/{id}/reconnect.
 *
 * What the user sees: a credentials form (host, user, password OR "use the
 * server's SSH key"), then, after submit, a success/warning panel plus the raw
 * setup log streamed back by the server.
 *
 * State machine: form -> (submitting) -> either `error` + `errorLog` shown
 * inline with the form still editable, or `done` set, which swaps the whole
 * body for the result panel. There is no way back to the form once `done` is
 * set; the operator closes and reopens.
 *
 * @param node the offline node being reconnected; supplies the id for the
 *   request and the last-known IP as a default host.
 * @param onClose dismiss the dialog.
 * @param onDone called immediately after a successful request so the parent can
 *   refetch the node list; note it fires *before* the user dismisses the result
 *   panel, so the table updates behind the modal.
 *
 * AI Note: SSH credentials are intentionally never persisted anywhere — they
 * live in component state only, are POSTed to the server to open a one-shot
 * session, and vanish when the dialog unmounts. Do not add localStorage
 * caching here.
 */
function ReconnectDialog({
  node,
  onClose,
  onDone,
}: {
  node: NodeInfo;
  onClose: () => void;
  onDone: () => void;
}) {
  // AI Note: "0.0.0.0" is the placeholder IP written by register-only node
  // creation (see RegisterNodeDialog), not a real address — treating it as a
  // default SSH host would guarantee a failed connection, so it is filtered out.
  const defaultHost = node.ip_address && node.ip_address !== "0.0.0.0" ? node.ip_address : "";
  const [sshHost, setSshHost] = useState(defaultHost);
  const [sshUser, setSshUser] = useState("");
  const [sshPassword, setSshPassword] = useState("");
  const [useServerKey, setUseServerKey] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorLog, setErrorLog] = useState<string[]>([]);
  const [done, setDone] = useState<{ online: boolean; log: string[] } | null>(null);

  // Submit gate: host and user are always required; a password is required
  // unless the operator opted into the server's own SSH key/agent.
  const incomplete = !sshHost.trim() || !sshUser.trim() || (!useServerKey && !sshPassword);

  /**
   * POSTs /api/nodes/{id}/reconnect and renders the outcome.
   *
   * AI Note: `ssh_password` is sent as `undefined` (i.e. omitted from the JSON
   * body) rather than an empty string when `useServerKey` is on — the server
   * distinguishes "no password field" (use key auth) from "empty password".
   *
   * AI Note: `api.reconnectNode` is one of the hand-rolled fetches in
   * `client.ts` that attaches a `log` array to the thrown Error, because a
   * failed SSH provision returns 502 with the install log in `detail.log`.
   * That is why the catch reads `(err as Error & { log?: string[] }).log` —
   * a plain `request<T>()` call would have discarded it.
   *
   * AI Note: `res.online === false` is NOT an error. It means the agent was
   * installed and started but its WebSocket has not dialled back yet (typical
   * with VPN/firewall/asymmetric routing). The agent keeps retrying, so the
   * result panel shows a yellow "still connecting" state instead of red.
   */
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setErrorLog([]);
    try {
      const res = await api.reconnectNode(node.id, {
        ssh_host: sshHost.trim(),
        ssh_user: sshUser.trim(),
        ssh_password: useServerKey ? undefined : sshPassword,
        use_server_key: useServerKey,
      });
      setDone({ online: res.online, log: res.log || [] });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to bring node online");
      setErrorLog(((err as Error & { log?: string[] }).log) || []);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    // AI Note: z-[60] (not z-50) because this dialog is opened *from* the
    // NodeDetailPanel slide-over, which already occupies z-50. Lowering this
    // would render the dialog behind the panel that launched it.
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4">
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-border bg-background p-6 shadow-xl">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold">Bring Node Online</h3>
          <button type="button" onClick={onClose} className="rounded-lg p-1.5 hover:bg-muted transition-colors">
            <X className="h-4 w-4" />
          </button>
        </div>

        {!done ? (
          <form onSubmit={handleSubmit} className="mt-4 space-y-4">
            <p className="text-sm text-muted-foreground">
              Reconnects <span className="font-medium text-foreground">{node.display_name || node.hostname}</span>{" "}
              by SSHing in and restarting its agent (re-picking a reachable address). SSH credentials
              aren't stored, so re-enter them.
            </p>

            {/* AI Note: focus is placed on whichever field is still blank —
                host when there was no usable last-known IP, otherwise user.
                The two `autoFocus` expressions are deliberate inverses; keep
                them mutually exclusive or React focuses the later one. */}
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label htmlFor="rc-host" className="mb-1.5 block text-sm font-medium">SSH host / IP</label>
                <input
                  id="rc-host" type="text" value={sshHost}
                  onChange={(e) => setSshHost(e.target.value)}
                  placeholder="192.168.1.50"
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                  autoFocus={!defaultHost}
                />
              </div>
              <div>
                <label htmlFor="rc-user" className="mb-1.5 block text-sm font-medium">SSH user</label>
                <input
                  id="rc-user" type="text" value={sshUser}
                  onChange={(e) => setSshUser(e.target.value)}
                  placeholder="ubuntu"
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                  autoFocus={!!defaultHost}
                />
              </div>
            </div>

            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <input type="checkbox" checked={useServerKey} onChange={(e) => setUseServerKey(e.target.checked)} className="h-4 w-4" />
              Use the server's SSH key / agent (no password)
            </label>

            {!useServerKey && (
              <div>
                <label htmlFor="rc-pass" className="mb-1.5 block text-sm font-medium">SSH password</label>
                <input
                  id="rc-pass" type="password" value={sshPassword}
                  onChange={(e) => setSshPassword(e.target.value)}
                  placeholder="••••••••" autoComplete="new-password"
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
            )}

            {submitting && (
              <p className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
                Reconnecting over SSH — this can take a moment.
              </p>
            )}
            {error && (
              <div className="rounded-lg bg-red-50 px-3 py-2 dark:bg-red-950">
                <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
                {errorLog.length > 0 && (
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-xs text-red-700/80 dark:text-red-400/80">
                    {errorLog.join("\n")}
                  </pre>
                )}
              </div>
            )}

            <div className="flex justify-end gap-3 pt-2">
              <button type="button" onClick={onClose} className="rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors">
                Cancel
              </button>
              <button
                type="submit"
                disabled={submitting || incomplete}
                className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
              >
                {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
                Bring Online
              </button>
            </div>
          </form>
        ) : (
          <div className="mt-4 space-y-4">
            <p className={cn(
              "rounded-lg px-3 py-2 text-sm",
              done.online
                ? "bg-green-50 text-green-700 dark:bg-green-950/50 dark:text-green-400"
                : "bg-yellow-50 text-yellow-700 dark:bg-yellow-950/50 dark:text-yellow-400"
            )}>
              {done.online
                ? "Agent reconnected — the node is online."
                : "Agent installed + started, but it hasn't connected back yet (the WebSocket isn't completing — likely VPN/firewall or routing). It keeps retrying; see the log."}
            </p>
            {done.log.length > 0 && (
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-muted px-3 py-2 text-xs">
                {done.log.join("\n")}
              </pre>
            )}
            <div className="flex justify-end pt-2">
              <button type="button" onClick={onClose} className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors">
                Done
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Node Detail Panel (slide-over)
// ---------------------------------------------------------------------------

/**
 * Right-hand slide-over showing everything known about one node, plus its
 * lifecycle actions.
 *
 * What the user sees: header with display name, a status badge, a spec
 * definition list (OS, arch, CPU, RAM, GPU, IP, agent version, registration
 * date, last heartbeat), tag chips, and an Actions block.
 *
 * Action visibility:
 *   - "Bring Online" — admins only, and only when the node is `offline`.
 *   - "Enable/Disable Maintenance" — visible to every authenticated user.
 *   - "Delete Node" — admins only.
 *
 * AI Note: these are UI affordances, not authorisation. The server re-checks
 * the caller's role on every one of these endpoints; hiding a button here must
 * never be treated as the security boundary.
 *
 * @param node the currently selected node. The parent keeps this object in sync
 *   with the store after refetches, so the panel re-renders with fresh data.
 * @param onClose closes the slide-over.
 * @param onRefresh refetches the node list; called after maintenance toggles,
 *   deletes, and successful reconnects.
 */
function NodeDetailPanel({
  node,
  onClose,
  onRefresh,
}: {
  node: NodeInfo;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin";
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [reconnectOpen, setReconnectOpen] = useState(false);

  /**
   * Flips the node in/out of maintenance via PUT /api/nodes/{id}/maintenance.
   *
   * AI Note: the desired state is derived as `node.status !== "maintenance"`,
   * i.e. any non-maintenance status (including `offline`) means "turn it on".
   * There is no separate persisted maintenance flag read here — status is the
   * single source of truth, so a node that goes offline while in maintenance
   * will show "Enable Maintenance" again.
   *
   * Failures are only logged to the console; the panel shows no error UI, and
   * `onRefresh()` is skipped so the stale status stays on screen.
   */
  const toggleMaintenance = async () => {
    setMaintenanceLoading(true);
    try {
      const enable = node.status !== "maintenance";
      await api.setMaintenance(node.id, enable);
      onRefresh();
    } catch (err) {
      console.error("Failed to toggle maintenance:", err);
    } finally {
      setMaintenanceLoading(false);
    }
  };

  /**
   * Permanently removes the node: DELETE /api/nodes/{id}, refetch, then close
   * the slide-over (order matters — closing first would unmount before the
   * refresh is issued).
   *
   * AI Note: `deleteTarget` is never cleared on the success path; the panel
   * unmounts via `onClose()` instead, which tears down that state. On failure
   * the confirmation dialog stays open with no error shown (console only), so
   * a repeated click just retries.
   */
  const handleDelete = async () => {
    try {
      await api.deleteNode(node.id);
      onRefresh();
      onClose();
    } catch (err) {
      console.error("Failed to delete node:", err);
    }
  };

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/30" onClick={onClose} />
      <div className="fixed inset-y-0 right-0 z-50 w-full max-w-md overflow-y-auto border-l border-border bg-background shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <div className="flex items-center gap-3">
            <Server className="h-5 w-5 text-muted-foreground" />
            <h2 className="text-lg font-semibold">{node.display_name || node.hostname}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 hover:bg-muted transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-6 px-6 py-6">
          {/* Status */}
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Status
            </h3>
            <StatusBadge status={node.status} />
          </div>

          {/* Specs */}
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Specifications
            </h3>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <dt className="text-muted-foreground">OS</dt>
              <dd>{node.os_type} {node.os_version}</dd>

              <dt className="text-muted-foreground">Architecture</dt>
              <dd>{node.arch}</dd>

              <dt className="text-muted-foreground flex items-center gap-1">
                <Cpu className="h-3.5 w-3.5" /> CPU
              </dt>
              <dd>{node.cpu_model} ({node.cpu_cores} cores)</dd>

              <dt className="text-muted-foreground flex items-center gap-1">
                <MemoryStick className="h-3.5 w-3.5" /> RAM
              </dt>
              <dd>{formatRam(node.ram_mb)}</dd>

              {node.gpu_info && (
                <>
                  <dt className="text-muted-foreground">GPU</dt>
                  <dd>{node.gpu_info}</dd>
                </>
              )}

              <dt className="text-muted-foreground flex items-center gap-1">
                <Wifi className="h-3.5 w-3.5" /> IP
              </dt>
              <dd className="font-mono text-xs">{node.ip_address}</dd>

              <dt className="text-muted-foreground">Agent</dt>
              <dd className="font-mono text-xs">{node.agent_version}</dd>

              <dt className="text-muted-foreground">Registered</dt>
              <dd>{new Date(node.registered_at).toLocaleDateString()}</dd>

              {node.last_heartbeat && (
                <>
                  <dt className="text-muted-foreground">Last Heartbeat</dt>
                  <dd>{formatRelativeTime(node.last_heartbeat)}</dd>
                </>
              )}
            </dl>
          </div>

          {/* Tags */}
          {node.tags.length > 0 && (
            <div>
              <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                <Tag className="h-3.5 w-3.5" /> Tags
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {node.tags.map((tag) => (
                  <span
                    key={tag}
                    className="inline-flex items-center rounded-full border border-border px-2.5 py-0.5 text-xs"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Actions */}
          <div className="space-y-3 pt-2">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Actions
            </h3>

            {isAdmin && node.status === "offline" && (
              <button
                type="button"
                onClick={() => setReconnectOpen(true)}
                className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                <Power className="h-4 w-4" />
                Bring Online
              </button>
            )}

            <button
              type="button"
              onClick={toggleMaintenance}
              disabled={maintenanceLoading}
              className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors disabled:opacity-50"
            >
              {maintenanceLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Wrench className="h-4 w-4" />
              )}
              {node.status === "maintenance" ? "Disable Maintenance" : "Enable Maintenance"}
            </button>

            {isAdmin && (
              <button
                type="button"
                onClick={() => setDeleteTarget(node.id)}
                className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-red-300 px-4 py-2 text-sm font-medium text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950 transition-colors"
              >
                <Trash2 className="h-4 w-4" />
                Delete Node
              </button>
            )}
          </div>
        </div>
      </div>

      {deleteTarget && (
        <DeleteDialog
          hostname={node.hostname}
          onConfirm={handleDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}

      {reconnectOpen && (
        <ReconnectDialog
          node={node}
          onClose={() => setReconnectOpen(false)}
          onDone={onRefresh}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Copyable code row
// ---------------------------------------------------------------------------

/**
 * A read-only `<code>` block with a copy-to-clipboard button.
 *
 * Used for the one-time node API key and the generated `nexus-agent run`
 * command in {@link RegisterNodeDialog}.
 *
 * State: `copied` drives a checkmark that reverts to the copy icon after
 * 1500 ms.
 *
 * AI Note: `navigator.clipboard` requires a secure context — over plain HTTP
 * on a non-localhost origin the write rejects, the catch swallows it, and the
 * icon simply never flips to a checkmark. The value stays selectable in the
 * `<code>` element so manual copy still works. Also note the timeout is not
 * cleared on unmount; harmless here (the setState no-ops on an unmounted
 * component in React 18) but do not copy this pattern into long-lived UI.
 */
function CopyRow({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };
  return (
    <div className="flex items-stretch gap-2">
      <code className="flex-1 overflow-x-auto whitespace-pre rounded-lg border border-border bg-muted px-3 py-2 font-mono text-xs">
        {value}
      </code>
      <button
        type="button"
        onClick={copy}
        title="Copy"
        className="shrink-0 rounded-lg border border-border px-2.5 hover:bg-muted transition-colors"
      >
        {copied ? <Check className="h-3.5 w-3.5 text-green-500" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Register Node Dialog (admin only)
// ---------------------------------------------------------------------------

/**
 * "Add Node" dialog (admin only) — the entry point for both onboarding paths.
 *
 * Two modes, toggled by the `setupSsh` checkbox at the top:
 *
 *   1. **Provision over SSH** (default). POST /api/nodes/provision. The server
 *      SSHes into the target box, clones the agent from GitHub, installs it
 *      (optionally installing Python 3.12 via Homebrew and/or registering a
 *      boot service) and starts it. On success the dialog shows the setup log
 *      and whether the agent has connected back yet.
 *   2. **Register only**. POST /api/nodes. Creates the DB row and returns a
 *      one-time `api_key`, which is displayed together with a ready-to-paste
 *      `nexus-agent run ...` command for the operator to run manually.
 *
 * @param onCreated called right after either request succeeds so the parent can
 *   refetch the node table (fires before the operator dismisses the dialog).
 * @param onClose dismisses the dialog. In register-only mode this is the point
 *   of no return for the API key — see the note on `result.api_key` below.
 */
function RegisterNodeDialog({
  onCreated,
  onClose,
}: {
  onCreated: () => void;
  onClose: () => void;
}) {
  const [setupSsh, setSetupSsh] = useState(true); // default: provision over SSH
  const [name, setName] = useState("");
  const [tags, setTags] = useState("");
  const [osType, setOsType] = useState<OSType>("linux"); // register-only mode

  // SSH provisioning fields (mode 1). Never persisted — see ReconnectDialog.
  const [sshHost, setSshHost] = useState("");
  const [sshUser, setSshUser] = useState("");
  const [sshPassword, setSshPassword] = useState("");
  const [useServerKey, setUseServerKey] = useState(false);
  const [service, setService] = useState(false);
  const [installPython, setInstallPython] = useState(true);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorLog, setErrorLog] = useState<string[]>([]);
  // AI Note: `result` is the union of both endpoints' response shapes; the
  // SSH-only fields (ws_url/mode/online/log) are optional because the
  // register-only path never returns them. `provisioned` — not the presence of
  // those fields — is what selects which success panel renders.
  const [result, setResult] = useState<
    (NodeInfo & { api_key: string; ws_url?: string; mode?: string; online?: boolean; log?: string[] }) | null
  >(null);
  const [provisioned, setProvisioned] = useState(false);

  /** Splits the comma-separated tag input into a clean string array, dropping
   * blanks so `"a, ,b,"` becomes `["a", "b"]`. */
  const tagList = () => tags.split(",").map((t) => t.trim()).filter(Boolean);

  /**
   * Runs whichever onboarding path the `setupSsh` toggle selected and stores
   * the response in `result`, which switches the dialog into its success view.
   *
   * AI Note: like {@link ReconnectDialog}, the catch pulls `.log` off the Error
   * because `api.provisionNode` attaches the partial install log from a 502
   * body. A generic `request<T>()` error would have `log === undefined`, hence
   * the `|| []` fallback.
   */
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setErrorLog([]);
    try {
      if (setupSsh) {
        const res = await api.provisionNode({
          ssh_host: sshHost.trim(),
          ssh_user: sshUser.trim(),
          ssh_password: useServerKey ? undefined : sshPassword,
          use_server_key: useServerKey,
          display_name: name.trim() || undefined,
          tags: tagList(),
          service,
          install_python: installPython,
        });
        setResult(res);
        setProvisioned(true);
        onCreated();
      } else {
        // Register only — hardware fields are placeholders; the agent reports
        // its real specs on connect. Only display_name (+ tags) persist.
        //
        // AI Note: these literals are not arbitrary. `hostname: "pending"`,
        // `cpu_model: "pending"` and `ip_address: "0.0.0.0"` are the sentinels
        // the rest of the UI keys off — e.g. ReconnectDialog refuses to
        // pre-fill an SSH host of "0.0.0.0". The POST body must satisfy the
        // server's required-field validation, which is why they exist at all.
        const created = await api.createNode({
          hostname: name.trim() || "pending",
          display_name: name.trim() || undefined,
          os_type: osType,
          os_version: "unknown",
          arch: "unknown",
          cpu_model: "pending",
          cpu_cores: 1,
          ram_mb: 1024,
          agent_version: "0.1.0",
          ip_address: "0.0.0.0",
          capabilities: {},
          tags: tagList(),
        });
        setResult(created);
        setProvisioned(false);
        onCreated();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
      setErrorLog(((err as Error & { log?: string[] }).log) || []);
    } finally {
      setSubmitting(false);
    }
  };

  // AI Note: the suggested agent command is assembled from the *browser's*
  // hostname and a hard-coded port 8000 — the server never tells us its own
  // externally reachable address. If the dashboard is behind a reverse proxy,
  // on a different port, or the agent sits on another network segment, this
  // command is wrong and the operator must edit it (the UI says so below).
  const wsHost = window.location.hostname || "localhost";
  const runCmd = result
    ? `nexus-agent run --server ws://${wsHost}:8000/ws/agent/${result.id} --api-key ${result.api_key} --node-id ${result.id}`
    : "";

  // Same submit gate as ReconnectDialog; only enforced when in SSH mode.
  const sshIncomplete = !sshHost.trim() || !sshUser.trim() || (!useServerKey && !sshPassword);
  const title = result
    ? (provisioned ? "Node Set Up" : "Node Registered")
    : "Add Node";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-border bg-background p-6 shadow-xl">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 hover:bg-muted transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {!result ? (
          <form onSubmit={handleSubmit} className="mt-4 space-y-4">
            {/* Mode toggle */}
            <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-border p-3">
              <input
                type="checkbox"
                checked={setupSsh}
                onChange={(e) => setSetupSsh(e.target.checked)}
                className="mt-0.5 h-4 w-4"
              />
              <span className="text-sm">
                <span className="font-medium">Set up the device over SSH</span>
                <span className="block text-xs text-muted-foreground">
                  The server connects to the device, clones the agent from GitHub, installs and
                  starts it. Uncheck to just register and get the run command.
                </span>
              </span>
            </label>

            <div>
              <label htmlFor="node-name" className="mb-1.5 block text-sm font-medium">
                Display Name <span className="font-normal text-muted-foreground">(optional)</span>
              </label>
              <input
                id="node-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={setupSsh ? "defaults to the host" : "e.g. lab-box-1"}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                autoFocus
              />
            </div>

            {setupSsh ? (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="ssh-host" className="mb-1.5 block text-sm font-medium">SSH host / IP</label>
                    <input
                      id="ssh-host" type="text" value={sshHost}
                      onChange={(e) => setSshHost(e.target.value)}
                      placeholder="192.168.1.50"
                      className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                    />
                  </div>
                  <div>
                    <label htmlFor="ssh-user" className="mb-1.5 block text-sm font-medium">SSH user</label>
                    <input
                      id="ssh-user" type="text" value={sshUser}
                      onChange={(e) => setSshUser(e.target.value)}
                      placeholder="ubuntu"
                      className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                    />
                  </div>
                </div>

                <label className="flex cursor-pointer items-center gap-2 text-sm">
                  <input
                    type="checkbox" checked={useServerKey}
                    onChange={(e) => setUseServerKey(e.target.checked)}
                    className="h-4 w-4"
                  />
                  Use the server's SSH key / agent (no password)
                </label>

                {!useServerKey && (
                  <div>
                    <label htmlFor="ssh-pass" className="mb-1.5 block text-sm font-medium">SSH password</label>
                    <input
                      id="ssh-pass" type="password" value={sshPassword}
                      onChange={(e) => setSshPassword(e.target.value)}
                      placeholder="••••••••"
                      autoComplete="new-password"
                      className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                    />
                    <p className="mt-1 text-xs text-muted-foreground">
                      Sent to the Nexus server to open the SSH session; not stored.
                    </p>
                  </div>
                )}

                <div className="space-y-2 rounded-lg border border-border p-3">
                  <label className="flex cursor-pointer items-center gap-2 text-sm">
                    <input type="checkbox" checked={service} onChange={(e) => setService(e.target.checked)} className="h-4 w-4" />
                    Auto-start on boot (install a service)
                    <span className="text-xs text-muted-foreground">— otherwise runs in the background</span>
                  </label>
                  <label className="flex cursor-pointer items-center gap-2 text-sm">
                    <input type="checkbox" checked={installPython} onChange={(e) => setInstallPython(e.target.checked)} className="h-4 w-4" />
                    Install Python 3.12 via Homebrew if missing
                  </label>
                </div>
              </>
            ) : (
              <div>
                <label htmlFor="node-os" className="mb-1.5 block text-sm font-medium">OS (provisional)</label>
                <select
                  id="node-os" value={osType}
                  onChange={(e) => setOsType(e.target.value as OSType)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  <option value="linux">Linux</option>
                  <option value="macos">macOS</option>
                  <option value="windows">Windows</option>
                </select>
              </div>
            )}

            <div>
              <label htmlFor="node-tags" className="mb-1.5 block text-sm font-medium">
                Tags <span className="font-normal text-muted-foreground">(optional, comma-separated)</span>
              </label>
              <input
                id="node-tags" type="text" value={tags}
                onChange={(e) => setTags(e.target.value)}
                placeholder="e.g. gpu, builder"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>

            {submitting && setupSsh && (
              <p className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
                Setting up over SSH — this can take a minute (longer if Python must be installed).
              </p>
            )}

            {error && (
              <div className="rounded-lg bg-red-50 px-3 py-2 dark:bg-red-950">
                <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
                {errorLog.length > 0 && (
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-xs text-red-700/80 dark:text-red-400/80">
                    {errorLog.join("\n")}
                  </pre>
                )}
              </div>
            )}

            <div className="flex justify-end gap-3 pt-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={submitting || (setupSsh && sshIncomplete)}
                className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
              >
                {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
                {setupSsh ? "Set Up Node" : "Register Node"}
              </button>
            </div>
          </form>
        ) : provisioned ? (
          <div className="mt-4 space-y-4">
            {result.online ? (
              <p className="rounded-lg bg-green-50 px-3 py-2 text-sm text-green-700 dark:bg-green-950/50 dark:text-green-400">
                <span className="font-medium">{result.display_name || result.hostname}</span> was set up
                over SSH and is <span className="font-medium">online</span>
                {result.mode === "service" ? " (auto-start service)" : " (background)"}.
              </p>
            ) : (
              <p className="rounded-lg bg-yellow-50 px-3 py-2 text-sm text-yellow-700 dark:bg-yellow-950/50 dark:text-yellow-400">
                <span className="font-medium">{result.display_name || result.hostname}</span> was installed
                and started, but the agent <span className="font-medium">hasn't connected back yet</span>.
                The device can reach the server over HTTP but the WebSocket isn't completing — usually a
                VPN/firewall or asymmetric routing between server and device. It keeps retrying; see the log.
              </p>
            )}
            {result.log && result.log.length > 0 && (
              <div>
                <div className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Setup log
                </div>
                <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-muted px-3 py-2 text-xs">
                  {result.log.join("\n")}
                </pre>
              </div>
            )}
            <div className="flex justify-end pt-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                Done
              </button>
            </div>
          </div>
        ) : (
          <div className="mt-4 space-y-4">
            <p className="text-sm text-muted-foreground">
              Node <span className="font-medium text-foreground">{result.display_name || result.hostname}</span>{" "}
              is registered. It will appear <span className="font-medium text-foreground">offline</span> until
              an agent connects with the credentials below.
            </p>

            {/* AI Note: security-sensitive — `result.api_key` is the plaintext
                node key and the server only ever returns it in this one
                create response (it stores a hash). Closing this dialog loses
                it permanently; the only recovery is deleting and re-creating
                the node. Do not add logging/telemetry around this value. */}
            <div>
              <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                <KeyRound className="h-3.5 w-3.5" /> API Key
              </div>
              <CopyRow value={result.api_key} />
              <p className="mt-1.5 rounded-lg bg-yellow-50 px-3 py-2 text-xs text-yellow-700 dark:bg-yellow-950/50 dark:text-yellow-400">
                Copy this now — it is shown only once and cannot be retrieved later.
              </p>
            </div>

            <div>
              <div className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Run on the target machine
              </div>
              <CopyRow value={runCmd} />
              <p className="mt-1.5 text-xs text-muted-foreground">
                Server host defaults to <code className="font-mono">{wsHost}:8000</code> — change it if the
                agent reaches this server at a different address.
              </p>
            </div>

            <div className="flex justify-end pt-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                Done
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

/**
 * Node inventory page (route `/nodes`).
 *
 * What the user sees: a header with the filtered node count, two filter
 * dropdowns (OS, status), an admin-only "Add Node" button, and a table of
 * nodes (hostname, OS, status, arch, CPU, RAM, IP, last heartbeat). Clicking a
 * row opens the {@link NodeDetailPanel} slide-over. Empty state offers a
 * "Register your first node" shortcut when the cluster is genuinely empty
 * (not merely filtered to nothing).
 *
 * Side effects: GET /api/nodes on mount and on every explicit refresh. All
 * mutations live in the child dialogs/panel.
 *
 * Props: none — routed component.
 */
export default function NodesPage() {
  const { nodes, isLoading, fetch: fetchNodes } = useNodesStore();
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin";
  const [osFilter, setOsFilter] = useState<OSType | "all">("all");
  const [statusFilter, setStatusFilter] = useState<NodeStatus | "all">("all");
  const [selectedNode, setSelectedNode] = useState<NodeInfo | null>(null);
  const [registerOpen, setRegisterOpen] = useState(false);

  useEffect(() => {
    fetchNodes();
  }, [fetchNodes]);

  /** Reload the node list. Passed to every child that mutates a node so the
   * table (and, via the effect below, the open slide-over) picks up the change.
   * Wrapped in `useCallback` to keep child prop identity stable. */
  const refreshAndReselect = useCallback(async () => {
    await fetchNodes();
  }, [fetchNodes]);

  // Keep selected node in sync after refresh
  //
  // AI Note: `selectedNode` holds a *snapshot* object, not an id, so after a
  // refetch it would otherwise show stale specs/status. This effect re-resolves
  // it against the fresh store array and closes the panel when the node is gone
  // (e.g. just deleted).
  //
  // AI Note: `selectedNode` is in the dependency array while the effect also
  // calls `setSelectedNode` — this only terminates because `nodes.find` returns
  // the same object reference on the next pass, so React bails out of the
  // re-render. If this store ever starts returning freshly-constructed node
  // objects on every read, this becomes an infinite render loop.
  useEffect(() => {
    if (selectedNode) {
      const updated = nodes.find((n) => n.id === selectedNode.id);
      if (updated) setSelectedNode(updated);
      else setSelectedNode(null);
    }
  }, [nodes, selectedNode]);

  // Client-side filtering (see the note on STATUS_OPTIONS): the server returns
  // every node and both dropdowns narrow the list in memory.
  const filtered = nodes.filter((node) => {
    if (osFilter !== "all" && node.os_type !== osFilter) return false;
    if (statusFilter !== "all" && node.status !== statusFilter) return false;
    return true;
  });

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <Server className="h-6 w-6 text-muted-foreground" />
          <h1 className="text-2xl font-bold tracking-tight">Nodes</h1>
          <span className="rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium text-muted-foreground">
            {filtered.length}
          </span>
        </div>

        {/* Filter controls */}
        <div className="flex items-center gap-2">
          <FilterDropdown options={OS_OPTIONS} value={osFilter} onChange={setOsFilter} />
          <FilterDropdown options={STATUS_OPTIONS} value={statusFilter} onChange={setStatusFilter} />
          {isAdmin && (
            <button
              type="button"
              onClick={() => setRegisterOpen(true)}
              className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
            >
              <Plus className="h-4 w-4" />
              Add Node
            </button>
          )}
        </div>
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="flex items-center justify-center py-20">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border py-16 text-center">
          <Server className="mx-auto h-10 w-10 text-muted-foreground/50" />
          <p className="mt-3 text-sm text-muted-foreground">No nodes found</p>
          {/* AI Note: the `nodes.length === 0` guard distinguishes "the cluster
              is empty" from "your filters matched nothing" — offering
              "Register your first node" in the latter case would be
              misleading. */}
          {isAdmin && nodes.length === 0 && (
            <button
              type="button"
              onClick={() => setRegisterOpen(true)}
              className="mt-4 inline-flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors"
            >
              <Plus className="h-4 w-4" />
              Register your first node
            </button>
          )}
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/50">
                  <th className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Hostname
                  </th>
                  <th className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    OS
                  </th>
                  <th className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Status
                  </th>
                  <th className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Arch
                  </th>
                  <th className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    CPU
                  </th>
                  <th className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    RAM
                  </th>
                  <th className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    IP
                  </th>
                  <th className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Last Heartbeat
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filtered.map((node) => (
                  <tr
                    key={node.id}
                    onClick={() => setSelectedNode(node)}
                    className="cursor-pointer transition-colors hover:bg-muted/50"
                  >
                    <td className="whitespace-nowrap px-4 py-3 font-medium">
                      {node.display_name || node.hostname}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3">
                      <OsIcon os={node.os_type} />
                    </td>
                    <td className="whitespace-nowrap px-4 py-3">
                      <StatusBadge status={node.status} />
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">
                      {node.arch}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">
                      {node.cpu_model}
                      <span className="ml-1 text-xs opacity-70">({node.cpu_cores}c)</span>
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">
                      {formatRam(node.ram_mb)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-muted-foreground">
                      {node.ip_address}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">
                      {node.last_heartbeat ? formatRelativeTime(node.last_heartbeat) : "--"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Slide-over detail panel */}
      {selectedNode && (
        <NodeDetailPanel
          node={selectedNode}
          onClose={() => setSelectedNode(null)}
          onRefresh={refreshAndReselect}
        />
      )}

      {/* Register node dialog */}
      {registerOpen && (
        <RegisterNodeDialog
          onCreated={refreshAndReselect}
          onClose={() => setRegisterOpen(false)}
        />
      )}
    </div>
  );
}
