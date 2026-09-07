import { describe, it, expect, vi } from 'vitest';

// The deployment's real base path. `vitest-stubs/app-paths.ts` answers `''`,
// which would make every prefix assertion below pass against a renderer that
// had dropped the base from the prefix entirely — the empty string is a prefix
// of everything. Naming the production value is what gives the negative cases
// (`/istota/api/rooms`) something to be refused for. Same shape as
// `lib/offline/clear.test.ts`.
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));

import { renderMarkdown } from './index';
import { chatFileUrl } from '$lib/api';

describe('renderMarkdown syntax highlighting', () => {
  it('emits hljs token spans for a fenced block with a known language', () => {
    const html = renderMarkdown('```python\ndef f():\n    return 1\n```');
    // The <code> carries the hljs class so the theme palette applies.
    expect(html).toContain('class="hljs language-python"');
    // Keywords are wrapped in token spans by highlight.js.
    expect(html).toContain('hljs-keyword');
  });

  it('still renders an unknown language as an escaped plain code block', () => {
    const html = renderMarkdown('```nosuchlang\n<script>x</script>\n```');
    expect(html).toContain('class="hljs language-nosuchlang"');
    // Raw HTML in the code body must be escaped, not passed through.
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<script>x');
  });

  it('renders a bare fenced block (no language) without crashing', () => {
    const html = renderMarkdown('```\nplain text\n```');
    expect(html).toContain('<pre>');
    expect(html).toContain('class="hljs"');
    expect(html).toContain('plain text');
  });

  it('leaves inline code as a plain <code> (no hljs tokens)', () => {
    const html = renderMarkdown('use `print()` here');
    expect(html).toContain('<code>print()</code>');
    expect(html).not.toContain('hljs');
  });
});

describe('file handover links', () => {
  // Web chat has no outbound attachment channel, so a task hands a file over
  // as a link to the authenticated download endpoint. If the sanitizer drops
  // that href the whole handover silently degrades to unclickable text.
  const url = '/istota/api/chat/files?path=%2FUsers%2Falice%2Fistota%2Freport.csv';

  it('keeps the relative download URL', () => {
    const html = renderMarkdown(`[report.csv](${url})`);
    expect(html).toContain('href="/istota/api/chat/files?path=');
    expect(html).toContain('report.csv');
  });

  it('preserves percent-encoding in the path query', () => {
    const html = renderMarkdown(`[Q3 report.csv](${url.replace('report', 'Q3%20report')})`);
    expect(html).toContain('Q3%20report.csv');
  });

  it('opens in a new tab with noopener', () => {
    const html = renderMarkdown(`[f](${url})`);
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('still refuses a javascript: href', () => {
    // Left as inert text rather than an anchor — the scheme survives in the
    // body, which is harmless; what matters is that no href is emitted.
    const html = renderMarkdown('[x](javascript:alert(1))');
    expect(html).not.toContain('href');
    expect(html).not.toContain('<a ');
  });
});

describe('inline images', () => {
  // ISSUE-431. An image is a string in the assistant's body — nothing on the
  // write path knows one is in there — so the renderer is the only thing
  // deciding which <img> the transcript draws. The rule is one prefix: our own
  // chat-files endpoint, which since Stage 1 serves a raster inline only after
  // sniffing the file's own magic numbers.
  const png = '/istota/api/chat/files?path=%2FUsers%2Fu%2Fx.png';

  it('draws an <img> for the chat-files endpoint', () => {
    const html = renderMarkdown(`![Doppler radar](${png})`);
    expect(html).toContain('<img');
    expect(html).toContain('class="md-image"');
    expect(html).toContain(`src="${png}"`);
    expect(html).toContain('alt="Doppler radar"');
    // Off the critical path: a transcript can hold many of these, and the
    // stylesheet gives them no height until they load.
    expect(html).toContain('loading="lazy"');
    expect(html).toContain('decoding="async"');
  });

  it('makes the image a keyboard-reachable lightbox trigger', () => {
    // The output goes through `{@html}`, so there is no element to wrap in a
    // real <button>. Message.svelte delegates click and Enter/Space off these.
    const html = renderMarkdown(`![radar](${png})`);
    expect(html).toContain('role="button"');
    expect(html).toContain('tabindex="0"');
  });

  it('preserves percent-encoding in the src', () => {
    const spaced = png.replace('x.png', 'Q3%20radar.png');
    const html = renderMarkdown(`![radar](${spaced})`);
    expect(html).toContain('Q3%20radar.png');
  });

  it('forces a non-empty alt', () => {
    // A broken image with an empty alt is a blank space with nothing to say
    // what was lost.
    const html = renderMarkdown(`![](${png})`);
    expect(html).toContain('<img');
    expect(html).toContain('alt="image"');
  });

  it('escapes an alt that carries markup', () => {
    const html = renderMarkdown(`![a" onerror="alert(1)](${png})`);
    expect(html).not.toContain('onerror="');
    expect(html).toContain('&quot;');
  });

  it('degrades a remote image to a link rather than fetching it', () => {
    // Rendering this inline fetches from a host the model chose, with the
    // reader's IP and referer, before the reader agreed to anything.
    const html = renderMarkdown('![a chart](https://evil.example/x.png)');
    expect(html).not.toContain('<img');
    expect(html).toContain('<a href="https://evil.example/x.png"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('a chart');
  });

  it('labels a degraded link with its URL when the alt is empty', () => {
    // An anchor with no text is invisible, which is indistinguishable from the
    // silent drop this path exists to avoid.
    const html = renderMarkdown('![](https://evil.example/x.png)');
    expect(html).not.toContain('<img');
    expect(html).toContain('>https://evil.example/x.png</a>');
  });

  it('refuses a root-relative URL that is not the chat-files endpoint', () => {
    // Root-relative is what `validateLink` admits; it is not the rule here.
    const html = renderMarkdown('![rooms](/istota/api/rooms)');
    expect(html).not.toContain('<img');
    expect(html).toContain('<a href="/istota/api/rooms"');
  });

  it('refuses a chat-files path under a different base', () => {
    // The base is part of the prefix. A path that merely looks like ours is a
    // route on somebody else's deployment, or nothing at all.
    const html = renderMarkdown('![x](/api/chat/files?path=%2FUsers%2Fu%2Fx.png)');
    expect(html).not.toContain('<img');
    expect(html).toContain('<a href="/api/chat/files?path=');
  });

  it('still refuses a javascript: image, as a link would be', () => {
    // markdown-it abandons the image token when `validateLink` refuses, so this
    // never reaches the rule at all — the source stays literal text.
    const html = renderMarkdown('![x](javascript:alert(1))');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('href');
  });

  it('refuses a data: image', () => {
    const html = renderMarkdown('![x](data:image/png;base64,iVBORw0KGgo=)');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('href');
  });

  it('admits the URL chatFileUrl actually builds', () => {
    // The prefix here and `chatFileUrl` in api.ts are two independent spellings
    // sharing only `base`. Drift between them is silent — a prefix that stops
    // matching degrades every image to a link, which is also the deliberate
    // behaviour for a foreign src, so no other test and no error tells the two
    // apart. This one renders the real builder's own output.
    const html = renderMarkdown(`![radar](${chatFileUrl('/Users/u/istota/radar.png')})`);
    expect(html).toContain('<img');
    expect(html).toContain('class="md-image"');
  });

  it('renders an empty destination as its alt text, not an empty link', () => {
    // `![alt]()` is the one src `validateLink` refuses that still yields a
    // token, with src=''. An <a href=""> navigates to a second copy of the
    // current page, which is worse than the broken <img> this replaced.
    const html = renderMarkdown('![alt]()');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<a ');
    expect(html).not.toContain('href');
    expect(html).toContain('alt');
  });

  it('carries a title through on both branches', () => {
    // markdown-it's default rule emits every attr, so dropping `title` would be
    // a silent behaviour change.
    expect(renderMarkdown(`![radar](${png} "Taken at 14:02")`)).toContain('title="Taken at 14:02"');
    expect(renderMarkdown('![c](https://evil.example/x.png "remote")')).toContain('title="remote"');
  });

  it('escapes a title that carries markup', () => {
    const html = renderMarkdown(`![radar](${png} "a\\" onerror=\\"alert(1)")`);
    expect(html).not.toContain('onerror="');
    expect(html).toContain('&quot;');
  });
});

describe('a dimension hint on an admitted image', () => {
  // The transcript reflows as images decode: the stylesheet gives one no height
  // until its bytes arrive, so the text after it is shoved down the page while
  // it is being read. `width` + `height` with `height: auto` reserves the final
  // box at parse time. The hint rides in the fragment, which is never sent to
  // the server and cannot collide with the endpoint's own `path` parameter.
  const png = '/istota/api/chat/files?path=%2FUsers%2Fu%2Fx.png';

  it('emits width and height from the fragment', () => {
    const html = renderMarkdown(`![radar](${png}#w=1439&h=812)`);
    expect(html).toContain('width="1439"');
    expect(html).toContain('height="812"');
  });

  it('still admits the image, hint and all', () => {
    // The prefix check reads the head of the src, so a fragment must not push
    // an admitted image onto the degrade-to-a-link branch.
    const html = renderMarkdown(`![radar](${png}#w=800&h=600)`);
    expect(html).toContain('<img');
    expect(html).toContain('class="md-image"');
    expect(html).toContain(
      'src="/istota/api/chat/files?path=%2FUsers%2Fu%2Fx.png#w=800&amp;h=600"',
    );
  });

  it('emits neither attribute when there is no hint', () => {
    const html = renderMarkdown(`![radar](${png})`);
    expect(html).not.toContain('width=');
    expect(html).not.toContain('height=');
  });

  /**
   * The ratio is not a duplicate of the two attributes — it is what lets the
   * stylesheet state the 50vh cap as a bound on WIDTH. As a `max-height` the
   * cap clamps the height of an element whose width the attributes have made
   * specified, and the width does not follow: the picture stretches, measured
   * at x1.23 on a 4:3 capture and x9.68 on a tall one with correct hints.
   */
  it('emits the ratio the width cap is derived from', () => {
    const html = renderMarkdown(`![radar](${png}#w=1439&h=812)`);
    expect(html).toContain('style="--md-img-ratio:1.772167"');
  });

  it('emits a ratio a tall image can be capped by', () => {
    // 0.2 rather than something that rounds to zero, which would be a
    // `max-width: 0` and an invisible image.
    const html = renderMarkdown(`![tall](${png}#w=800&h=4000)`);
    expect(html).toContain('style="--md-img-ratio:0.200000"');
  });

  it('emits no ratio without a usable hint, so the cap stays inert', () => {
    for (const frag of ['', '#w=1439', '#w=0&h=812']) {
      expect(renderMarkdown(`![radar](${png}${frag})`)).not.toContain('--md-img-ratio');
    }
  });

  it('keeps the ratio to digits and a dot even at the extremes', () => {
    // The value goes into a `style` attribute in an `{@html}` sink. Both axes
    // are already validated integers, so this pins that the quotient of the
    // most extreme admissible pair is still inert text.
    const html = renderMarkdown(`![x](${png}#w=1&h=20000)`);
    const ratio = html.match(/--md-img-ratio:([^"]*)"/)?.[1] ?? '';
    expect(ratio).toMatch(/^[0-9]+\.[0-9]+$/);
    expect(Number(ratio)).toBeGreaterThan(0);
  });

  it('ignores a hint carrying only one axis', () => {
    // One alone gives no ratio, so it reserves nothing and the box still jumps.
    for (const frag of ['#w=1439', '#h=812', '#w=1439&h=', '#h=812&w=']) {
      const html = renderMarkdown(`![radar](${png}${frag})`);
      expect(html).toContain('<img');
      expect(html).not.toContain('width=');
      expect(html).not.toContain('height=');
    }
  });

  it('ignores anything that is not a plain positive integer', () => {
    // Model-authored text landing in a `{@html}` sink: only digits are parsed,
    // and what is emitted is the parsed number rather than the source string.
    const bad = ['#w=1e3&h=812', '#w=-10&h=812', '#w=12.5&h=812', '#w=0&h=812', '#w=99999&h=812'];
    for (const frag of bad) {
      const html = renderMarkdown(`![radar](${png}${frag})`);
      expect(html).toContain('<img');
      expect(html).not.toContain('width=');
      expect(html).not.toContain('height=');
    }
  });

  it('cannot smuggle an attribute through the hint', () => {
    // Percent-encoded, because a raw space would end the link destination and
    // markdown-it would never build an image token at all.
    const html = renderMarkdown(`![radar](${png}#w=1%20onload%3Dalert(1)&h=2)`);
    expect(html).not.toContain('onload=');
    expect(html).not.toContain('width=');
  });

  it('leaves a degraded remote image a plain link', () => {
    // The hint is only ever read for an src we admit; a foreign one keeps
    // degrading to an anchor, fragment and all.
    const html = renderMarkdown('![c](https://evil.example/x.png#w=100&h=100)');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('width=');
    expect(html).toContain('<a href="https://evil.example/x.png#w=100&amp;h=100"');
  });
});

describe('an admitted image the model wrapped in a link', () => {
  // `[![](chat-files-url)](https://anywhere)` is a shape the model can write.
  // With the plain affordance it is a navigation to a host the model chose,
  // wearing a zoom-in cursor and a button role — a worse version of the thing
  // the remote-image degradation exists to prevent, and an interactive element
  // nested inside another one.
  const png = '/istota/api/chat/files?path=%2FUsers%2Fu%2Fx.png';
  const linked = renderMarkdown(`[![radar](${png})](https://evil.example/phish)`);

  it('still draws and still sizes the image', () => {
    expect(linked).toContain('<img');
    expect(linked).toContain('class="md-image md-image-linked"');
  });

  it('withholds the lightbox affordance, so the anchor is the only control', () => {
    expect(linked).not.toContain('role="button"');
    expect(linked).not.toContain('tabindex="0"');
  });

  it('keeps the anchor and its hardening', () => {
    expect(linked).toContain('<a href="https://evil.example/phish"');
    expect(linked).toContain('rel="noopener noreferrer"');
  });

  it('keeps the affordance on an image that is merely emphasized', () => {
    // `token.level` is 1 for this too, which is why the check walks the token
    // stream for an unmatched link_open rather than reading the level.
    const emphasized = renderMarkdown(`*![radar](${png})*`);
    expect(emphasized).toContain('role="button"');
    expect(emphasized).not.toContain('md-image-linked');
  });

  it('keeps the affordance on an image following a closed link', () => {
    const after = renderMarkdown(`[a](https://evil.example) ![radar](${png})`);
    expect(after).toContain('role="button"');
    expect(after).not.toContain('md-image-linked');
  });
});
