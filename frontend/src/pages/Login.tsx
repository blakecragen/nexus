/**
 * Login.tsx — unauthenticated entry point for the Nexus dashboard.
 *
 * Role in the system:
 *   Rendered by `App.tsx` at the `/login` route. This is the ONLY route that
 *   sits outside the `<Layout />` shell, so it renders no sidebar/header and
 *   assumes no session exists yet.
 *
 * Responsibilities:
 *   - Collect username/password and hand them to `useAuthStore.login()`
 *     (`frontend/src/stores/index.ts`), which POSTs `/api/auth/login`, stashes
 *     the access token via `setToken()` (localStorage key `nexus_token`), saves
 *     the refresh token under `nexus_refresh`, then GETs `/api/auth/me` to
 *     populate `user`.
 *   - Surface server-side auth errors inline instead of throwing.
 *   - Redirect to `/` on success so `<Layout />` can take over.
 *
 * Neighbours:
 *   - Callers: `App.tsx` route table.
 *   - Calls: `useAuthStore` (Zustand) -> `api.login` / `api.getMe`
 *     (`frontend/src/api/client.ts`).
 *   - Counterpart: `client.ts` `request()` force-navigates back here on any
 *     401, so this page is also the implicit "session expired" landing spot.
 */
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores";

/**
 * Full-screen centred sign-in card.
 *
 * What the user sees: the "NEXUS" wordmark, an optional red error banner, and
 * a two-field form (username, password) with a submit button whose label
 * flips to "Signing in..." while the request is in flight.
 *
 * State:
 *   - `username` / `password`: controlled inputs (never persisted anywhere but
 *     component state; only the resulting tokens are stored).
 *   - `error`: last failure message from the auth store, cleared on re-submit.
 *   - `isSubmitting`: disables the button to prevent duplicate login POSTs.
 *
 * Side effects: writes `nexus_token` + `nexus_refresh` to localStorage (via the
 * store) and navigates to `/` with `replace: true` so the browser Back button
 * does not return the user to the login form after a successful sign-in.
 *
 * Props: none — this is a routed page component.
 */
export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const login = useAuthStore((s) => s.login);
  const navigate = useNavigate();

  /**
   * Submit handler for the credentials form.
   *
   * Flow: prevent the native form navigation -> clear any previous error ->
   * `useAuthStore.login()` (network) -> navigate to the dashboard.
   *
   * Errors from `login()` (bad credentials, server down, network failure) are
   * caught and rendered in the banner rather than bubbling to an error
   * boundary, so a typo never blanks the page.
   *
   * AI Note: `isSubmitting` is reset in `finally`, including on the success
   * path where we have already called `navigate()`. React may still be mounted
   * for a tick during the route transition, so this must stay in `finally` —
   * moving it into the `catch` would leave the button permanently disabled if
   * navigation is ever cancelled or the route re-renders this page.
   */
  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      await login(username, password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-background">
      <div className="w-full max-w-sm">
        <div className="bg-card border border-border rounded-xl shadow-sm p-8">
          {/* Heading */}
          <h1 className="text-2xl font-bold tracking-widest text-center mb-8 text-foreground">
            NEXUS
          </h1>

          {/* Error */}
          {error && (
            <div className="mb-4 px-3 py-2 rounded-md bg-destructive/10 text-destructive text-sm">
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label htmlFor="username" className="block text-sm font-medium text-foreground mb-1">
                Username
              </label>
              <input
                id="username"
                type="text"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full border border-input rounded-md px-3 py-2 text-sm bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                placeholder="Enter username"
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-foreground mb-1">
                Password
              </label>
              <input
                id="password"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full border border-input rounded-md px-3 py-2 text-sm bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                placeholder="Enter password"
              />
            </div>

            <button
              type="submit"
              disabled={isSubmitting}
              className="w-full bg-primary text-primary-foreground rounded-md px-3 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
            >
              {isSubmitting ? "Signing in..." : "Sign In"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
