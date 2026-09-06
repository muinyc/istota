"""Briefings skill — in-process facade for the briefings content CLI.

Resolves the user's :class:`BriefingsContext` via
:func:`istota.briefings.resolve_for_user` and forwards its argv to
:mod:`istota.briefings.cli` through Click's ``CliRunner`` — a thin passthrough
so new CLI subcommands need no facade change. No subprocess, no HTTP. Mirrors
:mod:`istota.skills.feeds`.

Lets the model manage briefing blocks/sources conversationally, e.g.::

    istota-skill briefings list
    istota-skill briefings blocks list --briefing morning
    istota-skill briefings blocks add --briefing morning --title "World News"
    istota-skill briefings sources add --block 3 --kind rss --config '{"feed_ref": {...}}'
    istota-skill briefings archive list
"""

import json
import os
import sys

from istota.skills._cli import emit


def _run(args: list[str]) -> dict:
    from click.testing import CliRunner

    from istota.briefings import UserNotFoundError, ensure_initialised, resolve_for_user
    from istota.briefings.cli import cli
    from istota.config import load_config

    user_id = os.environ.get("BRIEFINGS_USER", "") or ""
    if not user_id:
        return {"status": "error", "error": "BRIEFINGS_USER not set"}

    istota_cfg = load_config()
    try:
        ctx = resolve_for_user(user_id, istota_cfg)
    except UserNotFoundError as e:
        return {"status": "error", "error": str(e)}

    ensure_initialised(ctx, app_config=istota_cfg)

    runner = CliRunner()
    result = runner.invoke(
        cli, args, obj=ctx, standalone_mode=False, catch_exceptions=True,
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
        return {"status": "error", "error": "no output from briefings CLI"}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"status": "ok", "raw": output}


def _output(data) -> None:
    emit(data)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(
            "Usage: istota-skill briefings <list|blocks|sources|archive> ...",
            file=sys.stderr,
        )
        sys.exit(1)
    _output(_run(argv))


if __name__ == "__main__":
    main()
