/**
 * Storage.tsx — artifact storage backend administration (route `/storage`).
 *
 * Role in the system:
 *   Job artifacts (build outputs, gem5 result tarballs, etc.) are persisted to
 *   pluggable storage backends — MinIO, an S3 bucket, a NAS mount, or Google
 *   Drive. This page is the operator UI for registering those backends,
 *   health-checking them, deleting them, and reviewing artifact transfers
 *   between them.
 *
 * Data flow:
 *   - `useStorageStore`      -> GET /api/storage/backends
 *   - `useCredentialsStore`  -> GET /api/credentials (populates the credential
 *     picker; the actual secrets stay server-side and are only referenced by id)
 *   - Transfers are held in local state, not a store:
 *     GET /api/storage/transfers
 *   - Mutations via `api` (`frontend/src/api/client.ts`):
 *       POST   /api/storage/backends
 *       DELETE /api/storage/backends/{id}
 *       GET    /api/storage/backends/{id}/health
 *
 * Neighbours: credentials themselves are managed on `Admin.tsx`; the backend
 * chosen here is what `JobDetail.tsx` ultimately downloads results from.
 *
 * AI Note: nothing on this page auto-refreshes. Health results, backend usage
 * and the transfer table are all point-in-time snapshots taken on mount or on
 * explicit user action — there is no polling and no WebSocket channel for
 * storage events.
 */
import { useEffect, useState, useCallback } from "react";
import {
  Plus,
  Loader2,
  Trash2,
  Activity,
  Star,
  HardDrive,
  Cloud,
  Server,
  FolderSync,
  X,
} from "lucide-react";
import { useStorageStore, useCredentialsStore } from "@/stores";
import { api } from "@/api/client";
import { cn, formatBytes } from "@/lib/utils";
import type { StorageBackendInfo, TransferInfo } from "@/types";

/**
 * Icon + colour per backend type, keyed by the LOWERCASED `backend_type`
 * string the server returns.
 *
 * AI Note: the keys must stay lowercase — {@link typeBadge} looks them up with
 * `.toLowerCase()`. Adding a backend type on the server without a matching key
 * here is safe (it falls back to a neutral drive icon) but visually
 * indistinguishable from the other unknown types. The `Add Storage Backend`
 * form's `<option>` values are the authoritative list of what the UI can
 * create.
 */
const BACKEND_TYPE_META: Record<string, { icon: React.ElementType; color: string }> = {
  minio: { icon: HardDrive, color: "bg-orange-100 text-orange-700" },
  nas: { icon: Server, color: "bg-purple-100 text-purple-700" },
  s3: { icon: Cloud, color: "bg-blue-100 text-blue-700" },
  gdrive: { icon: FolderSync, color: "bg-green-100 text-green-700" },
};

/**
 * Renders the type pill (icon + label) for a storage backend card.
 *
 * @param backendType raw `backend_type` from the API; matched
 *   case-insensitively against {@link BACKEND_TYPE_META}, with an unknown type
 *   falling back to a neutral drive icon rather than crashing.
 * @returns JSX, not a component — inlined directly into the card.
 */
function typeBadge(backendType: string) {
  const meta = BACKEND_TYPE_META[backendType.toLowerCase()] ?? {
    icon: HardDrive,
    color: "bg-secondary text-muted-foreground",
  };
  const Icon = meta.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium capitalize",
        meta.color
      )}
    >
      <Icon className="h-3 w-3" />
      {backendType}
    </span>
  );
}

/**
 * Renders the coloured status pill for one row of the transfers table.
 *
 * @param status server-side transfer status: `pending`, `in_progress`,
 *   `completed` or `failed`.
 *
 * AI Note: the label is `status.replace("_", " ")` (first underscore only), so
 * `in_progress` displays as "in progress". A future status with two
 * underscores would only have the first one replaced.
 */
function transferStatusBadge(status: string) {
  const colors: Record<string, string> = {
    pending: "bg-yellow-100 text-yellow-700",
    in_progress: "bg-blue-100 text-blue-700",
    completed: "bg-green-100 text-green-700",
    failed: "bg-red-100 text-red-700",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize",
        colors[status] ?? "bg-secondary text-muted-foreground"
      )}
    >
      {status.replace("_", " ")}
    </span>
  );
}

/**
 * Usage meter for a storage backend card.
 *
 * Two rendering modes:
 *   - No declared capacity (`capacity_bytes` null/0): a single text line
 *     "Used: X / Unknown capacity" with no bar, because a percentage would be
 *     meaningless.
 *   - Known capacity: a used/total line plus a bar that turns yellow above 70%
 *     and red above 90%.
 *
 * @param backend the backend whose `used_bytes` / `capacity_bytes` are shown.
 *
 * AI Note: the falsy check on `capacity_bytes` also catches an explicit 0,
 * which is intentional — a zero-capacity backend would otherwise divide by
 * zero and produce `Infinity`/`NaN` in the width style.
 *
 * AI Note: 70 and 90 are the warn/critical thresholds and `Math.min(100, ...)`
 * clamps over-provisioned backends so the bar can never overflow its track.
 */
function CapacityBar({ backend }: { backend: StorageBackendInfo }) {
  if (!backend.capacity_bytes) {
    return (
      <div className="text-xs text-muted-foreground">
        Used: {formatBytes(backend.used_bytes)} / Unknown capacity
      </div>
    );
  }
  const pct = Math.min(
    100,
    Math.round((backend.used_bytes / backend.capacity_bytes) * 100)
  );
  const barColor = pct > 90 ? "bg-red-500" : pct > 70 ? "bg-yellow-500" : "bg-green-500";
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {formatBytes(backend.used_bytes)} / {formatBytes(backend.capacity_bytes)}
        </span>
        <span>{pct}%</span>
      </div>
      <div className="h-2 w-full rounded-full bg-muted">
        <div
          className={cn("h-2 rounded-full transition-all", barColor)}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

/**
 * Storage administration page.
 *
 * What the user sees:
 *   1. Header with an "Add Backend" button that opens a modal form.
 *   2. A responsive grid of backend cards: name, "Default" star, type pill, an
 *      optional health dot (only after the user clicks Test), a
 *      {@link CapacityBar}, the scheduling `priority`, and Test/Delete buttons.
 *   3. A "Recent Transfers" table (artifact, source -> dest, status, bytes
 *      moved, error).
 *   4. The Add Backend modal, mounted only while `showAddDialog` is true.
 *
 * State groups:
 *   - Backends/credentials come from Zustand stores; `transfers` is local.
 *   - `healthResults` maps backend id -> healthy boolean. A key being
 *     *absent* means "never tested" (no dot is rendered), which is why the
 *     card checks `health !== undefined` rather than truthiness.
 *   - `testingId` / `deletingId` hold the id of the row with an in-flight
 *     request so only that row shows a spinner.
 *   - The `form*` fields are the Add Backend modal's controlled inputs; they
 *     live here rather than in a child component, so they persist if the modal
 *     is closed and reopened without a successful submit.
 *
 * Props: none — routed component.
 */
export default function Storage() {
  const { backends, isLoading, fetch } = useStorageStore();
  const { credentials, fetch: fetchCreds } = useCredentialsStore();
  const [transfers, setTransfers] = useState<TransferInfo[]>([]);
  const [transfersLoading, setTransfersLoading] = useState(false);
  const [healthResults, setHealthResults] = useState<Record<string, boolean | null>>({});
  const [testingId, setTestingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [showAddDialog, setShowAddDialog] = useState(false);

  // Add backend form state
  const [formName, setFormName] = useState("");
  const [formType, setFormType] = useState("minio");
  const [formConfig, setFormConfig] = useState("{}");
  const [formCredential, setFormCredential] = useState("");
  const [formCapacity, setFormCapacity] = useState("");
  const [formDefault, setFormDefault] = useState(false);
  const [formSubmitting, setFormSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Mount-only load of backends, credentials and transfers.
  //
  // AI Note: transfers are fetched with a raw promise chain (not through a
  // store) and the `.catch(() => {})` deliberately swallows failures — the
  // transfers endpoint is secondary, and a failure there must not blank the
  // backend cards. The consequence is a silent empty table if it 500s.
  useEffect(() => {
    fetch();
    fetchCreds();
    setTransfersLoading(true);
    api
      .listTransfers()
      .then(setTransfers)
      .catch(() => {})
      .finally(() => setTransfersLoading(false));
  }, [fetch, fetchCreds]);

  /**
   * Runs an on-demand reachability probe against one backend
   * (GET /api/storage/backends/{id}/health) and records the result in
   * `healthResults`, which lights the green/red dot on that card.
   *
   * AI Note: a thrown request (network error, 5xx) is recorded as
   * `healthy: false` — i.e. "could not verify" is presented to the operator as
   * "unhealthy". That conflation is deliberate for an ops dashboard, but it
   * means a red dot does not distinguish "backend is down" from "the Nexus
   * server could not run the check".
   *
   * Results are never cleared, so a stale green dot can persist after a
   * backend goes down; re-click Test to refresh.
   */
  const handleTest = useCallback(async (id: string) => {
    setTestingId(id);
    try {
      const res = await api.checkBackendHealth(id);
      setHealthResults((prev) => ({ ...prev, [id]: res.healthy }));
    } catch {
      setHealthResults((prev) => ({ ...prev, [id]: false }));
    } finally {
      setTestingId(null);
    }
  }, []);

  /**
   * Deletes a storage backend after a native `confirm()` prompt, then reloads
   * the backend list.
   *
   * AI Note: this uses the browser's blocking `window.confirm` rather than the
   * custom modal used elsewhere in the app (Jobs, Nodes, Pools). It is the odd
   * one out; if this page gains a design pass, this is the thing to replace.
   *
   * AI Note: deleting a backend does not delete the artifacts stored on it —
   * the server only drops the registration. Existing artifacts pointing at the
   * removed backend become undownloadable.
   *
   * Errors are swallowed (the api client already redirects on 401); the list
   * simply refetches unchanged.
   */
  const handleDelete = useCallback(async (id: string) => {
    if (!confirm("Delete this storage backend? This cannot be undone.")) return;
    setDeletingId(id);
    try {
      await api.deleteBackend(id);
      await useStorageStore.getState().fetch();
    } catch {
      // handled by api client
    } finally {
      setDeletingId(null);
    }
  }, []);

  /**
   * Submits the Add Backend modal: parses the free-form JSON config, POSTs
   * /api/storage/backends, refetches the list and resets the form.
   *
   * The `config` blob is backend-type specific (endpoint/bucket for MinIO and
   * S3, a mount path for NAS, and so on) and is passed through to the server
   * verbatim — the UI does no schema validation beyond "is it JSON".
   *
   * AI Note: the JSON parse failure path returns early *after* manually
   * clearing `formSubmitting`, because it sits before the try/finally that
   * would otherwise do it. Any new early return added here must do the same or
   * the submit button stays disabled forever.
   *
   * AI Note: `credential_id` is sent as `undefined` (omitted) when "None" is
   * selected, and `capacity_bytes` as explicit `null` when blank — two
   * different "absent" encodings that the server treats the same way. Do not
   * "normalise" one to the other without checking the API schema.
   *
   * AI Note: setting `is_default` on a new backend implicitly demotes whatever
   * backend was default before; that reassignment happens server-side, which
   * is why the local list is refetched rather than patched.
   *
   * The form is only reset on success, so a failed submit preserves the
   * operator's input.
   */
  const handleAddBackend = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setFormError(null);
      setFormSubmitting(true);
      try {
        let parsedConfig: Record<string, unknown>;
        try {
          parsedConfig = JSON.parse(formConfig);
        } catch {
          setFormError("Config must be valid JSON");
          setFormSubmitting(false);
          return;
        }
        await api.createBackend({
          name: formName,
          backend_type: formType,
          config: parsedConfig,
          credential_id: formCredential || undefined,
          capacity_bytes: formCapacity ? parseInt(formCapacity, 10) : null,
          is_default: formDefault,
        });
        await useStorageStore.getState().fetch();
        setShowAddDialog(false);
        setFormName("");
        setFormType("minio");
        setFormConfig("{}");
        setFormCredential("");
        setFormCapacity("");
        setFormDefault(false);
      } catch (err: unknown) {
        setFormError(err instanceof Error ? err.message : "Failed to create backend");
      } finally {
        setFormSubmitting(false);
      }
    },
    [formName, formType, formConfig, formCredential, formCapacity, formDefault]
  );

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Storage</h1>
        <button
          onClick={() => setShowAddDialog(true)}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          <Plus className="h-4 w-4" />
          Add Backend
        </button>
      </div>

      {/* Backend Cards */}
      {isLoading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : backends.length === 0 ? (
        <div className="rounded-xl border border-border bg-card px-6 py-12 text-center text-muted-foreground">
          No storage backends configured.
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {backends.map((b) => {
            const health = healthResults[b.id];
            return (
              <div
                key={b.id}
                className="rounded-xl border border-border bg-card p-5 space-y-4"
              >
                {/* Top row: name, type, health */}
                <div className="flex items-start justify-between">
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <h3 className="font-semibold">{b.name}</h3>
                      {b.is_default && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-yellow-100 px-2 py-0.5 text-xs font-medium text-yellow-700">
                          <Star className="h-3 w-3" />
                          Default
                        </span>
                      )}
                    </div>
                    {typeBadge(b.backend_type)}
                  </div>
                  <div className="flex items-center gap-1">
                    {/* AI Note: `!== undefined` (not truthiness) — an untested
                        backend has no key in `healthResults` and shows no dot,
                        whereas a tested-and-failing backend is `false` and must
                        show a red one. */}
                    {health !== undefined && (
                      <span
                        className={cn(
                          "h-2.5 w-2.5 rounded-full",
                          health ? "bg-green-500" : "bg-red-500"
                        )}
                        title={health ? "Healthy" : "Unhealthy"}
                      />
                    )}
                  </div>
                </div>

                {/* Capacity */}
                <CapacityBar backend={b} />

                {/* Meta */}
                <div className="text-xs text-muted-foreground">
                  Priority: {b.priority}
                </div>

                {/* Actions */}
                <div className="flex items-center gap-2 pt-1">
                  <button
                    onClick={() => handleTest(b.id)}
                    disabled={testingId === b.id}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:bg-muted transition-colors disabled:opacity-50"
                  >
                    {testingId === b.id ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <Activity className="h-3 w-3" />
                    )}
                    Test
                  </button>
                  <button
                    onClick={() => handleDelete(b.id)}
                    disabled={deletingId === b.id}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50 transition-colors disabled:opacity-50"
                  >
                    {deletingId === b.id ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <Trash2 className="h-3 w-3" />
                    )}
                    Delete
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Transfers Section */}
      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Recent Transfers</h2>
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/50">
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Artifact
                </th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Source -&gt; Dest
                </th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Status
                </th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Progress
                </th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Error
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {transfersLoading ? (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center">
                    <Loader2 className="mx-auto h-5 w-5 animate-spin text-muted-foreground" />
                  </td>
                </tr>
              ) : transfers.length === 0 ? (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-8 text-center text-muted-foreground"
                  >
                    No transfers recorded.
                  </td>
                </tr>
              ) : (
                transfers.map((t) => (
                  <tr key={t.id}>
                    {/* AI Note: ids are truncated to 8 chars purely for
                        display width. They are UUID prefixes, not stable
                        short-ids — never use these strings to look a record
                        back up. */}
                    <td className="px-4 py-3 font-mono text-xs">
                      {t.artifact_id.slice(0, 8)}...
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                      {t.source_backend_id.slice(0, 8)} -&gt;{" "}
                      {t.dest_backend_id.slice(0, 8)}
                    </td>
                    <td className="px-4 py-3">{transferStatusBadge(t.status)}</td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {formatBytes(t.bytes_transferred)}
                    </td>
                    <td className="px-4 py-3 text-xs text-red-500 truncate max-w-[200px]">
                      {t.error || "-"}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Add Backend Dialog */}
      {showAddDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="fixed inset-0 bg-black/50"
            onClick={() => setShowAddDialog(false)}
          />
          <div className="relative z-10 w-full max-w-lg rounded-xl border border-border bg-card p-6 shadow-xl">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">Add Storage Backend</h3>
              <button
                onClick={() => setShowAddDialog(false)}
                className="rounded-md p-1 hover:bg-muted transition-colors"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            {formError && (
              <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-600">
                {formError}
              </div>
            )}

            <form onSubmit={handleAddBackend} className="space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1">Name</label>
                <input
                  type="text"
                  required
                  value={formName}
                  onChange={(e) => setFormName(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="my-minio-backend"
                />
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">Type</label>
                <select
                  value={formType}
                  onChange={(e) => setFormType(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  <option value="minio">MinIO</option>
                  <option value="nas">NAS</option>
                  <option value="s3">S3</option>
                  <option value="gdrive">Google Drive</option>
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">
                  Config (JSON)
                </label>
                <textarea
                  required
                  value={formConfig}
                  onChange={(e) => setFormConfig(e.target.value)}
                  rows={4}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder='{"endpoint": "localhost:9000", "bucket": "artifacts"}'
                />
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">
                  Credential
                </label>
                <select
                  value={formCredential}
                  onChange={(e) => setFormCredential(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  <option value="">None</option>
                  {credentials.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} ({c.credential_type})
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">
                  Capacity (bytes)
                </label>
                <input
                  type="number"
                  value={formCapacity}
                  onChange={(e) => setFormCapacity(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="Leave blank for unknown"
                />
              </div>

              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={formDefault}
                  onChange={(e) => setFormDefault(e.target.checked)}
                  className="h-4 w-4 rounded border-border"
                />
                <span className="text-sm">Set as default backend</span>
              </label>

              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowAddDialog(false)}
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={formSubmitting}
                  className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
                >
                  {formSubmitting && (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  )}
                  Create Backend
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
