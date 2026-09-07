/**
 * The byte and amount rules, and the one input the two byte formatters
 * disagreed about.
 *
 * `health/documents.test.ts` already covered `formatBytes` and keeps doing so
 * through the re-export; what is here is the divergence itself, so the case
 * that motivated the move is written down where the survivor lives.
 */

import { describe, expect, it } from 'vitest';

import { formatAmount, formatBytes, formatDecimal } from '$lib/format';

describe('formatBytes', () => {
  it('answers 1536 the way the health helper did, not the way /admin did', () => {
    // /admin computed `(1536/1024).toFixed(1)` and rendered `1.5 KB`; this
    // rounds at KB, where the extra digit is noise.
    expect(formatBytes(1536)).toBe('2 KB');
  });

  it('is stricter than the /admin copy about values that are not sizes', () => {
    // /admin: `Math.log(-1)` is NaN, so `units[NaN]` rendered `NaN undefined`.
    expect(formatBytes(-1)).toBe('—');
    // /admin: `!NaN` is true, so a NaN rendered as a confident `0 B`.
    expect(formatBytes(Number.NaN)).toBe('—');
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe('—');
  });

  it('keeps one decimal below 10 and drops it above', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(1023)).toBe('1023 B');
    expect(formatBytes(1024)).toBe('1 KB');
    expect(formatBytes(1024 * 1024 * 1.5)).toBe('1.5 MB');
    expect(formatBytes(1024 * 1024 * 12.34)).toBe('12 MB');
  });
});

describe('formatDecimal and formatAmount', () => {
  it('renders two places', () => {
    expect(formatDecimal(1)).toBe('1.00');
    expect(formatDecimal(1.005)).toBe('1.01');
    expect(formatDecimal(0)).toBe('0.00');
  });

  it('puts the sign in front of the figure, not inside it', () => {
    // The six components that re-implemented the inner call wanted the bare
    // figure; `formatAmount` is the one that owns the sign and the currency.
    expect(formatAmount(-1234.5, 'USD')).toBe(`-${formatDecimal(1234.5)} USD`);
    expect(formatAmount(1234.5, 'USD')).toBe(`${formatDecimal(1234.5)} USD`);
  });
});
