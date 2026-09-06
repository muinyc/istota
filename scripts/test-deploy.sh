#!/usr/bin/env bash
# Converge the Ansible role on a real systemd host, and assert on the result.
#
# The bare-metal path is the only canonical deployment shape — `AGENTS.md` says
# so, because it is the only one where the sandbox actually works — and until
# ISSUE-439 it was the only shape with no tier that ran it. Fourteen
# `tests/test_ansible_*.py` files parse the role's YAML and assert on the parse;
# nothing in the repository ever executed `ansible-playbook`. So the role's
# systemd units, its `uv sync`, its rendered `config.toml` and the ordering
# between them were covered by reading the YAML and by deploying to production.
#
#   scripts/test-deploy.sh                     # build, converge, assert
#   scripts/test-deploy.sh -k idempotent       # arguments pass through to pytest
#
# It is a discretionary command. Nothing runs it automatically, `uv run pytest`
# on the host is unchanged by its existence, and the `deploy` marker is
# deselected by pyproject's addopts.
#
# **What it covers and what it does not** is in `tests/deploy/conftest.py`, in
# full and with the reason for each concession. The short version: it drives
# `deploy/install.sh --headless` for real, with rclone, zram, Talk and the web
# UI turned off; it does not cover reboot ordering, the `Require`/`After`
# relationship with the rclone mount unit, a real FUSE mount, or anything about
# the host's own kernel, which a container shares rather than owns.
#
# About three minutes on a warm Docker cache (146-194s over five runs), most of
# it `uv sync --extra all` and the pipx install of ansible-core.
#
# `ISTOTA_DEPLOY_TIER_KEEP=1` leaves the converged container running so it can
# be inspected afterwards; it is removed by default and by nothing else, so a
# kept one is yours to `docker rm -f`.
#
# Run it under `scripts/qtest`, as with any tier: it claims a Docker daemon and
# a chunk of the machine, and knows nothing about the semaphore on its own.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Refuse inside the sandbox, and refuse *before* asking about the daemon.
#
# The ordering is the point, and it is `scripts/test-linux.sh`'s hard-won one
# (ISSUE-293): `docker version` is on the devbox proxy's allowlist, so a
# precheck that asks it passes inside a task and the run then dies minutes later
# inside `docker build`, reporting a driver error that describes nothing about
# the real boundary.
#
# The collision here is the same structural one and there is no second route to
# probe for, which is why this message is shorter than the linux tier's: that
# one has a native mode to rule out, and this tier has none. It needs to *start*
# a container — with CAP_SYS_ADMIN, a writable cgroup tree and systemd as PID 1
# — and a task reaches Docker through an allowlist proxy that permits no
# create and no start, and should not. Widening it would hand every task a host
# escape.
if [ -n "${ISTOTA_SANDBOXED:-}" ]; then
    echo "scripts/test-deploy.sh cannot run inside the sandbox." >&2
    echo "" >&2
    echo "The tier boots a container with systemd as PID 1, CAP_SYS_ADMIN," >&2
    echo "CAP_NET_ADMIN and a writable /sys/fs/cgroup, then converges the" >&2
    echo "Ansible role inside it. A task reaches Docker through the devbox" >&2
    echo "allowlist proxy, which does not permit creating or starting a" >&2
    echo "container and should not — that grant would be a host escape." >&2
    echo "" >&2
    echo "There is no second route. Unlike the linux tier there is no native" >&2
    echo "mode to fall back to: the thing being tested is a host converge, and" >&2
    echo "running it on this host would install the deployment over it." >&2
    echo "" >&2
    echo "This is not a test failure. Nothing is broken and nothing is red." >&2
    echo "Say in the merge request that the change touches the Ansible role or" >&2
    echo "the bare-metal installer and that the deploy tier is out of reach" >&2
    echo "from a task, and ask for the run before merge. See" >&2
    echo "docs/development/testing.md, 'Deployment tiers'." >&2
    # 75, not 1: the tier did not run, which is a different thing from the tier
    # running and going red. `scripts/qtest` already uses 75 the same way, and
    # so does `scripts/test-linux.sh` — a caller has to be able to tell "out of
    # reach" from "broken".
    exit 75
fi

if ! docker info >/dev/null 2>&1; then
    echo "scripts/test-deploy.sh needs a running Docker daemon." >&2
    echo "This is a discretionary tier — 'uv run pytest' on the host does not" >&2
    echo "need one." >&2
    # `docker info` rather than `docker version`, deliberately: the proxy a
    # sandboxed task talks to answers `version` and denies `info`, so this is
    # the probe that fails closed if the refusal above is ever bypassed.
    exit 1
fi

# The grants below are what let systemd run as PID 1 and bubblewrap create
# namespaces inside the container. They exist for this local test runner and
# nowhere else: they must never appear in a compose file that could be pointed
# at a real deployment. The shipped `docker/docker-compose.yml` deliberately
# grants none of them, which is why a Docker deployment runs every task
# unsandboxed and why bare metal is the supported production shape.
#
# Seven settings, four separate reasons, and none of them substitutes for
# another:
#
#   --cgroupns=host + /sys/fs/cgroup:rw   systemd needs to create and own
#                                         cgroups. Without them PID 1 fails to
#                                         mount the hierarchy and every
#                                         `systemctl` in the role fails.
#                                         `host` rather than the `private` that
#                                         cgroup v2 usually wants, and this is
#                                         the one grant here with a real cost
#                                         rather than only a scary name: on a
#                                         Linux host it hands this container's
#                                         systemd the *host's own* cgroup tree,
#                                         which with SYS_ADMIN and unconfined
#                                         seccomp is effectively root on the
#                                         machine. On macOS a Docker Desktop VM
#                                         absorbs that and it is invisible,
#                                         which is exactly why it is written
#                                         down here. Both alternatives were
#                                         measured and neither works:
#                                         `private` with the bind boots
#                                         `degraded` with journald and its three
#                                         sockets failed — and the tier reads
#                                         the journal to explain a failure —
#                                         and `private` without the bind exits
#                                         255 before systemd logs anything.
#                                         Run this tier on a machine you would
#                                         run the linux tier on, and not on a
#                                         host running the deployment.
#   --cap-add=SYS_ADMIN, NET_ADMIN        bwrap creates mount and network
#                                         namespaces. NET_ADMIN is the
#                                         non-obvious one: on the deployment
#                                         the daemon is unprivileged so bwrap
#                                         holds it inside a user namespace,
#                                         while here bwrap runs as real root
#                                         and skips that, so Docker has to
#                                         grant it or `--unshare-net` dies
#                                         bringing up loopback.
#   apparmor=unconfined                   Docker applies a default AppArmor
#                                         profile on hosts that have it
#                                         (Debian and Ubuntu do), and it
#                                         denies the mount operations bwrap
#                                         makes. Inert on a host with no
#                                         AppArmor, which is why it is easy
#                                         to think it does nothing —
#                                         `scripts/test-linux.sh` passes it
#                                         for the same reason.
#   seccomp + systempaths unconfined      A pair, and the trap. Seccomp lets
#                                         bwrap *create* a user namespace; it
#                                         does not let it mount a procfs inside
#                                         one. Docker's masked /proc entries
#                                         make the container's procfs not
#                                         "fully visible" to the kernel, which
#                                         then refuses mount("proc") in a
#                                         nested user namespace — and
#                                         `build_bwrap_cmd` emits `--proc /proc`
#                                         on every sandbox. With only the
#                                         seccomp grant every real sandbox dies
#                                         at "Can't mount proc on /newroot/proc",
#                                         which is exactly what the first
#                                         version of this tier measured.
#                                         `.claude/rules/testbed.md` records the
#                                         same pair for the same reason.
echo "note: this runner grants CAP_SYS_ADMIN + CAP_NET_ADMIN, unconfined" >&2
echo "      seccomp/apparmor/systempaths and a writable cgroup tree so that" >&2
echo "      systemd can boot and bwrap can create namespaces. Local test" >&2
echo "      runner only — never a deployment." >&2

cd "$REPO_ROOT"

# `-n0` is not optional and the tier's own guard says so, but passing it here
# means a developer never has to know that. `-p no:randomly` is deliberately
# not passed: the tests are order-independent by construction, since every one
# of them reads state a session-scoped converge left behind.
exec uv run pytest -m deploy -n0 "$@"
