/**
 * Tests for the Admin console (src/pages/Admin.tsx).
 *
 * `Admin` is a three-tab shell whose tab bodies are conditionally MOUNTED, so
 * each tab is really its own mini-page with its own loading state and its own
 * fetch-on-mount. The three are deliberately not symmetrical, and this file is
 * organised around that asymmetry:
 *
 *  1. Users       — RAW `fetch()` against /api/admin/users (+ PUT .../role and
 *                   PUT .../active), plus the ONE typed call on the page,
 *                   `api.register`. Role/active edits are OPTIMISTIC and never
 *                   inspect `res.ok`.
 *  2. Groups      — RAW `fetch()` for everything. Only the create handler checks
 *                   `res.ok`; member/pool grants fire and forget, then refetch.
 *  3. Credentials — entirely through the typed `api` client plus
 *                   `useCredentialsStore`, with a form generated from
 *                   GET /api/credentials/types.
 *
 * What is real vs stubbed: the component tree, the tab strip, the real `cn` and
 * `formatRelativeTime` helpers all run for real. Two boundaries are replaced —
 * `@/stores` (only `useCredentialsStore` is imported by this page) and
 * `@/api/client`. The /api/admin/* endpoints have NO client wrapper, so those
 * are stubbed one level lower, at `global.fetch`, via the shared `mockFetch`
 * helper — which is also what lets these tests prove the raw calls carry an
 * Authorization header and the right method/body.
 *
 * Neighbouring pieces: credentials created here are picked on `Storage.tsx`
 * (see Storage.test.tsx), pool names granted to groups correspond to pools on
 * `Pools.tsx` (Pools.test.tsx), and the credentials store itself is covered by
 * src/stores/index.test.ts.
 */
import { vi, describe, it, expect, beforeEach } from "vitest";
import {
  renderWithRouter,
  screen,
  within,
  waitFor,
  mockFetch,
  jsonResponse,
  makeUser,
  makeCredential,
} from "../test/test-utils";
import type { UserInfo, CredentialInfo, CredentialTypeInfo } from "@/types";

// ── Mock the two module boundaries the page imports ──────────────────────────
// `useCredentialsStore` is consumed as a bare destructuring call
// (`const {credentials, isLoading, fetch} = useCredentialsStore()`), but the mock
// also honours the selector form so it behaves like zustand for any future
// caller. Every `api` member the page can reach is stubbed, so an accidental
// call is recorded rather than failing with "not a function" — and no request
// ever escapes to the network.
//
// AI Note: this must live in `vi.hoisted` — `vi.mock` factories are hoisted
// above every `const`, so a factory closing over a normally-declared binding
// throws "Cannot access 'x' before initialization" at import time.
const h = vi.hoisted(() => {
  const credFetch = vi.fn().mockResolvedValue(undefined);
  const credentialsState: {
    credentials: unknown[];
    isLoading: boolean;
    fetch: typeof credFetch;
  } = { credentials: [], isLoading: false, fetch: credFetch };
  const useCredentialsStore = vi.fn(
    (selector?: (s: typeof credentialsState) => unknown) =>
      selector ? selector(credentialsState) : credentialsState
  );
  const api = {
    register: vi.fn(),
    listCredentialTypes: vi.fn(),
    createCredential: vi.fn(),
    deleteCredential: vi.fn(),
    testCredential: vi.fn(),
  };
  return { credFetch, credentialsState, useCredentialsStore, api };
});

vi.mock("@/stores", () => ({ useCredentialsStore: h.useCredentialsStore }));
vi.mock("@/api/client", () => ({ api: h.api }));

import Admin from "./Admin";

/** Narrowed alias so tests can seed typed fixtures into the hoisted state. */
const credentialsState = h.credentialsState as {
  credentials: CredentialInfo[];
  isLoading: boolean;
  fetch: typeof h.credFetch;
};

// ── Local fixtures the shared factories don't cover ──────────────────────────

/** A group as returned by GET /api/admin/groups (the shape is declared inline in Admin.tsx, not in `@/types`). */
interface GroupFixture {
  id: string;
  name: string;
  description: string | null;
  members: { user_id: string; username: string }[];
  pool_access: string[];
}

let _seq = 0;
/** Distinct, UUID-shaped id. Never assert on a literal — capture it from the fixture. */
const gid = () => `10000000-0000-0000-0000-${String(++_seq).padStart(12, "0")}`;

/** An empty group with no pool grants. Override `members` / `pool_access` per test. */
function makeGroup(overrides: Partial<GroupFixture> = {}): GroupFixture {
  return {
    id: gid(),
    name: "gpu-team",
    description: null,
    members: [],
    pool_access: [],
    ...overrides,
  };
}

/** A credential TYPE (the form-generating schema), not a stored credential. */
function makeCredType(overrides: Partial<CredentialTypeInfo> = {}): CredentialTypeInfo {
  return {
    credential_type: "s3",
    required_fields: ["access_key_id", "secret_access_key"],
    optional_fields: ["region"],
    description: "S3 bucket credentials",
    ...overrides,
  };
}

// ── Fake /api/admin/* server ────────────────────────────────────────────────

/**
 * Mutable state the default fetch router serves.
 *
 * Reset in `beforeEach`. Reads are routed by path + method; every write (PUT /
 * POST / DELETE) gets `writeBody` with `writeStatus`, which is how a test makes
 * one mutation fail without describing the whole surface.
 */
const server = {
  users: [] as UserInfo[],
  usersStatus: 200,
  groups: [] as GroupFixture[],
  groupsStatus: 200,
  writeStatus: 200,
  writeBody: {} as unknown,
};

/**
 * Install the default /api/admin/* router.
 *
 * @param impl optional override consulted first; return `null` to fall through
 *   to the default routing (used for "this one endpoint hangs / 500s" tests).
 * @returns the `vi.fn()` behind `global.fetch`, so tests can assert on calls.
 *
 * AI Note: a fresh `Response` is built inside the router on every call, because
 * a `Response` body can only be consumed once — reusing one instance across a
 * fetch-and-refetch makes the second read throw "body already read".
 */
function installFetch(
  impl?: (url: string, init?: RequestInit) => Promise<Response | null> | Response | null
) {
  return mockFetch(async (url, init) => {
    const custom = impl ? await impl(url, init) : null;
    if (custom) return custom;
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET" && url.startsWith("/api/admin/users")) {
      return jsonResponse(server.users, server.usersStatus);
    }
    if (method === "GET" && url.startsWith("/api/admin/groups")) {
      return jsonResponse(server.groups, server.groupsStatus);
    }
    return jsonResponse(server.writeBody, server.writeStatus);
  });
}

/** A fetch that never settles — the page stays in its loading frame forever. */
const hangingFetch = () => mockFetch(() => new Promise<Response>(() => {}));

/** Recorded fetch calls narrowed to a path fragment and (optionally) a method. */
function callsTo(
  fn: ReturnType<typeof mockFetch>,
  urlPart: string,
  method = "GET"
): [string, RequestInit | undefined][] {
  return (fn.mock.calls as [unknown, RequestInit | undefined][])
    .map(([url, init]) => [String(url), init] as [string, RequestInit | undefined])
    .filter(
      ([url, init]) =>
        url.includes(urlPart) && (init?.method ?? "GET").toUpperCase() === method
    );
}

// ── Query helpers ───────────────────────────────────────────────────────────

/**
 * The form control that a visible `<label>` describes.
 *
 * Needed because Admin's dialog labels are plain siblings of their inputs (no
 * `htmlFor`/`id` pair and no `aria-label`), so `getByLabelText` cannot resolve
 * them. Scoping by `selector: "label"` also keeps the table's `<th>Username</th>`
 * from matching the dialog's `<label>Username</label>`.
 *
 * AI Note: this is an accessibility gap in the page, not a test convenience —
 * screen readers cannot associate these labels either. If the labels ever gain
 * `htmlFor`, replace this helper with `getByLabelText`.
 */
function inputFor(label: string | RegExp): HTMLElement {
  const el = screen.getByText(label, { selector: "label" });
  return el.parentElement!.querySelector("input, textarea, select") as HTMLElement;
}

/** The `<tr>` owning a username / credential name cell. */
const rowFor = (text: string) => screen.getByText(text).closest("tr")!;

beforeEach(() => {
  server.users = [];
  server.usersStatus = 200;
  server.groups = [];
  server.groupsStatus = 200;
  server.writeStatus = 200;
  server.writeBody = {};

  credentialsState.credentials = [];
  credentialsState.isLoading = false;
  credentialsState.fetch = h.credFetch;
  h.credFetch.mockResolvedValue(undefined);

  // Re-declare every api implementation per test: a test that overrides one
  // (e.g. to reject) must not leak that into the next.
  h.api.register.mockResolvedValue(makeUser());
  h.api.listCredentialTypes.mockResolvedValue([]);
  h.api.createCredential.mockResolvedValue(makeCredential());
  h.api.deleteCredential.mockResolvedValue(undefined);
  h.api.testCredential.mockResolvedValue({ success: true });

  // The page's destructive actions use the browser's blocking confirm(); default
  // to "the operator said yes" and override per test.
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

// ── Tab shell ───────────────────────────────────────────────────────────────

/** The root component: heading, tab strip, and which body is mounted. */
describe("Admin page — tab shell", () => {
  /** The three tabs exist, in declaration order, as real buttons. */
  it("renders the Admin heading and all three tabs in order", async () => {
    installFetch();
    renderWithRouter(<Admin />);

    expect(screen.getByRole("heading", { name: "Admin", level: 1 })).toBeInTheDocument();
    for (const label of ["Users", "Groups", "Credentials"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    // Let the Users tab's mount fetch settle inside act() before the test ends,
    // or React logs an "update ... not wrapped in act(...)" warning.
    await waitFor(() => expect(screen.getByText(/no users found/i)).toBeInTheDocument());
  });

  /** Users is the default tab, so a fresh visit lands on the account list. */
  it("shows the Users tab by default and marks it active", async () => {
    installFetch();
    renderWithRouter(<Admin />);

    expect(screen.getByRole("button", { name: "Users" }).className).toMatch(/border-primary/);
    expect(screen.getByRole("button", { name: /create user/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/no users found/i)).toBeInTheDocument());
  });

  /**
   * Switching tabs swaps the body and moves the active underline. Regression
   * guarded: a tab strip that highlights the new tab but keeps rendering the old
   * body (or renders both).
   */
  it("switches the mounted body and the active underline when a tab is clicked", async () => {
    installFetch();
    const { user } = renderWithRouter(<Admin />);

    await user.click(screen.getByRole("button", { name: "Groups" }));

    expect(screen.getByRole("button", { name: "Groups" }).className).toMatch(/border-primary/);
    expect(screen.getByRole("button", { name: "Users" }).className).toMatch(/border-transparent/);
    expect(screen.queryByRole("button", { name: /create user/i })).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Create Group" })).toBeInTheDocument()
    );
  });

  /**
   * Tab bodies are MOUNTED, not hidden, so leaving and returning re-runs the
   * fetch effect. Pinned because it is the page's caching contract: there is no
   * shared cache between tabs, which is also why stale admin data is never shown.
   */
  it("re-fetches the user list when returning to the Users tab (bodies are unmounted, not hidden)", async () => {
    const fetchFn = installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(callsTo(fetchFn, "/api/admin/users")).toHaveLength(1));

    await user.click(screen.getByRole("button", { name: "Credentials" }));
    await user.click(screen.getByRole("button", { name: "Users" }));

    await waitFor(() => expect(callsTo(fetchFn, "/api/admin/users")).toHaveLength(2));
  });
});

// ── Users tab: the list ─────────────────────────────────────────────────────

/** The account table: loading, populated, empty and (silently) failed states. */
describe("Admin page — Users tab list", () => {
  /**
   * The pre-response frame shows a spinner row and NOT the empty state.
   * Regression guarded: flashing "No users found." on every visit, which reads
   * as a wiped user database.
   */
  it("renders a spinner row and no empty state while the user list is loading", () => {
    hangingFetch();
    renderWithRouter(<Admin />);

    expect(screen.queryByText(/no users found/i)).not.toBeInTheDocument();
    // Header row only — no data rows yet.
    expect(screen.getAllByRole("row")).toHaveLength(2);
  });

  /** One row per account, with the four documented columns. */
  it("renders a row per user with username, email, role and an active toggle", async () => {
    server.users = [
      makeUser({ username: "alice", email: "alice@example.com", role: "admin" }),
      makeUser({ username: "bob", email: "bob@example.com", role: "user" }),
    ];
    installFetch();
    renderWithRouter(<Admin />);

    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());
    expect(screen.getByText("bob")).toBeInTheDocument();
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
    // Header + two data rows.
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });

  /** `email` is optional server-side; a blank one must render a dash, not an empty cell. */
  it("renders a dash for a user with no email", async () => {
    server.users = [makeUser({ username: "svc", email: "" })];
    installFetch();
    renderWithRouter(<Admin />);

    const row = await waitFor(() => rowFor("svc"));
    expect(within(row).getByText("-")).toBeInTheDocument();
  });

  /**
   * The role pill's colour escalates with privilege. Asserted on hue substrings
   * rather than exact Tailwind classes so shade tweaks don't break the suite
   * while a genuine mis-mapping still does.
   */
  it("colors the role pill per role (admin red, manager purple, user blue)", async () => {
    server.users = [
      makeUser({ username: "root", role: "admin" }),
      makeUser({ username: "mia", role: "manager" }),
      makeUser({ username: "bob", role: "user" }),
    ];
    installFetch();
    renderWithRouter(<Admin />);

    await waitFor(() => expect(screen.getByText("admin")).toBeInTheDocument());
    expect(screen.getByText("admin").className).toMatch(/red/);
    expect(screen.getByText("manager").className).toMatch(/purple/);
    expect(screen.getByText("user").className).toMatch(/blue/);
  });

  /** The active switch is green for an enabled account and muted for a disabled one. */
  it("renders the active switch in the on position only for active users", async () => {
    server.users = [
      makeUser({ username: "on", is_active: true }),
      makeUser({ username: "off", is_active: false }),
    ];
    installFetch();
    renderWithRouter(<Admin />);

    await waitFor(() => expect(screen.getByText("on")).toBeInTheDocument());
    // Two buttons per row: the role pill, then the active toggle.
    const onToggle = within(rowFor("on")).getAllByRole("button")[1];
    const offToggle = within(rowFor("off")).getAllByRole("button")[1];
    expect(onToggle.className).toMatch(/bg-green-500/);
    expect(offToggle.className).not.toMatch(/bg-green-500/);
  });

  /** Zero accounts shows an explicit empty state rather than a bare table. */
  it("shows the empty state when the server returns no users", async () => {
    server.users = [];
    installFetch();
    renderWithRouter(<Admin />);

    expect(await screen.findByText(/no users found/i)).toBeInTheDocument();
  });

  /**
   * Documents ACTUAL behaviour on failure: `fetchUsers` only assigns when
   * `res.ok`, so a 403 (non-admin) or a 500 renders the same "No users found."
   * as a genuinely empty database — with no error banner and no redirect.
   *
   * POSSIBLE BUG (reported, not fixed): these raw /api/admin/* calls bypass
   * `api.request`, so they also skip the global 401 -> clear-token -> /login
   * handling. An expired session shows an empty admin table instead of bouncing
   * the operator to the login page.
   */
  it("renders the empty state (not an error) when the user list request fails", async () => {
    server.usersStatus = 403;
    server.users = [];
    installFetch();
    renderWithRouter(<Admin />);

    expect(await screen.findByText(/no users found/i)).toBeInTheDocument();
    expect(screen.queryByText(/forbidden|error|failed/i)).not.toBeInTheDocument();
  });

  /** A thrown request (network down) is indistinguishable from the 403 above. */
  it("stops the spinner and shows the empty state when the user request throws", async () => {
    installFetch((url) => {
      if (url.startsWith("/api/admin/users")) throw new Error("network down");
      return null;
    });
    renderWithRouter(<Admin />);

    expect(await screen.findByText(/no users found/i)).toBeInTheDocument();
  });

  /** Every raw admin read carries a Bearer header, hand-rolled from localStorage. */
  it("sends an Authorization header on the raw user-list request", async () => {
    const fetchFn = installFetch();
    renderWithRouter(<Admin />);

    await waitFor(() => expect(callsTo(fetchFn, "/api/admin/users")).toHaveLength(1));
    const [, init] = callsTo(fetchFn, "/api/admin/users")[0];
    expect((init?.headers as Record<string, string>).Authorization).toMatch(/^Bearer /);
  });
});

// ── Users tab: create ───────────────────────────────────────────────────────

/** The create-user dialog, which is the page's only typed write (`api.register`). */
describe("Admin page — create user", () => {
  /** The dialog opens with all four controls and a submit button. */
  it("opens the create dialog with username, password, email and role controls", async () => {
    installFetch();
    const { user } = renderWithRouter(<Admin />);

    await user.click(screen.getByRole("button", { name: /create user/i }));

    expect(screen.getByRole("heading", { name: "Create User" })).toBeInTheDocument();
    expect(inputFor("Username")).toBeInTheDocument();
    expect(inputFor("Password")).toHaveAttribute("type", "password");
    expect(inputFor("Email")).toHaveAttribute("type", "email");
    expect(inputFor("Role")).toHaveValue("user");
  });

  /**
   * The happy path: POST /api/auth/register with the chosen role, then a refetch
   * so the new account appears, then the dialog closes.
   */
  it("registers the user with the selected role, refetches and closes the dialog", async () => {
    const fetchFn = installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(callsTo(fetchFn, "/api/admin/users")).toHaveLength(1));

    await user.click(screen.getByRole("button", { name: /create user/i }));
    await user.type(inputFor("Username"), "carol");
    await user.type(inputFor("Password"), "s3cret!");
    await user.type(inputFor("Email"), "carol@example.com");
    await user.selectOptions(inputFor("Role"), "manager");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(h.api.register).toHaveBeenCalledWith({
        username: "carol",
        password: "s3cret!",
        email: "carol@example.com",
        role: "manager",
      })
    );
    await waitFor(() => expect(callsTo(fetchFn, "/api/admin/users")).toHaveLength(2));
    expect(screen.queryByRole("heading", { name: "Create User" })).not.toBeInTheDocument();
  });

  /**
   * A blank email is sent as `undefined` (field omitted) rather than `""`, so the
   * server stores NULL — which is what makes the table's dash fallback correct.
   */
  it("omits the email field entirely when it is left blank", async () => {
    installFetch();
    const { user } = renderWithRouter(<Admin />);

    await user.click(screen.getByRole("button", { name: /create user/i }));
    await user.type(inputFor("Username"), "svc");
    await user.type(inputFor("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(h.api.register).toHaveBeenCalled());
    expect(h.api.register.mock.calls[0][0].email).toBeUndefined();
  });

  /**
   * A recoverable input problem (duplicate username, weak password) must be
   * surfaced and must NOT discard what the operator typed. Regression guarded:
   * closing the dialog on failure, which loses the form and hides the reason.
   */
  it("shows the server error inline and keeps the dialog and its values on failure", async () => {
    h.api.register.mockRejectedValue(new Error("Username already registered"));
    installFetch();
    const { user } = renderWithRouter(<Admin />);

    await user.click(screen.getByRole("button", { name: /create user/i }));
    await user.type(inputFor("Username"), "alice");
    await user.type(inputFor("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText("Username already registered")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Create User" })).toBeInTheDocument();
    expect(inputFor("Username")).toHaveValue("alice");
  });

  /** Cancel dismisses without registering anything. */
  it("closes the dialog without registering when Cancel is clicked", async () => {
    installFetch();
    const { user } = renderWithRouter(<Admin />);

    await user.click(screen.getByRole("button", { name: /create user/i }));
    await user.type(inputFor("Username"), "temp");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("heading", { name: "Create User" })).not.toBeInTheDocument();
    expect(h.api.register).not.toHaveBeenCalled();
  });
});

// ── Users tab: inline role editing ──────────────────────────────────────────

/**
 * The role cell toggles between a pill and an uncontrolled `<select>`.
 *
 * AI Note: the select uses `defaultValue` + `onBlur`, so choosing an option
 * fires change -> save -> collapse, while blurring without choosing just
 * collapses. Both halves are pinned below.
 */
describe("Admin page — inline role editing", () => {
  /** Clicking the pill reveals a select seeded with the user's current role. */
  it("turns the role pill into a select seeded with the current role", async () => {
    server.users = [makeUser({ username: "mia", role: "manager" })];
    installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(screen.getByText("mia")).toBeInTheDocument());

    await user.click(within(rowFor("mia")).getByRole("button", { name: "manager" }));

    expect(screen.getByRole("combobox")).toHaveValue("manager");
  });

  /** Choosing a role PUTs it and repaints the pill with the new value. */
  it("PUTs the new role and updates the pill when an option is chosen", async () => {
    server.users = [makeUser({ username: "bob", role: "user" })];
    const fetchFn = installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());

    await user.click(within(rowFor("bob")).getByRole("button", { name: "user" }));
    await user.selectOptions(screen.getByRole("combobox"), "admin");

    await waitFor(() => expect(callsTo(fetchFn, "/role", "PUT")).toHaveLength(1));
    const [url, init] = callsTo(fetchFn, "/role", "PUT")[0];
    expect(url).toBe(`/api/admin/users/${server.users[0].id}/role`);
    expect(init?.body).toBe(JSON.stringify({ role: "admin" }));
    await waitFor(() =>
      expect(within(rowFor("bob")).getByText("admin")).toBeInTheDocument()
    );
  });

  /** The select collapses back to a pill once a role has been saved. */
  it("collapses the select back to a pill after saving", async () => {
    server.users = [makeUser({ username: "bob", role: "user" })];
    installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());

    await user.click(within(rowFor("bob")).getByRole("button", { name: "user" }));
    await user.selectOptions(screen.getByRole("combobox"), "manager");

    await waitFor(() => expect(screen.queryByRole("combobox")).not.toBeInTheDocument());
  });

  /** Blurring without choosing must exit edit mode and change nothing. */
  it("exits edit mode without a request when the select is blurred unchanged", async () => {
    server.users = [makeUser({ username: "bob", role: "user" })];
    const fetchFn = installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());

    await user.click(within(rowFor("bob")).getByRole("button", { name: "user" }));
    await user.click(screen.getByRole("heading", { name: "Users", level: 2 }));

    await waitFor(() => expect(screen.queryByRole("combobox")).not.toBeInTheDocument());
    expect(callsTo(fetchFn, "/role", "PUT")).toHaveLength(0);
    expect(within(rowFor("bob")).getByText("user")).toBeInTheDocument();
  });

  /**
   * Documents ACTUAL behaviour: the local patch is applied WITHOUT checking
   * `res.ok`, so a rejected privilege change still repaints the pill.
   *
   * POSSIBLE BUG (reported, not fixed): `handleRoleChange` never inspects the
   * response. A server that refuses the change (e.g. demoting the last admin)
   * leaves the table claiming a role the backend does not hold, until the next
   * refetch. Security-relevant, so pinned here rather than left implicit.
   */
  it("optimistically shows the new role even when the server rejects the change", async () => {
    server.users = [makeUser({ username: "bob", role: "user" })];
    server.writeStatus = 500;
    server.writeBody = { detail: "cannot change role" };
    installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());

    await user.click(within(rowFor("bob")).getByRole("button", { name: "user" }));
    await user.selectOptions(screen.getByRole("combobox"), "admin");

    await waitFor(() =>
      expect(within(rowFor("bob")).getByText("admin")).toBeInTheDocument()
    );
    expect(screen.queryByText(/cannot change role/i)).not.toBeInTheDocument();
  });
});

// ── Users tab: activate / deactivate ────────────────────────────────────────

/** The active switch. Deactivating an account is a security control. */
describe("Admin page — activate/deactivate user", () => {
  /** Toggling an active user sends `is_active: false` (the handler negates the CURRENT state). */
  it("PUTs is_active:false when an active user is toggled", async () => {
    server.users = [makeUser({ username: "alice", is_active: true })];
    const fetchFn = installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    await user.click(within(rowFor("alice")).getAllByRole("button")[1]);

    await waitFor(() => expect(callsTo(fetchFn, "/active", "PUT")).toHaveLength(1));
    const [url, init] = callsTo(fetchFn, "/active", "PUT")[0];
    expect(url).toBe(`/api/admin/users/${server.users[0].id}/active`);
    expect(init?.body).toBe(JSON.stringify({ is_active: false }));
  });

  /** ...and the reverse direction re-enables the account. */
  it("PUTs is_active:true when a disabled user is toggled", async () => {
    server.users = [makeUser({ username: "ghost", is_active: false })];
    const fetchFn = installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(screen.getByText("ghost")).toBeInTheDocument());

    await user.click(within(rowFor("ghost")).getAllByRole("button")[1]);

    await waitFor(() => expect(callsTo(fetchFn, "/active", "PUT")).toHaveLength(1));
    expect(callsTo(fetchFn, "/active", "PUT")[0][1]?.body).toBe(
      JSON.stringify({ is_active: true })
    );
  });

  /** The switch repaints immediately so the operator sees the change land. */
  it("flips the switch styling after a successful toggle", async () => {
    server.users = [makeUser({ username: "alice", is_active: true })];
    installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    await user.click(within(rowFor("alice")).getAllByRole("button")[1]);

    await waitFor(() =>
      expect(within(rowFor("alice")).getAllByRole("button")[1].className).not.toMatch(
        /bg-green-500/
      )
    );
  });

  /**
   * Documents ACTUAL behaviour: same un-checked optimistic update as role
   * changes, on a control whose whole purpose is to stop someone logging in.
   *
   * POSSIBLE BUG (reported, not fixed): a rejected deactivation still shows the
   * account as disabled, so an operator can believe they have locked an account
   * out when they have not.
   */
  it("shows the account as disabled even when the server rejects the deactivation", async () => {
    server.users = [makeUser({ username: "alice", is_active: true })];
    server.writeStatus = 500;
    installFetch();
    const { user } = renderWithRouter(<Admin />);
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    await user.click(within(rowFor("alice")).getAllByRole("button")[1]);

    await waitFor(() =>
      expect(within(rowFor("alice")).getAllByRole("button")[1].className).toMatch(/bg-muted/)
    );
  });
});

// ── Groups tab: the accordion ───────────────────────────────────────────────

/** The group list, its loading/empty/failed frames, and expand/collapse. */
describe("Admin page — Groups tab list", () => {
  /** Switch to the Groups tab and wait for its first render to settle. */
  async function openGroups() {
    const { user, ...view } = renderWithRouter(<Admin />);
    await user.click(screen.getByRole("button", { name: "Groups" }));
    return { user, ...view };
  }

  /**
   * The whole tab body is replaced by a spinner while loading — the heading and
   * Create Group button are not rendered yet. Pinned because it means a test
   * cannot click Create Group before the list resolves.
   */
  it("renders only a spinner (no heading, no create button) while groups load", async () => {
    hangingFetch();
    const { user } = renderWithRouter(<Admin />);
    await user.click(screen.getByRole("button", { name: "Groups" }));

    expect(screen.queryByRole("heading", { name: "Groups", level: 2 })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create Group" })).not.toBeInTheDocument();
  });

  /** One collapsible card per group, showing name, member count and description. */
  it("renders a collapsible card per group with its member count and description", async () => {
    server.groups = [
      makeGroup({
        name: "gpu-team",
        description: "GPU owners",
        members: [
          { user_id: "u1", username: "alice" },
          { user_id: "u2", username: "bob" },
        ],
      }),
      makeGroup({ name: "solo", members: [{ user_id: "u3", username: "carol" }] }),
    ];
    installFetch();
    await openGroups();

    expect(await screen.findByRole("button", { name: /gpu-team/ })).toBeInTheDocument();
    expect(screen.getByText("2 members")).toBeInTheDocument();
    // Correct singularisation for a one-member group.
    expect(screen.getByText("1 member")).toBeInTheDocument();
    expect(screen.getByText("GPU owners")).toBeInTheDocument();
  });

  /** A group with no members reads "0 members" (plural branch). */
  it("labels an empty group as '0 members'", async () => {
    server.groups = [makeGroup({ name: "empty-team", members: [] })];
    installFetch();
    await openGroups();

    expect(await screen.findByText("0 members")).toBeInTheDocument();
  });

  /** Zero groups shows an explicit empty state. */
  it("shows the empty state when there are no groups", async () => {
    server.groups = [];
    installFetch();
    await openGroups();

    expect(await screen.findByText(/no groups created yet/i)).toBeInTheDocument();
  });

  /**
   * Documents ACTUAL behaviour: a failed group fetch is indistinguishable from
   * an empty list — same silent-failure design as the Users tab.
   */
  it("renders the empty state (not an error) when the group list request fails", async () => {
    server.groupsStatus = 500;
    installFetch();
    await openGroups();

    expect(await screen.findByText(/no groups created yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/error|failed/i)).not.toBeInTheDocument();
  });

  /** Expanding reveals the two management sections. */
  it("reveals the Members and Pool Access sections when a group is expanded", async () => {
    server.groups = [makeGroup({ name: "gpu-team" })];
    installFetch();
    const { user } = await openGroups();

    await user.click(await screen.findByRole("button", { name: /gpu-team/ }));

    expect(screen.getByRole("heading", { name: "Members", level: 4 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Pool Access", level: 4 })).toBeInTheDocument();
  });

  /** Clicking the same header again collapses it. */
  it("collapses an expanded group when its header is clicked again", async () => {
    server.groups = [makeGroup({ name: "gpu-team" })];
    installFetch();
    const { user } = await openGroups();

    const header = await screen.findByRole("button", { name: /gpu-team/ });
    await user.click(header);
    await user.click(header);

    expect(screen.queryByRole("heading", { name: "Members", level: 4 })).not.toBeInTheDocument();
  });

  /**
   * Only one group is expanded at a time (`expandedId` is a single value).
   * Regression guarded: an accordion that accumulates open panels, which makes
   * the "+ Add Member" inputs ambiguous.
   */
  it("keeps at most one group expanded at a time", async () => {
    server.groups = [makeGroup({ name: "alpha" }), makeGroup({ name: "beta" })];
    installFetch();
    const { user } = await openGroups();

    await user.click(await screen.findByRole("button", { name: /alpha/ }));
    await user.click(screen.getByRole("button", { name: /beta/ }));

    expect(screen.getAllByRole("heading", { name: "Members", level: 4 })).toHaveLength(1);
  });

  /** Empty sub-lists get their own copy rather than rendering nothing. */
  it("shows 'No members.' and 'No pool access configured.' for an empty group", async () => {
    server.groups = [makeGroup({ name: "gpu-team", members: [], pool_access: [] })];
    installFetch();
    const { user } = await openGroups();

    await user.click(await screen.findByRole("button", { name: /gpu-team/ }));

    expect(screen.getByText("No members.")).toBeInTheDocument();
    expect(screen.getByText("No pool access configured.")).toBeInTheDocument();
  });
});

// ── Groups tab: membership ──────────────────────────────────────────────────

/** Adding and removing members, both of which take free text and refetch. */
describe("Admin page — group membership", () => {
  /** Expand the first group of `server.groups` and return the userEvent instance. */
  async function expandFirstGroup() {
    const { user } = renderWithRouter(<Admin />);
    await user.click(screen.getByRole("button", { name: "Groups" }));
    await user.click(await screen.findByRole("button", { name: new RegExp(server.groups[0].name) }));
    return user;
  }

  /** Member chips are rendered, each with its own remove control. */
  it("renders a chip per member with a remove button", async () => {
    server.groups = [
      makeGroup({
        members: [
          { user_id: "u1", username: "alice" },
          { user_id: "u2", username: "bob" },
        ],
      }),
    ];
    installFetch();
    await expandFirstGroup();

    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Remove member" })).toHaveLength(2);
  });

  /** The add-member input is revealed on demand, not always present. */
  it("toggles the add-member input open and closed via '+ Add Member'", async () => {
    server.groups = [makeGroup()];
    const fetchFn = installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: /add member/i }));
    expect(screen.getByPlaceholderText("Username")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /add member/i }));
    expect(screen.queryByPlaceholderText("Username")).not.toBeInTheDocument();
    expect(callsTo(fetchFn, "/members", "POST")).toHaveLength(0);
  });

  /** The happy path: POST the username, refetch, and close the input. */
  it("POSTs the typed username, refetches the groups and closes the input", async () => {
    const group = makeGroup();
    server.groups = [group];
    const fetchFn = installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: /add member/i }));
    await user.type(screen.getByPlaceholderText("Username"), "carol");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(callsTo(fetchFn, "/members", "POST")).toHaveLength(1));
    const [url, init] = callsTo(fetchFn, "/members", "POST")[0];
    expect(url).toBe(`/api/admin/groups/${group.id}/members`);
    expect(init?.body).toBe(JSON.stringify({ username: "carol" }));
    await waitFor(() =>
      expect(screen.queryByPlaceholderText("Username")).not.toBeInTheDocument()
    );
    // Two GETs: the initial load and the post-mutation refetch.
    expect(callsTo(fetchFn, "/api/admin/groups")).toHaveLength(2);
  });

  /** Enter in the field submits, so the flow is keyboard-only usable. */
  it("submits the member add when Enter is pressed in the input", async () => {
    server.groups = [makeGroup()];
    const fetchFn = installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: /add member/i }));
    await user.type(screen.getByPlaceholderText("Username"), "carol{Enter}");

    await waitFor(() => expect(callsTo(fetchFn, "/members", "POST")).toHaveLength(1));
  });

  /** A blank/whitespace username is guarded client-side and sends nothing. */
  it("sends no request for a whitespace-only username and keeps the input open", async () => {
    server.groups = [makeGroup()];
    const fetchFn = installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: /add member/i }));
    await user.type(screen.getByPlaceholderText("Username"), "   ");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(callsTo(fetchFn, "/members", "POST")).toHaveLength(0);
    expect(screen.getByPlaceholderText("Username")).toBeInTheDocument();
  });

  /**
   * Documents ACTUAL behaviour: the input closes and clears on a completed
   * fetch, not on a successful REQUEST.
   *
   * POSSIBLE BUG (reported, not fixed): adding an unknown username 404s, yet the
   * UI closes the input exactly as if it worked. The only signal is that the
   * refetched chip list is unchanged — the most confusing behaviour on this tab.
   */
  it("closes the input as if it worked when the username does not exist (404)", async () => {
    server.groups = [makeGroup({ members: [] })];
    server.writeStatus = 404;
    server.writeBody = { detail: "User not found" };
    installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: /add member/i }));
    await user.type(screen.getByPlaceholderText("Username"), "nobody");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(screen.queryByPlaceholderText("Username")).not.toBeInTheDocument()
    );
    expect(screen.queryByText(/user not found/i)).not.toBeInTheDocument();
    expect(screen.getByText("No members.")).toBeInTheDocument();
  });

  /** Removing a member DELETEs by user id, then refetches the whole list. */
  it("DELETEs the membership by user id and refetches the group list", async () => {
    const group = makeGroup({ members: [{ user_id: "u1", username: "alice" }] });
    server.groups = [group];
    const fetchFn = installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: "Remove member" }));

    await waitFor(() => expect(callsTo(fetchFn, "/members/", "DELETE")).toHaveLength(1));
    expect(callsTo(fetchFn, "/members/", "DELETE")[0][0]).toBe(
      `/api/admin/groups/${group.id}/members/u1`
    );
    await waitFor(() => expect(callsTo(fetchFn, "/api/admin/groups")).toHaveLength(2));
  });

  /** Removal is deliberately unconfirmed — re-adding is a one-field operation. */
  it("removes a member without a confirmation prompt", async () => {
    server.groups = [makeGroup({ members: [{ user_id: "u1", username: "alice" }] })];
    const fetchFn = installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: "Remove member" }));

    await waitFor(() => expect(callsTo(fetchFn, "/members/", "DELETE")).toHaveLength(1));
    expect(window.confirm).not.toHaveBeenCalled();
  });
});

// ── Groups tab: pool access grants ──────────────────────────────────────────

/**
 * Pool grants widen which pools a group's members may target when submitting
 * jobs, so this section is security-relevant.
 */
describe("Admin page — group pool access", () => {
  async function expandFirstGroup() {
    const { user } = renderWithRouter(<Admin />);
    await user.click(screen.getByRole("button", { name: "Groups" }));
    await user.click(await screen.findByRole("button", { name: new RegExp(server.groups[0].name) }));
    return user;
  }

  /** Existing grants render as chips. */
  it("renders a chip per granted pool", async () => {
    server.groups = [makeGroup({ pool_access: ["gpu-cluster", "build-farm"] })];
    installFetch();
    await expandFirstGroup();

    expect(screen.getByText("gpu-cluster")).toBeInTheDocument();
    expect(screen.getByText("build-farm")).toBeInTheDocument();
  });

  /** The happy path: POST `{pool_name}` from free text, refetch, close the input. */
  it("POSTs the pool name, refetches and closes the input", async () => {
    const group = makeGroup();
    server.groups = [group];
    const fetchFn = installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: /add pool/i }));
    await user.type(screen.getByPlaceholderText("Pool name or ID"), "gpu-cluster");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(callsTo(fetchFn, "/pools", "POST")).toHaveLength(1));
    const [url, init] = callsTo(fetchFn, "/pools", "POST")[0];
    expect(url).toBe(`/api/admin/groups/${group.id}/pools`);
    expect(init?.body).toBe(JSON.stringify({ pool_name: "gpu-cluster" }));
    await waitFor(() =>
      expect(screen.queryByPlaceholderText("Pool name or ID")).not.toBeInTheDocument()
    );
  });

  /** Enter submits the grant too. */
  it("submits the pool grant when Enter is pressed", async () => {
    server.groups = [makeGroup()];
    const fetchFn = installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: /add pool/i }));
    await user.type(screen.getByPlaceholderText("Pool name or ID"), "build-farm{Enter}");

    await waitFor(() => expect(callsTo(fetchFn, "/pools", "POST")).toHaveLength(1));
  });

  /** A blank grant is guarded client-side. */
  it("sends no request for a blank pool name", async () => {
    server.groups = [makeGroup()];
    const fetchFn = installFetch();
    const user = await expandFirstGroup();

    await user.click(screen.getByRole("button", { name: /add pool/i }));
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(callsTo(fetchFn, "/pools", "POST")).toHaveLength(0);
  });

  /**
   * Documents a real gap: pool chips are display-only, so a grant made here can
   * never be revoked from this UI.
   *
   * POSSIBLE BUG (reported, not fixed): there is no DELETE control for
   * `pool_access`, unlike members. Revoking a mistaken grant requires a direct
   * API call. Pinned as current behaviour — if a revoke button is ever added,
   * this test fails and should be replaced with real assertions on it.
   */
  it("offers no way to revoke a pool grant (known gap)", async () => {
    server.groups = [makeGroup({ pool_access: ["gpu-cluster"] })];
    installFetch();
    await expandFirstGroup();

    const chip = screen.getByText("gpu-cluster");
    expect(chip.querySelector("button")).toBeNull();
    expect(screen.queryByRole("button", { name: /remove pool|revoke/i })).not.toBeInTheDocument();
  });
});

// ── Groups tab: create ─────────────────────────────────────────────────────

/** The only raw-fetch handler on the page that inspects `res.ok`. */
describe("Admin page — create group", () => {
  async function openGroups() {
    const { user } = renderWithRouter(<Admin />);
    await user.click(screen.getByRole("button", { name: "Groups" }));
    await screen.findByRole("button", { name: "Create Group" });
    return user;
  }

  /** The dialog opens with a name and an optional description. */
  it("opens the create-group dialog with name and description fields", async () => {
    installFetch();
    const user = await openGroups();

    await user.click(screen.getByRole("button", { name: "Create Group" }));

    expect(screen.getByRole("heading", { name: "Create Group" })).toBeInTheDocument();
    expect(inputFor("Name")).toBeInTheDocument();
    expect(inputFor("Description")).toBeInTheDocument();
  });

  /** The happy path: POST name + description, refetch, close. */
  it("POSTs the group, refetches the list and closes the dialog", async () => {
    const fetchFn = installFetch();
    const user = await openGroups();

    await user.click(screen.getByRole("button", { name: "Create Group" }));
    await user.type(inputFor("Name"), "gpu-team");
    await user.type(inputFor("Description"), "GPU owners");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(callsTo(fetchFn, "/api/admin/groups", "POST")).toHaveLength(1));
    expect(callsTo(fetchFn, "/api/admin/groups", "POST")[0][1]?.body).toBe(
      JSON.stringify({ name: "gpu-team", description: "GPU owners" })
    );
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Create Group" })).not.toBeInTheDocument()
    );
  });

  /** A blank description is sent as explicit `null`, not omitted (unlike the user form). */
  it("sends description as null when it is left blank", async () => {
    const fetchFn = installFetch();
    const user = await openGroups();

    await user.click(screen.getByRole("button", { name: "Create Group" }));
    await user.type(inputFor("Name"), "bare");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(callsTo(fetchFn, "/api/admin/groups", "POST")).toHaveLength(1));
    expect(callsTo(fetchFn, "/api/admin/groups", "POST")[0][1]?.body).toBe(
      JSON.stringify({ name: "bare", description: null })
    );
  });

  /**
   * A duplicate name is a normal, recoverable 4xx that must be shown. This is
   * the one place on the Groups tab where `detail` is unwrapped from the body.
   */
  it("shows the server's detail message and keeps the dialog open on failure", async () => {
    server.writeStatus = 409;
    server.writeBody = { detail: "Group name already exists" };
    installFetch();
    const user = await openGroups();

    await user.click(screen.getByRole("button", { name: "Create Group" }));
    await user.type(inputFor("Name"), "gpu-team");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText("Group name already exists")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Create Group" })).toBeInTheDocument();
  });

  /** A failure body with no `detail` falls back to a generic message rather than "undefined". */
  it("falls back to a generic message when the error body has no detail", async () => {
    server.writeStatus = 500;
    server.writeBody = {};
    installFetch();
    const user = await openGroups();

    await user.click(screen.getByRole("button", { name: "Create Group" }));
    await user.type(inputFor("Name"), "gpu-team");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText("Failed to create group")).toBeInTheDocument();
  });
});

// ── Credentials tab: the list ───────────────────────────────────────────────

/** The encrypted secret store — the only tab that goes through the typed client. */
describe("Admin page — Credentials tab list", () => {
  /** Switch to the Credentials tab. */
  async function openCredentials() {
    const { user, ...view } = renderWithRouter(<Admin />);
    await user.click(screen.getByRole("button", { name: "Credentials" }));
    return { user, ...view };
  }

  /** Mount loads both the list and the type catalogue that drives the form. */
  it("fetches the credential list and the type catalogue on mount", async () => {
    installFetch();
    await openCredentials();

    await waitFor(() => expect(h.credFetch).toHaveBeenCalledTimes(1));
    expect(h.api.listCredentialTypes).toHaveBeenCalledTimes(1);
  });

  /**
   * Loading suppresses both rows and the empty state. Regression guarded:
   * flashing "No credentials stored." over a populated secret store.
   */
  it("renders a spinner row and no empty state while credentials load", async () => {
    credentialsState.isLoading = true;
    installFetch();
    await openCredentials();

    expect(screen.queryByText(/no credentials stored/i)).not.toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(2);
  });

  /** A populated row shows every metadata column the API returns. */
  it("renders a row per credential with type, description, shared flag and age", async () => {
    credentialsState.credentials = [
      makeCredential({
        name: "prod-s3",
        credential_type: "s3",
        description: "Prod bucket",
        is_shared: true,
        created_at: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
      }),
    ];
    installFetch();
    await openCredentials();

    const row = rowFor("prod-s3");
    expect(within(row).getByText("s3")).toBeInTheDocument();
    expect(within(row).getByText("Prod bucket")).toBeInTheDocument();
    expect(within(row).getByText("Yes")).toBeInTheDocument();
    expect(within(row).getByText("2h ago")).toBeInTheDocument();
  });

  /** A private credential and a missing description use their fallbacks. */
  it("renders 'No' for a private credential and a dash for a missing description", async () => {
    credentialsState.credentials = [
      makeCredential({ name: "private-cred", description: null, is_shared: false }),
    ];
    installFetch();
    await openCredentials();

    const row = rowFor("private-cred");
    expect(within(row).getByText("No")).toBeInTheDocument();
    expect(within(row).getByText("-")).toBeInTheDocument();
  });

  /** Zero credentials shows an explicit empty state. */
  it("shows the empty state when no credentials are stored", async () => {
    credentialsState.credentials = [];
    installFetch();
    await openCredentials();

    expect(screen.getByText(/no credentials stored/i)).toBeInTheDocument();
  });

  /**
   * There is no edit action, by design: the encrypted secret is never sent to
   * the browser, so nothing could pre-fill an edit form. Only create/test/delete
   * exist. Pinned so an "edit credential" flow cannot be added without a
   * deliberate decision about where the existing secret would come from.
   */
  it("offers only Test and Delete per row — never an edit action", async () => {
    credentialsState.credentials = [makeCredential({ name: "prod-s3" })];
    installFetch();
    await openCredentials();

    const row = rowFor("prod-s3");
    expect(within(row).getAllByRole("button")).toHaveLength(2);
    expect(within(row).getByRole("button", { name: "Test" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /edit/i })).not.toBeInTheDocument();
  });
});

// ── Credentials tab: test / delete ─────────────────────────────────────────

/** The per-row Test probe and the confirm-gated Delete. */
describe("Admin page — credential test and delete", () => {
  async function openCredentials(creds: CredentialInfo[]) {
    credentialsState.credentials = creds;
    installFetch();
    const { user } = renderWithRouter(<Admin />);
    await user.click(screen.getByRole("button", { name: "Credentials" }));
    return user;
  }

  /** A successful probe shows a green tick against that row. */
  it("shows a green tick after a successful credential test", async () => {
    const cred = makeCredential({ name: "prod-s3" });
    h.api.testCredential.mockResolvedValue({ success: true });
    const user = await openCredentials([cred]);

    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => expect(h.api.testCredential).toHaveBeenCalledWith(cred.id));
    await waitFor(() =>
      expect(rowFor("prod-s3").querySelector(".text-green-500")).not.toBeNull()
    );
  });

  /** A `{success: false}` probe (still a 200) shows a red cross. */
  it("shows a red cross when the probe reports the credential is invalid", async () => {
    h.api.testCredential.mockResolvedValue({ success: false, error: "Access denied" });
    const user = await openCredentials([makeCredential({ name: "bad-s3" })]);

    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() =>
      expect(rowFor("bad-s3").querySelector(".text-red-500")).not.toBeNull()
    );
  });

  /**
   * Documents ACTUAL behaviour: a THROWN probe is recorded as `false`, so
   * "could not run the test" is displayed identically to "the credential is
   * invalid".
   *
   * POSSIBLE BUG (reported, not fixed): the conflation means a red cross does
   * not distinguish a bad secret from an unreachable Nexus server. Deliberate
   * for an ops dashboard, but worth surfacing.
   */
  it("shows a red cross when the probe request itself fails (failure conflation)", async () => {
    h.api.testCredential.mockRejectedValue(new Error("HTTP 500"));
    const user = await openCredentials([makeCredential({ name: "prod-s3" })]);

    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() =>
      expect(rowFor("prod-s3").querySelector(".text-red-500")).not.toBeNull()
    );
    expect(screen.queryByText(/http 500/i)).not.toBeInTheDocument();
  });

  /** Confirming the prompt deletes and reloads the list. */
  it("deletes the credential and refetches the list once confirmed", async () => {
    const cred = makeCredential({ name: "prod-s3" });
    const user = await openCredentials([cred]);
    await waitFor(() => expect(h.credFetch).toHaveBeenCalledTimes(1));

    await user.click(within(rowFor("prod-s3")).getAllByRole("button")[1]);

    await waitFor(() => expect(h.api.deleteCredential).toHaveBeenCalledWith(cred.id));
    await waitFor(() => expect(h.credFetch).toHaveBeenCalledTimes(2));
  });

  /**
   * Declining the prompt is a hard stop. Regression guarded: a confirm() whose
   * return value is ignored, which would delete a secret on a stray click.
   */
  it("deletes nothing when the confirm prompt is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = await openCredentials([makeCredential({ name: "prod-s3" })]);

    await user.click(within(rowFor("prod-s3")).getAllByRole("button")[1]);

    expect(h.api.deleteCredential).not.toHaveBeenCalled();
  });
});

// ── Credentials tab: create ────────────────────────────────────────────────

/** The dynamic create form, generated from the selected type's field contract. */
describe("Admin page — create credential", () => {
  async function openCreateDialog(types: CredentialTypeInfo[]) {
    h.api.listCredentialTypes.mockResolvedValue(types);
    installFetch();
    const { user } = renderWithRouter(<Admin />);
    await user.click(screen.getByRole("button", { name: "Credentials" }));
    await user.click(screen.getByRole("button", { name: /create credential/i }));
    return user;
  }

  /** Submitting is impossible until a type is chosen, since the form is type-driven. */
  it("disables the submit button until a credential type is selected", async () => {
    const user = await openCreateDialog([makeCredType()]);

    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    await user.selectOptions(screen.getByRole("combobox"), "s3");

    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
  });

  /** Choosing a type renders its required fields (marked) and its optional ones. */
  it("renders the selected type's required and optional fields", async () => {
    const user = await openCreateDialog([makeCredType()]);

    await user.selectOptions(screen.getByRole("combobox"), "s3");

    expect(screen.getByText("s3 fields")).toBeInTheDocument();
    expect(inputFor(/^access_key_id/)).toBeRequired();
    expect(inputFor(/^secret_access_key/)).toBeRequired();
    expect(inputFor(/^region/)).not.toBeRequired();
  });

  /**
   * Secret masking is decided by NAME SUBSTRING ("password" / "secret" / "key").
   *
   * POSSIBLE BUG (reported, not fixed): the heuristic misses obvious secret
   * names — a field called `token` renders as plain text. Pinned in both
   * directions so extending the substring list is a deliberate change.
   */
  it("masks fields named like secrets and leaves 'token' in plain text (heuristic gap)", async () => {
    const user = await openCreateDialog([
      makeCredType({
        credential_type: "api",
        required_fields: ["secret_access_key", "token"],
        optional_fields: ["endpoint"],
        description: "API token",
      }),
    ]);

    await user.selectOptions(screen.getByRole("combobox"), "api");

    expect(inputFor(/^secret_access_key/)).toHaveAttribute("type", "password");
    // "token" contains none of password/secret/key -> rendered unmasked.
    expect(inputFor(/^token/)).toHaveAttribute("type", "text");
    expect(inputFor(/^endpoint/)).toHaveAttribute("type", "text");
  });

  /**
   * Switching type WIPES the collected values. Regression guarded (security):
   * carrying values over would submit a secret typed for one service in a
   * request destined for another.
   */
  it("clears already-typed field values when the credential type is changed", async () => {
    const user = await openCreateDialog([
      makeCredType(),
      makeCredType({
        credential_type: "ssh",
        required_fields: ["username"],
        optional_fields: [],
        description: "SSH login",
      }),
    ]);

    await user.selectOptions(screen.getByRole("combobox"), "s3");
    await user.type(inputFor(/^access_key_id/), "AKIA-LEAK");
    await user.selectOptions(screen.getByRole("combobox"), "ssh");
    await user.selectOptions(screen.getByRole("combobox"), "s3");

    expect(inputFor(/^access_key_id/)).toHaveValue("");
  });

  /** The happy path: the dynamic values are posted as the `data` map, then the list reloads. */
  it("creates the credential with the dynamic field values and refetches", async () => {
    const user = await openCreateDialog([makeCredType()]);
    await waitFor(() => expect(h.credFetch).toHaveBeenCalledTimes(1));

    await user.type(inputFor("Name"), "prod-s3");
    await user.selectOptions(screen.getByRole("combobox"), "s3");
    await user.type(inputFor(/^access_key_id/), "AKIA123");
    await user.type(inputFor(/^secret_access_key/), "shhh");
    await user.click(screen.getByLabelText(/shared credential/i));
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(h.api.createCredential).toHaveBeenCalledTimes(1));
    expect(h.api.createCredential.mock.calls[0][0]).toMatchObject({
      name: "prod-s3",
      credential_type: "s3",
      is_shared: true,
      data: { access_key_id: "AKIA123", secret_access_key: "shhh" },
    });
    // Blank description is omitted rather than sent as "".
    expect(h.api.createCredential.mock.calls[0][0].description).toBeUndefined();
    await waitFor(() => expect(h.credFetch).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Create Credential" })).not.toBeInTheDocument()
    );
  });

  /**
   * A failed create keeps the dialog and everything typed into it — including
   * secrets, which stay in component state and never reach storage.
   */
  it("shows the error inline and preserves the form when creation fails", async () => {
    h.api.createCredential.mockRejectedValue(new Error("Credential name in use"));
    const user = await openCreateDialog([makeCredType()]);

    await user.type(inputFor("Name"), "prod-s3");
    await user.selectOptions(screen.getByRole("combobox"), "s3");
    await user.type(inputFor(/^access_key_id/), "AKIA123");
    await user.type(inputFor(/^secret_access_key/), "shhh");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText("Credential name in use")).toBeInTheDocument();
    expect(inputFor("Name")).toHaveValue("prod-s3");
    expect(inputFor(/^access_key_id/)).toHaveValue("AKIA123");
  });

  /**
   * Documents ACTUAL behaviour when the type catalogue cannot be loaded: the
   * failure is swallowed, so the Type dropdown holds only its placeholder and
   * the Create button can never be enabled — the whole flow is dead with no
   * message explaining why.
   */
  it("leaves the type dropdown empty and the form unusable when the type catalogue fails", async () => {
    h.api.listCredentialTypes.mockRejectedValue(new Error("HTTP 500"));
    installFetch();
    const { user } = renderWithRouter(<Admin />);
    await user.click(screen.getByRole("button", { name: "Credentials" }));
    await user.click(screen.getByRole("button", { name: /create credential/i }));

    const select = screen.getByRole("combobox");
    expect(within(select).getAllByRole("option")).toHaveLength(1);
    expect(within(select).getByRole("option", { name: /select type/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });

  /** Cancel dismisses without writing a secret anywhere. */
  it("closes the dialog without creating anything when Cancel is clicked", async () => {
    const user = await openCreateDialog([makeCredType()]);

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("heading", { name: "Create Credential" })).not.toBeInTheDocument();
    expect(h.api.createCredential).not.toHaveBeenCalled();
  });
});
