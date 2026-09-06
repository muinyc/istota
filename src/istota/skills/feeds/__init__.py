"""Feeds skill — in-process facade for the native feeds CLI.

Resolves the user's :class:`FeedsContext` via
:func:`istota.feeds.resolve_for_user` and invokes
:mod:`istota.feeds.cli` through Click's ``CliRunner``. No subprocess,
no HTTP. Mirrors :mod:`istota.skills.money`.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from istota.skills._cli import emit, error_envelope, run_skill_cli


def _run(args: list[str]) -> dict:
    """Resolve the user's FeedsContext, invoke feeds.cli.cli, return parsed JSON."""
    from click.testing import CliRunner

    from istota.config import load_config
    from istota.feeds import UserNotFoundError, ensure_initialised, resolve_for_user
    from istota.feeds.cli import cli

    user_id = os.environ.get("FEEDS_USER", "") or ""
    if not user_id:
        return {"status": "error", "error": "FEEDS_USER not set"}

    istota_cfg = load_config()
    try:
        feeds_ctx = resolve_for_user(user_id, istota_cfg)
    except UserNotFoundError as e:
        return {"status": "error", "error": str(e)}

    ensure_initialised(feeds_ctx)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["-u", user_id, *args],
        obj=feeds_ctx,
        standalone_mode=False,
        catch_exceptions=True,
    )

    if result.exception is not None and not isinstance(result.exception, SystemExit):
        return {
            "status": "error",
            "error": f"{type(result.exception).__name__}: {result.exception}",
        }
    if result.exit_code not in (0, None):
        return {
            "status": "error",
            "error": (result.output or f"exit {result.exit_code}").strip(),
        }

    output = (result.output or "").strip()
    if not output:
        return {"status": "error", "error": "no output from feeds CLI"}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        # Some commands (export-opml without --output) emit raw OPML on stdout.
        return {"status": "ok", "raw": output}


def _output(data) -> None:
    emit(data)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_list(args):
    _output(_run(["list"]))


def cmd_categories(args):
    _output(_run(["categories"]))


def cmd_entries(args):
    cli_args = ["entries"]
    if args.status:
        cli_args += ["--status", args.status]
    if args.feed_id:
        cli_args += ["--feed-id", str(args.feed_id)]
    if args.category_id:
        cli_args += ["--category-id", str(args.category_id)]
    if args.category:
        cli_args += ["--category", args.category]
    if args.limit is not None:
        cli_args += ["--limit", str(args.limit)]
    if args.offset is not None:
        cli_args += ["--offset", str(args.offset)]
    if args.before is not None:
        cli_args += ["--before", str(args.before)]
    if args.order:
        cli_args += ["--order", args.order]
    if args.direction:
        cli_args += ["--direction", args.direction]
    _output(_run(cli_args))


def cmd_add(args):
    cli_args = ["add", "--url", args.url]
    if args.title:
        cli_args += ["--title", args.title]
    if args.category:
        cli_args += ["--category", args.category]
    if args.poll_interval_minutes is not None:
        cli_args += ["--poll-interval-minutes", str(args.poll_interval_minutes)]
    _output(_run(cli_args))


def cmd_remove(args):
    cli_args = ["remove"]
    if args.url:
        cli_args += ["--url", args.url]
    if args.id is not None:
        cli_args += ["--id", str(args.id)]
    _output(_run(cli_args))


def cmd_refresh(args):
    cli_args = ["refresh"]
    if args.id is not None:
        cli_args += ["--id", str(args.id)]
    _output(_run(cli_args))


def cmd_poll(args):
    cli_args = ["poll"]
    if args.limit is not None:
        cli_args += ["--limit", str(args.limit)]
    _output(_run(cli_args))


def cmd_run_scheduled(args):
    cli_args = ["run-scheduled"]
    if args.limit is not None:
        cli_args += ["--limit", str(args.limit)]
    _output(_run(cli_args))


def cmd_prune(args):
    cli_args = ["prune"]
    if args.dry_run:
        cli_args += ["--dry-run"]
    _output(_run(cli_args))


def _scoped(raw: str, *, writable: bool, operation: str) -> str:
    """The resolved host path for an OPML argument, or an error envelope out.

    Both OPML verbs take a *host* path and this facade runs host-side — the
    proxy spawns it outside the sandbox with the daemon's filesystem view — so
    the read was an arbitrary-file read whose parse errors quote the file back,
    and the write an arbitrary write as the daemon user. Scoped to the roots
    the sandbox binds for this caller.

    The resolved path is what goes down to the Click CLI. Handing it the
    original would re-walk every symlink the check just settled, in a process
    that opens the path itself.
    """
    from istota.skill_host_paths import resolve_host_path

    resolved, err = resolve_host_path(
        Path(raw), writable=writable, operation=operation,
    )
    if err:
        # `emit` exits 1 on an error envelope, so this does not return.
        _output(error_envelope(err))
    return str(resolved)


def cmd_import_opml(args):
    cli_args = ["import-opml", _scoped(
        args.path, writable=False, operation="feeds import-opml",
    )]
    _output(_run(cli_args))


def cmd_export_opml(args):
    cli_args = ["export-opml"]
    if args.output:
        cli_args += ["--output", _scoped(
            args.output, writable=True, operation="feeds export-opml --output",
        )]
    _output(_run(cli_args))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.feeds",
        description="Native feeds (RSS / Atom / Tumblr / Are.na)",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List subscribed feeds")
    sub.add_parser("categories", help="List categories")

    p_ent = sub.add_parser("entries", help="List entries")
    p_ent.add_argument("--status", choices=["unread", "read", "removed"])
    p_ent.add_argument("--feed-id", type=int)
    p_ent.add_argument("--category-id", type=int)
    p_ent.add_argument("--category", help="Category slug")
    p_ent.add_argument("--limit", type=int)
    p_ent.add_argument("--offset", type=int)
    p_ent.add_argument("--before", type=int, help="Unix ts; only entries published before this")
    p_ent.add_argument("--order", choices=["published_at", "created_at", "id"])
    p_ent.add_argument("--direction", choices=["asc", "desc"])

    p_add = sub.add_parser("add", help="Subscribe to a feed")
    p_add.add_argument("--url", required=True)
    p_add.add_argument("--title")
    p_add.add_argument("--category")
    p_add.add_argument("--poll-interval-minutes", type=int)

    p_rm = sub.add_parser("remove", help="Unsubscribe from a feed")
    p_rm.add_argument("--url")
    p_rm.add_argument("--id", type=int)

    p_ref = sub.add_parser("refresh", help="Mark feeds as due for next poll")
    p_ref.add_argument("--id", type=int, help="Feed id; omit for all feeds")

    p_poll = sub.add_parser("poll", help="Poll all due feeds now")
    p_poll.add_argument("--limit", type=int)

    p_run = sub.add_parser("run-scheduled", help="Periodic poll entry point used by the scheduler")
    p_run.add_argument("--limit", type=int)

    p_prune = sub.add_parser(
        "prune", help="Apply the entry retention policy (run daily by the scheduler)",
    )
    p_prune.add_argument(
        "--dry-run", action="store_true",
        help="Report what would go; delete nothing",
    )

    p_imp = sub.add_parser("import-opml", help="Import an OPML file")
    p_imp.add_argument(
        "path", help="Path to OPML file, inside your own workspace",
    )

    p_exp = sub.add_parser("export-opml", help="Export subscriptions as OPML")
    p_exp.add_argument(
        "--output", "-o",
        help="Write to this file inside your own workspace instead of stdout",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "list": cmd_list,
        "categories": cmd_categories,
        "entries": cmd_entries,
        "add": cmd_add,
        "remove": cmd_remove,
        "refresh": cmd_refresh,
        "poll": cmd_poll,
        "run-scheduled": cmd_run_scheduled,
        "prune": cmd_prune,
        "import-opml": cmd_import_opml,
        "export-opml": cmd_export_opml,
    }
    if args.command not in commands:
        parser.print_help()
        sys.exit(1)

    # Every handler prints its own envelope and returns nothing, so the
    # epilogue's job here is the facade's rule that a raised exception comes
    # back as one JSON line and exit 1 rather than a traceback on stderr.
    run_skill_cli(commands, args, handlers_print=True)


if __name__ == "__main__":
    main()
