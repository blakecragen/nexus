/**
 * Live dashboard WebSocket hook.
 *
 * Role in the system: the frontend's only push channel. The server's
 * `/ws/dashboard` endpoint (`packages/server/src/nexus_server/api/routes/ws.py`,
 * `dashboard_websocket`) broadcasts node-status, job-status, step-log and
 * job-completed events; this hook receives them, JSON-decodes them into
 * `WsMessage` (see `@/types`) and hands each one to the supplied callback.
 *
 * In practice there is exactly one caller: `@/components/Layout`, which passes
 * `handleWsMessage` from `@/stores`. That single subscription keeps the nodes,
 * jobs and live-log stores current for the entire session.
 *
 * Responsibilities: connect, decode, reconnect with exponential backoff, and
 * tear everything down on unmount. The channel is receive-only — the hook never
 * sends. It returns the socket ref purely as an escape hatch; nothing uses it
 * to send today.
 *
 * AI Note: the URL is derived from `window.location`, so the socket always goes
 * to the page's own origin. In dev, `vite.config.ts` proxies `/ws` → the FastAPI
 * server with `ws: true`. If the app is ever served from a different origin than
 * the API, this hook must be changed too — an env-var API base alone will not
 * move the socket.
 */
import { useEffect, useRef, useCallback } from "react";
import type { WsMessage } from "@/types";
import { getToken } from "@/api/client";

/** Callback invoked once per successfully decoded server message. */
type MessageHandler = (msg: WsMessage) => void;

/**
 * Open (and keep open) the dashboard WebSocket for the lifetime of the caller.
 *
 * @param onMessage handler for each decoded `WsMessage`.
 *
 *   AI Note: `onMessage` is a `useCallback` dependency, so its IDENTITY is part
 *   of the hook's contract: passing a new function every render tears down and
 *   reopens the socket every render. Callers must pass a stable reference (a
 *   module-level function, or one wrapped in `useCallback`).
 *
 * @returns a ref to the current `WebSocket` (may be null before the first
 *   connect, and points at a CLOSED socket while a reconnect is pending).
 *
 * Side effects: creates a WebSocket; schedules `setTimeout` reconnects; both are
 * cleaned up on unmount.
 *
 * Reconnect semantics: start at 1000ms, double after each scheduled attempt,
 * cap at 30000ms; reset to 1000ms once a connection actually opens. There is no
 * attempt limit — the hook retries forever, which is what you want for a
 * dashboard left open across a server restart.
 */
export function useWebSocket(onMessage: MessageHandler) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeout = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  /** Current backoff delay in ms (1000 → 30000). Kept in a ref so changing it never re-renders. */
  const reconnectDelay = useRef(1000);

  /**
   * Build the URL, open the socket, and wire its four event handlers.
   * Re-created only when `onMessage` changes; the effect below re-runs and
   * reconnects when that happens.
   */
  const connect = useCallback(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const token = getToken();
    // AI Note: the token is passed as a QUERY PARAM, not a header — the browser
    // WebSocket constructor accepts no custom headers. Consequences worth
    // knowing: (a) the token can land in proxy/access logs, so keep access-token
    // lifetimes short; (b) `getToken()` is read at connect time, so every
    // reconnect picks up the current token and a login mid-session is honoured
    // on the next attempt. The server currently does not verify this param
    // (`dashboard_websocket` is an unauthenticated read-only feed), so it is
    // forward-compatibility, not protection.
    const url = `${protocol}//${host}/ws/dashboard${token ? `?token=${token}` : ""}`;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      reconnectDelay.current = 1000;
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as WsMessage;
        onMessage(msg);
      } catch {
        // ignore malformed messages
        //
        // AI Note: the try also wraps `onMessage`, so a throw inside the store
        // dispatcher is swallowed here and looks identical to a JSON parse
        // failure. Deliberate — one bad event must not kill the feed — but it
        // means handler bugs are silent. Add logging inside the handler, not here.
      }
    };

    // AI Note: `connect` recurses through this timer. The delay used is the
    // CURRENT value; it is doubled inside the callback just before reconnecting,
    // so the first retry waits 1000ms, the second 2000ms, and so on to the 30s
    // cap. Because `reconnectDelay` is a ref, the doubling survives the
    // re-created closure.
    ws.onclose = () => {
      reconnectTimeout.current = setTimeout(() => {
        reconnectDelay.current = Math.min(reconnectDelay.current * 2, 30000);
        connect();
      }, reconnectDelay.current);
    };

    // AI Note: `onerror` only closes the socket — it never reconnects directly.
    // The browser fires onerror-then-onclose, so reconnecting here as well would
    // spawn two sockets per failure and compound into a connection storm.
    ws.onerror = () => {
      ws.close();
    };
  }, [onMessage]);

  useEffect(() => {
    connect();
    // AI Note: cleanup must cancel the pending timer BEFORE closing the socket.
    // Closing fires `onclose`, which schedules another reconnect; clearing first
    // and letting the post-unmount `onclose` fire is why an unmounted component
    // can still leave one orphan timer — the closed socket's handler runs after
    // this cleanup. Tests assert no new socket appears after unmount, so if you
    // touch this ordering, re-run useWebSocket.test.tsx.
    return () => {
      clearTimeout(reconnectTimeout.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return wsRef;
}
