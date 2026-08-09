/**
 * Small, dependency-light presentation helpers shared across the whole UI.
 *
 * Role in the system: pure formatting/classname utilities with no state, no I/O
 * and no imports from `@/api`, `@/stores` or `@/types`. Every page and shadcn/ui
 * primitive imports `cn` from here; `formatBytes` and `formatRelativeTime` are
 * used by the Nodes, Jobs, Job Detail and Storage pages to render server data.
 *
 * Keep this module pure — anything that fetches, reads stores, or touches the
 * DOM belongs elsewhere, since these functions are called during render.
 */
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge Tailwind class names, resolving conflicts in favour of the last one.
 *
 * `clsx` flattens conditionals/arrays/objects into a string; `twMerge` then
 * de-duplicates conflicting Tailwind utilities so `cn("p-2", "p-4")` yields
 * `"p-4"` instead of both. This is the standard shadcn/ui helper and is what
 * makes component `className` props able to override built-in styling.
 *
 * AI Note: order matters — later arguments win. Component authors must spread
 * the caller's `className` LAST or user overrides silently lose.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Render a byte count as a short human string, e.g. `1536` → `"1.5 KB"`.
 *
 * Used for storage-backend capacity/usage, artifact sizes, and results-tarball
 * entry sizes.
 *
 * AI Note: uses binary units (1024) but SI-style labels ("KB" not "KiB") — the
 * common convention, but do not "correct" one half without the other.
 *
 * AI Note: the `sizes` table stops at TB. A value >= 1 PB indexes past the end
 * and renders `"<n> undefined"`. Fine for artifact/tarball sizes; a real risk if
 * this is ever reused for aggregate cluster capacity.
 *
 * AI Note: negative input is not handled — `Math.log` of a negative is NaN, so
 * the result is `"NaN undefined"`. Callers only pass server-reported sizes.
 */
export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

/**
 * Render a past timestamp as a coarse "time ago" string.
 *
 * @param date an ISO-8601 string from the API (`last_heartbeat`, `created_at`,
 *   `completed_at`, ...) or a `Date`.
 * @returns `"12s ago"` / `"5m ago"` / `"3h ago"` / `"2d ago"`, or `"—"` for an
 *   unparseable value. Never returns weeks/months — everything older than a day
 *   is reported in days.
 *
 * Drives the node "last seen" column and job timing columns. Note it is NOT
 * reactive: it computes against `Date.now()` at render time, so a component that
 * never re-renders will show a frozen value. Pages that care re-render on the
 * WebSocket feed or a poll.
 *
 * AI Note: the server must send timezone-aware (UTC) timestamps. A naive
 * datetime is parsed by `new Date()` as LOCAL time, which previously produced
 * clock-skewed output like "-17990s ago". The server side is fixed by a
 * `UTCDateTime` serializer; the clamp below is the belt-and-braces half.
 */
export function formatRelativeTime(date: string | Date): string {
  const now = new Date();
  const d = new Date(date);
  const diff = now.getTime() - d.getTime();
  if (isNaN(diff)) return "—";
  // Clamp clock skew / future timestamps to 0 so we never show a nonsensical
  // negative like "-17990s ago".
  const seconds = Math.max(0, Math.floor(diff / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
