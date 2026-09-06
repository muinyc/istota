# Layer 1 — the Linux test runner.
#
# The unit suite runs on the developer's macOS host, where `bwrap` does not
# exist and `tests/test_sandbox.py` therefore patches `_bwrap_available` and
# asserts argv. This image is where the sandbox code path actually executes: a
# real kernel, a real bubblewrap, real mount namespaces. `scripts/test-linux.sh`
# is the only supported way to run it — see that script for the capability
# grants it needs and why they must never appear in a deployment compose file.
#
# Trixie, matching every supported deployment, because the base is part of what
# this tier observes rather than a detail of how it is built: the apt line below
# takes bubblewrap and git from it, and bookworm's are 0.8.0 and 2.39 against
# the 0.12.0 and 2.47 production runs. A sandbox tier answering about a
# bubblewrap four years older than the deployed one is answering about nothing.
# `tests/test_docker_base_images.py` holds this and the other images to it.
FROM python:3.12-slim-trixie

ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8

# bubblewrap is the point of the image. The rest mirrors what the runtime image
# installs for the same tests: git and sqlite3 for the developer and skill
# paths, tmux for the tmux brain, curl + ca-certificates for anything that
# resolves a URL.
# procps is not decoration: tests/test_qtest_semaphore.py and the process-group
# tests shell out to `ps` to prove a grandchild died with its group, and a
# slim image has no `ps` at all — the assertion then fails on exit 127 rather
# than on the property it is about.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bubblewrap git sqlite3 curl tmux ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# Pinned, not `:latest`. The Python dependencies are pinned by `uv sync
# --frozen` a few lines down, so a floating toolchain is the one thing in this
# file that could make two builds of the same commit disagree.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

# The venv lives OUTSIDE the source tree, because scripts/test-linux.sh binds
# the host checkout over the working directory read-only. A venv at ./.venv
# would be shadowed by that mount, and `uv run` would then try to rebuild an
# environment inside a read-only directory and fail before collecting a test.
ENV UV_PROJECT_ENVIRONMENT=/venv
ENV UV_CACHE_DIR=/uv-cache
ENV PATH="/venv/bin:${PATH}"
# ruff writes its cache beside the rootdir by default, and the rootdir is the
# read-only bind. Point it at the container's writable /tmp so a cache miss is
# a cache miss rather than a warning on every run.
ENV RUFF_CACHE_DIR=/tmp/ruff-cache

WORKDIR /build
COPY pyproject.toml uv.lock ./
# --no-install-project: `istota` is a hatchling package with no
# `[tool.uv] package = false`, so a plain sync would try to build the root
# project and fail — src/ is not in the build context at this point. The source
# arrives at run time via the bind mount, and the .pth written below is what
# makes it importable. See that step for why pythonpath is not enough.
#
# Extras: `all` minus the two heavy ones. `memory-search` pulls torch and
# sentence-transformers and `whisper` pulls faster-whisper, av and onnxruntime
# — together about 750 MB of wheels for an image whose job is mount namespaces.
# Nothing imports either at module scope; every heavy import in src/ is inside a
# function, deliberately, so the suite collects and runs clean without them.
#
# The one test that needs a heavy extra carries the `ml` marker and is
# deselected by both pyproject's addopts and scripts/test-linux.sh. If a future
# test imports torch or faster-whisper at module scope, collection fails loudly
# here rather than quietly passing on a developer host that happens to have
# them; the fix is the marker, not a bigger install.
#
# `test` is that set, defined once in pyproject.toml — it used to be nine
# `--extra` flags spelled out here, because there is no way to subtract an extra
# from `all`, and this file is where such a list drifts. jinja2 and psutil used
# to be in it by proxy (via `docs` and `whisper`); they are test dependencies
# and now live in the `dev` group, which `--group dev` below installs.
RUN uv sync --frozen --no-install-project --group dev --extra test

# ...and then put the project itself on that venv's import path, pointing at
# where the source arrives at run time (ISSUE-398).
#
# **The import path, and deliberately nothing else.** This writes no
# `.dist-info` and no console scripts, so it is weaker than the `uv sync` a
# deployment runs: `importlib.metadata.version("istota")` still raises here —
# `cli._installed_version` is the fallback that exists for it — and
# `istota-skill` is still absent from this image, so anything resolving the
# package by console-script name rather than as
# `[sys.executable, "-m", "istota.…"]` does not work in the tier and never did.
# What it fixes is importability, which is the whole of what was broken.
#
# Written by hand because the two halves happen at different times: `uv sync`
# cannot install a project whose source is not in the build context, and the
# source is not in the build context because the point of the tier is to test
# the developer's checkout rather than a copy baked into a layer. A real
# install would bake that copy and shadow the bind, so the tier would report on
# code nobody edited.
#
# `pythonpath = ["src", "."]` in pyproject.toml is NOT a substitute, and
# assuming it was cost 47 failures on a clean `main`. That setting mutates
# `sys.path` inside the pytest process and reaches no child of it — and the
# native brain spawns `[sys.executable, "-m", "istota.tool_server"]` with the
# *task* env, which `build_clean_env` builds from PATH, HOME and
# PYTHONUNBUFFERED. Neither the ini setting nor a `-e PYTHONPATH` on the
# `docker run` reaches that spawn, so every native task in the tier died at the
# handshake with `ModuleNotFoundError: No module named 'istota'`.
# `tests/test_security.py::TestTheTaskEnvCanImportIstota` is what states the
# contract now, and it runs inside the tier.
#
# `/src/src` is the checkout bind in `scripts/test-linux.sh`'s `run_in_container`
# (`-v "$REPO_ROOT:/src:ro"`), which is the only thing making this path true.
# Change it there and change it here. A .pth line naming a directory that does
# not exist is ignored in silence, so an image run without the bind behaves
# exactly as it did.
#
# `sysconfig` names the directory rather than `site.getsitepackages()[0]`
# indexing into it: that list's order is not contracted and a patched
# interpreter returns several entries, so an index picks the right one here and
# could pick a `dist-packages` a venv never scans on a different base image —
# writing the file, exiting 0, and resurfacing as the same 47
# ModuleNotFoundErrors. The assertions are what make that a build failure
# instead: the target has to be a directory `site` actually scans, and the file
# has to be there afterwards.
RUN /venv/bin/python -c "\
import pathlib, site, sysconfig; \
target = pathlib.Path(sysconfig.get_paths()['purelib']); \
assert str(target) in site.getsitepackages(), (target, site.getsitepackages()); \
entry = target / '_istota_src.pth'; \
entry.write_text('/src/src\n'); \
assert entry.read_text() == '/src/src\n', entry"

# ruff is in [dependency-groups] dev as of ISSUE-301 and so already arrives in
# /venv above. Keeping the tool install anyway is deliberate: /root/.local/bin
# is prepended below, so the lint gate in scripts/test-linux.sh resolves this
# copy whatever a future `uv sync` does with the group, and the runner's
# behaviour does not change with a flag somewhere else. It used to be the only
# place ruff was installed at all, which is what left a developer on a fresh
# clone unable to run the check AGENTS.md mandates.
#
# Pinned for the same reason as uv above, and because the lint gate in
# scripts/test-linux.sh is what the whole suite run hangs on: an unpinned ruff
# turns a new rule in a new release into "the Linux runner is broken". This is
# the version the tree is known clean under; bump it deliberately, here and in
# the dev group together — tests/test_lean_install.py pins them equal.
RUN uv tool install ruff==0.16.4
ENV PATH="/root/.local/bin:${PATH}"
