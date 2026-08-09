/**
 * Tests for the useWebSocket hook (src/hooks/useWebSocket.ts).
 *
 * The hook opens a WebSocket to /ws/dashboard, appends ?token= when a token is
 * present, parses incoming JSON into the onMessage handler, swallows malformed
 * payloads, reconnects with exponential backoff on close, and tears everything
 * down on unmount.
 *
 * We replace the global WebSocket with a fake that records the constructed URL
 * and exposes hooks to fire onopen/onmessage/onclose/onerror by hand. Timers are
 * faked so the reconnect schedule is deterministic.
 *
 * Role in the system: this hook is the frontend half of the live-update path.
 * The server side is packages/server/src/nexus_server/api/routes/ws.py
 * (`dashboard_websocket`), which broadcasts node.status / job.status / step.log
 * frames; the parsed objects are handed to `handleWsMessage` in src/stores to
 * mutate the zustand stores. A silent break here means the dashboard stops
 * updating without any visible error — hence the heavy coverage of the
 * reconnect/backoff schedule.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

// client.ts (imported transitively via useWebSocket → getToken) reads
// localStorage.getItem("nexus_token") at module-eval time. The shared setup
// installs its localStorage stub in beforeEach, which runs AFTER this module is
// imported — so guarantee a localStorage exists before the imports below are
// evaluated. vi.hoisted runs before the (hoisted) imports.
//
// AI Note: ordering-critical, same reason as in api/client.test.ts. This block
// must stay above the imports; vitest hoists it, a plain beforeEach would run
// too late and `@/api/client` would throw (or capture a null token) at import.
vi.hoisted(() => {
  if (
    !globalThis.localStorage ||
    typeof globalThis.localStorage.getItem !== "function"
  ) {
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

import { useWebSocket } from "./useWebSocket";
import { setToken } from "@/api/client";

// ── Fake WebSocket ────────────────────────────────────────────────────────────
// Records every instance + the URL it was opened with, and lets tests fire the
// lifecycle callbacks the SUT assigns.

/**
 * Stand-in for the browser `WebSocket`, installed via `vi.stubGlobal`.
 *
 * It performs no I/O. Construction merely records the URL and pushes the
 * instance onto the static `instances` array, which is how every test counts
 * reconnects (`instances.length`) and inspects the URL the hook built.
 * `close` is a `vi.fn()` so teardown can be asserted.
 *
 * The `fireX` helpers invoke whichever callback the hook assigned to
 * `onopen`/`onmessage`/`onclose`/`onerror`, letting a test drive the socket
 * lifecycle synchronously and deterministically.
 *
 * AI Note: `instances` is static and therefore shared across tests — the
 * `beforeEach` below resets it to `[]`. Forgetting that reset makes reconnect
 * counts accumulate across tests and produces confusing off-by-N failures.
 * AI Note: this fake deliberately does NOT auto-fire `onopen`. A real socket
 * opens asynchronously, and several tests depend on the backoff *not* having
 * been reset yet.
 */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();

  /** Records the dialled URL and registers this instance for the assertions. */
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  // helpers for the tests to drive the socket
  /** Simulate a successful handshake (the hook resets its backoff here). */
  fireOpen() {
    this.onopen?.();
  }
  /** Deliver a raw frame body; the hook is responsible for JSON parsing it. */
  fireMessage(data: string) {
    this.onmessage?.({ data });
  }
  /** Simulate the socket closing, which is what schedules a reconnect. */
  fireClose() {
    this.onclose?.();
  }
  /** Simulate a transport error; the hook responds by closing the socket. */
  fireError() {
    this.onerror?.();
  }
}

/**
 * The most recently constructed fake socket — i.e. the one the hook is
 * currently using. After a reconnect the hook drops the old instance, so tests
 * must always drive `last()` rather than a captured reference.
 */
function last() {
  return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  // Ensure a known host/protocol for URL assertions. jsdom default is
  // http://localhost:3000 (or similar); we pin it for determinism.
  vi.stubGlobal("location", {
    protocol: "http:",
    host: "nexus.test:8080",
    href: "http://nexus.test:8080/",
  });
  setToken(null);
});

afterEach(() => {
  // AI Note: useRealTimers() must run even for tests that never enabled fake
  // timers — the reconnect describe block turns them on in its own beforeEach,
  // and leaving them installed would freeze timers for every later test file.
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setToken(null);
});

/**
 * URL construction: scheme derived from the page protocol, host copied from the
 * page, and the JWT passed as a query param (browsers cannot set headers on a
 * WebSocket handshake, which is why the token rides in the URL).
 */
describe("useWebSocket – connection URL", () => {
  /**
   * Baseline: exactly one socket, dialled at the dashboard endpoint, with no
   * query string when logged out. Regression guarded: a duplicate connection
   * per mount (double the server load and duplicated store updates).
   */
  it("connects to the ws dashboard endpoint without a token when none is set", () => {
    renderHook(() => useWebSocket(vi.fn()));

    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(last().url).toBe("ws://nexus.test:8080/ws/dashboard");
  });

  /**
   * The authenticated case. The server's `dashboard_websocket` rejects sockets
   * without a valid `?token=`, so dropping this would make every dashboard
   * connection close immediately and spin in the reconnect loop.
   */
  it("appends ?token= when a token exists in the api client", () => {
    setToken("secret-jwt-123");

    renderHook(() => useWebSocket(vi.fn()));

    expect(last().url).toBe(
      "ws://nexus.test:8080/ws/dashboard?token=secret-jwt-123"
    );
  });

  /**
   * Scheme must follow the page protocol. Regression guarded: a hard-coded
   * `ws://` on an https deployment is blocked by the browser as mixed content,
   * so live updates would fail in production while working locally.
   */
  it("uses the wss:// scheme when the page is served over https", () => {
    vi.stubGlobal("location", {
      protocol: "https:",
      host: "nexus.test",
      href: "https://nexus.test/",
    });

    renderHook(() => useWebSocket(vi.fn()));

    expect(last().url).toBe("wss://nexus.test/ws/dashboard");
  });

  /**
   * Falsy-token branch. An empty string must be treated as "no token", not as
   * a token. Regression guarded: dialling `?token=` (empty value) which the
   * server rejects differently from an absent param.
   */
  it("omits ?token= for an empty-string token (falsy branch)", () => {
    // The hook uses `token ? \`?token=...\` : ""`, so an empty string must NOT
    // produce a dangling "?token=" query.
    setToken("");

    renderHook(() => useWebSocket(vi.fn()));

    expect(last().url).toBe("ws://nexus.test:8080/ws/dashboard");
  });

  /**
   * The hook's return value is the live socket ref, which callers use to
   * `send()` on the open connection. Regression guarded: returning a stale or
   * empty ref would break any consumer that publishes upstream.
   */
  it("returns a ref pointing at the live socket instance", () => {
    const { result } = renderHook(() => useWebSocket(vi.fn()));

    // The hook returns wsRef; it should reference the socket it just created.
    expect(result.current.current).toBe(last());
  });
});

/**
 * Inbound frame handling: JSON parse, forward to the consumer, and never let a
 * bad frame escape as an exception (an unhandled throw inside `onmessage` would
 * be swallowed by the browser but can kill the handler chain in tests/consumers).
 */
describe("useWebSocket – incoming messages", () => {
  /** The happy path: a JSON frame arrives as a parsed object, exactly once. */
  it("parses JSON payloads and forwards the object to the handler", () => {
    const handler = vi.fn();
    renderHook(() => useWebSocket(handler));

    const payload = { type: "job_update", data: { id: "abc", status: "running" } };
    last().fireMessage(JSON.stringify(payload));

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith(payload);
  });

  /**
   * Malformed frames must be swallowed silently. Regression guarded: a single
   * truncated/garbage frame throwing out of `onmessage` and taking down the
   * live-update pipeline for the rest of the session.
   */
  it("swallows malformed JSON without throwing or calling the handler", () => {
    const handler = vi.fn();
    renderHook(() => useWebSocket(handler));

    expect(() => last().fireMessage("{ not valid json ")).not.toThrow();
    expect(handler).not.toHaveBeenCalled();
  });

  /**
   * Resilience follow-up: after a bad frame the socket must still deliver good
   * ones. Regression guarded: an error path that nulls out `onmessage` or sets
   * a "broken" flag, which would look like the dashboard freezing mid-session.
   */
  it("forwards subsequent valid messages after a malformed one", () => {
    // A bad frame must not poison the socket for later good frames.
    const handler = vi.fn();
    renderHook(() => useWebSocket(handler));

    last().fireMessage("<<garbage>>");
    expect(handler).not.toHaveBeenCalled();

    const good = { type: "node_update", data: { id: "n1" } };
    last().fireMessage(JSON.stringify(good));
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith(good);
  });
});

/**
 * Reconnect schedule — the highest-risk logic in the hook.
 *
 * Contract: on close, schedule a reconnect after the current delay, then double
 * the delay, capped at 30 000 ms; a successful open resets it to 1 000 ms.
 * Getting this wrong produces either a reconnect storm against the server or a
 * dashboard that never recovers from a blip.
 *
 * AI Note: every test here uses fake timers, and the `advanceTimersByTime(d-1)`
 * / `advanceTimersByTime(1)` pairs are intentional — they prove the reconnect
 * fires at the boundary rather than merely "eventually", which is what actually
 * distinguishes the backoff steps from one another.
 */
describe("useWebSocket – reconnect with backoff", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  /** First close reconnects after exactly 1000 ms — not sooner, not never. */
  it("schedules a reconnect on close and creates a new socket after the delay", () => {
    renderHook(() => useWebSocket(vi.fn()));
    expect(FakeWebSocket.instances).toHaveLength(1);

    // Closing schedules a reconnect 1000ms out; nothing reconnects before then.
    last().fireClose();
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  /**
   * Three consecutive failures must back off 1000 -> 2000 -> 4000 ms.
   * Regression guarded: a fixed delay, which under a server outage turns N
   * dashboards into a sustained 1 Hz connection flood.
   */
  it("doubles the backoff delay on each successive close (exponential)", () => {
    renderHook(() => useWebSocket(vi.fn()));

    // 1st close → reconnect after 1000ms (delay then doubles to 2000)
    last().fireClose();
    vi.advanceTimersByTime(1000);
    expect(FakeWebSocket.instances).toHaveLength(2);

    // 2nd close → next reconnect should require 2000ms, not 1000ms.
    last().fireClose();
    vi.advanceTimersByTime(1999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);

    // 3rd close → next reconnect should require 4000ms.
    last().fireClose();
    vi.advanceTimersByTime(3999);
    expect(FakeWebSocket.instances).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(4);
  });

  /**
   * A successful handshake must reset the delay. Regression guarded: after a
   * long outage the delay would stay pinned at 30 s, so a later brief blip
   * would leave the dashboard stale for half a minute.
   */
  it("resets the backoff to 1000ms after a successful open", () => {
    renderHook(() => useWebSocket(vi.fn()));

    // Grow the backoff to 2000ms.
    last().fireClose();
    vi.advanceTimersByTime(1000); // reconnect #2; delay now 2000
    last().fireClose();
    vi.advanceTimersByTime(2000); // reconnect #3; delay now 4000
    expect(FakeWebSocket.instances).toHaveLength(3);

    // A successful open resets the delay back to 1000ms.
    last().fireOpen();
    last().fireClose();
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(4);
  });

  /**
   * The 30 s ceiling. The loop walks the full doubling ladder, then does one
   * extra close to prove the delay stopped growing. Regression guarded: an
   * uncapped doubling that after ~10 failures would wait hours before retrying,
   * i.e. a dashboard that never comes back.
   */
  it("caps the backoff delay at 30000ms after repeated closes", () => {
    renderHook(() => useWebSocket(vi.fn()));

    // Drive the delay through 1000→2000→4000→8000→16000→30000 (capped).
    // Each close schedules with the *current* delay, then doubles (min 30000).
    //
    // AI Note: the 6th step is 30000, not 32000 — the hook caps with
    // Math.min(delay * 2, 30000) *after* scheduling, so the ladder is
    // 1k,2k,4k,8k,16k then straight to the 30k ceiling.
    const delays = [1000, 2000, 4000, 8000, 16000, 30000];
    let expectedCount = 1;
    for (const delay of delays) {
      last().fireClose();
      vi.advanceTimersByTime(delay - 1);
      expect(FakeWebSocket.instances).toHaveLength(expectedCount);
      vi.advanceTimersByTime(1);
      expectedCount += 1;
      expect(FakeWebSocket.instances).toHaveLength(expectedCount);
    }

    // One more close must still only need 30000ms — not 60000 — proving the cap.
    last().fireClose();
    vi.advanceTimersByTime(29999);
    expect(FakeWebSocket.instances).toHaveLength(expectedCount);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(expectedCount + 1);
  });

  /**
   * The token is read at *connect* time, not once at mount. This is what lets a
   * dashboard opened while logged out pick up the session after login without a
   * page reload. Regression guarded: caching the token in a closure, leaving
   * the socket permanently unauthenticated after a login.
   */
  it("re-reads the token on reconnect so a late login is picked up", () => {
    // No token at first connect → bare URL.
    renderHook(() => useWebSocket(vi.fn()));
    expect(last().url).toBe("ws://nexus.test:8080/ws/dashboard");

    // User logs in after the first connection; the reconnect should include it.
    setToken("late-token");
    last().fireClose();
    vi.advanceTimersByTime(1000);

    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(last().url).toBe(
      "ws://nexus.test:8080/ws/dashboard?token=late-token"
    );
  });
});

/**
 * Effect-dependency behaviour. `connect` is memoized on `[onMessage]`, so the
 * identity of the handler decides whether the socket is recycled.
 *
 * AI Note: this makes the hook sensitive to callers passing an inline arrow
 * function — every parent re-render would produce a new identity and therefore
 * tear down and re-dial the socket. Consumers must wrap their handler in
 * `useCallback`; these two tests pin both sides of that contract.
 */
describe("useWebSocket – onMessage handler identity changes", () => {
  /**
   * A genuinely new handler must close the old socket and route frames to the
   * new one. Regression guarded: a stale closure delivering updates into a
   * discarded handler (dashboard appears frozen while frames still arrive).
   */
  it("tears down the old socket and reconnects when onMessage changes", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ h }) => useWebSocket(h), {
      initialProps: { h: first },
    });

    expect(FakeWebSocket.instances).toHaveLength(1);
    const originalSocket = last();

    // connect is memoized on [onMessage]; a new handler must re-run the effect.
    rerender({ h: second });

    expect(originalSocket.close).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    // The new socket routes messages to the *new* handler, not the stale one.
    const payload = { type: "ping" };
    last().fireMessage(JSON.stringify(payload));
    expect(second).toHaveBeenCalledWith(payload);
    expect(first).not.toHaveBeenCalled();
  });

  /**
   * The stability half of the contract: a re-render with the same handler must
   * not churn the connection. Regression guarded: a missing/incorrect memo
   * dependency causing a new socket on every render — a self-inflicted
   * connection storm.
   */
  it("does not reconnect when re-rendered with the same handler", () => {
    const handler = vi.fn();
    const { rerender } = renderHook(({ h }) => useWebSocket(h), {
      initialProps: { h: handler },
    });

    expect(FakeWebSocket.instances).toHaveLength(1);

    // Same handler reference → memoized connect is stable → no new socket.
    rerender({ h: handler });

    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

/** Transport errors: close the socket and let the normal close path recover. */
describe("useWebSocket – error handling", () => {
  /**
   * `onerror` must only close — reconnection is owned solely by `onclose`.
   * Regression guarded: reconnecting from both handlers would double-dial on
   * every failure (a browser fires error then close), doubling sockets each
   * round.
   */
  it("closes the socket when an error fires", () => {
    renderHook(() => useWebSocket(vi.fn()));

    last().fireError();

    expect(last().close).toHaveBeenCalledTimes(1);
    // onerror itself must not synchronously spawn a new socket; reconnection is
    // driven solely by the subsequent onclose event.
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

/** Unmount cleanup: no orphaned socket, no orphaned reconnect timer. */
describe("useWebSocket – teardown on unmount", () => {
  /**
   * Unmounting with a reconnect already pending must cancel that timer as well
   * as close the socket. Regression guarded: an uncleared timeout resurrecting
   * a socket for an unmounted component — a leak that in dev/HMR accumulates
   * one zombie connection per navigation.
   */
  it("clears the pending reconnect timer and closes the socket on unmount", () => {
    vi.useFakeTimers();
    const { unmount } = renderHook(() => useWebSocket(vi.fn()));

    const socket = last();
    // Schedule a reconnect, then unmount before it fires.
    socket.fireClose();
    unmount();

    expect(socket.close).toHaveBeenCalled();

    // The scheduled reconnect must have been cancelled — no new socket appears.
    // 60s comfortably exceeds the 30s backoff ceiling, so a surviving timer
    // would definitely have fired by now.
    vi.advanceTimersByTime(60000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
