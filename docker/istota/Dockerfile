# --- Frontend build stage ---
# SvelteKit static build for the web UI. The output (web/build) is copied into
# the runtime image so the web service can mount it without `npm install` at
# container start.
FROM node:20-slim AS web-builder

WORKDIR /app/web

# Copy lock first for cache-friendly dependency install.
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

# --- Runtime stage ---
FROM python:3.12-slim-trixie

ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8

# System deps (including weasyprint/tesseract requirements for all extras)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git sqlite3 \
    # tmux — required by the tmux_claude brain (drives the interactive claude TUI)
    tmux \
    # Bubblewrap sandbox (per-invocation filesystem isolation)
    bubblewrap \
    # WeasyPrint (accounting/invoicing)
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev libcairo2 \
    # Tesseract OCR (transcribe skill)
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 (for Claude Code)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code
RUN npm install -g @anthropic-ai/claude-code

# uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Forge CLIs (developer skill). The skill's forge verbs — MR/PR creation,
# review, ci/run status, repo view — exec a real `gh` or `glab` through
# src/istota/forge_cli.py, which the daemon writes per task. Without the
# binaries every one of them exits 6 at exec, while git clone/branch/push keep
# working, so the skill looks configured and dies at the point it would publish.
#
# Not from apt: this is trixie, whose gh is 2.46 and glab 1.53 — roughly 50 and
# 60 releases behind the 2.98 and 1.114 these verbs have actually been
# exercised against. It was bookworm until ISSUE-440, where the gap was wider
# still (gh 2.23, and no glab packaged at all); the base moved, the reason for
# vendoring did not.
# Same versions as docker/devbox/Dockerfile and the Ansible role's
# istota_developer_gh_version / _glab_version; nothing can share a literal
# across a Docker build and an Ansible role, so
# tests/test_ansible_forge_cli_install.py holds the three equal instead. Bump
# them together.
#
# This builds on arm64 as well as amd64, so the asset is chosen per
# architecture and each has its own pinned checksum. The devbox image does the
# same, in the same way, and a test holds the two in step — it used to be
# amd64-only because of a hardcoded Go tarball, until ISSUE-280. The arch comes
# from dpkg rather than BuildKit's TARGETARCH: dpkg's names are already the
# ones these two projects use for their assets, and it is correct under the
# legacy builder, where TARGETARCH is simply empty. An architecture with no
# pinned checksum fails the build rather than going unverified.
#
# Under /usr/local/lib rather than /usr/local/bin, and deliberately: the only
# `gh` resolvable *by name* must be the wrapper, which is what applies the deny
# policy and injects the token. Note the limit of that — the sandbox binds /usr
# read-only (`build_bwrap_cmd`), so the real binary is still reachable at its
# absolute path. What this placement buys is that nothing reaches it by
# accident or habit; it is not a containment boundary. The boundary is the
# skill proxy, which keeps the token out of the model's environment entirely,
# so a direct exec gets an unauthenticated `gh`. With the proxy turned off the
# token is ambient and a direct exec is unpoliced — the same exposure a host
# install already has at /usr/local/bin/gh, which is why this matches rather
# than worsens the Ansible shape. The role deliberately does not copy this
# placement: on a host an apt-installed `gh` may still be present and
# name-resolvable, so moving the role's own binary off PATH would buy the
# property for nobody and break `gh` for an operator on the box.
#
# The daemon cannot find an off-PATH binary by name, so it is named twice:
# render-config.sh renders gh_bin_path / glab_bin_path into the [developer] block
# on a first boot, and `_resolve_real_bin` probes this location for containers
# upgraded past a config.toml that predates it.
#
# Extracted from the .deb rather than installed with it: dpkg would put a
# second, real `gh` at /usr/bin/gh, which is the same bypass by another route.
#
# The `dpkg-deb | tar` pipeline has no pipefail under Docker's default /bin/sh
# (dash), so a dpkg-deb that fails partway leaves tar exiting 0 over a
# truncated stream. The `--version` calls at the end are what actually catch
# that — a truncated ELF cannot exec — so do not remove them thinking they are
# decorative.
ARG GH_VERSION=2.98.0
ARG GH_DEB_SHA256_AMD64=f65a3fa2fa0eb2e97c445ee3f5e087a40aae03b64847f45a8f13805e504535d6
ARG GH_DEB_SHA256_ARM64=bbc4ac7964c2a091fd555cd1758d10a7cfcfdc472e405f0b0fb958f05d535cb6
ARG GLAB_VERSION=1.114.0
ARG GLAB_DEB_SHA256_AMD64=6df680f88a2a45700d1dd6a08a44a74838473ef3dcc5cb1ad243f4d1fd704ad0
ARG GLAB_DEB_SHA256_ARM64=c2bcb44cc10d849cccb879e687f3b9e102a3415c3d02f0cecfd9ce1442519350
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64) gh_sha="${GH_DEB_SHA256_AMD64}"; glab_sha="${GLAB_DEB_SHA256_AMD64}" ;; \
        arm64) gh_sha="${GH_DEB_SHA256_ARM64}"; glab_sha="${GLAB_DEB_SHA256_ARM64}" ;; \
        *) echo "no pinned gh/glab checksum for ${arch}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /usr/local/lib/istota_forge; \
    curl -fsSL -o /tmp/gh.deb \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.deb"; \
    echo "${gh_sha}  /tmp/gh.deb" | sha256sum -c -; \
    dpkg-deb --fsys-tarfile /tmp/gh.deb \
        | tar -xO ./usr/bin/gh > /usr/local/lib/istota_forge/gh; \
    curl -fsSL -o /tmp/glab.deb \
        "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/${GLAB_VERSION}/glab_${GLAB_VERSION}_linux_${arch}.deb"; \
    echo "${glab_sha}  /tmp/glab.deb" | sha256sum -c -; \
    dpkg-deb --fsys-tarfile /tmp/glab.deb \
        | tar -xO ./usr/bin/glab > /usr/local/lib/istota_forge/glab; \
    rm -f /tmp/gh.deb /tmp/glab.deb; \
    chmod 0755 /usr/local/lib/istota_forge/gh /usr/local/lib/istota_forge/glab; \
    /usr/local/lib/istota_forge/gh --version; \
    /usr/local/lib/istota_forge/glab --version

# Project files
WORKDIR /app
COPY pyproject.toml uv.lock schema.sql README.md ./
COPY src/ src/
COPY config/ config/

# Install all deps (core + all optional extras)
RUN uv sync --frozen --no-dev --extra all

# Put the venv bin dir on the default PATH so the console scripts (istota,
# istota-scheduler, istota-skill, money) resolve from any shell or subprocess
# without relying on `uv run` or a per-process PATH injection surviving the
# interactive TUI's profile re-initialization (the tmux_claude brain otherwise
# sees "istota-skill: command not found" when it shells out).
ENV PATH="/app/.venv/bin:${PATH}"

# SvelteKit static build (from web-builder stage)
COPY --from=web-builder /app/web/build /app/web/build

# Data directories (persistent via volume)
RUN mkdir -p /data/db /data/config /data/tmp

# Entrypoint. render-config.sh must land beside it — the entrypoint resolves it
# relative to its own directory, and the image tier runs it on its own to render
# a config in a volume-less container.
COPY docker/istota/entrypoint.sh /entrypoint.sh
COPY docker/istota/render-config.sh /render-config.sh
COPY docker/istota/config-diff.py /config-diff.py
RUN chmod +x /entrypoint.sh /render-config.sh /config-diff.py

ENTRYPOINT ["/entrypoint.sh"]
