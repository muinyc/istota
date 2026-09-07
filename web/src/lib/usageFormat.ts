/**
 * Rendering rules for token/cost usage.
 *
 * Extracted from the admin page rather than left inline because this is where
 * the cost decision actually lands: everything upstream stores `cost_usd` and
 * `cost_basis` on every row, and the surface is what decides whether a currency
 * figure appears. A dashboard that quietly invents an invoice is worse than one
 * that declines to guess, so the rule is worth testing directly.
 *
 * Python applies the same rule in `usage_render.render_cost`, which the CLI and
 * the `!usage` chat command both import. The two are deliberately separate
 * implementations of one stated rule rather than a shared artifact — they
 * render into different media — but they must not disagree about *when* a
 * dollar sign appears, or about what follows it.
 */

import type { AdminStatsUser } from '$lib/api';
import { formatDuration } from '$lib/dateFormat';

export const COST_PLACEHOLDER = '—';

export function formatNumber(n: number): string {
  return n.toLocaleString();
}

/**
 * One rule: no currency unless it is money.
 *
 * A group spanning bases shows its `api` total and nothing else. Nothing is
 * summed across bases — an operator switching the CLI's auth mid-window has
 * rows of both kinds, and adding a plan-equivalent to real spend is the misread
 * this design refuses.
 *
 * The other bases are not named. A `+estimated+subscription+unknown` suffix
 * used to follow the figure, and it was wrong on two counts: it overflowed a
 * fixed-width column into the one beside it, and it asked the reader to act on
 * a distinction they have no way to act on. What a cost column is for is the
 * money; the rest of the breakdown is still on the wire in `cost_by_basis` for
 * anything that wants it.
 *
 * With no `api` rows at all there is no money to report, so the placeholder
 * stands alone — which on a subscription deployment is most of the time.
 */
export function formatCost(byBasis: Record<string, number> | undefined): string {
  const real = (byBasis ?? {})['api'];
  if (real !== undefined) return `$${formatMoney(real)}`;
  return COST_PLACEHOLDER;
}

/**
 * Two decimals, or four when that would round a real figure to nothing.
 *
 * A 24h per-user figure is routinely sub-cent, and at two decimals it renders
 * `$0.00` — indistinguishable from a genuine zero, which is the one thing a
 * cost column must not be ambiguous about. `usage_render.fmt_money` states the
 * same rule; the two are separate implementations of one rule and must not
 * disagree.
 */
function formatMoney(value: number): string {
  if (value !== 0 && Math.abs(value) < 0.01) return value.toFixed(4);
  return value.toFixed(2);
}

/** Null, never zero, when unmeasured — a zero would be a measurement. */
export function formatContext(n: number | null | undefined): string {
  return n === null || n === undefined ? COST_PLACEHOLDER : formatNumber(Math.round(n));
}

export function formatPercent(n: number | null | undefined): string {
  return n === null || n === undefined ? COST_PLACEHOLDER : `${(n * 100).toFixed(1)}%`;
}

/**
 * A plan window's utilization, as a whole number where it is one.
 *
 * Distinct from `formatPercent` above, which takes a 0–1 rate and always shows a
 * decimal: this takes the 0–100 figure the usage endpoint reports, where a
 * tenth of a percent of a weekly quota is noise on a glanceable tile but `40.5`
 * still has to render as itself rather than as `41`.
 *
 * Clamped by default rather than trusted. The server clamps a plan window too,
 * but this figure comes off an external endpoint and is written into a tile that
 * is also *tinted* by it — a `-5` would read as healthy green and a `140` would
 * be a red tile claiming more than a full quota. Anything that is not a real
 * number is the placeholder, never a zero: a fabricated 0% on an exhausted plan
 * is the worst error here.
 *
 * **`clamp: false` for pay-as-you-go credits, and only those.** A plan window's
 * ceiling is the plan, so above 100 is nonsense there; a spend percentage above
 * 100 is real money already committed, and `subscription_usage._unclamped_percent`
 * exists on the Python side to keep it — so clamping it back here would hide an
 * overage on the one figure in this card that is not a token count.
 *
 * No Python counterpart is pinned to this. Two render the same number and both
 * differ deliberately: `doctor._usage_window` uses `:g` (`40.55` where this
 * gives `40.6`) and `commands.cmd_usage` uses `:.0f` for a compact chat line
 * (`40%` where this gives `40.5%`). Each is sized for its own medium, and the
 * spec says these helpers need no parity test for exactly that reason.
 */
export function formatUtilization(
  percent: number | null | undefined,
  options: { clamp?: boolean } = {},
): string {
  if (percent === null || percent === undefined || !Number.isFinite(percent)) {
    return COST_PLACEHOLDER;
  }
  const clamp = options.clamp ?? true;
  const bounded = clamp ? Math.min(100, Math.max(0, percent)) : Math.max(0, percent);
  // `Number()` drops a trailing `.0`, so 40 renders as `40%` and 40.5 as
  // `40.5%` — one rule rather than a branch on whether the value is an integer.
  return `${Number(bounded.toFixed(1))}%`;
}

/**
 * When a plan window resets, as the tile's sub-line.
 *
 * `dateFormat.formatDuration` is the rule — two units at most, matching
 * `doctor._duration` — and this adds only the sentence around it. The `<= 0`
 * branch is deliberately not in that helper: `_duration(0)` is `0s`, which is
 * the honest answer to "how long is this" and the wrong one to "when does it
 * reset".
 *
 * `null` is a window with no scheduled reset (or one whose timestamp the parser
 * could not read), and it says so rather than rendering an empty line: a tile
 * showing a percentage and nothing under it reads as a missing value.
 */
export function formatResetIn(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return 'no reset scheduled';
  }
  if (seconds <= 0) return 'resetting now';
  return `resets in ${formatDuration(seconds)}`;
}

/**
 * The breakdown that explains a user's usage row count.
 *
 * `usage_rows_24h` exceeding `tasks_last_24h` is by design — the column
 * includes spend with no task row at all (a nightly sleep cycle, health OCR) —
 * so the breakdown rides the same cell as a tooltip. Without it the row reads as
 * an arithmetic error.
 */
export function usageOriginTitle(u: AdminStatsUser): string {
  const parts = Object.entries(u.usage_by_origin_24h ?? {})
    .sort((a, b) => b[1].tokens - a[1].tokens)
    .map(([origin, v]) => `${origin}: ${formatNumber(v.tokens)}`);
  const head = `${u.usage_rows_24h} usage row(s)`;
  const unmeasured =
    u.usage_unmeasured_24h > 0 ? ` · ${u.usage_unmeasured_24h} unmeasured task(s)` : '';
  return parts.length > 0 ? `${head}${unmeasured} — ${parts.join(', ')}` : `${head}${unmeasured}`;
}
