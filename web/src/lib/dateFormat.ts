/**
 * Date, date-time and duration rendering, in one place.
 *
 * Twenty-odd components each carried their own copy of these four or five
 * lines, and the copies had drifted: nine health and location pages appended
 * `T00:00:00` only when the string did not already carry a `T`, while three
 * money pages appended it unconditionally. Handed a full timestamp the second
 * shape builds `2026-09-05T14:22:31ZT00:00:00`, which is an Invalid Date — and
 * the `try/catch` every copy wrapped itself in cannot help, because `new Date`
 * of a nonsense string does not throw, it returns a value.
 *
 * The guard is what survived. Everything else that differed between the copies
 * — the `Intl` option set, what an empty input renders as — is a parameter, so
 * converting a call site changes nothing it renders.
 *
 * What is deliberately **not** here: the four relative-time ladders
 * (`ui/NotificationItem`, `/admin`, `/location`, `DeviceTrackerCard`). They
 * render four different things — floor versus round, four different threshold
 * sets, and one of them falls back to an absolute timestamp past a day — so
 * folding them is a change to what three of the four surfaces show rather than
 * an extraction. `NotificationItem`'s own comment already says so.
 */

/** `Intl` options plus what to render for a missing value. */
export type DateFormatOptions = Intl.DateTimeFormatOptions & {
  /** Rendered for `null`, `undefined` and `''`. Defaults to `''`. */
  empty?: string;
  /**
   * A fixed locale, where a surface has one. Almost nothing does — the two
   * feed components are the only callers, and they pin `en-US` because that is
   * what they rendered before this module existed. Everything else omits it and
   * gets the reader's own locale.
   */
  locale?: string;
};

const DEFAULT_DATE_OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
};

/**
 * A calendar date, from either a bare `YYYY-MM-DD` or a full timestamp.
 *
 * The midnight suffix is what makes a bare date render as *that* date rather
 * than as the day before in any timezone west of UTC — `new Date('2026-09-05')`
 * is parsed as UTC midnight, `new Date('2026-09-05T00:00:00')` as local
 * midnight. It is appended only when there is no time part to displace, which
 * is the whole of the guard.
 *
 * A value that will not parse is returned as it arrived, which is what every
 * copy of this did inside its `catch`: an unrenderable date should read as the
 * raw string a reader can report, never as `Invalid Date`.
 */
export function formatDate(iso: string | null | undefined, opts: DateFormatOptions = {}): string {
  const { empty = '', locale, ...intl } = opts;
  if (!iso) return empty;
  // A space-separated `YYYY-MM-DD HH:MM:SS` is SQLite's `datetime('now')`
  // default, which reaches the documents table. It has a time part and so
  // takes no suffix, but needs the separator normalised to parse at all.
  const hasTime = /\d{2}:\d{2}/.test(iso);
  const d = new Date(hasTime ? iso.replace(' ', 'T') : `${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(locale, Object.keys(intl).length > 0 ? intl : DEFAULT_DATE_OPTIONS);
}

/**
 * A date and a time together, for a value that is always a full timestamp.
 *
 * Separate from `formatDate` rather than an option on it, because it must not
 * carry the midnight suffix: a caller reaching for this has a timestamp, and
 * appending to one is the defect above.
 */
export function formatDateTime(
  iso: string | null | undefined,
  opts: DateFormatOptions = {},
): string {
  const { empty = '', locale, ...intl } = opts;
  if (!iso) return empty;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(locale, Object.keys(intl).length > 0 ? intl : undefined);
}

/**
 * A coarse duration in seconds: `6d 2h`, `1h 04m`, `12m`, `45s`.
 *
 * Two units at most. A reader is deciding whether to wait rather than timing
 * anything, so seconds of precision six hours out is noise.
 *
 * `doctor._duration` and `commands._usage_age` state the same rule in Python
 * and `usageFormat.parity.test.ts` holds the three in step. The minutes are
 * zero-padded for the same reason they are there: the field sits in a column
 * and `1h 4m` and `1h 04m` are different widths.
 */
export function formatDuration(seconds: number): string {
  const total = Math.floor(Math.max(0, Number.isFinite(seconds) ? seconds : 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
  if (minutes) return `${minutes}m`;
  return `${secs}s`;
}

/**
 * A duration already measured in whole minutes: `45m`, `2h`, `2h 30m`.
 *
 * A different rule from `formatDuration`, not a unit conversion of it — an
 * exact hour renders as `2h` rather than `2h 00m`, which is what both location
 * surfaces show for a visit that happens to land on the hour.
 */
export function formatMinutes(minutes: number | null | undefined, empty = ''): string {
  if (minutes == null || !Number.isFinite(minutes)) return empty;
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}
