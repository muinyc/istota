# Bot Detection Research & Evasion Notes

Reference document for anti-detection work on the stealth browser container.

## Current Status (2026-02-24)

### deviceandbrowserinfo.com/are_you_a_bot — 20/20 PASS

All 20 fingerprinting tests pass. `isBot: false`.

| Test | Status |
|------|--------|
| hasBotUserAgent | PASS |
| hasWebdriverTrue | PASS |
| hasWebdriverInFrameTrue | PASS |
| isPlaywright | PASS |
| hasInconsistentChromeObject | PASS |
| isPhantom | PASS |
| isNightmare | PASS |
| isSequentum | PASS |
| isSeleniumChromeDefault | PASS |
| isHeadlessChrome | PASS |
| isWebGLInconsistent | PASS |
| isAutomatedWithCDP | PASS |
| isAutomatedWithCDPInWebWorker | PASS |
| hasInconsistentClientHints | PASS |
| hasInconsistentGPUFeatures | PASS |
| isIframeOverridden | PASS |
| hasInconsistentWorkerValues | PASS |
| hasHighHardwareConcurrency | PASS |
| hasHeadlessChromeDefaultScreenResolution | PASS |
| hasSuspiciousWeakSignals | PASS |

### pixelscan.net/bot-check — PASS (Human Detected)

Previously flagged 7 navigator properties as spoofed via property descriptor detection. All resolved by removing JS overrides in favor of native Chrome values.

### bot.sannysoft.com — All PASS

Basic detection tests all passing.

## Working Configuration

- **Patchright 1.50.0** — avoids `Runtime.enable` at CDP protocol level
- **Real Google Chrome** (`google-chrome-stable` via `executable_path`) — genuine TLS fingerprint (JA3/JA4)
- **`headless=False`** on Xvfb display — avoids all headless detection
- **`no_viewport=True`** — disables Playwright's fixed viewport emulation (1280x720 default); viewport matches Chrome's window size naturally
- **No CDP locale override** — `locale` param omitted to avoid `Emulation.setLocaleOverride` (causes worker language inconsistency). System locale set via container `LANG=en_US.UTF-8` + Chrome `--lang=en-US,en` flag
- **No CDP timezone override** — `timezone_id` param omitted to avoid `Emulation.setTimezoneOverride`. Timezone set natively via `ENV TZ=America/New_York` in Dockerfile + passed through entrypoint
- **OS-level input via xdotool** — mouse movements and scrolling use `xdotool` (X11 events) instead of CDP `Input.dispatchMouseEvent`. X11 events are indistinguishable from real hardware input, defeating DataDome's screenX/pageX coordinate leak detection
- **Native navigator properties** — no JS overrides for webdriver, plugins, mimeTypes, languages, platform, hardwareConcurrency, deviceMemory. All use Chrome's native values to avoid property descriptor detection
- **Stealth init script** — remaining evasions for properties that can't be set natively (see sections below)
- **Patchright source patches** (applied in Dockerfile):
  - `crPage.js`: renamed `injected-playwright-init-script-` → `x-init-` (removes "playwright" from injected `<script>` class names)
  - `crPage.js`: renamed `__playwright_utility_world__` → `__chrome_utility_world__` (removes "playwright" from CDP world name)
  - `crPage.js`: patched `_evaluateOnNewDocument` to call real CDP `Page.addScriptToEvaluateOnNewDocument` instead of Patchright's no-op (which stores scripts for route-based injection via `Fetch.enable`). This avoids DataDome detecting `Fetch.enable` while still injecting stealth scripts before page JS executes

## Chrome Launch Flags

```
--disable-blink-features=AutomationControlled   # Native webdriver=false
--lang=en-US,en                                  # Native navigator.languages
--window-size=1440,900                           # Match Xvfb display size
--window-position=0,0
--enable-unsafe-swiftshader / --use-gl=swiftshader / --enable-webgl
--enable-features=SharedArrayBuffer
--disable-features=DnsOverHttps                  # Use Docker DNS, not hardcoded DoH
--disable-client-side-phishing-detection         # Reduce background network requests
--disable-component-update                       # Reduce background network requests
--enable-logging=stderr --v=0                    # Chrome errors in container logs
```

Note: `AsyncDns` is intentionally left enabled. Chrome's async DNS resolver is non-blocking and reads Docker's `127.0.0.11` from `/etc/resolv.conf`. Disabling it forces synchronous `getaddrinfo()` with a limited thread pool, causing address bar DNS hangs when background queries (prediction, Safe Browsing) occupy all threads.

## Key Fixes

### Pixelscan Navigator Property Descriptor Detection — Fixed

Pixelscan inspects property descriptors via `Object.getOwnPropertyDescriptor` to detect non-native getters set via `Object.defineProperty`. Removed all JS overrides for navigator properties, relying on native Chrome values:

- **webdriver** — Patchright (avoids `Runtime.enable`) + `--disable-blink-features=AutomationControlled`
- **plugins/mimeTypes** — Real Google Chrome in headed mode has 5 PDF plugins built-in
- **languages** — `--lang=en-US,en` flag + container `LANG=en_US.UTF-8` (not Patchright `locale` which uses CDP emulation)
- **platform** — Native `Linux x86_64` on Linux host
- **hardwareConcurrency** — Native host CPU count
- **deviceMemory** — Native host RAM bucket (>=8GB hosts report 8)

Worker patch (section 21) stripped to WebGL-only — navigator properties are consistent between main context and workers without any JS overrides.

### Worker Language Inconsistency — Fixed

Patchright's `locale="en-US"` uses CDP `Emulation.setLocaleOverride` which only affects the main page. Workers don't receive CDP emulation, so they used the `--lang` flag values, causing `navigator.languages` to differ between contexts (`["en-US"]` vs `["en-US", "en"]`). Fix: removed `locale` param, set container system locale to `en_US.UTF-8`, rely on `--lang=en-US,en` for consistent native values everywhere.

### isPlaywright — Fixed

Two issues layered on top of each other:

1. **Injected `<script>` DOM elements**: Patchright injects init scripts as `<script class="injected-playwright-init-script-{hex}">` elements. Detection scripts run before Patchright's `DOMContentLoaded` cleanup. **Fix:** Patched Patchright source to rename the class prefix, and added stealth section 20b with `MutationObserver` to remove elements immediately.

2. **`Object.defineProperty` traps created detectable own properties**: Detection checks `"__pwInitScripts" in window`, not the value — any own property triggers detection. **Fix:** Removed traps, leaving only `delete`. With Patchright 1.50.0's route-based injection, these globals are never set in the main world.

## Stealth Script Sections

Init script (`_STEALTH_SCRIPT` in `browse_api.py`). Sections 1, 4, 5, 7, 8, 9 removed — native Chrome values used instead.

| # | Section | Status |
|---|---------|--------|
| 1 | navigator.webdriver | REMOVED — native via Patchright + `--disable-blink-features` |
| 2 | window.chrome | Active — full chrome object with app, csi, loadTimes, runtime |
| 3 | Permissions API | Active — notifications permission returns actual state |
| 4 | Plugins & MimeTypes | REMOVED — real Chrome has 5 PDF plugins natively |
| 5 | Languages | REMOVED — native via `--lang=en-US,en` flag |
| 6 | navigator.vendor | Active — conditional, only if not already `'Google Inc.'` |
| 7 | Platform | REMOVED — native `Linux x86_64` on Linux |
| 8 | Hardware concurrency | REMOVED — native host CPU count |
| 9 | Device memory | REMOVED — native host RAM bucket |
| 10 | Network information | Active — 4g, 50ms RTT, 10Mbps |
| 11 | WebGL vendor/renderer | Active — Intel UHD 630 (hides SwiftShader) |
| 12 | Screen dimensions | Active — 1440x900, availHeight 875, screenX/Y fallback |
| 13 | Broken image dimensions | Active — 16x16 for failed images |
| 14 | Media codecs | Active — common video/audio codec support |
| 15 | Iframe contentWindow | Active — copies `window.chrome` to srcdoc iframes |
| 16 | console.debug | Active — marked as native |
| 17 | Error stack traces | Active — strips playwright/puppeteer from stacks |
| 18 | Notification constructor | Active — marked as native |
| 19 | Battery API | Active — passthrough |
| 20 | Playwright globals cleanup | Active — deletes `__pwInitScripts` etc. (no traps) |
| 20b | Injected script DOM cleanup | Active — MutationObserver removes playwright script elements |
| 21 | Worker WebGL consistency | Active — patches WebGL vendor/renderer in workers only |

**Native function spoofing:** `_makeNative(fn, name)` + patched `Function.prototype.toString`.

## Container Configuration

- **Base:** `python:3.12-slim-trixie` with real `google-chrome-stable`
- **User:** Non-root `browser` user (entrypoint drops from root via `su`), suppresses `--no-sandbox` infobar
- **Display:** Xvfb 1440x900, x11vnc, noVNC on port 6080
- **Input:** `xdotool` for OS-level X11 mouse/keyboard events (replaces CDP input)
- **Browser window:** `--window-size=1440,900` (Chrome sizes itself without a WM; `no_viewport=True` ensures viewport matches window)
- **Locale:** `LANG=en_US.UTF-8` (generated via `locales` package)
- **Timezone:** `TZ=America/New_York` (OS-level, not CDP emulation)
- **Resources:** 2 CPUs, 3GB memory, 2GB shm
- **Monitoring:** Background resource monitor (30s interval), request logging, Chrome stderr logging, page crash/context close event handlers
- **Health endpoint:** `/health` (basic) or `/health?v=1` (Chrome process detail, memory breakdown)

### DataDome (NYTimes) — Fixed

Two separate DataDome detection vectors were fixed:

**1. CDP Input Events:** DataDome detected CDP-dispatched input events (`Input.dispatchMouseEvent` via `page.mouse.move()`) through screenX/pageX coordinate leaks.

**Fix:** Replaced all CDP input with OS-level X11 events via `xdotool`:
- `xdotool mousemove --screen 0 X Y` for mouse movement
- `xdotool key Page_Down` / `xdotool key Page_Up` for scrolling
- `xdotool click 1` for clicks

xdotool sends events through the X11 event pipeline, which Chrome receives as genuine OS input — indistinguishable from real hardware. Also removed `timezone_id` param (CDP `Emulation.setTimezoneOverride`) in favor of OS-level `TZ` env var.

**2. Fetch.enable / Route-Based Injection:** Patchright's `_evaluateOnNewDocument` is a no-op that only pushes scripts to an array. Actual injection happens via HTTP response modification: the network manager intercepts responses via `Fetch.enable`, prepends `<script>` tags, and serves modified HTML via `Fetch.fulfillRequest`. DataDome detects `Fetch.enable` and blocks the session. Even a `"void 0;"` init script triggers this.

**Fix:** Patched `_evaluateOnNewDocument` in `crPage.js` to call the real CDP `Page.addScriptToEvaluateOnNewDocument` instead. This injects scripts into Chrome's V8 engine directly (before any page JS), without intercepting HTTP responses. The array-based storage is removed, so the route-based injection path never activates and `Fetch.enable` is never sent.

### Cloudflare Challenge (economist.com) — Fixed

Cloudflare challenge page ("Just a moment...") would spin forever when navigated via automation, but resolved instantly via manual VNC browsing with the same browser instance. Root cause: CDP commands detectable during the challenge fingerprinting window.

**Two detection vectors:**

**1. CDP Page.navigate:** `page.goto()` sends the CDP `Page.navigate` command. Manual browsing uses the address bar (keyboard input). Cloudflare's challenge JS can detect that navigation was triggered programmatically.

**Fix:** Navigate via xdotool keyboard input — `Ctrl+L` (focus address bar), type URL, `Enter`. Uses X11 events identical to manual browsing. Falls back to CDP if xdotool fails. Applied to first navigation in new sessions; reused sessions already have the Cloudflare clearance cookie.

**2. CDP Runtime.evaluate during challenge window:** Immediately after navigation, we called `page.evaluate()` for DataDome detection. This sends `Runtime.evaluate` CDP commands while Cloudflare's challenge JS is running its fingerprinting checks. The challenge detects active CDP usage and fails.

**Fix:** Added 3-5 second passive wait (no CDP evaluate calls) after navigation. DataDome and captcha checks moved to after this window. `page.wait_for_timeout()` is a pure timer that doesn't send CDP commands.

### Behavioral Realism Improvements

Replaced uniform-random timing model with human motor patterns:
- **Gaussian timing** — `random.gauss(0.008, 0.003)` bell-curve distribution matching real motor control
- **Fitts's Law speed profile** — slow at start (acceleration), fast in middle, slow at end (deceleration) via `delay * (1 + 2 * abs(0.5 - progress))`
- **Reading pause** — 1.5-4s pause after page load before any interaction
- **Micro-jitter** — ±1-2px gaussian perturbation on each mouse position
- **Idle moments** — 30% chance of 0.3-1.0s pause between movements (thinking/reading)

## Open Issues

### Patchright Version Lock

Pinned to Patchright 1.50.0. Version 1.57.2 regressed on `Runtime.enable` avoidance and Playwright artifact cleanup. Monitor Patchright releases for fixes, but test thoroughly before upgrading.

### ~~Container Runs as Root~~ — Fixed

Entrypoint runs Xvfb/VNC as root, then drops to a non-root `browser` user via `su` for Flask + Chrome. Chrome still uses `--no-sandbox` (Docker containers lack namespace privileges for Chrome's native sandbox), but the yellow infobar warning only appears for root — non-root users don't see it.

## References

- [Castle.io — Why a classic CDP detection signal stopped working](https://blog.castle.io/why-a-classic-cdp-bot-detection-signal-suddenly-stopped-working-and-nobody-noticed/)
- [Rebrowser — How to fix Runtime.Enable CDP detection](https://rebrowser.net/blog/how-to-fix-runtime-enable-cdp-detection-of-puppeteer-playwright-and-other-automation-libraries)
- [Rebrowser Bot Detector (source)](https://github.com/rebrowser/rebrowser-bot-detector)
- [Rebrowser — Sensitive CDP methods](https://rebrowser.net/docs/sensitive-cdp-methods)
- [Castle.io — How to detect scripts injected via CDP](https://blog.castle.io/how-to-detect-scripts-injected-via-cdp-in-chrome-2/)
- [Patchright GitHub](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)
