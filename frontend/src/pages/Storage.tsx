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

const BACKEND_TYPE_META: Record<string, { icon: React.ElementType; color: string }> = {
  minio: { icon: HardDrive, color: "bg-orange-100 text-orange-700" },
  nas: { icon: Server, color: "bg-purple-100 text-purple-700" },
  s3: { icon: Cloud, color: "bg-blue-100 text-blue-700" },
  gdrive: { icon: FolderSync, color: "bg-green-100 text-green-700" },
};

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
