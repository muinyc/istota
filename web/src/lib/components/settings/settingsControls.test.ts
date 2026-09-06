import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

/**
 * The settings surface goes through the `ui/` primitives, and does not
 * hand-roll a second copy of them.
 *
 * `ui/Field` and `ui/Input` were extracted *after* several settings surfaces
 * were written, and the pre-primitive hand-rolls outlived the extraction — so
 * `/money/settings` shipped four field treatments at three different heights,
 * with the Monarch login inputs standing ~8px taller than every other input on
 * the page. Each fork restated Field's declarations while dropping the two that
 * are not obvious: `min-height` off the tier channel, and the `line-height`
 * that has to be pinned beside `font: inherit` because the shorthand drags the
 * body's 1.5 leading in at a specificity nothing outside can correct.
 *
 * That is invisible at any one call site — a fork looks *more* deliberate than
 * a `<Field>`, and passes `lint:design` because it uses the right tokens.
 * Hence these tests rather than a note. Companion to controlTier.test.ts, which
 * holds the same invariant for the primitives themselves.
 */

const SRC = resolve(process.cwd(), 'src');

/**
 * Directories whose controls this holds, plus the two money cards that live
 * under `components/money/` but render only on `/money/settings`.
 */
const SURFACE_DIRS = [
  'lib/components/settings',
  'routes/settings',
  'routes/briefings/settings',
  'routes/feeds/settings',
  'routes/health/settings',
  'routes/location/settings',
  'routes/money/settings',
];

const SURFACE_FILES = [
  'lib/components/money/PortfolioAccountsCard.svelte',
  'lib/components/money/PortfolioClassificationsCard.svelte',
  'lib/components/money/TransactionRulesCard.svelte',
  'lib/components/money/TransactionRuleTestCard.svelte',
  'lib/components/money/TransactionRuleCoverageCard.svelte',
];

function svelteFiles(dir: string, out: string[] = []): string[] {
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) svelteFiles(path, out);
    else if (entry.name.endsWith('.svelte')) out.push(path);
  }
  return out;
}

const surface: string[] = [
  ...SURFACE_DIRS.flatMap((d) => svelteFiles(join(SRC, d))),
  ...SURFACE_FILES.map((f) => join(SRC, f)),
];

const rel = (file: string) => file.slice(SRC.length + 1);
const read = (file: string) => readFileSync(file, 'utf8');

const stripComments = (source: string) => source.replace(/\/\*[\s\S]*?\*\//g, '');

/** Markup — everything outside `<script>` and `<style>`. */
const markup = (source: string) =>
  source
    .replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/<style[\s\S]*?<\/style>/g, '')
    .replace(/<!--[\s\S]*?-->/g, '');

interface Rule {
  selector: string;
  body: string;
}

function rules(source: string): Rule[] {
  const out: Rule[] = [];
  for (const block of source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)) {
    for (const m of stripComments(block[1]).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      out.push({ selector: m[1].trim().replace(/\s+/g, ' '), body: m[2] });
    }
  }
  return out;
}

describe('settings surface', () => {
  it('finds the files it is supposed to scan', () => {
    // Without this the assertions below pass vacuously the moment a directory
    // is renamed — which is the failure mode of every path-driven scan.
    expect(surface.length).toBeGreaterThan(10);
    for (const f of SURFACE_FILES) expect(existsSync(join(SRC, f)), f).toBe(true);
  });
});

/**
 * A raw `<button>` that is a *control* — `IconButton` and `Button` exist and
 * carry the tier, the focus ring and the disabled state. Each exemption states
 * why the element is not a control.
 */
const RAW_BUTTON_OK = new Map([
  [
    'routes/briefings/settings/+page.svelte: block-name',
    'a table-row disclosure toggle, not a control: it wraps the row title and ' +
      'its chevron, and styling it as a button would make the row look clickable ' +
      'twice over',
  ],
]);

describe('settings controls go through the primitives', () => {
  it('renders no raw <button>', () => {
    const offenders: string[] = [];
    for (const file of surface) {
      for (const m of markup(read(file)).matchAll(/<button\b[\s\S]{0,200}?>/g)) {
        const cls = /class="([\w-]+)/.exec(m[0])?.[1] ?? '(no class)';
        if (RAW_BUTTON_OK.has(`${rel(file)}: ${cls}`)) continue;
        offenders.push(`${rel(file)}: <button class="${cls}">`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('keeps every raw-button exemption pointed at an element that still exists', () => {
    // An exemption outliving its markup silently blankets the next button that
    // happens to take the same class.
    const live = new Set<string>();
    for (const file of surface) {
      for (const m of markup(read(file)).matchAll(/<button\b[\s\S]{0,200}?>/g)) {
        const cls = /class="([\w-]+)/.exec(m[0])?.[1] ?? '(no class)';
        live.add(`${rel(file)}: ${cls}`);
      }
    }
    for (const key of RAW_BUTTON_OK.keys()) expect(live).toContain(key);
  });

  it('draws no field box of its own', () => {
    // A rule that gives a control a visible border is re-implementing
    // Input/Field — and, every time so far, dropping the min-height and the
    // leading pin with it. Two things are deliberately not caught:
    // `border-radius`/`border-color`, which adjust a box the primitive already
    // drew, and a *transparent* border, which reserves layout rather than
    // drawing anything (the feeds category table's borderless cell editor, an
    // affordance `Input` cannot express).
    const CONTROL = /(^|[\s,>+~(.])(input|textarea|select)(?![\w-])/;
    const DRAWS_BORDER = /(?:^|[;\s])border:\s*(?![^;}]*transparent)[^;}]*\d/;
    const offenders: string[] = [];
    for (const file of surface) {
      for (const r of rules(read(file))) {
        if (!CONTROL.test(r.selector) || !DRAWS_BORDER.test(r.body)) continue;
        offenders.push(`${rel(file)}: ${r.selector}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe('SecretField composes rather than forks', () => {
  const source = read(join(SRC, 'lib/components/settings/SecretField.svelte'));

  it('builds on Field, Input and IconButton', () => {
    // It used to restate Field's descendant rule declaration-for-declaration
    // minus the tier height and the leading pin, so on touch it computed
    // ~34.8px against a 32px tier — the exact defect those two lines were
    // added to fix, reproduced in the one component that renders on five
    // settings pages.
    for (const primitive of ['Field', 'Input', 'IconButton']) {
      expect(source, `SecretField should use ${primitive}`).toMatch(new RegExp(`<${primitive}\\b`));
    }
  });

  it('declares no control appearance of its own', () => {
    for (const r of rules(source)) {
      expect(r.body, r.selector).not.toMatch(/(?:^|[;\s])border:\s*[^;}]*\d/);
      expect(r.body, r.selector).not.toMatch(/font:\s*inherit/);
    }
  });
});
