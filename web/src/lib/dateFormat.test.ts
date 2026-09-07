/**
 * The guard that motivated this module, and the tree guard that keeps it the
 * only copy.
 *
 * The assertions below deliberately do not hardcode a rendered date: the output
 * is locale- and timezone-dependent, and a table of literals would say more
 * about the machine than about the code. What is asserted instead is the thing
 * that was actually broken — `Invalid Date` — plus the identities that pin the
 * option and fallback plumbing.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { formatDate, formatDateTime, formatDuration, formatMinutes } from '$lib/dateFormat';

describe('formatDate', () => {
  it('renders a bare YYYY-MM-DD as that calendar day', () => {
    // Local midnight, not UTC midnight — `new Date('2026-09-05')` is UTC and
    // renders as the 4th anywhere west of Greenwich.
    expect(formatDate('2026-09-05')).toBe(
      new Date(2026, 8, 5).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      }),
    );
  });

  it('renders a value that already carries a T instead of Invalid Date', () => {
    // The whole finding: three money components appended `T00:00:00`
    // unconditionally, so a full timestamp became `…ZT00:00:00`.
    const rendered = formatDate('2026-09-05T14:22:31Z');
    expect(rendered).not.toBe('Invalid Date');
    expect(rendered).toBe(
      new Date('2026-09-05T14:22:31Z').toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      }),
    );
  });

  it('renders a space-separated SQLite timestamp instead of Invalid Date', () => {
    const rendered = formatDate('2026-09-05 14:22:31');
    expect(rendered).not.toBe('Invalid Date');
    expect(rendered).toBe(formatDate('2026-09-05T14:22:31'));
  });

  it('returns the empty string for a missing value, and the override when given', () => {
    expect(formatDate(null)).toBe('');
    expect(formatDate(undefined)).toBe('');
    expect(formatDate('')).toBe('');
    expect(formatDate(null, { empty: '—' })).toBe('—');
  });

  it('returns an unparseable value as it arrived, never Invalid Date', () => {
    expect(formatDate('not a date')).toBe('not a date');
  });

  it('takes the caller Intl options in place of the default, not merged with it', () => {
    // `/health/bloodwork` asks for 2-digit month and day and no short month;
    // a merge would leave `month: 'short'` in and the option would do nothing.
    const opts: Intl.DateTimeFormatOptions = {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    };
    expect(formatDate('2026-09-05', opts)).toBe(
      new Date(2026, 8, 5).toLocaleDateString(undefined, opts),
    );
  });

  it('honours a pinned locale', () => {
    expect(formatDate('2026-09-05', { locale: 'en-US', month: 'short', day: 'numeric' })).toBe(
      'Sep 5',
    );
    expect(formatDate('2026-09-05', { locale: 'de-DE', month: 'short', day: 'numeric' })).not.toBe(
      'Sep 5',
    );
  });
});

describe('formatDateTime', () => {
  it('does not append a midnight suffix', () => {
    expect(formatDateTime('2026-09-05T14:22:31Z')).toBe(
      new Date('2026-09-05T14:22:31Z').toLocaleString(),
    );
  });

  it('carries its own empty and unparseable fallbacks', () => {
    expect(formatDateTime(null, { empty: 'never' })).toBe('never');
    expect(formatDateTime('')).toBe('');
    expect(formatDateTime('nonsense')).toBe('nonsense');
  });
});

describe('formatDuration', () => {
  it('renders two units at most', () => {
    expect(formatDuration(45)).toBe('45s');
    expect(formatDuration(90)).toBe('1m');
    expect(formatDuration(3600)).toBe('1h 00m');
    expect(formatDuration(3900)).toBe('1h 05m');
    expect(formatDuration(86400)).toBe('1d 0h');
    expect(formatDuration(200000)).toBe('2d 7h');
  });

  it('floors at zero rather than rendering a negative', () => {
    expect(formatDuration(-5)).toBe('0s');
    expect(formatDuration(Number.NaN)).toBe('0s');
  });
});

describe('formatMinutes', () => {
  it('drops the minutes on a whole hour', () => {
    expect(formatMinutes(45)).toBe('45m');
    expect(formatMinutes(120)).toBe('2h');
    expect(formatMinutes(150)).toBe('2h 30m');
  });

  it('renders the caller fallback for a missing value', () => {
    expect(formatMinutes(null)).toBe('');
    expect(formatMinutes(null, '—')).toBe('—');
  });
});

/**
 * The pin: one implementation, not twenty.
 *
 * An exact expected set rather than a ceiling. A `<=` comparison is what round
 * 1 of this spec measured going quietly blind — it stays green while the copies
 * it was meant to catch come back under a different name in a different file.
 */
describe('no second copy of the date coercion', () => {
  const SRC = resolve(__dirname, '..');

  function walk(dir: string): string[] {
    const out: string[] = [];
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) out.push(...walk(full));
      else if (/\.(svelte|ts)$/.test(name)) out.push(full);
    }
    return out;
  }

  const files = walk(SRC).filter((f) => !/\.test\.(ts|svelte)$/.test(f));

  it('appends the midnight suffix in exactly one file', () => {
    const hits = files
      .filter(
        (f) =>
          readFileSync(f, 'utf8').includes("T00:00:00'") ||
          readFileSync(f, 'utf8').includes('T00:00:00`'),
      )
      .map((f) => relative(SRC, f))
      .sort();
    expect(hits).toEqual(['lib/dateFormat.ts']);
  });

  it('splits seconds into days and hours in exactly one file', () => {
    // Narrower than a bare `86400`, deliberately: the four relative-time
    // ladders this module does not own each divide by it too, and a guard
    // matching those would have to carry an exemption list — which is the
    // shape round 1 measured going blind.
    const hits = files
      .filter((f) => readFileSync(f, 'utf8').includes('% 86400) / 3600'))
      .map((f) => relative(SRC, f))
      .sort();
    expect(hits).toEqual(['lib/dateFormat.ts']);
  });

  it('formats a fixed two-decimal figure in exactly one file', () => {
    // The literal `2`, not the option name: `/admin`'s currency formatter and
    // the two portfolio pages take a variable digit count off their own data
    // and are a different rule.
    const hits = files
      .filter((f) => readFileSync(f, 'utf8').includes('minimumFractionDigits: 2,'))
      .map((f) => relative(SRC, f))
      .sort();
    expect(hits).toEqual(['lib/format.ts']);
  });
});
