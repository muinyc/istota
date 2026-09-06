"""Code review skill CLI.

`istota-skill code_review run --worktree <path> [--base <ref>] [--range <r>]
[--intent <text>] [--agents both]` assembles a review of a branch diff and runs
it past one or two text-only reviewers through the configured brain.

Where this runs matters more than what it does. The skill proxy spawns the
module *outside* the sandbox with the daemon's filesystem view, so `load_config`,
`make_brain` and the worktree are all reachable here and none of them is
reachable from the model. Everything the reviewers see is assembled by
`engine.py` from the repository; the caller supplies a path, a range and a line
of intent, and nothing else. A model-authored prompt never becomes a
daemon-side read.

Four things gate a run before a single token is spent, and all four are in
`cmd_run` rather than spread across the engine:

* `developer.enabled`, a non-empty `repos_dir`, and `developer.review.enabled`.
* `config.is_admin(ISTOTA_USER_ID)`. This **fails open** — `is_admin` returns
  True when no admins file exists — and that is correct here, because it matches
  the sandbox bind exactly: on such a deployment every user already gets
  `repos_dir` bound. The shared-KV gate next door deliberately fails closed; do
  not collapse the two.
* `resolve_under_repos`, which is also what `devbox cp-in` and `kv
  set --value-file` use. Containment is necessary and nowhere near sufficient —
  `repos_dir` is bound read-write into the admin sandbox, so the engine's
  hardened git runner is what stands between a contained path and a repository
  whose configuration the model wrote.
* The per-task call budget in `code_review_calls`, in the framework database
  rather than a file under `ISTOTA_DEFERRED_DIR` — that directory is writable
  from the sandbox, so a loop that reached a file-backed cap could delete the
  counter and carry on spending.

The budget also decides one thing that is not a gate. A reviewer may ask to see
files it was not given, which costs a second model round, so the offer is made
only when the remaining budget can pay for one — otherwise the reviewer spends
its answer on a request the CLI would refuse. See `engine.collect_needed_files`
for what may be served and `engine._round_trip` for why there is exactly one.

Heavy imports (`config`, `brain`, `db`) are function-local so the module stays
cheap to import and so tests can patch them at their real home.
"""

import argparse
import time
import logging
import os
import sys
from pathlib import Path

from istota.skill_host_paths import developer_repos_root, resolve_under_repos
from istota.skills._cli import emit, run_skill_cli

from . import engine

logger = logging.getLogger(__name__)

# Context assembly, prompt building and merging happen outside any agent's
# timeout, so the command's own wall time is `timeout_seconds` plus this. The
# proxy kills the command at `security.skill_proxy_timeout`, and an operator who
# raises `timeout_seconds` past that ceiling should learn about it from a
# startup warning rather than from a review that dies half-finished.
ASSEMBLY_ALLOWANCE_SECONDS = 60

# Floor for the clamp above. A proxy ceiling tighter than the assembly allowance
# would otherwise hand an agent zero or negative seconds, which is not a shorter
# review but no review at all.
MIN_AGENT_TIMEOUT_SECONDS = 30


def _emit(envelope: dict, code: int):
    """The facade contract: one line of JSON on stdout, then an exit code.

    The shared `emit`'s status rule is not enough on its own here: `_skip`
    exits 0 on an envelope that is deliberately not an error, so the code stays
    explicit and the status check is switched off.
    """
    emit(envelope, indent=None, ensure_ascii=True, exit_on_error=False)
    sys.exit(code)


def _fail(reason: str, message: str, **extra):
    """Something is wrong with the *request*, so the workflow blocks the push.

    Logged with the task id and the rejected input: a guard refusal with neither
    is a line an operator cannot act on.
    """
    logger.warning(
        "code_review refused (task=%s, reason=%s): %s",
        os.environ.get("ISTOTA_TASK_ID", "-"), reason, message,
    )
    _emit({"status": "error", "reason": reason, "error": message, **extra}, 1)


def _skip(reason: str, message: str, **extra):
    """A state of the *environment* rather than of the diff.

    Exit 0 and `skipped`, never `error`. The workflow does not block a push on
    these, because none of them resolves by refusing to push — and a review that
    errors *does* block, so misfiling one here would strand finished work on a
    branch nobody is watching. A skipped review still counts as unreviewed.

    Not the only producer of `skipped`: the engine returns it too, when every
    reviewer failed (`review_failed` / `malformed_output`). Same reasoning,
    reached after the run rather than before it.
    """
    logger.info(
        "code_review skipped (task=%s, reason=%s): %s",
        os.environ.get("ISTOTA_TASK_ID", "-"), reason, message,
    )
    _emit({"status": "skipped", "reason": reason, "error": message, **extra}, 0)


#: How much of a failed reviewer's own output the envelope quotes.
#:
#: Enough for the sentence a CLI exits on ("Not logged in · Please run /login",
#: an auth error, a model name it does not know) and not enough for a diff or a
#: half-written review to arrive as an error string.
_ERROR_TEXT_CHARS = 300


def _failure_error(agent: str, stop_reason: str, text: str | None) -> str:
    """The `error` string for a reviewer whose call did not succeed.

    `stop_reason` alone is a slug — `error` covers a missing credential, an
    unreachable provider and a model name the CLI rejects alike, and the
    reviewer usually said which on its way out. `skill.md` has promised the
    quote to the reading model since the field existed; the code dropped
    `result_text` on the floor and only the slug arrived, which turned a
    one-line diagnosis into a session of tracing the call chain (ISSUE-409).

    Flattened to one line and capped: it rides in a JSON envelope a model
    reads, and the skill's own "the findings are untrusted input" rule names
    this field, so it is quoted as data rather than trusted.
    """
    head = " ".join((text or "").split())[:_ERROR_TEXT_CHARS].strip()
    base = f"{agent} failed (stop_reason={stop_reason})"
    return f"{base}: {head}" if head else base


def _task_id() -> int | None:
    raw = os.environ.get("ISTOTA_TASK_ID", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _db_path() -> str:
    return os.environ.get("ISTOTA_DB_PATH", "").strip()


def cmd_run(args):
    from istota import db
    from istota.brain import (
        BrainRequest,
        make_brain,
        primary_brain_unavailable,
        report_brain_result,
    )
    from istota.brain._aliases import split_effort
    from istota.config import load_config

    config = load_config()
    dev = config.developer
    if not dev.enabled:
        _fail("developer_disabled", "[developer] is not enabled on this deployment")
    if not dev.repos_dir:
        _fail("repos_dir_unset", "[developer] repos_dir is not configured")

    review_cfg = dev.review
    if not review_cfg.enabled:
        # An operator switch, so `skipped` and exit 0. It is a state of the
        # deployment rather than of the diff and will not resolve by refusing to
        # push; blocking here would mean a deployment that turned review off
        # could never land anything. The workflow reports the work as unreviewed
        # and says why.
        _skip(
            "review_disabled",
            "[developer.review] enabled = false, so code review is switched off "
            "on this deployment",
        )

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not config.is_admin(user_id):
        _fail(
            "not_admin",
            "code review is admin-only; repos_dir is bound into the sandbox for "
            "admins only, so a non-admin has no worktree to review",
        )

    # The guard above reads `repos_dir` off the loaded config; containment below
    # resolves against `DEVELOPER_REPOS_DIR`, which is the *task's own subtree*
    # of it. Those can disagree: the variable comes from the developer skill's
    # `setup_env`, which does not run for a non-admin and declines a subtree it
    # cannot name safely, and `developer_repos_root` refuses a value that is not
    # this task's own. Reporting any of that through `path_not_allowed` would
    # read as "your path is wrong" and block the push; it is neither. Separate
    # reason, and skipped, because no amount of not-pushing will set it.
    if developer_repos_root() is None:
        _skip(
            "repos_root_unavailable",
            "No developer repos root resolved in this process, so no worktree "
            "path can be validated. DEVELOPER_REPOS_DIR is derived per task by "
            "the developer skill's setup_env and must name this task's own "
            "subtree (ISTOTA_USER_ID); check that both are set.",
        )

    worktree, error = resolve_under_repos(args.worktree)
    if error:
        _fail("path_not_allowed", error)

    # No text-only path on tmux at all, so there is nothing to construct. This
    # is a property of the deployment and will not change by retrying.
    if config.brain.kind == "tmux_claude":
        _skip(
            "brain_unsupported",
            "the tmux_claude brain has no text-only call path, so no reviewer "
            "can be driven on this deployment",
        )

    # Before the cap and the breaker, so an operator whose budget cannot fit
    # learns about it even on a run those short-circuit — a warning that only
    # fires on the runs that were going to work is not much of a warning.
    proxy_ceiling = config.security.skill_proxy_timeout
    # Coerced rather than trusted: nothing in the loader validates either value,
    # and a float from the TOML would land in the envelope as a float where the
    # doc promises whole seconds.
    configured = int(review_cfg.timeout_seconds)
    # Only the non-positive case is floored. A `timeout_seconds` of 0 or less
    # otherwise reaches the brains, which disagree about what it means — the
    # native one runs unbounded until the proxy kills the command, `claude_code`
    # hands it to a `threading.Timer` and kills each agent at once — and neither
    # is a review. A small *positive* budget is left alone: it is a choice an
    # operator can legitimately make, and raising it would mean overriding the
    # number the envelope reports in the same breath as reporting it.
    agent_timeout = configured if configured > 0 else MIN_AGENT_TIMEOUT_SECONDS
    # `> 0` rather than a truthiness test: a non-positive ceiling is a
    # misconfiguration the proxy surfaces on its own by killing the command
    # immediately, and reading it as "no ceiling" at least leaves the budget
    # saying what was configured instead of blaming a clamp that never applied.
    if proxy_ceiling > 0:
        # Clamped, not just warned about. Left alone, every agent would be given
        # a budget the proxy kills the whole command before it can spend, so
        # each review would die half-finished having paid for both agents.
        # Shrinking is the only outcome that returns anything.
        #
        # Downward only, and the floor bounds how far down rather than being
        # applied to the result. Written the other way round — as
        # `max(floor, ceiling - allowance)` over the configured value — it could
        # *raise* a small budget: 25s under an 85s ceiling became 30s, which is
        # not a clamp, and it made the fit strictly worse rather than better.
        ceiling_budget = max(
            MIN_AGENT_TIMEOUT_SECONDS, proxy_ceiling - ASSEMBLY_ALLOWANCE_SECONDS
        )
        agent_timeout = min(agent_timeout, ceiling_budget)
        if proxy_ceiling - ASSEMBLY_ALLOWANCE_SECONDS < MIN_AGENT_TIMEOUT_SECONDS:
            # The floor won, so the clamp could not deliver the fit it exists to
            # produce and the command will overrun the ceiling anyway. Worth its
            # own line: the caller gets no envelope at all in this case — the
            # proxy kills the command with empty stdout — so the log is the only
            # place the deployment can say what went wrong.
            logger.warning(
                "security.skill_proxy_timeout of %ss cannot fit a review at all: "
                "%ss of assembly plus the %ss agent floor needs %ss. The proxy "
                "will kill this command before it answers. Raise "
                "skill_proxy_timeout.",
                proxy_ceiling, ASSEMBLY_ALLOWANCE_SECONDS,
                MIN_AGENT_TIMEOUT_SECONDS,
                ASSEMBLY_ALLOWANCE_SECONDS + MIN_AGENT_TIMEOUT_SECONDS,
            )
    if agent_timeout < configured:
        logger.warning(
            "code_review timeout_seconds of %ss plus %ss of assembly exceeds "
            "security.skill_proxy_timeout of %ss, so each agent is being given "
            "%ss instead. Lower timeout_seconds or raise skill_proxy_timeout.",
            configured, ASSEMBLY_ALLOWANCE_SECONDS,
            proxy_ceiling, agent_timeout,
        )

    task_id = _task_id()
    db_path = _db_path()
    cap = review_cfg.max_calls_per_task
    calls_used = None
    # Distinct from `calls_used is not None`: this says a budget *applies*, not
    # that reading it worked. The two come apart on a database error and the
    # round-trip decision below turns on the difference.
    has_task_budget = task_id is not None and bool(db_path)
    if has_task_budget:
        # A read that fails must not sink a review. Losing the budget check is a
        # cost risk bounded by whatever else is wrong with the database; refusing
        # the review outright turns a transient lock into a blocked push.
        try:
            with db.get_db(db_path) as conn:
                calls_used = db.code_review_calls_get(conn, task_id)
        except Exception as exc:
            logger.error(
                "code_review could not read the call budget for task %s, "
                "proceeding uncapped: %s", task_id, exc,
            )
        # `<= 0` means no reviews, matching `max_need_files = 0` next door rather
        # than reading as "unlimited". Two adjacent knobs where 0 means opposite
        # things is a trap, and on a spend control the expensive reading is the
        # wrong one to guess at.
        if cap <= 0:
            _skip(
                "call_cap",
                f"max_calls_per_task is {cap}, so no review rounds are permitted "
                "for this task",
                calls_used=calls_used or 0,
                max_calls=cap,
            )
        if calls_used is not None and calls_used >= cap:
            _skip(
                "call_cap",
                f"this task has already spent {calls_used} review rounds, at the "
                f"max_calls_per_task cap of {cap}",
                calls_used=calls_used,
                max_calls=cap,
            )
    else:
        # An operator-driven run rather than a task's. Both variables come from
        # the proxy, not from the model, so their absence means there is no task
        # to budget against — not that a budget was evaded.
        logger.warning(
            "code_review running without a task budget (ISTOTA_TASK_ID=%r, "
            "ISTOTA_DB_PATH set=%s)",
            os.environ.get("ISTOTA_TASK_ID", ""),
            bool(db_path),
        )

    # The `need_files` round trip spends a second model round, so it is only
    # offered when the budget can pay for one. Advertising it otherwise leaves
    # two bad outcomes and no good one: overshoot the operator's cap, or refuse
    # a request the prompt had just invited after the reviewer spent its answer
    # making it. When there is no task budget at all there is nothing to
    # overshoot, so the offer stands.
    # Three states, not two, and the middle one is why this is not a single
    # `calls_used is not None` test. No task budget at all (an operator-driven
    # run) has nothing to overshoot, so the offer stands. A budget that was read
    # gates on the arithmetic. A budget whose *read failed* leaves `calls_used`
    # None with a real cap still in force — the review proceeds uncapped rather
    # than being sunk by a transient lock, but it does not also get to spend the
    # optional extra round on a budget nobody could check.
    if not has_task_budget:
        allow_need_files = True
    else:
        allow_need_files = calls_used is not None and calls_used + 2 <= cap

    available, breaker_reason = primary_brain_unavailable(config.brain)
    if not available:
        _skip(
            "brain_unavailable",
            f"the primary brain is degraded ({breaker_reason or 'cooling down'}), "
            "so the review was not attempted",
            calls_used=calls_used,
            max_calls=cap,
        )

    cwd = Path(config.temp_dir) if config.temp_dir else Path("/tmp")

    def invoke(agent: str, prompt: str, timeout: int):
        raw_model = (
            review_cfg.conformance_model
            if agent == engine.CONFORMANCE
            else review_cfg.bughunt_model
        )
        # Split here, not in the brain. `resolve_model_name` strips a `:effort`
        # tail and keeps only the base, so a configured "smart:high" handed to
        # it whole runs at default effort and silently drops the operator's
        # setting.
        base_model, effort = split_effort(raw_model)
        brain = make_brain(config.brain)
        # Imported here rather than at module scope: `executor` imports
        # `briefings.generate`, and a top-level import from any of these
        # callers risks closing a cycle back through it.
        from istota.executor import build_model_cli_env

        req = BrainRequest(
            prompt=prompt,
            allowed_tools=[],
            cwd=cwd,
            # Not `dict(os.environ)` (ISSUE-395). What that carried depends
            # on how this CLI was started: spawned by the skill proxy it holds
            # the manifest-injected provider key rather than the daemon's own
            # credentials, which `_split_credential_env` already removed; run
            # host-side directly, `os.environ` *is* the daemon environment,
            # master Fernet key and all. `build_model_cli_env` is the right
            # answer to both.
            env=build_model_cli_env(config),
            timeout_seconds=timeout,
            model=brain.resolve_model_name(base_model),
            effort=effort or "",
            streaming=False,
            on_progress=None,
            cancel_check=None,
            on_pid=None,
            sandbox_wrap=None,
            result_file=None,
        )
        primary_started_at = time.time()
        primary_started_monotonic = time.monotonic()
        result = brain.execute(req)

        # Imported here rather than at module scope: `executor` imports
        # `briefings.generate`, and a top-level import from any of these callers
        # risks closing a cycle back through it.
        from istota.executor import persist_brain_usage

        # One call per review agent, with no task row behind it. The review runs
        # up to four model invocations per round, so this is real spend.
        persist_brain_usage(
            config, None, usage=result.usage, origin="code_review",
            user_id=user_id, brain_kind=result.brain_kind,
            model=result.model_used or req.model,
            stop_reason=result.stop_reason, success=result.success,
        )

        report_brain_result(
            result, config.brain, config=config, started_at=primary_started_at,
            started_monotonic=primary_started_monotonic,
        )
        if not result.success:
            logger.error(
                "code_review %s failed (stop_reason=%s)", agent, result.stop_reason
            )
            return engine.AgentReply(
                ok=False,
                error=_failure_error(agent, result.stop_reason, result.result_text),
            )
        return engine.AgentReply(ok=True, text=result.result_text or "")

    try:
        envelope = engine.run_review(
            worktree,
            intent=args.intent or "",
            base=args.base,
            explicit_range=getattr(args, "range", None),
            forced_agents=args.agents,
            cfg=engine.ReviewConfig(
                both_agents_threshold_lines=review_cfg.both_agents_threshold_lines,
                boundary_patterns=tuple(review_cfg.boundary_patterns),
                max_diff_chars=review_cfg.max_diff_chars,
                max_context_chars=review_cfg.max_context_chars,
                max_file_chars=review_cfg.max_file_chars,
                max_callers_per_symbol=review_cfg.max_callers_per_symbol,
                max_need_files=review_cfg.max_need_files,
            ),
            invoke=invoke,
            timeout_seconds=agent_timeout,
            allow_need_files=allow_need_files,
        )
    except engine.ReviewError as exc:
        _fail(exc.reason, str(exc))

    rounds = envelope.pop("rounds", 0)
    if rounds and task_id is not None and db_path:
        # The review is already paid for by this point, so a failure to record
        # the charge must not lose it. Emitting an un-counted review is a cost
        # risk; a traceback instead of an envelope violates the facade contract
        # and hands the caller nothing at all.
        try:
            with db.get_db(db_path) as conn:
                calls_used = db.code_review_calls_increment(conn, task_id, rounds)
        except Exception as exc:
            logger.error(
                "code_review completed but could not record the call against "
                "task %s: %s", task_id, exc,
            )
    envelope["calls_used"] = calls_used
    envelope["max_calls"] = cap
    # `agent_timeout_seconds` comes back from the engine, which was handed the
    # already-clamped value. These two are what make it readable: without the
    # configured number there is nothing to compare it against, and the clamp's
    # own warning goes to the daemon journal, which the model that invoked this
    # CLI has no route to. Derived from the comparison rather than from a flag
    # set at the clamp, and `<` rather than `!=`, so that the two cases where
    # the clamp runs without costing anything — a budget already at the floor,
    # and one the floor raised — report honestly. The question a caller is
    # asking is "did this review run short", not "was the branch taken".
    envelope["agent_timeout_configured"] = configured
    envelope["agent_timeout_clamped"] = agent_timeout < configured
    if envelope["status"] != "ok":
        # The guard refusals above each log through `_fail` / `_skip`; a run that
        # got as far as calling models and came back with nothing had no line at
        # any level, because the engine does not log and `invoke` logs only a
        # call that failed — a reviewer answering unparseably is `success=True`.
        # That silence is the expensive part. A broken adapter makes *every*
        # review on the deployment come back this way (ISSUE-271), and since
        # this status no longer blocks the push, nothing else would show it: the
        # branch lands unreviewed, the breaker sees a healthy call, and a
        # scheduled review exits 0 and never trips the auto-disable counter.
        # WARNING because one of these is a bad day and a run of them is an
        # outage, and the reason slug is what tells them apart.
        logger.warning(
            "code_review returned no findings (task=%s, status=%s, reason=%s, "
            "rounds=%s): %s",
            os.environ.get("ISTOTA_TASK_ID", "-"), envelope["status"],
            envelope.get("reason", "-"), rounds,
            str(envelope.get("error", ""))[:200],
        )
    # Kept on the envelope rather than popped with the charge: it is what
    # separates a skip that spent model calls from one that refused before
    # spending any, and those two read identically otherwise.
    envelope["rounds"] = rounds
    _emit(envelope, 1 if envelope["status"] == "error" else 0)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.code_review",
        description="Review a branch diff with one or two text-only reviewers",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Review the changes in a worktree")
    p_run.add_argument(
        "--worktree",
        required=True,
        help="Path to the worktree to review. Must resolve inside $DEVELOPER_REPOS_DIR",
    )
    p_run.add_argument(
        "--base",
        help="Review <base>...HEAD. Three-dot: a two-dot range inverts every "
             "base-only commit once the base moves ahead of the branch point",
    )
    p_run.add_argument(
        "--range",
        help="An explicit range, which wins over --base. Defaults to the merge "
             "base against the tracked default branch",
    )
    p_run.add_argument(
        "--intent",
        default="",
        help="One line on what the change is meant to do, shown to the reviewers",
    )
    p_run.add_argument(
        "--agents",
        choices=["both", "conformance", "bughunt"],
        help="Force the reviewer set. Default sizes it from the diff",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {"run": cmd_run}

    def describe(exc: BaseException) -> dict:
        # The facade contract is one line of JSON and an exit code, and the
        # scheduler sniffs stdout for that shape. The engine shells out to git
        # through `subprocess.Popen`, which raises `OSError` and friends outside
        # `ReviewError`, so without this the caller gets a traceback on stderr,
        # empty stdout, and nothing it can classify.
        #
        # `_fail` emits and exits rather than returning, which is what keeps
        # this module's `reason` discriminator and its refusal log line; the
        # epilogue's own envelope below is therefore unreachable. `_emit` is
        # also how a *successful* run returns, and its `SystemExit` is not an
        # `Exception`, so it passes the epilogue untouched with no re-raise
        # clause of its own.
        logger.exception("code_review failed unexpectedly")
        _fail("internal_error", f"{type(exc).__name__}: {exc}")
        raise AssertionError("unreachable")  # pragma: no cover

    run_skill_cli(commands, args, on_exception=describe)


if __name__ == "__main__":
    main()
