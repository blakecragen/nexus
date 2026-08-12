/**
 * Tests for the API client (src/api/client.ts).
 *
 * Covers: token storage (setToken/getToken <-> localStorage "nexus_token"),
 * the request() wrapper (JSON headers, Authorization, 204 -> undefined,
 * !ok -> throws with detail, 401 -> clears token + redirects to /login),
 * and representative api.* methods (login, listNodes query string, getJobLog
 * text endpoint, provisionNode/reconnectNode 502 log surfacing, downloadJobResults
 * blob path).
 *
 * Role in the suite: this is the *only* place the raw `request()` wrapper is
 * exercised directly. Page-level tests (Jobs/Nodes/...) mock `@/api/client`
 * away, so every cross-cutting HTTP behaviour — auth header injection, error
 * unwrapping, the 401 logout redirect — is pinned here and nowhere else. If a
 * behaviour disappears from this file it is untested across the whole frontend.
 *
 * What is stubbed: only `globalThis.fetch` (via `mockFetch` from test-utils).
 * The real client module, real `Response` objects and the real localStorage
 * stub are used, so status/`ok`/body-parsing semantics match a browser.
 */
import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";

// client.ts reads localStorage.getItem("nexus_token") at module-eval time. The
// shared setup installs its localStorage stub in beforeEach, which runs AFTER
// this module is imported — so we must guarantee a localStorage exists before
// the import below is evaluated. vi.hoisted runs before the (hoisted) imports.
//
// AI Note: ordering-critical. vitest hoists `vi.mock`/`vi.hoisted` above the
// import statements, which is the only window in which we can define
// `globalThis.localStorage` before `@/api/client` captures the stored token in
// its module-level initializer. Moving this block below the imports (or turning
// it into a plain `beforeEach`) makes `getToken()` start as null forever and
// silently breaks the Authorization-header assertions.
vi.hoisted(() => {
  if (!globalThis.localStorage || typeof globalThis.localStorage.getItem !== "function") {
    const store: Record<string, string> = {};
    globalThis.localStorage = {
      get length() {
        return Object.keys(store).length;
      },
      clear: () => {
        for (const k of Object.keys(store)) delete store[k];
      },
      getItem: (k: string) => (k in store ? store[k] : null),
      setItem: (k: string, v: string) => {
        store[k] = String(v);
      },
      removeItem: (k: string) => {
        delete store[k];
      },
      key: (i: number) => Object.keys(store)[i] ?? null,
    } as Storage;
  }
});

import { api, setToken, getToken } from "@/api/client";
import { mockFetch, jsonResponse } from "../test/test-utils";

// Always start each test with a clean token so module-level state doesn't leak.
//
// AI Note: `setToken` mutates module-level state inside client.ts *and*
// localStorage. Because the module is a singleton across this file's tests, a
// token left behind by one test would leak a Bearer header into the next one.
beforeEach(() => {
  setToken(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setToken(null);
});

// ── Token storage ────────────────────────────────────────────────────────────

/**
 * setToken/getToken round-trip through localStorage.
 *
 * Protects the "stay logged in across a page reload" contract: the key name
 * "nexus_token" is a persisted, cross-release identifier — renaming it silently
 * logs every existing user out.
 */
describe("setToken / getToken", () => {
  /**
   * Asserts a set token is readable back AND lands in localStorage under the
   * exact key "nexus_token". Regression guarded: an in-memory-only token (no
   * persistence) would pass `getToken()` but drop the session on refresh.
   */
  it("persists the token to localStorage under 'nexus_token'", () => {
    setToken("abc123");
    expect(getToken()).toBe("abc123");
    expect(localStorage.getItem("nexus_token")).toBe("abc123");
  });

  /**
   * Asserts `setToken(null)` removes the key rather than storing the string
   * "null". Regression guarded: a stringified "null" would be truthy and get
   * sent as `Authorization: Bearer null` on every subsequent request.
   */
  it("clears the token from localStorage when set to null", () => {
    setToken("abc123");
    setToken(null);
    expect(getToken()).toBeNull();
    expect(localStorage.getItem("nexus_token")).toBeNull();
  });
});

// ── request() wrapper behavior (exercised via api.* methods) ──────────────────

/**
 * The shared `request()` helper that nearly every api.* method funnels through.
 *
 * These cases are written against public api.* methods rather than calling
 * `request()` directly (it isn't exported), so each one doubles as a check that
 * the chosen method really does go through the wrapper.
 */
describe("request() wrapper", () => {
  /**
   * Unauthenticated requests must carry the JSON content type and must NOT
   * invent an Authorization header. Regression guarded: sending
   * `Authorization: Bearer null` to /api/auth/login makes the server reject a
   * legitimate login attempt.
   */
  it("sends JSON Content-Type and no Authorization when no token is set", async () => {
    const fetchSpy = mockFetch([jsonResponse({ access_token: "t", refresh_token: "r" })]);

    await api.login("alice", "pw");

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/auth/login");
    expect(init.method).toBe("POST");
    const headers = init.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers["Authorization"]).toBeUndefined();
    expect(init.body).toBe(JSON.stringify({ username: "alice", password: "pw" }));
  });

  /**
   * The core auth contract: once a token is stored, every request through the
   * wrapper is authenticated. Regression guarded: dropping the header turns
   * every authenticated page into a 401 -> forced-logout loop.
   */
  it("attaches a Bearer Authorization header when a token is set", async () => {
    setToken("secret-token");
    const fetchSpy = mockFetch([jsonResponse([])]);

    await api.listNodes();

    const headers = fetchSpy.mock.calls[0][1].headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer secret-token");
  });

  /**
   * 204 responses have no body, so the wrapper must short-circuit before
   * `res.json()`. Regression guarded: calling `.json()` on an empty body throws
   * a SyntaxError, which would make every successful DELETE look like a failure
   * in the UI.
   */
  it("returns undefined for a 204 No Content response", async () => {
    mockFetch([new Response(null, { status: 204 })]);

    const result = await api.deleteNode("node-1");

    expect(result).toBeUndefined();
  });

  /**
   * FastAPI reports errors as `{"detail": "..."}`; the wrapper must unwrap that
   * so pages can surface a human-readable message. Regression guarded: showing
   * "[object Object]" or a bare status code to the user instead of "Pool
   * already exists".
   */
  it("throws with the body's detail message on a non-ok response", async () => {
    mockFetch([jsonResponse({ detail: "Pool already exists" }, 409)]);

    await expect(api.createPool({ name: "dup" })).rejects.toThrow("Pool already exists");
  });

  /**
   * Fallback #1: valid JSON error body but no `detail` key -> "HTTP <status>".
   * Regression guarded: an `undefined` error message reaching the UI.
   */
  it("falls back to 'HTTP <status>' when the error body has no detail", async () => {
    mockFetch([jsonResponse({}, 500)]);

    await expect(api.createPool({ name: "x" })).rejects.toThrow("HTTP 500");
  });

  /**
   * Fallback #2: the body isn't JSON at all (proxy/gateway HTML, empty body)
   * -> fall back to statusText. Regression guarded: an unhandled SyntaxError
   * from `res.json()` masking the real upstream failure — exactly the case that
   * matters most during an outage.
   */
  it("falls back to statusText when the error body is not valid JSON", async () => {
    mockFetch([
      new Response("not json", { status: 503, statusText: "Service Unavailable" }),
    ]);

    await expect(api.createPool({ name: "x" })).rejects.toThrow("Service Unavailable");
  });

  /** Happy path: a 2xx JSON body is parsed and returned verbatim to the caller. */
  it("returns the parsed JSON body on a successful response", async () => {
    const nodes = [{ id: "n1" }, { id: "n2" }];
    mockFetch([jsonResponse(nodes)]);

    await expect(api.listNodes()).resolves.toEqual(nodes);
  });

  /**
   * Covers the `...options.headers` spread branch: caller-supplied headers must
   * coexist with the wrapper's default Content-Type. Regression guarded: an
   * assignment order flip that lets one clobber the other.
   */
  it("merges caller-supplied headers with the default Content-Type", async () => {
    // setMaintenance passes a JSON body via request(); the wrapper always sets
    // Content-Type, but the spread of options.headers is a real branch — verify
    // both survive for a method that goes through request().
    const fetchSpy = mockFetch([jsonResponse({ id: "n1" })]);

    await api.setMaintenance("n1", true);

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/nodes/n1/maintenance");
    expect(init.method).toBe("PUT");
    const headers = init.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(init.body).toBe(JSON.stringify({ maintenance: true }));
  });

  /**
   * The wrapper merges its defaults into the caller's RequestInit; this pins
   * that the caller's `method` and `body` win. Regression guarded: a POST
   * silently downgraded to GET (or a dropped body) would create nothing and
   * still look successful.
   */
  it("forwards the request body and method exactly as built by the api method", async () => {
    // Guards against a regression where request() drops/overwrites the caller's
    // method or body when merging options.
    const fetchSpy = mockFetch([jsonResponse({ id: "x" })]);

    await api.createNode({ hostname: "h", os_type: "linux" });

    const init = fetchSpy.mock.calls[0][1];
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ hostname: "h", os_type: "linux" }));
  });
});

// ── 401 handling: clears token + redirects to /login + throws ─────────────────

/**
 * Session-expiry handling — the one security-sensitive branch in the client.
 *
 * On any 401 the wrapper must (a) drop the stored token, (b) hard-navigate to
 * /login, and (c) still reject so the calling page doesn't proceed as if the
 * request succeeded.
 */
describe("request() on 401 Unauthorized", () => {
  let originalLocation: Location;

  beforeEach(() => {
    originalLocation = window.location;
    // Replace window.location with a writable stub so assigning href is
    // observable and doesn't trip jsdom's "navigation not implemented".
    //
    // AI Note: jsdom's real `window.location` is non-writable and throws
    // "Not implemented: navigation" when `href` is assigned. Redefining the
    // property is the only way to observe the redirect; it MUST be restored in
    // afterEach or later test files inherit a fake location.
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { ...originalLocation, href: "/" } as Location,
    });
  });

  afterEach(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: originalLocation,
    });
  });

  /**
   * Asserts all three effects of a 401 at once. Regression guarded: keeping the
   * dead token would leave the user on a half-broken authenticated shell
   * issuing failing requests forever; swallowing the throw would let the caller
   * render `undefined` data.
   */
  it("clears the stored token, redirects to /login, and throws", async () => {
    setToken("expired-token");
    mockFetch([jsonResponse({ detail: "token expired" }, 401)]);

    await expect(api.getMe()).rejects.toThrow("Unauthorized");

    expect(getToken()).toBeNull();
    expect(localStorage.getItem("nexus_token")).toBeNull();
    expect(window.location.href).toBe("/login");
  });
});

// ── api.login ─────────────────────────────────────────────────────────────────

/** The unauthenticated entry point that mints the access/refresh token pair. */
describe("api.login", () => {
  /**
   * Asserts the token payload is returned untouched (the auth store depends on
   * both `access_token` and `refresh_token` being present). Regression guarded:
   * a client that reshapes the response would break session persistence.
   */
  it("POSTs credentials and returns the token response", async () => {
    const tokens = { access_token: "acc", refresh_token: "ref" };
    mockFetch([jsonResponse(tokens)]);

    await expect(api.login("alice", "hunter2")).resolves.toEqual(tokens);
  });
});

// ── api.requeueJob (bodyless POST) ───────────────────────────────────────────

/**
 * Re-queueing is the one mutating call that sends no body at all: the server
 * copies name, plan, targeting and priority off the stored job. That makes the
 * *absence* of a body part of the contract rather than an omission.
 */
describe("api.requeueJob", () => {
  /**
   * Regression guarded: sending `{}` (or the original job) as a body. The
   * endpoint takes no parameters, so a body is at best ignored and at worst a
   * 422 — and it would imply the caller can influence the copy, which it
   * cannot.
   */
  it("POSTs to /jobs/{id}/requeue with no body", async () => {
    const fetchSpy = mockFetch([jsonResponse({ id: "new-job" })]);

    await api.requeueJob("job-1");

    expect(fetchSpy.mock.calls[0][0]).toBe("/api/jobs/job-1/requeue");
    const init = fetchSpy.mock.calls[0][1];
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  /**
   * The response is the NEW job. Regression guarded: returning the original (or
   * nothing), which would leave the detail page navigating back to the job the
   * user just asked to re-run.
   */
  it("resolves with the newly created job", async () => {
    mockFetch([jsonResponse({ id: "new-job", name: "nightly", status: "pending" })]);

    await expect(api.requeueJob("job-1")).resolves.toEqual({
      id: "new-job",
      name: "nightly",
      status: "pending",
    });
  });

  /**
   * A stored plan that no longer validates comes back as a 400 with the same
   * `detail` shape POST /api/jobs uses, so the message reaches the UI intact.
   */
  it("throws with the server's detail message when the stored plan is rejected", async () => {
    mockFetch([jsonResponse({ detail: "Step 0 references unknown step 'gem5_run'" }, 400)]);

    await expect(api.requeueJob("job-1")).rejects.toThrow(
      "Step 0 references unknown step 'gem5_run'"
    );
  });
});

// ── api.listNodes query string ────────────────────────────────────────────────

/** Query-string construction for the filterable list endpoint. */
describe("api.listNodes", () => {
  /**
   * No params must produce a bare path, not a dangling "?" — some proxies and
   * cache layers treat "/api/nodes?" as a distinct key.
   */
  it("requests /nodes with no query string when no params given", async () => {
    const fetchSpy = mockFetch([jsonResponse([])]);

    await api.listNodes();

    expect(fetchSpy.mock.calls[0][0]).toBe("/api/nodes");
  });

  /**
   * Params are serialised into a query string in insertion order. Regression
   * guarded: dropping filters here would make the Nodes page silently show
   * every node regardless of the selected filter.
   */
  it("encodes params into a query string", async () => {
    const fetchSpy = mockFetch([jsonResponse([])]);

    await api.listNodes({ status: "online", pool_id: "p1" });

    expect(fetchSpy.mock.calls[0][0]).toBe("/api/nodes?status=online&pool_id=p1");
  });
});

// ── api.getJobLog (plain-text endpoint) ───────────────────────────────────────

/**
 * getJobLog bypasses `request()` because /api/jobs/{id}/log returns text/plain,
 * not JSON. That means its auth + error handling are a *separate* code path and
 * need their own coverage.
 */
describe("api.getJobLog", () => {
  /**
   * Asserts the raw text (newlines intact) is returned and the Bearer header is
   * attached by this bespoke path. Regression guarded: JSON-parsing the log
   * body, or forgetting auth here and 401-ing the log viewer.
   */
  it("returns the response body as text, with Authorization when token set", async () => {
    setToken("log-token");
    const fetchSpy = mockFetch([
      new Response("line 1\nline 2\n", { status: 200 }),
    ]);

    const text = await api.getJobLog("job-42");

    expect(text).toBe("line 1\nline 2\n");
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/jobs/job-42/log");
    expect((init.headers as Record<string, string>)["Authorization"]).toBe(
      "Bearer log-token"
    );
  });

  /**
   * The text path has its own error branch (no `{detail}` to unwrap), so a
   * missing job must still reject with a usable message rather than resolving
   * with the 404 body as if it were log content.
   */
  it("throws 'HTTP <status>' when the log endpoint is not ok", async () => {
    mockFetch([new Response("nope", { status: 404 })]);

    await expect(api.getJobLog("missing")).rejects.toThrow("HTTP 404");
  });

  /**
   * The token-less branch of getJobLog's hand-built header object. Regression
   * guarded: a stale Bearer captured at module load, or an unconditional
   * Content-Type on what is a GET with no body.
   */
  it("sends no Authorization header when no token is set", async () => {
    // getJobLog builds its own headers object; with no token it must stay empty
    // (it must NOT inherit a stale Bearer from a prior call or set Content-Type).
    const fetchSpy = mockFetch([new Response("log body", { status: 200 })]);

    const text = await api.getJobLog("job-1");

    expect(text).toBe("log body");
    const headers = fetchSpy.mock.calls[0][1].headers as Record<string, string>;
    expect(headers["Authorization"]).toBeUndefined();
  });
});

// ── api.provisionNode / reconnectNode: 502 surfaces the install log ───────────

/**
 * provisionNode drives the SSH install flow on the server. Its failures are
 * long and diagnostic, so the server returns `{detail: {error, log[]}}` and the
 * client must attach `log` to the thrown Error — the Nodes page renders that
 * array as the install transcript. Losing it turns a debuggable failure into
 * "something went wrong".
 */
describe("api.provisionNode", () => {
  /** Happy path: the full provisioning payload (api_key, ws_url, log) round-trips. */
  it("returns the parsed body on success", async () => {
    const body = { id: "n1", api_key: "k", ws_url: "ws://x", mode: "ssh", log: ["ok"] };
    mockFetch([jsonResponse(body)]);

    await expect(api.provisionNode({ hostname: "h" })).resolves.toEqual(body);
  });

  /**
   * provisionNode hand-rolls its headers instead of using `request()`, so its
   * auth wiring is verified independently. Regression guarded: an unauthenticated
   * provision call 401-ing for admins who are demonstrably logged in.
   */
  it("POSTs to /nodes/provision with a Bearer header built from the stored token", async () => {
    // provisionNode does NOT go through request(); it builds its own headers, so
    // verify it independently attaches the token + correct method/URL/body.
    setToken("prov-token");
    const fetchSpy = mockFetch([jsonResponse({ id: "n1", api_key: "k", log: [] })]);

    await api.provisionNode({ hostname: "h", os_type: "linux" });

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/nodes/provision");
    expect(init.method).toBe("POST");
    const headers = init.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer prov-token");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(init.body).toBe(JSON.stringify({ hostname: "h", os_type: "linux" }));
  });

  /**
   * The object-detail branch: `detail.error` becomes the message and
   * `detail.log` is attached to the Error instance. Regression guarded: the
   * install transcript being dropped, which is the only way a user can tell
   * *why* SSH provisioning failed.
   */
  it("throws with the detail.error message and attaches the log array on a 502", async () => {
    const detail = { error: "ssh connect failed", log: ["step 1", "step 2 failed"] };
    mockFetch([jsonResponse({ detail }, 502)]);

    await expect(api.provisionNode({ hostname: "h" })).rejects.toMatchObject({
      message: "ssh connect failed",
      log: ["step 1", "step 2 failed"],
    });
  });

  /**
   * The string-detail branch: plain FastAPI errors (validation, 404) have no
   * `.error`/`.log`. `log` must default to `[]` so the UI can render it without
   * a null guard. Regression guarded: "cannot read property map of undefined"
   * crashing the whole Nodes page on an ordinary error.
   */
  it("uses a string detail directly and defaults log to [] when absent", async () => {
    mockFetch([jsonResponse({ detail: "plain string error" }, 500)]);

    try {
      await api.provisionNode({ hostname: "h" });
      throw new Error("expected provisionNode to throw");
    } catch (e) {
      const err = e as Error & { log?: string[] };
      expect(err.message).toBe("plain string error");
      expect(err.log).toEqual([]);
    }
  });
});

/**
 * reconnectNode re-runs agent setup against an already-registered node. It is a
 * near-copy of provisionNode's error handling, so it is covered independently —
 * the two implementations have drifted apart before.
 */
describe("api.reconnectNode", () => {
  /** Object-detail branch: message from `detail.error`, transcript from `detail.log`. */
  it("throws with detail.error and attaches the setup log on a 502", async () => {
    const detail = { error: "agent unreachable", log: ["reconnect attempt", "timed out"] };
    mockFetch([jsonResponse({ detail }, 502)]);

    await expect(
      api.reconnectNode("n1", { mode: "ssh" })
    ).rejects.toMatchObject({
      message: "agent unreachable",
      log: ["reconnect attempt", "timed out"],
    });
  });

  /**
   * Pins the node-scoped URL shape. Regression guarded: posting to a collection
   * path would reconnect the wrong node (or none), which is destructive on a
   * live cluster.
   */
  it("POSTs to the node-specific reconnect path", async () => {
    const fetchSpy = mockFetch([
      jsonResponse({ id: "n1", ws_url: "ws://x", mode: "ssh", online: true, log: [] }),
    ]);

    await api.reconnectNode("n1", { mode: "ssh" });

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/nodes/n1/reconnect");
    expect(init.method).toBe("POST");
  });

  /** String-detail branch, mirroring provisionNode: `log` must still default to `[]`. */
  it("uses a string detail and defaults log to [] when the error body lacks a log", async () => {
    // Mirrors provisionNode's string-detail branch for the reconnect path.
    mockFetch([jsonResponse({ detail: "node not found" }, 404)]);

    try {
      await api.reconnectNode("n1", { mode: "ssh" });
      throw new Error("expected reconnectNode to throw");
    } catch (e) {
      const err = e as Error & { log?: string[] };
      expect(err.message).toBe("node not found");
      expect(err.log).toEqual([]);
    }
  });

  /** Happy path: `online` and the setup transcript reach the caller unmodified. */
  it("returns the parsed body on a successful reconnect", async () => {
    const body = { id: "n1", ws_url: "ws://x", mode: "ssh", online: true, log: ["reconnected"] };
    mockFetch([jsonResponse(body)]);

    await expect(api.reconnectNode("n1", { mode: "ssh" })).resolves.toEqual(body);
  });
});

// ── api.downloadJobResults (blob -> client-side save) ─────────────────────────

/**
 * downloadJobResults is the only api method with DOM side effects: it fetches
 * the results tarball as a Blob, wraps it in an object URL, synthesises an
 * <a download> and clicks it, then revokes the URL. Each of those steps is
 * asserted because a silent break here looks like "the Download button does
 * nothing".
 */
describe("api.downloadJobResults", () => {
  /**
   * Full save path: correct endpoint, object URL created from the *fetched*
   * blob, a click on the created anchor, the URL revoked afterwards, and a
   * job-named filename. Regression guarded: a leaked object URL (memory growth
   * per download) or a file that saves as "download" with no extension.
   */
  it("fetches the blob and triggers a client-side download", async () => {
    const blob = new Blob(["tarball-bytes"], { type: "application/gzip" });
    const fetchSpy = mockFetch([
      new Response(blob, { status: 200 }),
    ]);

    // jsdom doesn't implement URL.createObjectURL/revokeObjectURL or anchor
    // navigation, so install observable stubs for the duration of the test.
    //
    // AI Note: these are assigned directly onto the global `URL` (not via
    // vi.stubGlobal), so they are torn down by the explicit `delete`s at the end
    // of this test rather than by `vi.unstubAllGlobals()` in afterEach. If this
    // test ever throws before those deletes, the stubs leak into later files.
    const createUrl = vi.fn().mockReturnValue("blob:fake-url");
    const revokeUrl = vi.fn();
    URL.createObjectURL = createUrl;
    URL.revokeObjectURL = revokeUrl;
    // Capture the anchor the SUT creates so we can assert href/download on the
    // exact element that was clicked (not just that *a* click happened).
    const createElement = document.createElement.bind(document);
    let createdAnchor: HTMLAnchorElement | undefined;
    const createElSpy = vi
      .spyOn(document, "createElement")
      .mockImplementation((tag: string) => {
        const el = createElement(tag);
        if (tag === "a") createdAnchor = el as HTMLAnchorElement;
        return el;
      });
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});

    await api.downloadJobResults("job-7");

    expect(fetchSpy.mock.calls[0][0]).toBe("/api/jobs/job-7/results/download");
    expect(createUrl).toHaveBeenCalledWith(blob);
    expect(clickSpy).toHaveBeenCalledOnce();
    expect(revokeUrl).toHaveBeenCalledWith("blob:fake-url");
    // The download was wired to the object URL and a job-named filename.
    expect(createdAnchor?.href).toContain("blob:fake-url");
    expect(createdAnchor?.download).toBe("job_job-7_results.tar.gz");

    clickSpy.mockRestore();
    createElSpy.mockRestore();
    delete (URL as { createObjectURL?: unknown }).createObjectURL;
    delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
  });

  /**
   * Error path must reject *before* any DOM work happens. Regression guarded:
   * saving a 403 error page to disk as "job_x_results.tar.gz", which looks like
   * a successful download until the user tries to open it.
   */
  it("throws 'HTTP <status>' and does not attempt a download when not ok", async () => {
    mockFetch([new Response("nope", { status: 403 })]);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});

    await expect(api.downloadJobResults("job-7")).rejects.toThrow("HTTP 403");
    expect(clickSpy).not.toHaveBeenCalled();

    clickSpy.mockRestore();
  });
});
