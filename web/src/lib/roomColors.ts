/**
 * The room-colour palette (ISSUE-433) — names only.
 *
 * Deliberately *not* the shape of `location-constants.ts`, which holds hex.
 * That file has to, because MapLibre paints in WebGL and cannot resolve
 * `var(--token)`; nothing here has that problem, so `styles/tokens.css` stays
 * the single source of the colour values and this file carries the names that
 * index into it. A hex here would be a second definition of every colour, and
 * the one that could not be themed.
 *
 * The hue encodes a *kind* — which room this is — not a severity, which is the
 * carve-out `web/AGENTS.md` makes for a categorical palette. The amber band is
 * skipped: `.room-origin.talk` already spends `--accent-amber` in this same row
 * to mean "mirrored to Nextcloud Talk", and a tint landing there would read as
 * a surface binding rather than a user's choice.
 *
 * `src/istota/room_colors.py` is the same list for the server's validation and
 * `tests/test_room_colors.py` holds the two — and the tokens — in step.
 */
export const ROOM_COLORS = [
  'rose',
  'coral',
  'citron',
  'green',
  'teal',
  'sky',
  'indigo',
  'plum',
] as const;

export type RoomColor = (typeof ROOM_COLORS)[number];

/** Human label for a swatch's tooltip and its accessible name. */
export const ROOM_COLOR_LABELS: Record<RoomColor, string> = {
  rose: 'Rose',
  coral: 'Coral',
  citron: 'Citron',
  green: 'Green',
  teal: 'Teal',
  sky: 'Sky',
  indigo: 'Indigo',
  plum: 'Plum',
};

/**
 * The CSS custom property carrying `name`'s value, or null when the name is not
 * in the palette.
 *
 * Returning null rather than building the property string unconditionally is
 * what keeps a stale value out of the DOM: a room tinted with a colour a later
 * build removed would otherwise interpolate into `var(--room-color-mauve)`,
 * which resolves to nothing and paints an invisible tint the settings modal
 * still shows as unset. The caller drops the tint instead.
 */
export function roomColorVar(name: string | null | undefined): string | null {
  if (!name) return null;
  // design-lint-allow: categorical palette, one token per ROOM_COLORS entry.
  // The name is interpolated, so the rule cannot resolve it statically — but
  // the membership test on the line is what bounds it to the eight tokens
  // defined in both theme blocks, and tokens.test.ts asserts that pairing.
  return (ROOM_COLORS as readonly string[]).includes(name) ? `var(--room-color-${name})` : null;
}
