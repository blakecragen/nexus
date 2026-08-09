/**
 * Pools.tsx — node-pool management (route `/pools`).
 *
 * Role in the system:
 *   A pool is a named group of nodes that a job can target instead of naming a
 *   specific machine (`target_pool_id` in the job submit payload — see
 *   `JobBuilder.tsx`). This page is where pools are created, populated, emptied
 *   and deleted, so it is effectively the scheduler's routing configuration UI.
 *
 * Data flow:
 *   - `usePoolsStore`  -> GET /api/pools (list + `node_count` per pool).
 *   - `useNodesStore`  -> GET /api/nodes, used only to populate the
 *     "Add Node" picker with candidates.
 *   - Detail view fetches membership directly (not through a store):
 *     GET /api/pools/{id} -> `{ pool, nodes }`.
 *   - Mutations via `api` (`frontend/src/api/client.ts`):
 *       POST   /api/pools
 *       DELETE /api/pools/{id}
 *       POST   /api/pools/{id}/nodes            (add member)
 *       DELETE /api/pools/{id}/nodes/{nodeId}   (remove member)
 *   - `useAuthStore` gates pool deletion behind `role === "admin"`.
 *
 * AI Note: pool membership is NOT pushed over the dashboard WebSocket. Every
 * change here requires an explicit refetch, which is why the child components
 * take both a local `fetchDetail` and a parent `onRefresh` — the former updates
 * the member list, the latter updates the `node_count` badge on the cards.
 *
 * File layout: dialogs -> node picker -> slide-over detail -> card -> the
 * exported page component at the bottom.
 */
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

/**
 * Modal form for creating a pool (POST /api/pools).
 *
 * What the user sees: a name field (required) and an optional description
 * textarea. The submit button is disabled while the name is blank or a request
 * is in flight; server errors render inline in a red banner.
 *
 * @param onCreated called after a successful POST so the parent refetches the
 *   pool list.
 * @param onClose dismisses the dialog. Note the success path calls
 *   `onCreated()` then `onClose()`, so the dialog closes itself.
 *
 * AI Note: unlike most pools/nodes mutations on this page (which only
 * `console.error`), this one surfaces the failure to the user — a duplicate
 * pool name is a normal, recoverable 4xx that the operator must see.
 */
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

  /**
   * Validates, POSTs the new pool, then closes.
   *
   * AI Note: an empty description is sent as `undefined` (field omitted) rather
   * than `""` so the server stores NULL instead of an empty string — the card
   * and detail view both test `pool.description` for truthiness to decide
   * whether to render that block.
   */
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

/**
 * Modal confirmation for pool deletion.
 *
 * Stateless: the parent ({@link PoolDetail}) owns both callbacks and performs
 * the DELETE. The copy deliberately states that member nodes survive — the
 * server only drops the pool and its membership rows, never the nodes.
 *
 * @param poolName shown so the operator can verify the target.
 */
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

/**
 * Searchable dropdown for adding a node to a pool
 * (POST /api/pools/{poolId}/nodes).
 *
 * What the user sees: an "Add Node" pill; clicking it opens a panel with a
 * search box and a scrollable list of candidate nodes. Clicking a row adds it
 * immediately (no confirmation) and shows a spinner on that row.
 *
 * @param poolId target pool for the add request.
 * @param existingNodeIds ids already in the pool — these are filtered out so a
 *   node can never be added twice.
 * @param allNodes the full cluster inventory from `useNodesStore`; this
 *   component does not fetch.
 * @param onAdded fired after each successful add so the parent can refresh both
 *   the member list and the pool's node count.
 *
 * AI Note: the dropdown stays open after an add so several nodes can be added
 * in a row. Because `existingNodeIds` is derived from the parent's freshly
 * refetched member list, the just-added node disappears from the list on the
 * next render — that is the only feedback that it worked.
 */
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

  // AI Note: the search is matched against hostname OR display_name, but the
  // row renders `display_name || hostname` — so a node can match on a hostname
  // that is never shown. Intentional: operators often know the real hostname.
  // `display_name?.` yields undefined for unnamed nodes, which `||` treats as
  // "no match" rather than throwing.
  const available = allNodes.filter(
    (n) => !existingNodeIds.has(n.id) && (n.hostname.toLowerCase().includes(search.toLowerCase()) || n.display_name?.toLowerCase().includes(search.toLowerCase()))
  );

  /**
   * Adds one node to the pool and notifies the parent.
   *
   * Failures are console-only: the row's spinner clears and nothing visibly
   * changes, which reads as "nothing happened". Worth improving if pool adds
   * start failing for reasons other than a race with a concurrent delete.
   */
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
          {/* AI Note: same click-outside pattern as FilterDropdown in
              Nodes.tsx — an invisible full-screen catcher at z-10 beneath the
              z-20 menu. Because this dropdown lives inside the z-50 slide-over,
              the catcher only shields content below it in the stacking
              context; clicks on the slide-over header still land normally. */}
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

/**
 * Right-hand slide-over showing one pool's description, metadata and member
 * nodes, plus the admin-only delete action.
 *
 * What the user sees: pool name header, optional description, a Created /
 * Node Count detail list, a "Member Nodes" section with an inline
 * {@link AddNodeDropdown} and one removable row per member, and (for admins) a
 * red "Delete Pool" button.
 *
 * Data: fetches membership itself via GET /api/pools/{id} into local
 * `poolNodes` state — deliberately not stored globally, since only this panel
 * needs it.
 *
 * @param pool the selected pool. The parent keeps this object in sync with the
 *   pools store after refreshes.
 * @param onClose closes the slide-over.
 * @param onRefresh refetches the pool list so the card grid's `node_count`
 *   badge stays accurate.
 *
 * AI Note: `pool.node_count` in the Details list comes from the *list*
 * endpoint, while the Member Nodes section comes from the *detail* endpoint.
 * They can briefly disagree right after an add/remove, until `onRefresh()`
 * lands. Both are refreshed together on every mutation to keep the window
 * small.
 */
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

  /** Loads this pool's member nodes (GET /api/pools/{id}); the response's
   * `pool` field is discarded because the parent already supplies it. Keyed on
   * `pool.id` so switching pools refetches. */
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

  /**
   * Removes a node from this pool (DELETE /api/pools/{id}/nodes/{nodeId}).
   *
   * AI Note: refresh order matters — `fetchDetail()` is awaited first so the
   * member list is correct before `onRefresh()` updates the parent's count;
   * doing it the other way round briefly renders a card count that is lower
   * than the visible member rows.
   *
   * AI Note: no confirmation prompt. Removal is cheap and reversible via the
   * Add Node dropdown, unlike pool deletion which does confirm.
   */
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

  /**
   * Deletes the pool (DELETE /api/pools/{id}), refreshes the grid, then closes
   * the slide-over. Member nodes are unaffected — only the pool and its
   * membership rows go away.
   *
   * AI Note: jobs that were submitted with this `target_pool_id` are not
   * cleaned up here; the server decides what happens to anything still queued
   * against a deleted pool. Do not assume the UI has handled that case.
   */
  const handleDeletePool = async () => {
    try {
      await api.deletePool(pool.id);
      onRefresh();
      onClose();
    } catch (err) {
      console.error("Failed to delete pool:", err);
    }
  };

  // Membership set handed to the picker so already-joined nodes are hidden.
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
                  // AI Note: a third copy of the node-status colour map (see
                  // STATUS_COLORS in Nodes.tsx). Declared inside the map
                  // callback, so it is rebuilt per row — negligible cost, but
                  // it must be kept in sync with Nodes.tsx when statuses
                  // change. The `?? "bg-gray-400"` makes an unknown status
                  // render as offline-grey rather than unstyled.
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

/**
 * Grid tile for one pool.
 *
 * What the user sees: pool name with a layers icon, a pill showing the member
 * count (correctly singularised), a two-line-clamped description, and the
 * creation date. The entire card is a `<button>`, so it is keyboard-focusable
 * and activates the same way as a click.
 *
 * Purely presentational — `onClick` opens {@link PoolDetail} in the parent.
 */
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

/**
 * Pools page (route `/pools`).
 *
 * What the user sees: a header with the pool count and a "Create Pool" button,
 * then a responsive grid of {@link PoolCard}s (or a dashed empty state with a
 * "Create your first pool" shortcut). Clicking a card opens the
 * {@link PoolDetail} slide-over.
 *
 * Side effects on mount: GET /api/pools *and* GET /api/nodes — the node list is
 * fetched here (not in the detail panel) so the "Add Node" picker inside the
 * slide-over can render instantly without its own request.
 *
 * AI Note: "Create Pool" is visible to every authenticated user, while
 * deleting a pool is admin-only (see {@link PoolDetail}). That asymmetry is
 * intentional — the server enforces the real rules on both endpoints.
 *
 * Props: none — routed component.
 */
export default function PoolsPage() {
  const { pools, isLoading, fetch: fetchPools } = usePoolsStore();
  const fetchNodes = useNodesStore((s) => s.fetch);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedPool, setSelectedPool] = useState<PoolInfo | null>(null);

  useEffect(() => {
    fetchPools();
    fetchNodes();
  }, [fetchPools, fetchNodes]);

  /** Reload the pool list (and therefore every card's `node_count`). Stable
   * identity via `useCallback` so child effects do not re-run. */
  const refreshPools = useCallback(async () => {
    await fetchPools();
  }, [fetchPools]);

  // Keep selected pool in sync after refresh
  //
  // AI Note: identical pattern to the selected-node effect in Nodes.tsx —
  // `selectedPool` is a snapshot, so it must be re-resolved against the fresh
  // store array after every refetch, and set to null when the pool has been
  // deleted (which closes the slide-over). Loop termination depends on
  // `pools.find` returning a stable object reference.
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
