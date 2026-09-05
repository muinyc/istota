"""CLI interface for local testing and administration."""

import argparse
import importlib.metadata
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import db
from . import user_profiles
from .user_scope import is_scopable_user_id
from .config import load_config
from .logging_setup import setup_logging
from .executor import execute_task
from .scheduler import process_one_task, check_briefings
from .email_support import get_email_config
from .transport.email import poll_emails
from .skills.email import list_emails, send_email
from .storage import (
    ensure_user_directories_v2,
    user_directories_exist_v2,
    init_user_memory_v2,
    get_memory_line_count_v2,
    get_user_base_path,
)
from .skills.calendar import (
    get_caldav_client,
    list_calendars,
    get_today_events,
    create_event,
    delete_event,
    format_event_for_display,
)
from .tasks_file_poller import (
    discover_tasks_files,
    poll_user_tasks_file,
)
from .usage_render import (
    COST_PLACEHOLDER,
    fmt_context,
    fmt_int,
    render_cost,
)


def _installed_version() -> str:
    """The installed distribution's version, or a placeholder.

    `importlib.metadata.version` raises `PackageNotFoundError` when istota is
    importable but not installed — the Linux tier's image, which reaches the
    source through a path entry in its venv and so has no distribution metadata
    for the package (ISSUE-398). That raise happened while *building the
    parser*, so it took down every command rather than only `--version`.
    """
    try:
        return importlib.metadata.version("istota")
    except importlib.metadata.PackageNotFoundError:
        return "unknown (not installed)"


def cmd_init(args):
    """Initialize the database."""
    config = load_config(Path(args.config) if args.config else None)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(config.db_path)
    print(f"Database initialized at {config.db_path}")


def cmd_doctor(args):
    """Run the runtime self-check and report.

    The operator-facing half of :mod:`istota.doctor`. Always probes: an operator
    on a host wants the binaries actually executed, unlike the config-load path
    that the ``probe`` flag exists for.

    Returns 1 if any check failed, so a script can branch on it. Warnings and
    skips are not failures — a skill that is not wired is not a broken install.
    """
    from . import doctor

    requested = Path(args.config) if args.config else None
    config = load_config(requested)
    # A run that loaded no config file describes a default `Config`, not this
    # deployment, and says so nowhere (ISSUE-412). Render the one line that says
    # it and stop: there is no subset of such a run worth reading. Inside a task
    # this is the unconditional state — the config directory is bound into no
    # sandbox. `scope` is passed because `--scope image` is the one narrowing
    # flag the gate honours rather than swallows; `config_visibility` says why.
    scope = args.scope or ""
    gate = doctor.config_visibility(config, requested=requested, scope=scope)
    results = [gate] if gate is not None else doctor.run_checks(
        config,
        only=tuple(args.only or ()),
        scope=scope,
        deep=bool(args.deep),
        probe=True,
    )
    # The renderers redact configured credential values out of `detail` and
    # `remedy` before anything is printed. `detail` carries observed paths and
    # raw exception text, and terminal output is where a pasted credential ends
    # up in a bug report.
    secrets = doctor.config_secrets(config)
    if args.json:
        print(doctor.render_json(results, secrets=secrets))
    else:
        print(doctor.render_text(results, secrets=secrets))
    return doctor.exit_code(results)


def cmd_task(args):
    """Submit a task directly."""
    config = load_config(Path(args.config) if args.config else None)

    if args.prompt:
        prompt = args.prompt
    else:
        # Read from stdin
        print("Enter task (Ctrl+D to submit):")
        prompt = sys.stdin.read().strip()

    if not prompt:
        print("Error: No prompt provided", file=sys.stderr)
        sys.exit(1)

    # Determine source type and conversation token
    if args.source_type:
        source_type = args.source_type
    elif args.conversation_token:
        source_type = "talk"
    else:
        source_type = "cli"

    use_context = not args.no_context

    # A dry run assembles the prompt, prints it, and leaves nothing behind.
    # It must short-circuit BEFORE create_task: a persisted `pending` row is
    # picked up by any running scheduler within a tick, so creating one and
    # only then consulting `--dry-run` meant the flag whose whole promise is
    # that nothing executes was queueing real, billed model work. `id=0` is
    # this codebase's marker for a task with no row behind it (the heartbeat
    # builds one the same way).
    if args.dry_run:
        with db.get_db(config.db_path) as conn:
            task = db.Task(
                id=0,
                status="pending",
                source_type=source_type,
                user_id=args.user,
                prompt=prompt,
                conversation_token=args.conversation_token,
            )
            user_resources = db.get_user_resources(conn, args.user)
            _success, result, _actions, _trace = execute_task(
                task,
                config,
                user_resources,
                dry_run=True,
                use_context=use_context,
                conn=conn,
            )
        print(result)
        return

    with db.get_db(config.db_path) as conn:
        try:
            task_id = db.create_task(
                conn,
                prompt=prompt,
                user_id=args.user,
                source_type=source_type,
                conversation_token=args.conversation_token,
            )
        except ValueError as e:
            # This entry point is one of the three the guard exists for
            # (ISSUE-402), so the refusal is intended — a traceback is not.
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Task created: {task_id}")

    if args.execute:
        # Execute immediately
        print("Executing task...")
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            if task:
                user_resources = db.get_user_resources(conn, args.user)
                # Not `dry_run=args.dry_run`: a dry run returned above, so the
                # only way to reach here is a real execution.
                success, result, _actions, _trace = execute_task(
                    task,
                    config,
                    user_resources,
                    use_context=use_context,
                    conn=conn,
                )
                if success:
                    db.update_task_status(conn, task_id, "completed", result=result)
                    print("\n--- Result ---")
                    print(result)
                else:
                    db.update_task_status(
                        conn, task_id, "failed", result=task.partial_result,
                        error=result, actions_taken=_actions,
                        execution_trace=_trace,
                    )
                    print("\n--- Error ---", file=sys.stderr)
                    print(result, file=sys.stderr)
                    sys.exit(1)


def cmd_repl(args):
    """Launch the interactive terminal assistant (full-stack, streamed)."""
    from .repl import run_session

    config = load_config(Path(args.config) if args.config else None)

    user_id = args.user
    if not user_id:
        # Default to the sole configured user, else the first admin.
        if len(config.users) == 1:
            user_id = next(iter(config.users))
        elif config.admin_users:
            user_id = sorted(config.admin_users)[0]
    if not user_id:
        print(
            "Error: could not infer a user; pass -u/--user.", file=sys.stderr,
        )
        sys.exit(1)
    # Checked once here rather than caught per turn: `db.create_task` refuses a
    # user id that cannot name a directory of its own (ISSUE-402), and a session
    # that accepted `-u` and then failed on every line is worse than one that
    # declines to start.
    if not is_scopable_user_id(user_id):
        print(
            f"Error: user {user_id!r} cannot name a per-user directory; it must "
            "be a non-empty path component other than '.' or '..'.",
            file=sys.stderr,
        )
        sys.exit(1)

    run_session(
        config,
        user_id=user_id,
        token=args.token,
        workspace=args.workspace,
        model=args.model,
        effort=args.effort,
    )


def _default_env_file(args) -> Path:
    """Where ``serve``/``setup`` read/write the secrets env file.

    Sibling to an explicit ``-c`` config file, else the standard
    ``~/.config/istota/istota.env`` (where ``istota setup`` writes it).
    """
    if getattr(args, "config", None):
        return Path(args.config).expanduser().parent / "istota.env"
    return Path.home() / ".config" / "istota" / "istota.env"


def cmd_setup(args):
    """Interactive first-run installer for the local single-user shape."""
    from . import setup_wizard

    try:
        rc = setup_wizard.run_setup(args)
    except setup_wizard.SetupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc)


def cmd_serve(args):
    """Combined local launcher: scheduler loop + web server in one process."""
    from . import serve

    # Propagate an explicit -c path to the web app, which loads its own config
    # via load_config() (no arg) in its lifespan — ISTOTA_CONFIG_PATH is the
    # documented way to point it at a non-standard config location.
    import os
    if args.config:
        os.environ["ISTOTA_CONFIG_PATH"] = str(Path(args.config).expanduser())

    # Source the secrets env file (native API key, session secret, …) BEFORE
    # load_config so its env overrides apply. Non-clobbering.
    env_file = Path(args.env_file).expanduser() if args.env_file else _default_env_file(args)
    n = serve.load_env_file(env_file)
    config = load_config(Path(args.config) if args.config else None)
    setup_logging(config, verbose=args.verbose, daemon_mode=True)
    if n:
        print(f"Loaded {n} value(s) from {env_file}")

    host = args.host or "127.0.0.1"
    port = args.port  # None → serve uses config.web.port
    try:
        serve.run_serve(config, host=host, port=port)
    except serve.ServeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        sys.exit(0)


def cmd_update(args):
    """Self-update a standalone (local) install to the latest code."""
    from . import updater

    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)
    record_path = updater.install_record_path(config_path)
    try:
        rc = updater.run_update(
            config, record_path=record_path, config_path=config_path,
            force=args.force, channel=args.channel,
        )
    except updater.UpdateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc)


def cmd_run(args):
    """Run the scheduler once (process pending tasks)."""
    config = load_config(Path(args.config) if args.config else None)

    if args.briefings:
        # Check and queue briefings
        with db.get_db(config.db_path) as conn:
            briefing_tasks = check_briefings(conn, config)
            if briefing_tasks:
                print(f"Queued {len(briefing_tasks)} briefing(s)")
            else:
                print("No briefings due")

    # Process tasks
    processed = 0
    while True:
        result = process_one_task(config, dry_run=args.dry_run)
        if result is None:
            break
        task_id, success = result
        status = "completed" if success else "failed"
        print(f"Task {task_id}: {status}")
        processed += 1

        if args.once:
            break

    if processed == 0:
        print("No pending tasks")
    else:
        print(f"Processed {processed} task(s)")

    # process_one_task may have lazily started the persistent asyncio runtime
    # (Talk delivery via run_coro). Stop it so the shared httpx client closes
    # cleanly rather than being dropped on interpreter exit. No-op if unused.
    from .async_runtime import reset_async_runtime

    reset_async_runtime()


def _usage_window(args):
    """Resolve the CLI's date arguments into one window, in both formats.

    Returns `(since_iso, until_iso, since_sql, until_sql)`. Both formats come
    back together because the window has two readers whose tables store dates
    differently — `task_usage` in ISO-Z, `tasks` in `datetime('now')` — and
    deriving one and forgetting the other is what makes the unmeasured-task
    counter describe a different window than the table above it.

    A bare `--until D` is expanded to D+1 at midnight. Without that,
    `--since 2026-08-01 --until 2026-08-20` silently loses the whole of 20 Aug,
    which is the kind of wrong number nobody notices.

    Raises `ValueError` on an unparseable or inverted window, so the caller can
    refuse rather than print an empty table that looks like an answer.
    """
    from datetime import datetime, timedelta, timezone

    def _iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    def _sql(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _parse_day(value):
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if args.since:
        since_dt = _parse_day(args.since)
    else:
        if args.days < 1:
            # `--days 0` reads as "no limit" and does the opposite: it puts the
            # bound at now and reports nothing. A negative one puts it in the
            # future. Both print a table that looks like an answer.
            raise ValueError("--days must be at least 1")
        since_dt = datetime.now(timezone.utc) - timedelta(days=args.days)

    until_dt = _parse_day(args.until) + timedelta(days=1) if args.until else None
    if until_dt is not None and until_dt <= since_dt:
        raise ValueError("--until must be after --since")

    return (
        _iso(since_dt),
        _iso(until_dt) if until_dt else None,
        _sql(since_dt),
        _sql(until_dt) if until_dt else None,
    )


def cmd_usage(args):
    """Report token and cost usage. Operator-facing; run from the shell."""
    import json as _json

    config = load_config(Path(args.config) if args.config else None)

    try:
        since, until, since_sql, until_sql = _usage_window(args)
    except ValueError as e:
        print(str(e) if str(e).startswith("--") else "Dates must be YYYY-MM-DD")
        return 1

    filters = dict(
        since=since, until=until, user_id=args.user, brain_kind=args.brain,
        source_type=args.source, origin=args.origin, model=args.model,
    )

    try:
        with db.get_db(config.db_path) as conn:
            if args.by:
                groups = db.usage_summary(conn, group_by=args.by, **filters)
            else:
                groups = [db.usage_summary(conn, **filters)]
                groups[0]["key"] = "all"
            # The same window the table above describes, in the format `tasks`
            # stores. Deriving it separately is how the trailer came to say
            # "in this window" about a different one.
            unmeasured = db.unmeasured_task_count(
                conn, since=since_sql, until=until_sql, user_id=args.user,
            )
    except db.sqlite3.OperationalError as e:
        if "no such table" in str(e):
            print(
                "No usage data yet — the task_usage table is created on the "
                "next database open. Restart the daemon, or run `istota init`."
            )
            return 1
        raise

    if args.json:
        # Deliberately exempt from the cost-render rule: `cost_basis` travels
        # with the figure, so a consumer can apply its own. Suppressing the
        # field here would force it to re-derive what is already known — the
        # suppression is a rendering rule, not a data rule.
        print(_json.dumps({
            "since": since, "until": until,
            "group_by": args.by, "unmeasured_tasks": unmeasured,
            "groups": groups,
        }, indent=2, default=str))
        return 0

    if not any(g["rows"] for g in groups):
        print("No usage recorded in this window.")
        return 0

    label = {"day": "Day", "user": "User", "model": "Model", "source": "Source",
             "brain": "Brain", "origin": "Origin"}.get(args.by, "")

    # Two column blocks, visually separated, because the two groups of measures
    # are not comparable: the first sums across requests, the second is a first
    # and a max over per-request prompt sizes.
    header = (
        # Wide enough for a 30-day fleet total with separators — 15 columns
        # holds 999,999,999,999, where 12 overflowed at a billion and shifted
        # every column after it.
        f"{label or 'Totals':<22} {'Rows':>6} {'Billed in':>15} {'Cache rd':>15} "
        f"{'Cache wr':>15} {'Output':>12} {'Hit%':>6} {'Cost':>16}"
    )
    print(header)
    print("-" * len(header))
    for g in groups:
        key = str(g.get("key") or "")[:22]
        print(
            f"{key:<22} {g['rows']:>6} {fmt_int(g['billed_input_tokens']):>15} "
            f"{fmt_int(g['cache_read_tokens']):>15} "
            f"{fmt_int(g['cache_write_tokens']):>15} "
            f"{fmt_int(g['output_tokens']):>12} "
            f"{g['cache_hit_rate'] * 100:>5.1f}% {render_cost(g['cost_by_basis']):>16}"
        )

    print()
    ctx_header = (
        f"{label or 'Context':<22} {'Measured':>9} {'Avg initial':>13} "
        f"{'Avg peak':>13} {'Peak % of window':>18}"
    )
    print(ctx_header)
    print("-" * len(ctx_header))
    for g in groups:
        key = str(g.get("key") or "")[:22]
        window = g.get("avg_context_window")
        peak = g.get("avg_peak_context_tokens")
        # `peak is not None`, not truthiness: a measured peak of 0 is a real
        # 0.0%, and the whole point of the nullable columns is that only NULL
        # means unmeasured.
        pct = (
            f"{peak / window * 100:.1f}%"
            if window and peak is not None
            else COST_PLACEHOLDER
        )
        print(
            f"{key:<22} {g['context_rows']:>9} "
            f"{fmt_context(g.get('avg_initial_context_tokens')):>13} "
            f"{fmt_context(peak):>13} {pct:>18}"
        )

    if unmeasured:
        print(
            f"\n{unmeasured} task(s) in this window recorded no usage "
            "(a tmux-brain run reports none; a synthetic zero would drag every "
            "average)."
        )
    return 0


def cmd_list(args):
    """List tasks."""
    config = load_config(Path(args.config) if args.config else None)

    with db.get_db(config.db_path) as conn:
        tasks = db.list_tasks(
            conn,
            status=args.status,
            user_id=args.user,
            limit=args.limit,
        )

    if not tasks:
        print("No tasks found")
        return

    for t in tasks:
        # Skill-tasks and command-tasks don't have a prompt; fall back to
        # whichever shape was actually populated so the operator sees what
        # ran. Mirrored in cmd_show.
        if t.prompt:
            label = t.prompt
        elif t.skill:
            args_label = (t.skill_args or "").strip()
            label = f"<skill:{t.skill}> {args_label}".rstrip()
        elif t.command:
            label = f"<cmd> {t.command}"
        else:
            label = ""
        preview = label[:60] + "..." if len(label) > 60 else label
        preview = preview.replace("\n", " ")
        print(f"[{t.id}] {t.status:20} {t.user_id:15} {preview}")


def cmd_show(args):
    """Show task details."""
    config = load_config(Path(args.config) if args.config else None)

    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, args.task_id)
        if not task:
            print(f"Task {args.task_id} not found", file=sys.stderr)
            sys.exit(1)

        logs = db.get_task_logs(conn, args.task_id)

    print(f"Task ID: {task.id}")
    print(f"Status: {task.status}")
    print(f"User: {task.user_id}")
    print(f"Source: {task.source_type}")
    print(f"Created: {task.created_at}")
    print(f"Attempts: {task.attempt_count}/{task.max_attempts}")
    if task.prompt:
        print(f"\nPrompt:\n{task.prompt}")
    elif task.skill:
        print(f"\nSkill: {task.skill}")
        if task.skill_args:
            print(f"Skill args: {task.skill_args}")
    elif task.command:
        print(f"\nCommand:\n{task.command}")

    if task.result:
        print(f"\nResult:\n{task.result}")
    if task.error:
        print(f"\nError:\n{task.error}")
    if task.confirmation_prompt:
        print(f"\nPending confirmation:\n{task.confirmation_prompt}")

    if logs:
        print("\nLogs:")
        for log in logs:
            print(f"  [{log['level']}] {log['timestamp']}: {log['message']}")


def _coerce_extras_value(raw: str):
    """Best-effort coerce a CLI ``key=value`` string to its natural Python type.

    Operators shouldn't have to learn JSON quoting just to pass an integer
    like ``default_radius=75`` or a bool like ``reconcile_enabled=true``. We try
    JSON first (handles ints, floats, bools, null, lists, dicts) and fall
    back to a plain string. Mirrors how TOML would have parsed the same
    field.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _build_resource_extras(args) -> "dict[str, object] | None":
    """Assemble the extras payload from CLI flags.

    Returns ``None`` when the operator hasn't expressed an intent — neither
    ``--extras`` nor ``--extras-json`` nor ``--extras-clear`` was passed.
    Returns ``{}`` when ``--extras-clear`` is set or ``--extras-json``
    decodes to an empty dict. Otherwise returns the assembled dict.
    """
    extras_json = getattr(args, "extras_json", None)
    extras_kv = getattr(args, "extras", None)
    extras_clear = getattr(args, "extras_clear", False)

    if extras_clear:
        return {}

    if extras_json is not None:
        try:
            decoded = json.loads(extras_json)
        except json.JSONDecodeError as e:
            print(f"Error: --extras-json is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(decoded, dict):
            print("Error: --extras-json must decode to a JSON object", file=sys.stderr)
            sys.exit(1)
        return decoded

    if extras_kv:
        result: dict[str, object] = {}
        for pair in extras_kv:
            if "=" not in pair:
                print(f"Error: --extras pair must be key=value, got {pair!r}", file=sys.stderr)
                sys.exit(1)
            key, _, value = pair.partition("=")
            key = key.strip()
            if not key:
                print(f"Error: --extras key cannot be empty in {pair!r}", file=sys.stderr)
                sys.exit(1)
            result[key] = _coerce_extras_value(value)
        return result

    return None


def cmd_resource(args):
    """Manage user resources."""
    config = load_config(Path(args.config) if args.config else None)

    if args.action == "list":
        # Show config-defined resources
        user_config = config.get_user(args.user)
        if user_config and user_config.resources:
            print(f"Config resources for {args.user}:")
            for r in user_config.resources:
                print(f"  [config] {r.type:12} {r.path:40} {r.permissions:6} {r.name or ''}")
        else:
            print(f"No config resources for {args.user}")

        # Show DB resources (shared_file entries from auto-organizer)
        with db.get_db(config.db_path) as conn:
            db_resources = db.get_user_resources(conn, args.user)
        if db_resources:
            print("\nDynamic resources (DB):")
            for r in db_resources:
                print(f"  [{r.id:4}] {r.resource_type:12} {r.resource_path:40} {r.permissions:6} {r.display_name or ''}")

    elif args.action == "add":
        if not all([args.type, args.path]):
            print("Error: --type and --path required for add", file=sys.stderr)
            sys.exit(1)
        with db.get_db(config.db_path) as conn:
            resource_id = db.add_user_resource(
                conn,
                user_id=args.user,
                resource_type=args.type,
                resource_path=args.path,
                display_name=args.name,
                permissions=args.permissions or "read",
            )
            print(f"Resource added to DB: {resource_id}")
            print("Note: For permanent resources, prefer `istota resource ensure`.")

    elif args.action == "ensure":
        if not args.type:
            print("Error: --type is required for ensure", file=sys.stderr)
            sys.exit(1)

        # folder is the only declarable type after the Resources sunset and
        # always requires an explicit --path (a folder mount without a path is
        # meaningless; the old module-shaped pseudo-path default is gone).
        if not args.path:
            print(
                "Error: --path is required for folder resources",
                file=sys.stderr,
            )
            sys.exit(1)
        resource_path = args.path
        display_name = args.name
        permissions = args.permissions or "read"
        new_extras = _build_resource_extras(args)

        kwargs: dict[str, object] = {
            "user_id": args.user,
            "resource_type": args.type,
            "resource_path": resource_path,
            "display_name": display_name,
            "permissions": permissions,
        }
        if new_extras is not None:
            kwargs["extras"] = new_extras
        with db.get_db(config.db_path) as conn:
            _, state = db.upsert_user_resource(conn, **kwargs)

        print(f"Resource ensured for {args.user!r}: type={args.type} path={resource_path}")
        print(f"STATE: {state}")


def run_briefing_schedule(
    config,
    action,
    *,
    user=None,
    name=None,
    cron=None,
    title="",
    conversation_token=None,
    output="talk",
    disabled=False,
):
    """Core of ``istota briefings schedule`` (framework ``briefing_configs``).

    Shared by the deprecated ``istota briefing`` shim (``cmd_briefing``) and the
    unified ``istota briefings schedule`` command (``cli_briefings``). Manages
    *schedule + delivery* only — content blocks live in the module DB.
    """
    from . import user_briefings as _ub

    if action == "list":
        found = False
        for user_id, user_config in config.users.items():
            if user and user_id != user:
                continue
            if not user_config.briefings:
                continue
            found = True
            for b in user_config.briefings:
                print(f"{user_id:15} {b.name:10} {b.cron:15} -> {b.conversation_token}")
        db_rows = _ub.list_briefings(config.db_path)
        disabled_rows = [r for r in db_rows if not r.enabled]
        if user:
            disabled_rows = [r for r in disabled_rows if r.user_id == user]
        if disabled_rows:
            print("\nDisabled briefings (DB):")
            for r in disabled_rows:
                print(f"  {r.user_id:15} {r.name:10} {r.cron:15} -> {r.conversation_token}")
        if not found and not disabled_rows:
            print("No briefings configured")
        return

    if action == "ensure":
        if not user or not name or not cron:
            print("Error: --user, --name, and --cron are required for ensure", file=sys.stderr)
            sys.exit(1)

        output = output or "talk"
        from .transport import parse_output_target
        talk_leaf = any(d.surface == "talk" for d in parse_output_target(output))
        if talk_leaf and not conversation_token:
            print(
                f"Error: --conversation-token is required when --output is {output!r}",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            briefing, state = _ub.ensure_briefing(
                config.db_path,
                user_id=user,
                name=name,
                cron=cron,
                title=title or "",
                conversation_token=conversation_token or "",
                output=output,
                enabled=not disabled,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        print(
            f"Briefing ensured for {user!r}: name={briefing.name} cron={briefing.cron!r} "
            f"title={briefing.title or '(derived)'} "
            f"output={briefing.output} enabled={briefing.enabled}"
        )
        print(f"STATE: {state}")
        return

    if action == "delete":
        if not user or not name:
            print("Error: --user and --name are required for delete", file=sys.stderr)
            sys.exit(1)
        removed = _ub.delete_briefing(config.db_path, user, name)
        if removed:
            print(f"Briefing deleted: user={user} name={name}")
        else:
            print(f"No briefing found: user={user} name={name}")
            sys.exit(1)
        return


def cmd_briefing(args):
    """Manage briefing configurations (deprecated: use `istota briefings schedule`)."""
    print(
        "Note: `istota briefing` is deprecated; use `istota briefings schedule` instead.",
        file=sys.stderr,
    )
    config = load_config(Path(args.config) if args.config else None)
    run_briefing_schedule(
        config,
        args.action,
        user=args.user,
        name=args.name,
        cron=args.cron,
        conversation_token=args.conversation_token,
        output=args.output or "talk",
        disabled=args.disabled,
    )


def cmd_secret(args):
    """Manage per-user encrypted secrets.

    Same partial-update + state-output contract as ``user ensure`` and
    ``resource ensure``. Validation is gated by the central
    ``secret_schema`` registry — operators get a loud error on a typo
    instead of an orphan row that no skill ever reads.

    Plaintext values are never echoed to stdout. The ``ensure`` action
    prints the decision (created / updated / noop) but not the value;
    ``list`` prints (service, key, last_updated) tuples only.
    """
    from . import secrets_store
    from .secret_schema import all_known_services, known_service_keys

    config = load_config(Path(args.config) if args.config else None)
    db_path = config.db_path

    if args.action == "list":
        if not args.user:
            print("Error: --user is required for list", file=sys.stderr)
            sys.exit(1)
        stored = secrets_store.list_user_services(db_path, args.user)
        if not stored:
            print(f"No secrets stored for {args.user!r}.")
            return
        print(f"Secrets stored for {args.user!r}:")
        for service in sorted(stored):
            for entry in stored[service]:
                ts = entry.get("updated_at") or "?"
                print(f"  {service:20} {entry['key']:20} updated_at={ts}")
        return

    # ensure / remove both need (user, service, key).
    if not args.user or not args.service or not args.key:
        print("Error: --user, --service, and --key are required", file=sys.stderr)
        sys.exit(1)

    schema = all_known_services()
    if args.service not in schema:
        print(
            f"Error: unknown service {args.service!r} "
            f"(known: {', '.join(sorted(schema))})",
            file=sys.stderr,
        )
        sys.exit(1)

    valid_keys = known_service_keys()[args.service]
    if not valid_keys:
        print(
            f"Error: service {args.service!r} has no operator-writable keys "
            "(OAuth-only — use the web UI's Connect button)",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.key not in valid_keys:
        print(
            f"Error: unknown key {args.key!r} for service {args.service!r} "
            f"(known: {', '.join(sorted(valid_keys))})",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.action == "ensure":
        if not args.value:
            print(
                "Error: --value is required for ensure (use `secret remove` to clear)",
                file=sys.stderr,
            )
            sys.exit(1)
        state = secrets_store.upsert_secret(
            db_path, args.user, args.service, args.key, args.value,
        )
        print(f"Secret ensured for {args.user!r}: service={args.service} key={args.key}")
        print(f"STATE: {state}")
        return

    if args.action == "remove":
        removed = secrets_store.delete_secret(
            db_path, args.user, args.service, args.key,
        )
        state = "removed" if removed else "noop"
        print(f"Secret remove for {args.user!r}: service={args.service} key={args.key}")
        print(f"STATE: {state}")
        return


def cmd_email(args):
    """Email management commands."""
    config = load_config(Path(args.config) if args.config else None)

    if args.action == "poll":
        if not config.email.enabled:
            print("Email is not enabled in config", file=sys.stderr)
            sys.exit(1)
        task_ids = poll_emails(config)
        if task_ids:
            print(f"Created {len(task_ids)} task(s): {task_ids}")
        else:
            print("No new emails to process")

    elif args.action == "list":
        if not config.email.enabled:
            print("Email is not enabled in config", file=sys.stderr)
            sys.exit(1)
        email_config = get_email_config(config)
        try:
            emails = list_emails(
                folder=config.email.poll_folder,
                limit=args.limit,
                config=email_config,
            )
            if not emails:
                print("No emails found")
                return
            for e in emails:
                read_marker = " " if e.is_read else "*"
                subject = e.subject[:50] + "..." if len(e.subject) > 50 else e.subject
                print(f"{read_marker} [{e.id:6}] {e.sender:30} {subject}")
        except Exception as e:
            print(f"Error listing emails: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.action == "test":
        if not all([args.to, args.subject, args.body]):
            print("Error: --to, --subject, and --body required for test", file=sys.stderr)
            sys.exit(1)
        email_config = get_email_config(config)
        try:
            send_email(
                to=args.to,
                subject=args.subject,
                body=args.body,
                config=email_config,
                from_addr=config.email.bot_email,
            )
            print(f"Email sent to {args.to}")
        except Exception as e:
            print(f"Error sending email: {e}", file=sys.stderr)
            sys.exit(1)


def cmd_user_list(args):
    """List configured users."""
    config = load_config(Path(args.config) if args.config else None)

    if not config.users:
        print("No users configured")
        return
    for user_id, user_config in config.users.items():
        emails = ", ".join(user_config.email_addresses) if user_config.email_addresses else "(none)"
        print(f"{user_id:15} {user_config.display_name:20} {emails}")


def cmd_user_lookup(args):
    """Look up a user by email."""
    config = load_config(Path(args.config) if args.config else None)

    if not args.email:
        print("Error: --email required for lookup", file=sys.stderr)
        sys.exit(1)
    user_id = config.find_user_by_email(args.email)
    if user_id:
        user_config = config.get_user(user_id)
        print(f"User ID: {user_id}")
        print(f"Display name: {user_config.display_name}")
        print(f"Email addresses: {', '.join(user_config.email_addresses)}")
    else:
        print(f"No user found for email: {args.email}")


def cmd_user_init(args):
    """Initialize bot-managed directories for a user."""
    config = load_config(Path(args.config) if args.config else None)

    user_id = args.username

    # Warn if user not in config but proceed anyway
    if user_id not in config.users:
        print(f"Warning: User '{user_id}' not found in config, but proceeding anyway")

    print(f"Initializing directories for user '{user_id}'...")
    if config.use_mount:
        print(f"Mount: {config.nextcloud_mount_path}")
    else:
        print(f"Remote: {config.rclone_remote}")
    print(f"Base path: {get_user_base_path(user_id)}")

    success = ensure_user_directories_v2(config, user_id)
    if success:
        print(f"Directories created: inbox/, memories/, {config.bot_dir_name}/, shared/, scripts/")
    else:
        print("Warning: Some directories may not have been created", file=sys.stderr)

    if args.init_memory:
        print("Initializing memory file...")
        if init_user_memory_v2(config, user_id):
            print(f"Memory file created: {config.bot_dir_name}/config/USER.md")
        else:
            print("Error: Failed to create memory file", file=sys.stderr)
            sys.exit(1)


def cmd_user_status(args):
    """Show status of user's bot-managed directories."""
    config = load_config(Path(args.config) if args.config else None)

    user_id = args.username

    print(f"User: {user_id}")
    if config.use_mount:
        print(f"Mount: {config.nextcloud_mount_path}")
    else:
        print(f"Remote: {config.rclone_remote}")
    print(f"Base path: {get_user_base_path(user_id)}")
    print()

    # Check if user is in config
    if user_id in config.users:
        user_config = config.get_user(user_id)
        print(f"Config: Found (display_name: {user_config.display_name})")
    else:
        print("Config: Not found in config")
    print()

    # Check directories
    print("Directories:")
    dir_status = user_directories_exist_v2(config, user_id)
    for subdir, exists in dir_status.items():
        status = "exists" if exists else "missing"
        print(f"  {subdir}/: {status}")
    print()

    # Check memory file
    print("Memory file:")
    line_count = get_memory_line_count_v2(config, user_id)
    if line_count is not None:
        print(f"  Status: initialized ({line_count} lines)")
    else:
        print("  Status: not initialized")


def cmd_user_ensure(args):
    """Create or update a user_profiles row (idempotent).

    Drop-in replacement for templating per-user TOML files via Ansible.
    Only the flags the operator passes are written; omitted flags leave
    the existing column value untouched (or use defaults on first insert).
    """
    from . import user_profiles

    config = load_config(Path(args.config) if args.config else None)
    user_id = args.name
    db_path = config.db_path

    if not Path(db_path).exists():
        print(f"Error: DB not found at {db_path}; run `istota init` first", file=sys.stderr)
        sys.exit(1)

    # Build the partial-update dict from flags the user actually passed.
    updates: dict[str, object] = {}
    if args.display_name is not None:
        updates["display_name"] = args.display_name
    if args.tz is not None:
        updates["timezone"] = args.tz
    if args.email is not None:
        updates["email_addresses"] = list(args.email)
    if args.trusted_sender is not None:
        updates["trusted_email_senders"] = list(args.trusted_sender)
    if args.quiet_sender is not None:
        updates["quiet_email_senders"] = list(args.quiet_sender)
    if args.log_channel is not None:
        updates["log_channel"] = args.log_channel
    if args.alerts_channel is not None:
        updates["alerts_channel"] = args.alerts_channel
    if args.max_foreground_workers is not None:
        updates["max_foreground_workers"] = args.max_foreground_workers
    if args.max_background_workers is not None:
        updates["max_background_workers"] = args.max_background_workers
    if args.disabled_skill is not None:
        updates["disabled_skills"] = list(args.disabled_skill)
    if args.disabled_module is not None:
        from .modules import MODULE_NAMES

        # Drop empty strings so `--disabled-module ""` is the explicit-clear
        # form (argparse delivers ["" ] when the flag is passed once with no
        # value). Validate the rest against the module registry — a typo
        # would silently disable nothing otherwise.
        names = [m for m in args.disabled_module if m]
        unknown = [m for m in names if m not in MODULE_NAMES]
        if unknown:
            print(
                f"Error: unknown module name(s): {', '.join(unknown)} "
                f"(known: {', '.join(sorted(MODULE_NAMES))})",
                file=sys.stderr,
            )
            sys.exit(1)
        updates["disabled_modules"] = names
    if args.default_destination is not None:
        from .transport import parse_output_target
        if args.default_destination and not parse_output_target(args.default_destination):
            print(
                f"Error: invalid default destination descriptor: "
                f"{args.default_destination!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        updates["default_destination"] = args.default_destination or "talk"
    if args.route is not None:
        from .notifications import PURPOSES
        from .transport import parse_output_target
        routing: dict[str, str] = {}
        for entry in args.route:
            purpose, sep, descriptor = entry.partition("=")
            purpose = purpose.strip()
            descriptor = descriptor.strip()
            if not sep or not purpose:
                print(
                    f"Error: --route expects purpose=descriptor, got {entry!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
            # Reject unknown purposes — a typo would persist a dead routing entry
            # that nothing reads (mirrors the web settings validator).
            if purpose not in PURPOSES:
                print(
                    f"Error: unknown route purpose {purpose!r}; expected one of "
                    f"{', '.join(PURPOSES)}",
                    file=sys.stderr,
                )
                sys.exit(1)
            # Empty descriptor clears the route for that purpose.
            if descriptor and not parse_output_target(descriptor):
                print(
                    f"Error: invalid route descriptor for {purpose!r}: "
                    f"{descriptor!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if descriptor:
                routing[purpose] = descriptor
        updates["routing"] = routing
    if args.email_reply_routing is not None:
        valid = ("origin+thread", "origin", "thread")
        if args.email_reply_routing not in valid:
            print(
                f"Error: --email-reply-routing must be one of {', '.join(valid)}, "
                f"got {args.email_reply_routing!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        updates["email_reply_routing"] = args.email_reply_routing
    outbound_approval = getattr(args, "outbound_approval", None)
    if outbound_approval is not None:
        from .outbound_policy import VALID_POLICIES

        # "" is a real value: unset, meaning "follow the operator's
        # [email] outbound_approval_floor". It is not the same as "off", which
        # pins the user below a floor the operator may later raise. Not an
        # argparse `choices` list, so the error can name the empty case.
        if outbound_approval not in ("", *VALID_POLICIES):
            print(
                f"Error: --outbound-approval must be one of "
                f"{', '.join(VALID_POLICIES)}, or '' to follow the operator "
                f"floor; got {outbound_approval!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        updates["outbound_approval"] = outbound_approval
    external_turn_display = getattr(args, "external_turn_display", None)
    if external_turn_display is not None:
        # Belt and braces: the parser's `choices` already rejects an unknown
        # value, so this only fires for a caller building `args` by hand — the
        # `_FakeArgs` shim in the tests, and any future programmatic caller.
        values = user_profiles.EXTERNAL_TURN_DISPLAY_VALUES
        if external_turn_display not in values:
            print(
                f"Error: --external-turn-display must be one of "
                f"{', '.join(values)}, "
                f"got {external_turn_display!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        updates["external_turn_display"] = external_turn_display
    if getattr(args, "default_briefings", None) is not None:
        updates["default_briefings"] = args.default_briefings
    if getattr(args, "briefing_email_html", None) is not None:
        updates["briefing_email_html"] = args.briefing_email_html
    if getattr(args, "timezone_follow_location", None) is not None:
        updates["timezone_follow_location"] = args.timezone_follow_location

    profile, state = user_profiles.update_profile_with_status(db_path, user_id, **updates)

    print(f"User {user_id!r} ensured.")
    print(f"  display_name: {profile.display_name}")
    print(f"  timezone:     {profile.timezone}")
    if profile.email_addresses:
        print(f"  emails:       {', '.join(profile.email_addresses)}")
    if profile.log_channel:
        print(f"  log_channel:  {profile.log_channel}")
    if profile.alerts_channel:
        print(f"  alerts_channel: {profile.alerts_channel}")
    if profile.trusted_email_senders:
        print(f"  trusted_senders: {', '.join(profile.trusted_email_senders)}")
    if profile.quiet_email_senders:
        print(f"  quiet_senders: {', '.join(profile.quiet_email_senders)}")
    if profile.disabled_modules:
        print(f"  disabled_modules: {', '.join(profile.disabled_modules)}")
    if profile.default_destination and profile.default_destination != "talk":
        print(f"  default_destination: {profile.default_destination}")
    if profile.email_reply_routing and profile.email_reply_routing != "origin+thread":
        print(f"  email_reply_routing: {profile.email_reply_routing}")
    if profile.outbound_approval:
        print(f"  outbound_approval: {profile.outbound_approval}")
    if profile.external_turn_display and profile.external_turn_display != "collapsed":
        print(f"  external_turn_display: {profile.external_turn_display}")
    if not profile.default_briefings:
        print("  default_briefings: off")
    if not profile.briefing_email_html:
        print("  briefing_email_html: off")
    if profile.timezone_follow_location:
        print("  timezone_follow_location: on")
    if profile.routing:
        print(f"  routing: {', '.join(f'{k}={v}' for k, v in sorted(profile.routing.items()))}")
    print(f"STATE: {state}")


def cmd_user_show(args):
    """Show the stored profile for a user (DB row only — no TOML overlay)."""
    from . import user_profiles

    config = load_config(Path(args.config) if args.config else None)
    db_path = config.db_path
    if not Path(db_path).exists():
        print(f"Error: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    profile = user_profiles.get_profile(db_path, args.name)
    if profile is None:
        print(f"No DB profile row for {args.name!r}")
        return

    print(json.dumps({
        "user_id": profile.user_id,
        "display_name": profile.display_name,
        "email_addresses": profile.email_addresses,
        "timezone": profile.timezone,
        "log_channel": profile.log_channel,
        "alerts_channel": profile.alerts_channel,
        "max_foreground_workers": profile.max_foreground_workers,
        "max_background_workers": profile.max_background_workers,
        "disabled_skills": profile.disabled_skills,
        "disabled_modules": profile.disabled_modules,
        "trusted_email_senders": profile.trusted_email_senders,
        "quiet_email_senders": profile.quiet_email_senders,
        "routing": profile.routing,
        "default_destination": profile.default_destination,
        "email_reply_routing": profile.email_reply_routing,
        "outbound_approval": profile.outbound_approval,
        "external_turn_display": profile.external_turn_display,
        "default_briefings": profile.default_briefings,
        "briefing_email_html": profile.briefing_email_html,
        "timezone_follow_location": profile.timezone_follow_location,
    }, indent=2))


def cmd_user_remove(args):
    """Remove a user_profiles row and the user's avatars. Nothing else."""
    from . import user_profiles

    config = load_config(Path(args.config) if args.config else None)
    db_path = config.db_path
    if user_profiles.delete_profile(db_path, args.name):
        print(f"Removed profile row for {args.name!r}.")
    else:
        print(f"No profile row for {args.name!r} (nothing to remove).")


def cmd_calendar_discover(args):
    """Discover calendars accessible to the istota bot."""
    config = load_config(Path(args.config) if args.config else None)

    if not config.caldav_url or not config.caldav_username or not config.caldav_password:
        print("Error: CalDAV settings not configured", file=sys.stderr)
        print("Required: caldav_url, caldav_username, caldav_password in config", file=sys.stderr)
        sys.exit(1)

    try:
        with get_caldav_client(
            config.caldav_url,
            config.caldav_username,
            config.caldav_password,
        ) as client:
            calendars = list_calendars(client)

            if not calendars:
                print("No calendars found")
                return

            print(f"Found {len(calendars)} calendar(s):\n")
            for name, url in calendars:
                # Determine if owned or shared based on URL path
                is_owned = f"/calendars/{config.caldav_username}/" in url
                ownership = "owned" if is_owned else "shared"
                print(f"  {name}")
                print(f"    URL: {url}")
                print(f"    Type: {ownership}")
                print()

    except Exception as e:
        print(f"Error connecting to CalDAV server: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_calendar_test(args):
    """Test calendar access."""
    from datetime import datetime, timedelta

    config = load_config(Path(args.config) if args.config else None)

    if not config.caldav_url or not config.caldav_username or not config.caldav_password:
        print("Error: CalDAV settings not configured", file=sys.stderr)
        sys.exit(1)

    calendar_url = args.url

    try:
        with get_caldav_client(
            config.caldav_url,
            config.caldav_username,
            config.caldav_password,
        ) as client:
            # Test read access
            print(f"Testing read access to: {calendar_url}")
            try:
                events = get_today_events(client, calendar_url)
                print(f"  Read access: OK ({len(events)} event(s) today)")
                for event in events[:3]:  # Show up to 3 events
                    print(f"    - {format_event_for_display(event)}")
                if len(events) > 3:
                    print(f"    ... and {len(events) - 3} more")
            except Exception as e:
                print(f"  Read access: FAILED - {e}", file=sys.stderr)
                sys.exit(1)

            # Test write access if requested
            if args.test_write:
                print("\nTesting write access...")
                try:
                    # Create a test event
                    now = datetime.now()
                    test_start = now + timedelta(days=30)  # 30 days in future
                    test_end = test_start + timedelta(hours=1)

                    uid = create_event(
                        client,
                        calendar_url,
                        summary="[Istota Test Event - DELETE ME]",
                        start=test_start,
                        end=test_end,
                        description="This is a test event created by istota calendar test --test-write. It should be automatically deleted.",
                    )
                    print(f"  Create event: OK (UID: {uid})")

                    # Delete the test event
                    deleted = delete_event(client, calendar_url, uid)
                    if deleted:
                        print("  Delete event: OK")
                    else:
                        print("  Delete event: FAILED - event not found after creation", file=sys.stderr)
                        sys.exit(1)

                    print("\n  Write access: OK")

                except Exception as e:
                    error_msg = str(e).lower()
                    if "authorization" in error_msg or "forbidden" in error_msg or "403" in error_msg:
                        print("  Write access: DENIED (read-only calendar)")
                    else:
                        print(f"  Write access: FAILED - {e}", file=sys.stderr)
                    sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_tasks_file_poll(args):
    """Poll TASKS.md files for new tasks."""
    config = load_config(Path(args.config) if args.config else None)

    # Discover TASKS files
    discovered = discover_tasks_files(config)

    if not discovered:
        print("No TASKS.md files found")
        return

    if args.user:
        # Filter to specific user
        discovered = [f for f in discovered if f.owner_id == args.user]
        if not discovered:
            print(f"No TASKS.md file found for user '{args.user}'")
            return

    print(f"Found {len(discovered)} TASKS.md file(s):")
    for tf in discovered:
        print(f"  {tf.file_path} (owner: {tf.owner_id})")

    all_task_ids = []
    for tf in discovered:
        task_ids = poll_user_tasks_file(config, tf.owner_id, tf.file_path)
        all_task_ids.extend(task_ids)

    if all_task_ids:
        print(f"Created {len(all_task_ids)} task(s): {all_task_ids}")
    else:
        print("No new tasks found")


def _load_kv_config(args):
    """Load config for KV commands (shared writes need the admin allowlist)."""
    return load_config(Path(args.config) if args.config else None)


def _get_kv_conn(args):
    """Get a DB connection for KV commands."""
    return db.get_db(_load_kv_config(args).db_path)


def _shared_write_denied(config, user_id) -> bool:
    """Print the standard error envelope + return True when a shared write is
    refused (non-admin identity / blank allowlist). Callers exit non-zero."""
    if config.is_shared_kv_writer(user_id):
        return False
    print(json.dumps({
        "status": "error", "error": "shared KV writes require admin",
    }))
    return True


def cmd_kv_get(args):
    """Get a value from the KV store."""
    with _get_kv_conn(args) as conn:
        if getattr(args, "shared", False):
            result = db.shared_kv_get(conn, args.namespace, args.key)
        else:
            result = db.kv_get(conn, args.user, args.namespace, args.key)
    if result is None:
        print(json.dumps({"status": "not_found"}))
    else:
        print(json.dumps({"status": "ok", "value": json.loads(result["value"])}))


def cmd_kv_set(args):
    """Set a value in the KV store.

    `--value-file` is the operator's way past the same 128 KiB argv cap the
    skill CLI documents. There is no host-path allowlist here: this runs as the
    operator in their own shell, reading a file they can already read, so
    scoping it would only be theatre.
    """
    value_file = getattr(args, "value_file", None)
    if args.value is not None and value_file:
        print(json.dumps({
            "status": "error",
            "message": "pass either a positional value or --value-file, not both",
        }))
        sys.exit(1)
    if args.value is None and not value_file:
        print(json.dumps({
            "status": "error",
            "message": "no value given: pass a JSON value or --value-file <path>",
        }))
        sys.exit(1)
    if value_file:
        try:
            args.value = Path(value_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(json.dumps({
                "status": "error", "message": f"could not read --value-file: {e}",
            }))
            sys.exit(1)

    try:
        json.loads(args.value)
    except json.JSONDecodeError:
        print(json.dumps({"status": "error", "message": "invalid JSON value"}))
        return
    if getattr(args, "shared", False):
        if _shared_write_denied(_load_kv_config(args), args.user):
            sys.exit(1)
        with _get_kv_conn(args) as conn:
            db.shared_kv_set(conn, args.namespace, args.key, args.value, args.user)
    else:
        with _get_kv_conn(args) as conn:
            db.kv_set(conn, args.user, args.namespace, args.key, args.value)
    print(json.dumps({"status": "ok"}))


def cmd_kv_list(args):
    """List all entries in a namespace.

    Unlike the skill CLI, this defaults to whole values: an operator piping to
    jq wants the entry, not a preview. `--keys-only` / `--max-value-chars` are
    here for the same orienting case, opt-in.
    """
    from istota.skills.kv import render_list_entries

    max_value_chars = getattr(args, "max_value_chars", 0)
    if max_value_chars < 0:
        print(json.dumps({
            "status": "error",
            "error": "--max-value-chars must be >= 0 (0 disables truncation)",
        }))
        sys.exit(1)

    with _get_kv_conn(args) as conn:
        if getattr(args, "shared", False):
            entries = db.shared_kv_list(conn, args.namespace)
        else:
            entries = db.kv_list(conn, args.user, args.namespace)
    truncated = render_list_entries(
        entries,
        keys_only=getattr(args, "keys_only", False),
        max_value_chars=max_value_chars,
    )
    print(json.dumps({
        "status": "ok",
        "count": len(entries),
        "truncated_count": truncated,
        "entries": entries,
    }))


def cmd_kv_delete(args):
    """Delete a key from the KV store."""
    if getattr(args, "shared", False):
        if _shared_write_denied(_load_kv_config(args), args.user):
            sys.exit(1)
        with _get_kv_conn(args) as conn:
            deleted = db.shared_kv_delete(conn, args.namespace, args.key)
    else:
        with _get_kv_conn(args) as conn:
            deleted = db.kv_delete(conn, args.user, args.namespace, args.key)
    if deleted:
        print(json.dumps({"status": "ok", "deleted": True}))
    else:
        print(json.dumps({"status": "not_found"}))


def cmd_kv_namespaces(args):
    """List namespaces for a user."""
    with _get_kv_conn(args) as conn:
        if getattr(args, "shared", False):
            namespaces = db.shared_kv_namespaces(conn)
        else:
            namespaces = db.kv_namespaces(conn, args.user)
    print(json.dumps({"status": "ok", "namespaces": namespaces}))


def cmd_kv_shared_status(args):
    """Report whether a user may write shared KV on this deployment."""
    config = _load_kv_config(args)
    can_write = config.is_shared_kv_writer(args.user)
    print(json.dumps({
        "status": "ok",
        "user_id": args.user,
        "can_write_shared": can_write,
        "admins_configured": bool(config.admin_users),
    }))


def cmd_chat_backfill_history(args):
    """Recover dormant rooms' transcripts from the Talk message cache.

    The web transcript reads the durable `messages` store; rooms whose turns
    were task-retention-deleted before the unified-room-sync migration came out
    empty (their tasks were already GC'd). This folds the surviving
    `talk_messages` copy back into the store. Idempotent — safe to re-run.
    """
    config = load_config(Path(args.config) if args.config else None)
    with db.get_db(config.db_path) as conn:
        if args.token:
            tokens = [args.token]
        else:
            rows = conn.execute(
                "SELECT token FROM rooms WHERE origin = 'talk'"
            ).fetchall()
            tokens = [r["token"] for r in rows]
        total = 0
        per_room = []
        for tok in tokens:
            n = db.backfill_room_messages_from_talk_cache(conn, tok)
            total += n
            if n:
                per_room.append({"token": tok, "inserted": n})
    print(json.dumps({
        "status": "ok", "rooms_scanned": len(tokens),
        "rows_inserted": total, "per_room": per_room,
    }, indent=2))


def cmd_tasks_file_status(args):
    """Show status of TASKS.md file tasks."""
    config = load_config(Path(args.config) if args.config else None)

    # Discover and show TASKS files
    print("Discovered TASKS.md files:")
    discovered = discover_tasks_files(config)

    if args.user:
        discovered = [f for f in discovered if f.owner_id == args.user]

    if not discovered:
        print("  (none found)")
    else:
        for tf in discovered:
            user_config = config.get_user(tf.owner_id)
            email_status = "yes" if (user_config and user_config.email_addresses and config.email.enabled) else "no"
            print(f"  {tf.file_path} (owner: {tf.owner_id}, email notifications: {email_status})")

    print()

    # Show tracked tasks from database
    with db.get_db(config.db_path) as conn:
        tasks = db.list_istota_file_tasks(conn, user_id=args.user, limit=args.limit)

    if not tasks:
        print("No tracked TASKS.md tasks")
        return

    print(f"Tracked tasks (most recent {len(tasks)}):")
    for t in tasks:
        content_preview = t.normalized_content[:40]
        if len(t.normalized_content) > 40:
            content_preview += "..."
        print(f"  [{t.id}] {t.status:12} {t.user_id:15} {content_preview}")


def cmd_nextcloud_capabilities(args):
    """Probe what the configured Nextcloud server supports — the deployment fit-check."""
    from istota.nextcloud import OcsError, capabilities as caps_mod

    config = load_config(Path(args.config) if args.config else None)
    if not config.nextcloud.url:
        print(json.dumps({"status": "error", "error": "Nextcloud is not configured"}, indent=2))
        sys.exit(1)

    try:
        payload = caps_mod.fetch_capabilities(config)
    except OcsError as e:
        print(json.dumps(e.to_envelope(), indent=2))
        sys.exit(1)

    if args.raw:
        print(json.dumps(payload, indent=2, default=str))
        return

    if args.check:
        names = [n.strip() for n in args.check.split(",") if n.strip()]
        checks = caps_mod.evaluate_checks(payload, names)
        for name, ok in checks.items():
            print(f"  [{'ok ' if ok else 'MISSING'}] {name}")
        missing = [n for n, ok in checks.items() if not ok]
        if missing:
            print(f"\nMissing: {', '.join(missing)}")
            sys.exit(1)
        return

    account = {}
    try:
        account = caps_mod.fetch_account(config)
    except OcsError:
        pass
    print(json.dumps(caps_mod.summarize(payload, account), indent=2, default=str))


def cmd_nextcloud_provision_rooms(args):
    """Create the user's default Talk rooms and seed their channel tokens.

    The bare-metal counterpart to what `docker/istota/entrypoint.sh` does for a
    Docker install (ISSUE-115). Idempotent, and prints a `STATE:` line so the
    Ansible role can report `changed` off it the way `user ensure` does.
    """
    from istota import provision_rooms as provision_rooms_mod

    config = load_config(Path(args.config) if args.config else None)
    nc = config.nextcloud
    if not nc.url or not nc.username or not nc.app_password:
        print(
            "Error: Nextcloud is not configured (need url, username and app_password)",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = Path(config.db_path)
    if not args.no_seed and not db_path.exists():
        print(f"Error: DB not found at {db_path}; run `istota init` first", file=sys.stderr)
        sys.exit(1)

    # `--adopt NAME=TOKEN` records a token for a room that already exists and
    # exits. It is the repair path for an install upgraded past ISSUE-342 with
    # a duplicate already on it: the record starts empty, so the first run after
    # the upgrade still matches by name — and for the room that *caused* the
    # report, the name no longer matches anything. Without this the only way to
    # point the record at the room you kept is to rename it back to `general`
    # for one deploy and then rename it again, which is the user undoing the
    # thing they wanted. Talk is not contacted; a wrong token simply falls back
    # to name matching on the next run.
    if args.adopt:
        adopted: list = []
        for pair in args.adopt:
            name, sep, token = pair.partition("=")
            name, token = name.strip(), token.strip()
            if not sep or not name or not token:
                print(
                    f"Error: --adopt takes NAME=TOKEN, got {pair!r}", file=sys.stderr,
                )
                sys.exit(1)
            adopted.append(
                provision_rooms_mod.ProvisionedRoom(
                    name=name, token=token, created=False, invited=False,
                )
            )
        if not provision_rooms_mod.record_provisioned_rooms(
            db_path, args.user, adopted,
        ):
            print(
                f"Error: could not record room tokens for {args.user!r}; "
                "see the log for the cause.",
                file=sys.stderr,
            )
            sys.exit(1)
        for room in adopted:
            print(f"  recorded {room.name} = {room.token}")
        print("STATE: updated")
        return

    names = tuple(args.room) if args.room else provision_rooms_mod.DEFAULT_ROOMS
    # Don't mint a `logs` room beside the hand-made one an operator pinned in
    # inventory; `user ensure` has already written that token by now.
    if not args.no_seed and not args.reseed:
        names = provision_rooms_mod.pending_channel_rooms(db_path, args.user, names)
    if not names:
        print(f"Talk rooms for {args.user!r}: all channels already configured.")
        print("STATE: noop")
        return

    # What a previous run provisioned, so a room the user has since renamed is
    # reused rather than duplicated (ISSUE-342). Empty on a first run or an
    # unreadable DB, which puts us back on the name-matching path.
    known_records = provision_rooms_mod.read_provisioned_records(db_path, args.user)

    # `partial` is filled as each room resolves, so a failure on the third name
    # still records the two that succeeded — those rooms exist on Talk, and
    # losing their tokens puts the next deploy back on name matching for them.
    partial: list = []
    try:
        rooms = provision_rooms_mod.provision_user_rooms(
            config, args.user, names, known_records=known_records, resolved=partial,
        )
    except Exception as e:
        provision_rooms_mod.record_provisioned_rooms(db_path, args.user, partial)
        print(f"Error: could not provision Talk rooms for {args.user!r}: {e}", file=sys.stderr)
        sys.exit(1)

    # A lost record is not a broken deploy — the next run falls back to matching
    # by name, which is what every install did before the record existed — so
    # this warns rather than exiting. It still has to be visible: silently
    # reverting to name matching is the bug.
    if not provision_rooms_mod.record_provisioned_rooms(db_path, args.user, rooms):
        print(
            f"Warning: could not record the room tokens for {args.user!r}. The "
            "rooms exist, but a later rename may produce a duplicate until this "
            "is fixed; see the log for the cause.",
            file=sys.stderr,
        )

    seeded: dict = {}
    seed_state = "noop"
    if not args.no_seed:
        seeded, seed_state = provision_rooms_mod.seed_channel_profile(
            db_path, args.user, rooms, force=args.reseed,
        )

    # A re-invite counts as `updated` whichever way it went: it either added a
    # participant, or it failed and the operator needs the play to say so.
    if any(r.created for r in rooms):
        state = "created"
    elif seed_state != "noop" or any(r.adopted or r.reinvited for r in rooms):
        state = "updated"
    else:
        state = "noop"

    if args.json:
        print(json.dumps({
            "user": args.user,
            "rooms": [
                {
                    "name": r.name, "token": r.token, "created": r.created,
                    "invited": r.invited, "adopted": r.adopted,
                    "reinvited": r.reinvited, "absent": r.absent,
                    "invite_failed": r.invite_failed,
                }
                for r in rooms
            ],
            "seeded": seeded,
            "state": state,
        }, indent=2))
    else:
        print(f"Talk rooms for {args.user!r}:")
        for room in rooms:
            if room.created:
                note = "created"
            elif room.adopted:
                note = "adopted"
            elif room.reinvited:
                note = "re-invited"
            else:
                note = "existing"
            # Not a failure — leaving a room is allowed, and the deploy must not
            # fail on it — but it is also where a room stranded by an invite
            # that failed before ISSUE-408 recorded outcomes ends up. Saying
            # `existing` for both would hide the unreadable one.
            if room.absent:
                note += ", user not a member"
            if room.invite_failed:
                note += ", invite FAILED"
            print(f"  {room.name}: {room.token} ({note})")
        for field, token in sorted(seeded.items()):
            print(f"  seeded {field} = {token}")

    # A room the user was never added to is one they cannot read, so say so
    # loudly. The next run retries that room rather than making another one —
    # because this run recorded the failure (ISSUE-408) — but a persistent
    # failure needs an operator. The predicate is `ProvisionedRoom.invite_failed`
    # rather than a copy of it, since the same fact is what the record stores
    # and what authorizes that retry; two spellings could disagree about which
    # rooms are stranded and which get another attempt.
    stranded = [r.name for r in rooms if r.invite_failed]
    if stranded:
        print(
            f"Warning: could not add {args.user!r} to: {', '.join(stranded)}. "
            "Check that the bot account may add participants and that the user exists.",
            file=sys.stderr,
        )
    print(f"STATE: {state}")


# --- bot-icon ---------------------------------------------------------------
#
# The headless counterpart to the admin page's upload control: one icon for the
# deployment, in the `bot_avatar` table. Three verbs, in `cli.py` rather than a
# module of their own, because that is what every small group here does —
# `chat`, `nextcloud`, `kv`, `calendar` and `experimental` are all inline, and
# only `money` and `briefings` are large enough to have earned a file.
#
# Idempotent by hash and reporting `STATE: created|updated|noop`, matching
# `user ensure` and `nextcloud provision-rooms`, so an Ansible play can call it
# on every deploy and compute `changed_when` from stdout.


def _bot_icon_die(exc: sqlite3.Error) -> None:
    """A database that cannot answer, reported the way every other refusal is.

    `docs/reference/cli.md` sells `set` as the thing an Ansible play runs on
    every deploy, so the ordering of "code lands, play runs, migrations run"
    decides whether a database predating the `bot_avatar` table gives the play a
    clean refusal or a Python traceback out of `main()`.
    """
    print(
        f"Error: could not read the bot icon table: {exc}. "
        "Run `istota init` to apply pending migrations.",
        file=sys.stderr,
    )
    sys.exit(1)


def _bot_icon_max_bytes(config) -> int:
    """The decode cap for an operator-supplied file.

    `web.max_avatar_kb = 0` switches the *upload endpoint* off — a statement
    about an unauthenticated network body. This reads a local file as the
    operator, in their own shell, so it falls back to the shipped default
    rather than refusing everything.
    """
    from istota.config import WebConfig

    return (config.web.max_avatar_kb or WebConfig.max_avatar_kb) * 1024


def cmd_bot_icon_set(args):
    """Store an image file as the deployment's bot icon."""
    from istota import avatars

    config = load_config(Path(args.config) if args.config else None)
    path = Path(args.path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        print(f"Error: could not read {path}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        image, digest = avatars.normalize(
            raw, declared_format=None, max_bytes=_bot_icon_max_bytes(config),
        )
    except avatars.AvatarError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        sys.exit(1)

    try:
        with db.get_db(config.db_path) as conn:
            current = avatars.bot_avatar_hash(conn)
            if current == digest:
                state = "noop"
            else:
                state = "updated" if current else "created"
                avatars.put_bot_avatar(conn, image=image, content_hash=digest)
    except sqlite3.Error as e:
        _bot_icon_die(e)

    print(f"  mime:  {avatars.NORMALIZED_MIME}")
    print(f"  bytes: {len(image)}")
    print(f"  hash:  {digest}")
    print(f"STATE: {state}")


def cmd_bot_icon_clear(args):
    """Remove the bot icon. The web UI reverts to the initial chip."""
    from istota import avatars

    config = load_config(Path(args.config) if args.config else None)
    try:
        with db.get_db(config.db_path) as conn:
            deleted = avatars.delete_bot_avatar(conn)
    except sqlite3.Error as e:
        _bot_icon_die(e)
    print(f"STATE: {'updated' if deleted else 'noop'}")


def cmd_bot_icon_show(args):
    """Report what is stored — never the bytes."""
    from istota import avatars

    config = load_config(Path(args.config) if args.config else None)
    try:
        with db.get_db(config.db_path) as conn:
            icon = avatars.get_bot_avatar(conn)
    except sqlite3.Error as e:
        _bot_icon_die(e)
    if icon is None:
        print("No bot icon set.")
        return
    print("Bot icon:")
    print(f"  mime:       {icon.mime}")
    print(f"  bytes:      {len(icon.image)}")
    print(f"  hash:       {icon.content_hash}")
    print(f"  updated_at: {icon.updated_at}")


def cmd_experimental_list(args):
    """List known experimental feature flags with current on/off status."""
    from istota.experimental import KNOWN_FEATURES
    config = load_config(Path(args.config) if args.config else None)
    enabled = set(config.experimental.features)
    rows = []
    width = max((len(name) for name in KNOWN_FEATURES), default=0)
    for name, desc in sorted(KNOWN_FEATURES.items()):
        status = "on " if name in enabled else "off"
        rows.append(f"  [{status}] {name.ljust(width)}  {desc}")
    if not rows:
        print("(no experimental features registered)")
        return
    print("Experimental features:")
    print("\n".join(rows))
    unknown = sorted(enabled - set(KNOWN_FEATURES))
    if unknown:
        print()
        print("Configured but unknown (typo or stale flag):")
        for name in unknown:
            print(f"  {name}")


# --------------------------------------------------------------------------
# session — the native brain's per-attempt transcripts
# --------------------------------------------------------------------------
#
# The parsing lives in `session/session_log_read.py`, not here: the `tasks
# transcript` skill verb reads the same files with the same rules, and a second
# copy in this file is how the two would start disagreeing about what a
# transcript says. What belongs here is the *rendering* — these four commands
# are for a person at a terminal, so thinking is shown by default and tool
# output is printed plainly. The skill verb renders the same data for a model,
# wrapped in untrusted-content delimiters, which is a different job.

_SESSION_RULE = "─" * 60


def _session_root(args):
    """The resolved log directory, and the config it came from.

    Also the one place every session command passes through, which is why the
    stdout guard is here. These four print transcript content — arbitrary tool
    output, in whatever language the run was in — and the reader decodes with
    ``errors="replace"``, whose own U+FFFD output is not ASCII either. On a
    single-byte locale (`LC_ALL=en_US.ISO8859-1`; `LC_ALL=C` is safe, PEP 538
    coerces it) printing any of that raises `UnicodeEncodeError` and turns a
    readable transcript into a traceback. `repos_relocate.py` sets the precedent
    and states the reason in the same words.
    """
    from .session.session_log import resolve_session_log_dir

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            # A stream somebody replaced with something that is not a
            # TextIOWrapper — a test's capture buffer, a pipe wrapper. Nothing
            # to configure, and nothing here is worth failing a command over.
            pass

    config = load_config(Path(args.config) if args.config else None)
    root = resolve_session_log_dir(config.db_path, config.brain.native.session_log.dir)
    return config, Path(root)


def _fmt_bytes(size) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _fmt_mtime(mtime) -> str:
    import datetime as _dt

    if not mtime:
        return "-"
    return _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


def _resolve_session_target(root: Path, target: str, attempt=None):
    """A path or a bare task id to one file, or ``None`` with a printed reason.

    A bare task id is what the file-name convention is for: glob
    ``*_task-{id}-*.jsonl`` across every user directory. Newest first, so with
    no ``--attempt`` the most recent attempt is what a bare id means.

    A path is tried first, and a target that is *both* a path in the working
    directory and a valid task id falls through to the id when the file turns
    out not to be a transcript. Without that, a directory named ``4471`` in
    whatever shell the operator is standing in silently shadows task 4471 and
    the failure names the wrong problem.
    """
    from .session import session_log_read as slr

    try:
        candidate = Path(target).expanduser()
    except (RuntimeError, OSError):
        # `~nosuchuser/x` — pathlib raises where `os.path.expanduser` hands the
        # string back unchanged. Not a path, then; try the id.
        candidate = None
    task_id = None
    try:
        task_id = int(str(target).strip())
    except (TypeError, ValueError):
        pass

    if candidate is not None and candidate.exists():
        if task_id is None or slr.read_header(candidate) is not None:
            return candidate

    if task_id is None:
        print(f"No such file: {target}", file=sys.stderr)
        return None

    found = slr.find_all_logs(root, task_id=task_id)
    if attempt is not None:
        found = [
            p for p in found
            if (slr.parse_log_name(p.name) or {}).get("attempt") == attempt
        ]
    if not found:
        # `is not None`, not truthiness: attempts are 1-based, so `--attempt 0`
        # matches nothing and reporting "no log for task N" would name a
        # different problem from the one the operator has.
        where = f"task {task_id}"
        if attempt is not None:
            where += f" attempt {attempt}"
        print(f"No session log for {where} under {root}", file=sys.stderr)
        return None
    return found[0]


def cmd_session_list(args):
    """List session logs, newest first."""
    from .session import session_log_read as slr

    _config, root = _session_root(args)
    # `is None`, not truthiness. `find_logs`' whole contract is that a falsy user
    # id finds nothing rather than quietly widening to every user, and branching
    # on truthiness here reaches around it: `-u ""` listed the whole tree. It
    # does not matter much for an operator already holding the tree — it matters
    # because this is the first caller and the pattern the skill verb will be
    # written against, and there the falsy value is `ISTOTA_USER_ID` unset.
    if args.user is not None:
        paths = slr.find_logs(root, args.user, task_id=args.task)
    else:
        paths = slr.find_all_logs(root, task_id=args.task)
    if not paths:
        print(f"No session logs under {root}")
        return

    limit = args.limit if args.limit and args.limit > 0 else len(paths)
    print(f"{root}  ({len(paths)} file(s))")
    for path in paths[:limit]:
        row = slr.summarize(path)
        task = row["task_id"] if row["task_id"] is not None else "?"
        attempt = row["attempt"] if row["attempt"] is not None else "?"
        if not row["readable"]:
            state = f"unreadable ({row['reason']})"
        elif row["complete"]:
            turns = "" if row["turns"] is None else f" {row['turns']} turns"
            mark = "" if row["success"] else " FAILED"
            state = f"{row['stop_reason']}{mark}{turns}"
        else:
            # Not the same thing as a failure: the file has no terminal record,
            # so the run is still going or the daemon died under it.
            state = "interrupted (no result record)"
        print(
            f"  {_fmt_mtime(row['mtime'])}  {row['user_id']:<12} "
            f"task {task}-{attempt}  {_fmt_bytes(row['size']):>9}  {state}"
        )
        print(f"      {row['name']}")
        _print_identity_disagreements(row)


def _print_identity_disagreements(row: dict, indent: str = "      ") -> None:
    """Say when a file's header disagrees with where and what it is filed as.

    All three identity fields shown are the name's and the directory's, because
    those are what every lookup filters on — so a header claiming otherwise is a
    file that did not come from where it sits, and believing it would print a
    row the finder cannot resolve. Reporting it is not decoration: taking the
    header's `task_id` made `list --task 5000` print "task 4471", after which
    `show 4471` found nothing.
    """
    for key, label in (
        ("header_user_id", "user"),
        ("header_task_id", "task"),
        ("header_attempt", "attempt"),
    ):
        claimed = row.get(key)
        if claimed in (None, ""):
            continue
        actual = row.get(key.replace("header_", "", 1))
        print(f"{indent}! header claims {label} {claimed} — filed as {actual}")


def _print_session_header(header: dict, path: Path, row: dict) -> None:
    print(f"Session {header.get('session_id', '')}")
    # The identity line comes off `row`, not off `header`, for the reason
    # `_print_identity_disagreements` states: the name and the directory are
    # what every lookup filters on, so they are what an operator has to be shown.
    # Printing the header's here while `list` printed the name's would have the
    # two commands disagree about the same file.
    print(
        f"  task {row['task_id']} attempt {row['attempt']} "
        f"user {row['user_id']} source {header.get('source_type') or '-'}"
    )
    # `is_fallback` comes off `row`, which is where the tri-state lives: a file
    # written before the field existed says nothing, and printing `fallback=no`
    # for it would answer a question the run never answered. Printed only when
    # the file does say — and the affirmative is what an operator is looking
    # for, since it names which of a rerouted attempt's two `task_usage` rows
    # this transcript's spend is in (ISSUE-378).
    fallback = row.get("is_fallback")
    fallback_note = "" if fallback is None else f" fallback={'yes' if fallback else 'no'}"
    print(
        f"  brain={header.get('brain') or '-'} model={header.get('model') or '-'} "
        f"effort={header.get('effort') or '-'} "
        f"host={header.get('base_url_host') or '-'}{fallback_note}"
    )
    print(f"  started {header.get('ts', '-')}")
    print(f"  file {path} ({_fmt_bytes(row['size'])})")
    _print_identity_disagreements(row, indent="  ")


def _print_blocks(blocks, indent="    ") -> None:
    # `isinstance`, not `or []`: the content list comes out of `json.loads` and
    # its type is whatever was on the line. A scalar there was a `TypeError` out
    # of `session show` on a damaged or foreign file, which is the case this
    # command exists to be usable on.
    if not isinstance(blocks, list):
        if blocks is not None:
            print(_indent(f"[unreadable content: {type(blocks).__name__}]", indent))
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            print(_indent(block.get("text", ""), indent))
        elif kind == "thinking":
            print(_indent("[thinking] " + (block.get("thinking") or ""), indent))
        elif kind == "tool_call":
            print(
                _indent(
                    f"[tool_call {block.get('name')} {block.get('id')}] "
                    f"{json.dumps(block.get('arguments'), ensure_ascii=False)}",
                    indent,
                )
            )
        elif kind == "image":
            print(
                _indent(
                    f"[image {block.get('display_name') or ''} "
                    f"{block.get('media_type') or ''} {block.get('bytes')} bytes]",
                    indent,
                )
            )
        else:
            print(_indent(json.dumps(block, ensure_ascii=False), indent))
        if block.get("truncated"):
            print(_indent(f"… [capped from {block.get('chars_total')} chars]", indent))


def _indent(text, prefix) -> str:
    return "\n".join(prefix + line for line in str(text).splitlines()) or prefix


def _print_record(record) -> None:
    """One conversation record, in the place the run put it.

    A ``message`` is the common case; the other four are mid-run events, and
    printing them **here rather than as a summary at the end** is the whole
    point. A steer and a nudge are injections, so a transcript that omits them
    shows a user turn arriving in the middle of an agent loop with nothing
    saying where it came from — an operator then reads a steered run as one the
    model wandered into, or attributes to the user a sentence the turn-budget
    nudge wrote. Position carries the same weight for the other two: a
    compaction explains why the model forgot something, and *which two turns it
    landed between* is the fact, not that it happened.
    """
    kind = record.get("type")
    stamp = record.get("ts", "")
    if kind == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            print(f"{stamp}  message (unreadable: {type(message).__name__})")
            return
        role = message.get("role", "?")
        if role == "assistant":
            head = f"assistant ({message.get('stop_reason') or '-'})"
        elif role == "tool_result":
            state = "error" if message.get("is_error") else "ok"
            head = f"tool_result {message.get('tool_name') or '?'} ({state})"
        else:
            head = role
        print(f"{stamp}  {head}")
        _print_blocks(message.get("content"))
    elif kind == "steer":
        # Labelled as an injection rather than as a user message, because that
        # is what distinguishes it from the turn it is about to become.
        print(f"{stamp}  steer (injected mid-run)")
        print(_indent(record.get("text") or "", "    "))
    elif kind == "nudge":
        # The framework talking to the model about its own turn budget, not the
        # user. `remaining` is the number the notice leads with.
        print(
            f"{stamp}  nudge ({record.get('phase') or '-'}): "
            f"{record.get('remaining')} of {record.get('max_turns')} turns remaining"
        )
    elif kind == "compaction":
        print(
            f"{stamp}  compaction ({record.get('trigger') or '-'}): dropped "
            f"{record.get('messages_dropped')} messages"
        )
        summary = record.get("summary")
        if summary:
            print(_indent(summary, "    "))
    elif kind == "error":
        print(f"{stamp}  error: {record.get('kind')}: {record.get('message')}")
    elif kind == "serialization_error":
        # A record the writer could not serialize. Shown in place because the
        # only thing it says is "something is missing here", which is a claim
        # about a position in the run.
        print(
            f"{stamp}  serialization error: a {record.get('record_type') or '?'} "
            f"record was lost ({record.get('error')})"
        )
    else:
        print(f"{stamp}  {kind}")


def cmd_session_show(args):
    """Render one session log as readable text."""
    from .session import session_log_read as slr

    _config, root = _session_root(args)
    path = _resolve_session_target(root, args.target, args.attempt)
    if path is None:
        return 1

    row = slr.summarize(path)
    digest = slr.digest(path)
    if not digest["ok"]:
        # Unreadable, not partially rendered: a file whose line 1 is not a
        # header is not a transcript, and guessing at the rest of it is how
        # somebody else's JSONL gets presented as this task's run.
        print(f"{path}: {digest['reason']}", file=sys.stderr)
        return 1

    _print_session_header(digest["header"], path, row)

    out = slr.excerpt(
        path,
        thinking=not args.no_thinking,
        # `--full` turns off the reader's display cap. That is a different cap
        # from the writer's: the file already holds head-and-tail-truncated
        # blocks, and nothing here can put back what was never written.
        max_chars=0 if args.full else args.max_chars,
    )
    if not out["ok"]:
        # `digest` and `excerpt` are two reads of one path and the retention
        # sweep unlinks under this root on the scheduler's interval, so the file
        # can go between them. Discarding the reason rendered a header, a result
        # summary and no conversation at all, exit 0 — a complete-looking run
        # with nothing in it.
        print(f"{path}: {out['reason']}", file=sys.stderr)
        return 1
    context = digest.get("context")
    if context:
        print(f"\n{_SESSION_RULE}")
        print(
            f"context  tools: {', '.join(context['tools']) or '-'}  "
            f"system prompt: {context['system_prompt_chars']} chars"
            + (f" from {context['system_prompt_source']}"
               if context["system_prompt_source"] else "")
        )

    for record in out["records"]:
        print(f"\n{_SESSION_RULE}")
        _print_record(record)

    print(f"\n{_SESSION_RULE}")
    result = digest["result"]
    if result is None:
        print("no result record — interrupted run")
    else:
        print(
            f"result: {result['stop_reason']} "
            f"({'success' if result['success'] else 'failed'}), "
            f"{result['turns']} turns, {result['duration_ms']} ms"
        )
        print(_indent(result["result_text_preview"], "  "))

    notes = []
    if out["truncated"]:
        notes.append(
            f"display capped: {out['records_returned']} of {out['records_total']} "
            f"records shown (--full for all)"
        )
        # The mid-run events are rendered in place, so a cut display can drop
        # the compaction or the error that explains the whole run. Counting them
        # from the digest — which reads the file whole — is what keeps the cut
        # from being a silent one.
        held = [
            (len(digest["compactions"]), "compaction"),
            (len(digest["errors"]), "error"),
            (digest["steers"], "steer"),
            (digest["nudges"], "nudge"),
        ]
        present = [f"{count} {label}" for count, label in held if count]
        if present:
            notes.append("this run also holds " + ", ".join(present) + " record(s)")
    if digest["serialization_errors"]:
        notes.append(
            f"{digest['serialization_errors']} record(s) the writer could not "
            "serialize"
        )
    if digest["malformed"]:
        notes.append(f"{digest['malformed']} malformed line(s) skipped")
    if digest["partial_tail"]:
        notes.append("trailing partial line skipped (session still being written)")
    if digest["unreadable"]:
        # The read died partway through — the file went, or the mount did. What
        # was rendered above is a prefix of the run, and saying nothing here
        # would present it as the whole of it.
        notes.append(
            f"the read stopped early ({digest['unreadable']}) — "
            "what is shown above is a prefix of the run, not all of it"
        )
    for note in notes:
        print(f"note: {note}")
    return None


def cmd_session_tail(args):
    """Print the end of a session log, and by default follow it.

    The reader is deliberately not used here. `tail` prints raw JSONL lines and
    follows by byte offset, so it works on bytes throughout; parsing each line
    only to re-serialize it would spend the whole file's decode cost to print
    what was already there, and would put a *second* rule for what a partial
    trailing line is beside the one the offset arithmetic already implements.
    """
    import time as _time

    _config, root = _session_root(args)
    path = _resolve_session_target(root, args.target, args.attempt)
    if path is None:
        return 1

    # One binary read produces both the records to print and the offset to
    # resume from, and they have to come from the same read. Taking the offset
    # from a fresh `getsize` after a separate `read_records` pass is wrong twice
    # over, and both were measured. `read_records` stops *before* an
    # unterminated trailing line, by contract — a live session always has one —
    # so an offset past it makes the follow loop print that record's tail as
    # though it were a whole line, which is the very thing the non-follow half
    # refuses to do. And any record the writer completes between the end of the
    # read and the `getsize` sits below the offset and is never printed by
    # either half.
    complete, offset = _read_complete_lines(path)
    if complete is None:
        return 1
    lines = [line for line in complete.split(b"\n") if line.strip()]
    for line in lines[-args.lines:] if args.lines > 0 else lines:
        print(line.decode("utf-8", "replace"))
    if args.no_follow:
        return None

    polls = 0
    try:
        while args.poll_limit is None or polls < args.poll_limit:
            polls += 1
            try:
                size = os.path.getsize(path)
            except (OSError, ValueError) as exc:
                # Almost always the retention sweep unlinking underneath. The
                # sibling branch below says so when the file merely shrank;
                # breaking out in silence here returned 0 with nothing printed,
                # which to a script and to an operator is indistinguishable from
                # the session having ended normally.
                print(f"{path}: {exc}", file=sys.stderr)
                return 1
            if size < offset:
                # Shorter than what we have already read: the file was replaced
                # or truncated under us — the retention sweep unlinks under this
                # root on the scheduler's interval. Say so and start over rather
                # than waiting silently for a size that will never come back.
                print("note: file truncated or replaced — following from the start")
                offset = 0
                continue
            if size > offset:
                with open(path, "rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                body, newline, _partial = chunk.rpartition(b"\n")
                if newline:
                    for line in body.split(b"\n"):
                        text = line.decode("utf-8", "replace").strip()
                        if text:
                            print(text)
                    offset += len(body) + 1
            _time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return None


def _read_complete_lines(path):
    """``(bytes up to the last newline, byte offset just past it)``.

    Binary, so the offset is the byte count a later `seek` needs: decoding first
    and measuring the text drifts the moment a replacement character stands in
    for a multi-byte sequence, and a drifted offset prints fragments. ``(None,
    0)`` when the file cannot be read at all.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except (OSError, ValueError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return None, 0
    cut = data.rfind(b"\n")
    if cut < 0:
        return b"", 0
    return data[:cut], cut + 1


def cmd_session_stats(args):
    """Count and size the session logs, split by user."""
    import time as _time

    from .session import session_log_read as slr

    config, root = _session_root(args)
    since = None
    if args.days and args.days > 0:
        since = _time.time() - args.days * 86400.0
    stats = slr.tree_stats(root, since=since)

    window = f" in the last {args.days} day(s)" if since else ""
    print(f"{root}")
    print(f"  {stats.files} file(s), {_fmt_bytes(stats.bytes)}{window}")
    ceiling = config.brain.native.session_log.max_total_gb
    if ceiling:
        print(f"  ceiling: {ceiling} GB (deployment-wide, all users)")
        # Printing the two figures on adjacent lines invites a comparison they
        # do not support, so the difference is said rather than left to be
        # discovered. It is two differences: the sweep counts blocks where this
        # counts content, and the sweep totals every file under a user directory
        # at any depth where this counts only recognised transcript names one
        # level down. So this is a floor on what the ceiling sees.
        print(
            "  (the sweep measures blocks, and counts every file under a user "
            "directory — this counts transcripts only, so it reads low)"
        )
    if stats.files:
        print(f"  oldest {_fmt_mtime(stats.oldest)}, newest {_fmt_mtime(stats.newest)}")
    for user, entry in sorted(
        stats.per_user.items(), key=lambda kv: (-kv[1]["bytes"], kv[0])
    ):
        print(f"  {user:<16} {entry['files']:>5} file(s)  {_fmt_bytes(entry['bytes']):>9}")


def main():
    # `istota money <op> …` forwards operational commands verbatim to the money
    # Click tree. argparse REMAINDER can't capture a leading option (e.g.
    # `money list -u U`), so peel those off before the strict parse_args().
    from istota import cli_money
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("-c", "--config")
    _pre.add_argument("-v", "--verbose", action="store_true")
    _known, _rest = _pre.parse_known_args()
    if len(_rest) >= 2 and _rest[0] == "money" and cli_money.is_operational(_rest[1]):
        config = load_config(Path(_known.config) if _known.config else None)
        setup_logging(config, verbose=_known.verbose)
        rc = cli_money.dispatch_operational(_rest[1], _rest[2:], config)
        sys.exit(rc or 0)

    # `istota briefings <group> …` — the unified briefings tree (schedule +
    # blocks/sources/archive). Peeled here (like `money`) so cli_briefings owns
    # the whole subtree with its own nested argparse.
    if len(_rest) >= 1 and _rest[0] == "briefings":
        from istota import cli_briefings
        config = load_config(Path(_known.config) if _known.config else None)
        setup_logging(config, verbose=_known.verbose)
        rc = cli_briefings.dispatch(_rest[1:], config)
        sys.exit(rc or 0)

    parser = argparse.ArgumentParser(description="Istota CLI")
    parser.add_argument("-c", "--config", help="Path to config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging")
    parser.add_argument(
        "--version", action="version", version=f"istota {_installed_version()}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    subparsers.add_parser("init", help="Initialize database")

    # doctor
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check the runtime this deployment actually has"
    )
    doctor_parser.add_argument("--json", action="store_true", help="Machine-readable output")
    doctor_parser.add_argument(
        "--deep",
        action="store_true",
        help="Include checks that spawn a sandbox namespace (slower)",
    )
    doctor_parser.add_argument(
        "--only",
        action="append",
        metavar="PREFIX",
        help="Run only checks whose name starts with PREFIX (repeatable)",
    )
    doctor_parser.add_argument(
        "--scope",
        choices=["image", "deployment"],
        help=(
            "Narrow to facts about the image (answerable by a bare `docker run`) "
            "or about the deployment (needs a mount, a database, a network)"
        ),
    )

    # task
    task_parser = subparsers.add_parser("task", help="Submit a task")
    task_parser.add_argument("prompt", nargs="?", help="Task prompt (or read from stdin)")
    task_parser.add_argument("-u", "--user", default="testuser", help="User ID")
    task_parser.add_argument("-x", "--execute", action="store_true", help="Execute immediately")
    task_parser.add_argument("--dry-run", action="store_true", help="Show prompt without executing")
    task_parser.add_argument("-t", "--conversation-token", help="Conversation token (room ID) for context lookup")
    task_parser.add_argument("--source-type", help="Source type (cli, talk, briefing, email, istota_file)")
    task_parser.add_argument("--no-context", action="store_true", help="Disable conversation context lookup")

    # run
    run_parser = subparsers.add_parser("run", help="Process pending tasks")
    run_parser.add_argument("--once", action="store_true", help="Process only one task")
    run_parser.add_argument("--briefings", action="store_true", help="Check and queue briefings first")
    run_parser.add_argument("--dry-run", action="store_true", help="Don't actually execute tasks")

    # setup (interactive first-run installer for the local single-user shape)
    setup_parser = subparsers.add_parser(
        "setup", help="Interactive first-run installer (local single-user install)",
    )
    setup_parser.add_argument("--workspace", help="Workspace directory (default ~/.istota)")
    setup_parser.add_argument(
        "--brain", choices=["claude_code", "native"], help="Model backend (skip detection)",
    )
    setup_parser.add_argument("--native-base-url", help="Native brain API base URL")
    setup_parser.add_argument("--native-model", help="Native brain model id")
    setup_parser.add_argument("--native-api-key", help="Native brain API key (written to istota.env)")
    setup_parser.add_argument("--user", help="User id (default OS username)")
    setup_parser.add_argument("--display-name", help="Display name")
    setup_parser.add_argument("--timezone", help="Timezone (default from system)")
    setup_parser.add_argument("--port", type=int, help="Web port (default 8766)")
    setup_parser.add_argument("--email", action="store_true", help="Enable email surface")
    setup_parser.add_argument("--location", action="store_true", help="Enable GPS/location tracking")
    setup_parser.add_argument(
        "--no-money", action="store_true",
        help="Disable the money module (double-entry accounting; on by default)",
    )
    setup_parser.add_argument(
        "--no-health", action="store_true",
        help="Disable the health module (body stats, bloodwork; on by default)",
    )
    setup_parser.add_argument(
        "--no-feeds", action="store_true",
        help="Disable the feeds module (RSS/Atom reader; on by default)",
    )
    setup_parser.add_argument(
        "--no-briefings", action="store_true",
        help="Disable the briefings module (scheduled digests; on by default)",
    )
    setup_parser.add_argument("--yes", action="store_true", help="Non-interactive; take defaults + flags")
    setup_parser.add_argument("--force", action="store_true", help="Overwrite an existing config")

    # serve (combined local launcher: scheduler + web in one process)
    serve_parser = subparsers.add_parser(
        "serve", help="Run the scheduler loop and web server in one process (local install)",
    )
    serve_parser.add_argument("--host", help="Web bind host (default 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, help="Web port (default from [web] port)")
    serve_parser.add_argument(
        "--env-file", help="Path to a KEY=VALUE secrets env file to source before start",
    )

    # update (standalone/local install self-update)
    update_parser = subparsers.add_parser(
        "update", help="Update a standalone (local) install to the latest code",
    )
    update_parser.add_argument(
        "--force", action="store_true",
        help="Update even if the install checkout has uncommitted changes",
    )
    update_parser.add_argument(
        "--channel", choices=["stable", "main"], default=None,
        help="Update channel: 'stable' tracks the latest release, 'main' tracks "
             "the development branch. The choice is remembered for future runs "
             "(default: stable for new installs).",
    )

    # repl
    repl_parser = subparsers.add_parser(
        "repl", help="Interactive terminal assistant (full-stack, streamed)",
    )
    repl_parser.add_argument("-u", "--user", help="User id (defaults to the sole/admin user)")
    repl_parser.add_argument(
        "-t", "--token", help="Resume a named conversation token (default: a fresh one)",
    )
    repl_parser.add_argument(
        "--workspace", default="cwd",
        help="Working directory: cwd (default) | standard (per-user temp) | PATH",
    )
    repl_parser.add_argument("--model", help="Model alias for turns (e.g. opus, sonnet)")
    repl_parser.add_argument("--effort", help="Reasoning effort (low|medium|high|xhigh|max)")

    # list
    list_parser = subparsers.add_parser("list", help="List tasks")
    list_parser.add_argument("-s", "--status", help="Filter by status")
    list_parser.add_argument("-u", "--user", help="Filter by user")
    list_parser.add_argument("-n", "--limit", type=int, default=20, help="Max results")

    # usage — token/cost reporting. Operator-facing only: it runs from the
    # operator's shell, so per-user cost data never reaches a user-facing
    # surface and `--user` is a convenience filter rather than a boundary.
    usage_parser = subparsers.add_parser(
        "usage", help="Report token and cost usage"
    )
    usage_parser.add_argument(
        "--days", type=int, default=30, help="Window size in days (default: 30)"
    )
    usage_parser.add_argument("--since", help="Start date, YYYY-MM-DD")
    usage_parser.add_argument(
        "--until",
        help="End date, YYYY-MM-DD (inclusive — expanded to the following midnight)",
    )
    usage_parser.add_argument("-u", "--user", help="Filter by user")
    usage_parser.add_argument("--brain", help="Filter by brain kind")
    usage_parser.add_argument("--source", help="Filter by task source type")
    usage_parser.add_argument("--model", help="Filter by model")
    usage_parser.add_argument(
        "--origin", help="Filter by origin (task, sleep_cycle, health_ocr, …)"
    )
    usage_parser.add_argument(
        "--by",
        choices=["day", "user", "model", "source", "brain", "origin"],
        help="Group results",
    )
    usage_parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a table"
    )

    # show
    show_parser = subparsers.add_parser("show", help="Show task details")
    show_parser.add_argument("task_id", type=int, help="Task ID")

    # resource — folder-only operator command (Resources sunset). The
    # other declarable types were retired; folder survives as the mechanism
    # for mounting an out-of-workspace path into the sandbox.
    resource_parser = subparsers.add_parser("resource", help="Manage user folder mounts")
    resource_parser.add_argument("action", choices=["list", "add", "ensure"], help="Action")
    resource_parser.add_argument("-u", "--user", required=True, help="User ID")
    resource_parser.add_argument("-t", "--type", choices=["folder"], help="Resource type (folder only — other types were retired by the Resources sunset)")
    resource_parser.add_argument("-p", "--path", help="Resource path (required for folder)")
    resource_parser.add_argument("-n", "--name", help="Display name")
    resource_parser.add_argument("--permissions", help="Permissions: read (default) or readwrite")
    resource_parser.add_argument(
        "--extras", action="append",
        help="Extra resource-specific config as key=value (repeatable). "
             "Values parsed as JSON when possible (e.g. default_radius=75 → int).",
    )
    resource_parser.add_argument(
        "--extras-json",
        help="Full extras payload as a JSON object. Overrides --extras pairs.",
    )
    resource_parser.add_argument(
        "--extras-clear", action="store_true",
        help="Wipe extras on the row. Use to explicitly clear what --extras would otherwise preserve.",
    )

    # briefing
    briefing_parser = subparsers.add_parser("briefing", help="Manage briefings")
    briefing_parser.add_argument(
        "action", choices=["list", "ensure", "delete"], help="Action",
    )
    briefing_parser.add_argument("-u", "--user", help="User id (required for ensure/delete)")
    briefing_parser.add_argument("--name", help="Briefing name (e.g. 'morning')")
    briefing_parser.add_argument("--cron", help="Cron expression (user TZ), e.g. '0 7 * * 1-5'")
    briefing_parser.add_argument(
        "--conversation-token",
        help="Talk room token (required when output includes 'talk')",
    )
    briefing_parser.add_argument(
        "--output", default="talk",
        help="Delivery target: talk / email / ntfy, or a comma list / "
             "surface:channel descriptor (validated by parse_output_target)",
    )
    briefing_parser.add_argument(
        "--disabled",
        action="store_true",
        help="Mark this briefing as disabled (drops the corresponding TOML entry without scheduling)",
    )

    # secret (encrypted per-user credentials)
    secret_parser = subparsers.add_parser(
        "secret",
        help="Manage per-user encrypted secrets (Ansible-friendly, idempotent)",
    )
    secret_parser.add_argument(
        "action", choices=["ensure", "list", "remove"], help="Action",
    )
    secret_parser.add_argument("-u", "--user", help="User id")
    secret_parser.add_argument(
        "--service",
        help="Service name (karakeep, monarch, overland, feeds, ...)",
    )
    secret_parser.add_argument("--key", help="Secret key within the service")
    secret_parser.add_argument(
        "--value",
        help="Secret value (ensure only). Use `secret remove` to clear.",
    )

    # email
    email_parser = subparsers.add_parser("email", help="Email management")
    email_parser.add_argument("action", choices=["poll", "list", "test"], help="Action")
    email_parser.add_argument("-n", "--limit", type=int, default=20, help="Max emails to list")
    email_parser.add_argument("--to", help="Recipient for test email")
    email_parser.add_argument("--subject", help="Subject for test email")
    email_parser.add_argument("--body", help="Body for test email")

    # user (with subparsers)
    user_parser = subparsers.add_parser("user", help="User management")
    user_subparsers = user_parser.add_subparsers(dest="user_action", required=True)

    # user list
    user_subparsers.add_parser("list", help="List configured users")

    # user lookup
    user_lookup_parser = user_subparsers.add_parser("lookup", help="Look up user by email")
    user_lookup_parser.add_argument("--email", required=True, help="Email address to lookup")

    # user init
    user_init_parser = user_subparsers.add_parser("init", help="Initialize bot-managed directories")
    user_init_parser.add_argument("username", help="User ID to initialize")
    user_init_parser.add_argument("--init-memory", action="store_true", help="Create initial memory file")

    # user status
    user_status_parser = user_subparsers.add_parser("status", help="Show user directory status")
    user_status_parser.add_argument("username", help="User ID to check")

    # user ensure  (Phase 6: idempotent profile upsert; replaces per-user TOML in Ansible)
    user_ensure_parser = user_subparsers.add_parser(
        "ensure",
        help="Create or update a user profile row (idempotent; for Ansible)",
    )
    user_ensure_parser.add_argument("--name", required=True, help="User ID (Nextcloud username)")
    user_ensure_parser.add_argument("--display-name", help="Display name shown in prompts")
    user_ensure_parser.add_argument("--tz", "--timezone", dest="tz", help="IANA timezone (e.g. America/Los_Angeles)")
    user_ensure_parser.add_argument(
        "--email", action="append", help="User email address (repeatable; replaces existing list when passed)"
    )
    user_ensure_parser.add_argument(
        "--trusted-sender", action="append",
        help="Trusted email sender pattern (repeatable; fnmatch syntax)",
    )
    user_ensure_parser.add_argument(
        "--quiet-sender", action="append",
        help="Quiet email sender pattern — mail filed silently, no task "
             "(repeatable; fnmatch syntax)",
    )
    user_ensure_parser.add_argument("--log-channel", help="Talk room token for verbose execution logs")
    user_ensure_parser.add_argument("--alerts-channel", help="Talk room token for confirmations and alerts")
    user_ensure_parser.add_argument("--max-foreground-workers", type=int, help="Per-user fg worker cap (0 = global default)")
    user_ensure_parser.add_argument("--max-background-workers", type=int, help="Per-user bg worker cap (0 = global default)")
    user_ensure_parser.add_argument(
        "--disabled-skill", action="append",
        help="Skill name to exclude from selection (repeatable)",
    )
    user_ensure_parser.add_argument(
        "--disabled-module", action="append",
        help=(
            "Module to opt this user out of (repeatable). One of "
            "feeds, money, location. Pass an empty value to clear."
        ),
    )
    user_ensure_parser.add_argument(
        "--default-destination",
        help=(
            "Fallback delivery descriptor (e.g. talk, email, both, talk:<token>). "
            "Default 'talk'."
        ),
    )
    user_ensure_parser.add_argument(
        "--route", action="append", metavar="PURPOSE=DESCRIPTOR",
        help=(
            "Per-purpose delivery route (repeatable; replaces the routing table "
            "when passed). PURPOSE is one of reply/alert/log/briefing/"
            "notification; DESCRIPTOR is an output_target like email or "
            "matrix:<room>. Empty descriptor clears that purpose."
        ),
    )
    user_ensure_parser.add_argument(
        "--email-reply-routing",
        choices=["origin+thread", "origin", "thread"],
        help=(
            "Where a reply to an email this bot sent is delivered: 'origin+thread' "
            "(default — origin surface and the email thread), 'origin' (origin "
            "surface only), or 'thread' (email only)."
        ),
    )
    user_ensure_parser.add_argument(
        "--outbound-approval",
        metavar="POLICY",
        help=(
            "Outbound email approval policy for this user: 'off' (send "
            "everything), 'untrusted' (hold unless every recipient is trusted), "
            "or 'all' (hold unless every recipient is one of their own "
            "addresses). Pass '' to follow the operator's "
            "[email] outbound_approval_floor, which is the default. The floor "
            "is a minimum — a value weaker than it has no effect."
        ),
    )
    user_ensure_parser.add_argument(
        "--external-turn-display",
        # From the shared constant, not a literal: argparse rejects the value
        # before `cmd_user_ensure`'s own check ever runs, so a hardcoded list
        # here is the one that decides, and a fourth copy would make adding a
        # display mode fail with argparse's message while the handler's own
        # error advertised the value as valid.
        choices=list(user_profiles.EXTERNAL_TURN_DISPLAY_VALUES),
        help=(
            "How much of a turn that arrived from outside the room (an email "
            "from an external contact) is shown inline in web chat: 'full', "
            "'collapsed' (default — sender and subject, expandable), or "
            "'hidden'. The turn itself is always shown."
        ),
    )
    user_ensure_parser.add_argument(
        "--default-briefings",
        dest="default_briefings", default=None,
        action=argparse.BooleanOptionalAction,
        help=(
            "Seed the shared [[default_briefings]] set into this user (default on). "
            "Pass --no-default-briefings to opt out; already-seeded briefings are "
            "left intact."
        ),
    )
    user_ensure_parser.add_argument(
        "--briefing-email-html",
        dest="briefing_email_html", default=None,
        action=argparse.BooleanOptionalAction,
        help=(
            "Send briefing email as multipart/alternative — HTML with clickable "
            "links plus a plain-text fallback (default on). Pass "
            "--no-briefing-email-html for plain text only."
        ),
    )
    user_ensure_parser.add_argument(
        "--timezone-follow-location",
        dest="timezone_follow_location", default=None,
        action=argparse.BooleanOptionalAction,
        help=(
            "Set this user's timezone from their GPS position once they have "
            "settled in a new zone (default off). Needs the location module; "
            "the user is notified on every change."
        ),
    )
    # user show  (Phase 6: dump the DB row as JSON)
    user_show_parser = user_subparsers.add_parser("show", help="Show stored profile row as JSON")
    user_show_parser.add_argument("--name", required=True, help="User ID")

    # user remove  (Phase 6: delete a user_profiles row, plus that user's avatar rows)
    user_remove_parser = user_subparsers.add_parser(
        "remove", help="Remove a user's profile row and avatars"
    )
    user_remove_parser.add_argument("--name", required=True, help="User ID")

    # calendar (with subparsers)
    calendar_parser = subparsers.add_parser("calendar", help="Calendar management")
    calendar_subparsers = calendar_parser.add_subparsers(dest="calendar_action", required=True)

    # calendar discover
    calendar_subparsers.add_parser("discover", help="Discover accessible calendars")

    # calendar test
    calendar_test_parser = calendar_subparsers.add_parser("test", help="Test calendar access")
    calendar_test_parser.add_argument("url", help="Calendar URL to test")
    calendar_test_parser.add_argument("--test-write", action="store_true", help="Test write access by creating/deleting a test event")

    # tasks-file (with subparsers)
    tasks_file_parser = subparsers.add_parser("tasks-file", help="TASKS.md file management")
    tasks_file_subparsers = tasks_file_parser.add_subparsers(dest="tasks_file_action", required=True)

    # tasks-file poll
    tasks_file_poll_parser = tasks_file_subparsers.add_parser("poll", help="Poll TASKS.md files for new tasks")
    tasks_file_poll_parser.add_argument("-u", "--user", help="User ID to poll (or all if not specified)")

    # tasks-file status
    tasks_file_status_parser = tasks_file_subparsers.add_parser("status", help="Show TASKS.md file task status")
    tasks_file_status_parser.add_argument("-u", "--user", help="Filter by user")
    tasks_file_status_parser.add_argument("-n", "--limit", type=int, default=20, help="Max tasks to show")

    # kv (with subparsers)
    kv_parser = subparsers.add_parser("kv", help="Key-value store for script state")
    kv_subparsers = kv_parser.add_subparsers(dest="kv_action", required=True)

    _shared_help = "Operate on the cross-user shared_kv store (writes admin-only)"

    # kv get
    kv_get_parser = kv_subparsers.add_parser("get", help="Get a value")
    kv_get_parser.add_argument("namespace", help="Namespace")
    kv_get_parser.add_argument("key", help="Key")
    kv_get_parser.add_argument("-u", "--user", required=True, help="User ID")
    kv_get_parser.add_argument("--shared", action="store_true", help=_shared_help)

    # kv set
    kv_set_parser = kv_subparsers.add_parser("set", help="Set a value (JSON)")
    kv_set_parser.add_argument("namespace", help="Namespace")
    kv_set_parser.add_argument("key", help="Key")
    kv_set_parser.add_argument(
        "value", nargs="?", help="JSON-encoded value (max 128 KiB as an argument)",
    )
    kv_set_parser.add_argument(
        "--value-file",
        help="Read the JSON value from this file instead of the argument",
    )
    kv_set_parser.add_argument("-u", "--user", required=True, help="User ID")
    kv_set_parser.add_argument("--shared", action="store_true", help=_shared_help)

    # kv list
    kv_list_parser = kv_subparsers.add_parser("list", help="List entries in a namespace")
    kv_list_parser.add_argument("namespace", help="Namespace")
    kv_list_parser.add_argument("-u", "--user", required=True, help="User ID")
    kv_list_parser.add_argument(
        "--keys-only", action="store_true",
        help="Return keys and value sizes without the values themselves",
    )
    kv_list_parser.add_argument(
        "--max-value-chars", type=int, default=0,
        help="Truncate each value to N characters (default 0 = whole values)",
    )
    kv_list_parser.add_argument("--shared", action="store_true", help=_shared_help)

    # kv delete
    kv_delete_parser = kv_subparsers.add_parser("delete", help="Delete a key")
    kv_delete_parser.add_argument("namespace", help="Namespace")
    kv_delete_parser.add_argument("key", help="Key")
    kv_delete_parser.add_argument("-u", "--user", required=True, help="User ID")
    kv_delete_parser.add_argument("--shared", action="store_true", help=_shared_help)

    # kv namespaces
    kv_ns_parser = kv_subparsers.add_parser("namespaces", help="List namespaces")
    kv_ns_parser.add_argument("-u", "--user", required=True, help="User ID")
    kv_ns_parser.add_argument("--shared", action="store_true", help=_shared_help)

    # kv shared-status
    kv_status_parser = kv_subparsers.add_parser(
        "shared-status", help="Report whether a user may write shared KV",
    )
    kv_status_parser.add_argument("-u", "--user", required=True, help="User ID")

    # session (with subparsers) — the native brain's per-attempt transcripts
    session_parser = subparsers.add_parser(
        "session", help="Native-brain session logs (transcripts)",
    )
    session_subparsers = session_parser.add_subparsers(
        dest="session_action", required=True,
    )

    session_list_parser = session_subparsers.add_parser(
        "list", help="List session logs, newest first",
    )
    session_list_parser.add_argument("-u", "--user", help="Only this user's logs")
    session_list_parser.add_argument(
        "-t", "--task", type=int, help="Only this task id (every attempt)",
    )
    session_list_parser.add_argument(
        "-n", "--limit", type=int, default=20, help="Max rows (0 = all)",
    )

    session_show_parser = session_subparsers.add_parser(
        "show", help="Render one session log as readable text",
    )
    session_show_parser.add_argument(
        "target", help="Path to a .jsonl log, or a bare task id",
    )
    session_show_parser.add_argument(
        "--attempt", type=int, help="Which attempt, when the target is a task id",
    )
    session_show_parser.add_argument(
        "--no-thinking", action="store_true", help="Omit thinking blocks",
    )
    session_show_parser.add_argument(
        "--full", action="store_true",
        help="Show every record (disables the display cap, not the writer's caps)",
    )
    session_show_parser.add_argument(
        "--max-chars", type=int, default=200_000,
        help="Display cap in characters of record JSON (0 = none)",
    )

    session_tail_parser = session_subparsers.add_parser(
        "tail", help="Follow a live session log",
    )
    session_tail_parser.add_argument(
        "target", help="Path to a .jsonl log, or a bare task id",
    )
    session_tail_parser.add_argument(
        "--attempt", type=int, help="Which attempt, when the target is a task id",
    )
    session_tail_parser.add_argument(
        "-n", "--lines", type=int, default=20,
        help="How many existing records to print first (0 = all)",
    )
    session_tail_parser.add_argument(
        "--no-follow", action="store_true", help="Print and exit rather than following",
    )
    session_tail_parser.add_argument(
        "--interval", type=float, default=0.5, help="Poll interval in seconds",
    )
    # Bounded polling, so the follow loop is reachable from a test without one
    # sitting in a terminal deciding when it has seen enough.
    session_tail_parser.add_argument("--poll-limit", type=int, default=None,
                                     help=argparse.SUPPRESS)

    session_stats_parser = session_subparsers.add_parser(
        "stats", help="Count and size the logs, split by user",
    )
    session_stats_parser.add_argument(
        "--days", type=int, default=0, help="Only files written in the last N days",
    )

    # chat (with subparsers)
    chat_parser = subparsers.add_parser("chat", help="Web chat room maintenance")
    chat_subparsers = chat_parser.add_subparsers(dest="chat_action", required=True)
    chat_backfill_parser = chat_subparsers.add_parser(
        "backfill-history",
        help="Recover dormant rooms' transcripts from the Talk message cache",
    )
    chat_backfill_parser.add_argument(
        "-t", "--token", help="Single room token (default: all Talk-origin rooms)",
    )

    # money (with subparsers)
    from istota import cli_money
    cli_money.add_subparser(subparsers)

    # nextcloud (with subparsers)
    nc_parser = subparsers.add_parser("nextcloud", help="Nextcloud server operations")
    nc_subparsers = nc_parser.add_subparsers(dest="nextcloud_action", required=True)
    nc_caps_parser = nc_subparsers.add_parser(
        "capabilities", help="What the configured Nextcloud server supports",
    )
    nc_caps_parser.add_argument("--raw", action="store_true", help="Full capabilities payload")
    nc_caps_parser.add_argument(
        "--check",
        default=None,
        help="Comma list of dotted feature names; exits non-zero if any is missing",
    )
    nc_rooms_parser = nc_subparsers.add_parser(
        "provision-rooms",
        help="Create a user's default Talk rooms and seed log/alerts channels",
    )
    nc_rooms_parser.add_argument("--user", required=True, help="Nextcloud user id")
    nc_rooms_parser.add_argument(
        "--room",
        action="append",
        default=None,
        help="Room name to provision; repeatable (default: general, logs, alerts)",
    )
    nc_rooms_parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Create the rooms but don't write log_channel/alerts_channel",
    )
    nc_rooms_parser.add_argument(
        "--reseed",
        action="store_true",
        help="Re-point log_channel/alerts_channel at these rooms, overwriting what's set",
    )
    nc_rooms_parser.add_argument(
        "--adopt",
        action="append",
        default=None,
        metavar="NAME=TOKEN",
        help="Record an existing Talk room as the provisioned one and exit; "
             "repeatable. Use it to point the record at a room you renamed",
    )
    nc_rooms_parser.add_argument("--json", action="store_true", help="Machine-readable output")

    # bot-icon (with subparsers)
    bot_icon_parser = subparsers.add_parser(
        "bot-icon", help="The deployment's bot icon in the web UI",
    )
    bot_icon_subparsers = bot_icon_parser.add_subparsers(
        dest="bot_icon_action", required=True,
    )
    bot_icon_set_parser = bot_icon_subparsers.add_parser(
        "set", help="Store an image file as the bot icon (idempotent by content)",
    )
    bot_icon_set_parser.add_argument("path", help="Path to a JPEG, PNG, WebP, GIF or HEIC")
    bot_icon_subparsers.add_parser("clear", help="Remove the bot icon")
    bot_icon_subparsers.add_parser("show", help="Show what is stored, never the bytes")

    # experimental
    exp_parser = subparsers.add_parser("experimental", help="Experimental feature flags")
    exp_subparsers = exp_parser.add_subparsers(dest="experimental_action", required=True)
    exp_subparsers.add_parser("list", help="List known feature flags with on/off status")

    args = parser.parse_args()

    # Load config and setup logging (except for init/setup which don't need — or
    # may pre-date — a config file).
    if args.command not in ("init", "setup"):
        config = load_config(Path(args.config) if args.config else None)
        setup_logging(config, verbose=args.verbose)

    commands = {
        "init": cmd_init,
        "doctor": cmd_doctor,
        "task": cmd_task,
        "run": cmd_run,
        "list": cmd_list,
        "usage": cmd_usage,
        "show": cmd_show,
        "resource": cmd_resource,
        "briefing": cmd_briefing,
        "secret": cmd_secret,
        "email": cmd_email,
        "repl": cmd_repl,
        "serve": cmd_serve,
        "setup": cmd_setup,
        "update": cmd_update,
    }

    if args.command == "user":
        user_commands = {
            "list": cmd_user_list,
            "lookup": cmd_user_lookup,
            "init": cmd_user_init,
            "status": cmd_user_status,
            "ensure": cmd_user_ensure,
            "show": cmd_user_show,
            "remove": cmd_user_remove,
        }
        user_commands[args.user_action](args)
    elif args.command == "calendar":
        calendar_commands = {
            "discover": cmd_calendar_discover,
            "test": cmd_calendar_test,
        }
        calendar_commands[args.calendar_action](args)
    elif args.command == "tasks-file":
        tasks_file_commands = {
            "poll": cmd_tasks_file_poll,
            "status": cmd_tasks_file_status,
        }
        tasks_file_commands[args.tasks_file_action](args)
    elif args.command == "kv":
        kv_commands = {
            "get": cmd_kv_get,
            "set": cmd_kv_set,
            "list": cmd_kv_list,
            "delete": cmd_kv_delete,
            "namespaces": cmd_kv_namespaces,
            "shared-status": cmd_kv_shared_status,
        }
        kv_commands[args.kv_action](args)
    elif args.command == "session":
        session_commands = {
            "list": cmd_session_list,
            "show": cmd_session_show,
            "tail": cmd_session_tail,
            "stats": cmd_session_stats,
        }
        # A handler returning a non-zero code means it: `show` on a target that
        # resolves to nothing must not exit 0 for a script.
        rc = session_commands[args.session_action](args)
        if rc:
            sys.exit(rc)
    elif args.command == "chat":
        chat_commands = {
            "backfill-history": cmd_chat_backfill_history,
        }
        chat_commands[args.chat_action](args)
    elif args.command == "money":
        rc = cli_money.dispatch(args, config)
        if rc:
            sys.exit(rc)
    elif args.command == "nextcloud":
        nextcloud_commands = {
            "capabilities": cmd_nextcloud_capabilities,
            "provision-rooms": cmd_nextcloud_provision_rooms,
        }
        nextcloud_commands[args.nextcloud_action](args)
    elif args.command == "bot-icon":
        bot_icon_commands = {
            "set": cmd_bot_icon_set,
            "clear": cmd_bot_icon_clear,
            "show": cmd_bot_icon_show,
        }
        bot_icon_commands[args.bot_icon_action](args)
    elif args.command == "experimental":
        experimental_commands = {
            "list": cmd_experimental_list,
        }
        experimental_commands[args.experimental_action](args)
    else:
        # A handler that returns a non-zero code means it. Most return None, so
        # this is a no-op for them — but without it a handler's `return 1` is
        # thrown away and the process exits 0, which is a failure a script
        # cannot see. Mirrors the `money` branch above.
        rc = commands[args.command](args)
        if rc:
            sys.exit(rc)


if __name__ == "__main__":
    main()
