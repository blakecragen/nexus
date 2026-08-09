/**
 * E2E smoke suite for the Nexus frontend (Playwright, system Chrome).
 *
 * This is the cheapest possible end-to-end signal: it proves the whole stack is
 * actually wired together — Vite serving the app on :3000, the proxy reaching
 * the FastAPI server on :8000, the auth endpoint rejecting bad credentials, and
 * the router's auth guard redirecting unauthenticated users. Unlike the vitest
 * suites, nothing is mocked here.
 *
 * All three cases are intentionally credential-free so they run everywhere;
 * flows that need a real session use the `authedPage` fixture from fixtures.ts.
 */
import { test, expect } from "@playwright/test";

/**
 * E2E smoke test — validates that Playwright can drive system Chrome against
 * the running Nexus stack and that the public login page renders. This does
 * NOT require credentials, so it always runs; authenticated flows belong in a
 * separate spec using the `authedPage` fixture from fixtures.ts, which gates on
 * env-provided credentials (no such spec exists yet).
 *
 * AI Note: these specs assume a stack is already up (playwright.config.ts has
 * no `webServer` block — see the commented-out block there). A failure here is
 * far more often "dev.sh isn't running" than a genuine UI regression; check
 * that first.
 */
test.describe("smoke", () => {
  /**
   * The public login page renders its complete form.
   *
   * Doubles as infrastructure validation (Chrome launches, the dev server
   * responds) and as an accessibility contract: the fields are located by
   * *label*, which is exactly how fixtures.ts `login()` finds them. Losing the
   * labels would break every authenticated spec, and this test localises that
   * failure to one obvious cause.
   */
  test("login page renders the form", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "NEXUS" })).toBeVisible();
    await expect(page.getByLabel(/username/i)).toBeVisible();
    await expect(page.getByLabel(/password/i)).toBeVisible();
    await expect(page.getByRole("button", { name: /sign in/i })).toBeVisible();
  });

  /**
   * Rejected credentials surface an error and keep the user on /login.
   *
   * This is the one test that proves the browser -> Vite proxy -> API -> DB
   * chain is genuinely connected: a disconnected backend would produce a
   * different failure (network/500) instead of a credential rejection.
   *
   * Regression guarded (security-relevant): navigating away from /login on a
   * failed login would drop an unauthenticated user into the app shell.
   *
   * AI Note: the message regex is deliberately broad — the visible text differs
   * depending on whether the server returns a `{detail}` body or the client
   * falls back to "HTTP 401", and both are acceptable outcomes here.
   */
  test("submitting bad credentials surfaces an error and stays on /login", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel(/username/i).fill("definitely-not-a-user");
    await page.getByLabel(/password/i).fill("wrong-password");
    await page.getByRole("button", { name: /sign in/i }).click();
    // Error banner appears; URL remains /login.
    await expect(page.getByText(/invalid credentials|login failed|http 401/i)).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
  });

  /**
   * The router's auth guard: a protected route visited without a session
   * redirects to /login.
   *
   * Regression guarded (security-relevant): rendering /jobs to an anonymous
   * visitor. Even though the API would refuse the data, the shell should never
   * be reachable unauthenticated. The 15 s allowance covers the auth store's
   * boot-time `fetchUser()` round-trip before the guard can decide.
   */
  test("unauthenticated visit to a protected route redirects to login", async ({ page }) => {
    await page.goto("/jobs");
    await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 });
  });
});
