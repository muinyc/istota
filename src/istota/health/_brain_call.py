"""The one daemon-side brain call the health module makes (F10, ISSUE-395/397).

Four functions built the same ``BrainRequest`` — ``ocr``, ``encounter_ocr`` and
``immunization_ocr`` byte for byte apart from a log prefix and an ``origin``
string, and ``explainer`` as a shorter text-only variant. Three of the four
therefore carried a copy of the fail-closed refusal below, and the fourth
carried a copy of the env narrowing. Each is now stated here once and the four
keep their ``_call_brain`` names as thin callers.

Not a leaf: it imports the brain factory and the sandbox builder. Both imports
are inside the function rather than at module scope, as all four copies already
did — ``executor`` imports ``briefings.generate``, and a module-scope import
from any health caller risks closing a cycle back through it.

**Two things differ per caller and both are parameters rather than branches.**
``origin`` is the ``task_usage`` row's origin and is what a cost breakdown
groups on. ``log_prefix`` is what an operator greps for, and it is *not* always
``origin``: the encounter and immunization extractors log under
``health_enc_ocr`` / ``health_imm_ocr`` while persisting under
``health_encounter_ocr`` / ``health_immunization_ocr``. Deriving one from the
other would rewrite log lines this consolidation is not meant to touch, so it
defaults to ``origin`` and the two OCR callers that need a different one say so.

``sandboxed=False`` is the explainer, and it is a preserved difference rather
than an oversight: that call grants no tool, so ``build_claude_cli_flags`` adds
no ``--dangerously-skip-permissions`` and there is no wider grant for a
namespace to be containing. Building one there would change where the process
runs on every explain request, which is a runtime change and not a tightening.

**That parameter is also the one thing this consolidation could have made
worse, and the guard below is why it does not.** In the four separate bodies
the pairing was structural: the three that took a ``read_path`` built a
namespace in the same function and refused in the same function, and the
explainer had no ``read_path`` parameter at all, so "grant ``Read`` and do not
wrap" was not a thing anyone could write. One function with two keywords makes
it writable — and the tree-wide AST guard that would have caught it
(``tests/test_brain_request_confinement.py::test_a_file_tool_grant_carries_a_
sandbox_wrap``) cannot: it requires the ``sandbox_wrap=`` keyword to be present
and not a literal ``None``, and what it now sees is a bare name that is ``None``
on a live branch. So the two are re-paired at run time instead, at the top of
the function, before anything else happens.

``logger`` is the fourth per-caller value and is passed for the same reason
``log_prefix`` is. ``logging_setup`` formats every console and file line as
``[%(name)-18s]``, so the record name is on the line an operator reads;
collapsing four onto this module's own would have moved every health warning
line while the message prefix stayed put, which is a change in exactly the
thing the prefix is preserved for.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: The document extractors' ceiling. The explainer passes its own 120.
DEFAULT_TIMEOUT_SECONDS = 180


def call_health_brain(
    prompt: str,
    config,
    *,
    origin: str,
    log_prefix: str | None = None,
    log: logging.Logger | None = None,
    user_id: str = "",
    read_path: Path | None = None,
    sandboxed: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Run one health prompt through the active brain.

    Returns the raw response text, or ``None`` on any failure — no caller of
    this has an error path that wants an exception, and each renders
    "extraction unavailable, add the rows by hand" instead.

    ``read_path`` is the document a vision-mode prompt names by absolute path.
    Passing it is what grants the ``Read`` tool, and it is simultaneously the
    only path ``Read`` may touch — the two travel together so that granting the
    tool without a root is not expressible (ISSUE-395). The root is the file
    rather than its directory: on a shared deployment the uploads directory
    holds other users' documents.

    ``fs_read_roots`` is an allowlist, and its absent value means "no
    allowlist" rather than "nothing allowed" (``session/tools/env.py``), so an
    empty list here would leave the tools unconfined. That is why the caller
    names a file rather than passing a possibly-empty list.

    Confinement covers ``NativeBrain``, whose file tools read these roots.

    **The Claude Code brains ignore those roots entirely and take their
    boundary from bubblewrap**, which is what ``sandbox_wrap`` supplies
    (ISSUE-397). Without it the grant was far wider than it reads:
    ``build_claude_cli_flags`` treats a non-empty ``allowed_tools`` as the
    signal to add ``--dangerously-skip-permissions`` and no ``--allowedTools``
    allowlist at all, so the CLI ran its full default toolset — ``Bash`` and
    ``Write`` included — host-side as the daemon user, on the default
    deployment, driven by a prompt whose input is an uploaded document.

    The wrap is passed on both branches rather than only on the vision one, so
    "an OCR call runs in a namespace" is a property of the call rather than of
    which branch it took. ``build_daemon_sandbox`` names the document in
    ``extra_ro_binds``: the ``{mount}/Users/{user_id}`` bind covers a panel's
    upload, but the encounter and immunization routes hand over a temp copy and
    ``python -m istota.health.ocr`` an arbitrary local file, and a wrap that
    hides the document is an outage rather than a boundary.
    """
    prefix = log_prefix or origin
    log = log or logger
    if read_path is not None and not sandboxed:
        # The one combination the four separate bodies could not express, and
        # the one the AST guard cannot see (see the module docstring). It is a
        # programming error rather than a runtime condition, but it is refused
        # rather than raised because every caller of this treats `None` as
        # "extraction unavailable" and none of them catches anything.
        log.error(
            "%s_unconfined_grant_refused user_id=%r — a Read grant may not be "
            "paired with sandboxed=False", prefix, user_id,
        )
        return None
    try:
        from istota.brain import BrainRequest, make_brain  # noqa: PLC0415
    except ImportError as e:
        log.warning("%s_brain_import_failed error=%s", prefix, e)
        return None
    if config is None:
        return None
    try:
        brain = make_brain(config.brain)
        model = brain.resolve_model_name("general")
    except Exception as e:  # noqa: BLE001
        log.warning("%s_brain_init_failed error=%s", prefix, e)
        return None
    # Imported here rather than at module scope: `executor` imports
    # `briefings.generate`, and a top-level import from any of these callers
    # risks closing a cycle back through it.
    from istota.executor import (  # noqa: PLC0415
        build_daemon_sandbox,
        build_model_cli_env,
        persist_brain_usage,
    )

    if sandboxed:
        sandbox = build_daemon_sandbox(
            config, user_id, extra_ro_binds=[read_path] if read_path else None
        )
        if sandbox.refused and read_path:
            # Fail closed. A namespace was wanted and could not be built, and
            # the tool grant below is only safe inside one — on the Claude
            # brains it is the CLI's whole default toolset, confined by nothing
            # else. Better no extraction than an unconfined one: the caller
            # renders "extraction unavailable, add the rows by hand", which is
            # a recoverable answer.
            log.warning(
                "%s_sandbox_refused user_id=%r — not granting Read "
                "outside a namespace", prefix, user_id,
            )
            return None
        cwd = sandbox.work_dir
        sandbox_wrap = sandbox.wrap
    else:
        # The explainer: no tool grant, so no namespace and no per-user work
        # dir. `config.temp_dir` is the shared root and is what that call has
        # always used as its cwd.
        cwd = Path(getattr(config, "temp_dir", None) or "/tmp")
        sandbox_wrap = None

    req = BrainRequest(
        prompt=prompt,
        allowed_tools=["Read"] if read_path else [],
        cwd=cwd,
        # Not `dict(os.environ)`: this is a daemon-side call with no task
        # behind it, so nothing has stripped the master Fernet key, the
        # Nextcloud app password, the mail passwords or the forge tokens
        # (ISSUE-395). `build_model_cli_env` is the existing answer for a
        # daemon-side model spawn that is not a task (ISSUE-232).
        env=build_model_cli_env(config),
        fs_read_roots=[read_path] if read_path else None,
        # The Claude brains' filesystem boundary (ISSUE-397). `NativeBrain`
        # reads `native_sandbox_wrap` and not this one, and is confined by the
        # roots above instead.
        sandbox_wrap=sandbox_wrap,
        timeout_seconds=timeout_seconds,
        model=model,
        streaming=False,
    )
    try:
        result = brain.execute(req)
    except Exception as e:  # noqa: BLE001
        log.warning("%s_brain_failed error=%s", prefix, e)
        return None

    # One call per uploaded document or explain request, with no task row
    # behind it.
    persist_brain_usage(
        config, None, usage=result.usage, origin=origin,
        user_id=user_id, brain_kind=result.brain_kind,
        model=result.model_used or req.model,
        stop_reason=result.stop_reason, success=result.success,
    )

    if not result.success:
        log.warning(
            "%s_brain_unsuccessful stop_reason=%s",
            prefix, getattr(result, "stop_reason", "?"),
        )
        return None
    return result.result_text or ""


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "call_health_brain"]
