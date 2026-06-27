import { useEffect, useState, useCallback } from "react";
import {
  Layers,
  Plus,
  Trash2,
  X,
  Server,
  Loader2,
  UserMinus,
  Search,
  Calendar,
} from "lucide-react";
import { usePoolsStore, useNodesStore, useAuthStore } from "@/stores";
import { api } from "@/api/client";
import { cn } from "@/lib/utils";
import type { PoolInfo, NodeInfo } from "@/types";

// ---------------------------------------------------------------------------
// Create Pool Dialog
// ---------------------------------------------------------------------------

function CreatePoolDialog({
  onCreated,
  onClose,
}: {
  onCreated: () => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    setSubmitting(true);
    setError(null);
    try {
      await api.createPool({
        name: name.trim(),
        description: description.trim() || undefined,
      });
      onCreated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create pool");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-md rounded-xl border border-border bg-background p-6 shadow-xl">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold">Create Pool</h3>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 hover:bg-muted transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          <div>
            <label htmlFor="pool-name" className="mb-1.5 block text-sm font-medium">
              Name
            </label>
            <input
              id="pool-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. gpu-cluster"
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              required
              autoFocus
            />
          </div>

          <div>
            <label htmlFor="pool-desc" className="mb-1.5 block text-sm font-medium">
              Description
            </label>
            <textarea
              id="pool-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional description of this pool..."
              rows={3}
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring resize-none"
            />
          </div>

          {error && (
            <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600 dark:bg-red-950 dark:text-red-400">
              {error}
            </p>
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
              disabled={submitting || !name.trim()}
              className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
            >
              {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
              Create Pool
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Delete Pool Confirmation
// ---------------------------------------------------------------------------

function DeletePoolDialog({
  poolName,
  onConfirm,
  onCancel,
}: {
  poolName: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-sm rounded-xl border border-border bg-background p-6 shadow-xl">
        <h3 className="text-lg font-semibold">Delete Pool</h3>
        <p className="mt-2 text-sm text-muted-foreground">
          Are you sure you want to delete <span className="font-medium text-foreground">{poolName}</span>?
          This will not delete the nodes, but they will be removed from this pool.
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
// Add Node Dropdown
// ---------------------------------------------------------------------------

function AddNodeDropdown({
  poolId,
  existingNodeIds,
  allNodes,
  onAdded,
}: {
  poolId: string;
  existingNodeIds: Set<string>;
  allNodes: NodeInfo[];
  onAdded: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState<string | null>(null);

  const available = allNodes.filter(
    (n) => !existingNodeIds.has(n.id) && (n.hostname.toLowerCase().includes(search.toLowerCase()) || n.display_name?.toLowerCase().includes(search.toLowerCase()))
  );

  const handleAdd = async (nodeId: string) => {
    setAdding(nodeId);
    try {
      await api.addNodeToPool(poolId, nodeId);
      onAdded();
    } catch (err) {
      console.error("Failed to add node to pool:", err);
    } finally {
      setAdding(null);
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-border px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
      >
        <Plus className="h-3.5 w-3.5" />
        Add Node
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute left-0 z-20 mt-1 w-72 rounded-xl border border-border bg-background shadow-lg">
            {/* Search */}
            <div className="flex items-center gap-2 border-b border-border px-3 py-2">
              <Search className="h-3.5 w-3.5 text-muted-foreground" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search nodes..."
                className="w-full bg-transparent text-sm placeholder:text-muted-foreground focus:outline-none"
                autoFocus
              />
            </div>

            <div className="max-h-48 overflow-y-auto py-1">
              {available.length === 0 ? (
                <p className="px-3 py-3 text-center text-xs text-muted-foreground">
                  No available nodes
                </p>
              ) : (
                available.map((node) => (
                  <button
                    key={node.id}
                    type="button"
                    onClick={() => handleAdd(node.id)}
                    disabled={adding === node.id}
                    className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-muted transition-colors disabled:opacity-50"
                  >
                    <span className="flex items-center gap-2">
                      <Server className="h-3.5 w-3.5 text-muted-foreground" />
                      {node.display_name || node.hostname}
                    </span>
                    {adding === node.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Plus className="h-3.5 w-3.5 text-muted-foreground" />
                    )}
                  </button>
                ))
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pool Detail (inline expanded view)
// ---------------------------------------------------------------------------

function PoolDetail({
  pool,
  onClose,
  onRefresh,
}: {
  pool: PoolInfo;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin";
  const allNodes = useNodesStore((s) => s.nodes);

  const [poolNodes, setPoolNodes] = useState<NodeInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [removingNode, setRemovingNode] = useState<string | null>(null);

  const fetchDetail = useCallback(async () => {
    setLoading(true);
    try {
      const detail = await api.getPool(pool.id);
      setPoolNodes(detail.nodes);
    } catch (err) {
      console.error("Failed to fetch pool detail:", err);
    } finally {
      setLoading(false);
    }
  }, [pool.id]);

  useEffect(() => {
    fetchDetail();
  }, [fetchDetail]);

  const handleRemoveNode = async (nodeId: string) => {
    setRemovingNode(nodeId);
    try {
      await api.removeNodeFromPool(pool.id, nodeId);
      await fetchDetail();
      onRefresh();
    } catch (err) {
      console.error("Failed to remove node:", err);
    } finally {
      setRemovingNode(null);
    }
  };

  const handleDeletePool = async () => {
    try {
      await api.deletePool(pool.id);
      onRefresh();
      onClose();
    } catch (err) {
      console.error("Failed to delete pool:", err);
    }
  };

  const existingNodeIds = new Set(poolNodes.map((n) => n.id));

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/30" onClick={onClose} />
      <div className="fixed inset-y-0 right-0 z-50 w-full max-w-lg overflow-y-auto border-l border-border bg-background shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <div className="flex items-center gap-3">
            <Layers className="h-5 w-5 text-muted-foreground" />
            <h2 className="text-lg font-semibold">{pool.name}</h2>
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
          {/* Description */}
          {pool.description && (
            <div>
              <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Description
              </h3>
              <p className="text-sm text-muted-foreground">{pool.description}</p>
            </div>
          )}

          {/* Meta */}
          <div>
            <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Details
            </h3>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <dt className="text-muted-foreground">Created</dt>
              <dd>{new Date(pool.created_at).toLocaleDateString()}</dd>
              <dt className="text-muted-foreground">Node Count</dt>
              <dd>{pool.node_count}</dd>
            </dl>
          </div>

          {/* Member Nodes */}
          <div>
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Member Nodes
              </h3>
              <AddNodeDropdown
                poolId={pool.id}
                existingNodeIds={existingNodeIds}
                allNodes={allNodes}
                onAdded={() => {
                  fetchDetail();
                  onRefresh();
                }}
              />
            </div>

            {loading ? (
              <div className="flex justify-center py-8">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              </div>
            ) : poolNodes.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border py-6 text-center">
                <Server className="mx-auto h-8 w-8 text-muted-foreground/40" />
                <p className="mt-2 text-xs text-muted-foreground">No nodes in this pool</p>
              </div>
            ) : (
              <div className="space-y-2">
                {poolNodes.map((node) => {
                  const statusColor: Record<string, string> = {
                    online: "bg-green-500",
                    offline: "bg-gray-400",
                    busy: "bg-yellow-500",
                    maintenance: "bg-orange-500",
                  };
                  return (
                    <div
                      key={node.id}
                      className="flex items-center justify-between rounded-lg border border-border px-4 py-2.5"
                    >
                      <div className="flex items-center gap-3">
                        <Server className="h-4 w-4 text-muted-foreground" />
                        <div>
                          <p className="text-sm font-medium">
                            {node.display_name || node.hostname}
                          </p>
                          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                            <span className={cn("h-1.5 w-1.5 rounded-full", statusColor[node.status] ?? "bg-gray-400")} />
                            {node.status} -- {node.os_type} -- {node.arch}
                          </p>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => handleRemoveNode(node.id)}
                        disabled={removingNode === node.id}
                        title="Remove from pool"
                        className="rounded-lg p-1.5 text-muted-foreground hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950 dark:hover:text-red-400 transition-colors disabled:opacity-50"
                      >
                        {removingNode === node.id ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <UserMinus className="h-4 w-4" />
                        )}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Admin: Delete Pool */}
          {isAdmin && (
            <div className="pt-2">
              <button
                type="button"
                onClick={() => setDeleteDialogOpen(true)}
                className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-red-300 px-4 py-2 text-sm font-medium text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950 transition-colors"
              >
                <Trash2 className="h-4 w-4" />
                Delete Pool
              </button>
            </div>
          )}
        </div>
      </div>

      {deleteDialogOpen && (
        <DeletePoolDialog
          poolName={pool.name}
          onConfirm={handleDeletePool}
          onCancel={() => setDeleteDialogOpen(false)}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Pool Card
// ---------------------------------------------------------------------------

function PoolCard({
  pool,
  onClick,
}: {
  pool: PoolInfo;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex flex-col items-start rounded-xl border border-border bg-background p-6 shadow-sm text-left transition-shadow hover:shadow-md"
    >
      <div className="flex w-full items-start justify-between">
        <div className="flex items-center gap-2.5">
          <Layers className="h-5 w-5 text-muted-foreground" />
          <h3 className="text-base font-semibold">{pool.name}</h3>
        </div>
        <span className="inline-flex items-center rounded-full bg-primary/10 px-2 py-0.5 text-xs font-semibold text-primary">
          {pool.node_count} {pool.node_count === 1 ? "node" : "nodes"}
        </span>
      </div>

      {pool.description && (
        <p className="mt-2 line-clamp-2 text-sm text-muted-foreground">
          {pool.description}
        </p>
      )}

      <div className="mt-4 flex items-center gap-1.5 text-xs text-muted-foreground">
        <Calendar className="h-3 w-3" />
        Created {new Date(pool.created_at).toLocaleDateString()}
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function PoolsPage() {
  const { pools, isLoading, fetch: fetchPools } = usePoolsStore();
  const fetchNodes = useNodesStore((s) => s.fetch);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedPool, setSelectedPool] = useState<PoolInfo | null>(null);

  useEffect(() => {
    fetchPools();
    fetchNodes();
  }, [fetchPools, fetchNodes]);

  const refreshPools = useCallback(async () => {
    await fetchPools();
  }, [fetchPools]);

  // Keep selected pool in sync after refresh
  useEffect(() => {
    if (selectedPool) {
      const updated = pools.find((p) => p.id === selectedPool.id);
      if (updated) setSelectedPool(updated);
      else setSelectedPool(null);
    }
  }, [pools, selectedPool]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <Layers className="h-6 w-6 text-muted-foreground" />
          <h1 className="text-2xl font-bold tracking-tight">Pools</h1>
          <span className="rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium text-muted-foreground">
            {pools.length}
          </span>
        </div>

        <button
          type="button"
          onClick={() => setCreateOpen(true)}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          <Plus className="h-4 w-4" />
          Create Pool
        </button>
      </div>

      {/* Pool Grid */}
      {isLoading ? (
        <div className="flex items-center justify-center py-20">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : pools.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border py-16 text-center">
          <Layers className="mx-auto h-10 w-10 text-muted-foreground/50" />
          <p className="mt-3 text-sm text-muted-foreground">No pools created yet</p>
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="mt-4 inline-flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors"
          >
            <Plus className="h-4 w-4" />
            Create your first pool
          </button>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {pools.map((pool) => (
            <PoolCard key={pool.id} pool={pool} onClick={() => setSelectedPool(pool)} />
          ))}
        </div>
      )}

      {/* Create Pool Dialog */}
      {createOpen && (
        <CreatePoolDialog
          onCreated={refreshPools}
          onClose={() => setCreateOpen(false)}
        />
      )}

      {/* Pool Detail Slide-over */}
      {selectedPool && (
        <PoolDetail
          pool={selectedPool}
          onClose={() => setSelectedPool(null)}
          onRefresh={refreshPools}
        />
      )}
    </div>
  );
}
