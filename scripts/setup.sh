#!/bin/bash
# Setup script for istota

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "Setting up istota..."

# Configure git hooks (pre-commit secret + private-data scan)
echo "Configuring git hooks..."
git config core.hooksPath .githooks
echo "  Git hooks configured"

# Presence is not enough to report the gate as armed: `gitleaks git` arrived in
# 8.19 and Debian 13 ships 8.16, so an apt-installed binary is present, looks
# fine here, and fails at the first commit. Probe the subcommand the hook
# actually calls.
if ! command -v gitleaks &> /dev/null; then
    gitleaks_problem="not found"
elif ! gitleaks git --help &> /dev/null; then
    gitleaks_problem="too old (no \`git\` subcommand — that arrived in 8.19)"
else
    gitleaks_problem=""
fi

if [ -n "$gitleaks_problem" ]; then
    echo "  WARNING: gitleaks $gitleaks_problem"
    echo "  Half the pre-commit gate is inactive: the private-data scan matches"
    echo "  patterns somebody wrote down, and gitleaks is what catches a"
    echo "  credential by shape and entropy. Nothing else covers that."
    echo "  Install: brew install gitleaks (macOS), or the release tarball from"
    echo "  https://github.com/gitleaks/gitleaks/releases (Linux)"
fi

if [ ! -f ".private-data-local" ]; then
    echo "  No .private-data-local yet — copy .private-data-local.example and add"
    echo "  your own names, hostnames and account numbers (the file is gitignored)"
fi

# Check for uv
if ! command -v uv &> /dev/null; then
    echo "Error: uv not found. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Create virtual environment and install dependencies.
#
# Not a bare `uv sync`: that installs the base dependencies only, and the suite
# needs eight of the optional groups (click from money, fastapi from location
# and web, and so on). A bare sync leaves several hundred ModuleNotFoundError
# collection errors, which is a big enough number to read as a broken checkout
# rather than as a missing package.
#
# `test` is `all` minus the two heavy ML extras — memory-search (torch,
# sentence-transformers) and whisper (faster-whisper, av, onnxruntime). The
# suite runs clean without them, at 291 MB against 1.1 GB; the one test that
# needs them carries the `ml` marker and is deselected by default. Add
# --all-extras if you want that test, or the real libraries to hand-test with.
# See docs/development/testing.md.
#
# No --group flag: uv installs the default groups, so `dev` — pytest, its
# plugins, jinja2, psutil and ruff — arrives with this. That is why ruff belongs
# in the group rather than in an extra: `ruff check` is a documented
# verification step and until ISSUE-301 this command installed no linter at all.
echo "Installing dependencies..."
uv sync --extra test

# Create data directory
mkdir -p data

# Initialize database
echo "Initializing database..."
uv run python -c "
from pathlib import Path
import sys
sys.path.insert(0, 'src')
from istota.db import init_db
init_db(Path('data/tasks.db'))
print('Database initialized at data/tasks.db')
"

# Create config from example if it doesn't exist
if [ ! -f "config/config.toml" ]; then
    cp config/config.example.toml config/config.toml
    echo "Created config/config.toml from example - please edit with your settings"
fi

# Create temp directory
mkdir -p /tmp/istota

echo ""
echo "Setup complete!"
echo ""
echo "This is a development checkout, not an install. For a machine you"
echo "actually want to run istota on, use install.sh — see"
echo "docs/getting-started/ for the three deployment shapes."
echo ""
echo "Next steps:"
echo "1. Edit config/config.toml (gitignored). At minimum a model backend:"
echo "   the claude CLI logged in for the default, or [brain.native] with a"
echo "   base_url and ISTOTA_BRAIN_NATIVE_API_KEY in your environment."
echo "2. Run one task end to end:"
echo "     uv run istota task 'What time is it?' -u testuser -x"
echo "3. Run the whole thing locally — scheduler and web in one process:"
echo "     uv run istota serve"
echo ""
echo "Verification. Python and web/ are independent; run the half you touched."
echo "docs/development/testing.md has the full table, including the six"
echo "discretionary tiers (linux, image, smoke, full, testbed, upgrade)."
echo ""
echo "  scripts/qt                    # the edit loop: only the tests your change hits"
echo "  scripts/qtest uv run pytest   # the full run, once, before a commit"
echo "  ruff check --output-format concise src tests testbed \\"
echo "      docker/browser docker/devbox docker/istota scripts"
echo ""
echo "Frontend, if you touched web/ (needs npm --prefix web ci first):"
echo ""
echo "  npm --prefix web run lint:design"
echo "  npm --prefix web run check"
echo "  scripts/qtest npm --prefix web run test"
echo "  npm --prefix web run format:check"
echo ""
echo "Do not hand-pick a test subset by reading the code, and do not run"
echo "pytest --testmon by hand — this repo's addopts carries a -m expression,"
echo "which switches testmon's selection off while looking like it worked."
echo "scripts/qt is the wrapper that gets this right. Wrap a full run in"
echo "scripts/qtest: both suites size their pool from cpu_count(), so"
echo "concurrent runs across worktrees fail on timeouts unrelated to the code."
