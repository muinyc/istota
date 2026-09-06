import { describe, it, expect, vi } from 'vitest';

// The deployment's real base path. `vitest-stubs/app-paths.ts` answers `''`,
// which would make every prefix assertion below pass against a renderer that
// had dropped the base from the prefix entirely — the empty string is a prefix
// of everything. Naming the production value is what gives the negative cases
// (`/istota/api/rooms`) something to be refused for. Same shape as
// `lib/offline/clear.test.ts`.
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));

import { renderMarkdown } from './index';

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
});
