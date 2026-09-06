import { describe, it, expect } from 'vitest';
import { readCascade } from './cascade';

/**
 * The two caps on an inline image in a chat answer, and why the second one is
 * breakpointed while the first is not.
 *
 * `.md-image` is drawn only for the chat-files endpoint, so the source is a
 * file out of the reader's own workspace at whatever size the task wrote it —
 * commonly a 2000px screenshot. `max-width: 100%` keeps that inside the
 * transcript column, and on a wide image that is the whole job. On a tall or
 * square one it does nothing: a 2000x2000 image scaled to the column is as
 * tall as the column is wide, which on a desktop is most of the pane, so one
 * attachment pushes the turn's own text off the screen.
 *
 * `max-height: 50vh` is the other axis. It is safe to add only because the
 * base rule sets `height: auto` and no explicit `width` — the CSS2.1 §10.4
 * constraint table for replaced elements then takes whichever of the two
 * maxima binds harder and shrinks the other side with it, so the image keeps
 * its intrinsic ratio and needs no `object-fit`. Both halves of that premise
 * are asserted below, because losing either turns the cap into a squash.
 *
 * Non-mobile only. Below the app's 768px line the cap is inert at best — a
 * phone's transcript column is narrower than half its viewport height, so on a
 * square image `max-width` binds first and this never applies — and on a
 * landscape phone, where it would apply, 50vh is a few hundred pixels and
 * shrinks the image for nothing.
 *
 * These read the stylesheet rather than a layout, because jsdom does no
 * layout: they guard the rule against being removed or weakened, and are not a
 * proof that it renders. Same standing as markdownTable.test.ts.
 */

const appCss = readCascade();

/** The body of a top-level rule, by its full selector text. */
function ruleBody(css: string, selector: string): string | undefined {
  const at = css.indexOf(`\n${selector} {`);
  if (at === -1) return undefined;
  return css.slice(at, css.indexOf('}', at));
}

/**
 * Every `@media` block in the sheet as a `{ query, body }` pair. Brace-matched
 * rather than regexed: a media block holds whole rules, so it contains nested
 * braces and the first `}` after the header is the end of the rule inside it,
 * not the end of the block.
 */
function mediaBlocks(css: string): Array<{ query: string; body: string }> {
  const out: Array<{ query: string; body: string }> = [];
  for (const m of css.matchAll(/@media([^{]*)\{/g)) {
    const open = m.index! + m[0].length - 1;
    let depth = 0;
    for (let i = open; i < css.length; i++) {
      if (css[i] === '{') depth++;
      else if (css[i] === '}' && --depth === 0) {
        out.push({ query: m[1].trim(), body: css.slice(open + 1, i) });
        break;
      }
    }
  }
  return out;
}

/** The blocks that gate on a viewport at least `px` wide. */
const nonMobileBlocks = (px: number) =>
  mediaBlocks(appCss).filter((b) => b.query.includes(`min-width: ${px}px`));

describe('inline chat image', () => {
  it('has a rule at all', () => {
    expect(ruleBody(appCss, '.markdown .md-image')).toBeDefined();
  });

  /**
   * The premise of the height cap. If either of these goes, the cap starts
   * distorting the image instead of scaling it, and the reasoning above needs
   * revisiting rather than silently rotting.
   */
  it('leaves the browser free to preserve the aspect ratio', () => {
    const body = ruleBody(appCss, '.markdown .md-image') ?? '';
    expect(body).toMatch(/height:\s*auto/);
    // A fixed `width` would be the other half of an over-constrained box. Only
    // `max-width` is set, so the used width is still free to come down.
    expect(body).not.toMatch(/^\s*width:/m);
    expect(body).toMatch(/max-width:\s*100%/);
  });

  it('caps the height, so a square image cannot fill the pane', () => {
    const block = nonMobileBlocks(769).find((b) => b.body.includes('.md-image'));
    expect(block, 'the height cap must be in a non-mobile media block').toBeDefined();
    const match = block!.body.match(/max-height:\s*([\d.]+)vh/);
    expect(match, 'the cap is in vh, so it tracks the pane rather than the image').toBeTruthy();
    // The value is a judgement: half the viewport leaves the turn's own text on
    // screen beside the image while keeping the image large enough to read
    // without opening the lightbox. The bound asserted here is only that it is
    // a cap rather than a formality — above ~75vh a square image is back to
    // owning the pane, which is the bug.
    expect(Number(match![1])).toBeLessThanOrEqual(75);
  });

  /**
   * The cap must be reached ONLY through the breakpoint. In the unconditional
   * rule it would also apply on a phone, where it is inert at best and a
   * pointless shrink in landscape.
   */
  it('does not cap the height on mobile', () => {
    expect(ruleBody(appCss, '.markdown .md-image') ?? '').not.toMatch(/max-height/);
  });
});
