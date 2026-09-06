---
name: browse
triggers: [browse, website, web page, scrape, screenshot, url, http, visit, open page, fetch page, web search, look up, check the site]
description: Web browsing and scraping via headless browser
cli: true
companion_skills: [untrusted_input]
requires_capability: [browser]
---
# Web Browsing

Headless browser for fetching pages that need JavaScript rendering or bot detection bypass. For simple static pages or APIs, prefer `curl` or `httpx` — they're faster.

**Reach for `render` first.** It returns the page as markdown, so headings, list position and link URLs arrive together — which is what lets you tell an article link from footer chrome. `get` returns flattened text with every URL stripped out, and `links` returns a position-stripped list where nav items and articles look identical. Use those two only when you specifically want plain text or a bare link list.

## Commands

```bash
# Render a page to markdown — the default read path
istota-skill browse render "https://example.com"                    # whole page (hubs, index pages)
istota-skill browse render "https://example.com/story" --mode article  # main content only (article bodies)
istota-skill browse render "https://example.com" --keep-session
istota-skill browse render --session <id>                           # re-render what a session already holds
istota-skill browse render "https://example.com" --max-chars 250000 # raise the markdown budget

# Fetch a page as plain text + a flat link list
istota-skill browse get "https://example.com"
istota-skill browse get "https://example.com" --keep-session --timeout 60
istota-skill browse get "https://example.com" --wait-for "article.content"

# Navigate within an existing session (preserves cookies, referrer, state)
istota-skill browse get "https://example.com/page2" --session <id>
istota-skill browse render "https://example.com/page2" --session <id>

# Fetch only links (no page text)
istota-skill browse links "https://example.com"
istota-skill browse links "https://example.com" --selector "nav a"

# Screenshot — lands in your own workspace, and only there
istota-skill browse screenshot "https://example.com"
istota-skill browse screenshot --session <id> --full-page
istota-skill browse screenshot "https://example.com" -o "$NEXTCLOUD_MOUNT_PATH/Users/$ISTOTA_USER_ID/{BOT_DIR}/radar.png"

# Extract by CSS selector
istota-skill browse extract "https://example.com" -s "article"
istota-skill browse extract --session <id> -s ".price" --limit 50 --max-chars 80000

# Interact with existing session (click, fill forms, scroll)
istota-skill browse interact <id> --click ".button"
istota-skill browse interact <id> --fill "#email=user@example.com"
istota-skill browse interact <id> --scroll down --scroll-amount 1000

# Close session
istota-skill browse close <id>
```

## Output format

`render`:

```json
{"status": "ok", "url": "...", "title": "...", "mode": "full", "requested_mode": "article",
 "markdown": "## Top stories\n\n* [Headline](https://site.example/2026/07/26/story.html)\n...",
 "chars": 20539, "truncated": false, "notes": ["..."], "session_id": "..."}
```

Every URL in the markdown is already absolute — use them exactly as given. `mode` is what actually ran, which can differ from what you asked for: a URL shaped like a section front is rendered in full unless the page turns out to hold one dominant article, because isolating "the article" on an index page throws the headline grid away; a page with no article in it falls back to full too. Either way `notes` says what happened. `truncated` means you hit `--max-chars`; re-run with a bigger budget or `--mode article`.

`get`:

```json
{"status": "ok", "title": "...", "url": "...", "text": "...", "links": [{"text": "...", "href": "..."}], "session_id": "..."}
```

`screenshot`:

```json
{"status": "ok", "path": "/mnt/.../Users/{user_id}/{BOT_DIR}/screenshots/screenshot-20260906-141530.png",
 "size": 184213, "media_type": "image/png",
 "workspace_path": "/Users/{user_id}/{BOT_DIR}/screenshots/screenshot-20260906-141530.png"}
```

With no `-o` the file lands in your own workspace under `{BOT_DIR}/screenshots/`, named for the moment it was taken, and `path` is where it actually went — read it from the answer rather than assuming a name. `workspace_path` is the same file spelled the way `/istota/api/chat/files?path=` wants it, so a web-chat reply can embed the picture without rebuilding the path by hand.

`-o` takes an **absolute path inside your own workspace**. Anywhere else is refused, nothing is written, and no directory is created. A refusal before the capture costs you nothing; a refusal is not something to retry with a different path outside the workspace.

`links` here are relative or absolute exactly as the page wrote them. `session_id` is only present with `--keep-session`. `extract` returns `{"status": "ok", "selector": "...", "count": N, "elements": [{"text": "...", "html": "...", "href": "...", ...}]}`.

## Researching articles from news sites

1. Render the hub/index page with `--keep-session`:
   ```bash
   istota-skill browse render "https://www.theguardian.com/world" --keep-session
   ```
   The markdown gives you headlines with their URLs, under the section headings they sit beneath. Pick the articles you want.

2. Render each article in the same session, in article mode:
   ```bash
   istota-skill browse render "https://www.theguardian.com/world/2026/jul/26/story" --session <id> --mode article
   ```
   Same tab, so cookies, referrer and session state carry over. Article mode drops nav, ads and related-links so you get the body.

3. Close the session when done:
   ```bash
   istota-skill browse close <session_id>
   ```

This works the same on every site — Reuters, Le Monde, Der Spiegel, AP, BBC, NPR — with no per-site knowledge. If a hub looks like nothing but section names, you are almost certainly looking at `get` output rather than `render` output.

### When a hub still looks empty

- **Check you used `render`, not `get`.** A JS-rendered index page reads as bare section names through `get` because the URLs are gone.
- **Scroll for click-to-load / infinite-scroll hubs**, then re-render the same session:
  ```bash
  istota-skill browse interact <session_id> --scroll down --scroll-amount 2000
  istota-skill browse render --session <session_id>
  ```
  **Max 3 scroll rounds** — stop and use what you have.
- **Only then reach for a CSS selector.** `extract` / `links --selector` still work when you know a site's markup, but selectors rot on every redesign — treat them as a last resort, not the first move:
  ```bash
  istota-skill browse links "https://www.theguardian.com/world" --selector "a[data-link-name='article']"
  ```
  Common patterns: `a[data-link-name]`, `a[data-testid]`, `a[data-link-type]`, `h3 a`, `article a`.

## Rules

**Run browse commands yourself.** Always execute `istota-skill browse` directly in Bash. Never delegate browsing to a subtask or subagent — they lose the session context and skill instructions, leading to repeated failures.

**URLs**: Never construct, guess, or modify URLs. Take them from `render` markdown (already absolute) or from a `links`/`extract` `href` (combine with the site origin when relative). If a fetch fails, skip it — do not retry with a guessed variant.

**Failures**: If a site returns an error, empty content, captcha, or no `session_id` — try once more. If it fails twice, skip that site and use an alternative source.

**No debugging**: Never read the browse skill source code, inspect docker containers, curl the browser API directly, test session internals, or debug the browser infrastructure. If the CLI fails, move on.

**Scrolling**: Max 3 rounds. Infinite feeds never end.

## Captcha handling

If a response has `"status": "captcha"`, tell the user and provide the `vnc_url`. Wait for them to solve it, then retry with `--session <session_id>`.

## Fallback for web tools

When WebSearch or WebFetch aren't available, use `istota-skill browse` as a fallback — it always works since it runs {BOT_NAME}'s own headless browser.

## Notes

- Sessions expire after 10 minutes of inactivity — always close them when done
- Anti-fingerprinting (stealth mode) is enabled by default
- Budgets are caller-raisable: `render --max-chars` (default 100,000), `get --max-chars` / `--max-links` (50,000 / 100), `extract --max-chars` / `--limit` (25,000 per element / 20 elements)
