/**
 * Browser entry point for the Nexus dashboard SPA.
 *
 * Role in the system: this is the single module Vite loads from `index.html`
 * (`<script type="module" src="/src/main.tsx">`). It mounts the React tree into
 * the `#root` element and pulls in the global Tailwind stylesheet. Everything
 * else — routing, auth gating, the live WebSocket feed — is set up downstream
 * by `@/App` and `@/components/Layout`.
 *
 * Neighbours:
 * - imports `@/index.css` (Tailwind v4 layers + the design tokens the whole UI
 *   references as `bg-background`, `text-primary`, `bg-sidebar`, ...).
 * - imports `@/App`, which owns the react-router route table.
 *
 * There is no provider stack here on purpose: global state lives in zustand
 * stores (`@/stores`) which are module singletons and need no React context.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@/index.css";
import App from "@/App";

// AI Note: the `!` non-null assertion on getElementById is load-bearing — if
// `index.html` ever stops shipping a `<div id="root">`, this throws at startup
// with a confusing "null is not an object" instead of a clear error.
//
// AI Note: StrictMode double-invokes effects in development. That is why
// `useWebSocket` (see @/hooks/useWebSocket) must be idempotent: its cleanup
// closes the socket and cancels the pending reconnect timer, otherwise dev
// builds would leak a second dashboard WebSocket per mount.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
