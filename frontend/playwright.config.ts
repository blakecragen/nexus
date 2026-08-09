import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E configuration for Nexus.
 *
 * Browser binaries from cdn.playwright.dev are blocked in this environment, so
 * we drive the locally-installed Google Chrome via `channel: "chrome"` instead
 * of a Playwright-managed Chromium.
 *
 * By default we target an already-running dev stack (UI on :3000 proxying the
 * API on :8000 — see dev.sh). Set PLAYWRIGHT_BASE_URL to point elsewhere.
 * Credentials come from env so secrets stay out of the repo; they default to
 * the dev admin account.
 */
const BASE_URL = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [["list"], ["html", { open: "never" }]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chrome",
      use: { ...devices["Desktop Chrome"], channel: "chrome" },
    },
  ],
  // No `webServer` block: we reuse the running dev stack. To have Playwright
  // manage it, uncomment below and ensure dev.sh deps are available.
  // webServer: {
  //   command: "./dev.sh up",
  //   url: BASE_URL,
  //   reuseExistingServer: true,
  //   timeout: 120_000,
  // },
});
