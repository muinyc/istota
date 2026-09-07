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

  it('leaves an RFC 822 feed date for the platform to parse', () => {
    // It has a space and a time part, so an unanchored `replace(' ', 'T')`
    // makes `Tue,T05 …` of it and loses a date the platform parses fine. Feed
    // entries carry this shape whenever feedparser could not normalise one.
    expect(formatDate('Tue, 05 Sep 2026 14:22:31 GMT', { locale: 'en-US' })).toBe('Sep 5, 2026');
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

  it('takes a space-separated SQLite timestamp, like formatDate does', () => {
    expect(formatDateTime('2026-09-05 14:22:31')).toBe(formatDateTime('2026-09-05T14:22:31'));
  });

  it('leaves an RFC 822 date alone, like formatDate does', () => {
    expect(formatDateTime('Tue, 05 Sep 2026 14:22:31 GMT')).not.toBe(
      'Tue, 05 Sep 2026 14:22:31 GMT',
    );
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

  // Read once, keyed by path: three guards over ~700 files is three reads of
  // each otherwise, and the first draft read every candidate twice within one
  // guard.
  const sources = new Map(
    walk(SRC)
      .filter((f) => !/\.test\.(ts|svelte)$/.test(f))
      .map((f) => [relative(SRC, f), readFileSync(f, 'utf8')] as const),
  );

  function filesMatching(pattern: RegExp): string[] {
    const hits: string[] = [];
    for (const [path, body] of sources) if (pattern.test(body)) hits.push(path);
    return hits.sort();
  }

  it('appends a midnight suffix in exactly one file', () => {
    // A pattern rather than two `includes` calls: `"T00:00:00"` in double
    // quotes and a bare `T00:00` are the same copy coming back, and a literal
    // match would report the tree clean.
    expect(filesMatching(/T00:00(:00)?['"`]/)).toEqual(['lib/dateFormat.ts']);
  });

  it('splits seconds into days and hours in exactly one file', () => {
    // Narrower than a bare `86400`, deliberately: the four relative-time
    // ladders this module does not own each divide by it too, and a guard
    // matching those would have to carry an exemption list — which is the
    // shape round 1 measured going blind. Whitespace is loose so a reformat
    // or a named `DAY` constant does not slip past.
    expect(filesMatching(/%\s*86400\s*\)?\s*\/\s*3600/)).toEqual(['lib/dateFormat.ts']);
  });

  it('formats a fixed two-decimal figure in exactly one file', () => {
    // The literal `2`, not the option name: `/admin`'s currency formatter and
    // the two portfolio pages take a variable digit count off their own data
    // and are a different rule.
    expect(filesMatching(/minimumFractionDigits:\s*2\b/)).toEqual(['lib/format.ts']);
  });

  /**
   * The guard the other three could not be.
   *
   * Each of those greps a fragment of one *implementation* — the midnight
   * suffix, the day/hour split, the fixed decimals — so a copy written
   * differently is invisible to all three. That is not hypothetical: this
   * stage's first pass converted twenty-eight call sites and left
   * `briefings/settings`' `fmtLastRun` and four inline dates in `health/stats`
   * behind, and all three guards were green with them in the tree. Naming the
   * platform call instead catches any spelling.
   *
   * The exemption list is the whole cost, and it is explicit and short by
   * design. A file goes on it with a reason or it gets converted; a wildcard
   * or a `<=` comparison here would put the guard straight back where it was.
   */
  it('calls the platform date formatters only where this module says it may', () => {
    const EXEMPT: Record<string, string> = {
      'lib/dateFormat.ts': 'the implementation',
      'lib/format.ts': 'formatDecimal, its number-side sibling',
      'lib/usageFormat.ts': 'formatNumber, which is a number rule and not a date one',
      'lib/components/location/DeviceTrackerCard.svelte':
        'the relative ladder, whose fallback past a day is an absolute timestamp',
      'routes/chat/+page.svelte':
        'dayLabel: Today / Yesterday / weekday / date, a relative rule of its own',
      'routes/money/portfolio/history/+page.svelte': 'a variable-digit money figure',
      'routes/money/portfolio/overview/+page.svelte': 'a variable-digit money figure',
      'routes/money/transactions/+page.svelte': 'a row count, grouped for the reader',
    };
    const hits = filesMatching(/\.toLocaleDateString\(|\.toLocaleString\(/);
    expect(hits.filter((f) => !(f in EXEMPT))).toEqual([]);
    // And the list does not outlive what it exempts.
    expect(Object.keys(EXEMPT).filter((f) => !hits.includes(f))).toEqual([]);
  });
});
