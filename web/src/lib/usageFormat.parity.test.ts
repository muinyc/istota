/**
 * Cross-implementation parity for the cost render rule.
 *
 * `usage_render.render_cost` and `formatCost` are two implementations of one
 * stated rule, in two languages, rendering into two media. Nothing structural stops
 * them drifting, and they already had: the CLI showed four decimals and the
 * dashboard two, which turned a sub-cent 24h figure into a flat `$0.00`.
 *
 * The expectations below are the CLI's actual output over this case list,
 * captured by running `render_cost` against it. Regenerate with:
 *
 *     uv run python - <<'PY'
 *     from istota.usage_render import render_cost
 *     for c in CASES: print(c, render_cost(c))
 *     PY
 *
 * A change to either side that is not made to both fails here — but only if the
 * table below is right about the CLI, which nothing in this file can check.
 * `tests/test_cli_render_cost.py` holds the same cases against the real
 * `render_cost`; edit the two together.
 */

import { describe, expect, it } from 'vitest';

import { formatDuration } from '$lib/dateFormat';
import { formatContext, formatCost, formatNumber, formatResetIn } from '$lib/usageFormat';

// [input, what usage_render.render_cost produces]
const PARITY: [Record<string, number>, string][] = [
  [{}, '—'],
  [{ api: 0 }, '$0.00'],
  [{ api: 1.5 }, '$1.50'],
  [{ api: 0.0004 }, '$0.0004'],
  [{ api: 0.009 }, '$0.0090'],
  [{ api: 0.01 }, '$0.01'],
  [{ api: 1234.5 }, '$1234.50'],
  [{ api: 9.0 }, '$9.00'],
  [{ estimated: 0 }, '—'],
  [{ subscription: 99 }, '—'],
  [{ api: 1, subscription: 2 }, '$1.00'],
  [{ estimated: 0, subscription: 1, unknown: 2 }, '—'],
  [{ api: 1.5, estimated: 0, subscription: 9 }, '$1.50'],
];

describe('formatCost parity with the CLI', () => {
  it.each(PARITY)('renders %j the same as the CLI', (input, expected) => {
    expect(formatCost(input)).toBe(expected);
  });
});

/**
 * The three rules that had no parity case, added here rather than in a second
 * file so all four are regenerated together.
 *
 * Two of them are locale-sensitive on the TypeScript side and are not on the
 * Python side, so the literals below need a comma-grouping default locale. That
 * is asserted rather than skipped: a skip would make the whole block silently
 * vacuous, which is the failure mode this spec's round 1 measured five times.
 */
describe('the number and duration rules', () => {
  it('runs under a comma-grouping locale', () => {
    // Python's `f"{n:,}"` always groups with commas; `toLocaleString()` follows
    // the runtime. On a `de-DE` default the tables below are not the CLI's
    // output and the failure is this line, not a code change.
    expect((1234567).toLocaleString()).toBe('1,234,567');
  });

  // [input, what usage_render.fmt_int produces]
  const INT_PARITY: [number, string][] = [
    [0, '0'],
    [999, '999'],
    [1000, '1,000'],
    [1500, '1,500'],
    [1234567, '1,234,567'],
    [-1234, '-1,234'],
  ];

  it.each(INT_PARITY)('formatNumber renders %d as the CLI does', (input, expected) => {
    expect(formatNumber(input)).toBe(expected);
  });

  // [input, what usage_render.fmt_context produces]
  const CONTEXT_PARITY: [number | null, string][] = [
    [null, '—'],
    [0, '0'],
    [999, '999'],
    [1000, '1,000'],
    [14433.6, '14,434'],
    [1234567, '1,234,567'],
    [-1234, '-1,234'],
  ];

  it.each(CONTEXT_PARITY)('formatContext renders %s as the CLI does', (input, expected) => {
    expect(formatContext(input)).toBe(expected);
  });

  /**
   * Two measured differences, asserted so neither can be fixed on one side
   * only. Neither is reachable from a caller today — both figures are token
   * counts — and both would be a real disagreement if one ever were.
   *
   * `fmt_int` casts with `int()`, which truncates, where `toLocaleString`
   * renders the fraction; and `fmt_context` uses Python's banker's rounding
   * where `Math.round` takes a half away from zero.
   */
  it('differs from the CLI on a fractional count, in the two known places', () => {
    expect(formatNumber(2.7)).toBe('2.7'); // fmt_int(2.7) === '2'
    expect(formatContext(2.5)).toBe('3'); // fmt_context(2.5) === '2'
  });

  // [seconds, what doctor._duration and commands._usage_age both produce]
  const DURATION_PARITY: [number, string][] = [
    [1, '1s'],
    [45, '45s'],
    [59, '59s'],
    [60, '1m'],
    [90, '1m'],
    [599, '9m'],
    [3599, '59m'],
    [3600, '1h 00m'],
    [3660, '1h 01m'],
    [3900, '1h 05m'],
    [7200, '2h 00m'],
    [86399, '23h 59m'],
    [86400, '1d 0h'],
    [90000, '1d 1h'],
    [200000, '2d 7h'],
    [525600, '6d 2h'],
  ];

  it.each(DURATION_PARITY)('formatDuration renders %d as the CLI does', (input, expected) => {
    expect(formatDuration(input)).toBe(expected);
  });

  it('formatResetIn is that rule with a sentence around it', () => {
    // The only three places the tile's line is not `resets in ${_duration(s)}`.
    expect(formatResetIn(3900)).toBe('resets in 1h 05m');
    expect(formatResetIn(0)).toBe('resetting now');
    expect(formatResetIn(null)).toBe('no reset scheduled');
  });
});
