import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { readLayer } from './cascade';
import { ROOM_COLORS } from '../roomColors';
import { describe, expect, it } from 'vitest';

// app.css is the token home, so the invariants that make the tokens usable have
// to be asserted here rather than inferred from the pages that consume them.
// Read from the project root: vitest serves this file over a vite-style URL, so
// import.meta.url is not a file: URL here.

// The token layer specifically: this file asks what :root declares, and the
// other layers declare no tokens.
const APP_CSS = readLayer('tokens');

/** The declarations inside one top-level block, by selector. */
function block(selector: string): Record<string, string> {
  const start = APP_CSS.indexOf(`${selector} {`);
  if (start === -1) throw new Error(`no ${selector} block in app.css`);
  const end = APP_CSS.indexOf('\n}', start);
  const body = APP_CSS.slice(start, end);
  const out: Record<string, string> = {};
  for (const [, name, value] of body.matchAll(/^\s*(--[\w-]+)\s*:\s*([^;]+);/gm)) {
    out[name] = value.trim();
  }
  return out;
}

const dark = block(':root');
const light = block(":root[data-theme='light']");

describe('theme parity', () => {
  // The anti-pattern AGENTS.md names: a color defined once, in dark, then
  // rendered as a dark fill on white. A color token without a light value is
  // that bug waiting to be noticed.
  const THEME_INVARIANT = new Set([
    // Deliberately one value in both themes: these sit on a surface the theme
    // does not control, so flipping them would put dark text on a dark scrim.
    '--on-accent-fg',
    '--on-scrim-fg',
    '--scrim-bg',
    // A solid amber fill sets its own text color, so it needs none of the
    // darkening --accent-amber gets so amber *text* passes on white.
    '--accent-amber-fill',
    '--accent-amber-fill-hover',
    '--accent-amber-fill-fg',
    '--status-dot-ok',
    '--status-dot-bad',
    '--status-dot-warn',
    '--status-dot-info',
    '--status-critical-fg',
  ]);

  const COLOR_PREFIXES = [
    '--status-',
    '--surface-',
    '--text-',
    '--border-',
    '--accent',
    '--money-',
    // The room-colour palette (ISSUE-433). Without this prefix the parity
    // check above simply does not see these tokens, which is the quiet failure
    // this file exists to prevent — a categorical palette is exactly the kind
    // of token somebody defines in dark and never gives a light value.
    '--room-color-',
  ];
  const isColorToken = (name: string) =>
    COLOR_PREFIXES.some((p) => name.startsWith(p)) &&
    // The --text-* scale is half type sizes and half colors; sizes are rem.
    !/^\d|rem|px|%/.test(dark[name] ?? '');

  /**
   * A `filter` that corrects an asset the theme does not control — the octopus
   * sigil, the browser's calendar glyph. Not a colour by prefix, but it fails
   * exactly the same way: define it in dark only and the light theme renders
   * an inverted mark it never asked for.
   *
   * The prefix list missed both of these. `--sigil-filter` had carried the gap
   * since it was introduced; `--calendar-icon-filter` would have inherited it.
   */
  const isThemeFilter = (name: string) => name.endsWith('-filter');

  const needsBothThemes = (name: string) => isColorToken(name) || isThemeFilter(name);

  it('every color token has a light-theme value', () => {
    const missing = Object.keys(dark).filter(
      (name) => needsBothThemes(name) && !THEME_INVARIANT.has(name) && !(name in light),
    );
    expect(missing).toEqual([]);
  });

  it('covers the theme-correcting filters, which are not colors by name', () => {
    // Guards the guard: if the prefix list or the suffix test stops matching
    // these, the parity check above goes quiet rather than failing, which is
    // the failure mode this whole file exists to prevent.
    const filters = Object.keys(dark).filter(isThemeFilter);
    expect(filters).toContain('--sigil-filter');
    expect(filters).toContain('--calendar-icon-filter');
    for (const name of filters) expect(light, `${name} needs a light value`).toHaveProperty(name);
  });

  it('the light theme defines no token the dark theme lacks', () => {
    // A light-only token renders as nothing in dark, the same bug mirrored.
    expect(Object.keys(light).filter((name) => !(name in dark))).toEqual([]);
  });

  it('covers the room-colour palette, which is what the prefix was added for', () => {
    // Guards the guard, the way the filter test above does: the parity check
    // is a filter over a prefix list, so a palette the list stops matching
    // goes quiet rather than failing. ROOM_COLORS is the palette itself, so
    // this also catches a name added there with no token behind it.
    const names = Object.keys(dark).filter((n) => n.startsWith('--room-color-'));
    expect(names.length).toBe(ROOM_COLORS.length);
    for (const c of ROOM_COLORS) {
      expect(dark, `--room-color-${c} needs a dark value`).toHaveProperty(`--room-color-${c}`);
      expect(light, `--room-color-${c} needs a light value`).toHaveProperty(`--room-color-${c}`);
    }
  });

  it('gives each room colour a distinct value within a theme', () => {
    // Two names resolving to one hue is a picker offering the same swatch
    // twice, which reads as a rendering bug rather than a palette mistake.
    for (const [label, blockValues] of [
      ['dark', dark],
      ['light', light],
    ] as const) {
      const values = ROOM_COLORS.map((c) => blockValues[`--room-color-${c}`]);
      expect(new Set(values).size, `${label} has a duplicate room colour`).toBe(values.length);
    }
  });
});

describe('control heights', () => {
  // Button, Chip and the Select trigger each computed their own box out of a
  // font size, a padding and (for Select) a border, and landed on four
  // different answers — so a Select beside a Button sat ~2.6px short, by an
  // amount that changed with the text-scale preference, since everything but
  // the border is in rem.
  it('defines both steps', () => {
    expect(dark['--control-height-sm']).toBeDefined();
    expect(dark['--control-height-md']).toBeDefined();
  });

  it('orders sm below md', () => {
    const rem = (v: string) => Number(v.replace('rem', ''));
    expect(rem(dark['--control-height-sm'])).toBeLessThan(rem(dark['--control-height-md']));
  });

  it('is in rem, so a control tracks the text-scale preference', () => {
    // px here would reintroduce the original bug in the other direction: the
    // box would stay put while the type inside it grew.
    expect(dark['--control-height-sm']).toMatch(/rem$/);
    expect(dark['--control-height-md']).toMatch(/rem$/);
  });
});

describe('z-index scale', () => {
  const z = (name: string) => {
    const raw = dark[name];
    expect(raw, `${name} is not defined`).toBeDefined();
    return Number(raw);
  };

  it('is strictly ordered bottom to top', () => {
    const ladder = [
      '--z-sticky',
      '--z-drawer-backdrop',
      '--z-drawer',
      '--z-notice',
      '--z-modal',
      '--z-modal-panel',
      '--z-viewer',
      '--z-viewer-control',
      '--z-lightbox',
      '--z-popover',
      '--z-toast',
    ];
    const values = ladder.map(z);
    expect(values).toEqual([...values].sort((a, b) => a - b));
    expect(new Set(values).size).toBe(values.length);
  });

  it('puts a popover above a dialog panel', () => {
    // Not theoretical: every money form is a Select inside a Modal, and both
    // portal to <body>, so the two values compete in the root stacking context.
    expect(z('--z-popover')).toBeGreaterThan(z('--z-modal-panel'));
  });

  it('puts the lightbox above the reader it opens from', () => {
    expect(z('--z-lightbox')).toBeGreaterThan(z('--z-viewer-control'));
  });
});
