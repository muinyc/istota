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

/**
 * `Intl` options plus what to render for a missing value.
 *
 * The `Intl` half **replaces** the module default rather than merging with it,
 * so a caller asking for `{ month: '2-digit' }` gets month and nothing else.
 * Merging would make the default un-overridable — two call sites want a date
 * with no year — but the corollary is that a caller wanting only a
 * non-component option (`timeZone`, `hour12`) has to restate the components
 * beside it.
 */
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

/** A bare calendar date: `2026-09-05`. */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/** SQLite's `datetime('now')` default: `2026-09-05 14:22:31`. */
const SQLITE_TIMESTAMP = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/;

/**
 * The `T` separator ISO wants, where a value came out of SQLite without one.
 *
 * `datetime('now')` is the schema DEFAULT on several `created_at` columns and
 * it separates with a space. V8 accepts that; the standard does not, and the
 * other engines have not always.
 *
 * **The pattern is anchored, and that is the whole of the correctness here.** A
 * loose "does it contain a time part" test is also true of an RFC 822 date —
 * `Tue, 05 Sep 2026 14:22:31 GMT`, which is what a feed entry carries when
 * feedparser could not normalise it — and replacing the first space of one
 * produces `Tue,T05 …`, an Invalid Date out of a string `new Date` parses
 * correctly untouched.
 */
function separator(iso: string): string {
  return SQLITE_TIMESTAMP.test(iso) ? iso.replace(' ', 'T') : iso;
}

/** What `formatDate` hands `new Date`, for the two shapes it gets wrong. */
function coerce(iso: string): string {
  // A bare date is parsed as UTC midnight, so it renders as the day before
  // anywhere west of Greenwich; the suffix makes it local midnight instead.
  if (DATE_ONLY.test(iso)) return `${iso}T00:00:00`;
  return separator(iso);
}

/**
 * A calendar date, from either a bare `YYYY-MM-DD` or a full timestamp.
 *
 * `coerce` above decides what the platform is handed; nothing else here does.
 *
 * A value that will not parse is returned as it arrived, which is what every
 * copy of this did inside its `catch`: an unrenderable date should read as the
 * raw string a reader can report, never as `Invalid Date`.
 */
export function formatDate(iso: string | null | undefined, opts: DateFormatOptions = {}): string {
  const { empty = '', locale, ...intl } = opts;
  if (!iso) return empty;
  const d = new Date(coerce(iso));
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(locale, Object.keys(intl).length > 0 ? intl : DEFAULT_DATE_OPTIONS);
}

/**
 * A date and a time together, for a value that is always a full timestamp.
 *
 * Separate from `formatDate` rather than an option on it, because it must not
 * carry the midnight suffix: a caller reaching for this has a timestamp, and
 * appending to one is the defect above. It shares the separator rule, which
 * only ever normalises a value that is already a timestamp — four of these
 * callers render a `created_at`-shaped column and the four local copies this
 * replaced each parsed one on V8's tolerance alone.
 */
export function formatDateTime(
  iso: string | null | undefined,
  opts: DateFormatOptions = {},
): string {
  const { empty = '', locale, ...intl } = opts;
  if (!iso) return empty;
  const d = new Date(separator(iso));
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
