/**
 * Admin.tsx — the administrative console (route `/admin`).
 *
 * Role in the system:
 *   A three-tab page covering the identity and secrets side of Nexus:
 *     - Users:       create accounts, change roles, activate/deactivate.
 *     - Groups:      create groups, manage membership and pool access grants.
 *     - Credentials: store/test/delete the encrypted secrets that storage
 *                    backends and job steps use.
 *
 * Data flow:
 *   - Credentials go through `useCredentialsStore` and the typed `api` client
 *     (GET/POST/DELETE /api/credentials, POST /api/credentials/{id}/test,
 *     GET /api/credentials/types).
 *   - Users and Groups use RAW `fetch()` against /api/admin/* — those endpoints
 *     have no wrappers in `frontend/src/api/client.ts`.
 *
 * AI Note: that raw-fetch split matters. `api.request()` centralises the Bearer
 * header, the 401 -> clear-token -> redirect-to-login behaviour, and error
 * extraction from `detail`. None of that applies to the /api/admin/* calls
 * here: they hand-roll the Authorization header from localStorage and mostly
 * swallow failures, so an expired session on the Users/Groups tabs silently
 * renders an empty table instead of bouncing the user to /login.
 *
 * AI Note: nothing on this page checks the caller's role client-side — it is
 * routed like any other page. Authorisation is enforced entirely by the server
 * on the /api/admin/* endpoints.
 *
 * Neighbours: credentials created here are selected on `Storage.tsx` when
 * registering a backend; pool names granted to groups correspond to pools
 * managed on `Pools.tsx`.
 */
import { useEffect, useState, useCallback } from "react";
import {
  Plus,
  Loader2,
  Trash2,
  X,
  Users,
  Shield,
  Key,
  ChevronDown,
  ChevronRight,
  UserMinus,
  CheckCircle,
  XCircle,
} from "lucide-react";
import { useCredentialsStore } from "@/stores";
import { api } from "@/api/client";
import { cn, formatRelativeTime } from "@/lib/utils";
import type { UserInfo, UserRole, CredentialTypeInfo } from "@/types";

/** Which admin sub-view is showing; see {@link TAB_CONFIG}. */
type AdminTab = "users" | "groups" | "credentials";

// ── Role badge colors ─────────────────────────────────────────────────

/** Pill styling per role, escalating red -> purple -> blue by privilege. Keys
 * mirror the server's `UserRole` enum. */
const ROLE_COLORS: Record<UserRole, string> = {
  admin: "bg-red-100 text-red-700",
  manager: "bg-purple-100 text-purple-700",
  user: "bg-blue-100 text-blue-700",
};

/** Renders the role pill for a user row. Unknown roles fall back to neutral
 * styling rather than rendering unstyled. */
function roleBadge(role: UserRole) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize",
        ROLE_COLORS[role] ?? "bg-secondary text-muted-foreground"
      )}
    >
      {role}
    </span>
  );
}

// ── Group types (local) ───────────────────────────────────────────────
//
// AI Note: these shapes are declared here rather than in `@/types` because the
// /api/admin/groups endpoints have no client wrapper and no shared type. They
// are hand-written mirrors of the server response — if the API changes, nothing
// will fail to compile; it will just render wrong at runtime.

/** One member row inside a {@link GroupInfo}. */
interface GroupMember {
  user_id: string;
  username: string;
}

/** A group as returned by GET /api/admin/groups. `pool_access` is a list of
 * pool identifiers (names or ids) the group is allowed to target. */
interface GroupInfo {
  id: string;
  name: string;
  description: string | null;
  members: GroupMember[];
  pool_access: string[];
}

// ── Users Tab ─────────────────────────────────────────────────────────

/**
 * "Users" tab — the account list plus inline role editing, an active toggle and
 * a create-user dialog.
 *
 * What the user sees: a four-column table (Username, Email, Role, Active).
 * Clicking the role pill turns it into a `<select>`; the Active cell is a
 * switch that flips immediately.
 *
 * Endpoints:
 *   - GET  /api/admin/users              (raw fetch)
 *   - PUT  /api/admin/users/{id}/role    (raw fetch)
 *   - PUT  /api/admin/users/{id}/active  (raw fetch)
 *   - POST /api/auth/register            (via `api.register`, the one typed call)
 *
 * AI Note: creating a user reuses the public registration endpoint with an
 * explicit `role`. The server must therefore be the thing that prevents a
 * non-admin from self-registering as `admin` — this form happily sends any role
 * the dropdown offers.
 */
function UsersTab() {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [formUsername, setFormUsername] = useState("");
  const [formPassword, setFormPassword] = useState("");
  const [formEmail, setFormEmail] = useState("");
  const [formRole, setFormRole] = useState<UserRole>("user");
  const [formSubmitting, setFormSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [editingRoleId, setEditingRoleId] = useState<string | null>(null);

  /**
   * Loads the user list.
   *
   * AI Note: reads the token straight out of localStorage (`nexus_token`)
   * instead of `getToken()` from the api client. Both read the same key today,
   * but this bypass means the in-memory token set by `setToken()` is ignored —
   * if token storage ever moves off localStorage, every raw fetch on this page
   * breaks at once.
   *
   * AI Note: a non-OK response is not distinguished from a network error — both
   * leave `users` at its previous value and stop the spinner, so a 403 looks
   * identical to "no users".
   */
  const fetchUsers = useCallback(async () => {
    setIsLoading(true);
    try {
      const res = await fetch("/api/admin/users", {
        headers: {
          Authorization: `Bearer ${localStorage.getItem("nexus_token")}`,
        },
      });
      if (res.ok) {
        const data = await res.json();
        setUsers(data);
      }
    } catch {
      // ignore
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchUsers();
  }, [fetchUsers]);

  /**
   * Creates a user via POST /api/auth/register, refetches the table and resets
   * the dialog. Unlike the other handlers here this one *does* surface the
   * error (duplicate username, weak password) in a red banner, because those
   * are recoverable input problems the operator must see.
   */
  const handleCreate = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setFormError(null);
      setFormSubmitting(true);
      try {
        await api.register({
          username: formUsername,
          password: formPassword,
          email: formEmail || undefined,
          role: formRole,
        });
        await fetchUsers();
        setShowCreateDialog(false);
        setFormUsername("");
        setFormPassword("");
        setFormEmail("");
        setFormRole("user");
      } catch (err: unknown) {
        setFormError(err instanceof Error ? err.message : "Failed to create user");
      } finally {
        setFormSubmitting(false);
      }
    },
    [formUsername, formPassword, formEmail, formRole, fetchUsers]
  );

  /**
   * Changes a user's role (PUT /api/admin/users/{id}/role).
   *
   * AI Note: security-sensitive and OPTIMISTIC — the local `users` array is
   * patched without checking `res.ok`. If the server rejects the change (e.g.
   * an admin trying to demote the last admin, or a stale session), the table
   * will display the new role while the backend still holds the old one until
   * the next refetch. Do not treat what this table shows as authoritative.
   */
  const handleRoleChange = useCallback(
    async (userId: string, newRole: UserRole) => {
      try {
        await fetch(`/api/admin/users/${userId}/role`, {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("nexus_token")}`,
          },
          body: JSON.stringify({ role: newRole }),
        });
        setUsers((prev) =>
          prev.map((u) => (u.id === userId ? { ...u, role: newRole } : u))
        );
      } catch {
        // ignore
      }
      setEditingRoleId(null);
    },
    []
  );

  /**
   * Enables/disables a user account (PUT /api/admin/users/{id}/active).
   *
   * @param isActive the user's CURRENT state — the handler sends `!isActive`.
   *   Passing the desired state instead would invert the toggle.
   *
   * AI Note: same optimistic-update caveat as {@link handleRoleChange}: the
   * response is never inspected, so a rejected deactivation still flips the
   * switch on screen. Deactivating an account is a security control (it should
   * stop that user from logging in), so a silent failure here is worth fixing.
   */
  const handleToggleActive = useCallback(
    async (userId: string, isActive: boolean) => {
      try {
        await fetch(`/api/admin/users/${userId}/active`, {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("nexus_token")}`,
          },
          body: JSON.stringify({ is_active: !isActive }),
        });
        setUsers((prev) =>
          prev.map((u) =>
            u.id === userId ? { ...u, is_active: !isActive } : u
          )
        );
      } catch {
        // ignore
      }
    },
    []
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Users</h2>
        <button
          onClick={() => setShowCreateDialog(true)}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          <Plus className="h-4 w-4" />
          Create User
        </button>
      </div>

      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/50">
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Username
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Email
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Role
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Active
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {isLoading ? (
              <tr>
                <td colSpan={4} className="px-4 py-8 text-center">
                  <Loader2 className="mx-auto h-5 w-5 animate-spin text-muted-foreground" />
                </td>
              </tr>
            ) : users.length === 0 ? (
              <tr>
                <td
                  colSpan={4}
                  className="px-4 py-8 text-center text-muted-foreground"
                >
                  No users found.
                </td>
              </tr>
            ) : (
              users.map((u) => (
                <tr key={u.id}>
                  <td className="px-4 py-3 font-medium">{u.username}</td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {u.email || "-"}
                  </td>
                  <td className="px-4 py-3">
                    {editingRoleId === u.id ? (
                      // AI Note: `defaultValue` (uncontrolled) plus `onBlur` to
                      // exit edit mode. `handleRoleChange` also clears
                      // `editingRoleId`, so selecting an option fires change ->
                      // save -> collapse; blurring without choosing just
                      // collapses. Switching this to a controlled `value` would
                      // require tracking a draft role in state.
                      <select
                        defaultValue={u.role}
                        onChange={(e) =>
                          handleRoleChange(u.id, e.target.value as UserRole)
                        }
                        onBlur={() => setEditingRoleId(null)}
                        autoFocus
                        className="rounded-md border border-border bg-background px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-ring"
                      >
                        <option value="user">user</option>
                        <option value="manager">manager</option>
                        <option value="admin">admin</option>
                      </select>
                    ) : (
                      <button
                        onClick={() => setEditingRoleId(u.id)}
                        title="Click to edit role"
                      >
                        {roleBadge(u.role)}
                      </button>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <button
                      onClick={() => handleToggleActive(u.id, u.is_active)}
                      className={cn(
                        "relative inline-flex h-5 w-9 items-center rounded-full transition-colors",
                        u.is_active ? "bg-green-500" : "bg-muted"
                      )}
                    >
                      <span
                        className={cn(
                          "inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform",
                          u.is_active ? "translate-x-4.5" : "translate-x-0.5"
                        )}
                      />
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Create User Dialog */}
      {showCreateDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="fixed inset-0 bg-black/50"
            onClick={() => setShowCreateDialog(false)}
          />
          <div className="relative z-10 w-full max-w-md rounded-xl border border-border bg-card p-6 shadow-xl">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">Create User</h3>
              <button
                onClick={() => setShowCreateDialog(false)}
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

            <form onSubmit={handleCreate} className="space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1">
                  Username
                </label>
                <input
                  type="text"
                  required
                  value={formUsername}
                  onChange={(e) => setFormUsername(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">
                  Password
                </label>
                <input
                  type="password"
                  required
                  value={formPassword}
                  onChange={(e) => setFormPassword(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">Email</label>
                <input
                  type="email"
                  value={formEmail}
                  onChange={(e) => setFormEmail(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="Optional"
                />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">Role</label>
                <select
                  value={formRole}
                  onChange={(e) => setFormRole(e.target.value as UserRole)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  <option value="user">User</option>
                  <option value="manager">Manager</option>
                  <option value="admin">Admin</option>
                </select>
              </div>
              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowCreateDialog(false)}
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
                  Create
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Groups Tab ────────────────────────────────────────────────────────

/**
 * "Groups" tab — accordion of groups, each expanding to show member chips and
 * pool-access chips with inline add/remove controls.
 *
 * What the user sees: one collapsible card per group showing its name, member
 * count and description. Expanding reveals two sections ("Members", "Pool
 * Access"), each with a "+ Add" link that toggles an inline text input
 * (Enter or the Add button submits).
 *
 * Endpoints (all raw fetch, no client wrapper):
 *   - GET    /api/admin/groups
 *   - POST   /api/admin/groups
 *   - POST   /api/admin/groups/{id}/members         `{ username }`
 *   - DELETE /api/admin/groups/{id}/members/{userId}
 *   - POST   /api/admin/groups/{id}/pools           `{ pool_name }`
 *
 * AI Note: adding a member takes a free-text USERNAME (not an id) and adding
 * pool access takes a free-text pool name/id — there is no picker and no
 * validation, so a typo produces a silent no-op (all these handlers swallow
 * errors and just refetch). There is also no way to REVOKE pool access from
 * this UI; the chips are display-only.
 */
function GroupsTab() {
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [formName, setFormName] = useState("");
  const [formDescription, setFormDescription] = useState("");
  const [formSubmitting, setFormSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Add member / pool state
  const [addMemberGroupId, setAddMemberGroupId] = useState<string | null>(null);
  const [addMemberUsername, setAddMemberUsername] = useState("");
  const [addPoolGroupId, setAddPoolGroupId] = useState<string | null>(null);
  const [addPoolName, setAddPoolName] = useState("");

  // AI Note: built once per render from localStorage, then captured by the
  // `useCallback` handlers below — which deliberately do NOT list it as a
  // dependency (it is a fresh object every render and would defeat the
  // memoisation). The captured value is the token as of the render in which
  // the callback was created; that is fine because the token only changes on
  // login/logout, both of which remount this page.
  const authHeader = {
    Authorization: `Bearer ${localStorage.getItem("nexus_token")}`,
    "Content-Type": "application/json",
  };

  /** Loads all groups with their members and pool grants. Same silent-failure
   * behaviour as the Users tab: a 403 renders as "no groups". */
  const fetchGroups = useCallback(async () => {
    setIsLoading(true);
    try {
      const res = await fetch("/api/admin/groups", { headers: authHeader });
      if (res.ok) setGroups(await res.json());
    } catch {
      // ignore
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchGroups();
  }, [fetchGroups]);

  /**
   * Creates a group (POST /api/admin/groups), refetches and resets the dialog.
   *
   * This is the only raw-fetch handler on the page that checks `res.ok` and
   * pulls `detail` out of the error body — the rest fire and forget. An empty
   * description is sent as explicit `null` rather than omitted.
   */
  const handleCreate = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setFormError(null);
      setFormSubmitting(true);
      try {
        const res = await fetch("/api/admin/groups", {
          method: "POST",
          headers: authHeader,
          body: JSON.stringify({
            name: formName,
            description: formDescription || null,
          }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || "Failed to create group");
        }
        await fetchGroups();
        setShowCreateDialog(false);
        setFormName("");
        setFormDescription("");
      } catch (err: unknown) {
        setFormError(err instanceof Error ? err.message : "Failed");
      } finally {
        setFormSubmitting(false);
      }
    },
    [formName, formDescription, fetchGroups]
  );

  /** Revokes a user's membership, then refetches the whole group list (rather
   * than patching locally) so member counts stay consistent. No confirmation
   * prompt — re-adding is a one-field operation. */
  const handleRemoveMember = useCallback(
    async (groupId: string, userId: string) => {
      try {
        await fetch(`/api/admin/groups/${groupId}/members/${userId}`, {
          method: "DELETE",
          headers: authHeader,
        });
        await fetchGroups();
      } catch {
        // ignore
      }
    },
    [fetchGroups]
  );

  /**
   * Adds a member by username, then closes the inline input.
   *
   * AI Note: the input is cleared and closed unconditionally on success *of the
   * fetch call*, not of the request — a 404 for an unknown username looks
   * exactly like a successful add until the refetched list shows the member is
   * absent. This is the single most confusing behaviour on this tab.
   */
  const handleAddMember = useCallback(
    async (groupId: string) => {
      if (!addMemberUsername.trim()) return;
      try {
        await fetch(`/api/admin/groups/${groupId}/members`, {
          method: "POST",
          headers: authHeader,
          body: JSON.stringify({ username: addMemberUsername }),
        });
        await fetchGroups();
        setAddMemberGroupId(null);
        setAddMemberUsername("");
      } catch {
        // ignore
      }
    },
    [addMemberUsername, fetchGroups]
  );

  /**
   * Grants the group access to a pool (POST .../pools with `{ pool_name }`).
   *
   * AI Note: security-sensitive — this widens which pools a group's members can
   * target when submitting jobs. Same silent-failure behaviour as
   * {@link handleAddMember}: a bad pool name closes the input as if it worked.
   * The displayed chips come from the refetch, so they are the source of truth.
   */
  const handleAddPool = useCallback(
    async (groupId: string) => {
      if (!addPoolName.trim()) return;
      try {
        await fetch(`/api/admin/groups/${groupId}/pools`, {
          method: "POST",
          headers: authHeader,
          body: JSON.stringify({ pool_name: addPoolName }),
        });
        await fetchGroups();
        setAddPoolGroupId(null);
        setAddPoolName("");
      } catch {
        // ignore
      }
    },
    [addPoolName, fetchGroups]
  );

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Groups</h2>
        <button
          onClick={() => setShowCreateDialog(true)}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          <Plus className="h-4 w-4" />
          Create Group
        </button>
      </div>

      {groups.length === 0 ? (
        <div className="rounded-xl border border-border bg-card px-6 py-12 text-center text-muted-foreground">
          No groups created yet.
        </div>
      ) : (
        <div className="space-y-3">
          {groups.map((g) => {
            const isExpanded = expandedId === g.id;
            return (
              <div
                key={g.id}
                className="rounded-xl border border-border bg-card overflow-hidden"
              >
                {/* Group header */}
                <button
                  onClick={() =>
                    setExpandedId(isExpanded ? null : g.id)
                  }
                  className="flex w-full items-center gap-3 px-5 py-4 text-left hover:bg-muted/50 transition-colors"
                >
                  {isExpanded ? (
                    <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="h-4 w-4 text-muted-foreground" />
                  )}
                  <span className="font-semibold">{g.name}</span>
                  <span className="text-xs text-muted-foreground">
                    {g.members.length} member{g.members.length !== 1 ? "s" : ""}
                  </span>
                  {g.description && (
                    <span className="ml-auto text-xs text-muted-foreground truncate max-w-[200px]">
                      {g.description}
                    </span>
                  )}
                </button>

                {/* Expanded content */}
                {isExpanded && (
                  <div className="border-t border-border px-5 py-4 space-y-4">
                    {/* Members */}
                    <div className="space-y-2">
                      <div className="flex items-center justify-between">
                        <h4 className="text-sm font-medium">Members</h4>
                        <button
                          onClick={() =>
                            setAddMemberGroupId(
                              addMemberGroupId === g.id ? null : g.id
                            )
                          }
                          className="text-xs text-primary hover:underline"
                        >
                          + Add Member
                        </button>
                      </div>
                      {addMemberGroupId === g.id && (
                        <div className="flex items-center gap-2">
                          <input
                            type="text"
                            value={addMemberUsername}
                            onChange={(e) => setAddMemberUsername(e.target.value)}
                            placeholder="Username"
                            className="flex-1 rounded-lg border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                            onKeyDown={(e) => {
                              if (e.key === "Enter") handleAddMember(g.id);
                            }}
                          />
                          <button
                            onClick={() => handleAddMember(g.id)}
                            className="rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
                          >
                            Add
                          </button>
                        </div>
                      )}
                      {g.members.length === 0 ? (
                        <p className="text-xs text-muted-foreground">
                          No members.
                        </p>
                      ) : (
                        <div className="flex flex-wrap gap-2">
                          {g.members.map((m) => (
                            <span
                              key={m.user_id}
                              className="inline-flex items-center gap-1.5 rounded-full bg-muted px-3 py-1 text-xs font-medium"
                            >
                              {m.username}
                              <button
                                onClick={() =>
                                  handleRemoveMember(g.id, m.user_id)
                                }
                                className="rounded-full p-0.5 hover:bg-red-100 hover:text-red-600 transition-colors"
                                title="Remove member"
                              >
                                <UserMinus className="h-3 w-3" />
                              </button>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Pool Access */}
                    <div className="space-y-2">
                      <div className="flex items-center justify-between">
                        <h4 className="text-sm font-medium">Pool Access</h4>
                        <button
                          onClick={() =>
                            setAddPoolGroupId(
                              addPoolGroupId === g.id ? null : g.id
                            )
                          }
                          className="text-xs text-primary hover:underline"
                        >
                          + Add Pool
                        </button>
                      </div>
                      {addPoolGroupId === g.id && (
                        <div className="flex items-center gap-2">
                          <input
                            type="text"
                            value={addPoolName}
                            onChange={(e) => setAddPoolName(e.target.value)}
                            placeholder="Pool name or ID"
                            className="flex-1 rounded-lg border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                            onKeyDown={(e) => {
                              if (e.key === "Enter") handleAddPool(g.id);
                            }}
                          />
                          <button
                            onClick={() => handleAddPool(g.id)}
                            className="rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
                          >
                            Add
                          </button>
                        </div>
                      )}
                      {g.pool_access.length === 0 ? (
                        <p className="text-xs text-muted-foreground">
                          No pool access configured.
                        </p>
                      ) : (
                        <div className="flex flex-wrap gap-2">
                          {g.pool_access.map((p) => (
                            <span
                              key={p}
                              className="inline-flex items-center rounded-full bg-blue-50 px-3 py-1 text-xs font-medium text-blue-700"
                            >
                              {p}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Create Group Dialog */}
      {showCreateDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="fixed inset-0 bg-black/50"
            onClick={() => setShowCreateDialog(false)}
          />
          <div className="relative z-10 w-full max-w-md rounded-xl border border-border bg-card p-6 shadow-xl">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">Create Group</h3>
              <button
                onClick={() => setShowCreateDialog(false)}
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
            <form onSubmit={handleCreate} className="space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1">Name</label>
                <input
                  type="text"
                  required
                  value={formName}
                  onChange={(e) => setFormName(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">
                  Description
                </label>
                <input
                  type="text"
                  value={formDescription}
                  onChange={(e) => setFormDescription(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="Optional"
                />
              </div>
              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowCreateDialog(false)}
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
                  Create
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Credentials Tab ───────────────────────────────────────────────────

/**
 * "Credentials" tab — the encrypted secret store.
 *
 * What the user sees: a table of stored credentials (name, type pill,
 * description, shared flag, created time) with per-row Test and Delete
 * actions, plus a "Create Credential" dialog whose fields are generated from
 * the selected credential type's schema.
 *
 * Endpoints (all through the typed `api` client, unlike the other two tabs):
 *   - GET    /api/credentials
 *   - GET    /api/credentials/types      -> drives the dynamic form
 *   - POST   /api/credentials
 *   - POST   /api/credentials/{id}/test
 *   - DELETE /api/credentials/{id}
 *
 * AI Note: secret VALUES are write-only. The list endpoint returns metadata
 * only — the encrypted `data` blob is never sent to the browser, which is why
 * there is no edit action here (only create, test, delete). Do not add an
 * "edit" flow that pre-fills existing secret values; they are not available.
 */
function CredentialsTab() {
  const { credentials, isLoading, fetch } = useCredentialsStore();
  const [credTypes, setCredTypes] = useState<CredentialTypeInfo[]>([]);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [testResults, setTestResults] = useState<Record<string, boolean | null>>({});
  const [testingId, setTestingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Form state
  const [formName, setFormName] = useState("");
  const [formType, setFormType] = useState("");
  const [formDescription, setFormDescription] = useState("");
  const [formShared, setFormShared] = useState(false);
  const [formData, setFormData] = useState<Record<string, string>>({});
  const [formSubmitting, setFormSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Loads the credential list and the type catalogue. A failed type fetch is
  // swallowed, which leaves the Type dropdown empty and the Create button
  // permanently disabled (it requires `formType`).
  useEffect(() => {
    fetch();
    api.listCredentialTypes().then(setCredTypes).catch(() => {});
  }, [fetch]);

  // The schema for the type currently chosen in the dialog; drives which
  // required/optional inputs are rendered. Undefined until a type is picked.
  const selectedType = credTypes.find((t) => t.credential_type === formType);

  /**
   * Verifies a stored credential against its target service
   * (POST /api/credentials/{id}/test) and shows a tick or cross in the row.
   *
   * AI Note: as on the Storage page, a thrown request is recorded as
   * `false` — "could not run the test" is displayed identically to "the
   * credential is invalid". Results are keyed by id and never cleared, so a
   * tick can go stale after the underlying secret is rotated elsewhere.
   */
  const handleTest = useCallback(async (id: string) => {
    setTestingId(id);
    try {
      const res = await api.testCredential(id);
      setTestResults((prev) => ({ ...prev, [id]: res.success }));
    } catch {
      setTestResults((prev) => ({ ...prev, [id]: false }));
    } finally {
      setTestingId(null);
    }
  }, []);

  /**
   * Deletes a credential after a native `confirm()`.
   *
   * AI Note: nothing here checks whether the credential is still referenced by
   * a storage backend (`Storage.tsx` binds backends to `credential_id`) or by a
   * job step. If the server allows the delete, those consumers break at their
   * next use. Verify referential behaviour server-side before relying on this.
   */
  const handleDelete = useCallback(
    async (id: string) => {
      if (!confirm("Delete this credential? This cannot be undone.")) return;
      setDeletingId(id);
      try {
        await api.deleteCredential(id);
        await fetch();
      } catch {
        // handled
      } finally {
        setDeletingId(null);
      }
    },
    [fetch]
  );

  /**
   * Creates a credential (POST /api/credentials) and resets the dialog.
   *
   * `formData` is the free-form `{ fieldName: value }` map assembled from the
   * selected type's required/optional fields; the server encrypts it at rest
   * and it is never returned by the list endpoint.
   *
   * AI Note: `is_shared` makes the credential visible to ALL users, not just
   * its creator — a deliberate privilege decision surfaced as a single
   * checkbox. The form is only cleared on success, so a validation failure
   * preserves whatever secrets were typed (they stay in component state, never
   * in storage).
   */
  const handleCreate = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setFormError(null);
      setFormSubmitting(true);
      try {
        await api.createCredential({
          name: formName,
          credential_type: formType,
          description: formDescription || undefined,
          is_shared: formShared,
          data: formData,
        });
        await fetch();
        setShowCreateDialog(false);
        setFormName("");
        setFormType("");
        setFormDescription("");
        setFormShared(false);
        setFormData({});
      } catch (err: unknown) {
        setFormError(
          err instanceof Error ? err.message : "Failed to create credential"
        );
      } finally {
        setFormSubmitting(false);
      }
    },
    [formName, formType, formDescription, formShared, formData, fetch]
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Credentials</h2>
        <button
          onClick={() => setShowCreateDialog(true)}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          <Plus className="h-4 w-4" />
          Create Credential
        </button>
      </div>

      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/50">
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Name
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Type
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Description
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Shared
              </th>
              <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                Created
              </th>
              <th className="px-4 py-3 text-right font-medium text-muted-foreground">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {isLoading ? (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center">
                  <Loader2 className="mx-auto h-5 w-5 animate-spin text-muted-foreground" />
                </td>
              </tr>
            ) : credentials.length === 0 ? (
              <tr>
                <td
                  colSpan={6}
                  className="px-4 py-8 text-center text-muted-foreground"
                >
                  No credentials stored.
                </td>
              </tr>
            ) : (
              credentials.map((c) => {
                const testRes = testResults[c.id];
                return (
                  <tr key={c.id}>
                    <td className="px-4 py-3 font-medium">{c.name}</td>
                    <td className="px-4 py-3">
                      <span className="inline-flex items-center rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium">
                        {c.credential_type}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground truncate max-w-[200px]">
                      {c.description || "-"}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {c.is_shared ? "Yes" : "No"}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {formatRelativeTime(c.created_at)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="inline-flex items-center gap-1">
                        {testRes !== undefined && (
                          <span className="mr-1">
                            {testRes ? (
                              <CheckCircle className="h-4 w-4 text-green-500" />
                            ) : (
                              <XCircle className="h-4 w-4 text-red-500" />
                            )}
                          </span>
                        )}
                        <button
                          onClick={() => handleTest(c.id)}
                          disabled={testingId === c.id}
                          className="rounded-md px-2 py-1 text-xs font-medium border border-border hover:bg-muted transition-colors disabled:opacity-50"
                        >
                          {testingId === c.id ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            "Test"
                          )}
                        </button>
                        <button
                          onClick={() => handleDelete(c.id)}
                          disabled={deletingId === c.id}
                          className="rounded-md p-1 text-muted-foreground hover:bg-red-50 hover:text-red-600 transition-colors disabled:opacity-50"
                        >
                          {deletingId === c.id ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                          ) : (
                            <Trash2 className="h-4 w-4" />
                          )}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Create Credential Dialog */}
      {showCreateDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="fixed inset-0 bg-black/50"
            onClick={() => setShowCreateDialog(false)}
          />
          <div className="relative z-10 w-full max-w-lg rounded-xl border border-border bg-card p-6 shadow-xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">Create Credential</h3>
              <button
                onClick={() => setShowCreateDialog(false)}
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

            <form onSubmit={handleCreate} className="space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1">Name</label>
                <input
                  type="text"
                  required
                  value={formName}
                  onChange={(e) => setFormName(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">Type</label>
                <select
                  required
                  value={formType}
                  onChange={(e) => {
                    // AI Note: switching type WIPES `formData`. Field names are
                    // type-specific, so carrying values over would submit
                    // fields the new type does not declare — and could leak a
                    // secret typed for one service into a request to another.
                    setFormType(e.target.value);
                    setFormData({});
                  }}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  <option value="">Select type...</option>
                  {credTypes.map((t) => (
                    <option key={t.credential_type} value={t.credential_type}>
                      {t.credential_type} - {t.description}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">
                  Description
                </label>
                <input
                  type="text"
                  value={formDescription}
                  onChange={(e) => setFormDescription(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="Optional"
                />
              </div>

              {/* Dynamic fields based on selected type.
                  AI Note: input masking is decided by NAME SUBSTRING —
                  anything containing "password", "secret" or "key" renders as
                  type="password". This is a heuristic, not a guarantee: a
                  sensitive field named e.g. "token" or "passphrase" will be
                  displayed in plain text. Extend the substring list rather
                  than assuming the server marks fields as secret. */}
              {selectedType && (
                <div className="space-y-3 rounded-lg border border-border p-4">
                  <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                    {selectedType.credential_type} fields
                  </p>
                  {selectedType.required_fields.map((field) => (
                    <div key={field}>
                      <label className="block text-sm font-medium mb-1">
                        {field}{" "}
                        <span className="text-red-500">*</span>
                      </label>
                      <input
                        type={
                          field.toLowerCase().includes("password") ||
                          field.toLowerCase().includes("secret") ||
                          field.toLowerCase().includes("key")
                            ? "password"
                            : "text"
                        }
                        required
                        value={formData[field] || ""}
                        onChange={(e) =>
                          setFormData((prev) => ({
                            ...prev,
                            [field]: e.target.value,
                          }))
                        }
                        className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                      />
                    </div>
                  ))}
                  {selectedType.optional_fields.map((field) => (
                    <div key={field}>
                      <label className="block text-sm font-medium mb-1">
                        {field}
                      </label>
                      <input
                        type={
                          field.toLowerCase().includes("password") ||
                          field.toLowerCase().includes("secret") ||
                          field.toLowerCase().includes("key")
                            ? "password"
                            : "text"
                        }
                        value={formData[field] || ""}
                        onChange={(e) =>
                          setFormData((prev) => ({
                            ...prev,
                            [field]: e.target.value,
                          }))
                        }
                        className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                        placeholder="Optional"
                      />
                    </div>
                  ))}
                </div>
              )}

              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={formShared}
                  onChange={(e) => setFormShared(e.target.checked)}
                  className="h-4 w-4 rounded border-border"
                />
                <span className="text-sm">Shared credential (visible to all users)</span>
              </label>

              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowCreateDialog(false)}
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={formSubmitting || !formType}
                  className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
                >
                  {formSubmitting && (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  )}
                  Create
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Admin Page (root) ─────────────────────────────────────────────────

/** Tab strip definition for {@link Admin}; order here is render order. */
const TAB_CONFIG: Array<{ key: AdminTab; label: string; icon: React.ElementType }> = [
  { key: "users", label: "Users", icon: Users },
  { key: "groups", label: "Groups", icon: Shield },
  { key: "credentials", label: "Credentials", icon: Key },
];

/**
 * Admin console shell (route `/admin`).
 *
 * What the user sees: an "Admin" heading, an icon tab strip, and the active
 * tab's content below it. Defaults to Users.
 *
 * AI Note: tab bodies are conditionally MOUNTED, not hidden — switching tabs
 * unmounts the previous one, discarding its local state and re-running its
 * fetch effect on return. That is why each tab owns its own loading state and
 * refetches from scratch; there is no shared cache between them (except
 * credentials, which live in a Zustand store).
 *
 * Props: none — routed component. Holds no data of its own.
 */
export default function Admin() {
  const [activeTab, setActiveTab] = useState<AdminTab>("users");

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight">Admin</h1>

      {/* Tab Navigation */}
      <div className="flex border-b border-border">
        {TAB_CONFIG.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={cn(
                "inline-flex items-center gap-2 px-4 py-2.5 text-sm font-medium transition-colors border-b-2 -mb-px",
                activeTab === tab.key
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              )}
            >
              <Icon className="h-4 w-4" />
              {tab.label}
            </button>
          );
        })}
      </div>

      {/* Tab Content */}
      {activeTab === "users" && <UsersTab />}
      {activeTab === "groups" && <GroupsTab />}
      {activeTab === "credentials" && <CredentialsTab />}
    </div>
  );
}
