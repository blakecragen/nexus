/**
 * Shared test utilities for the Nexus frontend suite.
 *
 * - `renderWithRouter`: render a component inside a MemoryRouter so components
 *   using react-router hooks (useNavigate, Link, useParams) work in isolation.
 * - `mockFetch`: install a typed fetch stub returning a sequence of responses.
 * - Fixture factories (makeUser, makeNode, makeJob, ...) produce valid,
 *   override-able domain objects matching `src/types`.
 *
 * Role in the suite: every page/component test imports from here instead of
 * `@testing-library/react` directly — the re-export at the bottom means
 * `screen`, `waitFor`, `within`, `fireEvent` etc. all come from one place.
 * Global environment stubbing (localStorage, matchMedia, ResizeObserver) lives
 * in the sibling `./setup.ts`, which vitest loads automatically.
 *
 * AI Note: this file stubs the network at the `fetch` boundary rather than
 * mocking `@/api/client`. That is deliberate — it keeps the client's real
 * behaviour (Bearer header, 401 redirect, `{detail}` error unwrapping, 204
 * handling) under test instead of mocking it away.
 */
import type { ReactElement } from "react";
import { render, type RenderOptions } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import type {
  UserInfo,
  NodeInfo,
  PoolInfo,
  JobInfo,
  StepRunInfo,
  StorageBackendInfo,
  CredentialInfo,
  StepSchemaInfo,
} from "@/types";

// ── Render helpers ───────────────────────────────────────────────────────────

/**
 * Render `ui` inside a `MemoryRouter` and return RTL's result plus a
 * pre-configured `userEvent` instance.
 *
 * Two modes:
 * - No `path`: the component is rendered directly as the router's child. Enough
 *   for anything that only needs `useNavigate` or renders `<Link>`s.
 * - With `path`: the component is mounted behind a matching `<Route>`, which is
 *   what makes `useParams()` resolve. Required for route-parameterised pages —
 *   e.g. JobDetail must be rendered with
 *   `{route: "/jobs/abc", path: "/jobs/:id"}` or `id` is undefined and the page
 *   silently skips its data fetch.
 *
 * @param route initial history entry (the "current URL"). Defaults to "/".
 * @param path  optional route pattern to mount `ui` behind.
 * @returns `{user, ...renderResult}` — `user` is `userEvent.setup()`, created
 *   per render so each test gets its own pointer/keyboard state.
 *
 * AI Note: `userEvent.setup()` must be called BEFORE `render` (as it is here).
 * Calling it after, or reusing one instance across tests, breaks its
 * document/clipboard bookkeeping.
 *
 * AI Note: uses MemoryRouter rather than BrowserRouter (which `@/App` uses in
 * production) so navigation never touches jsdom's URL and tests stay isolated.
 * Consequence: assertions on `window.location` will not see router navigations.
 */
export function renderWithRouter(
  ui: ReactElement,
  {
    route = "/",
    path,
    ...options
  }: { route?: string; path?: string } & Omit<RenderOptions, "wrapper"> = {}
) {
  const user = userEvent.setup();
  const view = render(ui, {
    wrapper: ({ children }) =>
      path ? (
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path={path} element={children} />
          </Routes>
        </MemoryRouter>
      ) : (
        <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
      ),
    ...options,
  });
  return { user, ...view };
}

// ── fetch mocking ────────────────────────────────────────────────────────────

/**
 * Shape for describing a canned response.
 *
 * AI Note: currently unused by `mockFetch`, which takes real `Response` objects.
 * Kept as the declared vocabulary for a future higher-level helper — do not
 * assume passing one of these to `mockFetch` works.
 */
export interface MockResponseInit {
  status?: number;
  json?: unknown;
  text?: string;
  blob?: Blob;
}

/**
 * Build a real `Response` with a JSON body and the JSON content type.
 *
 * Uses the genuine `Response` class (available in jsdom under Node 18+) rather
 * than a hand-rolled object, so `res.ok`, `res.status`, `res.json()`,
 * `res.text()` and `res.blob()` all behave exactly as they do in a browser —
 * important because `@/api/client` branches on `res.ok`, `res.status === 401`
 * and `res.status === 204`.
 *
 * AI Note: a `Response` body can only be consumed once. Reusing the same
 * instance for two mocked calls makes the second one throw "body already read",
 * which is why the array form of `mockFetch` shifts each response off the queue.
 */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Replace global.fetch with a mock. Pass a function for full control, or an
 * array of responses consumed in order.
 *
 * Function form: receives `(url, init)` with `url` already stringified, so tests
 * can route on path (`url.includes("/api/jobs")`) and assert on `init.method` /
 * `init.headers.Authorization`. Use this whenever call ORDER is not guaranteed —
 * e.g. a page firing several fetches concurrently from one effect.
 *
 * Array form: responses are shifted off in call order. Once exhausted, every
 * further call gets `jsonResponse({}, 500)`.
 *
 * @returns the `vi.fn()` itself, so tests can assert on `mock.calls`.
 *
 * AI Note: the exhausted-queue fallback is a 500 rather than a throw, on
 * purpose: an unexpected extra request surfaces as a visible error state in the
 * component instead of an unhandled rejection with no stack pointing at the
 * cause. If a test fails with a mysterious error banner, suspect a missing entry
 * in this array.
 *
 * AI Note: installed via `vi.stubGlobal`, so it is only undone by
 * `vi.unstubAllGlobals()` (or `unstubGlobals: true` in the vitest config) —
 * `vi.restoreAllMocks()` does NOT undo it.
 */
export function mockFetch(
  impl: ((url: string, init?: RequestInit) => Promise<Response>) | Response[]
) {
  const fn = Array.isArray(impl)
    ? vi.fn().mockImplementation(() => Promise.resolve(impl.shift() ?? jsonResponse({}, 500)))
    : vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) =>
        impl(String(input), init)
      );
  vi.stubGlobal("fetch", fn);
  return fn;
}

// ── Fixture factories ────────────────────────────────────────────────────────
//
// Each factory returns a fully-populated, type-valid object and spreads
// `overrides` LAST, so a test only states the fields it actually cares about
// (`makeJob({status: "running"})`). Adding a required field to a type in
// `@/types` means adding a default here, which is the intended coupling — it
// makes the compiler point at every fixture that needs updating.

/**
 * Monotonic counter behind `id()`.
 *
 * AI Note: module-level and never reset, so ids are unique across an entire test
 * FILE but NOT stable between runs of different subsets. Never assert on a
 * literal id string — capture the value from the fixture instead.
 */
let _seq = 0;
/** Generate a distinct, UUID-shaped id (`0000...0001`, `...0002`, ...). Valid-looking without being random, so failures are readable. */
const id = () => `00000000-0000-0000-0000-${String(++_seq).padStart(12, "0")}`;

/** A non-admin, active user. Override `role: "admin"` to exercise admin-only UI. */
export function makeUser(overrides: Partial<UserInfo> = {}): UserInfo {
  return {
    id: id(),
    username: "alice",
    email: "alice@example.com",
    role: "user",
    is_active: true,
    ...overrides,
  };
}

/**
 * A healthy online Linux node.
 *
 * AI Note: `last_heartbeat` / `registered_at` are `new Date().toISOString()`,
 * i.e. evaluated when the factory RUNS. That makes `formatRelativeTime` render
 * "0s ago" — so a test asserting on a specific relative-time string must pass an
 * explicit timestamp override rather than relying on the default.
 */
export function makeNode(overrides: Partial<NodeInfo> = {}): NodeInfo {
  return {
    id: id(),
    hostname: "node-1.test",
    display_name: "Node 1",
    os_type: "linux",
    os_version: "Ubuntu 22.04",
    arch: "x86_64",
    cpu_model: "Test CPU",
    cpu_cores: 8,
    ram_mb: 16384,
    gpu_info: null,
    agent_version: "0.1.0",
    ip_address: "10.0.0.1",
    status: "online",
    tags: [],
    last_heartbeat: new Date().toISOString(),
    registered_at: new Date().toISOString(),
    ...overrides,
  };
}

/** An empty pool (`node_count: 0`). Override `node_count` to test the members badge. */
export function makePool(overrides: Partial<PoolInfo> = {}): PoolInfo {
  return {
    id: id(),
    name: "pool-1",
    description: "Test pool",
    node_count: 0,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

/**
 * A freshly-submitted job: `pending`, step 0, no target, not started.
 *
 * The natural starting point for status-transition tests — override `status`
 * and `current_step` to represent any later point in the lifecycle.
 */
export function makeJob(overrides: Partial<JobInfo> = {}): JobInfo {
  return {
    id: id(),
    name: "test-job",
    submitted_by: id(),
    target_pool_id: null,
    target_node_id: null,
    priority: 1,
    status: "pending",
    current_step: 0,
    error: null,
    created_at: new Date().toISOString(),
    started_at: null,
    completed_at: null,
    ...overrides,
  };
}

/**
 * A not-yet-dispatched step run (`pending`, `node_id: null`, no timestamps).
 *
 * AI Note: `job_id` defaults to a FRESH id, so it does not belong to any
 * `makeJob()` fixture. Tests that render a JobDetail must pass
 * `makeStepRun({job_id: job.id})` explicitly.
 */
export function makeStepRun(overrides: Partial<StepRunInfo> = {}): StepRunInfo {
  return {
    id: id(),
    job_id: id(),
    step_index: 0,
    step_name: "run_command",
    status: "pending",
    node_id: null,
    input_params: null,
    output_params: null,
    error: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

/**
 * A minimal step-type schema: one required string field with a `required` rule,
 * runnable on all three OSes, publishing `exit_code`.
 *
 * This is what drives Job Builder tests — the palette entry, the generated form
 * control for `command`, and required-field validation all come from this shape.
 */
export function makeStepSchema(overrides: Partial<StepSchemaInfo> = {}): StepSchemaInfo {
  return {
    name: "run_command",
    description: "Run a shell command",
    requires_node: true,
    supported_os: ["macos", "linux", "windows"],
    output_keys: ["exit_code"],
    fields: [
      { name: "command", required: true, description: "Command", default: null, examples: ["echo hi"], field_type: "string" },
    ],
    rules: [{ rule_type: "required", fields: ["command"] }],
    os_variants: {},
    ...overrides,
  };
}

/**
 * A default, active MinIO backend with 1 TiB capacity and nothing used.
 *
 * AI Note: `1024 ** 4` (1 TiB) is chosen so `formatBytes` renders a clean
 * "1 TB" — it sits exactly on the last entry of that helper's unit table.
 */
export function makeBackend(overrides: Partial<StorageBackendInfo> = {}): StorageBackendInfo {
  return {
    id: id(),
    name: "minio-1",
    backend_type: "minio",
    config: {},
    credential_id: id(),
    capacity_bytes: 1024 ** 4,
    used_bytes: 0,
    is_default: true,
    is_active: true,
    priority: 10,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

/** A private (non-shared) S3 credential. Contains no secret material, mirroring the real API response. */
export function makeCredential(overrides: Partial<CredentialInfo> = {}): CredentialInfo {
  return {
    id: id(),
    name: "cred-1",
    credential_type: "s3",
    description: null,
    is_shared: false,
    owner_id: id(),
    created_at: new Date().toISOString(),
    updated_at: null,
    ...overrides,
  };
}

// AI Note: re-exporting all of RTL (plus userEvent) means test files import
// `screen`, `waitFor`, `render`, `within`, ... from this module. Because this
// re-export comes AFTER the local declarations, `renderWithRouter` and RTL's own
// `render` coexist — do not add a local `render` here or it would shadow RTL's.
export * from "@testing-library/react";
export { userEvent };
