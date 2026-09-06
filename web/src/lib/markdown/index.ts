/**
 * Markdown renderer for chat messages, built on markdown-it.
 *
 * Safe by construction: the parser runs with `html: false`, so any raw HTML in
 * the source is escaped rather than passed through. The only tags that reach
 * the DOM are the ones markdown-it emits itself (a fixed, known set), which is
 * why we can use `{@html}` on the output without a separate sanitizer pass.
 * Link hrefs are additionally restricted to an http/https/mailto/relative
 * allowlist via `validateLink`, and every link gets `target`/`rel` hardening.
 * Images are narrower still: only our own chat-files endpoint may draw one —
 * see the `image` rule below.
 *
 * Supports the full CommonMark grammar plus markdown-it's built-in GFM tables
 * and strikethrough: fenced/indented code, inline code, bold, italic, strike,
 * links, autolinks, headings, nested ordered/unordered lists, blockquotes,
 * tables, and paragraphs.
 *
 * Fenced code blocks are syntax-highlighted with highlight.js (the `lib/common`
 * build — ~37 common languages). highlight.js escapes the code body itself, so
 * the `<pre>`-prefixed string it returns is safe to pass through markdown-it's
 * `highlight` hook. The emitted token spans (`.hljs-*`) are styled by the
 * palette in `src/app.css`.
 */
import MarkdownIt from 'markdown-it';
import hljs from 'highlight.js/lib/common';
import { base } from '$app/paths';

const md = new MarkdownIt({
  html: false, // never emit raw HTML from source — safe-by-construction
  linkify: true, // auto-link bare URLs
  breaks: true, // single newline -> <br>, which reads better in chat
  typographer: false,
  // Syntax-highlight fenced blocks. Returning a full `<pre><code>…</code></pre>`
  // string tells markdown-it to use it verbatim (it won't re-wrap). The `hljs`
  // class on <code> activates the token palette; `language-<lang>` is kept for
  // parity with the un-highlighted path and CSS hooks.
  highlight(str, lang): string {
    const langClass = lang ? ` language-${md.utils.escapeHtml(lang)}` : '';
    if (lang && hljs.getLanguage(lang)) {
      try {
        const { value } = hljs.highlight(str, { language: lang, ignoreIllegals: true });
        return `<pre><code class="hljs${langClass}">${value}</code></pre>`;
      } catch {
        // Fall through to the escaped-plain path on any hljs failure.
      }
    }
    // Unknown / missing language: escape the body ourselves and still tag it
    // `hljs` so the block background/padding match highlighted blocks.
    return `<pre><code class="hljs${langClass}">${md.utils.escapeHtml(str)}</code></pre>`;
  },
});

// Disable linkify's fuzzy (schema-less) link detection. Without this, bare
// tokens like `FILENAME.md` get auto-linked because `.md` is a real TLD
// (Moldova) — chat text is full of `something.md` filenames that must stay
// plain text. Bare URLs that carry an explicit http(s)://  scheme still linkify.
md.linkify.set({ fuzzyLink: false, fuzzyEmail: false });

const SAFE_URL = /^(https?:\/\/|mailto:|\/)/i;

// Restrict link + image hrefs to a safe scheme allowlist. markdown-it already
// blocks javascript:/vbscript:/etc.; this tightens it to exactly what chat
// content should ever produce.
md.validateLink = (url: string): boolean => SAFE_URL.test(url.trim());

// Open links in a new tab with noopener/noreferrer. We layer onto the default
// renderer rather than replacing it so URL normalization/encoding still runs.
const defaultLinkOpen =
  md.renderer.rules.link_open ??
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const token = tokens[idx];
  token.attrSet('target', '_blank');
  token.attrSet('rel', 'noopener noreferrer');
  return defaultLinkOpen(tokens, idx, options, env, self);
};

// The one `src` prefix an <img> may be drawn from: our own authenticated
// chat-files endpoint. This restates the shape `chatFileUrl` builds (api.ts) and
// shares only `base` with it, so the two *can* drift — and the drift is silent,
// since a prefix that stops matching degrades every image to a link, which is
// also the deliberate behaviour for a foreign src. What holds them together is a
// test: `index.test.ts` renders `chatFileUrl(...)`'s own output and requires an
// <img>, so a change to either spelling goes red. Importing api.ts here instead
// would pull its whole graph into a module that is otherwise a leaf.
//
// Everything served through it is a file out
// of the caller's own workspace, and since ISSUE-431 the server decides from the
// file's own magic numbers whether those bytes may render on our origin at all
// (`image_sniff.py`) — so a non-raster behind this prefix comes back as an
// attachment and the <img> degrades to its alt text rather than to a document.
//
// `validateLink` cannot carry this rule: markdown-it shares it between links and
// images, and a *link* to this same endpoint is the shipped file-handover form
// (`chatFileUrl`), which must keep working.
const CHAT_FILES_PREFIX = `${base}/api/chat/files?`;

/**
 * Is this token inside an `<a>`? Walk left for an unmatched `link_open`.
 *
 * `token.level` will not answer it — an image inside emphasis is level 1 too —
 * and the inline token stream is flat, so there is no parent to ask.
 */
function insideLink(tokens: { type: string }[], idx: number): boolean {
  let closed = 0;
  for (let i = idx - 1; i >= 0; i--) {
    const { type } = tokens[i];
    if (type === 'link_close') closed++;
    else if (type === 'link_open') {
      if (closed === 0) return true;
      closed--;
    }
  }
  return false;
}

/**
 * A `#w=<px>&h=<px>` hint on an admitted src, as `width` / `height` attributes.
 *
 * Without them the transcript reflows while it is being read: the stylesheet
 * gives an image no height until its bytes arrive, so each one is a zero-height
 * box that grows when it decodes and shoves the text after it down the page.
 * `width` + `height` alongside the stylesheet's `height: auto` hands the
 * browser the intrinsic ratio at parse time, so the final box is reserved
 * before the fetch starts. The `max-width` / `max-height` caps compose with it:
 * replaced-element sizing takes whichever constraint binds harder and shrinks
 * the other axis with it, so the reserved box is the one the loaded image ends
 * up in.
 *
 * The fragment carries the hint rather than a query parameter, for two
 * reasons. It is never sent to the server, so it cannot collide with the
 * `path` parameter `/chat/files` reads. And it does not fork the HTTP cache the
 * way `&w=` would — a link and an image to the same file stay one entry. It is
 * inert to everything that is not this rule, so the URL still works when a
 * message is mirrored to Talk or email, and an image written without one
 * renders exactly as it did before.
 *
 * Both axes or neither: one alone gives no ratio, which is the whole point.
 * What is emitted is the parsed number and never the source string — this is
 * model-authored text landing in a `{@html}` sink, and a hint it got wrong
 * reserves the wrong box, which is no worse than reserving none.
 */
const MAX_DIMENSION = 20000;

function pixels(raw: string | null): number | null {
  if (!raw || !/^[0-9]+$/.test(raw)) return null;
  const value = Number(raw);
  return value >= 1 && value <= MAX_DIMENSION ? value : null;
}

function sizeAttrs(src: string): string {
  const hash = src.indexOf('#');
  if (hash < 0) return '';
  const params = new URLSearchParams(src.slice(hash + 1));
  const width = pixels(params.get('w'));
  const height = pixels(params.get('h'));
  return width && height ? ` width="${width}" height="${height}"` : '';
}

/**
 * Images: draw one only for our own chat-files endpoint, and degrade the rest
 * to links.
 *
 * A `![](https://someone-elses-host/x.png)` in an assistant body renders inline
 * under markdown-it's default rule, which fetches from a host the model picked
 * out of a page it was reading, with the reader's IP and referer, before the
 * reader has agreed to anything. `FeedCard` draws remote images too, but a
 * subscription is consent and a model's page-read is not. So an unadmitted src
 * becomes an ordinary link — the URL stays visible and following it is the
 * reader's choice. Nothing is silently dropped.
 *
 * A src the shared `validateLink` already refused (`javascript:`, `data:`) never
 * reaches here at all: markdown-it abandons the image token and leaves the
 * source as literal text, which is the existing refusal and stays as it is.
 * `![alt]()` is the one exception — an *empty* destination is refused by
 * `validateLink` and still yields a token, with `src=''` (measured) — so it is
 * handled here rather than assumed away. It renders as its own alt text: there
 * is nothing to link to, and an `<a href="">` would navigate to a second copy
 * of the current page.
 *
 * `role="button"` / `tabindex="0"` are the affordance for the lightbox: the
 * output goes through `{@html}`, so there is no element to wrap in a real
 * `<button>` and no Svelte-side way to make one focusable. `Message.svelte`
 * delegates click and Enter/Space off them.
 *
 * An admitted image *inside a link* gets neither, and gets `md-image-linked`
 * instead. `[![](chat-files-url)](https://anywhere)` is a shape the model can
 * write, and with the plain affordance it is a navigation to a host the model
 * chose wearing a zoom-in cursor and a button role — a worse version of the
 * thing the degradation above exists to prevent, and an interactive element
 * nested in another one. Here the anchor is the control, with the `target`/`rel`
 * hardening and a visible URL, and the image is its content. A Stage 3 click
 * handler must skip an image with an `<a>` ancestor for the same reason.
 *
 * This is the only rule here that builds its own markup rather than layering
 * onto `renderToken`, so it is the only one that has to escape by hand. Every
 * `escapeHtml` call on the src is defence rather than the guard: markdown-it's
 * `normalizeLink` has already percent-encoded a `"` in the src by the time the
 * rule sees it (measured). Keep them anyway — the thing being escaped is
 * model-authored text landing in a `{@html}` sink, and that upstream property
 * is not ours to rely on. A test pinning the src half would be vacuous for that
 * reason and is deliberately not written; `alt` and `title` have one each,
 * since neither `renderInlineAsText` nor the raw attr escapes.
 */
md.renderer.rules.image = (tokens, idx, options, env, self) => {
  const token = tokens[idx];
  const src = token.attrGet('src') ?? '';
  // markdown-it puts the alt text in the token's inline children, not the attr.
  const alt = self.renderInlineAsText(token.children ?? [], options, env).trim();
  // Carried through rather than dropped: the default rule emits every attr, so
  // discarding `title` would be a silent behaviour change for `![a](x "t")`.
  const title = token.attrGet('title');
  const titleAttr = title ? ` title="${md.utils.escapeHtml(title)}"` : '';
  const href = md.utils.escapeHtml(src);

  if (!src) return md.utils.escapeHtml(alt);

  if (!src.startsWith(CHAT_FILES_PREFIX)) {
    // The label falls back to the URL rather than to nothing: an empty alt would
    // render an anchor with no text, which is invisible and unreachable.
    const label = md.utils.escapeHtml(alt || src);
    return `<a href="${href}"${titleAttr} target="_blank" rel="noopener noreferrer">${label}</a>`;
  }

  // Forced non-empty: a broken image with an empty alt is a blank space with
  // nothing to say what was lost.
  const altAttr = md.utils.escapeHtml(alt || 'image');
  const linked = insideLink(tokens, idx);
  const cls = linked ? 'md-image md-image-linked' : 'md-image';
  const affordance = linked ? '' : ' role="button" tabindex="0"';
  return (
    `<img class="${cls}" src="${href}" alt="${altAttr}"${titleAttr}${sizeAttrs(src)}` +
    ` loading="lazy" decoding="async"${affordance} />`
  );
};

export function renderMarkdown(src: string): string {
  if (!src) return '';
  return md.render(src);
}
