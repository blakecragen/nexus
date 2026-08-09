/**
 * Playwright E2E fixtures and helpers for the Nexus frontend.
 *
 * Role in the suite: `playwright.config.ts` points `testDir` at ./e2e and runs
 * every *.spec.ts through system Chrome against an already-running dev stack
 * (UI on :3000 proxying the API on :8000 — see dev.sh). Specs that need an
 * authenticated session import `test` from here instead of "@playwright/test"
 * to get the `authedPage` fixture; `smoke.spec.ts` deliberately does not,
 * because it only exercises public pages.
 *
 * Unlike the vitest suites (which mock the store/api boundary), nothing here is
 * stubbed: these helpers drive a real browser against a real server and a real
 * database.
 */
import { test as base, expect, type Page } from "@playwright/test";

/**
 * Shared E2E helpers and fixtures.
 *
 * Credentials are read from env (NEXUS_E2E_USER / NEXUS_E2E_PASSWORD), falling
 * back to the dev admin account so the suite runs out-of-the-box against a
 * local `dev.sh up` stack. Never hardcode real secrets here.
 *
 * AI Note: the "admin"/"admin" defaults are the dev-only seed credentials that
 * dev.sh provisions (NEXUS_ADMIN_PASSWORD defaults to "admin"). They exist so
 * the suite is zero-config locally. Running against any shared or deployed
 * environment requires setting the env vars — the defaults will simply fail to
 * log in there, which is the intended outcome.
 */
export const E2E_USER = process.env.NEXUS_E2E_USER || "admin";
/** Password counterpart to {@link E2E_USER}; see the note above on defaults. */
export const E2E_PASSWORD = process.env.NEXUS_E2E_PASSWORD || "admin";

/**
 * Log in through the real UI and wait until the app shell is reachable.
 *
 * Drives the actual login form (rather than seeding a token) so the fixture
 * exercises the same path a user takes, including the auth store writing the
 * token and the post-login redirect.
 *
 * @param page     Playwright page to drive
 * @param user     username; defaults to {@link E2E_USER}
 * @param password password; defaults to {@link E2E_PASSWORD}
 *
 * AI Note: success is asserted as "the URL is no longer /login" rather than
 * "the URL is /", because the app redirects to the originally-requested route
 * after login. The 15 s timeout covers the cold-start cost of the dev server
 * compiling the authenticated route chunks on first hit.
 */
export async function login(page: Page, user = E2E_USER, password = E2E_PASSWORD) {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(user);
  await page.getByLabel(/password/i).fill(password);
  await page.getByRole("button", { name: /sign in|log ?in/i }).click();
  // After login we should leave /login.
  await expect(page).not.toHaveURL(/\/login$/, { timeout: 15_000 });
}

/**
 * Playwright `test` extended with an `authedPage` fixture.
 *
 * A spec that declares `async ({ authedPage })` receives a page that has
 * already completed the login flow, so it can start at the feature under test.
 *
 * AI Note: the fixture logs in per test rather than reusing a saved storage
 * state. That costs one form submission per test but guarantees isolation —
 * important because these specs run `fullyParallel` against one shared backend.
 * If suite runtime becomes a problem, the right fix is Playwright's
 * `storageState`, not sharing a page between tests.
 */
export const test = base.extend<{ authedPage: Page }>({
  authedPage: async ({ page }, use) => {
    await login(page);
    await use(page);
  },
});

/** Re-exported so specs get `test` and `expect` from a single import. */
export { expect };
