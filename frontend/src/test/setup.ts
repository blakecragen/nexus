/**
 * Global test setup for the Nexus frontend suite.
 *
 * - Extends Vitest's `expect` with jest-dom matchers (toBeInTheDocument, etc.).
 * - Runs RTL cleanup after each test so the jsdom DOM never leaks between tests.
 * - Stubs browser APIs that components/hooks touch but jsdom doesn't implement
 *   (matchMedia, ResizeObserver, scrollIntoView). Individual tests can still
 *   override these via vi.spyOn / vi.stubGlobal.
 * - Provides a minimal localStorage that resets between tests.
 *
 * Loaded automatically for every test file via `setupFiles` in
 * `frontend/vitest.config.ts`; nothing imports it directly. Companion module:
 * `./test-utils.tsx`, which provides render helpers and fixture factories.
 *
 * AI Note: order of the sections below is deliberate. The `afterEach(cleanup)`
 * and `beforeEach(localStorage)` hooks registered here run BEFORE hooks
 * registered inside individual test files (Vitest runs beforeEach in
 * registration order and afterEach in reverse), so a test file's own beforeEach
 * can rely on localStorage already being fresh.
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// AI Note: without this, a component mounted in one test stays in document.body
// and the next test's `screen.getByText` finds two matches and throws
// "found multiple elements" — a confusing failure in a test that looks correct.
afterEach(() => {
  cleanup();
});

// ── localStorage: real-ish in-memory implementation, reset per test ──────────

/**
 * Minimal in-memory `Storage` implementation.
 *
 * A real object (not a `vi.fn()` mock) because production code round-trips
 * values through it — `@/api/client.setToken` writes `nexus_token` and expects
 * a later `getItem` to return it, and `useAuthStore.login` writes
 * `nexus_refresh`. A jest-style mock returning `undefined` would make those
 * flows untestable.
 *
 * AI Note: `getItem` returns `null` (not `undefined`) for a missing key,
 * matching the real Storage contract — tests assert `toBeNull()` after logout.
 */
class LocalStorageMock implements Storage {
  private store: Record<string, string> = {};
  get length() {
    return Object.keys(this.store).length;
  }
  clear() {
    this.store = {};
  }
  getItem(key: string) {
    return key in this.store ? this.store[key] : null;
  }
  setItem(key: string, value: string) {
    this.store[key] = String(value);
  }
  removeItem(key: string) {
    delete this.store[key];
  }
  key(index: number) {
    return Object.keys(this.store)[index] ?? null;
  }
}

// AI Note: a BRAND NEW instance per test, so a token written by one test cannot
// leak into the next and accidentally authenticate it. This is why auth tests
// can assert on an empty starting state without clearing anything themselves.
//
// AI Note: `@/api/client` reads localStorage at MODULE LOAD to seed its token,
// which happens before this hook on first import. Tests that need a pre-seeded
// token must therefore call `setToken()` explicitly rather than writing the key
// and expecting the client to pick it up.
beforeEach(() => {
  vi.stubGlobal("localStorage", new LocalStorageMock());
});

// ── matchMedia ───────────────────────────────────────────────────────────────
// AI Note: guarded with `if (!...)` so a newer jsdom that ships a real
// implementation wins over this stub. `matches: false` means every media query
// reports "not matching" — components with responsive branches always render
// their default/desktop branch in tests.
if (!window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

// ── ResizeObserver (Radix UI / charts rely on it) ────────────────────────────
// AI Note: a no-op class, not a spy — Radix only needs the constructor and the
// three methods to exist. Because `observe` never fires a callback, anything
// that sizes itself from an observed box measures 0 in tests; assert on
// presence/props rather than computed dimensions.
if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// ── Element methods jsdom omits ──────────────────────────────────────────────
// AI Note: jsdom has no layout engine, so scrollIntoView is simply absent and
// calling it throws. Log viewers and select menus auto-scroll, so the stub keeps
// those components from crashing the test rather than being asserted on.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}
