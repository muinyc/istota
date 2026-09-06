FROM python:3.12-slim-trixie

# Set system locale so Chrome and Intl APIs use en_US natively
# (avoids needing CDP Emulation.setLocaleOverride which only affects main page)
ENV LANG=en_US.UTF-8
ENV TZ=America/New_York

# Install system deps: VNC, noVNC, Google Chrome, fonts, and utilities.
# The font packages are the debian12 `tools` list that `patchright install-deps`
# used to install for us. That command is gone: Patchright 1.50 has no Debian 13
# entry, so on trixie it falls back to its ubuntu20.04 list and asks for
# ttf-unifont / ttf-ubuntu-font-family, which trixie renamed away. Installing the
# list by hand keeps the font set -- and so the font-fingerprint surface -- the
# same as the bookworm image, where the detection picked debian12 correctly.
# Chrome's own .deb pulls in the chromium shared libs, which is the rest of what
# install-deps did.
#
# fonts-dejavu-core is not on that list: it arrived on bookworm as a *recommends*
# of the install-deps apt call, which ran without --no-install-recommends. DejaVu
# is the standard Linux fallback family, so losing it is itself a fingerprint,
# and nothing here would have named it. fonts-liberation-sans-narrow is separate
# because trixie's fonts-liberation moved to Liberation 2.x, which dropped the
# Narrow faces bookworm's 1.x shipped. Both are here to keep the font list level
# with the bookworm image; check `fc-list : family` against it before trimming.
RUN apt-get update && apt-get install -y --no-install-recommends \
    x11vnc \
    novnc \
    xvfb \
    x11-utils \
    xdotool \
    wget \
    curl \
    openssl \
    gnupg \
    locales \
    fonts-noto-color-emoji \
    fonts-unifont \
    fonts-liberation \
    fonts-liberation-sans-narrow \
    fonts-dejavu-core \
    fonts-ipafont-gothic \
    fonts-wqy-zenhei \
    fonts-tlwg-loma-otf \
    fonts-freefont-ttf \
    xfonts-scalable \
    libfontconfig1 \
    libfreetype6 \
    && sed -i '/en_US.UTF-8/s/^# //' /etc/locale.gen && locale-gen \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user so Chrome doesn't show --no-sandbox infobar
RUN groupadd -r browser && useradd -r -g browser -G audio,video -d /home/browser -m browser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Patch Patchright source to remove "playwright" from identifiers.
# These patches are still useful when Patchright connects via CDP.
RUN find /usr/local/lib/python3.12/site-packages/patchright -name 'crPage.js' \
    -exec sed -i 's/injected-playwright-init-script-/x-init-/g' {} + && \
    find /usr/local/lib/python3.12/site-packages/patchright -name 'crPage.js' \
    -exec sed -i "s/__playwright_utility_world__/__chrome_utility_world__/g" {} +

# Copy stealth extension source files
COPY stealth-extension/manifest.json stealth-extension/stealth.js \
     stealth-extension/autoconsent-prefix.js stealth-extension/autoconsent-suffix.js \
     /app/stealth-extension/

# Download autoconsent bundle and build the extension's autoconsent.js
RUN curl -fsSL -o /tmp/autoconsent-bundle.js \
    "https://cdn.jsdelivr.net/npm/@duckduckgo/autoconsent@14.28.0/dist/autoconsent.playwright.js" && \
    cat /app/stealth-extension/autoconsent-prefix.js \
        /tmp/autoconsent-bundle.js \
        /app/stealth-extension/autoconsent-suffix.js \
        > /app/stealth-extension/autoconsent.js && \
    rm /tmp/autoconsent-bundle.js \
       /app/stealth-extension/autoconsent-prefix.js \
       /app/stealth-extension/autoconsent-suffix.js

COPY browse_api.py chrome.py browsing.py render.py xdotool.py ./
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Set up directories the browser user needs write access to
RUN mkdir -p /data/browser-profile /tmp/.X11-unix && \
    chown -R browser:browser /data/browser-profile /app

# API port and noVNC port
EXPOSE 9223 6080

# Health is the lightweight liveness endpoint on a separate thread/port (9224),
# not the single-threaded Flask /health (9223): a long in-flight browse blocks
# Flask, which would make a busy-but-healthy container read as unhealthy and get
# restarted mid-operation (ISSUE-143). The liveness thread answers even while
# Flask is busy. The deep tier (`?deep=1`) also probes Chrome's DevTools endpoint
# so a wedged-but-alive Chrome (frozen process, hung CDP) reads as unhealthy too,
# not just a dead process — a busy browse still passes (ISSUE-149). Both of those
# ask about Chrome; the deep tier's third arm asks whether the API process can
# still drive it, by reading the CDP heartbeat rather than by trying (ISSUE-384).
# A fourth arm reads how often the browse watchdog has had to relaunch Chrome
# (ISSUE-394): a browser whose UI thread is blocked satisfies all three of the
# others -- the process lives, DevTools answers on its IO thread, and a hung CDP
# call records no failure -- so a container wedging every 90s read as healthy for
# hours. The recovery rate is the one signal the wedge cannot fake.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf "http://localhost:9224/live?deep=1" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
