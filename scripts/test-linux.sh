#!/usr/bin/env bash
# Run the test suite on a real Linux kernel, with a real bubblewrap.
#
# Everything istota knows about its own runtime is otherwise asserted on the
# one platform that cannot run it: `tests/test_sandbox.py` patches
# `_bwrap_available` and checks argv, so on darwin the sandbox code path has
# never executed. This driver runs the suite where bwrap is real — including
# the `linux`-marked tests, which are deselected everywhere else.
#
# It is a discretionary command. Nothing runs it automatically, `uv run pytest`
# on the host is unchanged by its existence, and the `linux` marker is
# deselected by pyproject's addopts so a developer on a box without Docker can
# run and develop against the whole suite as before.
#
#   scripts/test-linux.sh                      # ruff + the suite + the linux tests
#   scripts/test-linux.sh -m linux             # just the sandbox tests
#   scripts/test-linux.sh tests/test_sandbox.py -x
#
# Any arguments are passed through to pytest.
#
# TWO MODES, one switch: ISTOTA_LINUX_TIER_MODE=auto|native|container, default
# `auto`.
#
#   container  Build docker/test/Dockerfile, bind the checkout read-only at
#              /src, and run the suite inside. What every macOS developer gets,
#              and what this driver did exclusively before native mode existed.
#   native     Run the suite directly on this host, in the worktree venv. No
#              Docker, no image build.
#   auto       native when the host is Linux and bwrap can create both a user
#              and a network namespace here; container otherwise. `auto` never
#              picks native on a host running the deployment — see below.
#
# `ISTOTA_LINUX_TIER_PRINT_MODE=1` resolves the mode, prints it, and runs
# nothing. Useful for asking a host what it would do. It inherits the refusals
# rather than reporting past them: on a host that looks like a deployment it
# exits 75 with that message, which is the honest answer to "what would you do
# here".
#
# WHY NATIVE MODE EXISTS. On a Linux host the container is not just slower, it
# tests *less*. Inside it bwrap runs as real root and therefore never creates a
# user namespace, which is why the container needs CAP_NET_ADMIN granted from
# outside; the deployment runs bwrap unprivileged, and a host run reproduces
# that. `--disable-userns` is never exercised in the container either — the
# flag needs /proc/sys/user/max_user_namespaces, which is read-only there, so
# `_bwrap_supports_disable_userns()` probes false and the flag is dropped from
# argv. And the `requires_dac` tests skip as uid 0 and run as an ordinary user.
#
# WHAT NATIVE MODE GIVES UP. The pinned toolchain: the image does
# `uv sync --frozen` and `uv tool install ruff==0.16.4`, where a host run gets
# the worktree venv as it finds it. `tests/test_lean_install.py` pins the
# dev-group ruff equal to the image's, so the lint gate still matches — but the
# venv is a prerequisite rather than something this script builds: run
# `uv sync --extra test` first, or the suite reports several hundred
# ModuleNotFoundError collection errors that read as a code regression. The
# host also needs `bubblewrap git sqlite3 tmux procps`, which the image
# installs and which native mode checks for and names. And the cgroup tests
# skip unless the run has a delegated subtree of its own: see the note on the
# cgroup helper further down.
#
# Run it under `scripts/qtest`, as with any full suite: both modes size the
# worker pool from the host's cores and know nothing about the semaphore, so an
# unwrapped run competes with whatever else is testing on the machine.
#
# One observed quirk of container mode, so a phantom failure is not chased:
# Docker Desktop's file sharing has served a *stale* copy of a just-edited file
# through the read-only bind, which surfaced as a test asserting against a file
# that no longer looked like that. A second run picked up the current one. If a
# result contradicts what you can read on disk, run it again before believing
# it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_TAG="${ISTOTA_TEST_IMAGE:-istota-test:local}"
CACHE_VOLUME="istota-test-cache"

# Refuse inside the sandbox, and refuse *before* asking about the daemon.
#
# A task's Docker access is the devbox allowlist proxy, which permits ping,
# version, container list, and inspect/archive/restart/exec on the task's own
# container — and nothing that creates or starts one. This tier needs to run a
# container with CAP_SYS_ADMIN, CAP_NET_ADMIN and unconfined seccomp, which is
# the exact capability the sandbox exists to deny. The collision is structural:
# it is not a misconfiguration to be fixed by widening the allowlist, and
# widening it would hand every task a host escape.
#
# The ordering is the point. `docker version` is *on* the proxy's allowlist, so
# the precheck below passes inside a task and the run then died minutes later
# inside `docker build`, reporting a buildx driver error that describes nothing
# about the real boundary (ISSUE-293). The pytest tiers never had this failure
# mode because they precheck with `docker info`, which the proxy denies.
#
# It is also ahead of the mode switch, and has to stay there: native mode does
# not go near Docker, so a sandboxed task that got as far as resolving a mode
# would be told to try the one route this check exists to close.
if [ -n "${ISTOTA_SANDBOXED:-}" ]; then
    # Both routes are named, because Docker is not the only one and on a Linux
    # deployment host it is not the one that bites first (ISSUE-315). The
    # original message explained itself entirely in terms of Docker, which left
    # an agent on a Linux box with `/usr/bin/bwrap` in view an obvious next
    # move: skip the container and run bwrap directly. That fails with a
    # namespace error naming nothing about the real boundary — the same
    # confusion ISSUE-293 was filed about, one layer along — and native mode
    # turned "skip the container" into a supported flag.
    #
    # The Docker half is fixed and prints first, ahead of the probe. A probe is
    # a subprocess and a subprocess can hang; a refusal that printed nothing
    # until bwrap came back would read as a wedged task rather than as a
    # boundary, which is the failure mode this whole message exists to remove.
    echo "scripts/test-linux.sh cannot run inside the sandbox." >&2
    echo "" >&2
    echo "Container mode is closed by Docker. It runs a container with" >&2
    echo "CAP_SYS_ADMIN and CAP_NET_ADMIN so that bwrap can create namespaces," >&2
    echo "and a task reaches Docker through the devbox allowlist proxy, which" >&2
    echo "does not permit creating or starting a container and should not —" >&2
    echo "that grant would be a host escape." >&2
    echo "" >&2

    # The nested-namespace half is *probed*, not asserted. A task's sandbox
    # passes `--unshare-user --disable-userns` only where bwrap supports the
    # flag (0.8+), and `_bwrap_supports` also records it unsupported when its
    # own probe times out — so on some hosts the flag never reached the argv
    # and a nested namespace really would start. Claiming otherwise there would
    # be the same kind of confident wrong answer this issue is about.
    #
    # Deliberately *not* the same probe as the product's
    # `_bwrap_supports_disable_userns`, which passes `--disable-userns` and
    # asks whether bwrap accepts the flag. This one omits it and asks whether a
    # nested namespace starts, which is the question a reader here actually
    # has, and it answers it about the sandbox they are standing in rather than
    # about the bwrap binary.
    #
    # Bounded where `timeout` exists, and the output is kept rather than
    # discarded: the probe answers "did a nested bwrap start", which is one
    # question short of "why", and bwrap's own stderr is the only thing that
    # closes that gap. Printing it is what keeps the branch below from
    # asserting a cause it did not establish.
    nested_userns="absent"
    probe_output=""
    probe_status=0
    probe_bounded=""
    if command -v bwrap >/dev/null 2>&1; then
        bwrap_probe=(bwrap)
        if command -v timeout >/dev/null 2>&1; then
            bwrap_probe=(timeout 5 bwrap)
            probe_bounded="1"
        fi
        probe_output="$("${bwrap_probe[@]}" --unshare-user --ro-bind / / -- true 2>&1)" \
            || probe_status=$?
        if [ "$probe_status" -eq 0 ]; then
            nested_userns="open"
        elif [ -n "$probe_bounded" ] && [ "$probe_status" -eq 124 ]; then
            nested_userns="stuck"
        else
            nested_userns="blocked"
        fi
    fi

    case "$nested_userns" in
        blocked)
            echo "Native mode is closed too. It needs bwrap to create a user namespace," >&2
            echo "and a nested one does not start in here. Probed just now:" >&2
            echo "" >&2
            echo "  \$ bwrap --unshare-user --ro-bind / / -- true" >&2
            echo "  ${probe_output}" >&2
            echo "" >&2
            echo "That is what --disable-userns does, and your sandbox passes it wherever" >&2
            echo "bwrap supports the flag. It is the sandbox working, not a broken" >&2
            echo "install, and not a flag to remove." >&2
            ;;
        open)
            echo "Native mode is not closed by the sandbox on this host: 'bwrap" >&2
            echo "--unshare-user' succeeds in here, so --disable-userns never reached" >&2
            echo "the sandbox argv (bwrap older than 0.8, or a support probe that could" >&2
            echo "not run). It is refused anyway. A task runs on the machine the daemon" >&2
            echo "runs on, where this tier would claim every core beside it, and the" >&2
            echo "sandbox masks the database directories, keeps config out of view and" >&2
            echo "routes the network through an allowlist — so the suite would go red on" >&2
            echo "the sandbox rather than tell you anything about it." >&2
            ;;
        stuck)
            echo "Native mode cannot be vouched for: the nested-namespace probe did not" >&2
            echo "return within 5 seconds, so this says nothing either way. It is" >&2
            echo "refused regardless, for the reasons above." >&2
            ;;
        *)
            echo "Native mode is closed too: it needs bwrap, and there is none on PATH" >&2
            echo "in here to run or to probe." >&2
            ;;
    esac
    echo "" >&2
    echo "This is not a test failure. Nothing is broken and nothing is red." >&2
    echo "No value of ISTOTA_LINUX_TIER_MODE opens either route." >&2
    echo "Say in the merge request that the change touches the sandbox and that" >&2
    echo "the linux tier is out of reach from a task, and ask for the run before" >&2
    echo "merge. See docs/development/testing.md, 'Deployment tiers'." >&2
    # 75, not 1: the tier did not run, which is a different thing from the tier
    # running and going red. 1 is what a real failure exits with here — the
    # daemon precheck below, the bwrap probe, a failing suite — so reusing it
    # would leave a caller unable to tell "out of reach" from "broken", which
    # is the confusion this whole change exists to remove. `scripts/qtest`
    # already uses 75 for "no slot came free and the command did not run".
    exit 75
fi

# ---------------------------------------------------------------------------
# Mode resolution. Nothing below this block may call Docker until the mode is
# known: a Linux workstation that lands in native mode need not have a daemon
# at all, and prechecking for one would refuse a run that was about to work.
# ---------------------------------------------------------------------------

tier_mode="${ISTOTA_LINUX_TIER_MODE:-auto}"
case "$tier_mode" in
    auto|native|container) ;;
    *)
        echo "error: ISTOTA_LINUX_TIER_MODE='${tier_mode}' is not a mode." >&2
        echo "       Use auto (the default), native, or container." >&2
        # 1, not 75: a typo is a broken invocation, not a tier out of reach.
        exit 1
        ;;
esac

# An *additional* path to treat as evidence of a deployment. Additive and
# never a replacement: this is the input to a safety guard, and a variable that
# swapped the list out would be one `export` between an operator and an
# unsandboxed, core-claiming run beside a live daemon — while the refusal
# below calls its two ways on "neither reachable by forgetting". It exists so
# the tests can name a path that definitely does exist without writing to
# `/etc`; making the guard stricter is the only thing it can do.
EXTRA_DEPLOYMENT_CONFIG="${ISTOTA_LINUX_TIER_DEPLOYMENT_CONFIG:-}"

host_is_linux() {
    [ "$(uname -s 2>/dev/null || echo unknown)" = "Linux" ]
}

# Affirmative set only, never bare truthiness. `ISTOTA_LINUX_TIER_PRINT_MODE=0`
# left exported in a shell would otherwise print a word, run nothing and exit
# 0 — a tier that silently did not run, which is the failure the sentinel and
# the bwrap probes exist to prevent. Same parse as `ISTOTA_UPDATE_GOLDEN` and
# `PRECOMMIT_SCANS_REQUIRED`, including refusing a value it cannot read.
is_affirmative() {
    case "$1" in
        1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
        ""|0|false|FALSE|False|no|NO|No|off|OFF|Off) return 1 ;;
    esac
    echo "error: ${2}='${1}' is neither affirmative nor negative." >&2
    echo "       Use 1/true/yes/on or 0/false/no/off." >&2
    exit 1
}

# Empty output means bwrap works here; anything else is the reason it does not.
#
# Two probes, not one, for the same reason container mode runs two:
# `--unshare-user` and `--unshare-net` fail for different reasons, so folding
# them into a single invocation would let a passing user-namespace probe vouch
# for a network namespace that cannot come up. They stay separate in the other
# direction too, though the mechanism differs from the container's: there,
# adding `--unshare-user` to the network probe would hand bwrap a namespace
# where CAP_NET_ADMIN comes free and the probe would pass on a host where the
# real sandbox still fails. Here bwrap is unprivileged and unshares the user
# namespace regardless, so the network probe is honest either way — but it is
# still asking a second question, and the answer is still worth having on its
# own.
host_bwrap_failure() {
    local out
    if ! command -v bwrap >/dev/null 2>&1; then
        echo "no bwrap on PATH"
        return 0
    fi
    if ! out="$(bwrap --unshare-user --ro-bind / / -- true 2>&1)"; then
        echo "bwrap cannot create a user namespace: ${out}"
        return 0
    fi
    if ! out="$(bwrap --unshare-net --ro-bind / / -- true 2>&1)"; then
        echo "bwrap cannot bring up a network namespace: ${out}"
        return 0
    fi
    return 0
}

# Is this machine running the bot? Three arms, because no one of them sees
# every install shape, and the direction that matters is the false *negative*
# — a "no" here on a machine that is running the daemon is what puts an
# unsandboxed suite next to it. A false positive costs one environment
# variable, which the refusal names.
#
# **The units cannot be named in advance.** The Ansible role writes
# `/etc/systemd/system/{{ istota_namespace }}-scheduler.service` and the same
# for `-web`, `-webhooks`, `-devbox-proxy@` and `-devbox-iptables`, and
# `istota_namespace` is an inventory variable — so a
# probe asking `is-active istota-scheduler.service` by name is blind on every
# install that set it, which includes the production host. Enumerate what is
# running and match on the suffix instead. Both managers, since a per-user
# install has its units under `--user`.
#
# Captured into a variable rather than piped into `grep -q`: `grep -q` exits on
# the first match and SIGPIPEs the producer, which under `set -o pipefail`
# reads as a failed probe — the same trap documented on the `--progress` flag
# selector below, and here it would fail in the unsafe direction.
#
# **The config is not at one known path either.** The Ansible role renders it
# to `{{ istota_repo_dir }}/config/config.toml`, under a home this script
# cannot guess; what it can check is the two search-order paths from
# `config.py` that are absolute or HOME-relative. `~/.config/istota/config.toml`
# is what `istota setup` writes (`setup_wizard.DEFAULT_CONFIG_PATH`) and is the
# shape most likely to be on a Linux workstation, where it has no unit at all.
#
# The third arm is the Docker shape, whose config lives inside a volume and
# whose units are nowhere. `docker ps` is guarded on the binary being present
# and its failure is ignored, so a host with no daemon pays nothing.
host_runs_deployment() {
    local candidate
    for candidate in \
        "$EXTRA_DEPLOYMENT_CONFIG" \
        /etc/istota/config.toml \
        "${XDG_CONFIG_HOME:-${HOME:-/nonexistent}/.config}/istota/config.toml"; do
        if [ -n "$candidate" ] && [ -f "$candidate" ]; then
            return 0
        fi
    done

    local units manager
    if command -v systemctl >/dev/null 2>&1; then
        for manager in --system --user; do
            units="$(systemctl "$manager" list-units --type=service --state=active \
                --no-legend --no-pager 2>/dev/null || true)"
            # `-docker-proxy@` is a *retired* unit and is still matched on
            # purpose: the role tears those down, but a host that has not run
            # the play since is exactly one running the daemon, and a false
            # negative here is what puts an unsandboxed suite beside it.
            case "$units" in
                *-scheduler.service*|*-webhooks.service*|*-devbox-proxy@*|\
                *-docker-proxy@*|*-devbox-iptables.service*)
                    return 0
                    ;;
            esac
        done
    fi

    local containers
    if command -v docker >/dev/null 2>&1; then
        containers="$(docker ps --format '{{.Image}} {{.Names}}' 2>/dev/null || true)"
        case "$containers" in
            *istota*) return 0 ;;
        esac
    fi

    return 1
}

refuse_on_deployment_host() {
    echo "scripts/test-linux.sh will not pick native mode on a host running the" >&2
    echo "deployment." >&2
    echo "" >&2
    echo "Unsandboxed and correct is not the same as safe. The native tier spawns" >&2
    echo "real bwrap namespaces, claims every core through pytest's -n auto, and" >&2
    echo "runs beside the daemon's own per-task cgroups. This machine looks like a" >&2
    echo "deployment: an istota-shaped unit is active, or a config.toml is installed," >&2
    echo "or a container of the stack is running." >&2
    echo "" >&2
    echo "This is not a test failure. Nothing is broken and nothing is red." >&2
    echo "Two ways on, both deliberate and neither reachable by forgetting:" >&2
    echo "  ISTOTA_LINUX_TIER_MODE=container  build and run the image here instead" >&2
    echo "  ISTOTA_LINUX_TIER_MODE=native     run natively anyway, having read the above" >&2
    # 75 for the same reason the sandbox refusal uses it: the tier did not run,
    # which is a different thing from the tier running and going red.
    exit 75
}

bwrap_failure=""
case "$tier_mode" in
    auto)
        if ! host_is_linux; then
            tier_mode="container"
        else
            bwrap_failure="$(host_bwrap_failure)"
            if [ -n "$bwrap_failure" ]; then
                tier_mode="container"
            elif host_runs_deployment; then
                refuse_on_deployment_host
            else
                tier_mode="native"
            fi
        fi
        ;;
    native)
        if ! host_is_linux; then
            echo "error: ISTOTA_LINUX_TIER_MODE=native, but this host is not Linux." >&2
            echo "       Unset it, or use ISTOTA_LINUX_TIER_MODE=container." >&2
            exit 1
        fi
        bwrap_failure="$(host_bwrap_failure)"
        if host_runs_deployment; then
            echo "warning: this host looks like a deployment and native mode was asked" >&2
            echo "         for explicitly. The suite will claim every core and create" >&2
            echo "         real namespaces beside the running daemon." >&2
        fi
        ;;
esac

if is_affirmative "${ISTOTA_LINUX_TIER_PRINT_MODE:-}" ISTOTA_LINUX_TIER_PRINT_MODE; then
    echo "$tier_mode"
    exit 0
fi

# ---------------------------------------------------------------------------
# Shared between the two modes. Everything that makes the tier mean something
# is duplicated the moment there are two ways to run it, so the pieces that
# must not drift are written once, here, above the branch.
# ---------------------------------------------------------------------------

# Run the suite *including* the linux tests. Without this the driver would
# inherit pyproject's addopts, which deselects `linux` — the exact tests the
# driver exists to run.
#
# Prepended rather than conditional: pytest's `-m` is last-wins, so a user's
# own `-m` still overrides this in every spelling (`-m x`, `-m=x`, `-mx`). The
# version of this that tried to detect an incoming `-m` matched two spellings
# pytest does not accept and missed one it does, and could not have mattered
# either way.
#
# The expression is the same deselection set as pyproject's addopts, restated
# because there is no way to say "the default expression, plus linux".
# tests/test_linux_runner.py fails if this set ever falls behind addopts —
# which is the direction that would silently start running a marker meant to be
# off by default — and it reads the first `default_markers=` in the file, so
# there must never be a second one inside a branch for it to fall behind.
default_markers='linux or (not integration and not live and not image and not smoke and not full and not testbed and not deploy and not ml)'
pytest_args=(-m "$default_markers" "$@")

# ---------------------------------------------------------------------------
# Container mode.
# ---------------------------------------------------------------------------

# Quiet by default: a cached build is fourteen CACHED lines ahead of the test
# output every run. ISTOTA_TEST_BUILD_PROGRESS=plain when a build is what you
# are debugging. A failing build still prints its error either way.
#
# `--progress` is a BuildKit flag, and the legacy builder refuses it outright
# ("unknown flag: --progress") before it reads the Dockerfile. That is not a
# hypothetical path: `DOCKER_BUILDKIT=0` is how a host whose default buildx
# builder is a `docker-container` driver that cannot reach the daemon gets a
# build at all, so the one configuration that needs the legacy builder was the
# one this script refused to build on (ISSUE-293).
#
# Ask the CLI which build it is about to run rather than inferring it from a
# daemon version: `docker build --help` is the same switch the CLI itself makes
# on DOCKER_BUILDKIT, so it answers the actual question. Confirmed against
# Docker 29.6.2 (build dfc4efb): the help lists `--progress` under the default
# builder and does not list it under `DOCKER_BUILDKIT=0`. Captured rather than
# piped into grep, because `grep -q` exits on the first match and SIGPIPEs the
# producer — under `set -o pipefail` that reads as a failed probe and would
# silently drop the flag on a BuildKit host.
#
# NUL-delimited, not newline: `ISTOTA_TEST_BUILD_PROGRESS` is read from the
# environment, and a value containing a newline would otherwise arrive as two
# array elements — `docker build --progress pl ain -f …` names a second build
# context and fails obscurely. A value with a space is already safe by way of
# `IFS=`; this covers the other one. `read -r -d ''` works on bash 3.2.
build_progress_args() {
    local help_text
    help_text="$(docker build --help 2>/dev/null || true)"
    case "$help_text" in
        *--progress*) printf '%s\0%s\0' --progress "${ISTOTA_TEST_BUILD_PROGRESS:-quiet}" ;;
    esac
}

# In a linked worktree, `.git` is a *file* pointing at a gitdir outside the
# checkout, so binding the checkout alone leaves every git command run from the
# container's working directory failing with "fatal: not a git repository" and
# exit 128 — including the ones tests inherit their cwd for
# (`git_remote_scrub`, the private-data scanner). Bind the common gitdir at the
# same absolute path so the pointer resolves. In an ordinary clone the common
# dir is inside the checkout and this adds nothing. Native mode needs none of
# it: there is no bind and the checkout is where git already expects it.
git_common_dir=""
resolve_git_common_dir() {
    if git_common_dir="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"; then
        case "$git_common_dir" in
            # Already inside the checkout — an ordinary clone, nothing to add.
            "$REPO_ROOT"/*) git_common_dir="" ;;
            # A colon would be read as the separator in `src:dst:ro` and bind
            # something else entirely. Rare enough to decline rather than escape.
            *:*) git_common_dir="" ;;
        esac
        # `rev-parse` reports what the gitfile *says*, not what exists, so a
        # stale `commondir` yields a path it returns with exit 0 — and `docker
        # run -v` creates a missing source as a root-owned empty directory
        # rather than failing. The result would be an empty bind, the same "not
        # a git repository" as before, and a stray directory on the host.
        if [ -n "$git_common_dir" ] && [ ! -d "$git_common_dir" ]; then
            echo "warning: git reports a common dir that does not exist: $git_common_dir" >&2
            echo "         not binding it; git commands inside the runner may fail." >&2
            git_common_dir=""
        fi
    else
        git_common_dir=""
    fi
}

run_in_container() {
    # --tmpfs /tmp because the source bind is read-only and pytest's tmp_path,
    # the sandbox probes and the uv cache all need somewhere to write.
    #
    # There is deliberately no `-e PYTHONPATH` here (ISSUE-398). The image puts
    # the project on /venv's import path as an entry naming /src/src — the bind
    # below, which is the only thing making that path true, so the two move
    # together — and `istota` is therefore importable by that interpreter in
    # every child however its environment was built. Setting the variable as
    # well would be a second mechanism that only reaches processes inheriting
    # this one's environment, which is not the tool-server spawn: that one gets
    # the task env, and `build_clean_env` builds it from scratch. It would mask
    # a broken install for most of the suite and for none of the native brain.
    #
    # The narrowing this costs, so it is written down rather than discovered:
    # only /venv/bin/python can import the project now. The base image's
    # /usr/local/bin/python3 cannot, where the variable used to serve every
    # interpreter. Nothing needs it today — every in-repo spawn of the package
    # is `[sys.executable, "-m", …]`, and /venv/bin precedes /usr/local/bin on
    # the image PATH so a bare `python3` resolves to the venv anyway.
    #
    # A git identity as environment variables rather than a global config: a
    # dozen tests build throwaway repositories and commit into them, and they
    # pass on a developer host only because that host has a `user.email` set.
    # `GIT_AUTHOR_*` survives the tests that repoint HOME, which a
    # `git config --global` in the image would not.
    local gitdir_bind=()
    if [ -n "$git_common_dir" ]; then
        gitdir_bind=(-v "$git_common_dir:$git_common_dir:ro")
    fi
    # --init puts a real reaper at PID 1. Without it PID 1 is pytest, which
    # reaps only its own children, so an orphaned grandchild stays a zombie —
    # and `os.kill(pid, 0)` on a zombie succeeds. The process-group and qtest
    # tests then report "the grandchild survived the group kill" when what
    # survived is a defunct entry nobody collected. On a real host systemd
    # does this job.
    # `${a[@]+"${a[@]}"}` rather than `"${a[@]}"`: on bash 3.2 — which is what
    # /bin/bash is on macOS, the platform this script exists for — expanding an
    # empty array under `set -u` is a fatal "unbound variable". The array is
    # empty in an ordinary clone, so the plain form worked in a linked worktree
    # and died in the normal checkout. It dies inside a `$( )` too, so the
    # first thing it breaks is the bwrap probe, which then reports a namespace
    # failure that never happened.
    docker run --rm --init \
        ${gitdir_bind[@]+"${gitdir_bind[@]}"} \
        --cap-add=SYS_ADMIN \
        --cap-add=NET_ADMIN \
        --security-opt seccomp=unconfined \
        --security-opt apparmor=unconfined \
        -v "$REPO_ROOT:/src:ro" \
        -e ISTOTA_LINUX_TIER=1 \
        -e GIT_AUTHOR_NAME=istota-test -e GIT_AUTHOR_EMAIL=test@istota.invalid \
        -e GIT_COMMITTER_NAME=istota-test -e GIT_COMMITTER_EMAIL=test@istota.invalid \
        -v "$CACHE_VOLUME:/uv-cache" \
        --tmpfs /tmp:exec \
        -w /src \
        "$IMAGE_TAG" "$@"
}

run_container_tier() {
    if ! docker version >/dev/null 2>&1; then
        echo "scripts/test-linux.sh needs a running Docker daemon." >&2
        echo "This is a discretionary tier — 'uv run pytest' on the host does not need it." >&2
        exit 1
    fi

    resolve_git_common_dir

    # The capability grants below are what let bwrap create namespaces inside
    # the container. --cap-add=SYS_ADMIN with an unconfined seccomp profile is
    # close to host-equivalent on a Linux Docker host, and bounded by the VM on
    # Docker Desktop. They exist for this local test runner and nowhere else:
    # they must never appear in a compose file that could be pointed at a real
    # deployment.
    #
    # NET_ADMIN is the one that is not obvious. On the deployment the daemon
    # runs unprivileged, so bwrap creates a user namespace and holds
    # CAP_NET_ADMIN inside it — enough to bring up the loopback interface
    # `--unshare-net` requires. In this container bwrap runs as real root and
    # therefore skips the user namespace, so the capability has to come from
    # Docker instead. Without it every network-isolated sandbox dies at startup
    # with "bwrap: loopback: Failed RTM_NEWADDR: No child processes".
    echo "note: this runner grants CAP_SYS_ADMIN + CAP_NET_ADMIN and unconfined" >&2
    echo "      seccomp/apparmor so bwrap can create namespaces. Local test runner" >&2
    echo "      only — never a deployment." >&2

    local progress_args=() progress_arg
    while IFS= read -r -d '' progress_arg; do
        progress_args+=("$progress_arg")
    done < <(build_progress_args)

    docker build ${progress_args[@]+"${progress_args[@]}"} \
        -f "$REPO_ROOT/docker/test/Dockerfile" -t "$IMAGE_TAG" "$REPO_ROOT"

    docker volume create "$CACHE_VOLUME" >/dev/null

    # Fail loudly, not silently, when the namespace cannot be created. A skip
    # on the one layer built to end silent non-execution would repeat the
    # original defect exactly. A developer who knowingly cannot run bwrap here
    # sets ISTOTA_ALLOW_NO_BWRAP=1 and gets the rest of the suite.
    #
    # Individual `linux`-marked tests still skip when collected outside this
    # driver (a bare `pytest -m linux` on darwin) — that is a different
    # situation from the driver claiming success. Inside the driver they cannot
    # skip at all: ISTOTA_LINUX_TIER=1, set on every container above, turns
    # their skip guard into a hard failure. Without that the two questions
    # could drift apart — these probes ask about `--unshare-user` and
    # `--unshare-net`, while the tests guard on `_bwrap_available()`, which
    # probes neither — and every linux test could skip itself while the driver
    # reported a clean run.
    #
    # Two probes, not one. `--unshare-user` and `--unshare-net` fail for
    # different reasons and only the first is fixed by CAP_SYS_ADMIN, so
    # folding them into a single bwrap invocation would let a passing
    # user-namespace probe vouch for a network namespace that cannot come up.
    # They must also stay separate in the other direction: adding
    # `--unshare-user` to the network probe makes bwrap create a user namespace
    # where CAP_NET_ADMIN comes free, so the probe would pass on a host where
    # the real sandbox — which does not pass `--unshare-user` here, because
    # `--disable-userns` is unsupported in a container — still fails.
    local probe_failure="" probe_output=""
    if ! probe_output="$(run_in_container bwrap --unshare-user --ro-bind / / -- true 2>&1)"; then
        probe_failure="bwrap cannot create a user namespace: ${probe_output}"
    elif ! probe_output="$(run_in_container bwrap --unshare-net --ro-bind / / -- true 2>&1)"; then
        probe_failure="bwrap cannot bring up a network namespace: ${probe_output}"
    fi
    report_bwrap_failure "$probe_failure"

    # pytest writes its cache beside the rootdir, and the rootdir is the
    # read-only bind. Redirect it rather than disabling the cache plugin, so
    # --lf and --ff still work inside the runner.
    #
    # ruff first: a lint failure is cheaper to read before a suite's worth of
    # output, and this is the run that sees the tree as Linux sees it.
    # The cgroup setup is sourced, not run: it exports ISTOTA_TEST_CGROUP_ROOT
    # into the pytest process, and it has to happen inside the container
    # because what it builds is that container's own cgroup subtree. It never
    # fails the run — a Docker that cannot delegate cgroups is a limitation of
    # the machine, and the tests skip themselves there rather than taking the
    # whole tier down with them.
    run_in_container sh -c '. /src/scripts/dev/linux-tier-cgroup.sh
ruff check --output-format concise src tests testbed docker/browser docker/devbox docker/istota scripts && exec pytest "$@"' \
        -- -o cache_dir=/tmp/pytest_cache "${pytest_args[@]}"
}

# ---------------------------------------------------------------------------
# Native mode.
# ---------------------------------------------------------------------------

run_native_tier() {
    # Container mode is cwd-immune by construction — the bind, the `-f` path
    # and `-w /src` are all absolute. Native mode is not: `uv` walks up from
    # the process's cwd to find a project, and `ruff check … src tests testbed
    # docker/devbox` names four relative paths. Run from inside another checkout, the driver
    # would lint and collect that one and report the answer as this
    # repository's.
    cd "$REPO_ROOT"

    # The image installs these; a host may not have them, and each failure
    # reads as a code regression rather than as a missing package. `procps`
    # (for `ps`) is the one that bites hardest — the process-group and qtest
    # tests exit 127 without it.
    local tool missing=()
    for tool in uv git sqlite3 tmux ps; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            missing+=("$tool")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        echo "error: native mode needs these on PATH and they are missing: ${missing[*]}" >&2
        echo "       Debian: apt install bubblewrap git sqlite3 tmux procps" >&2
        echo "       Or use ISTOTA_LINUX_TIER_MODE=container, which installs them itself." >&2
        exit 1
    fi

    report_bwrap_failure "$bwrap_failure"

    # The same four the container sets, and for the same reason: a dozen tests
    # build throwaway repositories and commit into them, and they pass on a
    # developer host only because that host has a `user.email` set. A fresh VM
    # or a CI runner has none. `GIT_AUTHOR_*` also survives the tests that
    # repoint HOME, which a `git config --global` would not.
    export GIT_AUTHOR_NAME=istota-test GIT_AUTHOR_EMAIL=test@istota.invalid
    export GIT_COMMITTER_NAME=istota-test GIT_COMMITTER_EMAIL=test@istota.invalid

    # The cgroup helper is deliberately not sourced here, and this is the one
    # thing native mode must never borrow from the container.
    # `scripts/dev/linux-tier-cgroup.sh` remounts /sys/fs/cgroup read-write,
    # moves every pid in the root cgroup into a `supervisor` leaf and writes
    # cgroup.subtree_control. In a throwaway container that is the point of the
    # file; on a real host it rearranges the machine's own cgroup tree, and on
    # a deployment that tree is where the daemon's per-task cgroups live
    # (`task_cgroup.py`, ISSUE-285). So ISTOTA_TEST_CGROUP_ROOT stays unset and
    # the cgroup tests skip themselves, which is already their documented
    # best-effort behaviour. A developer who wants them runs this under
    # `systemd-run --user -p Delegate=yes`.
    #
    # `unset`, not merely "not set by us": those tests treat the variable as a
    # promise and fail rather than skip when it is present and unusable, so a
    # value left exported in the calling shell would turn the documented skip
    # into a red suite.
    echo "note: running natively on this host — no container, no image build." >&2
    echo "      The cgroup tests will skip: their subtree is built only inside the" >&2
    echo "      container, because on a real host that setup rearranges the" >&2
    echo "      machine's own cgroup tree. Run under 'systemd-run --user -p" >&2
    echo "      Delegate=yes' if you want them." >&2

    unset ISTOTA_TEST_CGROUP_ROOT
    export ISTOTA_LINUX_TIER=1

    # ruff first, same as container mode and for the same reason: a lint
    # failure is cheaper to read before a suite's worth of output.
    #
    # `--frozen` on both: without it `uv run` may re-resolve and rewrite
    # `uv.lock`, which is a tracked file, so a test run would leave a diff
    # behind. It does not install anything the venv is missing either way —
    # `uv sync --extra test` is the prerequisite, stated in the header.
    uv run --frozen ruff check --output-format concise src tests testbed docker/browser docker/devbox docker/istota scripts
    exec uv run --frozen pytest "${pytest_args[@]}"
}

# Shared by both modes so the escape hatch and the wording cannot drift.
report_bwrap_failure() {
    local failure="$1"
    if [ -z "$failure" ]; then
        return 0
    fi
    if [ "${ISTOTA_ALLOW_NO_BWRAP:-}" = "1" ]; then
        echo "warning: ${failure}" >&2
        echo "         ISTOTA_ALLOW_NO_BWRAP=1 is set, so the linux-marked tests will" >&2
        echo "         skip themselves and the rest of the suite runs." >&2
        return 0
    fi
    echo "error: ${failure}" >&2
    echo "       The linux tier exists to execute the sandbox path, so this is a" >&2
    echo "       failure, not a skip." >&2
    echo "       Set ISTOTA_ALLOW_NO_BWRAP=1 to run the rest of the suite anyway." >&2
    exit 1
}

if [ "$tier_mode" = "native" ]; then
    run_native_tier
else
    run_container_tier
fi
