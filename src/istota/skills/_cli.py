"""The skill CLI facade: one JSON envelope on stdout, and the exit code with it.

Every skill CLI answers the same way — a JSON object on stdout and an exit
status — and the status is not decoration. The scheduler detects the
module-skill facade convention and treats a non-zero exit as a failed step, so
a handler that *returns* ``{"status": "error", ...}`` has to fail the task just
as a raised exception does. That rule was stated six ways across eight files
before this module existed: five copies checked the status inside the ``try``,
``skills/email`` moved it outside and wrote down why, ``skills/browse`` and
``skills/code_review`` each grew their own, and ``skills/kv`` and
``skills/location`` had sites that printed an error envelope and exited 0.

Imports ``json`` and ``sys`` and nothing else, so a skill subprocess pays
nothing for it beyond what ``istota.skills.__init__`` already costs.

Two things a reader will want to know before converting the next call site.

**A handler that returns ``None`` has already printed.** Several skills
(``kv``, ``location``, ``health``, ``feeds``) are written the other way round —
each ``cmd_*`` prints its own envelope through ``emit`` and returns nothing.
``run_skill_cli`` prints only what a handler hands back, so both shapes work
and neither prints twice.

**``sys.exit`` raises ``SystemExit``, which is not an ``Exception``.** That is
what lets a handler emit-and-exit from inside ``run_skill_cli``'s ``try``
without the epilogue catching it and rewriting the envelope, and it is why the
status check outside the ``try`` needs no guard against re-entry.

The one deliberate non-consumer is ``istota/ocr_leaf.py``, which carries the
same epilogue and may not import it: that module's whole contract is that it
imports the standard library, Pillow and pytesseract and nothing from
``istota``, pinned by
``tests/test_transcribe_out_of_process.py::TestTheChildImportSurface``.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, NoReturn


def error_envelope(message: str, **extra: Any) -> dict:
    """The error envelope every skill returns, built in one place."""
    return {"status": "error", "error": message, **extra}


def is_error(payload: Any) -> bool:
    """Whether a handler's return value reports a failure.

    ``isinstance`` rather than ``payload.get`` because a handler may return a
    list — ``skills/nextcloud`` has several — and a list has no ``get``.
    """
    return isinstance(payload, dict) and payload.get("status") == "error"


def status_exit_code(payload: Any) -> int:
    """0 when the envelope reports success, 1 otherwise.

    For the two skills — ``memory`` and ``ntfy`` — whose ``main`` *returns* an
    exit code rather than calling ``sys.exit``, so their ``__main__`` can pass
    it on. Note the asymmetry with `is_error`, which is deliberate and is the
    behaviour both already had: this asks whether the status is ``ok``, so a
    third status such as ``skipped`` is non-zero here and zero there.
    """
    return 0 if isinstance(payload, dict) and payload.get("status") == "ok" else 1


def emit(
    payload: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    default: Callable[[Any], Any] | None = None,
    exit_on_error: bool = True,
) -> None:
    """Print one JSON envelope, and exit 1 if it reports an error.

    The four keyword arguments are the serialization each converted call site
    already used; the defaults are the majority shape. They are knobs rather
    than a single format because the envelope is what the model reads, and
    re-serializing nine skills' output under a refactor is a user-visible
    change nobody asked for.
    """
    print(json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii, default=default))
    if exit_on_error and is_error(payload):
        sys.exit(1)


def fail(message: str, **extra: Any) -> NoReturn:
    """Print an error envelope and exit 1.

    Compact and ASCII-escaped, which is what every ``_fail`` variant and every
    ``except`` branch this replaced already emitted.
    """
    print(json.dumps(error_envelope(message, **extra)))
    sys.exit(1)


def run_skill_cli(
    commands: dict,
    args: Any,
    *,
    command: str | None = None,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    default: Callable[[Any], Any] | None = None,
    on_exception: Callable[[BaseException], Any] | None = None,
    error_ensure_ascii: bool = True,
    handlers_print: bool = False,
) -> None:
    """Dispatch one skill subcommand and apply the facade's exit-code rule.

    ``on_exception`` maps a raised exception onto the envelope to print; the
    default names the exception's message. It is where a skill that needs to
    say more than ``str(exc)`` — ``browse`` naming the endpoint it could not
    reach, ``devbox`` classifying a protocol refusal — puts that, so the
    epilogue itself stays one shape.

    A raised exception is serialized compact rather than through the ``indent``
    the result path uses, because that is what all ten converted ``except``
    branches already printed. It keeps the result path's ``default``, since
    ``skills/nextcloud`` printed both through one helper carrying ``str``.
    ``error_ensure_ascii`` is the one place they disagreed and ``devbox`` is the
    one caller that passes it.

    ``handlers_print`` is for the skills written the other way round — every
    ``cmd_*`` prints its own envelope and exits — where the return value means
    nothing and printing it would be a second envelope. Stated by the caller
    rather than inferred from a ``None`` return, because inferring it makes the
    epilogue's behaviour depend on a property no signature declares.

    The status check runs **outside** the ``try``: a returned error envelope
    must fail the task just as a raised exception does, and running it inside
    would make an emitting handler's own ``SystemExit`` look like a dispatch
    failure to any future ``except BaseException``.
    """
    name = args.command if command is None else command
    handler = commands.get(name)
    if handler is None:
        fail(f"unknown command: {name!r}")

    try:
        result = handler(args)
    except Exception as exc:
        envelope = (
            error_envelope(str(exc)) if on_exception is None else on_exception(exc)
        )
        print(json.dumps(envelope, ensure_ascii=error_ensure_ascii, default=default))
        sys.exit(1)

    if handlers_print:
        return

    if result is not None:
        emit(result, indent=indent, ensure_ascii=ensure_ascii, default=default,
             exit_on_error=False)

    if is_error(result):
        sys.exit(1)
