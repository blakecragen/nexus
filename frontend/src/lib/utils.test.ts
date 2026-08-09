/**
 * Tests for the shared UI helpers in src/lib/utils.ts.
 *
 * Three tiny but pervasive functions are covered:
 * - `cn`   — clsx + tailwind-merge class combiner used by essentially every
 *            component; its conflict-resolution semantics decide which Tailwind
 *            utility actually wins at render time.
 * - `formatBytes`     — human-readable sizes (storage/results UI).
 * - `formatRelativeTime` — "5m ago" timestamps (node heartbeats, job rows).
 *
 * These are pure functions with no I/O, so nothing is stubbed. The suite leans
 * on exact boundary values (1023/1024, 59s/60s) because the bugs these helpers
 * attract are all off-by-one/unit-selection bugs.
 *
 * AI Note: `formatRelativeTime` is also where the historical "-17990s ago"
 * regression lived (naive UTC datetimes from the server). The future-date
 * clamping cases at the bottom of this file are what pin that fix.
 */
import { describe, it, expect } from "vitest";
import { cn, formatBytes, formatRelativeTime } from "./utils";

/**
 * `cn` — conditional class names with Tailwind conflict resolution.
 *
 * Behaviour comes from two layers: clsx (falsy filtering, objects, arrays) and
 * tailwind-merge (later utility in the same group wins). Both layers are pinned
 * because swapping the implementation for a plain `join(" ")` would pass the
 * simple cases and silently break every `className` override in the app.
 */
describe("cn", () => {
  /** Baseline concatenation with a single space separator. */
  it("joins multiple class strings", () => {
    expect(cn("foo", "bar")).toBe("foo bar");
  });

  /**
   * Falsy inputs are dropped entirely. This is the `cond && "class"` idiom used
   * throughout the components; without it the DOM would receive literal
   * "false"/"null" class tokens.
   */
  it("drops falsy/conditional inputs (false, null, undefined, empty)", () => {
    expect(cn("foo", false, null, undefined, "", "bar")).toBe("foo bar");
  });

  /** Object syntax: keys with truthy values are emitted, falsy keys omitted. */
  it("supports object syntax for conditional classes", () => {
    expect(cn("base", { active: true, hidden: false })).toBe("base active");
  });

  /** Arrays are flattened, supporting the `cn([...base, extra])` pattern. */
  it("flattens array inputs", () => {
    expect(cn(["foo", "bar"], "baz")).toBe("foo bar baz");
  });

  /**
   * The tailwind-merge contract: conflicting utilities collapse to the last
   * one. This is what makes `className` props able to override a component's
   * defaults. Regression guarded: emitting both `p-2 p-4` leaves the winner up
   * to CSS source order, which is effectively random.
   */
  it("dedupes/merges conflicting tailwind classes (last wins)", () => {
    // tailwind-merge: a later padding overrides an earlier one
    expect(cn("p-2", "p-4")).toBe("p-4");
  });

  /**
   * Conflict resolution is per axis: `px-*` overrides `px-*` while `py-*`
   * survives untouched. Regression guarded: an over-eager merge that discards
   * unrelated utilities on the other axis.
   */
  it("merges conflicting directional tailwind utilities", () => {
    // px-2 and px-4 conflict -> px-4 kept
    expect(cn("px-2 py-1", "px-4")).toBe("py-1 px-4");
  });

  /** Non-conflicting utilities from different groups must both be preserved. */
  it("keeps non-conflicting tailwind classes together", () => {
    expect(cn("text-red-500", "font-bold")).toBe("text-red-500 font-bold");
  });

  /**
   * Empty/all-falsy input yields "" rather than undefined, so it is safe to
   * hand straight to `className` without a fallback.
   */
  it("returns an empty string for no/all-falsy inputs", () => {
    expect(cn()).toBe("");
    expect(cn(false, null, undefined)).toBe("");
  });
});

/**
 * `formatBytes` — binary (1024-based) size formatting used by the storage and
 * job-results UI. Every unit boundary is asserted because unit selection uses a
 * log/divide and is the classic place for an off-by-one.
 */
describe("formatBytes", () => {
  /** Zero is special-cased; without it a log(0) would yield -Infinity/NaN. */
  it("returns '0 B' for zero", () => {
    expect(formatBytes(0)).toBe("0 B");
  });

  /** Everything below 1 KiB stays in raw bytes, including the 1023 edge. */
  it("formats raw bytes (< 1 KB)", () => {
    expect(formatBytes(1)).toBe("1 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1023)).toBe("1023 B");
  });

  /** Exactly 1024 must roll over to KB (binary units, not 1000-based SI). */
  it("formats KB at the 1024 boundary", () => {
    expect(formatBytes(1024)).toBe("1 KB");
  });

  /** MB rollover boundary. */
  it("formats MB at the boundary", () => {
    expect(formatBytes(1024 * 1024)).toBe("1 MB");
  });

  /** GB rollover boundary. */
  it("formats GB at the boundary", () => {
    expect(formatBytes(1024 * 1024 * 1024)).toBe("1 GB");
  });

  /** TB rollover boundary — the largest unit the table supports. */
  it("formats TB at the boundary", () => {
    expect(formatBytes(1024 ** 4)).toBe("1 TB");
  });

  /** One decimal of precision, both for exact and rounded fractions. */
  it("rounds to one decimal place", () => {
    // 1536 bytes = 1.5 KB
    expect(formatBytes(1536)).toBe("1.5 KB");
    // 1500 bytes = 1.46... KB -> rounds to 1.5 KB
    expect(formatBytes(1500)).toBe("1.5 KB");
  });

  /**
   * `parseFloat` strips a trailing ".0" so whole values read as "2 KB" rather
   * than "2.0 KB". Regression guarded: dropping parseFloat makes every round
   * number in the storage UI grow a noisy decimal.
   */
  it("strips trailing .0 via parseFloat", () => {
    // 2048 bytes = 2.0 KB -> parseFloat removes the .0
    expect(formatBytes(2048)).toBe("2 KB");
  });

  /**
   * One byte below the MB boundary must still report in KB (as "1024 KB").
   * Regression guarded: a `>=`/`>` flip that would show "1 MB" for a value that
   * is not yet a megabyte.
   */
  it("picks the correct unit just below a boundary", () => {
    // 1024*1024 - 1 bytes is still < 1 MB, expressed in KB
    expect(formatBytes(1024 * 1024 - 1)).toBe("1024 KB");
  });

  /** Fractional values in the larger units keep their single decimal. */
  it("formats fractional MB and GB with one decimal", () => {
    expect(formatBytes(2.5 * 1024 * 1024)).toBe("2.5 MB");
    expect(formatBytes(1.5 * 1024 * 1024 * 1024)).toBe("1.5 GB");
  });

  /** Rounding direction at the tenths place, in both directions. */
  it("rounds at the tenths place", () => {
    // 1280 / 1024 = 1.25 KB -> rounds up to 1.3 KB
    expect(formatBytes(1280)).toBe("1.3 KB");
    // 1075 / 1024 = 1.0498... KB -> rounds down to 1 KB (.0 stripped by parseFloat)
    expect(formatBytes(1075)).toBe("1 KB");
  });

  /**
   * Characterisation test, not an endorsement: past TB the unit table runs out
   * and the function emits the literal string "undefined" as the unit.
   *
   * AI Note: this asserts current (wrong-looking but deliberate-to-pin)
   * behaviour. If someone extends the `sizes` table with "PB" this test will
   * fail — that failure is the signal to update the expectation, not a bug.
   */
  it("falls off the sizes table beyond TB (PB-scale yields 'undefined' unit)", () => {
    // The sizes array stops at TB; a petabyte-scale value indexes past the end.
    // The UI never passes sizes this large, but we pin the real behavior so a
    // future change to the table is caught.
    expect(formatBytes(1024 ** 5)).toBe("1 undefined");
  });
});

/**
 * `formatRelativeTime` — "Ns/Nm/Nh/Nd ago" strings shown on node heartbeats and
 * job rows.
 *
 * AI Note: these tests compute their inputs from the real `Date.now()` rather
 * than using fake timers. That keeps them realistic but makes them latency
 * sensitive, which is why every boundary case adds a cushion (e.g. `59s + 500ms`)
 * — without it, a slow CI machine could tick past the unit boundary between
 * building the date and formatting it, producing a flaky failure.
 */
describe("formatRelativeTime", () => {
  // Construct dates relative to "now" with wide margins to avoid flakiness.
  /** Build a Date `ms` milliseconds in the past, relative to the current clock. */
  const ago = (ms: number) => new Date(Date.now() - ms);
  /** Millisecond constants used to express the unit boundaries readably. */
  const SEC = 1000;
  const MIN = 60 * SEC;
  const HOUR = 60 * MIN;
  const DAY = 24 * HOUR;

  /** Sub-minute deltas render in seconds. */
  it("formats sub-minute differences in seconds", () => {
    expect(formatRelativeTime(ago(5 * SEC))).toBe("5s ago");
  });

  /** A brand-new timestamp reads "0s ago" rather than "" or "NaN". */
  it("reports '0s ago' for a just-now timestamp", () => {
    expect(formatRelativeTime(ago(0))).toBe("0s ago");
  });

  /** Seconds -> minutes rollover, asserted on both sides of the boundary. */
  it("stays in seconds at the 59s edge and rolls to minutes at 60s", () => {
    // Add a small cushion so test-execution latency can't push us over a unit.
    expect(formatRelativeTime(ago(59 * SEC + 500))).toBe("59s ago");
    expect(formatRelativeTime(ago(60 * SEC + 500))).toBe("1m ago");
  });

  /** Minutes -> hours rollover, both sides. */
  it("stays in minutes at the 59m edge and rolls to hours at 60m", () => {
    expect(formatRelativeTime(ago(59 * MIN + 30 * SEC))).toBe("59m ago");
    expect(formatRelativeTime(ago(60 * MIN + 30 * SEC))).toBe("1h ago");
  });

  /** Hours -> days rollover, both sides. */
  it("stays in hours at the 23h edge and rolls to days at 24h", () => {
    expect(formatRelativeTime(ago(23 * HOUR + 30 * MIN))).toBe("23h ago");
    expect(formatRelativeTime(ago(24 * HOUR + 30 * MIN))).toBe("1d ago");
  });

  /** Sub-unit remainders are floored, never rounded up (5m30s -> "5m ago"). */
  it("formats minute-range differences in minutes", () => {
    // 5.5 min -> floors to 5m
    expect(formatRelativeTime(ago(5 * MIN + 30 * SEC))).toBe("5m ago");
  });

  /** Same flooring rule in the hour range. */
  it("formats hour-range differences in hours", () => {
    expect(formatRelativeTime(ago(3 * HOUR + 10 * MIN))).toBe("3h ago");
  });

  /** Same flooring rule in the day range. */
  it("formats day-range differences in days", () => {
    expect(formatRelativeTime(ago(2 * DAY + 5 * HOUR))).toBe("2d ago");
  });

  /**
   * ISO strings are accepted as well as Date objects — the API returns strings,
   * so this is the path actually used in production.
   */
  it("accepts a string date as well as a Date object", () => {
    const iso = ago(10 * SEC).toISOString();
    expect(formatRelativeTime(iso)).toBe("10s ago");
  });

  /** Unparseable strings degrade to an em-dash rather than "NaN ago". */
  it("returns '—' for an invalid date string", () => {
    expect(formatRelativeTime("not-a-real-date")).toBe("—");
  });

  /** Same guard for an Invalid Date object (e.g. `new Date(undefined)`). */
  it("returns '—' for an invalid Date object", () => {
    expect(formatRelativeTime(new Date("nonsense"))).toBe("—");
  });

  /**
   * Future timestamps clamp to "0s ago".
   *
   * AI Note: this is the frontend half of the naive-UTC-datetime fix. Server
   * timestamps can legitimately land slightly in the future (clock skew between
   * the API host and the browser); without the clamp the UI printed negative
   * durations such as "-17990s ago".
   */
  it("clamps a future date to '0s ago'", () => {
    const future = new Date(Date.now() + 60 * SEC);
    expect(formatRelativeTime(future)).toBe("0s ago");
  });

  /**
   * The same clamp must hold for gross skew (hours, e.g. a timezone bug), and
   * the output must contain no "-" at all — the explicit anti-regression assert
   * for the "-17990s ago" display bug.
   */
  it("clamps a large clock-skew future date to '0s ago' (no negative)", () => {
    const future = new Date(Date.now() + 5 * HOUR);
    const result = formatRelativeTime(future);
    expect(result).toBe("0s ago");
    expect(result).not.toContain("-");
  });
});
