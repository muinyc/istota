/**
 * Number rendering shared across the app: money and file sizes.
 *
 * Both were already written down once and re-implemented anyway.
 * `money/utils/accounts.formatAmount` was exported and six components declared
 * the same `toLocaleString` call under five different names without importing
 * it; `health/documents.formatBytes` was exported *and unit-tested*, and
 * `/admin` carried a second version that answers differently for the same
 * input — 1536 bytes reads `1.5 KB` there and `2 KB` here.
 *
 * The health copy is the survivor, on the rule this spec applies everywhere:
 * it is the stricter of the two. A negative byte count renders `—` rather than
 * `NaN undefined`, and a `NaN` renders `—` rather than `0 B`, both of which the
 * `/admin` version got wrong by not asking.
 *
 * Re-exported from `$lib/money/utils/accounts` and `$lib/health/documents` so
 * existing imports keep working; those are the module docs a reader looking for
 * either helper will reach first.
 */

/**
 * A bare decimal figure, two places, grouped for the reader's locale.
 *
 * No currency symbol and no sign handling: the callers put the `$` in the
 * template, and `formatAmount` below is the one that owns the sign.
 */
export function formatDecimal(value: number): string {
  return value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** A signed amount with its currency: `-1,234.56 USD`. */
export function formatAmount(value: number, currency: string): string {
  const sign = value < 0 ? '-' : '';
  return `${sign}${formatDecimal(Math.abs(value))} ${currency}`;
}

/**
 * Human file size. Binary units, one decimal above KB.
 *
 * Whole numbers at KB and from 10 upwards in every unit, because the extra
 * digit is noise at that magnitude: `180 KB`, `1.5 MB`, `12 MB`.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = value >= 10 || unit === 0 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${units[unit]}`;
}
