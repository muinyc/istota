#!/bin/bash
# Prove the deploy tier can see a broken deployment.
#
# Every assertion in `tests/deploy/` is a claim about a container, and nothing
# inside that suite can tell a working assertion from one that matches nothing.
# That is not hypothetical here: a container that converged nothing at all still
# answers `systemctl`, still has a `/srv/app`, and still runs the command a
# skipped `bwrap` was supposed to wrap. `.claude/rules/testing.md` records eight
# separate cases of exactly that, five of them in the deployment tiers.
#
# So this is the tier's acceptance criterion, on the project's own rule: **a
# clean run here is the failure.** It converges three deliberately broken
# deployments and requires each to turn a named set of node ids red.
#
#   scripts/test-deploy-negative-control.sh            # all three controls
#   scripts/test-deploy-negative-control.sh sandbox    # just one
#
# Two of the three controls name the node ids they must turn red, and require
# them in pytest's `FAILED` summary specifically — not merely "the run went
# red", since a control can otherwise pass on an unrelated failure, and not
# `ERROR`, since a fixture that blows up turns the whole file into errors and
# would make every control pass for the wrong reason. Both breakages are chosen
# so the converge still *succeeds* and the damage shows up in the assertions,
# which is what keeps that distinction available.
#
# The third, `config`, has a different contract for a reason stated where it is
# defined below. Read that before assuming it is the odd one out by oversight.
#
# **Two things this script has already caught**, so it is not ceremony: the
# tier's sandbox probes ran as root while doctor probed as the daemon, so they
# passed on a container deliberately run without the grant that makes bwrap
# work; and the `config` control is what established that a bad render fails
# the play rather than reaching the assertions about it.
#
# Roughly three minutes per control.
#
# No arrays anywhere: macOS ships bash 3.2, where `"${empty[@]}"` under `set -u`
# is fatal, and this script's whole audience is a developer machine.
set -euo pipefail

cd "$(dirname "$0")/.."

# What each control must turn red. Deliberately not "every test that happens to
# fail" — a control is a claim about which assertions are load-bearing for which
# property, and an over-broad list stops being one.
#
#   unit     the unit file is rendered and installed and names a module that
#            does not exist, so the daemon crash-loops under Restart=always.
#   config   config.toml renders and is not TOML.
#   sandbox  the container is run without `systempaths=unconfined`, which is
#            the posture the shipped Docker stack is actually in: bwrap creates
#            a user namespace and then cannot mount a procfs inside it.
FILE="tests/deploy/test_bare_metal_deploy.py"

control_unit_nodes="
${FILE}::TestTheUnitsAreEnabledAndRunning::test_the_scheduler_unit_is_active
${FILE}::TestTheUnitsAreEnabledAndRunning::test_the_scheduler_is_not_restarting
"

# The `config` control has a different contract from the other two, and the
# reason is a property of the deployment rather than a limitation here.
#
# Breaking `config.toml.j2` so it renders invalid TOML does not reach the
# assertions in `TestTheRenderedConfigLoads` at all: the role *itself* runs
# `istota` CLI commands against the rendered config partway through the play,
# so the play fails there and the session reports twenty fixture ERRORs instead
# of two FAILEDs. That is the install failing loudly on an unloadable config,
# which is the behaviour you want — but it means this control proves the
# *fixture's* converge assertion is load-bearing rather than proving those two
# node ids can go red.
#
# Written down rather than papered over by loosening the other controls to
# accept ERROR: a fixture that blows up turns every test in the file into an
# error, so a control that accepted them would pass for the wrong reason every
# time. The two assertions in `TestTheRenderedConfigLoads` are therefore
# uncontrolled, and both fail closed — one is a positive `test -e` against a
# named absolute path, the other hands the file to the real loader and requires
# a specific value back.
control_config_marker="the role did not converge"

control_sandbox_nodes="
${FILE}::TestTheSandboxWorksHere::test_bubblewrap_can_create_a_namespace
${FILE}::TestTheSandboxWorksHere::test_the_probe_is_not_vacuous
${FILE}::TestDoctorIsTheOracle::test_the_sandbox_check_agrees_with_the_host
${FILE}::TestDoctorIsTheOracle::test_doctor_reports_no_unexpected_failure
"

# `test_the_probe_and_doctor_answer_the_same_question` is deliberately NOT in
# that list. It asserts the two readings *agree*, so a correctly broken
# container — where both say no — passes it, and it is the one assertion here
# that should. It earns its place by having gone red on the first run of this
# control, when the tier's own probe ran as root and doctor as the daemon.

if [ -n "${ISTOTA_SANDBOXED:-}" ]; then
    echo "scripts/test-deploy-negative-control.sh cannot run inside the sandbox." >&2
    echo "It runs the deploy tier three times; see scripts/test-deploy.sh for" >&2
    echo "why that tier is out of reach from a task." >&2
    exit 75
fi

if ! docker info >/dev/null 2>&1; then
    echo "scripts/test-deploy-negative-control.sh needs a running Docker daemon." >&2
    exit 1
fi

run_control() {
    breakage="$1"
    expected="$2"

    echo ""
    echo "=== control: ${breakage} ==="

    log="$(mktemp)"
    [ -n "$log" ] || { echo "mktemp failed" >&2; return 1; }
    # `|| true`: a red run is the expected outcome here, so the exit status
    # says nothing on its own and `set -e` must not act on it. What the control
    # asserts is *which* tests went red, below.
    ISTOTA_DEPLOY_TIER_BREAK="$breakage" \
        uv run pytest -m deploy -n0 -q > "$log" 2>&1 || true

    missing=""
    for node in $expected; do
        if ! grep -q "^FAILED ${node}" "$log"; then
            missing="${missing}  ${node}
"
        fi
    done

    if [ -n "$missing" ]; then
        echo "CONTROL FAILED: the '${breakage}' breakage did not turn these red:" >&2
        printf '%s' "$missing" >&2
        echo "" >&2
        echo "--- pytest output (tail) ---" >&2
        tail -40 "$log" >&2
        echo "" >&2
        echo "A clean run here means the tier would pass on the bug it exists" >&2
        echo "to catch. Do not merge on it." >&2
        return 1
    fi

    echo "ok: '${breakage}' turned all $(echo $expected | wc -w | tr -d ' ') expected node ids red"
    rm -f "$log"
    return 0
}

run_converge_control() {
    breakage="$1"
    marker="$2"

    echo ""
    echo "=== control: ${breakage} ==="

    log="$(mktemp)"
    [ -n "$log" ] || { echo "mktemp failed" >&2; return 1; }
    ISTOTA_DEPLOY_TIER_BREAK="$breakage" \
        uv run pytest -m deploy -n0 -q > "$log" 2>&1 || true

    # Three conditions, because each alone is satisfiable by an accident: the
    # converge has to have failed *for the stated reason*, the tier must not
    # have passed, and no test may have run green against a broken deployment.
    if ! grep -q "$marker" "$log"; then
        echo "CONTROL FAILED: the '${breakage}' breakage did not produce" >&2
        echo "  ${marker}" >&2
        tail -40 "$log" >&2
        return 1
    fi
    if grep -qE "(^|, )[1-9][0-9]* passed" "$log"; then
        echo "CONTROL FAILED: the '${breakage}' breakage left tests passing:" >&2
        grep -E "(^|, )[1-9][0-9]* passed" "$log" >&2
        return 1
    fi

    echo "ok: '${breakage}' failed the converge, and no test passed against it"
    rm -f "$log"
    return 0
}

requested="${1:-all}"
failures=0

case "$requested" in
    all)
        run_control unit    "$control_unit_nodes"    || failures=$((failures + 1))
        run_converge_control config "$control_config_marker" \
            || failures=$((failures + 1))
        run_control sandbox "$control_sandbox_nodes" || failures=$((failures + 1))
        ;;
    unit)    run_control unit    "$control_unit_nodes"    || failures=1 ;;
    config)  run_converge_control config "$control_config_marker" || failures=1 ;;
    sandbox) run_control sandbox "$control_sandbox_nodes" || failures=1 ;;
    *)
        echo "unknown control: $requested (want: all, unit, config, sandbox)" >&2
        exit 2
        ;;
esac

echo ""
if [ "$failures" -ne 0 ]; then
    echo "${failures} control(s) did not turn their tier red." >&2
    exit 1
fi
echo "all controls turned the tier red, as they must."
