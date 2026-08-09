/**
 * Tests for the Login page (src/pages/Login.tsx).
 *
 * The page reads `login` from the auth store via a selector
 * (`useAuthStore((s) => s.login)`), so the mock below mimics zustand's
 * selector behaviour: when called with a selector it applies it to a fake
 * state object holding our vi.fn login. react-router's useNavigate is also
 * mocked so we can assert navigation on success.
 *
 * Scope: this file covers the form's *local* behaviour — submit wiring, the
 * in-flight disabled state, error surfacing, and the post-success redirect. The
 * real credential exchange (api.login -> setToken -> getMe) belongs to the auth
 * store and is covered in src/stores/index.test.ts; the end-to-end browser flow
 * lives in e2e/smoke.spec.ts.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderWithRouter, screen, waitFor } from "../test/test-utils";

// Mock the auth store. `loginMock` is reused across the suite and reset in
// beforeEach. The mocked hook supports zustand's selector call signature.
//
// AI Note: the mock must honour the selector form. The page calls
// `useAuthStore((s) => s.login)`; a mock that simply returned `loginMock`
// would make the page invoke the state object instead of the function.
const loginMock = vi.fn();
vi.mock("@/stores", () => ({
  useAuthStore: (selector?: (s: { login: typeof loginMock }) => unknown) => {
    const state = { login: loginMock };
    return selector ? selector(state) : state;
  },
}));

// Mock useNavigate so we can assert the success-path redirect.
//
// AI Note: `importOriginal` is spread back in so MemoryRouter/Link/Route (used
// by renderWithRouter) keep working — only `useNavigate` is swapped out.
const navigateMock = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});

import LoginPage from "./Login";

beforeEach(() => {
  loginMock.mockReset();
  navigateMock.mockReset();
});

describe("LoginPage", () => {
  /**
   * Initial render: branding, both labelled inputs and the submit button, with
   * no error banner. Querying by *label* (not placeholder or testid) also pins
   * the accessible labelling that the Playwright e2e specs rely on.
   */
  it("renders the NEXUS heading, username/password inputs and Sign In button", () => {
    renderWithRouter(<LoginPage />);

    expect(screen.getByRole("heading", { name: "NEXUS" })).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign In" })).toBeInTheDocument();
    // No error banner on first render.
    expect(screen.queryByText(/failed/i)).not.toBeInTheDocument();
  });

  /**
   * Submitting forwards exactly what the user typed, once. Regression guarded:
   * swapped argument order (password sent as username) or a double submit
   * triggering two auth attempts and tripping rate limits/lockouts.
   */
  it("calls login with the typed username and password on submit", async () => {
    loginMock.mockResolvedValue(undefined);
    const { user } = renderWithRouter(<LoginPage />);

    await user.type(screen.getByLabelText("Username"), "alice");
    await user.type(screen.getByLabelText("Password"), "s3cret");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    expect(loginMock).toHaveBeenCalledTimes(1);
    expect(loginMock).toHaveBeenCalledWith("alice", "s3cret");
  });

  /**
   * Success redirects to "/" with `replace: true` so the login page is not left
   * in history — otherwise the browser Back button returns an authenticated
   * user to the login form. Also asserts the button returns to its idle state.
   */
  it("navigates to '/' on successful login and shows no error", async () => {
    loginMock.mockResolvedValue(undefined);
    const { user } = renderWithRouter(<LoginPage />);

    await user.type(screen.getByLabelText("Username"), "alice");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    // Redirect happens with replace:true after login resolves.
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith("/", { replace: true })
    );
    expect(navigateMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/failed/i)).not.toBeInTheDocument();
    // Button is back to its idle, enabled state once the request settles.
    expect(screen.getByRole("button", { name: "Sign In" })).toBeEnabled();
  });

  /**
   * Failure path: the rejection message is shown, navigation does NOT happen,
   * and the button is re-enabled for a retry.
   *
   * Regression guarded (security-relevant): navigating on a rejected login
   * would drop an unauthenticated user into the app shell, where every request
   * 401s and bounces them back — a confusing loop that also hides the real
   * "wrong password" reason.
   */
  it("shows an error banner with the rejection message and does not navigate", async () => {
    loginMock.mockRejectedValue(new Error("Invalid credentials"));
    const { user } = renderWithRouter(<LoginPage />);

    await user.type(screen.getByLabelText("Username"), "bob");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    expect(await screen.findByText("Invalid credentials")).toBeInTheDocument();
    expect(navigateMock).not.toHaveBeenCalled();
    // `finally` must re-enable the button so the user can retry.
    expect(screen.getByRole("button", { name: "Sign In" })).toBeEnabled();
  });

  /**
   * Non-Error rejections (a thrown string, a rejected fetch primitive) must be
   * replaced by a generic message.
   *
   * AI Note: the `queryByText("boom")` assertion is the point of this test —
   * raw rejection values can embed server internals, so the page must never
   * render them verbatim.
   */
  it("falls back to a generic message when the rejection is not an Error", async () => {
    loginMock.mockRejectedValue("boom");
    const { user } = renderWithRouter(<LoginPage />);

    await user.type(screen.getByLabelText("Username"), "bob");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    expect(await screen.findByText("Login failed")).toBeInTheDocument();
    // The raw rejection value should never leak into the UI.
    expect(screen.queryByText("boom")).not.toBeInTheDocument();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  /**
   * `handleSubmit` clears the previous error before awaiting, so a successful
   * retry leaves no stale banner. Regression guarded: a user who fixes their
   * password still seeing "Invalid credentials" after logging in successfully.
   */
  it("clears a previous error banner when the form is resubmitted", async () => {
    // First attempt fails, second succeeds: the banner must disappear.
    loginMock
      .mockRejectedValueOnce(new Error("Invalid credentials"))
      .mockResolvedValueOnce(undefined);
    const { user } = renderWithRouter(<LoginPage />);

    await user.type(screen.getByLabelText("Username"), "bob");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    // Error from the first (failed) attempt.
    expect(await screen.findByText("Invalid credentials")).toBeInTheDocument();

    // Retry — handleSubmit calls setError(null) before awaiting login.
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    await waitFor(() =>
      expect(screen.queryByText("Invalid credentials")).not.toBeInTheDocument()
    );
    expect(navigateMock).toHaveBeenCalledWith("/", { replace: true });
    expect(loginMock).toHaveBeenCalledTimes(2);
  });

  /**
   * In-flight state: the button is disabled and relabelled while the request is
   * outstanding, and navigation is deferred until it resolves.
   *
   * AI Note: `loginMock` is replaced with a manually-controlled promise
   * (`resolveLogin`) rather than a timer. That is the only way to observe the
   * intermediate render deterministically — an auto-resolving mock settles
   * before RTL can assert on the pending state.
   *
   * Regression guarded: an enabled button during submit lets an impatient user
   * fire several concurrent auth requests.
   */
  it("disables the button and shows 'Signing in...' while the request is in flight", async () => {
    // Keep login pending so we can observe the in-flight state.
    let resolveLogin: () => void = () => {};
    loginMock.mockImplementation(
      () => new Promise<void>((resolve) => { resolveLogin = resolve; })
    );
    const { user } = renderWithRouter(<LoginPage />);

    await user.type(screen.getByLabelText("Username"), "alice");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    const submitting = await screen.findByRole("button", { name: "Signing in..." });
    expect(submitting).toBeDisabled();
    // Navigation must NOT happen until login actually resolves.
    expect(navigateMock).not.toHaveBeenCalled();

    // Resolve and confirm the button returns to its idle label.
    resolveLogin();
    expect(await screen.findByRole("button", { name: "Sign In" })).toBeEnabled();
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith("/", { replace: true })
    );
  });
});
