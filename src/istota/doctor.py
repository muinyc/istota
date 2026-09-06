"""Runtime self-check: every environmental fact istota depends on, named once.

A whole class of istota bug is invisible to the test suite by construction. The
suite asserts against Python objects on a developer's macOS host; production is
a built image, a rendered ``config.toml``, a ``PATH`` and a bubblewrap
namespace. The forge-CLI failures (ISSUE-263 and its neighbours) were all the
same shape — a disagreement between what the code assumed about its runtime and
what the runtime was — and none of them was a Python defect.

This module writes those assumptions down. Each check answers one question
about the host, returns a :class:`CheckResult`, and never raises. It runs at
daemon start-up, on a scheduler interval, from ``istota doctor``, from the admin
dashboard, from ``!check`` and from the ``self-check`` heartbeat. It is also the
oracle the image and smoke test tiers reuse instead of hand-writing assertions
that drift from the code.

The last two are recent. ``commands.cmd_check`` and ``heartbeat._check_self``
each carried a near-verbatim copy of the other — the same five probes in the
same order, about 180 duplicated lines — and both had drifted from this registry
and from each other. ``tests/test_doctor.py::test_no_hand_rolled_health_probe``
is what stops them growing back.

Two constraints shape the design and are easy to violate by accident:

**No check on the config-load path may spawn a process.** ``_validate_forge_clis``
is called unconditionally from ``load_config``, and ``load_config`` runs in the
daemon, the web app, the webhook receiver, every CLI invocation, and every
host-side skill CLI the skill proxy spawns *per call*. ``probe=False`` is what
keeps a free ``os.path.exists`` from becoming five ``--version`` spawns.

**A check can only be an oracle for a test if the test names the environment
that makes it run.** The ``developer.*`` checks ``SKIP`` when no token is
configured — correct for an operator, and fatal for a test, because a suite
asserting "no FAIL" is green on exactly the broken image. Callers asserting over
doctor must assert the checks they care about did not ``SKIP``.

Plain functions over plain data: no classes beyond the frozen result record, and
no decorator-driven registration — a decorator makes the set of checks depend on
what happened to be imported.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Collection, Iterable
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from . import du, sqlite_util

if TYPE_CHECKING:  # pragma: no cover - typing only; a runtime import is a cycle
    from .config import Config
    from .subscription_usage import UsageSnapshot, UsageWindow

logger = logging.getLogger(__name__)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# What a check is a property *of*. An image-scoped check can be answered by a
# bare `docker run` with no volumes; a deployment-scoped one needs a real
# install (a mount, a database, a network). The image test tier asserts only
# over the former — without the split it would fail on a perfectly good image
# (no /mnt/shared, no DB), and the tempting repair is to soften the runtime
# check, which weakens the product to make a test green.
IMAGE, DEPLOYMENT = "image", "deployment"

# How long a probed subprocess gets. Doctor runs on the start-up path and from
# an HTTP handler; an unbounded wait on a wedged binary is an outage.
PROBE_TIMEOUT = 10

# The deep sandbox probe spawns bubblewrap around a shell. Bounded separately
# and more generously, and a timeout is reported as FAIL rather than hanging.
DEEP_TIMEOUT = 30

# How long the live model probe gets. The same 30s both hand-rolled health
# probes used, kept as its own name rather than borrowing DEEP_TIMEOUT: that
# one is the deep phase's budget and `web_app._doctor_deep_timeout` does
# arithmetic on it, so sharing the constant would couple two unrelated waits.
MODEL_PROBE_TIMEOUT = 30

# Below this length a configured "credential" is a placeholder or a mode string,
# not something worth scanning rendered output for.
_MIN_SECRET_LEN = 8

_REDACTED = "[redacted]"


@dataclass(frozen=True)
class CheckResult:
    """One answer about the host.

    ``name`` is a stable dotted id and the only thing machine consumers key on.
    ``detail`` is one line saying what was *observed* — never what was expected,
    and never a credential. ``remedy`` says what to do about it and is required
    for every ``WARN`` and ``FAIL``; a finding an operator cannot act on is a
    log line, not a check.
    """

    name: str
    status: str
    detail: str
    remedy: str = ""
    scope: str = DEPLOYMENT


# A check takes the loaded config and whether it may spawn anything, and
# returns one result or several.
Check = Callable[["Config", bool], "CheckResult | list[CheckResult]"]


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _run(argv: list[str], *, timeout: int = PROBE_TIMEOUT) -> subprocess.CompletedProcess | None:
    """Run `argv`, returning None on anything that stops it producing output.

    Every caller is a check, and a check that let an OSError out would take the
    daemon's start-up path with it.
    """
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _executable(path: str | Path) -> bool:
    p = Path(path)
    return p.is_file() and os.access(p, os.X_OK)


def _binary_status(path: str, *, probe: bool) -> tuple[str, str]:
    """``(status, detail)`` for "is this binary usable", honouring `probe`.

    Under ``probe=False`` the answer comes from the filesystem alone and says
    so, because an operator reading the result otherwise cannot tell whether
    anything was actually executed.
    """
    p = Path(path)
    if not p.exists():
        return FAIL, f"{path} does not exist"
    if not _executable(p):
        return FAIL, f"{path} is present but not executable"
    if not probe:
        return OK, f"{path} exists and is executable (not executed: probe disabled)"
    result = _run([str(p), "--version"])
    if result is None:
        return FAIL, f"{path} could not be executed"
    if result.returncode != 0:
        return FAIL, f"{path} exited {result.returncode} on --version"
    banner = (result.stdout or result.stderr or "").strip().splitlines()
    return OK, f"{path}: {banner[0] if banner else 'ran, no version output'}"


def _dev_gate(config: "Config") -> tuple[object | None, str]:
    """The developer skill's gating, lifted verbatim from ``_validate_forge_clis``.

    Returns ``(developer_config, "")`` when the checks should run, or
    ``(None, reason)`` when they must ``SKIP``. Without this a tokenless
    developer-skill deployment goes from silent today to alerting after this
    lands, and the boot alert makes that loud.
    """
    dev = getattr(config, "developer", None)
    if dev is None or not dev.enabled:
        return None, "developer skill is disabled"
    if not dev.repos_dir:
        return None, "developer skill has no repos_dir configured"
    return dev, ""


def _looks_like_a_user_id(value: str) -> bool:
    """A GitLab username is never all ASCII digits, so this can only be an id.

    ``str.isdigit`` is Unicode-wide — Arabic-Indic digits and a superscript two
    both answer True — and none of those is a user id either, so an ASCII test
    keeps the WARN's wording ("which is a user id") true of what it matched.
    """
    return value.isascii() and value.isdigit()


def _forge_token_gate(dev) -> str:
    """"" when a forge token is configured, else the reason to SKIP."""
    if dev.gitlab_token or dev.github_token:
        return ""
    return "no forge token configured"


def _bwrap_usable() -> bool:
    """Whether bubblewrap can actually create a namespace here.

    Delegates to the executor's own cached probe so doctor and the sandbox agree
    on one answer. Imported lazily: `executor` pulls in most of the package.
    """
    try:
        from .executor import _bwrap_available

        return _bwrap_available()
    except Exception:  # pragma: no cover - defensive; never fail a check
        return False


# ---------------------------------------------------------------------------
# runtime.*
# ---------------------------------------------------------------------------


def check_platform(config: "Config", probe: bool) -> CheckResult:
    """OS and architecture, reported always.

    Everything istota knows about its own runtime has historically been asserted
    on darwin — the one platform that cannot run the sandbox. Saying so out loud
    is the cheapest check here and the one that explains the others.
    """
    system, machine = platform.system(), platform.machine()
    detail = f"{system} {machine}"
    if system == "Linux":
        return CheckResult("runtime.platform", OK, detail, scope=IMAGE)
    if getattr(config.security, "sandbox_enabled", False):
        return CheckResult(
            "runtime.platform",
            FAIL,
            f"{detail}; bubblewrap sandboxing is enabled but only runs on Linux",
            remedy=(
                "Run on Linux, or set [security] sandbox_enabled = false to "
                "accept an unsandboxed deployment."
            ),
            scope=IMAGE,
        )
    return CheckResult(
        "runtime.platform",
        WARN,
        f"{detail}; not a supported deployment platform",
        remedy="Linux + bubblewrap is the only supported deployment shape.",
        scope=IMAGE,
    )


def check_bwrap(config: "Config", probe: bool) -> CheckResult:
    """``bwrap`` is installed and runnable. The sandbox is the per-user boundary."""
    if not getattr(config.security, "sandbox_enabled", False):
        return CheckResult(
            "runtime.bwrap", SKIP, "sandbox is disabled ([security] sandbox_enabled)", scope=IMAGE
        )
    path = shutil.which("bwrap")
    if path is None:
        return CheckResult(
            "runtime.bwrap",
            FAIL,
            "bwrap is not on PATH",
            remedy="Install bubblewrap (apt-get install bubblewrap).",
            scope=IMAGE,
        )
    status, detail = _binary_status(path, probe=probe)
    return CheckResult(
        "runtime.bwrap",
        status,
        detail,
        remedy="" if status == OK else "Install a working bubblewrap; the sandbox needs it.",
        scope=IMAGE,
    )


def _reachable_kinds(config: "Config") -> frozenset[str]:
    """The brain kinds a task on this deployment could run under.

    Imported per call rather than at module scope: ``istota.brain`` pulls in
    every brain implementation, and this module is reached from the config-load
    path, where three checks run inside every ``load_config``.
    """
    from .brain import reachable_brain_kinds

    return reachable_brain_kinds(getattr(config, "brain", None))


#: The kinds that exec the ``claude`` CLI, so a deployment reaching either of
#: them needs the binary. Restated here rather than asked of a brain instance,
#: which would mean constructing one to answer a question about a name.
_CLI_BRAIN_KINDS = frozenset({"claude_code", "tmux_claude"})


def check_model_cli(config: "Config", probe: bool) -> CheckResult:
    """The ``claude`` CLI the subprocess brains exec.

    Resolved from the daemon's PATH, matching ``ClaudeCodeBrain``'s own spawn
    (``["claude", "-p", "-"]``) — a check against a path the brain does not use
    would be asserting about the wrong thing.

    Asks the *reachable* set rather than ``brain.kind``, so a deployment whose
    base kind is ``native`` but which routes a source type to the CLI, falls
    back to it, or lets a room pin it is still told when the binary is missing.
    Before that it SKIPped, and the operator found out from a failed task.
    """
    reachable = _reachable_kinds(config)
    kinds = sorted(reachable & _CLI_BRAIN_KINDS)
    if not kinds:
        return CheckResult(
            "runtime.model_cli",
            SKIP,
            "no reachable brain kind execs the `claude` CLI "
            f"(reachable: {', '.join(sorted(reachable)) or 'none'})",
            scope=IMAGE,
        )
    reached = ", ".join(kinds)
    path = shutil.which("claude")
    if path is None:
        return CheckResult(
            "runtime.model_cli",
            FAIL,
            f"{reached} is reachable on this deployment but there is no `claude` on PATH",
            remedy="Install the Claude Code CLI, or stop routing tasks to it.",
            scope=IMAGE,
        )
    status, detail = _binary_status(path, probe=probe)
    return CheckResult(
        "runtime.model_cli",
        status,
        detail,
        remedy="" if status == OK else "Reinstall the Claude Code CLI.",
        scope=IMAGE,
    )


def check_tmux(config: "Config", probe: bool) -> CheckResult:
    """``tmux``, needed only by the brain that drives the interactive TUI.

    Reachability rather than ``brain.kind``, for the reason
    ``check_model_cli`` gives: a routing entry, a fallback or a room allowlist
    can put a task on ``tmux_claude`` without it being the base kind.
    """
    reachable = _reachable_kinds(config)
    if "tmux_claude" not in reachable:
        return CheckResult(
            "runtime.tmux",
            SKIP,
            "no reachable brain kind uses tmux "
            f"(reachable: {', '.join(sorted(reachable)) or 'none'})",
            scope=IMAGE,
        )
    path = shutil.which("tmux")
    if path is None:
        return CheckResult(
            "runtime.tmux",
            FAIL,
            "tmux_claude is reachable on this deployment but there is no `tmux` on PATH",
            remedy="Install tmux, or stop routing tasks to tmux_claude.",
            scope=IMAGE,
        )
    # tmux answers `-V`, not `--version`.
    if not probe:
        return CheckResult(
            "runtime.tmux",
            OK if _executable(path) else FAIL,
            f"{path} exists and is executable (not executed: probe disabled)",
            remedy="" if _executable(path) else "Install a working tmux.",
            scope=IMAGE,
        )
    result = _run([path, "-V"])
    if result is None or result.returncode != 0:
        return CheckResult(
            "runtime.tmux",
            FAIL,
            f"{path} could not be executed",
            remedy="Install a working tmux.",
            scope=IMAGE,
        )
    return CheckResult(
        "runtime.tmux", OK, f"{path}: {(result.stdout or '').strip()}", scope=IMAGE
    )


def _native_key_holders(config: "Config") -> int:
    """How many configured users have a ``native_brain``/``api_key`` secret.

    **Its own read-only query rather than ``secrets_store.secret_exists``**, for
    the reason ``check_framework_db`` states two ways below: that helper opens
    the database read-write and commits, which materializes the ``-wal`` /
    ``-shm`` sidecars, and against a *missing* file creates a zero-byte database
    that later reads as corruption rather than as absence. A diagnostic run as
    root beside a stopped daemon would leave all of that owned by the wrong
    user. So this opens ``mode=ro`` like every other database-touching check
    here.

    Presence, never the value: no decryption, so no provider credential enters
    this process and no ``last_accessed_at`` is bumped. Gated on the store's key
    being available, since without it no stored row can be read and counting one
    would report a credential the daemon cannot use. A row stored under a
    *rotated* key still reads as present — the one false OK left, and narrower
    than decrypting here would be worth.

    One query, and the user scoping is applied in Python rather than as an
    ``IN`` clause: the parameter count would then be the deployment's user
    count, which has a SQLite limit behind it, while the rows this selects are
    bounded by however many native keys exist. Scoped at all because a row left
    behind by a removed user would otherwise report a credential no current user
    has.

    Never raises — a missing database or an unreadable table is nought holders,
    which is the direction that reports a problem rather than hiding one.
    """
    conn = None
    try:
        from . import secrets_store

        if not secrets_store.secret_key_available():
            return 0
        users = {str(u) for u in (getattr(config, "users", None) or {})}
        if not users:
            return 0
        db_path = Path(getattr(config, "db_path", "") or "")
        if not db_path.name or not db_path.exists():
            return 0
        conn = sqlite_util.connect_read_only(db_path)
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM secrets WHERE service = ? AND key = ?",
            ("native_brain", "api_key"),
        ).fetchall()
        return len({str(row[0]) for row in rows} & users)
    except Exception:  # noqa: BLE001 — a check reports, it does not propagate
        logger.debug("native key secret lookup failed", exc_info=True)
        return 0
    finally:
        if conn is not None:
            conn.close()


#: Host spellings that mean "an endpoint on this machine or this network", where
#: a native provider commonly takes no API key at all. Lexical only: a check on
#: the config-load-adjacent path must not resolve a name, so a DNS-backed
#: private host is not recognised and takes the FAIL below.
_LOCAL_NATIVE_HOSTS = frozenset({"localhost", "host.docker.internal"})
_LOCAL_NATIVE_SUFFIXES = (".local", ".internal", ".lan", ".localhost")


def _native_endpoint_is_local(base_url: object) -> bool:
    """Whether ``base_url`` names an endpoint that plausibly needs no key.

    A single-label host counts, since a bare name is not a public provider —
    the cost of being wrong is a WARN where a FAIL was wanted, and the remedy
    is printed either way.
    """
    try:
        host = (urlsplit(str(base_url or "")).hostname or "").strip().lower()
    except Exception:  # noqa: BLE001 — a malformed URL is not a local endpoint
        return False
    if not host:
        return False
    if host in _LOCAL_NATIVE_HOSTS or host.endswith(_LOCAL_NATIVE_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return address.is_loopback or address.is_private or address.is_link_local


def check_native_brain(config: "Config", probe: bool) -> CheckResult:
    """The native brain has a provider credential to run on.

    Buildable is not runnable. ``make_brain("native")`` constructs a defaulted
    dataclass and asserts nothing about a key, and the per-user overlay falls
    back to the instance key rather than refusing — so a deployment that can
    reach the native brain with no credential anywhere is one where every
    native task fails at the provider, with nothing in the registry naming it.
    ``claude`` and ``tmux`` have had a check since they were added; this one
    did not.

    Any one of three sources satisfies it: ``[brain.native] api_key``, the
    ``ISTOTA_BRAIN_NATIVE_API_KEY`` variable that normally populates that field
    at load, and a per-user ``native_brain``/``api_key`` secret, which the
    executor overlays per task. The variable is asked separately even though
    ``load_config`` folds it in, because a ``Config`` assembled any other way
    holds the variable and not the field, and telling a process that is holding
    a credential it has none is how a check stops being believed.

    **Two shapes resolve no key and are not defects**, and both WARN rather than
    FAIL, because a FAIL here is not inert: it reaches the start-up report, the
    scheduler's sweep and the ``self-check`` heartbeat, all of which alert on
    ``FAIL`` and none of which alert on ``WARN`` — so a working deployment would
    get a permanently red Health pane and a repeating alert. An
    ``openai_compat`` endpoint on this machine (Ollama, vLLM, llama.cpp) takes
    no key, and an operator can authenticate through ``extra_headers`` instead,
    which the provider merges over the ``Authorization`` header it builds. This
    check cannot confirm either is really a credential, so it says what it found
    rather than passing them as OK.

    SKIPs where ``native`` is not reachable, so it stays silent on the
    deployments it does not describe — which is most of them.
    """
    name = "runtime.native_brain"
    reachable = _reachable_kinds(config)
    if "native" not in reachable:
        return CheckResult(
            name,
            SKIP,
            "no task on this deployment can run the native brain "
            f"(reachable: {', '.join(sorted(reachable)) or 'none'})",
        )
    native = getattr(getattr(config, "brain", None), "native", None)
    if str(getattr(native, "api_key", "") or "").strip():
        return CheckResult(name, OK, "[brain.native] api_key is set")
    if (os.environ.get("ISTOTA_BRAIN_NATIVE_API_KEY") or "").strip():
        return CheckResult(name, OK, "ISTOTA_BRAIN_NATIVE_API_KEY is set")
    holders = _native_key_holders(config)
    if holders:
        return CheckResult(
            name,
            OK,
            f"a per-user native_brain/api_key secret is stored for {holders} user(s)",
        )
    remedy = (
        "Set ISTOTA_BRAIN_NATIVE_API_KEY, or store one per user with "
        "`istota secret ensure -s native_brain -k api_key`."
    )
    unresolved = (
        "no provider API key resolves from config, the environment or the "
        "secrets store"
    )
    if getattr(native, "extra_headers", None):
        return CheckResult(
            name,
            WARN,
            f"{unresolved}; [brain.native] extra_headers is set, so a credential "
            "may be travelling there — this check cannot tell",
            remedy=remedy,
        )
    base_url = getattr(native, "base_url", "")
    if _native_endpoint_is_local(base_url):
        return CheckResult(
            name,
            WARN,
            f"{unresolved}; base_url names a local endpoint, which commonly "
            "needs none",
            remedy=remedy,
        )
    return CheckResult(
        name,
        FAIL,
        f"the native brain is reachable but {unresolved}",
        remedy=remedy,
    )


def check_framework_db(config: "Config", probe: bool) -> CheckResult:
    """The framework DB opens, ``PRAGMA quick_check`` is clean, and it has a schema.

    Read-only on purpose. ``scheduler.check_db_health`` owns the ``REINDEX``
    self-repair; a diagnostic that silently mutated the thing it was diagnosing
    would make its own next answer meaningless.
    """
    import sqlite3

    from .db_health import quick_check

    db_path = Path(config.db_path)
    if not db_path.exists():
        return CheckResult(
            "runtime.framework_db",
            WARN,
            f"{db_path} does not exist",
            remedy="Run `istota init` to create the framework database.",
        )
    try:
        # Read-only, via the URI form — `sqlite_util.connect_read_only` carries
        # the reason. Deliberately not a `with`: a failure to *open* is reported
        # differently from a failure in the body below, and one block cannot
        # tell them apart.
        conn = sqlite_util.connect_read_only(db_path)
    except sqlite3.DatabaseError as exc:
        return CheckResult(
            "runtime.framework_db",
            FAIL,
            f"{db_path} could not be opened: {exc}",
            remedy="Restore the database from a snapshot (`python -m istota.db_restore`).",
        )
    try:
        issues = quick_check(conn)
        # Asked here, on the connection already open, because the property is
        # "has this file a schema" and file size is only a proxy for it
        # (ISSUE-412). SQLite reads a zero-length file as a valid empty
        # database, so `quick_check` returns no issues and this reported
        # `quick_check clean` about a file with nothing in it — but so does a
        # header-only 4096-byte file, which an interrupted `istota init` or a
        # bare `PRAGMA journal_mode=WAL` leaves behind, and a size test misses
        # that one entirely. The integrity/schema split stands: this is the
        # empty-file case, and whether a *populated* schema is missing the
        # table the daemon wants stays `check_task_failure_rate`'s.
        tables = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        return CheckResult(
            "runtime.framework_db",
            FAIL,
            f"{db_path} failed quick_check: {exc}",
            remedy="Restore the database from a snapshot (`python -m istota.db_restore`).",
        )
    finally:
        conn.close()
    if issues:
        return CheckResult(
            "runtime.framework_db",
            FAIL,
            f"{db_path}: quick_check reported {len(issues)} issue(s)",
            remedy=(
                "The scheduler's db-health sweep attempts a REINDEX; if it does not "
                "clear, restore from a snapshot (`python -m istota.db_restore`)."
            ),
        )
    if not tables:
        # A file with no schema is always either uninitialised or the wrong
        # file — the state the not-exists branch above already warns about,
        # arriving one step later, with the same remedy. The size is reported
        # because it is the fact that separates the two ways to get here, and
        # a `stat` that fails omits it rather than falling through to `OK`.
        try:
            size = f"{db_path.stat().st_size} bytes, "
        except OSError:
            size = ""
        return CheckResult(
            "runtime.framework_db",
            WARN,
            f"{db_path}: {size}no schema",
            remedy="Run `istota init` to create the framework database.",
        )
    return CheckResult("runtime.framework_db", OK, f"{db_path}: quick_check clean")


def check_writable_dirs(config: "Config", probe: bool) -> list[CheckResult]:
    """The directories the daemon writes to exist and are writable.

    One result per directory, so a failure names which one rather than making an
    operator guess from a combined line.
    """
    candidates: list[tuple[str, Path | None]] = [
        ("temp_dir", Path(config.temp_dir)),
    ]
    try:
        candidates.append(("module_db_root", config.module_db_root()))
    except Exception as exc:  # noqa: BLE001 - a misconfigured module_data_dir raises
        candidates.append(("module_db_root", None))
        module_root_error = str(exc)
    else:
        module_root_error = ""
    if config.nextcloud_mount_path is not None:
        candidates.append(("mount", Path(config.nextcloud_mount_path)))

    results: list[CheckResult] = []
    for label, path in candidates:
        name = f"runtime.writable_dirs.{label}"
        if path is None:
            results.append(
                CheckResult(
                    name,
                    FAIL,
                    f"{label} could not be resolved: {module_root_error}",
                    remedy="Fix `module_data_dir`; it must not resolve under the Nextcloud mount.",
                )
            )
            continue
        if not path.exists():
            # Not a failure by itself: several of these are created on first
            # use. What matters is whether the *parent* would allow that.
            parent = path.parent
            if parent.is_dir() and os.access(parent, os.W_OK):
                results.append(
                    CheckResult(name, OK, f"{path} does not exist yet; {parent} is writable")
                )
            else:
                results.append(
                    CheckResult(
                        name,
                        FAIL,
                        f"{path} does not exist and {parent} is not writable",
                        remedy=f"Create {path} and make it writable by the daemon's user.",
                    )
                )
            continue
        if not os.access(path, os.W_OK):
            results.append(
                CheckResult(
                    name,
                    FAIL,
                    f"{path} is not writable",
                    remedy=f"chown/chmod {path} so the daemon's user can write to it.",
                )
            )
            continue
        results.append(CheckResult(name, OK, f"{path} is writable"))
    return results


def check_mount_liveness(config: "Config", probe: bool) -> CheckResult:
    """A configured Nextcloud mount is actually mounted.

    An rclone mount that dropped leaves a plain empty directory behind, which
    every path check above happily reports as fine while every read returns
    nothing. ``ismount`` is the only cheap way to tell the two apart.

    Gated on the workspace actually being Nextcloud-backed, not merely on a path
    being configured. The local single-user install sets
    ``nextcloud_mount_path`` to a plain directory under ``~`` and nothing ever
    mounts it — asserting ``ismount`` there reports a healthy install as broken.
    ``storage_is_nextcloud`` is the existing distinction between the two shapes.
    """
    mount = config.nextcloud_mount_path
    if mount is None:
        return CheckResult(
            "runtime.mount_liveness", SKIP, "no nextcloud_mount_path configured"
        )
    if not config.storage_is_nextcloud:
        return CheckResult(
            "runtime.mount_liveness",
            SKIP,
            f"{mount} is a local workspace folder, not a mount (no Nextcloud URL configured)",
        )
    path = Path(mount)
    if os.path.ismount(path):
        return CheckResult("runtime.mount_liveness", OK, f"{path} is a mount point")
    return CheckResult(
        "runtime.mount_liveness",
        FAIL,
        f"{path} is configured as the workspace mount but is not a mount point",
        remedy=(
            "Check the rclone mount unit; a dropped mount leaves an empty directory "
            "that reads as an empty workspace."
        ),
    )


def _session_log_tree(root: Path) -> tuple[int, int]:
    """``(jsonl file count, bytes)`` in the log tree, du-style.

    ``du.tree`` measurement because that is what the sweep measures with, and
    over the same **set** it measures: the per-user directories, which are the
    first-level subdirectories of the root, at any depth within each. A file
    sitting directly in the root is in no user's directory, so the sweep's
    ceiling never sees it and neither does this — otherwise a stray file would
    inflate the figure reported against the ceiling relative to the figure the
    ceiling is actually compared with.

    A directory that cannot be read is skipped and its bytes go unreported;
    saying so in the detail line is the sweep's job (it counts them) and this is
    a size for an operator to read.
    """
    count = 0
    total = 0
    for user_dir in du.first_level_dirs(root):
        for full, info in du.iter_tree(user_dir):
            total += du.entry_bytes(info)
            if full.endswith(".jsonl"):
                count += 1
    return count, total


def _native_is_reachable(config: "Config") -> bool:
    """Whether any task on this deployment could run on the native brain.

    Not ``brain.kind == "native"``. A ``claude_code`` primary with ``fallback =
    "native"`` runs the native harness — and therefore its session-log writer —
    on every availability failover, and a ``source_type_overrides`` entry does
    the same for a whole class of task. ``brain/native.py`` builds the writer
    from ``config.brain.native.session_log`` alone and consults ``kind``
    nowhere, so a check gated on ``kind`` would SKIP on exactly the mixed-brain
    deployment nobody would think to look at.

    The scheduler's step 7b makes the stronger version of this argument and
    consults no brain kind at all, because a directory on disk outlives the
    routing that filled it. The check keeps a gate only so a deployment that has
    never run the native brain is not told about a directory it has no reason to
    have.
    """
    brain = config.brain
    if getattr(brain, "kind", "") == "native":
        return True
    if getattr(brain, "fallback", "") == "native":
        return True
    overrides = getattr(brain, "source_type_overrides", None) or {}
    return "native" in overrides.values()


# The two ways this check can open a finding, and they are not
# interchangeable. The first asserts an exposure; the second says the run could
# not establish whether there is one. Composing an unestablished answer under
# the first prefix produced a sentence that contradicted itself — "the logs are
# unbound rather than masked — whether bubblewrap works here was not probed" —
# and read, on a correctly masked deployment, as a claim the transcripts were
# exposed.
_MASK_EXPOSED = "the logs are unbound rather than masked"
_MASK_UNKNOWN = "whether the logs are masked could not be established"


def _session_log_mask_finding(config: "Config", log_dir: Path, probe: bool) -> str:
    """"" when the sandbox's database mask covers ``log_dir``, else the finding.

    Returns the whole finding sentence, prefix included, rather than a bare
    reason: the prefix depends on which axis produced it, and only this
    function knows that. See `_MASK_EXPOSED` / `_MASK_UNKNOWN`.

    **Two axes, and both are reported.** Whether a mask is emitted at all, and
    where it would land if it were. They are independent — the standalone
    install fails both at once — and neither pre-empts the other, because an
    operator who reads one reason and fixes it would otherwise be told nothing
    about the second and would still have unmasked transcripts. Availability
    leads: whether a mask exists outranks where it would go.

    **The availability axis is ``effective_sandboxing``, not ``sandbox_enabled``**
    (ISSUE-381). The flag is what the operator asked for; the predicate is what
    they got, and the two diverge on the shipped Docker stack —
    ``docker-compose.yml`` grants neither ``seccomp:unconfined`` nor
    ``systempaths=unconfined``, so the bwrap probe fails, ``build_bwrap_cmd``
    never runs and no mask is emitted while the flag still reads true. Reading
    the flag alone reported a tmpfs mask protecting the transcripts on the one
    multi-user shape where every task runs with the daemon's own filesystem
    access.

    **This asks ``_mask_dir``'s own question rather than a copy of it.** "Is the
    resolved directory under ``db_path.parent``" is the tempting predicate and it
    is wrong in the one case that matters: it answers True on the standalone
    install, where ``db_path.parent`` *is* the workspace, ``mask_shadowed_by``
    is non-empty and the mask is refused — so the check would report the
    property holding while the directory sat outside every mask. That is the
    ``map_basemap`` two-consumers failure, which is why ``mask_shadowed_by``
    was lifted out of the sandbox builder's closure instead of restated here.

    The reasons are joined with ``", and "`` rather than a semicolon because
    the caller joins *findings* with ``"; "``: one separator per nesting level,
    or a reader cannot tell which clause belongs to which finding.

    Never raises. It runs on the daemon's start-up path, and a config that makes
    a path comparison throw must come back as a finding.
    """
    reasons: list[str] = []
    established = False

    available, why = _sandbox_mask_availability(config, probe)
    if why:
        reasons.append(why)
        established = available is False

    shape = _session_log_mask_shape_reason(config, log_dir)
    if shape:
        reasons.append(shape)
        established = True

    if not reasons:
        return ""
    prefix = _MASK_EXPOSED if established else _MASK_UNKNOWN
    return f"{prefix} — " + ", and ".join(reasons)


#: Set in a task's environment by ``task_env.build_task_runtime``, and only
#: where the sandbox was really in force. So a process carrying it is *inside* a
#: bubblewrap namespace — one carrying ``--unshare-user --disable-userns``
#: wherever bwrap supports them, which is what stops a second namespace being
#: created in there.
_IN_SANDBOX_MARKER = "ISTOTA_SANDBOXED"


def _sandbox_probe_is_nested() -> bool:
    """Would a bwrap probe in this process be a nested one, and so unanswerable."""
    return bool(os.environ.get(_IN_SANDBOX_MARKER))


def _deployment_sandboxing(
    config: "Config", probe: bool,
) -> tuple[bool | None, str]:
    """Is the sandbox in force *on this deployment*, in three states.

    ``(True, "")`` it is; ``(False, "")`` it is not; ``(None, reason)`` this run
    could not establish which. The three consumers below each phrase their own
    sentence around ``reason`` and read the bool in their own direction, but the
    question and the way it is asked are one thing and live here: three copies
    of the probe-versus-memo rule is the ``map_basemap`` two-consumers failure
    waiting to happen, and the copies had already drifted into three spellings
    of the same "could not be determined".

    **The nested arm is the one that earns the function**, and it is the shape
    ``check_skill_model_credential``'s value arm already answers for a
    credential: a check reading the current process while its subject is the
    daemon. ``effective_sandboxing`` asks whether *this* process can build a
    namespace, which is the right question for the executor and the wrong one
    for a diagnostic: run from inside a task's own sandbox the probe
    fails with ``ENOSPC`` on the nesting depth — the task's namespace carries
    ``--disable-userns`` — so the check reported that every task runs
    unsandboxed, from inside a demonstrably sandboxed one, and ``istota doctor``
    exited 1 about a deployment whose boundary was working.

    Nesting is read from the environment rather than from the probe's stderr:
    the marker is a fact the daemon recorded when it built the task, while the
    text of a bwrap failure is not a contract. Two consequences, both accepted.
    A task can set the marker itself and turn a real ``FAIL`` into an
    unestablished answer — on a deployment where that FAIL is true the task
    already has the daemon's own filesystem access, so it is not the cheapest
    thing it could do, and every authoritative surface (the boot log, the
    scheduler's sweep, the admin Health pane, the heartbeat) runs in the daemon,
    where no marker exists. And a task on an *unconfined* deployment carries
    ``ISTOTA_TASK_ID`` without this marker, so the probe there is a valid one
    and the ISSUE-381 finding still reaches an operator reading a doctor run
    from inside a task.

    On a deployment whose bwrap predates ``--disable-userns`` a nested probe
    could in fact succeed, so the marker costs that shape an ``OK`` it might
    have earned. That is the safe direction, and the only one available without
    reading bwrap's stderr for a reason.

    Never raises: an unobtainable answer is ``None`` with a reason.
    """
    if _sandbox_probe_is_nested():
        return None, (
            "this process is itself inside a bubblewrap namespace "
            f"({_IN_SANDBOX_MARKER}), which is built with --disable-userns, so "
            "a probe from here cannot create a nested one and says nothing "
            "about the daemon"
        )
    try:
        from .executor import effective_sandboxing, effective_sandboxing_if_known

        # `effective_sandboxing` consults the bwrap capability probe, which
        # spawns. `probe=False` forbids that, so ask the memo instead — which is
        # usually warm in the process that matters, since the daemon probes at
        # start-up. Only a genuinely cold memo is reported as unlooked-at.
        effective = (
            effective_sandboxing(config) if probe
            else effective_sandboxing_if_known(config)
        )
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        logger.debug("sandbox availability lookup failed", exc_info=True)
        return None, "whether bubblewrap works here could not be determined"

    if effective is None:
        return None, "whether bubblewrap works here was not probed on this run"
    return effective, ""


def _sandbox_mask_availability(
    config: "Config", probe: bool,
) -> tuple[bool | None, str]:
    """Is a database mask emitted at all on this deployment, and if not, why.

    ``(True, "")`` a mask is emitted; ``(False, reason)`` none is;
    ``(None, reason)`` the run could not establish which. The third state is
    the one that earns the tuple: a check whose subject is a boundary must not
    answer "fine" when it did not look, and must not assert an exposure it did
    not observe either.

    **Two checks share it, and they read the answer in opposite directions.**
    For ``runtime.session_log_dir`` a mask is defence in depth and its absence
    is the finding; for ``runtime.task_control_dir`` a mask *over* the tree
    takes away a file every task must open, so the mask existing is the
    finding. One function either way, because a second copy of the ISSUE-381
    reasoning — ``effective_sandboxing`` rather than ``sandbox_enabled`` — is
    the ``map_basemap`` two-consumers failure waiting to happen. The reasons it
    returns are phrased about the deployment rather than about either
    directory, so both callers can quote them verbatim.

    How the answer is obtained, and the three states it comes in, are
    :func:`_deployment_sandboxing`'s: the warm-memo rule under ``probe=False``
    and the nested-probe arm are shared with the other two consumers rather
    than restated here.

    Never raises: an unobtainable answer is ``None`` with a reason, not an
    exception and not a quiet ``True``. Returning ``True`` there was the first
    draft and it reinstated ISSUE-381 in miniature — an answer nobody could get
    reported as a protection in place, with only a ``logger.debug`` behind it.
    """
    if not getattr(config.security, "sandbox_enabled", False):
        # Not "the mask covers it": there is no mask. Reported as its own reason
        # rather than skipped, because the standalone install ships this way and
        # the whole point of the check is to say which condition a deployment is
        # in. `runtime.bwrap` answers a different question — whether bubblewrap
        # could work here — and SKIPs on the same setting.
        return False, (
            "the sandbox is switched off on this deployment ([security] "
            "sandbox_enabled), so nothing masks anything"
        )

    effective, why = _deployment_sandboxing(config, probe)
    if effective is None:
        return None, f"{why}, so no mask can be confirmed"
    if not effective:
        return False, (
            "[security] sandbox_enabled is set but bubblewrap does not work "
            "here (see runtime.bwrap), so every task runs with the daemon's "
            "own filesystem access and nothing masks anything"
        )
    return True, ""


def _session_log_mask_shape_reason(config: "Config", log_dir: Path) -> str:
    """"" when a mask *would* cover ``log_dir``, else why it would not.

    The shape half of `_session_log_mask_finding`: a pure question about the
    configured paths, asked whether or not a mask is actually emitted. Kept
    separate so the availability arm above can report alongside it rather than
    instead of it.
    """
    try:
        from .executor import mask_protected_paths, mask_shadowed_by
    except Exception:  # pragma: no cover - defensive; executor is always importable
        return "the sandbox builder could not be consulted"

    if not config.db_path:
        return "no db_path is configured, so the sandbox masks no database directory"

    try:
        protected = mask_protected_paths(config)
        candidates: list[Path] = [Path(config.db_path).parent]
        try:
            candidates.append(config.module_db_root())
        except Exception:  # noqa: BLE001 - a misconfigured module_data_dir raises
            pass

        refused: list[Path] = []
        names: list[Path] = []
        for candidate in candidates:
            # Both names, exactly as `_mask_dir` emits them: under a symlinked
            # deployment root a mask lands at one and not the other.
            for name in (candidate, candidate.resolve()):
                if name in names:
                    continue
                names.append(name)
                if mask_shadowed_by(name, protected):
                    refused.append(name)
                    continue
                for probe in (log_dir, log_dir.resolve()):
                    if probe == name or probe.is_relative_to(name):
                        return ""
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
        # Not `OSError` alone. The sandbox-off shape now reaches this code,
        # which it never did while the availability arm returned early, so the
        # set of configs that get here is wider than the one it was written
        # against — and `mask_protected_paths` resolves paths from a config
        # file, where a null byte raises `ValueError` rather than `OSError`.
        return f"the mask could not be evaluated ({exc})"

    if any(
        probe == r or probe.is_relative_to(r)
        for r in refused
        for probe in (log_dir, log_dir.resolve())
    ):
        return (
            "the sandbox refuses to mask the directory above it because that "
            "directory contains the workspace or the source tree"
        )
    return "it sits outside every directory the sandbox masks"


def check_session_log_dir(config: "Config", probe: bool) -> CheckResult:
    """Where native-brain session transcripts land, and what protects them.

    The boundary is that nothing binds the directory into any sandbox; the
    database mask is defence in depth behind it, and on two shipped shapes there
    is no mask at all — the standalone install refuses it, and the Docker stack
    never emits one because the bwrap probe fails there. Which condition a
    deployment is in is not visible from a config file, so this check says it,
    and says every one that holds rather than the first.

    **It is the one check in this module that will not report ``OK`` under
    ``probe=False``**, and that is deliberate rather than an oversight. The
    module's convention for a probe it may not run is to answer from the
    filesystem and say so in the ``detail`` while still passing —
    `_binary_status` and `check_tmux` return ``OK``, `check_subscription_usage`
    and `check_sandbox_masks` return ``SKIP``. Here the subject *is* a
    boundary, and the defect being fixed was this check reporting a protection
    that was not in place; a status an operator reads as "fine" on a run that
    did not look would be the same defect wearing a flag. It reads the warm
    probe memo first, so the divergence only bites where the answer is
    genuinely unavailable.

    The second ``WARN`` arm is about retention rather than exposure: when the
    last sweep evicted by *size*, ``retention_days`` is not the retention in
    force and the effective window is a function of load. An operator who wanted
    fourteen days and is getting three should be told, not left to infer it from
    a directory listing.

    The writer and the sweep have separate gates. A disabled or unreachable
    native brain stops new files, but active retention rules still apply to the
    files already on disk, so either half makes this check relevant.

    **The two arms are composed, not raced.** Returning at the first was the
    first draft and it made the retention arm unreachable on precisely the
    deployments that most need it: the standalone shape's mask refusal and an
    operator-set ``dir`` are both *permanent* conditions, so a check that
    returned on either could never go on to say that the ceiling was what
    actually bound. Both facts land in one result.
    """
    name = "runtime.session_log_dir"
    cfg = config.brain.native.session_log
    native_reachable = _native_is_reachable(config)
    sweep_enabled = cfg.retention_days > 0 or cfg.max_total_gb > 0
    if not native_reachable and not sweep_enabled:
        return CheckResult(
            name,
            SKIP,
            "no brain routing reaches the native harness, which is the only "
            "writer of session logs",
        )
    if not cfg.enabled and not sweep_enabled:
        return CheckResult(
            name, SKIP, "session logs are disabled ([brain.native.session_log] enabled)"
        )

    from .session.session_log import (
        SWEEP_STATE_KEY,
        SWEEP_STATE_NAMESPACE,
        decode_sweep_state,
        resolve_session_log_dir,
    )

    log_dir = resolve_session_log_dir(config.db_path, cfg.dir)

    # Writable first: an unwritable directory means no transcript is being
    # written at all, which outranks anything about who else could read one.
    if log_dir.exists():
        if not log_dir.is_dir():
            return CheckResult(
                name,
                FAIL,
                f"{log_dir} exists but is not a directory",
                remedy="Move it aside, or point [brain.native.session_log] dir elsewhere.",
            )
        if not os.access(log_dir, os.W_OK):
            return CheckResult(
                name,
                FAIL,
                f"{log_dir} is not writable",
                remedy=f"chown/chmod {log_dir} so the daemon's user can write to it.",
            )
    else:
        # Nothing creates it until the first native task, so an install that has
        # not run one is healthy. What matters is whether the parent allows it.
        parent = log_dir.parent
        if not (parent.is_dir() and os.access(parent, os.W_OK)):
            return CheckResult(
                name,
                FAIL,
                f"{log_dir} does not exist and {parent} is not writable",
                remedy=f"Create {log_dir} and make it writable by the daemon's user.",
            )

    files, total = _session_log_tree(log_dir)
    plural = "" if files == 1 else "s"
    if cfg.max_total_gb > 0 and math.isfinite(cfg.max_total_gb):
        size = f"{total / 1_073_741_824:.2f} GB of {cfg.max_total_gb:.1f} GB"
    else:
        # `isfinite` because the sweep reads a non-finite ceiling as no ceiling
        # (TOML spells `inf`), and this is the one place the two consumers of
        # the setting could disagree about what it means to an operator.
        size = f"{total / 1_073_741_824:.2f} GB, no ceiling configured"
    observed = f"{log_dir}: {files} file{plural}, {size}"

    findings: list[str] = []
    remedies: list[str] = []

    mask = _session_log_mask_finding(config, log_dir, probe)
    if mask:
        findings.append(mask)
        remedies.append(
            "Nothing binds the directory into a sandbox, which is the boundary; "
            "keep [security] sandbox_ro_paths narrow so nothing starts to."
        )

    # Retention: is the ceiling what is actually reclaiming? Only worth asking
    # while the sweep is running — with both rules off the scheduler's gate is
    # false and nothing rewrites the row, so a stale `deleted_size` from before
    # they were switched off would warn for ever, about a retention rule that no
    # longer runs at all.
    if cfg.retention_days > 0 or cfg.max_total_gb > 0:
        swept = None
        try:
            from . import db as _db

            with _db.get_db(config.db_path) as conn:
                row = _db.shared_kv_get(conn, SWEEP_STATE_NAMESPACE, SWEEP_STATE_KEY)
            swept = decode_sweep_state(row["value"]) if row else None
        except Exception:  # noqa: BLE001 - no sweep record is a normal answer
            swept = None
        if swept and swept.get("still_over"):
            # Ahead of `deleted_size` because it is the worse condition: the tree
            # is over its ceiling and everything left is inside the live window
            # or could not be removed, so nothing is reclaiming it at all.
            findings.append(
                "the last sweep left the tree over its ceiling with nothing it "
                "could evict"
            )
            remedies.append(
                "Raise [brain.native.session_log] max_total_gb; the tree is over it "
                "and the sweep has nothing left it is allowed to take."
            )
        elif swept and swept.get("deleted_size"):
            findings.append(
                f"the last sweep evicted {swept['deleted_size']} file(s) by size, so "
                f"retention_days = {cfg.retention_days} is not the retention in force"
            )
            remedies.append(
                "Raise [brain.native.session_log] max_total_gb, or lower "
                "retention_days so the configured window matches what the disk allows."
            )

    if findings:
        return CheckResult(
            name,
            WARN,
            f"{observed}; " + "; ".join(findings),
            remedy=" ".join(remedies),
        )
    return CheckResult(name, OK, observed)


# The three prefixes the control tree's mask axis renders under. The split is
# `_MASK_EXPOSED` / `_MASK_UNKNOWN` above with the direction inverted: there a
# mask is defence in depth and its absence is the finding, here a mask over the
# tree takes away a file the task must open, so the mask *existing* is.
#
# `_CONTROL_MASK_UNKNOWN` is deliberately worded so it does not contain the
# established sentence as a substring. The session-log check's own lesson was a
# fixed prefix asserting the exposure in its first clause and disclaiming it in
# the second; a prefix that reads as the assertion plus a hedge is the same
# defect, and it also makes the two states indistinguishable to anything
# grepping the detail — including this module's own tests.
_CONTROL_MASKED = "the task control directory is masked out of every sandbox"
_CONTROL_WOULD_BE_MASKED = (
    "the task control directory would be masked out of every sandbox"
)
_CONTROL_MASK_UNKNOWN = (
    "whether the database mask reaches the task control directory could not be "
    "established"
)

_CONTROL_OVERLAP_REMEDY = (
    "Nothing model-writable may be at or above the control tree: the framework "
    "writes every task's assembled prompt, briefing metadata and prepared image "
    "attachments there. Move the overlapping path, or point temp_dir somewhere "
    "no bind reaches."
)

_CONTROL_TREE_REMEDY = (
    "The daemon creates each level 0700 and re-asserts the mode on every task, "
    "so it repairs a widened mode by itself: move aside anything standing at "
    "one of these paths that is not a directory, and the next task recreates it."
)

# Separate from the one above, because the action depends on something only the
# reader knows. The check compares against its own euid and cannot tell whether
# this process is the daemon, so the remedy has to offer both readings rather
# than sending an operator to chown a tree that was already correct.
_CONTROL_TREE_UID_REMEDY = (
    "Check which account the daemon runs as. If it is the uid that owns the "
    "directory, nothing is wrong and this line is only the account you ran the "
    "check from; if it is not, chown the tree to the daemon's user, since the "
    "daemon refuses a level it does not own."
)

_CONTROL_MASK_REMEDY = (
    "Point temp_dir outside the database directory, or move db_path. The "
    "database mask is the sandbox's last mount operation, so a control "
    "directory under it cannot be opened inside the namespace and every "
    "Claude Code task fails at start-up on a file its own request named."
)


def _severer(a: str, b: str) -> str:
    """The worse of two statuses, FAIL over WARN over OK."""
    order = {OK: 0, SKIP: 0, WARN: 1, FAIL: 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def _overlaps(a: Path, b: Path) -> bool:
    """Whether either path is at or inside the other.

    Both directions, exactly as ``config._warn_ro_paths_over_control_tree``
    compares: above the control tree reaches every user's, inside it reaches
    one user's or one task's — which is smaller and no more acceptable.

    A plain ``PurePath`` comparison, so it does **not** mirror
    ``get_task_control_dir``'s case-folded refusal of a ``user_id`` equal to
    ``CONTROL_DIR_NAME``. On a case-insensitive filesystem a name differing
    only in case reaches the same directory and this returns False. Left as
    written rather than case-folded: the comparison would then be wrong on
    every case-*sensitive* filesystem, which is where bubblewrap runs and
    therefore where a bind exists at all.
    """
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


# Every path this module resolves is a config value or a join of one, and
# `Path.resolve()` answers with a different exception per failure and per
# interpreter. `RuntimeError` is in the tuple for CPython 3.11, whose pathlib
# raises it on a symlink loop rather than returning; 3.12 delegates to
# `os.path.realpath` and does not. `pyproject.toml` floors at 3.11, and this
# module's contract is that it never raises.
_PATH_ERRORS = (OSError, ValueError, TypeError, AttributeError, RuntimeError)


def _control_level_findings(path: Path, label: str) -> list[tuple[str, str, str]]:
    """One level of the control tree: a real directory, ours, at 0700.

    ``(status, finding, remedy)`` triples, empty when the level is healthy.
    Applied to the root **and to each configured user's level**, because
    ``_ensure_control_level`` asserts type, ownership and mode at all three and
    fails the task from any of them — so inspecting only the root reports a
    healthy deployment while every task of one user fails at start-up.
    ``get_task_control_dir``'s containment equality does not cover it either:
    it compares paths and says nothing about who owns one.

    A missing level is not a finding. ``ensure_task_control_dir`` creates the
    tree on the first task, so an install that has not run one is healthy, and
    a user who has never had a task has no level yet.
    ``runtime.writable_dirs.temp_dir`` is the check that says whether it will
    be creatable.

    ``lstat`` rather than ``stat``, because a symlink here is one of the two
    states ``get_task_control_dir`` refuses and following it would report the
    mode and owner of whatever it points at.

    **The ownership arm is a WARN and the type arms are FAILs**, which is not
    an inconsistency. A symlink or a regular file at a level is a fact about
    the filesystem and reads the same from any process. A uid comparison is
    not: the only uid available here is *this process's*, and doctor has four
    entry points of which ``istota doctor`` is commonly run by an operator's
    own account or by root. A FAIL there would set ``exit_code`` to 1 and, on
    the scheduler's sweep, mail every admin about a tree that is exactly
    correct — the precedent `check_subscription_usage` states for never
    failing on a fact that is not a defect in the host. So the finding reports
    both uids and lets the reader see which case they are in.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [(
            WARN,
            f"{label} ({path}) could not be inspected ({exc.strerror or exc})",
            _CONTROL_TREE_REMEDY,
        )]

    if stat.S_ISLNK(st.st_mode):
        return [(
            FAIL,
            f"{label} ({path}) is a symlink rather than a directory the daemon "
            f"owns, so no task can name a control directory under it",
            _CONTROL_TREE_REMEDY,
        )]
    if not stat.S_ISDIR(st.st_mode):
        return [(
            FAIL,
            f"{label} ({path}) exists and is not a directory, so every task "
            f"under it fails at start-up",
            _CONTROL_TREE_REMEDY,
        )]

    out: list[tuple[str, str, str]] = []
    # `hasattr` because the module's contract is that it never raises and
    # `os.geteuid` does not exist on every platform — the same guard the
    # devbox identity check uses.
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    if euid is not None and st.st_uid != euid:
        out.append((
            WARN,
            f"{label} ({path}) is owned by uid {st.st_uid} while this process "
            f"runs as uid {euid}: if the daemon is not uid {st.st_uid} it will "
            f"refuse a level it does not own and every task under it fails at "
            f"start-up",
            _CONTROL_TREE_UID_REMEDY,
        ))
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o700:
        out.append((
            WARN,
            f"{label} ({path}) is mode {mode:04o} rather than 0700, so another "
            f"local account can reach the control files under it",
            _CONTROL_TREE_REMEDY,
        ))
    return out


def _control_overlap_findings(
    config: "Config", users: list[str], root: Path,
) -> list[tuple[str, str, str]]:
    """Anything the model can reach at or above the control tree.

    This is the axis the whole change rests on. The tree is a *sibling* of the
    per-user workspaces rather than a child of one, which is what removes the
    symlink-swap window ISSUE-320 measured instead of racing it — and that is a
    property of the rendered config, not of the code, so it is a property the
    suite cannot see and doctor can.

    **Every bind `build_bwrap_cmd` emits whose path a config alone determines**,
    compared in both directions. Enumerating fewer was the first draft's defect
    and it was not a cosmetic one: with ``temp_dir`` set under
    ``{developer.repos_dir}/{user_id}`` the whole tree sits inside a read-write
    bind and the check reported ``ok``.

    - ``{temp_dir}/{user_id}``, the sandbox's ``--chdir`` target and
      ``ISTOTA_DEFERRED_DIR``. Checked whatever the sandbox setting says,
      because the model writes there on every shape. A user id equal to
      ``CONTROL_DIR_NAME`` lands here, since ``get_user_temp_dir`` is a plain
      join — ``get_task_control_dir`` refuses that name, and this is what says
      why out loud rather than leaving an operator with a user whose every task
      fails and nothing in the config pointing at the cause.
    - ``security.sandbox_ro_paths``, bound verbatim.
      ``config._warn_ro_paths_over_control_tree`` already warns at load; this
      repeats it because that warning goes to a log once per process and
      doctor is the surface an operator reads.
    - ``{repos_dir}/{user_id}`` and the package cache under it, read-write for
      an admin developer task.
    - ``{mount}/Users/{user_id}`` read-write, ``{mount}/Talk`` read-only, and
      ``{mount}/Channels`` — the last as the whole directory, since the bind is
      per conversation token and doctor has no task to take one from.
    - the **per-resource mounts**, which are the reason this axis reports more
      than the guards themselves cover. A ``user_resources`` row resolves to
      ``mount / resource_path``, bounded by the Nextcloud mount root and
      nothing else, so on a layout where ``temp_dir`` sits under the mount a
      row naming the tree binds it — read-write or read-only — and neither
      ``native_fs_roots`` entry nor the ``extra_ro_binds`` bind covers a
      sibling task's directory. ``native_fs_roots``' docstring records that as
      a known gap on the grounds that no shipped shape produces the layout and
      that refusing a resource path is a decision about resources. Reporting it
      is neither of those: it costs one comparison per row, it is silent on
      every shape that ships, and a gap named only in a docstring is one
      nothing would ever notice.

    A read-only bind is reported as readily as a read-write one. Read-only over
    the tree is the read exposure rather than the write vector — one task
    reading every other task of that user's assembled prompt — which is the
    same thing the load-time ``sandbox_ro_paths`` warning is about.

    Everything but the first is gated on the *requested* ``sandbox_enabled`` —
    the same gate ``config._warn_ro_paths_over_control_tree`` takes, and for
    the same two reasons: with the sandbox off nothing is bound at all, so
    there is no bind for an entry to widen, and the effective predicate spawns.

    Never raises: a path that cannot be resolved is skipped rather than
    reported, since a path this cannot resolve is one no bind can name either.
    """
    from .executor import (  # noqa: PLC0415 - executor pulls in most of the package
        get_user_repos_dir,
        get_user_temp_dir,
        resolve_sandbox_cache_dir,
    )

    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def _check(label: str, path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = Path(path).resolve()
        except _PATH_ERRORS:
            return
        if not _overlaps(resolved, root):
            return
        finding = f"{label} ({resolved}) overlaps the control tree"
        if finding in seen:
            return
        seen.add(finding)
        out.append((WARN, finding, _CONTROL_OVERLAP_REMEDY))

    for user_id in users:
        try:
            _check(f"the workspace of user {user_id!r}", get_user_temp_dir(config, user_id))
        except _PATH_ERRORS:
            continue

    security = getattr(config, "security", None)
    if not getattr(security, "sandbox_enabled", False):
        return out

    for entry in getattr(security, "sandbox_ro_paths", None) or ():
        try:
            _check(f"the sandbox_ro_paths entry {entry!r}", Path(entry))
        except _PATH_ERRORS:
            continue

    for user_id in users:
        for label, resolver in (
            ("the repos subtree", get_user_repos_dir),
            ("the package cache", resolve_sandbox_cache_dir),
        ):
            try:
                _check(f"{label} of user {user_id!r}", resolver(config, user_id))
            except Exception:  # noqa: BLE001 - a diagnostic must not raise
                continue

    mount = getattr(config, "nextcloud_mount_path", None)
    if not mount:
        return out
    try:
        mount = Path(mount)
    except _PATH_ERRORS:
        return out

    _check("the Talk directory on the mount", mount / "Talk")
    # The whole directory rather than one token's: the bind is per
    # conversation and doctor has no task to take a token from, so the
    # question it can answer is whether the tree is reachable from any of them.
    _check("the Channels directory on the mount", mount / "Channels")

    users_map = getattr(config, "users", None)
    for user_id in users:
        _check(f"the mounted home of user {user_id!r}", mount / "Users" / user_id)
        try:
            user_config = users_map.get(user_id)
        except AttributeError:
            continue
        for resource in getattr(user_config, "resources", None) or ():
            path = getattr(resource, "path", "")
            if not path or not isinstance(path, str):
                continue
            try:
                # `lstrip("/")` exactly as `build_bwrap_cmd` and
                # `native_fs_roots` do: an absolute `resource_path` is
                # re-rooted under the mount rather than escaping it.
                candidate = mount / path.lstrip("/")
            except _PATH_ERRORS:
                continue
            # Deliberately not gated on the path existing, unlike the two
            # binds themselves. A row naming a directory that is not there yet
            # binds it the moment it appears, and the control root is created
            # by the first task that runs.
            _check(
                f"the {getattr(resource, 'type', '?')} resource {path!r} of "
                f"user {user_id!r} ({getattr(resource, 'permissions', '?')})",
                candidate,
            )
    return out


def _control_mask_finding(
    config: "Config", root: Path, probe: bool,
) -> tuple[str, str] | None:
    """``(finding, remedy)`` when the database mask reaches the control tree.

    The mask is an empty read-only tmpfs emitted as the *last* mount operation
    and cannot be worked around, so a control directory under one could never
    be opened inside the namespace: the composed system prompt would be named
    by ``BrainRequest.composed_system_prompt_path`` and unreadable at that
    path, and both Claude Code backends would fail at start-up on a file their
    own request pointed at. ``## Design`` claims the layout avoids this on all
    three shipped shapes; this is what checks the claim against the config a
    deployment actually rendered rather than against the one it was written
    from.

    **Both directions**, through `_overlaps`, for the reason that function's
    docstring gives. A mask *above* the tree takes every user's away; a mask
    *inside* it takes one user's or one task's, which is smaller and no more
    acceptable — and it is emitted rather than refused, since
    ``{temp_dir}/.control/{user}`` shadows nothing in ``mask_protected_paths``.
    Testing only the upward direction was the first draft and it reported no
    finding on a ``db_path`` inside a user's own control subtree.

    **The shape question is asked first and the availability question only
    where its answer can change the verdict.** Availability is
    ``effective_sandboxing``, which spawns; on every layout where no mask would
    reach the tree the answer cannot matter, so asking it would pay a
    subprocess per doctor run for a fact nothing reads. That is the inverse of
    ``runtime.session_log_dir``, where the absence of a mask is itself the
    finding and availability therefore has to be asked every time.

    **A mask the builder refuses is not a mask.** ``mask_shadowed_by`` is the
    sandbox builder's own predicate rather than a copy of it, for the reason
    its docstring gives: "is the tree under ``db_path.parent``" answers True on
    the standalone install, where ``db_path.parent`` is the workspace and
    ``_mask_dir`` refuses outright, so a copy would report a mask that is never
    emitted.

    Being unable to consult the builder returns ``None`` rather than a finding,
    and that is the safe direction here where it would not be in the session-log
    check: there the absence of a mask is the exposure, so silence would hide
    one; here the presence of a mask is, so an unanswerable question cannot
    have found one. No ``db_path`` is the same answer for the same reason —
    there is no database directory to mask.

    A **relative** ``db_path`` is out of scope, as a relative ``temp_dir`` is
    for the caller: ``resolve()`` would answer against the calling process's
    working directory, so the same config would report a finding in the daemon
    and none in a skill CLI.
    """
    try:
        from .executor import (  # noqa: PLC0415 - executor pulls in most of the package
            mask_protected_paths,
            mask_shadowed_by,
        )
    except Exception:  # pragma: no cover - defensive; executor is always importable
        logger.debug("task control dir: the sandbox builder could not be consulted")
        return None

    db_path = getattr(config, "db_path", None)
    if not db_path:
        return None
    try:
        if not Path(db_path).is_absolute():
            return None
    except _PATH_ERRORS:
        return None

    try:
        protected = mask_protected_paths(config)
        candidates: list[Path] = [Path(db_path).parent]
        try:
            candidates.append(config.module_db_root())
        except Exception:  # noqa: BLE001 - a misconfigured module_data_dir raises
            pass

        swallowing: list[Path] = []
        seen: list[Path] = []
        for candidate in candidates:
            # Both names, exactly as `_mask_dir` emits them: under a symlinked
            # deployment root a mask lands at one and not the other.
            for name in (candidate, candidate.resolve()):
                if name in seen:
                    continue
                seen.append(name)
                if mask_shadowed_by(name, protected):
                    continue  # the builder refuses this one, so it masks nothing
                if _overlaps(root, name):
                    swallowing.append(name)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
        # Not `OSError` alone: `mask_protected_paths` resolves paths from a
        # config file, where a null byte raises `ValueError`.
        return (
            f"{_CONTROL_MASK_UNKNOWN} — the mask could not be evaluated ({exc})",
            _CONTROL_MASK_REMEDY,
        )

    if not swallowing:
        return None

    available, why = _sandbox_mask_availability(config, probe)
    if available is True:
        prefix = _CONTROL_MASKED
    elif available is False:
        prefix = _CONTROL_WOULD_BE_MASKED
    else:
        prefix = _CONTROL_MASK_UNKNOWN
    where = "inside" if root != swallowing[0] and root.is_relative_to(swallowing[0]) else "at or over"
    finding = (
        f"{prefix} — {root} is {where} {swallowing[0]}, which the sandbox masks "
        f"with an empty read-only tmpfs as its last mount operation"
    )
    if why:
        finding = f"{finding}, and {why}"
    return finding, _CONTROL_MASK_REMEDY


def check_task_control_dir(config: "Config", probe: bool) -> CheckResult:
    """The daemon-owned tree holding each task's framework-authored files.

    ``{temp_dir}/.control/{user_id}/task_{id}`` holds both halves of the
    assembled prompt, the briefing metadata and the prepared image attachments.
    The model must be able to read its own — the prompt names the attachment
    paths — and must not be able to write any of them or read another task's.
    Two mechanisms enforce that (one ``--ro-bind`` of the task's own directory,
    and the directory in both ``native_fs_roots`` lists), and both of them
    depend on a layout property that lives in the rendered config rather than
    in the code: the tree is a sibling of the per-user workspaces, so nothing
    model-writable is above it.

    **Two independent questions, both answered on every run**, following
    ``runtime.session_log_dir``. Is the tree itself made of directories the
    daemon owns at 0700, and is anything the model can reach at or above it —
    including the database mask, which reaches it from the other direction by
    taking it away. Neither pre-empts the other: an operator who reads one
    reason and fixes it would otherwise be told nothing about the second and
    would still have the tree exposed.

    ``SKIP`` with no configured users, because nothing names a control
    directory until a task runs for somebody and a check with no subject must
    not report a property holding.

    ``probe=False`` spawns nothing, and on every layout that ships neither does
    ``probe=True`` — see `_control_mask_finding`.

    **The one FAIL is a fact about the filesystem, never about a uid.** A
    ``user_id`` with no resolvable control directory, and a level that is a
    symlink or not a directory, read the same from any process; a uid
    comparison reads differently depending on who ran the check, and doctor is
    also run by hand. See `_control_level_findings`.

    Never raises. It runs on the daemon's start-up path, and it deliberately
    reports rather than repairs: ``ensure_task_control_dir`` is what creates and
    re-asserts the tree, and a diagnostic that quietly chmod'd a directory
    would make the next run's answer a fact about this one.
    """
    name = "runtime.task_control_dir"

    # Normalised once rather than at each use: `config.users` reaches here
    # from a TOML file and a database overlay, and every later reader would
    # otherwise need its own guard against a value that is not a mapping.
    users_map = getattr(config, "users", None)
    try:
        users = sorted(users_map or {})
    except TypeError:
        users = []
    if not users:
        return CheckResult(
            name, SKIP,
            "no users are configured, so nothing names a task control directory",
        )

    from .executor import CONTROL_DIR_NAME  # noqa: PLC0415 - executor pulls in most of the package

    temp_dir = getattr(config, "temp_dir", None)
    if not temp_dir:
        return CheckResult(
            name, FAIL, "no temp_dir is configured, so no control tree can exist",
            remedy="Set temp_dir to an absolute path the daemon owns.",
        )
    try:
        absolute = Path(temp_dir).is_absolute()
    except _PATH_ERRORS as exc:
        return CheckResult(
            name, FAIL, f"the control tree under {temp_dir!r} cannot be named: {exc}",
            remedy="Set temp_dir to an absolute path the daemon can resolve.",
        )
    if not absolute:
        # Out of scope rather than guessed at, the same way
        # `config._warn_ro_paths_over_control_tree` skips it: `resolve()` would
        # answer against the calling process's cwd, which differs between the
        # daemon, the web app and a skill CLI, so the same config would report
        # one thing here and another there.
        return CheckResult(
            name, SKIP,
            f"temp_dir ({temp_dir}) is relative, so the control tree resolves "
            f"differently in each process that loads this config",
        )
    try:
        root = Path(temp_dir).resolve() / CONTROL_DIR_NAME
    except _PATH_ERRORS as exc:
        return CheckResult(
            name, FAIL, f"the control tree under {temp_dir} cannot be named: {exc}",
            remedy="Set temp_dir to an absolute path the daemon can resolve.",
        )

    findings: list[str] = []
    remedies: list[str] = []
    status = OK

    def _note(triple: tuple[str, str, str]) -> None:
        nonlocal status
        severity, finding, remedy = triple
        status = _severer(status, severity)
        findings.append(finding)
        if remedy and remedy not in remedies:
            remedies.append(remedy)

    for triple in _control_level_findings(root, "the control tree"):
        _note(triple)

    # Resolution, per user rather than for a representative one: the refusals
    # are per-user-id (an id that is not a plain component, or one equal to
    # `CONTROL_DIR_NAME`), so one user can be unable to run a task while every
    # other user is fine — and the id is the thing an operator would change.
    from .executor import get_task_control_dir  # noqa: PLC0415

    unresolvable: list[str] = []
    for user_id in users:
        try:
            resolved = get_task_control_dir(config, user_id, 0)
        except Exception:  # noqa: BLE001 - a diagnostic must not raise
            resolved = None
        if resolved is None:
            unresolvable.append(user_id)
            continue
        # The per-user level, which `_ensure_control_level` asserts over
        # exactly as it does the root. A resolvable path says nothing about
        # who owns the directory at it.
        for triple in _control_level_findings(
            resolved.parent, f"the control directory of user {user_id!r}",
        ):
            _note(triple)
    if unresolvable:
        _note((
            FAIL,
            "no control directory can be named for "
            + ", ".join(repr(u) for u in unresolvable)
            + ", so every task of those users fails at start-up",
            "Rename the user, or clear whatever is standing at "
            f"{root}; the id must be a plain path component and must not be "
            f"{CONTROL_DIR_NAME!r}.",
        ))

    for triple in _control_overlap_findings(config, users, root):
        _note(triple)

    mask = _control_mask_finding(config, root, probe)
    if mask:
        _note((WARN, mask[0], mask[1]))

    plural = "" if len(users) == 1 else "s"
    try:
        st = os.lstat(root)
    except OSError:
        shape = "not created yet"
    else:
        # Only a directory's mode is worth printing. A symlink's is 0777 by
        # convention and would sit in the observed line contradicting the
        # finding beside it that says the root is a symlink.
        shape = (
            f"mode {stat.S_IMODE(st.st_mode):04o}"
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
            else "not a directory"
        )
    observed = f"{root}: {shape}, {len(users)} configured user{plural}"

    if not findings:
        return CheckResult(name, OK, observed)
    return CheckResult(
        name,
        status,
        f"{observed}; " + "; ".join(findings),
        remedy=" ".join(remedies),
    )


# The two remedies this check can offer. Both are fixed literals: `detail` and
# `remedy` are built from these plus a percentage, a duration, a resolver branch
# name and the configured fallback brain kind — never from the credential, the
# raw response body or an exception string. The fallback kind is safe to
# interpolate because `_validate_brain_fallback` blanks anything outside
# `KNOWN_BRAIN_KINDS` at load, and it is a setting rather than a secret.
#
# There used to be three. The other two answered a failure to obtain a reading —
# check your egress, re-run `claude setup-token`, the response shape changed —
# and both went with the WARNs they accompanied. A remedy belongs on a row an
# operator can act on, and "the endpoint will not serve this credential class"
# is not one: those rows are SKIPs now, carrying the reason and no instruction.
_USAGE_BUSY_REMEDY = (
    "Tasks will fail over to the {fallback} brain when this window is exhausted."
)

# The same row on a deployment with no `[brain] fallback` configured. The literal
# above asserted a failover that most deployments do not have: `claude_code` has
# never had an implicit fallback, and since ISSUE-362 neither has any other kind,
# so an exhausted window fails the task outright. Naming a repair the operator
# can act on beats promising a reroute that will not happen.
_USAGE_BUSY_NO_FALLBACK_REMEDY = (
    "No [brain] fallback is configured, so tasks fail when this window is "
    "exhausted. Set one to reroute them."
)


def check_subscription_usage(config: "Config", probe: bool) -> CheckResult:
    """Plan utilization for the Claude Code subscription, with its reset times.

    On a subscription deployment the dashboard's cost column is deliberately
    blank — a plan-equivalent list price is not spend — so these windows are the
    only budget there is, and the deployment currently learns it is out of plan
    headroom at the moment a task fails over.

    **This check never returns FAIL, at any utilization.** ``exit_code`` returns
    1 on any FAIL and ``scheduler._alert_doctor_failures`` messages every admin
    on the transition into failure. A plan at 97% is a fact about the plan, not a
    defect in the host: it would exit non-zero on a busy but perfectly healthy
    deployment, turn the Health pane red for a condition no operator action
    resolves, and mail everyone about it. Proactive alerting on utilization is a
    reasonable thing to want and belongs in a poller with its own per-window
    threshold state, not smuggled through doctor's failure channel.

    ``subscription_usage`` is imported inside the function, not at module scope:
    ``_validate_forge_clis`` imports this module from inside every
    ``load_config``, which runs in every CLI invocation and every host-side skill
    CLI the proxy spawns per call.
    """
    name = "runtime.subscription_usage"
    settings = getattr(getattr(config, "brain", None), "claude_code", None)

    if not getattr(settings, "subscription_usage", True):
        return CheckResult(name, SKIP, "subscription usage polling is disabled")
    if not probe:
        # Before the import, and before anything is asked of the module: the
        # reading is a network request and there is no cheap filesystem answer
        # that would still be true.
        return CheckResult(
            name,
            SKIP,
            "utilization cannot be observed without a network request (probe disabled)",
        )

    from . import subscription_usage

    # One clock for the fetch, the countdowns and the staleness age. Reading the
    # wall clock twice would let a cached snapshot's age be measured against a
    # different moment than the one the module used to decide it was fresh.
    now = time.time()
    snapshot = subscription_usage.get_snapshot(config, now_ts=now)

    # Two of the module's errors are conditions rather than faults, and a WARN
    # about network egress would be nonsense for either. `DISABLED_ERROR` is
    # unreachable through the gate above and handled anyway: the module reads
    # the same setting defensively and its own answer is the authoritative one.
    if snapshot.error in (
        subscription_usage.NO_CREDENTIAL_ERROR,
        subscription_usage.DISABLED_ERROR,
    ):
        return CheckResult(name, SKIP, snapshot.error)

    if snapshot.error and not snapshot.has_data:
        # SKIP, not WARN. This used to warn, on the reading that a reading the
        # operator expected and did not get is a problem worth surfacing. On the
        # deployment shapes that actually run, it is not: the endpoint does not
        # serve the long-lived setup-token credential Ansible and Docker deploy,
        # answering it with a persistent 429, so the WARN was permanent, matched
        # no operator action, and coloured the Health pane for a host with
        # nothing wrong with it. A reading that cannot be obtained is a check
        # that does not apply here, which is what SKIP means. The reason is
        # still carried, so anyone asking why the card is absent can read it.
        return CheckResult(name, SKIP, _usage_error(snapshot))

    if not snapshot.windows:
        # Unreachable: every error-free return from `get_snapshot` carries
        # windows, and the one that does not sets NO_WINDOWS_ERROR. Guarded
        # anyway, because the alternative is an IndexError below, and
        # `run_checks` turns a raising check into exactly the FAIL this check
        # exists never to produce. Two lines make the promise structural rather
        # than inherited from another module's invariant.
        return CheckResult(name, SKIP, subscription_usage.NO_WINDOWS_ERROR)

    # A snapshot with both windows and an error is the stale-cache branch: real
    # numbers from an older fetch, plus the failure that made them old.
    stale_note = ""
    if snapshot.error:
        age = snapshot.age_seconds(now)
        stale_note = (
            f"last successful reading is {_duration(age)} old: {_usage_error(snapshot)}"
        )
        stale_after = _setting_float(
            settings, "subscription_usage_stale_after_seconds", 3600.0
        )
        if age > stale_after:
            # Same reasoning as the no-data branch above: a reading this old
            # means the fetches are failing, which on a server shape is the
            # steady state rather than a fault. The numbers are too old to
            # report as current, so there is nothing to check.
            return CheckResult(name, SKIP, stale_note)

    # Worst first, and all of them: "5-hour at 12%, weekly at 94%" and "5-hour at
    # 94%, weekly at 12%" call for different operator responses, and this one
    # line is the whole of what a terminal reader sees.
    windows = sorted(snapshot.windows, key=lambda w: w.percent, reverse=True)
    detail = "; ".join(_usage_window(w) for w in windows)
    if stale_note:
        # Inside `stale_after` the status is still what the numbers say — that
        # threshold is the whole point of the setting — but the line has to
        # admit the numbers are old and say why. Otherwise an hour-long outage
        # reads as `OK` with an hour-old percentage beside a countdown that has
        # been recomputed against the current clock, which is the most
        # misleading pair this check could print. The admin card and `!usage`
        # both carry the same footer for the same reason.
        detail = f"{detail}; {stale_note}"

    warn_at = _setting_float(settings, "subscription_usage_warn_percent", 80.0)
    high_at = _setting_float(settings, "subscription_usage_high_percent", 95.0)
    # The status table's two busy rows differ only in which threshold caught the
    # reading; both are WARN with the same detail and the same remedy, because
    # `high` is what turns the dashboard tile red and doctor has no third colour.
    # `min` reproduces both rows including an inverted pair, which the loader
    # corrects but which a config reaching the dataclass some other way would not.
    if windows[0].percent >= min(warn_at, high_at):
        from .brain._fallback import effective_fallback_kind

        fallback_kind = effective_fallback_kind(config.brain)
        remedy = (
            _USAGE_BUSY_REMEDY.format(fallback=fallback_kind)
            if fallback_kind is not None
            else _USAGE_BUSY_NO_FALLBACK_REMEDY
        )
        return CheckResult(name, WARN, detail, remedy=remedy)
    return CheckResult(name, OK, detail)


def _usage_error(snapshot: "UsageSnapshot") -> str:
    """The module's error, plus which credential produced it.

    Which one it was is the whole diagnostic: a setup token in the environment
    and an interactive login in the keychain are refused for different reasons
    and have different repairs. The branch *name* only — the snapshot has never
    carried the credential itself.
    """
    if not snapshot.token_source:
        return snapshot.error
    return f"{snapshot.error} (credential source: {snapshot.token_source})"


def _usage_window(window: "UsageWindow") -> str:
    text = f"{window.label} at {window.percent:g}%"
    if window.resets_in_seconds is None:
        # No reset scheduled, or an unparseable one. A terminal line is better
        # short than padded with a clause that says nothing.
        return text
    if window.resets_in_seconds <= 0:
        return f"{text} (resetting now)"
    return f"{text} (resets in {_duration(window.resets_in_seconds)})"


def _duration(seconds: float) -> str:
    """A coarse human duration: ``6d 2h``, ``1h 04m``, ``12m``, ``45s``.

    Two units at most. An operator reading "resets in 1h 04m" is deciding
    whether to wait; seconds of precision six hours out is noise.
    """
    total = int(max(0.0, seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _setting_float(settings: object, field: str, default: float) -> float:
    """A numeric setting, or `default` for anything that is not a real number.

    The loader validates and corrects these fields; this is the second line, so
    that a value arriving past the loader cannot make the comparison below raise
    or silently never fire. ``bool`` is excluded explicitly — it is an ``int``,
    and ``True`` would read as a 1% threshold.
    """
    value = getattr(settings, field, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    as_float = float(value)
    return as_float if math.isfinite(as_float) else default


#: The last hour's task outcomes. Lifted verbatim off the two hand-rolled
#: health probes this check replaces, so the answer does not change with the
#: source of truth.
_RECENT_TASK_OUTCOMES_SQL = """
    SELECT SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END),
           SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END)
    FROM tasks
    WHERE created_at > datetime('now', '-1 hour')
"""


def check_task_failure_rate(config: "Config", probe: bool) -> CheckResult:
    """Are the last hour's tasks mostly failing?

    The query and the ``failed > 0 and failed >= completed`` predicate both come
    verbatim off ``heartbeat._check_self`` and ``commands.cmd_check``, which ran
    the same SQL and compared it the same way.

    ``WARN``, never ``FAIL``, on the rate itself: a failure rate is a symptom,
    and a check that failed the daemon's start-up report because one task failed
    an hour ago is noise on a path an operator has to be able to trust. ``FAIL``
    is for the database not answering the question at all — which is also the
    "does the schema exist" half of the old probe, and a gap
    :func:`check_framework_db` leaves open: ``PRAGMA quick_check`` never reads
    ``tasks``, so a database with no schema passes it and a missing table
    surfaces here.
    """
    import sqlite3

    name = "runtime.task_failure_rate"
    db_path = Path(config.db_path)
    if not db_path.exists():
        # `check_framework_db` already reports the absence and owns its remedy.
        return CheckResult(name, SKIP, f"{db_path} does not exist")
    try:
        # Read-only via the URI form; see `sqlite_util.connect_read_only`.
        conn = sqlite_util.connect_read_only(db_path)
    except sqlite3.Error as exc:
        return CheckResult(
            name,
            FAIL,
            f"{db_path} could not be opened: {exc}",
            remedy="Restore the database from a snapshot (`python -m istota.db_restore`).",
        )
    try:
        row = conn.execute(_RECENT_TASK_OUTCOMES_SQL).fetchone()
    except sqlite3.OperationalError as exc:
        # A missing or renamed table, which is the half `check_framework_db`
        # cannot see: `quick_check` never reads `tasks`.
        return CheckResult(
            name,
            FAIL,
            f"{db_path}: the tasks table could not be queried: {exc}",
            remedy="Run `istota init` to create or migrate the framework schema.",
        )
    except sqlite3.Error as exc:
        # Corruption, not a schema gap. `connect` under the URI form opens
        # lazily, so a file that is not a database — or a torn restore —
        # surfaces here rather than above, and `istota init` is the wrong
        # advice for it.
        return CheckResult(
            name,
            FAIL,
            f"{db_path} could not be read: {exc}",
            remedy="Restore the database from a snapshot (`python -m istota.db_restore`).",
        )
    finally:
        conn.close()

    # `SUM` over an empty window is NULL, not 0, and an idle deployment is the
    # common case rather than a corner. Both copies coalesce before comparing.
    completed = (row[0] if row else 0) or 0
    failed = (row[1] if row else 0) or 0
    detail = f"last hour: {completed} completed, {failed} failed"
    if failed > 0 and failed >= completed:
        return CheckResult(
            name,
            WARN,
            detail,
            remedy="Inspect the failures: `istota list --status failed`.",
        )
    return CheckResult(name, OK, detail)


#: What the live probe asks the model to echo back through its Bash tool. The
#: same marker both hand-rolled probes used.
_MODEL_PROBE_MARKER = "healthcheck-ok"


def _read_user_resources(config: "Config", user_id: str) -> list:
    """The user's resource rows, for the probe's sandbox plan. Never raises.

    Read-only through the URI form rather than through ``db.get_db``, which
    connects read-write and commits on exit: on a WAL database that
    materializes the ``-wal`` / ``-shm`` sidecars, and ``sudo istota doctor``
    against a stopped daemon would leave them owned by root. That is
    :func:`check_framework_db`'s rule, and a check reached from the same CLI
    does not get an exemption from it for being a port of daemon-side code.

    An empty list on any failure only narrows the mounts the probe's namespace
    gets; a check about the model must not fail on the resource table.
    """
    import sqlite3

    from . import db

    try:
        conn = sqlite_util.connect_read_only(config.db_path)
    except Exception:  # noqa: BLE001 - deliberate: doctor never raises
        return []
    try:
        conn.row_factory = sqlite3.Row
        return db.get_user_resources(conn, user_id)
    except Exception:  # noqa: BLE001 - deliberate: doctor never raises
        return []
    finally:
        conn.close()


def _probe_user(config: "Config") -> str:
    """The user a deployment-level probe runs as, or ``""`` for none.

    An admin who is also a configured user, else the first configured user,
    else a sole admin. Alphabetically first at each step rather than
    insertion-ordered, so the answer does not depend on how the config happened
    to be assembled.

    **An admin id is not necessarily a user.** ``admin_users`` is read from
    ``/etc/istota/admins`` by ``load_admin_users`` and has no relationship to
    ``config.users``, so an admin with no ``UserConfig`` behind it gets a
    namespace built around a workspace that does not exist — a failure about
    the deployment's user list wearing a model-failure label. That is why the
    intersection is preferred over either list, and why "several admins" does
    not fall straight through to an arbitrary user: it takes an admin from
    among the configured ones first, so the probe runs in the sandbox shape the
    operator asked about. An empty ``admin_users`` means *everyone* is admin
    (:meth:`Config.is_admin`) and so names nobody in particular, which is why it
    falls through rather than picking.

    Deliberately not :attr:`Config.local_user_id`, which puts ``users`` ahead of
    ``admin_users`` because it answers a different question — the sole user of
    the no-auth standalone shape, where there is one by construction.

    A narrowing from the two probes this replaces, which ran as the invoking
    user and as the heartbeat check's owner respectively. Deliberate, and not a
    knob to restore: a health probe answers a question about the deployment, and
    neither caller's behaviour depended on which user the echo ran as — both
    only read whether the marker came back.
    """
    admins = set(getattr(config, "admin_users", None) or ())
    users = sorted(getattr(config, "users", None) or ())
    configured_admins = [user_id for user_id in users if user_id in admins]
    if configured_admins:
        return configured_admins[0]
    if users:
        return users[0]
    sole_admin = sorted(admins)
    return sole_admin[0] if len(sole_admin) == 1 else ""


def check_model_execution(config: "Config", probe: bool) -> CheckResult:
    """The model answers: ``claude -p`` echoes a marker back through Bash.

    The one member of :data:`LIVE_CHECKS`. It reaches a model and therefore
    costs money, so no caller gets it without naming the axis. It honours
    ``probe`` as well: ``probe=False`` ``SKIP``s whatever ``live`` says, because
    the no-spawn rule at the top of this module is unconditional and this is the
    most expensive possible way to break it.

    Lifted off ``heartbeat._check_self`` and ``commands.cmd_check``, with two
    corrections. It gates the sandbox wrap on
    :func:`executor.effective_sandboxing` rather than on
    ``security.sandbox_enabled``: on the shipped Docker stack the flag is true
    and no namespace can be created, so the copies' spelling built a wrap that
    failed for a reason having nothing to do with the model. And it ``SKIP``s on
    a brain that runs no CLI, on :func:`check_model_cli`'s own predicate — both
    copies exec ``claude`` unconditionally, so on a ``native`` deployment (which
    is what ``docker/.env`` ships) they tested a binary nothing uses.

    One property of the FAIL detail is worth stating rather than assuming. It
    quotes a bounded excerpt of the CLI's own stderr, which is what makes the
    finding actionable and is what ``commands.cmd_check`` already printed. The
    redaction pass behind :func:`config_secrets` scrubs values it can find in
    the loaded ``Config``, and the credentials this particular call carries are
    not among them — ``CLAUDE_CODE_OAUTH_TOKEN`` comes from the daemon's own
    environment, as do the proxy and TLS names :func:`build_model_cli_env`
    passes through. So the excerpt is safe because the CLI does not print its
    own credential, not because anything here checks. If that stops being true,
    classify the failure instead of quoting it, the way
    ``security.devbox_netfilter`` does with ``iptables``.
    """
    name = "runtime.model_execution"
    if not probe:
        return CheckResult(
            name, SKIP, "probing is disabled; the model was not invoked"
        )
    kind = getattr(config.brain, "kind", "claude_code")
    if kind not in ("claude_code", "tmux_claude"):
        return CheckResult(
            name,
            SKIP,
            f"brain.kind = {kind!r} runs the agent loop in-process (native); no CLI to exercise",
        )
    user_id = _probe_user(config)
    if not user_id:
        return CheckResult(
            name, SKIP, "no user to run a probe as (no single admin, no configured user)"
        )

    from . import db
    from .executor import (
        SandboxProfile,
        build_bwrap_cmd,
        build_model_cli_env,
        effective_sandboxing,
    )

    cmd = [
        "claude",
        "-p",
        f"Run: echo {_MODEL_PROBE_MARKER}",
        "--allowedTools",
        "Bash",
        "--output-format",
        "text",
    ]
    try:
        env = build_model_cli_env(config)
        if effective_sandboxing(config):
            # Both probes this replaces created this directory. This one does
            # not: doctor is a diagnostic and its entry points include an
            # operator shell, so a `sudo istota doctor` would leave a
            # root-owned `{temp_dir}/{user_id}` that every later task for that
            # user then binds read-write and cannot write to — the same
            # ownership hazard `check_framework_db` opens read-only to avoid,
            # and worse, because it persists. A directory the daemon has not
            # made yet means no task has run as this user, which is a fact
            # worth reporting rather than papering over.
            user_temp = Path(config.temp_dir) / user_id
            if not user_temp.is_dir():
                return CheckResult(
                    name,
                    SKIP,
                    f"the sandbox wrap needs {user_temp}, which the daemon has not created yet",
                )
            fake_task = db.Task(
                id=0,
                status="running",
                source_type="cli",
                user_id=user_id,
                prompt="healthcheck",
                conversation_token="",
            )
            user_resources = _read_user_resources(config, user_id)
            # CLAUDE: the command being wrapped *is* the `claude` CLI, so it
            # needs its own binary, its credential and its state directory.
            cmd = build_bwrap_cmd(
                cmd,
                config,
                fake_task,
                config.is_admin(user_id),
                user_resources,
                user_temp,
                profile=SandboxProfile.CLAUDE,
            )
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=MODEL_PROBE_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name,
            FAIL,
            f"`claude -p` timed out after {MODEL_PROBE_TIMEOUT}s (as {user_id})",
            remedy="Check the model credential and network egress, then retry.",
        )
    except Exception as exc:  # noqa: BLE001 - deliberate: doctor never raises
        return CheckResult(
            name,
            FAIL,
            f"`claude -p` could not be run (as {user_id}): {type(exc).__name__}: {exc}",
            remedy="Check the model credential and network egress, then retry.",
        )

    if _MODEL_PROBE_MARKER in (result.stdout or ""):
        return CheckResult(
            name, OK, f"`claude -p` echoed {_MODEL_PROBE_MARKER!r} through Bash (as {user_id})"
        )
    observed = (
        _probe_output_line(result.stderr) or _probe_output_line(result.stdout) or "(no output)"
    )
    return CheckResult(
        name,
        FAIL,
        f"`claude -p` exited {result.returncode} without {_MODEL_PROBE_MARKER!r} "
        f"(as {user_id}): {observed}",
        remedy="Check the model credential and network egress, then retry.",
    )


def _probe_output_line(text: str | None, limit: int = 160) -> str:
    """One capped line, for a subprocess stream quoted into a ``detail``."""
    return " ".join((text or "").split())[:limit]


# ---------------------------------------------------------------------------
# security.*
# ---------------------------------------------------------------------------


#: Names that only an environment istota itself built for something other than
#: the daemon carries. Each is set by one of the two env builders and by
#: nothing else on a deployment, so finding one is evidence that ``os.environ``
#: here is not the daemon's.
#:
#: * ``ISTOTA_TASK_ID`` — `task_env.build_task_runtime` sets it on every task
#:   env, and it is not in ``_EXECUTOR_PROXY_ONLY_VARS``, so it survives into
#:   both the model's own environment and ``proxy_base_env``, which is what a
#:   host-side skill CLI runs with. The general marker.
#: * ``ISTOTA_SANDBOXED`` — narrower (it needs the sandbox in effect and the
#:   proxy on), and kept because its meaning is exactly "this environment was
#:   built for the model".
#: * ``PRECOMMIT_SCANS_REQUIRED`` — `executor.build_stripped_env` sets it, which
#:   is what a cron ``command`` job and a heartbeat ``shell-command`` run with;
#:   that builder filters every name matching the credential patterns, so all
#:   three names below are gone from it by construction. Unlike the other two an
#:   operator can also set this one by hand (ISSUE-291 made it an override), so
#:   it is evidence rather than proof. It errs toward a skip, which is the safe
#:   direction for a check whose observed failure mode is crying wolf.
#:
#: Named markers rather than the ``ISTOTA_`` namespace: the daemon's own
#: environment carries ``ISTOTA_CONFIG_PATH`` and ``ISTOTA_ADMINS_FILE``, so a
#: namespace test would skip everywhere and the check would never fail at all.
_NON_DAEMON_ENV_MARKERS = ("ISTOTA_TASK_ID", "ISTOTA_SANDBOXED",
                           "PRECOMMIT_SCANS_REQUIRED")


def _non_daemon_env_markers() -> list[str]:
    """Which of :data:`_NON_DAEMON_ENV_MARKERS` this process carries."""
    return [n for n in _NON_DAEMON_ENV_MARKERS if os.environ.get(n)]


def check_skill_model_credential(
    config: "Config", probe: bool
) -> list[CheckResult]:
    """Whether a skill CLI that calls a model can authenticate (ISSUE-409).

    The fact this states was unstated anywhere, which is why the failure it
    covers could only be found by running a review. `code_review` spawns
    `claude -p` per reviewer from inside a skill CLI, and the skill proxy
    strips the Claude credential out of the environment every host-side CLI
    runs with, so for a while every review on a subscription deployment came
    back `skipped / review_failed` while the daemon's own brain worked
    perfectly. Nothing in the deployment said so.

    Two results, because they fail independently and an operator fixes them
    differently.

    ``security.skill_model_credential.wiring`` is the drift guard, and it is
    the one that catches a *re*-regression. `SKILL_MODEL_CALLERS` is a list of
    skill *names*, matched against the loaded index at task-build time, so a
    renamed or removed skill directory silently stops the injection — there is
    no import to break and no test on a deployment to go red. A name that no
    longer resolves is reported here rather than discovered by a review.

    ``security.skill_model_credential.value`` asks whether the daemon holds
    anything to inject. Only the presence of a name is ever read or reported;
    a credential's value never reaches a `CheckResult`, which is rendered into
    the admin dashboard and the boot log.

    That half reads ``os.environ``, and the process it reads is not always the
    daemon. Four of the six entry points are, and a fifth is the web unit, but
    `istota doctor` is also a command a *task* can run, and a task environment
    is the one ISSUE-390 deliberately strips the Claude credential out of. The
    two API-key names reach a task env by no route at all, so on that shape all
    three names are absent whatever the daemon holds, and the first version of
    this check answered FAIL, and exited 1, about a deployment whose reviews
    were working. So absence is only a verdict where the environment could have
    held one: `_non_daemon_env_markers` names the environments istota built for
    something other than the daemon, and the answer there is a skip.

    Presence stays OK wherever it is read, which is the asymmetry that makes
    the skip narrow rather than a hole. `build_clean_env` copies the token out
    of the daemon's own environment into every task's, so seeing it inside a
    task does answer the question; only absence is the unanswerable direction.

    Spawns nothing, so it is safe under ``probe=False``, and never raises.
    """
    prefix = "security.skill_model_credential"
    results: list[CheckResult] = []

    try:
        from .executor import (  # noqa: PLC0415
            SKILL_MODEL_CALLERS,
            SKILL_MODEL_CREDENTIAL_VARS,
        )
        from .skills._loader import (  # noqa: PLC0415
            effective_disabled_skills,
            load_skill_index,
        )
        index = load_skill_index(
            config.skills_dir, bundled_dir=config.bundled_skills_dir
        )
    except Exception as exc:  # noqa: BLE001 - a check never raises
        return [
            CheckResult(
                f"{prefix}.wiring",
                SKIP,
                f"the skill index could not be loaded: {exc}",
            )
        ]

    # The operator's own disabled set, not a user's: this is a deployment-scope
    # statement, and `effective_disabled_skills` takes a user id because a user
    # may disable a skill for themselves. "" is the deployment's own view —
    # `config.disabled_skills` plus the capability gate — so a skill one user
    # turned off is still reported as wired, which is what it is.
    try:
        disabled = effective_disabled_skills(config, "", index)
    except Exception:  # noqa: BLE001 - a check never raises
        disabled = set()

    missing = sorted(n for n in SKILL_MODEL_CALLERS if n not in index)
    turned_off = sorted(n for n in SKILL_MODEL_CALLERS if n in disabled)
    live = sorted(
        n for n in SKILL_MODEL_CALLERS if n in index and n not in disabled
    )

    if missing:
        results.append(
            CheckResult(
                f"{prefix}.wiring",
                FAIL,
                f"SKILL_MODEL_CALLERS names {', '.join(missing)}, which the "
                f"skill index does not have — the proxy injects no model "
                f"credential for a name that does not resolve, so that skill's "
                f"model calls fail unauthenticated",
                remedy=(
                    "A skill was renamed or removed without updating "
                    "SKILL_MODEL_CALLERS in executor.py."
                ),
            )
        )
    elif not live:
        results.append(
            CheckResult(
                f"{prefix}.wiring",
                SKIP,
                f"every model-calling skill is disabled on this deployment "
                f"({', '.join(turned_off) or 'none configured'})",
            )
        )
    else:
        results.append(
            CheckResult(
                f"{prefix}.wiring",
                OK,
                f"the proxy injects a model credential for {', '.join(live)}",
            )
        )

    if not live:
        return results

    present = sorted(n for n in SKILL_MODEL_CREDENTIAL_VARS if os.environ.get(n))
    if present:
        results.append(
            CheckResult(
                f"{prefix}.value",
                OK,
                f"the daemon holds {', '.join(present)} to inject",
            )
        )
    elif not getattr(config.security, "skill_proxy_enabled", True):
        # With the proxy off there is no injection and no strip: the CLI is
        # re-exec'd with the daemon's own inherited environment, so whatever
        # authenticates the daemon authenticates it. Nothing to assert here,
        # and asserting anyway would report a deployment shape as broken for a
        # boundary it does not have.
        results.append(
            CheckResult(
                f"{prefix}.value",
                SKIP,
                "[security] skill_proxy_enabled = false, so a skill CLI "
                "inherits the daemon's environment directly",
            )
        )
    elif markers := _non_daemon_env_markers():
        # Absence in an environment that was never going to hold one is not a
        # verdict about the daemon. Ordered after the OK arm on purpose:
        # presence answers the question wherever it is read, so only absence is
        # unanswerable here. See the docstring.
        results.append(
            CheckResult(
                f"{prefix}.value",
                SKIP,
                f"this process carries {', '.join(markers)}, so its "
                f"environment is not the daemon's and the credential it would "
                f"inject is not visible from here",
                remedy=(
                    "Run the check as the daemon's user with the daemon's "
                    "environment loaded (the `<namespace>-run doctor` wrapper "
                    "on a server install), or read it from the admin "
                    "dashboard's Health pane."
                ),
            )
        )
    else:
        results.append(
            CheckResult(
                f"{prefix}.value",
                FAIL,
                "the daemon holds none of "
                f"{', '.join(sorted(SKILL_MODEL_CREDENTIAL_VARS))}, so "
                f"{', '.join(live)} will fail unauthenticated",
                remedy=(
                    "Set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in the "
                    "daemon's environment."
                ),
            )
        )
    return results


def check_secret_key(config: "Config", probe: bool) -> CheckResult:
    """Whether the encrypted secrets store has a master key to work with.

    ``secrets_store`` derives one Fernet key from ``$ISTOTA_SECRET_KEY`` and
    raises on every encrypt and decrypt without it, so a deployment missing it
    can store no credential and read none back: `istota secret`, the per-user
    ntfy service, Garmin, Monarch and the Google Workspace tokens all fail.

    Nothing said so. The failure is silent in the direction that reads as
    normal — ``_native_key_holders`` calls ``secret_key_available()`` and
    reports *0 holders* rather than an error, so "credentials cannot work
    here" rendered as "nobody has configured a credential". The standalone
    wizard generated no key at all for seven weeks and no surface anywhere
    reported it.

    The floor and the presence test are the secrets store's own. A second copy
    of ``_MIN_KEY_LEN`` here is precisely the drift this check exists to catch,
    so it is imported rather than restated.

    Ordering follows ``check_skill_model_credential``'s rule, and for the same
    reason: presence answers the question wherever it is read, so the OK arm
    and the too-short arm both come *before* the non-daemon skip and only
    absence is the unanswerable direction. Absence inside a task is guaranteed
    rather than informative — ``build_clean_env`` strips this name from every
    task env by design and ``_PROXY_LOOKUP_BLOCKED`` stops the proxy handing it
    back — so reading it there as the daemon's answer would fail a working
    deployment.

    **There is a fourth arm between those two, and it is the one the marker
    list cannot reach.** On standalone the key lives in ``istota.env``, and
    ``cmd_serve`` is the only thing in the tree that sources it — so ``istota
    doctor`` in an operator's shell is a process that carries none of the three
    markers *and* has never read the file, and taking its own environment as
    the answer meant a FAIL and an exit 1 about an install whose key was
    exactly where it belonged, with a remedy telling the operator to add a line
    already in the file. So an absent variable falls back to reading the
    sibling env file the daemon does source, and only a key missing from both
    is absence. The length is all that is ever read out of that file.

    **And absence itself is two verdicts rather than one**, because empty is a
    posture on one shape and the defect on the others. ``istota_secret_key``
    defaults to ``""``, ``secrets.env.j2`` renders the line only when it is
    non-empty, and the defaults file documents empty as *disabling* the store —
    so a hand-written Ansible inventory can be deliberately keyless, and a FAIL
    there pages an operator at every boot, every scheduler interval, ``!check``
    and the self-check heartbeat for a decision they made. ``verdict`` already
    draws that line: a warning that pages someone is a failure wearing the
    wrong label. So the ``secrets`` table settles it — rows present is the
    original defect and stays FAIL, an observed empty table is WARN, and a
    table that could not be read stays FAIL because it has established nothing.
    Only the count is read; no row is decrypted and no ciphertext is selected.

    Spawns nothing, so it is safe under ``probe=False``. Never reports the
    value or any prefix of it: a ``CheckResult`` is rendered into the boot log
    and the admin dashboard.
    """
    from . import secrets_store  # noqa: PLC0415

    name = "security.secret_key"
    var = "ISTOTA_SECRET_KEY"
    floor = secrets_store._MIN_KEY_LEN
    raw = os.environ.get(var, "").strip()

    if secrets_store.secret_key_available():
        return CheckResult(
            name, OK, f"{var} is set and meets the length floor",
        )

    if raw:
        return CheckResult(
            name,
            FAIL,
            f"{var} is {len(raw)} characters; the floor is {floor}, so every "
            f"encrypt and decrypt of a stored credential raises",
            remedy=_secret_key_remedy(config),
        )

    # Not in this process's environment — which on the standalone shape says
    # nothing, because only `cmd_serve` sources `istota.env` and `istota
    # doctor` is a separate process that does not. Reading absence as the
    # answer there reported FAIL, and exited 1, about an install whose key was
    # sitting in the file the daemon reads at start-up: the same mistake
    # `check_skill_model_credential` records making, in a shape its marker list
    # cannot see, since an operator's shell carries none of the three.
    env_file, file_key = _secret_key_from_env_file(config, var)
    if file_key:
        if len(file_key) >= floor:
            return CheckResult(
                name,
                OK,
                f"{var} is not in this process's environment, but a usable one "
                f"is set in {env_file}, which the daemon sources at start-up",
            )
        return CheckResult(
            name,
            FAIL,
            f"{var} in {env_file} is {len(file_key)} characters; the floor is "
            f"{floor}, so every encrypt and decrypt of a stored credential "
            f"raises",
            remedy=_secret_key_remedy(config),
        )

    if markers := _non_daemon_env_markers():
        return CheckResult(
            name,
            SKIP,
            f"this process carries {', '.join(markers)}, so its environment is "
            f"not the daemon's — {var} is stripped from a task env by design "
            f"and its absence here says nothing about the deployment",
            remedy=(
                "Run the check as the daemon's user with the daemon's "
                "environment loaded (the `<namespace>-run doctor` wrapper on a "
                "server install), or read it from the admin dashboard's Health "
                "pane."
            ),
        )

    # Absent, and what that costs is not one answer. `istota_secret_key`
    # defaults to `""` and `secrets.env.j2` renders the line only when it is
    # non-empty, with the defaults file documenting empty as *disabling* the
    # store — so a bare-metal deployment can legitimately run without one, and
    # paging that operator at every boot and every scheduler sweep is a warning
    # wearing a failure's label (`verdict`'s own docstring draws that line).
    # The same absence on a deployment that has stored something is the defect
    # this check was written for.
    #
    # The `secrets` table is the discriminator, and the split is deliberately
    # asymmetric: only an *observed* empty table softens the verdict. A table
    # that could not be read has not established that nothing is stored, so it
    # keeps the FAIL.
    stored = _stored_secret_count(config)
    if stored == 0:
        return CheckResult(
            name,
            WARN,
            f"{var} is not set and the secrets table is empty, so the "
            f"encrypted store is off — nothing is unreachable yet, but every "
            f"attempt to store a credential will raise",
            remedy=_secret_key_remedy(config),
        )
    counted = (
        f"{stored} stored credential{'s' if stored != 1 else ''} "
        if stored > 0
        else "any stored credential "
    )
    return CheckResult(
        name,
        FAIL,
        f"{var} is not set, so {counted}can be neither encrypted nor read "
        f"back — the secrets table is unreachable and a connected service "
        f"reports as unconfigured rather than as broken",
        remedy=_secret_key_remedy(config),
    )


def _stored_secret_count(config: "Config") -> int:
    """How many rows the ``secrets`` table holds, or ``-1`` if unreadable.

    Three values, not two, because the third is what keeps the softening above
    honest: ``0`` is an *observed* empty store, ``-1`` is a question this could
    not settle, and only the first is evidence that nothing is currently
    unreachable.

    Every row, not the rows of currently-configured users the way
    :func:`_native_key_holders` scopes its count. The questions differ. That
    one asks "does this user hold a native brain key", where a row left behind
    by a removed user would report a credential nobody has; this asks "is
    anything in here now undecryptable", and a stale row is exactly as
    undecryptable as a live one. Counting all of them also avoids the trap that
    scoping carries here: ``config.users`` can be empty on a shape whose rows
    are real, and an empty scope would then read as an empty store and soften
    the verdict on the deployment that most needs it.

    ``mode=ro`` like every other database-touching check here, for the reason
    :func:`_native_key_holders` states: a read-write open materializes the
    ``-wal``/``-shm`` sidecars and, against a missing file, creates a zero-byte
    database that later reads as corruption rather than as absence.

    Never decrypts and never selects ``encrypted_value``, so no ciphertext
    enters this process. Never raises — one caller is the daemon's boot
    sequence — and an unreadable table is ``-1`` rather than nought, which is
    the direction that reports a problem rather than hiding one.
    """
    conn = None
    try:
        db_path = Path(getattr(config, "db_path", "") or "")
        if not db_path.name or not db_path.exists():
            return -1
        conn = sqlite_util.connect_read_only(db_path)
        row = conn.execute("SELECT COUNT(*) FROM secrets").fetchone()
        return int(row[0]) if row else -1
    except Exception:  # noqa: BLE001 - a check never raises
        return -1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110 - nothing to do about it
                pass


def _secret_key_env_file(config: "Config") -> Path | None:
    """The secrets env file a standalone daemon sources, if one is derivable.

    Mirrors ``cli._default_env_file``: a sibling of the config that was
    actually loaded. Restated rather than imported because ``doctor`` sits
    below ``cli`` — ``cli`` imports it — and one dotted name is a cheaper
    duplicate than the cycle.
    """
    if not config.config_path:
        return None
    return Path(config.config_path).expanduser().parent / "istota.env"


def _secret_key_from_env_file(config: "Config", var: str) -> tuple[Path | None, str]:
    """Read ``var`` out of that file, returning the path and the raw value.

    Only the *length* of what comes back is ever reported. Never raises: the
    file is optional, may be unreadable by this process, and one caller is the
    daemon's boot sequence.
    """
    env_file = _secret_key_env_file(config)
    if env_file is None:
        return None, ""
    try:
        from .setup_wizard import _read_env_values  # noqa: PLC0415

        return env_file, _read_env_values(env_file).get(var, "").strip()
    except Exception:  # pragma: no cover - defensive; a check never raises
        return env_file, ""


def _secret_key_remedy(config: "Config") -> str:
    """Where the key belongs on the shape this config describes."""
    generate = (
        'generate one with `python3 -c "import secrets; '
        'print(secrets.token_hex(32))"`'
    )
    if config.is_standalone:
        # The real path, not the default one: `istota setup -c` puts the env
        # file beside whatever config it was given, and an operator told to
        # edit a file that does not exist has been sent the wrong way.
        env_file = _secret_key_env_file(config) or Path(
            "~/.config/istota/istota.env"
        )
        return (
            f"Add an ISTOTA_SECRET_KEY line to {env_file} beside the session "
            f"secret ({generate}); `istota setup` writes one for a fresh "
            f"install. Existing stored credentials, if any, were encrypted "
            f"under a key that is gone and will not come back."
        )
    return (
        f"Set ISTOTA_SECRET_KEY in the daemon's environment ({generate}). "
        f"Docker persists one to /data/.secret_key on first boot; Ansible "
        f"passes it as the `istota_secret_key` variable."
    )


def check_skill_proxy(config: "Config", probe: bool) -> list[CheckResult]:
    """The skill proxy's two independent facts.

    ``security.skill_proxy`` — the ``istota-skill`` entry point resolves on the
    daemon's own PATH. The proxy spawns it per call; an unresolvable one turns
    every skill CLI into a command-not-found inside somebody's task.

    ``security.skill_proxy.forge_posture`` — the wording is preserved from
    ``_validate_forge_clis`` because it is a security posture statement, not a
    bug report: with the proxy off the forge token sits in the environment the
    model's own shell inherits rather than being injected per call.
    """
    results: list[CheckResult] = []
    enabled = getattr(config.security, "skill_proxy_enabled", True)

    if not enabled:
        results.append(
            CheckResult(
                "security.skill_proxy",
                SKIP,
                "[security] skill_proxy_enabled = false",
            )
        )
    else:
        path = shutil.which("istota-skill")
        if path is None:
            results.append(
                CheckResult(
                    "security.skill_proxy",
                    FAIL,
                    "the skill proxy is enabled but `istota-skill` is not on the daemon's PATH",
                    remedy=(
                        "Install the package so its console scripts are on PATH "
                        "(the docker image sets ENV PATH for exactly this)."
                    ),
                )
            )
        else:
            results.append(
                CheckResult("security.skill_proxy", OK, f"istota-skill resolves to {path}")
            )

    dev, reason = _dev_gate(config)
    if dev is None:
        posture_reason = reason
    elif _forge_token_gate(dev):
        posture_reason = _forge_token_gate(dev)
    elif enabled:
        posture_reason = "the skill proxy is enabled; tokens are injected per call"
    else:
        posture_reason = ""

    if posture_reason:
        results.append(
            CheckResult("security.skill_proxy.forge_posture", SKIP, posture_reason)
        )
    else:
        results.append(
            CheckResult(
                "security.skill_proxy.forge_posture",
                WARN,
                (
                    "forge tokens are configured but [security] skill_proxy_enabled = false; "
                    "gh and glab will work — the policy grants them the ambient token — but "
                    "that token is readable by anything else the task runs, instead of being "
                    "injected per call"
                ),
                remedy="Enable the skill proxy to keep the token out of the task environment.",
            )
        )
    return results


def _sandbox_in_force_clause(config: "Config", probe: bool) -> str:
    """Whether the sandbox the operator asked for is actually in force.

    Appended to the credential finding rather than gating it, because the two
    facts are independent and only one of them is in doubt. The credentials are
    in the task environment on the strength of ``skill_proxy_enabled`` alone —
    ``_split_credential_env`` runs only inside the proxy branch of
    ``execute_task`` — so that half is established whatever bubblewrap does
    here. What the sandbox state changes is how much the exposure costs, which
    is a clause, not a condition.

    Three states, resolved by :func:`_deployment_sandboxing`: in force, not in
    force, and not established on this run. The third is why the answer is
    not a bool — a check whose subject is a boundary must not report "fine"
    where it did not look, and must not assert an exposure it did not observe.

    Never raises: every branch returns a clause, so the caller appends
    unconditionally.
    """
    effective, why = _deployment_sandboxing(config, probe)
    if effective is None:
        return (
            f"{why}, so whether the sandbox is actually in force is "
            "unestablished"
        )
    if effective:
        return (
            "the sandbox is in force, so they sit inside the boundary that was "
            "switched on to keep them out"
        )
    return (
        "bubblewrap does not work here (see runtime.bwrap), so the task is "
        "unconfined as well and every credential on the host is reachable"
    )


def check_sandbox_credentials(config: "Config", probe: bool) -> CheckResult:
    """What ``skill_proxy_enabled = false`` costs when the sandbox is on.

    ``_split_credential_env`` is called only inside the proxy branch of
    ``execute_task``, so with the proxy off every configured service credential
    — the Nextcloud password, the mail passwords, the forge tokens — stays in
    the environment handed to the model. ``load_config`` warns on exactly this
    pairing (ISSUE-393), but the boot log is read once at install; the Health
    pane and ``istota doctor`` are the surfaces an operator returns to, and
    before this check they said nothing (ISSUE-396).

    **A registry entry of its own rather than a third result inside
    ``check_skill_proxy``**, for two reasons that both come from that check
    being in ``config.CONFIG_LOAD_CHECKS``. It runs inside every
    ``load_config`` — the daemon, the web app, every CLI invocation, every
    host-side skill CLI the proxy spawns per call — which forbids reaching
    ``istota.executor`` for ``effective_sandboxing``
    (``TestConfigLoadPathStaysCheap`` asserts that import never happens). And
    ``_validate_forge_clis`` logs every WARN those checks return, which would
    print this finding beside the ISSUE-393 warning already on that path,
    twice per load.

    **Gated on the pairing, not on the proxy alone.** Both switches off together
    is the single-user install's deliberate trust decision — ``setup_wizard``
    writes the pair, the task runs unconfined as the daemon user, and removing a
    variable from an environment it can read out of ``/proc`` anyway is
    decorative. That shape stays silent here as it does in the warning.

    ``DEPLOYMENT`` scope, which is also what keeps the image tier out of it: a
    posture an operator chose must not turn a tier red, and ``--scope image``
    never selects this.

    **It overlaps ``security.skill_proxy.forge_posture`` and both are kept.**
    A deployment with the developer skill, a forge token and this pairing gets
    two WARNs for one setting. They answer different questions: that one fires
    with the sandbox off as well, and names a specific token an operator can go
    and rotate; this one is about the whole credential set and only where there
    is a boundary for it to sit inside. Narrowing the forge check to the
    sandbox-off case would remove the rotate-this-token line from the
    deployment most likely to need it, which is a change to a security warning's
    coverage rather than part of adding a missing one.

    **It does not gate on a credential actually being configured**, unlike
    ``forge_posture``'s ``_forge_token_gate``. The subject is the posture, not
    an inventory: a credential added to a running deployment lands in the task
    environment with nothing new to warn about, so a check that went quiet on
    an empty set would be silent for exactly as long as it took to become
    wrong. The sentence stays conditional — "every *configured* credential" —
    so it asserts nothing about how many there are.
    """
    sandbox = getattr(config.security, "sandbox_enabled", False)
    proxy = getattr(config.security, "skill_proxy_enabled", True)

    if not sandbox:
        return CheckResult(
            "security.sandbox_credentials",
            SKIP,
            (
                "[security] sandbox_enabled = false; the task runs unconfined "
                "by design, so there is no boundary for a credential to sit "
                "inside"
            ),
        )
    if proxy:
        return CheckResult(
            "security.sandbox_credentials",
            OK,
            (
                "the skill proxy is enabled; service credentials are injected "
                "per call and are not in the task environment"
            ),
        )

    # Deliberately *not* the load_config warning's opening phrase. That message
    # begins "[security] sandbox_enabled with skill_proxy_enabled = false", and
    # `tests/test_config.py::TestTheSandboxWithoutTheProxyWarning` filters
    # `caplog` on exactly that substring. A byte-identical prefix here would not
    # fail anything today — this check is off the config-load path — but the
    # moment it were moved onto it those filters would match two records and
    # weaken to `any(...)` over both rather than turning red. The settings are
    # named in the other order, so both remain greppable and neither collides.
    detail = (
        "[security] skill_proxy_enabled = false with sandbox_enabled = true: "
        "every configured service credential stays in the task environment, "
        "readable by the model"
    )
    detail = f"{detail} — {_sandbox_in_force_clause(config, probe)}"
    return CheckResult(
        "security.sandbox_credentials",
        WARN,
        detail,
        remedy=(
            "Enable the skill proxy ([security] skill_proxy_enabled = true) so "
            "credentials are injected per call, or turn the sandbox off if this "
            "is a trusted single-user install."
        ),
    )


# The one repair line for a deployment that asked for a sandbox and did not get
# one. It names both hosts because an operator meets this on either and the fix
# differs: a container needs the two `security_opt` settings the shipped
# `docker/docker-compose.yml` does not grant, a bare-metal host needs
# unprivileged user namespaces. A remedy naming one sends half its readers to
# the wrong file.
_SANDBOX_EFFECTIVE_REMEDY = (
    "On Docker, grant the istota service both seccomp:unconfined and "
    "systempaths=unconfined (security_opt) — bubblewrap needs the first to "
    "create the namespace and the second to mount a procfs inside it. On a "
    "host, allow unprivileged user namespaces "
    "(sysctl kernel.unprivileged_userns_clone=1) and install a working "
    "bubblewrap. Or set [security] sandbox_enabled = false to say the "
    "deployment is deliberately unconfined."
)


def check_sandbox_effective(config: "Config", probe: bool) -> CheckResult:
    """Whether the sandbox the operator asked for is actually in force.

    ``runtime.bwrap`` answers whether bubblewrap is installed and runnable, and
    that is all it answers. On the shipped Docker stack the binary is present
    and runs, ``sandbox_enabled`` reads true, and every task still runs with the
    daemon's own filesystem access, because ``docker-compose.yml`` grants
    neither ``seccomp:unconfined`` nor ``systempaths=unconfined`` and the
    namespace probe fails (ISSUE-381). Both hand-rolled health probes report
    "Sandbox (bwrap): PASS" on exactly that deployment, and so did every doctor
    surface before this check.

    **A check of its own rather than a capability arm on ``check_bwrap``, and
    the reason is scope.** "Is bubblewrap installed and runnable" is a property
    of the *image*; "can this host create a namespace" is a property of the
    *deployment* — the container's ``security_opt``, the host's sysctl. Folding
    the second into the first would fail every correct image:
    ``tests/image/test_istota_image.py::TestGroupATheDoctorUmbrella::test_no_check_fails``
    runs ``istota doctor --json --scope image`` inside a bare ``docker run``
    with no ``security_opt``, ``cmd_doctor`` passes ``probe=True``
    unconditionally, and ``render-config.sh`` defaults ``sandbox_enabled``
    true. ``DEPLOYMENT`` scope is filtered out before invocation, so the image
    tier never reaches this, and an operator running the whole registry gets
    both lines with the remedy on the one that has a repair.

    **Three states, not two**, resolved by :func:`_deployment_sandboxing` and
    shared with ``_sandbox_mask_availability`` and ``_sandbox_in_force_clause``.
    Under ``probe=False`` the answer would cost a spawn, so it comes from
    ``effective_sandboxing_if_known`` — warm in the daemon, which probes at
    start-up — and a genuinely cold memo is reported as unestablished rather
    than as either answer. A boundary check must not pass on a question it could
    not settle, and must not assert an exposure it did not observe.

    **Run from inside a task's own sandbox it settles nothing, and says so.**
    The probe fails in there on the nesting depth rather than on any capability
    the deployment lacks, so this reported every task unsandboxed from inside a
    confined one and exited 1 about a working boundary. That arm is a ``SKIP``,
    not the ``WARN`` the other two unestablished causes get: probing again is
    what settles those, and nothing settles this one from here. See
    :func:`_deployment_sandboxing` for why the marker rather than the probe's
    stderr answers it, and for the two consequences of that choice.

    Not in ``DEEP_CHECKS``: ``effective_sandboxing`` memoizes its probe and the
    daemon has already paid for it during prompt assembly, so inside the daemon
    this costs nothing. Its consumers today are the four that run the registry
    — ``cli.cmd_doctor``, the scheduler's start-up report and hourly sweep, and
    the admin Health pane. Being free is also what will let a surface on a
    per-user cadence afford it, where ``sandbox.masks`` — which builds a real
    namespace — could not.

    **On the shipped Docker stack the FAIL is permanent rather than drift**, and
    that is accepted rather than overlooked. ``scheduler.run_startup_checks``
    alerts on every failure unconditionally, where the hourly sweep alerts only
    on a transition, so that deployment gets one alert per daemon start until it
    grants the two settings or turns the sandbox off. Reporting a real
    unconfined deployment quietly would be the ISSUE-381 defect with a flag on
    it; the CHANGELOG says so where an operator meets it.

    ``SKIP`` when ``sandbox_enabled`` is false: the operator turned it off and
    knows, and ``check_bwrap`` skips on the same setting.
    """
    name = "security.sandbox_effective"
    if not getattr(config.security, "sandbox_enabled", False):
        return CheckResult(
            name,
            SKIP,
            (
                "[security] sandbox_enabled = false; the deployment is "
                "deliberately unconfined, so there is nothing to be in force"
            ),
        )

    effective, why = _deployment_sandboxing(config, probe)
    if effective is None:
        if _sandbox_probe_is_nested():
            # SKIP rather than WARN: this process can never settle the question,
            # so there is nothing for the reader to act on and a warning on every
            # doctor run a task makes is noise. The two remaining unestablished
            # causes are settled by probing, so they stay a WARN with the line
            # that says so.
            return CheckResult(
                name,
                SKIP,
                f"{why}, so whether tasks are confined cannot be answered here",
                remedy=(
                    "Run `istota doctor` on the host as the daemon user; a "
                    "probe from inside a task's own sandbox cannot answer it."
                ),
            )
        return CheckResult(
            name,
            WARN,
            (
                f"[security] sandbox_enabled is set but {why}, so whether "
                "tasks are confined is unknown"
            ),
            remedy=(
                "Run `istota doctor`, which probes, to settle it. "
                + _SANDBOX_EFFECTIVE_REMEDY
            ),
        )
    if not effective:
        return CheckResult(
            name,
            FAIL,
            (
                "[security] sandbox_enabled is set but a bubblewrap namespace "
                "could not be created here, so every task runs unsandboxed "
                "with the daemon's own filesystem access"
            ),
            remedy=_SANDBOX_EFFECTIVE_REMEDY,
        )
    # Hedged to what was actually observed, like the two branches above it.
    # `effective_sandboxing` is the flag and a process-memoized capability
    # probe, which may have run some time ago; "tasks run sandboxed" would be
    # asserting a live property from a cached one, and this is the line an
    # operator quotes back.
    return CheckResult(
        name,
        OK,
        "bubblewrap can create a namespace here, so tasks are confined by the sandbox",
    )


# ---------------------------------------------------------------------------
# developer.*
# ---------------------------------------------------------------------------

_FORGE_BINARIES = ("gh", "glab")


def _resolved_forge_bin(dev, name: str) -> str:
    """What the wrapper would actually exec for `name`."""
    # The leaf, not `skills.developer` — reaching the same function through the
    # skill package costs ~190ms of import on every `load_config`, which is the
    # exact expense `probe=False` exists to avoid.
    from .forge_bin import resolve_real_bin

    configured = dev.gh_bin_path if name == "gh" else dev.glab_bin_path
    return resolve_real_bin(configured, name)


def _configured_forge_bin(dev, name: str) -> str:
    return dev.gh_bin_path if name == "gh" else dev.glab_bin_path


def check_forge_binaries(config: "Config", probe: bool) -> list[CheckResult]:
    """The binary the wrapper will exec exists and is executable.

    This is the ISSUE-263 shape exactly: ``setup_env`` wrote the wrappers, ``gh``
    resolved on PATH to one, and the wrapper's ``os.execve`` hit a path that did
    not exist and exited 6 — after clone, branch and push had all worked, so the
    skill looked configured and died only where it would publish.

    ``FAIL`` is reserved for this, because it is unambiguous and needs no
    version knowledge.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return [
            CheckResult(f"developer.forge_binaries.{n}", SKIP, reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]
    token_reason = _forge_token_gate(dev)
    if token_reason:
        return [
            CheckResult(f"developer.forge_binaries.{n}", SKIP, token_reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]

    results: list[CheckResult] = []
    for name in _FORGE_BINARIES:
        resolved = _resolved_forge_bin(dev, name)
        status, detail = _binary_status(resolved, probe=probe)
        results.append(
            CheckResult(
                f"developer.forge_binaries.{name}",
                status,
                detail,
                remedy=(
                    ""
                    if status == OK
                    else (
                        f"Install {name} and point [developer] {name}_bin_path at it; "
                        f"every forge command will otherwise fail at exec time."
                    )
                ),
                scope=IMAGE,
            )
        )
    return results


def check_forge_config_drift(config: "Config", probe: bool) -> list[CheckResult]:
    """The configured path is the path resolution actually returns.

    ``_resolve_real_bin``'s fallback chain is correct and load-bearing — it is
    what makes a code-only auto-update keep working — but it *hides* the stale
    ``config.toml`` that ``config.py`` used to warn about. Routing the only
    check through resolution therefore reports ``ok`` on exactly the drifted
    deployment this exists to catch.

    ``WARN``, never ``FAIL``: the deployment works. What it has lost is the
    property that its config file describes it.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return [
            CheckResult(f"developer.forge_config_drift.{n}", SKIP, reason)
            for n in _FORGE_BINARIES
        ]
    token_reason = _forge_token_gate(dev)
    if token_reason:
        return [
            CheckResult(f"developer.forge_config_drift.{n}", SKIP, token_reason)
            for n in _FORGE_BINARIES
        ]

    results: list[CheckResult] = []
    for name in _FORGE_BINARIES:
        configured = _configured_forge_bin(dev, name)
        resolved = _resolved_forge_bin(dev, name)
        exists = Path(configured).exists() if configured else False
        if exists and configured == resolved:
            results.append(
                CheckResult(
                    f"developer.forge_config_drift.{name}",
                    OK,
                    f"[developer] {name}_bin_path = {configured} is what resolution returns",
                )
            )
            continue
        # Two conditions, two messages. An operator who set an explicit path that
        # does not exist gets `configured == resolved` from `_resolve_real_bin`
        # (it returns a chosen path as given rather than exec'ing something
        # else), so a single combined message reads as the self-contradicting
        # "x but the wrapper will exec x".
        if configured and configured == resolved:
            results.append(
                CheckResult(
                    f"developer.forge_config_drift.{name}",
                    WARN,
                    (
                        f"[developer] {name}_bin_path = {configured} is what the wrapper "
                        f"will exec, but nothing exists there"
                    ),
                    remedy=(
                        f"Install {name} at that path, or point {name}_bin_path at the "
                        f"real one. An explicitly chosen path is never silently replaced."
                    ),
                )
            )
            continue
        results.append(
            CheckResult(
                f"developer.forge_config_drift.{name}",
                WARN,
                (
                    f"[developer] {name}_bin_path = {configured or '(unset)'} but the wrapper "
                    f"will exec {resolved}"
                ),
                remedy=(
                    f"Rewrite config.toml so {name}_bin_path names the installed binary "
                    f"(a full Ansible play does; the auto-update cron does not. On "
                    f"docker, restarting the istota service re-renders it)."
                ),
            )
        )
    return results


# The sentinel `forge_cli.py` carries for exactly this purpose. Matching on a
# deliberate marker rather than on docstring text: the wrapper is a verbatim
# copy of that file, whose prose happens to contain "istota" today, and an
# identity test that depends on wording flips a correct install to a failure the
# next time someone rewrites a comment.
_WRAPPER_SENTINEL = b"ISTOTA_FORGE_WRAPPER"


def _looks_like_the_wrapper(path: str) -> bool | None:
    """Whether `path` is istota's forge wrapper rather than a real forge binary.

    ``None`` means "could not tell" — an unreadable file is not evidence of a
    shadowing real binary, and reporting it as one would fail a deployment over
    a permission bit.

    Read as bytes and bounded: a real ``gh`` is a ~40MB Go binary, and reading
    it whole to answer a yes/no would be its own defect.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192)
    except OSError:
        return None
    if head[:4] == b"\x7fELF":
        return False
    return _WRAPPER_SENTINEL in head


def check_forge_wrapper_shadowing(config: "Config", probe: bool) -> list[CheckResult]:
    """Nothing resolves ``gh`` / ``glab`` by name to an *unexpected* binary.

    The question is not "is a real forge binary on PATH" — that is true by
    design on the Ansible shape, which is what production runs: the role
    installs the vendors' binaries into ``/usr/local/bin`` and renders those
    paths into ``config.toml``. Asserting the image's off-PATH layout everywhere
    reports a correct bare-metal host as broken, and since a ``FAIL`` alerts the
    admin allowlist, it would do so on every boot and every sweep.

    What is worth catching is a *disagreement*: something reachable as ``gh``
    that is not the binary this deployment resolved. That is the regression the
    off-PATH design exists to prevent — someone ``apt install``s gh into
    ``/usr/bin`` on the image shape, and the model's shell finds it before the
    per-task wrapper, skipping the deny policy and the per-call token injection
    that both live in the wrapper.

    So the four cases, and why each lands where it does:

    * nothing on PATH — the image shape, working as designed. ``OK``.
    * the wrapper — also fine; that is the thing meant to be found. ``OK``.
    * the same real binary resolution returned — the Ansible shape, working as
      designed. ``OK``, and the detail says which shape it is.
    * a *different* real binary — nobody intended this. ``FAIL``.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return [
            CheckResult(f"developer.forge_wrapper_shadowing.{n}", SKIP, reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]
    # The token gate applies here too, which the spec's gating sentence assigns
    # only to the binary and drift checks. Both of the things a shadowing
    # binary bypasses — the deny policy and the per-call token injection — exist
    # to govern a credential, so with no credential configured there is nothing
    # being bypassed. Without this gate a tokenless deployment on any host with a
    # real `gh` on PATH (every developer laptop) goes from silent today to
    # alerting, which is the exact regression the gating exists to prevent.
    token_reason = _forge_token_gate(dev)
    if token_reason:
        return [
            CheckResult(f"developer.forge_wrapper_shadowing.{n}", SKIP, token_reason, scope=IMAGE)
            for n in _FORGE_BINARIES
        ]

    results: list[CheckResult] = []
    for name in _FORGE_BINARIES:
        found = shutil.which(name)
        if found is None:
            results.append(
                CheckResult(
                    f"developer.forge_wrapper_shadowing.{name}",
                    OK,
                    f"nothing on the daemon's PATH resolves `{name}`",
                    scope=IMAGE,
                )
            )
            continue
        identified = _looks_like_the_wrapper(found)
        if identified:
            results.append(
                CheckResult(
                    f"developer.forge_wrapper_shadowing.{name}",
                    OK,
                    f"`{name}` resolves to the istota wrapper at {found}",
                    scope=IMAGE,
                )
            )
            continue
        if identified is None:
            results.append(
                CheckResult(
                    f"developer.forge_wrapper_shadowing.{name}",
                    WARN,
                    f"`{name}` resolves to {found}, which could not be read to identify it",
                    remedy=(
                        f"Check the permissions on {found}. Until it can be read, whether "
                        f"it shadows the wrapper is unknown rather than fine."
                    ),
                    scope=IMAGE,
                )
            )
            continue
        resolved = _resolved_forge_bin(dev, name)
        if os.path.realpath(found) == os.path.realpath(resolved):
            results.append(
                CheckResult(
                    f"developer.forge_wrapper_shadowing.{name}",
                    OK,
                    (
                        f"`{name}` resolves to {found}, which is the binary this "
                        f"deployment resolved (the Ansible shape installs on PATH)"
                    ),
                    scope=IMAGE,
                )
            )
            continue
        results.append(
            CheckResult(
                f"developer.forge_wrapper_shadowing.{name}",
                FAIL,
                (
                    f"`{name}` resolves on PATH to {found}, but this deployment resolved "
                    f"{resolved} — neither the wrapper nor the intended binary"
                ),
                remedy=(
                    f"Remove the unexpected {name} from PATH. Whatever is found first "
                    f"bypasses the deny policy and the per-call token injection, which "
                    f"both live in the wrapper."
                ),
                scope=IMAGE,
            )
        )
    return results


def check_forge_policy(config: "Config", probe: bool) -> CheckResult:
    """A ``forge_cli_permit`` entry that matches no rule.

    Lifted from ``_validate_forge_clis``. A hatch that silently stopped matching
    after a baseline rewording looks exactly like one that is still open, and
    otherwise surfaces as nothing at all — which is the problem.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return CheckResult("developer.forge_policy", SKIP, reason)
    try:
        from .forge_cli import FORGE_GITHUB, FORGE_GITLAB, unmatched_permits

        dead = unmatched_permits(
            [FORGE_GITHUB, FORGE_GITLAB],
            list(dev.forge_cli_permit),
            list(dev.forge_cli_extra_denied),
        )
    except Exception as exc:  # noqa: BLE001 - never fail over a warning
        return CheckResult(
            "developer.forge_policy",
            WARN,
            f"forge_cli_permit validation could not run: {exc}",
            remedy="Check the [developer] forge_cli_permit / forge_cli_extra_denied syntax.",
        )
    if not dead:
        return CheckResult(
            "developer.forge_policy",
            OK,
            f"{len(dev.forge_cli_permit)} forge_cli_permit entrie(s), all matching a rule",
        )
    return CheckResult(
        "developer.forge_policy",
        WARN,
        f"forge_cli_permit entries matching no rule: {', '.join(repr(e) for e in dead)}",
        remedy=(
            "Check the spelling against the baseline policy before assuming the verb is "
            "permitted — the entry is turning nothing off."
        ),
    )


def check_gitlab_reviewer(config: "Config", probe: bool) -> CheckResult:
    """A GitLab MR reviewer that `glab` will not resolve.

    ISSUE-289. The setting is silent in both directions. A value `glab` cannot
    resolve fails inside the task, where only the model sees it; an unset one
    produces no message at all. Either way the MR opens with nobody assigned,
    which is the step that puts a person in the loop, and the deployment ran
    that way for weeks. WARN rather than FAIL: an MR with no reviewer is still
    an MR, and the operator may simply not want one.

    Everything is read through ``str()``. TOML types its scalars, so an
    unquoted ``gitlab_reviewer = 1234567`` — the natural hand-edit for a field
    whose example value is a number in quotes — arrives as an ``int``, and a
    check that called a string method on it would raise. ``run_checks`` turns a
    raising check into a FAIL, which is the one status that alerts, so the
    crash would page the operator in exactly the misconfiguration this exists
    to describe.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return CheckResult("developer.gitlab_reviewer", SKIP, reason)

    reviewer = str(dev.gitlab_reviewer or "")
    named = reviewer.strip()
    if named:
        if _looks_like_a_user_id(named):
            return CheckResult(
                "developer.gitlab_reviewer",
                WARN,
                f"developer.gitlab_reviewer is {named!r}, which is a user id, not a username",
                remedy=(
                    "`glab mr create --reviewer` resolves by username, and a GitLab "
                    "username is never all digits. Set the reviewer's username; "
                    "`glab api users/<id>` reports it."
                ),
            )
        if any(char.isspace() for char in reviewer):
            # The recipe expands `--reviewer $GITLAB_REVIEWER` unquoted, so an
            # internal space hands `glab` a stray positional and a surrounding
            # one is eaten by word-splitting. Neither is a username.
            return CheckResult(
                "developer.gitlab_reviewer",
                WARN,
                f"developer.gitlab_reviewer is {reviewer!r}, which contains whitespace",
                remedy=(
                    "A GitLab username has no spaces in it. This is most likely the "
                    "reviewer's display name; set their username instead."
                ),
            )
        return CheckResult(
            "developer.gitlab_reviewer",
            OK,
            f"MR reviewer {named!r}",
        )

    recorded = str(dev.gitlab_reviewer_id or "").strip()
    if recorded:
        # Which message is right depends on what the operator was told. The
        # field was documented as a username for one day before ISSUE-289 was
        # filed (56d21548), and as a numeric id for everything before that, so
        # both shapes are deployed and the remedy differs.
        if _looks_like_a_user_id(recorded):
            remedy = (
                "The id is recorded and read by nothing. Add "
                "developer.gitlab_reviewer with the same person's username; "
                "`glab api users/<id>` reports it."
            )
        else:
            remedy = (
                f"{recorded!r} is already a username — copy it verbatim into "
                "developer.gitlab_reviewer, which is the key that is read now."
            )
        return CheckResult(
            "developer.gitlab_reviewer",
            WARN,
            "developer.gitlab_reviewer_id is set but developer.gitlab_reviewer is not, "
            "so new merge requests get no reviewer",
            remedy=remedy,
        )
    return CheckResult(
        "developer.gitlab_reviewer",
        OK,
        "no MR reviewer configured",
    )


def check_forge_transport(config: "Config", probe: bool) -> CheckResult:
    """A forge token that travels over plain HTTP.

    WARN, never FAIL: the operator wrote the `http://` themselves, the
    deployment works, and refusing to run over it is not doctor's call. What it
    is is a credential leaving the host in cleartext, which nothing else in the
    report says.

    This is newly reachable rather than newly true. A plain-HTTP `gitlab_url`
    used to die at the TLS handshake — glab forces https and discards the
    scheme in `GITLAB_HOST` — so no token ever left, and the deployment was
    broken rather than insecure. The developer skill now seeds glab's own
    `api_protocol` for that case (`_plain_http_host_entry`), which makes the
    call work and the plaintext transport real.

    Both forges are checked, and for gh the plaintext is the whole of what is
    wrong: gh refuses a scheme inside `GH_HOST` outright, so a plain-HTTP
    `github_url` cannot connect however it is spelled. The port half of that
    used to be broken too and no longer is — `forge_cli._gh_host` keeps a
    non-default port (ISSUE-279), so a forge on `:8443` is reachable and only
    its scheme is this check's business.

    The detail names the URL and never the token. A URL can carry userinfo, so
    it is redacted rather than printed raw.
    """
    dev, reason = _dev_gate(config)
    if dev is None:
        return CheckResult("developer.forge_transport", SKIP, reason, scope=DEPLOYMENT)
    token_reason = _forge_token_gate(dev)
    if token_reason:
        return CheckResult(
            "developer.forge_transport", SKIP, token_reason, scope=DEPLOYMENT
        )

    plaintext, embedded = [], []
    for label, url, token in (
        ("gitlab_url", dev.gitlab_url, dev.gitlab_token),
        ("github_url", dev.github_url, dev.github_token),
    ):
        # Only where a token would actually be sent. A configured-but-tokenless
        # forge sends no credential, so its scheme is not this check's business.
        if not token or not url:
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            # `http://[::1` raises Invalid IPv6 URL. Unguarded, `run_checks`
            # turns that into a FAIL whose remedy says "this is a defect in the
            # check" — a WARN-only check emitting a FAIL, and blaming itself
            # for the operator's typo.
            embedded.append(f"{label} is not a parseable URL")
            continue
        if "@" in (parts.netloc or ""):
            # Any scheme. A credential in the URL is a disclosure on https too,
            # and since `_plain_http_host_entry` refuses to write glab's
            # protocol entry for such a URL — the entry would have to carry the
            # password, into a file the sandbox can read — a plain-HTTP one
            # also silently fails to connect. This is the only thing that says
            # why.
            embedded.append(f"{label} = {_redact_userinfo(url)}")
        elif parts.scheme == "http":
            plaintext.append(f"{label} = {_redact_userinfo(url)}")

    if not plaintext and not embedded:
        return CheckResult(
            "developer.forge_transport",
            OK,
            "every configured forge with a token is reached over https",
            scope=DEPLOYMENT,
        )

    details, remedies = [], []
    if embedded:
        details.append(f"a forge URL carries a credential: {', '.join(embedded)}")
        remedies.append(
            "Move the credential to [developer] gitlab_token / github_token and "
            "rotate it — a URL reaches logs, remotes and process arguments. A "
            "plain-HTTP forge configured this way also cannot connect at all, "
            "because the protocol entry that would fix it is not written for a "
            "URL that would put the password in a sandbox-readable file."
        )
    if plaintext:
        details.append(f"a forge token is sent over plain HTTP: {', '.join(plaintext)}")
        remedies.append(
            "Point the URL at https, or accept that the token — and everything "
            "the CLI sends with it — crosses the network in the clear. A "
            "loopback URL is usually a tunnel, and what is on its far side is "
            "not visible from here."
        )
    return CheckResult(
        "developer.forge_transport",
        WARN,
        "; ".join(details),
        remedy=" ".join(remedies),
        scope=DEPLOYMENT,
    )


def _redact_userinfo(url: str) -> str:
    """A URL safe to print: userinfo replaced, everything else intact.

    A forge URL is operator-written config and is not supposed to carry
    credentials, but `https://user:token@host` parses fine and this string goes
    straight into a report that reaches the admin dashboard and the log.

    **Replaced, not removed.** Deleting the userinfo renders
    `https://bot:token@host` as `https://host`, and an operator reading that
    cannot tell the configured value carried a credential at all — which is the
    most useful thing the report could have told them, and the thing they need
    in order to know something wants rotating.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unparseable)"
    if "@" not in (parts.netloc or ""):
        return url
    _, _, hostport = parts.netloc.rpartition("@")
    return urlunsplit(
        (parts.scheme, f"{_REDACTED}@{hostport}", parts.path, parts.query, parts.fragment)
    )


# ---------------------------------------------------------------------------
# web.*
# ---------------------------------------------------------------------------

def check_basemap(config: "Config", probe: bool) -> CheckResult:
    """Whether the map surfaces have a background that will actually render.

    **This check deliberately opens no socket, and that is the finding rather
    than a shortcut.** Two facts, both established by measurement, remove the
    value a fetch would have had:

    The watermark is not observable from the response. Measured against the
    live service on 2026-08-28, CARTO returns 200, ``content-type: image/png``
    and a byte-identical body and ETag for a keyless request, a request with a
    bogus key and (by construction) a good one. There is no status, header or
    length to key on, so a probe there reports a working basemap for a defaced
    one — worse than not probing, because it manufactures confidence.

    And the daemon is the wrong host. Tiles are fetched by the *browser*, over
    a different route. A deployment whose egress is a proxy would fail a probe
    for a basemap every browser on the network renders correctly, and one with
    open egress would pass for a browser network that blocks the CDN. Running
    it anyway also put a third-party request on the daemon's boot path and on
    the hourly doctor sweep — up to two per run, at ``PROBE_TIMEOUT`` each,
    where a CDN blip becomes a FAIL and pages the operator about a deployment
    with nothing wrong with it.

    What is left is the half that *is* decidable from here, and it happens to
    be the half that matters: a keyed provider with no key is exactly the
    reported bug (ISSUE-334), and configuration says so for free and with
    certainty. ``probe`` is accepted to satisfy the ``Check`` protocol and is
    unused.
    """
    from .map_basemap import resolve_basemap

    web = getattr(config, "web", None)
    if not web or not web.enabled:
        return CheckResult(
            "web.basemap", SKIP, "web interface disabled", scope=DEPLOYMENT
        )

    m = getattr(web, "map", None)
    if m is None:
        return CheckResult(
            "web.basemap", SKIP, "no [web.map] configuration", scope=DEPLOYMENT
        )

    spec = resolve_basemap(
        provider=m.provider,
        api_key=m.api_key,
        dark_style=m.dark_style,
        light_style=m.light_style,
        attribution=m.attribution,
    )

    if spec.needs_key:
        return CheckResult(
            "web.basemap",
            WARN,
            f"provider is {m.provider!r} with no API key, so its tiles come "
            "back watermarked 'API KEY REQUIRED' with a 200 status; the maps "
            f"are rendering on {spec.provider!r} instead",
            remedy=(
                "Request a free key at https://carto.com/basemaps/apikey/ and "
                "set it in [web.map] api_key, or per user on the location "
                "settings page. Or set [web.map] provider = \"openfreemap\", "
                "which needs no key. Note that CARTO is retiring its raster "
                "service, so a key buys time rather than a fix."
            ),
            scope=DEPLOYMENT,
        )

    if spec.fell_back:
        return CheckResult(
            "web.basemap",
            WARN,
            f"basemap config did not resolve as written: {spec.warning}",
            remedy=(
                "Fix [web.map] provider, or the custom style URLs beside it. "
                f"The maps are rendering on {spec.provider!r} meanwhile."
            ),
            scope=DEPLOYMENT,
        )

    detail = f"provider {spec.provider!r}"
    if spec.provider in _UNVERIFIABLE_KEY_PROVIDERS:
        detail += (
            " with an API key configured — configured, not verified: the "
            "service answers 200 with the same watermarked tile for a good "
            "key, a bad key and no key at all"
        )
    if spec.warning:
        detail += f"; {spec.warning}"
    return CheckResult("web.basemap", OK, detail, scope=DEPLOYMENT)


# Providers whose key cannot be validated from a response. Named here rather
# than imported so the reason travels with the sentence that depends on it.
_UNVERIFIABLE_KEY_PROVIDERS = frozenset({"carto"})


def check_avatar_import(config: "Config", probe: bool) -> CheckResult:
    """Whether Nextcloud profile pictures are being imported, and what happened.

    **This check opens no socket, and that is deliberate rather than lazy.**
    Doctor runs on the daemon's start-up path, on a scheduler interval, from
    `istota doctor` and from the admin dashboard's Health pane. A live Nextcloud
    call here would put a remote timeout in front of all four, and the Health
    pane is a page a person is waiting on. Same reasoning as `web.basemap`.

    So it reports configuration and recorded state: the two switches, the counts
    in `user_avatars`, and what the last tick wrote down. That last part is the
    only way to answer the question that actually matters here — whether
    Nextcloud sends the custom-avatar header at all. Without it nothing can ever
    be imported, and no count of stored rows distinguishes that from a
    deployment where nobody has set a Nextcloud avatar. `probe` is accepted to
    satisfy the `Check` protocol and is unused.
    """
    from . import avatars, db
    from .nextcloud.avatars import CUSTOM_AVATAR_HEADER

    name = "web.avatar_import"

    web = getattr(config, "web", None)
    if not web or not web.enabled:
        return CheckResult(name, SKIP, "web interface disabled", scope=DEPLOYMENT)
    if not config.storage_is_nextcloud:
        return CheckResult(
            name, SKIP,
            "storage backend is local; there is no Nextcloud to import from",
            scope=DEPLOYMENT,
        )
    if not getattr(web, "avatar_import_from_nextcloud", False):
        return CheckResult(
            name, SKIP, "[web] avatar_import_from_nextcloud is false",
            scope=DEPLOYMENT,
        )
    if not getattr(config.scheduler, "avatar_import_interval", 0):
        return CheckResult(
            name, SKIP, "[scheduler] avatar_import_interval is 0", scope=DEPLOYMENT,
        )

    try:
        with db.get_db(config.db_path) as conn:
            counts = avatars.import_counts(conn)
            state = avatars.read_import_state(conn)
    except Exception as exc:  # noqa: BLE001 - a check never raises
        return CheckResult(
            name, WARN, f"could not read the avatar tables: {exc}",
            remedy=(
                "Check `runtime.framework_db`, which reports on the database "
                "itself; this check reads it and nothing else."
            ),
            scope=DEPLOYMENT,
        )

    stored = (
        f"{counts['imported']} imported, "
        f"{counts['probes']} with no custom Nextcloud avatar"
    )

    if state is None:
        return CheckResult(
            name, OK,
            f"enabled every {config.scheduler.avatar_import_interval}s; "
            f"no import tick has been recorded yet; {stored}",
            scope=DEPLOYMENT,
        )

    header = state.get("header")
    ran = state.get("at") or "an unrecorded time"
    detail = (
        f"last tick at {ran} over {state.get('users', 0)} users "
        f"({state.get('imported', 0)} imported, "
        f"{state.get('no_custom', 0)} with no custom avatar, "
        # `unchanged` is the steady state — every user answering 304 — so
        # leaving it out made a healthy deployment print four zeroes that do
        # not add up to the user count beside them, which reads as a tick that
        # did nothing rather than one with nothing to do.
        f"{state.get('unchanged', 0)} unchanged, "
        f"{state.get('failed', 0)} failed); {stored}"
    )

    if header == avatars.HEADER_ABSENT:
        return CheckResult(
            name, WARN,
            f"{detail}; Nextcloud sent no {CUSTOM_AVATAR_HEADER} "
            "header, so a user-set picture cannot be told from the coloured "
            "letter it generates and nothing will be imported",
            remedy=(
                "Nothing here is broken and nothing is being imported. Either "
                "upgrade Nextcloud to a version that sends the header, or set "
                "[web] avatar_import_from_nextcloud = false to stop asking. "
                "Users can still upload their own picture in Settings."
            ),
            scope=DEPLOYMENT,
        )

    # **A tick every user failed is not an OK**, and it used to be: `failed` was
    # rendered into the detail and gated nothing, so the only non-OK this check
    # could produce was the absent header. A deployment whose every fetch raised
    # — a wrong `nextcloud.username`, an expired app password, a uid mapping
    # that matches no Nextcloud account — printed `5 failed` inside a green
    # line. The whole reason this row is written down is that doctor cannot open
    # a socket to find out; reading it and then ignoring the one column that
    # says "this is not working" gives that up for nothing.
    failed = state.get("failed", 0)
    progressed = state.get("imported", 0) + state.get("no_custom", 0)
    if failed and not progressed:
        return CheckResult(
            name, WARN,
            f"{detail}; every user the last tick tried failed",
            remedy=(
                "Check `nextcloud.username` and the app password, and that the "
                "ids in [users] are the Nextcloud uids. The daemon log carries "
                "the per-user reason at WARNING, tagged avatar_import_failed."
            ),
            scope=DEPLOYMENT,
        )

    # **A tick that has not run in days is not an OK either.** Two documented
    # paths stop this job silently and leave the last good row standing as the
    # current answer: `_spawn_background_check` will not start a second run
    # while the first is alive, so one wedged fetch means no further ticks
    # ever, and `check_avatar_import` returns early on an unreadable probe
    # state without recording anything at all.
    stale = _avatar_tick_is_stale(
        state.get("at"), config.scheduler.avatar_import_interval
    )
    if stale:
        return CheckResult(
            name, WARN,
            f"{detail}; that is more than {stale} — the import may have stopped",
            remedy=(
                "Check the daemon log for avatar-import errors. A tick that "
                "never finishes blocks every later one, since a second run is "
                "not started while the first thread is alive."
            ),
            scope=DEPLOYMENT,
        )

    if header == avatars.HEADER_UNOBSERVED:
        detail += "; nothing changed, so the custom-avatar header was not read"
    return CheckResult(name, OK, detail, scope=DEPLOYMENT)


def _avatar_tick_is_stale(at: object, interval: int) -> str | None:
    """How overdue the last tick is, or None if it is not.

    Returns a human phrase rather than a bool so the caller can say how late it
    is. Parses defensively and answers None on anything it cannot read: `at` is
    a JSON value out of a KV table, and a check never raises — reporting a
    healthy import as broken because a timestamp changed shape would be worse
    than the staleness it is looking for.
    """
    if not isinstance(at, str) or not at.strip() or interval <= 0:
        return None
    text = at.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    # Three intervals, not one: a tick runs on a cadence and a single missed
    # one is a restart or a slow fetch, not a fault worth paging about.
    limit = interval * 3
    age = (datetime.now(timezone.utc) - when).total_seconds()
    if age <= limit:
        return None
    return f"{limit // 3600}h" if limit >= 3600 else f"{limit}s"


def check_web_static(config: "Config", probe: bool) -> CheckResult:
    """The SvelteKit build the web surface serves actually exists.

    A web-builder stage that silently produced nothing gives a running server
    that 404s its own app shell — which reads as a routing bug for as long as it
    takes someone to look in the image.
    """
    if not getattr(config.web, "enabled", False) and not getattr(config, "site", None):
        return CheckResult("web.static", SKIP, "no web surface configured", scope=IMAGE)
    if not getattr(config.web, "enabled", False):
        return CheckResult("web.static", SKIP, "[web] enabled = false", scope=IMAGE)
    # The leaf, not `web_app`: importing that module pulls in FastAPI, authlib,
    # starlette and httpx (+56 MB RSS, permanently, in the scheduler process)
    # and runs a second full `load_config()` at import time. A diagnostic does
    # not get to cost that.
    from .static_dir import resolve_static_dir

    index = Path(resolve_static_dir()) / "index.html"
    if not index.is_file():
        return CheckResult(
            "web.static",
            FAIL,
            f"{index} does not exist",
            remedy="Build the frontend (`npm --prefix web run build`); the image's web-builder stage does this.",
            scope=IMAGE,
        )
    if index.stat().st_size == 0:
        return CheckResult(
            "web.static",
            FAIL,
            f"{index} is empty",
            remedy="Rebuild the frontend; the build produced a zero-byte app shell.",
            scope=IMAGE,
        )
    return CheckResult("web.static", OK, f"{index} is present ({index.stat().st_size} bytes)", scope=IMAGE)


_SHA_LENGTHS = (40, 64)


def _repo_root() -> Path:
    """The checkout this package was imported from, if it is one.

    The same derivation :func:`static_dir.resolve_static_dir` uses for its
    repo-relative candidate, so the two agree about which tree is in play.

    Two levels above the package directory. Under the Ansible deployment that
    is the checkout at ``istota_repo_dir``, since ``uv sync`` installs the
    project editable and ``__file__`` resolves into ``{repo}/src/istota/``. On
    a wheel install it is a directory inside the venv rather than
    site-packages, and either way it holds no ``.git`` — which is what makes
    the check below skip there instead of guessing.
    """
    return Path(__file__).resolve().parent.parent.parent


def _is_sha(value: str) -> bool:
    return len(value) in _SHA_LENGTHS and all(c in "0123456789abcdef" for c in value.lower())


def check_web_build_current(config: "Config", probe: bool) -> CheckResult:
    """The served bundle is current for the ``web/`` tree this checkout has.

    ``web.static`` asks only whether ``index.html`` exists and is non-empty,
    and both stay true across a stale bundle — which is the condition ISSUE-428
    reported: the auto-update cron shipped a frontend-only commit, restarted
    every unit, and the browser kept running old code with nothing on the host
    saying so. SvelteKit already emits ``_app/version.json`` for its own
    updated-build polling, and ``web/svelte.config.js`` stamps
    ``ISTOTA_BUILD_SHA`` into it, so the artifact names the commit it came from.

    **The question is whether ``web/`` moved, not whether HEAD did**, and the
    difference is the whole check. The cron rebuilds only when a commit touches
    ``web/``, by design, so on a busy branch the stamp trails HEAD almost all
    the time while the bundle is byte-for-byte the one this checkout would
    produce. Comparing the two shas for equality therefore reports a warning on
    an ordinary Python-only deploy — permanently, on a host whose cron fires
    every two minutes — and, worse, makes the genuinely stale bundle
    indistinguishable from the benign case the check exists to separate.

    That question needs git, so it is **probe-gated** and answers ``SKIP``
    without one. `probe` is True on every surface that matters — the boot run,
    the hourly sweep, ``istota doctor``, ``!check``, the admin pane — and False
    only on ``config.CONFIG_LOAD_CHECKS``, which this is not in.

    A mismatch is a **WARN**, and every state this cannot settle is a ``SKIP``.
    Three shipped shapes legitimately do not stamp a commit — a Docker image, a
    wheel install, a developer's own ``npm run build`` — and a check that cannot
    tell those from a stale deploy must not claim either. WARN rather than FAIL
    for the reason the severity exists: the next auto-update tick clears it, and
    a warning that pages someone is a failure wearing the wrong label.
    """
    if not getattr(config.web, "enabled", False):
        return CheckResult("web.build_current", SKIP, "[web] enabled = false")
    # Function-local like every other package import in this module: nothing
    # here may land on the config-load path's import graph.
    from .git_hardening import GIT_HARDENING
    from .static_dir import resolve_static_dir

    version_file = Path(resolve_static_dir()) / "_app" / "version.json"
    try:
        payload = json.loads(version_file.read_text(encoding="utf-8", errors="replace"))
        stamped = str(payload["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return CheckResult(
            "web.build_current",
            SKIP,
            f"{version_file} is missing or unreadable",
        )
    if not _is_sha(stamped):
        return CheckResult(
            "web.build_current",
            SKIP,
            "the bundle is not stamped with a commit (an image, a wheel install "
            "or a local build)",
        )
    if not probe:
        return CheckResult(
            "web.build_current",
            SKIP,
            f"the bundle names {stamped[:12]}; comparing it to the checkout needs "
            "git, which was not executed",
        )
    root = _repo_root()
    if not (root / ".git").exists():
        return CheckResult(
            "web.build_current",
            SKIP,
            f"{root} is not a git checkout, so there is nothing to compare against",
        )
    # `stamped` is hex by `_is_sha` and this is an argv rather than a shell
    # string, so it can name no option and start no command. GIT_HARDENING for
    # the reason it always goes on a `git` call here: `diff.external` runs a
    # program named by the repository's own config.
    proc = _run(
        [
            "git",
            "-C",
            str(root),
            *GIT_HARDENING,
            "diff",
            "--quiet",
            stamped,
            "HEAD",
            "--",
            "web/",
        ]
    )
    if proc is None or proc.returncode not in (0, 1):
        # An unknown object is the ordinary case here, not a fault: the bundle
        # can outlive a re-clone or a history rewrite. Unanswerable, not stale.
        return CheckResult(
            "web.build_current",
            SKIP,
            f"git could not compare the bundle's commit {stamped[:12]} against the checkout",
        )
    if proc.returncode == 0:
        return CheckResult(
            "web.build_current",
            OK,
            f"the bundle was built from {stamped[:12]} and web/ has not changed since",
        )
    return CheckResult(
        "web.build_current",
        WARN,
        f"web/ has changed since the bundle was built from {stamped[:12]}",
        remedy=(
            "Re-run the Ansible play, which rebuilds the frontend unconditionally. "
            "The auto-update cron also builds when a commit touches web/, so a bundle "
            "left behind means that run failed — see the update log. Deleting the "
            "deployed-revision marker does not force a rebuild; it is re-seeded from "
            "HEAD on the next tick."
        ),
    )


# ---------------------------------------------------------------------------
# sandbox.* (deep)
# ---------------------------------------------------------------------------


def check_sandbox_masks(config: "Config", probe: bool) -> CheckResult:
    """Spawn a real bubblewrap namespace and confirm the DB masks hold.

    The one check that costs a subprocess with a namespace in it, so it runs
    only under ``deep=True``. What it asserts is what argv assertions
    structurally cannot: that the database directories are empty and unwritable
    *inside* the namespace, rather than that the right flags were passed.

    **It probes under the NATIVE profile, and that narrows what it covers.**
    The masks and the system binds it reads are part of the generic plan and
    identical under both profiles, so its verdict is unchanged — that much is
    pinned by ``tests/test_doctor.py::TestSandboxMasksUsesTheNativeProfile``.
    What it stops doing is exercising whether the *CLAUDE* argv can be built at
    all on this host. bwrap exits before running anything on a bad mount
    operation, and the CLAUDE-only binds have a recorded failure of exactly
    that kind: a config directory under ``db_path.parent`` shadows the
    system-prompt bind, which is how narrowing ``sandbox_ro_paths`` to ``[]``
    made every task on a ``custom_system_prompt`` install exit with "System
    prompt file not found". A host in that state now passes this check while
    every claude_code task fails.

    What still covers it live is the heartbeat's ``self-check`` and ``!check``,
    both of which build a CLAUDE sandbox and run the CLI through it — but both
    are opt-in, where this ran on every ``--deep``. The tier that covers it
    unconditionally is ``tests/linux/test_sandbox_profiles_real.py``. Probing
    both profiles here would restore it for two subprocesses; that is a
    deliberate omission rather than an oversight.
    """
    if not probe:
        # The contract is unconditional: probe=False forbids spawning. Checked
        # before `_bwrap_usable()`, which spawns a probe of its own.
        return CheckResult(
            "sandbox.masks",
            SKIP,
            "a namespace cannot be entered without spawning one (probe disabled)",
        )
    if not getattr(config.security, "sandbox_enabled", False):
        # `_bwrap_usable()` answers "could a namespace be created", which is not
        # the same question. On a Linux host with bwrap installed and the
        # sandbox switched off, probing anyway would report a boundary healthy
        # that the executor never applies — the most misleading answer available.
        return CheckResult(
            "sandbox.masks",
            SKIP,
            "sandbox is disabled ([security] sandbox_enabled); the executor applies no masks",
        )
    if not _bwrap_usable():
        return CheckResult(
            "sandbox.masks", SKIP, "bubblewrap cannot create a namespace here"
        )
    try:
        import tempfile

        from . import db
        from .executor import SandboxProfile, build_bwrap_cmd

        # Both directories `build_bwrap_cmd` masks, not just the framework one —
        # the message says "directories" and `module_db_root()` is the one that
        # went unmasked in the first place.
        db_dirs = [Path(config.db_path).parent]
        try:
            db_dirs.append(config.module_db_root())
        except Exception:  # noqa: BLE001 - a misconfigured module_data_dir raises
            pass

        task = db.Task(
            id=0,
            status="running",
            source_type="doctor",
            user_id="doctor",
            prompt="",
        )
        # Two questions per directory: is it empty, and does a write into it
        # fail? A mask that is present but writable is the failure mode that
        # reads as corruption rather than as a boundary.
        probe_script = "; ".join(
            f'ls -A "{d}" 2>/dev/null | head -n 1; '
            f'touch "{d}/.doctor-probe" 2>/dev/null && echo WRITABLE'
            for d in db_dirs
        )
    except Exception as exc:  # noqa: BLE001 - a deep probe must not take the caller down
        return CheckResult(
            "sandbox.masks",
            FAIL,
            f"the sandbox probe could not be built: {exc}",
            remedy="Check [security] sandbox settings; build_bwrap_cmd refused to build a command.",
        )

    # A TemporaryDirectory, not a fixed path under temp_dir: the previous shape
    # created `{temp_dir}/doctor` on every deep run and never removed it.
    try:
        with tempfile.TemporaryDirectory(prefix="istota-doctor-") as user_temp:
            cmd = build_bwrap_cmd(
                ["/bin/sh", "-c", probe_script],
                config,
                task,
                is_admin=False,
                user_resources=[],
                user_temp_dir=Path(user_temp),
                # NATIVE, because this probe execs `/bin/sh` and not the
                # `claude` CLI — there is no reason for a diagnostic to build a
                # namespace holding the subscription credential. The masks it
                # asserts on are part of the generic plan and identical under
                # both profiles, so the verdict is unchanged.
                profile=SandboxProfile.NATIVE,
            )
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=DEEP_TIMEOUT, check=False
            )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "sandbox.masks",
            FAIL,
            f"the sandbox probe timed out after {DEEP_TIMEOUT}s",
            remedy="Investigate by hand; a bubblewrap spawn that never returns is not a mask problem.",
        )
    except Exception as exc:  # noqa: BLE001 - a deep probe must not take the caller down
        return CheckResult(
            "sandbox.masks",
            FAIL,
            f"the sandbox probe could not be run: {exc}",
            remedy="Confirm bubblewrap works (`bwrap --version`).",
        )

    output = (result.stdout or "").strip()
    if "WRITABLE" in output:
        return CheckResult(
            "sandbox.masks",
            FAIL,
            "a database directory is writable inside the sandbox",
            remedy=(
                "build_bwrap_cmd must mask the DB directories last, read-only. A writable "
                "mask lets a sqlite3 probe create a zero-byte file that reads as corruption."
            ),
        )
    visible = [line for line in output.splitlines() if line and line != "WRITABLE"]
    if visible:
        return CheckResult(
            "sandbox.masks",
            FAIL,
            f"a database directory is not empty inside the sandbox ({len(visible)} entr(ies) visible)",
            remedy=(
                "Narrow [security] sandbox_ro_paths; an earlier bind is showing through "
                "the mask, or the mask is no longer the last mount operation."
            ),
        )
    return CheckResult(
        "sandbox.masks", OK, "the database directories are empty and unwritable inside the sandbox"
    )


# ---------------------------------------------------------------------------
# Devbox network isolation
# ---------------------------------------------------------------------------

# The chain the role writes into, and the marker every rule it writes carries.
# The comment is how our rules are told apart from an operator's, and it is
# already load-bearing elsewhere: `iptables -C` matches on it, so the role's
# teardown depends on the exact string too.
DEVBOX_CHAIN = "DOCKER-USER"
_DEVBOX_RULE_MARK = "istota-devbox:"

# The boot script the Ansible role installs. Read as the *oracle* for what this
# host is supposed to be blocking, and for which subnet — a count hardcoded here
# would have to be updated in lockstep with the role's blocklist, and the first
# time it was not, the check would call a healthy host broken.
DEVBOX_BOOT_SCRIPT = Path("/usr/local/sbin/istota-devbox-iptables")

# Targets that end a packet's traversal of a user-defined chain without
# reaching what follows. DROP and REJECT are deliberately absent: a rule that
# blocks ahead of ours blocks more, not less, and reporting it would be noise.
_TERMINAL_TARGETS = frozenset({"RETURN", "ACCEPT"})

# Built-in targets, so a `-j` into anything else can be recognised as a jump
# into a user-defined chain — which this check does not follow, and which
# terminates traversal for whatever the target chain accepts.
_BUILTIN_TARGETS = frozenset(
    {
        "ACCEPT", "DROP", "RETURN", "REJECT", "LOG", "MARK", "MASQUERADE",
        "SNAT", "DNAT", "REDIRECT", "TCPMSS", "AUDIT", "CONNMARK", "NFLOG",
        "NFQUEUE", "NOTRACK", "TEE", "TPROXY", "TRACE", "ULOG",
    }
)

# Options that make a rule match less than every packet. `-m comment` is
# deliberately not one: a comment is an annotation, and treating it as a match
# condition is what let an annotated unconditional RETURN read as harmless.
_MATCH_OPTIONS = frozenset(
    {
        "-s", "--source", "-d", "--destination", "-i", "--in-interface",
        "-o", "--out-interface", "-p", "--protocol", "-f", "--fragment",
    }
)

_ENSURE_DROP = re.compile(r'^\s*ensure_drop\s+"([^"]+)"\s+"[^"]*"\s*$', re.M)
_SCRIPT_SUBNET = re.compile(r'^\s*SUBNET="([^"]*)"\s*$', re.M)


def parse_devbox_boot_script(text: str) -> set[str]:
    """The destinations the installed boot script blocks.

    Returns an empty set for anything it cannot read rather than raising —
    doctor runs on the daemon's start-up path, and a parser that threw on an
    unexpected file would turn a diagnostic into an outage.
    """
    try:
        return {match.group(1) for match in _ENSURE_DROP.finditer(text or "")}
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        return set()


def parse_devbox_boot_subnet(text: str) -> str:
    """The subnet the installed boot script scopes its rules to, or ""."""
    match = _SCRIPT_SUBNET.search(text or "")
    return match.group(1).strip() if match else ""


def parse_iptables_rule(line: str, chain: str) -> dict | None:
    """One ``-A <chain> ...`` line of ``iptables -S``, as fields.

    Tokenised with ``shlex`` rather than picked apart with a regex over the raw
    line, because a rule carries an arbitrary operator-supplied string in
    ``--comment``. A regex searching the whole line for ``-j`` finds the one
    inside ``--comment "see -j DROP note"`` and reports the wrong target, which
    on this check's FAIL path means reporting a shadowing rule as harmless.
    ``shlex`` puts the comment in a single token, so scanning tokens can only
    see real options.

    Returns None for a line this cannot read, so the caller can say it could not
    read it instead of quietly treating it as benign.
    """
    prefix = f"-A {chain}"
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    if len(tokens) < 2 or tokens[0] != "-A" or tokens[1] != chain.split()[0]:
        return None
    if not line.strip().startswith(prefix):
        return None

    rule = {
        "raw": line.strip(),
        "target": "",
        "goto": False,
        "source": "",
        "destination": "",
        "comment": "",
        "conditional": False,
    }
    index = 2
    while index < len(tokens):
        token = tokens[index]
        value = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token == "!":
            # A negated match is still a match condition.
            rule["conditional"] = True
            index += 1
            continue
        if token in ("-j", "--jump", "-g", "--goto"):
            rule["target"] = value
            rule["goto"] = token in ("-g", "--goto")
            index += 2
            continue
        if token in ("-s", "--source"):
            rule["source"] = value
            rule["conditional"] = True
            index += 2
            continue
        if token in ("-d", "--destination"):
            rule["destination"] = value
            rule["conditional"] = True
            index += 2
            continue
        if token == "--comment":
            rule["comment"] = value
            index += 2
            continue
        if token == "-m":
            # `-m comment` is an annotation, not a condition. Every other match
            # module narrows what the rule applies to.
            if value != "comment":
                rule["conditional"] = True
            index += 2
            continue
        if token in _MATCH_OPTIONS:
            rule["conditional"] = True
            index += 2
            continue
        if token.startswith("-"):
            # An option this does not model. Assume it narrows the rule rather
            # than assuming it does not: over-reporting a conditional rule is a
            # WARN, under-reporting one is a missed bypass.
            rule["conditional"] = True
            index += 2
            continue
        index += 1
    return rule


def _is_terminal(rule: dict) -> bool:
    """Does this rule stop traversal before the rules that follow it?

    A goto always does, and worse than a jump: when the target chain falls off
    its end, control returns to ``FORWARD``, not to ``DOCKER-USER``. A jump to
    a user-defined chain may, for whatever that chain accepts — this check does
    not follow it, so it counts as terminal and is reported as unfollowed.
    """
    if not rule["target"]:
        return False
    if rule["goto"]:
        return True
    if rule["target"] in _TERMINAL_TARGETS:
        return True
    return rule["target"] not in _BUILTIN_TARGETS


def _covers(rule: dict, subnet: str) -> bool | None:
    """Would `rule` catch traffic from `subnet`? True, False, or None for unknown.

    Three answers rather than two, because the two-answer versions are wrong in
    opposite directions and both were reachable here.

    An unscoped terminal rule catches everything, and that is the shape ISSUE-295
    is about — dockerd's own ``-j RETURN``. A rule scoped by ``-s`` is decidable:
    ``ufw-docker`` writes ``-s 172.16.0.0/12 -j RETURN``, and the devbox subnet
    lives inside that, so every devbox packet returns. Calling that merely
    "conditional" reports a total bypass as a warning nothing alerts on.

    But a rule scoped some other way — Docker Desktop seeds ``-i eth0 -j ACCEPT``
    — cannot be decided from the chain alone, because the devbox bridge's
    interface name is a generated hash this check has no way to learn. Answering
    True there would fire a FAIL on a common healthy shape, and a check that
    cries wolf is one nobody reads. So: unknown, reported as a WARN that names
    the rule.
    """
    if not rule["conditional"]:
        return True
    if not rule["source"] or not subnet:
        return None
    try:
        return ipaddress.ip_network(rule["source"], strict=False).overlaps(
            ipaddress.ip_network(subnet, strict=False)
        )
    except ValueError:
        return None


def check_repos_layout(config: "Config", probe: bool) -> CheckResult:
    """Are this deployment's clones where the daemon now looks for them?

    ``developer.repos_dir`` became a *per-user* root: every consumer that scopes
    a task — the bwrap bind, the native brain's write root, the
    ``DEVELOPER_REPOS_DIR`` manifest variable, the credential scrub — takes
    ``{repos_dir}/{user_id}``. That applies whatever the container backend is,
    because it is what closes cross-user worktree access rather than anything to
    do with containers.

    **Which makes the upgrade a silent failure without this check.** On a host
    whose clones still sit flat under ``repos_dir``, the per-user directory is
    empty, the bind is skipped because its source does not exist, and the
    developer skill is unusable with no error anywhere naming a path. The
    Ansible role performs the move and refuses to guess an owner where there is
    more than one user; this is what says so on a host it did not reach — a
    manual install, a half-finished play, an operator who moved one user's
    clones and not another's.

    Cheap and I/O-only: two ``iterdir`` passes and a marker test per entry, no
    subprocess. That is a statement about the work, not about the import graph,
    and it is not the same thing as being safe on the config-load path — the
    ``from .executor import`` below pulls in most of the package. This check is
    deliberately outside ``config.CONFIG_LOAD_CHECKS`` for that reason.
    """
    name = "developer.repos_layout"
    dev, reason = _dev_gate(config)
    if dev is None:
        return CheckResult(name, SKIP, reason)

    root = Path(dev.repos_dir)
    if not root.is_dir():
        return CheckResult(
            name, SKIP, f"{root} does not exist yet, so there is nothing filed in it",
        )

    from .executor import get_user_repos_dir  # noqa: PLC0415 - executor pulls in most of the package

    users = set(getattr(config, "users", {}) or {})
    stray: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        return CheckResult(
            name, SKIP, f"{root} could not be listed: {exc}",
        )
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink() or entry.name in users:
            continue
        if _holds_a_repository(entry):
            stray.append(entry.name)

    if not stray:
        expected = [u for u in sorted(users) if (root / u).is_dir()]
        return CheckResult(
            name, OK,
            f"{root} holds only per-user roots"
            + (f" ({', '.join(expected)})" if expected else " and is empty"),
        )

    example = get_user_repos_dir(config, sorted(users)[0]) if users else None
    return CheckResult(
        name, FAIL,
        f"{root} still holds repositories outside any user's directory: "
        f"{', '.join(stray[:5])}"
        + (f" and {len(stray) - 5} more" if len(stray) > 5 else ""),
        remedy=(
            "The daemon now looks under {repos_dir}/{user_id}"
            + (f" — for example {example} — " if example else " ")
            + "and binds nothing when that directory does not exist, so the "
            "developer skill cannot see these. Re-run the Ansible role with "
            "`istota_developer_repos_migrate_to` set to the user they belong "
            "to, or move them by hand."
        ),
    )


def _holds_a_repository(entry: "Path", depth: int = 0) -> bool:
    """Is there a git directory at or just below `entry`?

    Two levels, matching the documented layout (`<namespace>/<project>.git`) and
    the migration script's own scan. Bounded rather than a full walk, because
    this runs on the start-up path and `repos_dir` holds working trees.
    """
    markers = ("HEAD", "config", "objects")
    if all((entry / marker).exists() for marker in markers):
        return True
    if (entry / ".git").exists():
        return True
    if depth >= 2:
        return False
    try:
        children = sorted(entry.iterdir())
    except OSError:
        return False
    return any(
        child.is_dir() and not child.is_symlink()
        and _holds_a_repository(child, depth + 1)
        for child in children
    )


# ---------------------------------------------------------------------------
# The development container
# ---------------------------------------------------------------------------

#: The registry name. Four results hang off it, one per property.
CONTAINER_GROUP = "developer.container"

#: How long a transport probe waits on the socket. Shorter than `PROBE_TIMEOUT`
#: because there is a per-user loop behind it and a dead container should be
#: reported quickly rather than made to look like a hang.
CONTAINER_PROBE_TIMEOUT = 5.0

#: How long the *server* gives the one command this check runs (`test -d`).
#: A constant rather than the connect budget: they answer different questions,
#: and a command allowed exactly as long as the client will wait for a read is a
#: race the client loses about half the time.
CONTAINER_EXEC_TIMEOUT = 10.0


def _container_results(names: Iterable[str], status: str, detail: str, remedy: str = "") -> list[CheckResult]:
    """The same answer for several of the group's checks."""
    return [
        CheckResult(f"{CONTAINER_GROUP}.{name}", status, detail, remedy=remedy)
        for name in names
    ]


def _exec_transport_request(
    socket_path: "Path", payload: bytes, timeout: float
) -> tuple[list[dict], str]:
    """Send one request over the exec socket; return its control frames.

    Speaks the wire directly rather than shelling the client: the client's job
    is to be a shim's `exec` target and exit with a command's status, and doctor
    wants the control frames the server sent. `devbox_exec_protocol` is a
    stdlib-only leaf, so importing it here costs nothing.

    Returns ``(frames, "")`` or ``([], reason)``. Never raises — this is a
    start-up path.
    """
    import socket as socket_module  # noqa: PLC0415 - a leaf import on a probe path

    from . import devbox_exec_protocol as proto  # noqa: PLC0415

    sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        try:
            sock.connect(str(socket_path))
        except OSError as exc:
            return [], f"could not connect to {socket_path}: {exc}"

        try:
            sock.sendall(payload)
        except OSError as exc:
            return [], f"could not send to {socket_path}: {exc}"

        buffered = bytearray()

        def _recv_exactly(count: int) -> bytes | None:
            while len(buffered) < count:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    return None
                if not chunk:
                    return None
                buffered.extend(chunk)
            out = bytes(buffered[:count])
            del buffered[:count]
            return out

        # The acknowledgement line comes first, and an error one closes the
        # connection with nothing streamed behind it.
        line = bytearray()
        while b"\n" not in line:
            if buffered:
                line.extend(buffered)
                buffered.clear()
                continue
            try:
                chunk = sock.recv(65536)
            except OSError as exc:
                return [], f"no acknowledgement from {socket_path}: {exc}"
            if not chunk:
                return [], f"{socket_path} closed before acknowledging"
            line.extend(chunk)
        cut = line.index(b"\n")
        buffered[:0] = bytes(line[cut + 1 :])
        try:
            ack = proto.decode_ack(bytes(line[:cut]))
        except Exception as exc:  # noqa: BLE001 - a malformed ack is a finding
            return [], f"{socket_path} sent an unreadable acknowledgement: {exc}"
        if ack.get("status") != "ok":
            return [], (
                f"{socket_path} refused the request: "
                f"{ack.get('code', '?')} {ack.get('message', '')}".strip()
            )
        if not proto.supported_protocol(ack.get("protocol")):
            return [], (
                f"{socket_path} speaks protocol {ack.get('protocol')!r}; this "
                f"daemon speaks {proto.PROTOCOL_VERSION}"
            )

        frames: list[dict] = []
        while True:
            header = _recv_exactly(proto.FRAME_HEADER_BYTES)
            if header is None:
                return frames, f"{socket_path} closed before the terminal frame"
            try:
                stream, length = proto.unpack_header(header)
            except Exception as exc:  # noqa: BLE001
                return frames, f"{socket_path} sent an unreadable frame: {exc}"
            body = _recv_exactly(length) if length else b""
            if body is None:
                return frames, f"{socket_path} closed mid-frame"
            if stream != proto.STREAM_CONTROL:
                continue
            try:
                obj = proto.decode_control(body)
            except Exception as exc:  # noqa: BLE001
                return frames, f"{socket_path} sent an unreadable control frame: {exc}"
            frames.append(obj)
            if proto.is_terminal(obj):
                return frames, ""
    except Exception as exc:  # noqa: BLE001 - never raise from a check
        return [], f"{socket_path}: {type(exc).__name__}: {exc}"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def check_developer_container(config: "Config", probe: bool) -> list[CheckResult]:
    """The five properties of the development container that fail silently.

    Every one of these is a thing an operator learns about from a task failing
    hours later, or — worse — never learns about at all:

    * **backend** — the rendered config and the running daemon disagree, so a
      deployment that was switched on is still building on the host. A unit test
      cannot tell an operator that the host in front of them is the one with the
      problem.
    * **transport** — nothing answers on the socket. This is what replaces the
      per-task setup ping: the same question, asked once by an operator instead
      of once per task by every task, including the tasks that will never run a
      build.
    * **identity** — the container's uid is not the daemon's, or the two sides
      spell the repos root differently. Untreated, that ends in worktrees that
      can never be reaped, and there is no error message anywhere that says so.
    * **uv_cache** — the derived package cache, ``{repos_dir}/{user_id}/
      .package-caches``, is not visible inside the container, so it is not
      covered by the repos mount and every ``uv sync`` pays a full copy instead
      of a hardlink. Merely slow is what nobody investigates.
    * **command_reaper** — the server is running without the child that kills
      its commands when it is killed rather than stopped. Every command is
      still reaped on its own exit path, so nothing fails; what accumulates is
      builds that outlived a server the container's OOM killer picked off, and
      the only place that was ever visible is here.

    Returns five results whatever happens, so a caller can assert on a name
    rather than on a count.
    """
    from . import config as config_module  # noqa: PLC0415 - a cycle at module scope

    names = ("backend", "transport", "identity", "uv_cache", "command_reaper")
    backend = config_module.container_backend(config)
    results = [_container_backend_result(config, backend, config_module)]

    dev = getattr(config, "developer", None)
    if backend != config_module.CONTAINER_BACKEND_DEVBOX:
        # There used to be a fourth pair here worth warning about — the devbox
        # skill offered while `[developer.container] backend = "none"` meant
        # every verb but `reset` refused. The key is retired and the backend is
        # derived from `[devbox] enabled`, so that state can no longer be
        # configured and the detail only has to name whichever input is off.
        if not getattr(dev, "enabled", False):
            why = "the developer skill is off"
        elif not getattr(dev, "repos_dir", ""):
            why = "developer.repos_dir is empty, so there is no containment root"
        else:
            why = "[devbox] enabled is false"
        return results + _container_results(
            names[1:], SKIP,
            f"development commands run on the host: {why}",
        )

    users = sorted(getattr(config, "users", {}) or {})
    if not users:
        return results + _container_results(
            names[1:], SKIP, "no users are configured, so there is no devbox to reach",
        )
    if not probe:
        return results + _container_results(
            names[1:], SKIP,
            "reaching the container means opening its socket (probe disabled)",
        )

    return results + _container_probe_results(config, config_module, users)


def _container_backend_result(config: "Config", backend: str, config_module) -> CheckResult:
    """Does the file on disk derive what the running process believes?

    The daemon holds the config it loaded at start-up. An operator who edited
    ``config.toml`` — or an Ansible run that rendered a new one — has changed
    nothing until the daemon restarts, and the symptom is a feature that was
    switched on and did not switch on.

    Since the ``backend`` key was retired this has to re-derive rather than read
    one value, from the same three inputs :func:`config.devbox_container_backend`
    uses. That is the point of doing it here rather than comparing the key: a
    check that reads a key nobody sets any more reports ``OK`` on every
    deployment forever.

    A file still carrying the retired key is reported whatever the derivation
    says, because it is the one case where an operator's stated intent and the
    running behaviour can differ without any drift being present.
    """
    name = f"{CONTAINER_GROUP}.backend"
    path = getattr(config, "config_path", None)
    if not path:
        return CheckResult(
            name, SKIP,
            f"this process built its config in memory, so there is no rendered "
            f"file to compare backend={backend!r} against",
        )
    try:
        import tomllib  # noqa: PLC0415
    except ModuleNotFoundError:  # pragma: no cover - 3.10 and older
        import tomli as tomllib  # type: ignore[no-redef]  # noqa: PLC0415
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        return CheckResult(
            name, WARN,
            f"{path} could not be read, so backend={backend!r} could not be "
            f"checked against it: {exc}",
            remedy="Fix or re-render the config file the daemon was started with.",
        )

    developer = data.get("developer") or {}
    on_disk = (
        config_module.CONTAINER_BACKEND_DEVBOX
        if (
            developer.get("enabled", False)
            and str(developer.get("repos_dir", "") or "").strip()
            and (data.get("devbox") or {}).get("enabled", False)
        )
        else config_module.CONTAINER_BACKEND_NONE
    )

    retired = (developer.get("container") or {}).get("backend")

    # **Drift is asked first, and the retired key never suppresses it.** The
    # obvious order — report the stale key and return — makes this check dead
    # on exactly the hosts most likely to have one: `config.toml.j2` stopped
    # emitting the key, so an Ansible-managed host loses it on the next deploy,
    # while a hand-maintained `/etc/istota/config.toml` keeps it for ever. On
    # those, a WARN about a key would stand in for a FAIL about a daemon
    # running the wrong thing, permanently and silently, which is the failure
    # class this check exists for.
    if on_disk != backend:
        detail = (
            f"{path} derives backend={on_disk!r} and this process is running "
            f"backend={backend!r}"
        )
        remedy = (
            "Restart the daemon so it loads the rendered config. Until it does, "
            "development commands run wherever the *running* value says."
        )
        if retired is not None:
            detail += (
                f". The file also still sets [developer.container] "
                f"backend={retired!r}, which is retired and ignored — it is not "
                f"the cause of this drift and deleting it will not clear it"
            )
        return CheckResult(name, FAIL, detail, remedy=remedy)

    if retired is not None:
        return CheckResult(
            name, WARN,
            f"{path} still sets [developer.container] backend={retired!r}, which "
            f"is retired and ignored; this deployment derives backend="
            f"{on_disk!r} from [devbox] enabled",
            remedy=(
                "Delete the key. If it was set to 'none' to keep builds on the "
                "host, turn [devbox] enabled off instead — that is now the one "
                "switch, and leaving the stale key in place hides which of the "
                "two an operator meant."
            ),
        )

    return CheckResult(
        name, OK, f"{path} and this process agree: backend={backend!r}",
    )


def _container_probe_results(config: "Config", config_module, users: list[str]) -> list[CheckResult]:
    """Transport, identity and uv_cache, from one connection per user."""
    from . import devbox_exec_protocol as proto  # noqa: PLC0415

    timeout = min(
        float(
            getattr(
                getattr(getattr(config, "developer", None), "container", None),
                "connect_timeout_seconds",
                CONTAINER_PROBE_TIMEOUT,
            )
            or CONTAINER_PROBE_TIMEOUT
        ),
        PROBE_TIMEOUT,
    )

    # Not `security.sandbox_cache_dir`. That key stopped being the cache root:
    # the cache is derived at `{repos_dir}/{user_id}/.package-caches`, and the
    # key is read only where `repos_dir` is unset. Asking after it here would
    # warn on exactly the deployments that are configured correctly, since the
    # Ansible default for it is blank.
    repos_root_cfg = getattr(getattr(config, "developer", None), "repos_dir", "")
    from .executor import SANDBOX_CACHE_ROOT_NAME  # noqa: PLC0415 - executor pulls in most of the package

    reachable: list[str] = []
    transport_bad: list[str] = []
    identity_bad: list[str] = []
    identity_ok: list[str] = []
    cache_bad: list[str] = []
    cache_ok: list[str] = []
    reaper_bad: list[str] = []
    reaper_ok: list[str] = []
    without_a_devbox: list[str] = []

    for user_id in users:
        socket_path = config_module.exec_socket_path(config, user_id)
        if socket_path is None:
            transport_bad.append(f"{user_id}: no socket path could be composed")
            continue
        # **Which users have a devbox is not in the daemon's config.** The list
        # lives in Ansible (`istota_devbox_users`) and reaches neither
        # `config.users` nor `DevboxConfig`, so iterating every configured user
        # would FAIL this check permanently on the reference shape — one admin
        # with a container, several other users without — and `check_doctor`'s
        # hourly sweep would alert every admin on the transition. A check that
        # cries wolf is one nobody reads.
        #
        # The per-user socket *directory* is the discriminator available here:
        # the role creates it only for `istota_devbox_users`, both in the play
        # and in the tmpfiles snippet that recreates it at boot. Its absence is
        # "no devbox for this user", not "the devbox is broken".
        if not socket_path.parent.is_dir():
            without_a_devbox.append(user_id)
            continue
        frames, error = _exec_transport_request(
            socket_path, proto.encode_ping_request(), timeout
        )
        if error:
            transport_bad.append(f"{user_id}: {error}")
            continue
        if not any(frame.get("pong") is True for frame in frames):
            transport_bad.append(f"{user_id}: {socket_path} answered without a pong")
            continue
        reachable.append(user_id)

        frames, error = _exec_transport_request(
            socket_path, proto.encode_stat_request(), timeout
        )
        stat = next((f for f in frames if "uid" in f), None)
        if stat is None:
            identity_bad.append(
                f"{user_id}: {error or 'the server sent no stat reply'}"
            )
        else:
            findings = _identity_findings(config, config_module, user_id, stat)
            if findings:
                identity_bad.extend(findings)
            else:
                identity_ok.append(user_id)
            # A server too old to answer says nothing, which is not the same as
            # answering `false`. Only an explicit `false` is a finding here.
            if stat.get("reaper") is False:
                reaper_bad.append(user_id)
            elif stat.get("reaper") is True:
                reaper_ok.append(user_id)

        if not repos_root_cfg:
            continue
        cache_dir = Path(repos_root_cfg) / user_id / SANDBOX_CACHE_ROOT_NAME
        frames, error = _exec_transport_request(
            socket_path,
            # A *server-side* budget, deliberately not `timeout`. That one is
            # the connect budget, which `_parse_container_block` floors at 0.1 —
            # an operator who sets it small for fast failure would otherwise get
            # a 0.1s kill budget on `test -d` and be told their cache mount is
            # missing. The two numbers answer different questions and one of
            # them is not the operator's to set.
            proto.encode_exec_request(
                argv=["test", "-d", str(cache_dir)],
                cwd=None,
                stdin=False,
                timeout=CONTAINER_EXEC_TIMEOUT,
            ),
            timeout,
        )
        terminal = next((f for f in frames if proto.is_terminal(f)), None)
        if error and terminal is None:
            cache_bad.append(f"{user_id}: {error}")
        elif terminal is None or terminal.get("exit_code") != 0:
            cache_bad.append(f"{user_id}: {cache_dir} is not a directory in the container")
        else:
            cache_ok.append(user_id)

    results = [_transport_result(reachable, transport_bad, without_a_devbox)]
    results.append(_identity_result(identity_ok, identity_bad, reachable))
    results.append(_uv_cache_result(repos_root_cfg, cache_ok, cache_bad, reachable))
    results.append(_reaper_result(reaper_ok, reaper_bad, reachable))
    return results


def _identity_findings(config: "Config", config_module, user_id: str, stat: dict) -> list[str]:
    """What the container disagrees with the daemon about, if anything."""
    findings: list[str] = []
    daemon_uid = os.getuid() if hasattr(os, "getuid") else None
    container_uid = stat.get("uid")
    if daemon_uid is not None and container_uid != daemon_uid:
        findings.append(
            f"{user_id}: the container's server runs as uid {container_uid} and "
            f"this daemon is uid {daemon_uid}"
        )
    from .executor import get_user_repos_dir  # noqa: PLC0415 - executor pulls in most of the package

    expected = get_user_repos_dir(config, user_id)
    reported = stat.get("repos_root")
    if expected is not None and reported != str(expected):
        findings.append(
            f"{user_id}: the container's repos root is {reported!r} and this "
            f"daemon's is {str(expected)!r}"
        )
    return findings


def _transport_result(
    reachable: list[str], bad: list[str], without_a_devbox: list[str]
) -> CheckResult:
    name = f"{CONTAINER_GROUP}.transport"
    if not bad and not reachable:
        return CheckResult(
            name, SKIP,
            f"{len(without_a_devbox)} configured user(s) have no devbox socket "
            f"directory, so none of them routes development work into a container",
        )
    if bad:
        return CheckResult(
            name, FAIL,
            "the exec transport did not answer for " + "; ".join(bad),
            remedy=(
                "Check the devbox container is up and its exec server is running "
                "(`docker logs devbox-<user>`), that ISTOTA_EXEC_SOCKET and "
                "ISTOTA_EXEC_REPOS_ROOT are set on the service, and that the "
                "socket directory is mounted into it."
            ),
        )
    detail = f"the exec transport answered a ping for {len(reachable)} devbox user(s)"
    if without_a_devbox:
        detail += f"; {len(without_a_devbox)} configured user(s) have no devbox"
    return CheckResult(name, OK, detail)


def _identity_result(ok: list[str], bad: list[str], reachable: list[str]) -> CheckResult:
    name = f"{CONTAINER_GROUP}.identity"
    if bad:
        return CheckResult(
            name, FAIL,
            "the daemon and the container do not agree: " + "; ".join(bad),
            remedy=(
                "Rebuild the devbox image with DEV_UID/DEV_GID set to the "
                "daemon's own uid and gid, and recreate the container. Until "
                "they match, every worktree that runs a build becomes "
                "unreapable and nothing else reports it."
            ),
        )
    if not reachable:
        return CheckResult(name, SKIP, "no container answered, so nothing was compared")
    return CheckResult(
        name, OK,
        f"uid and repos root agree for {len(ok)} devbox user(s)",
    )


def _reaper_result(
    ok: list[str], bad: list[str], reachable: list[str]
) -> CheckResult:
    """Is anything behind the exec server if it is killed rather than stopped?

    WARN rather than FAIL: the transport works, commands run, and every one of
    them is still killed on its own exit path. What is missing is the backstop
    for the one death that skips those paths — so the cost is builds that
    outlive a server the container's OOM killer picked off, which is a leak
    rather than an outage.

    A server too old to report the field is not a finding. It reports SKIP by
    landing in neither list, the same way an unreachable one does.
    """
    name = f"{CONTAINER_GROUP}.command_reaper"
    if bad:
        return CheckResult(
            name, WARN,
            "the exec server has no command reaper for " + ", ".join(bad),
            remedy=(
                "Grep the container's log for 'reaper' (`docker logs "
                "devbox-<user>`) and restart the container. It either never "
                "started ('cannot start the reaper', 'cannot create the reaper "
                "pipe') or died later ('the reaper is gone', 'the reaper "
                "exited'), and the second is the common one. Until it is back, "
                "a server killed rather than stopped leaves every command it "
                "was running alive in the container."
            ),
        )
    if not ok:
        return CheckResult(
            name, SKIP,
            "no container reported whether it has a command reaper"
            if reachable
            else "no container answered, so nothing was asked",
        )
    return CheckResult(
        name, OK, f"the command reaper is running for {len(ok)} devbox user(s)",
    )


def _uv_cache_result(
    repos_root_cfg: str, ok: list[str], bad: list[str], reachable: list[str]
) -> CheckResult:
    """Is the derived package cache visible inside the container?

    The question this asks changed, and the old one would now be actively
    misleading. It used to be "did the operator set `security.sandbox_cache_dir`
    and is its bind present" — but the cache is derived at
    `{repos_dir}/{user_id}/.package-caches` now, that key is read only where
    `repos_dir` is unset, and its Ansible default is blank. Asking after the key
    would WARN on every correctly configured deployment.

    What is worth checking is the property, not the setting: the cache lives
    inside the repos subtree the container already mounts, so one mount covers
    cache and venv and `link(2)` hardlinks rather than copying. If that
    directory is missing from the container, the mount is wrong in a way that is
    slow rather than broken — which is exactly the failure nobody investigates
    on their own.
    """
    name = f"{CONTAINER_GROUP}.uv_cache"
    if not repos_root_cfg:
        return CheckResult(
            name, SKIP,
            "developer.repos_dir is unset, so there is no per-user repos subtree "
            "and no derived package cache to look for",
        )
    if bad:
        return CheckResult(
            name, WARN,
            "the derived package cache is not visible in the container for "
            + "; ".join(bad),
            remedy=(
                "The cache is {developer.repos_dir}/{user}/.package-caches and "
                "sits inside the repos bind, so a missing directory means that "
                "bind is wrong or the container predates it. Re-run the role and "
                "recreate the container. Slow rather than broken — uv falls back "
                "to copying every wheel — which is why nothing else will tell you."
            ),
        )
    if not reachable:
        return CheckResult(name, SKIP, "no container answered, so nothing was checked")
    return CheckResult(
        name, OK,
        f"the derived package cache is visible for {len(ok)} devbox user(s)",
    )


def check_devbox_netfilter(config: "Config", probe: bool) -> CheckResult:
    """Read the live ``DOCKER-USER`` chain and report anything shadowing our rules.

    This is the only witness over the devbox network boundary that looks at a
    running host. ``tests/test_ansible_devbox_iptables.py`` proves the role asks
    for the right rules in the right position; it cannot see what an operator,
    a host-firewall integration or a different Docker version put in the chain
    afterwards. ISSUE-295 is exactly that gap: four correct rules behind a
    ``-j RETURN`` are never evaluated, and ``iptables -S`` renders them
    identically to four that work.

    **Reading the chain needs root and the daemon does not run as root**, so
    under the scheduler unit this check reports SKIP and says so in as many
    words. That is a real limitation rather than a quiet one: the detail names
    the command to run by hand, because a SKIP that reads as "not applicable
    here" when it means "never runs under this unit" would be the same shape of
    silence the check exists to break.
    """
    name = "security.devbox_netfilter"
    # This was a disjunction over two switches, guarding the pair where
    # `backend = devbox` with `devbox.enabled = false` put every build in the
    # estate inside a container whose egress filtering nothing checked. The
    # backend is derived from `[devbox] enabled` now, so it can no longer be on
    # while this is off and the second arm could never fire. One switch, and it
    # is the one the Ansible role gates the rules themselves on.
    devbox_on = getattr(getattr(config, "devbox", None), "enabled", False)
    if not devbox_on:
        return CheckResult(
            name, SKIP,
            "devbox is disabled ([devbox] enabled); the role adds no rules",
        )
    if not probe:
        return CheckResult(
            name,
            SKIP,
            f"the live {DEVBOX_CHAIN} chain cannot be read without spawning iptables "
            "(probe disabled)",
        )

    result = _run(["iptables", "-S", DEVBOX_CHAIN])
    if result is None:
        return CheckResult(
            name,
            SKIP,
            f"iptables could not be run, so the {DEVBOX_CHAIN} chain was not read",
        )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        lowered = stderr.lower()
        if "permission denied" in lowered or "must be root" in lowered:
            return CheckResult(
                name,
                SKIP,
                f"reading {DEVBOX_CHAIN} needs root, and this process is not root — "
                "under the scheduler unit this check never runs, so verify the chain "
                "by hand with `sudo istota doctor --only security.devbox_netfilter`",
            )
        if "no chain" in lowered:
            return CheckResult(
                name,
                FAIL,
                f"the {DEVBOX_CHAIN} chain does not exist, so no devbox rule is present",
                remedy=(
                    "Start dockerd, which creates DOCKER-USER, then run "
                    "`systemctl start istota-devbox-iptables`."
                ),
            )
        return CheckResult(
            name,
            SKIP,
            f"`iptables -S {DEVBOX_CHAIN}` exited {result.returncode} and was not read",
        )

    # The boot script is the oracle for what this host should block and for the
    # subnet the rules are scoped to. Read before anything is judged: without it
    # there is no way to tell "configured to block nothing" from "should be
    # blocking four things and is blocking none".
    try:
        script = DEVBOX_BOOT_SCRIPT.read_text()
    except OSError:
        script = ""
    expected = parse_devbox_boot_script(script)
    subnet = parse_devbox_boot_subnet(script)

    parsed = [parse_iptables_rule(line, DEVBOX_CHAIN) for line in result.stdout.splitlines()]
    unreadable = sum(
        1
        for line, rule in zip(result.stdout.splitlines(), parsed)
        if rule is None and line.strip().startswith(f"-A {DEVBOX_CHAIN}")
    )
    rules = [rule for rule in parsed if rule is not None]

    marked = [rule for rule in rules if _DEVBOX_RULE_MARK in rule["comment"]]
    # Carrying our comment is not enough to be one of our rules. A rule with our
    # marker and a terminal target would otherwise be counted as ours, excluded
    # from the shadowing scan, and reported as part of a healthy boundary while
    # being the thing that breaks it.
    ours = [
        index
        for index, rule in enumerate(rules)
        if _DEVBOX_RULE_MARK in rule["comment"] and rule["target"] == "DROP"
    ]
    impostors = [r for r in marked if r["target"] != "DROP"]
    if impostors:
        return CheckResult(
            name,
            FAIL,
            f"{len(impostors)} rule(s) in {DEVBOX_CHAIN} carry the devbox comment but "
            f"jump to {impostors[0]['target']}, not DROP",
            remedy=(
                f"Read `iptables -S {DEVBOX_CHAIN}`; a rule wearing the devbox comment "
                "with another target did not come from this role. Remove it and "
                "re-run `systemctl restart istota-devbox-iptables`."
            ),
        )

    if not ours:
        if not script:
            return CheckResult(
                name,
                SKIP,
                f"{DEVBOX_CHAIN} carries no devbox rules and no boot script is "
                "installed, so there is nothing to say what this host should block",
            )
        if not expected:
            return CheckResult(
                name,
                OK,
                "the devbox is configured to block nothing "
                "(istota_devbox_block_metadata and istota_devbox_block_rfc1918 are "
                f"both off) and {DEVBOX_CHAIN} carries no devbox rules, as expected",
            )
        return CheckResult(
            name,
            FAIL,
            f"{DEVBOX_CHAIN} carries none of the {len(expected)} devbox DROP rules "
            f"the installed boot script blocks ({len(rules)} other rule(s) present)",
            remedy=(
                "Re-run the Ansible role, or `systemctl start "
                "istota-devbox-iptables` to re-apply them now."
            ),
        )

    # Everything ahead of the *last* of our rules, not the first: a terminal rule
    # interleaved among them leaves the ones behind it unreachable, and scanning
    # only up to the first would report that chain as healthy.
    preceding = [r for r in rules[: ours[-1]] if _DEVBOX_RULE_MARK not in r["comment"]]
    terminal = [r for r in preceding if _is_terminal(r)]
    covering = [r for r in terminal if _covers(r, subnet) is True]
    undecidable = [r for r in terminal if _covers(r, subnet) is None]
    if covering:
        first = covering[0]
        how = "a goto to" if first["goto"] else "a jump to"
        scope = f" scoped to {first['source']}" if first["source"] else " matching every packet"
        return CheckResult(
            name,
            FAIL,
            f"{len(covering)} rule(s) ahead of the devbox DROP rules in "
            f"{DEVBOX_CHAIN} end the chain for devbox traffic — the first is "
            f"{how} {first['target']}{scope}",
            remedy=(
                f"Remove that rule from {DEVBOX_CHAIN}, or re-insert the devbox rules "
                "in front of it with `systemctl restart istota-devbox-iptables`."
            ),
        )
    if undecidable:
        first = undecidable[0]
        return CheckResult(
            name,
            WARN,
            f"{len(undecidable)} rule(s) ahead of the devbox DROP rules in "
            f"{DEVBOX_CHAIN} end the chain for traffic they match, and whether that "
            f"includes the devbox cannot be told from the chain — the first jumps "
            f"to {first['target']}: {first['raw']}",
            remedy=(
                f"Read `iptables -S {DEVBOX_CHAIN}` and confirm that rule cannot "
                "match devbox traffic; if it can, move the devbox rules in front of it."
            ),
        )
    if terminal:
        # Terminal, but decidably not about us — a rule scoped to a range the
        # devbox subnet does not overlap. Worth neither an alert nor silence.
        first = terminal[0]
        return CheckResult(
            name,
            OK,
            f"{len(ours)} devbox IPv4 DROP rule(s) are in {DEVBOX_CHAIN}; "
            f"{len(terminal)} rule(s) ahead of them end the chain only for traffic "
            f"outside the devbox subnet (the first is scoped to {first['source']})",
        )
    if unreadable:
        return CheckResult(
            name,
            WARN,
            f"{unreadable} rule(s) in {DEVBOX_CHAIN} could not be parsed, so whether "
            "they shadow the devbox rules is unknown",
            remedy=f"Read `iptables -S {DEVBOX_CHAIN}` by hand and check what precedes the devbox rules.",
        )

    if expected:
        present = {rules[i]["destination"] for i in ours}
        missing = sorted(
            dest
            for dest in expected
            if not _same_network(dest, present)
        )
        if missing:
            return CheckResult(
                name,
                WARN,
                f"{len(present)} of {len(expected)} devbox DROP rules are in "
                f"{DEVBOX_CHAIN}; missing: {', '.join(missing)}",
                remedy=(
                    "Re-apply them with `systemctl restart istota-devbox-iptables` "
                    "and check the unit's journal — `set -e` means it stops at the "
                    "first rule the kernel rejects."
                ),
            )

    return CheckResult(
        name,
        OK,
        f"{len(ours)} devbox IPv4 DROP rule(s) are in {DEVBOX_CHAIN} with nothing "
        "ahead of them that ends the chain for devbox traffic",
    )


def _same_network(dest: str, present: set[str]) -> bool:
    """Is `dest` one of `present`, comparing as networks rather than as strings?

    The kernel renders a bare address with its prefix length, so a boot script
    naming `169.254.169.254` and a chain carrying `169.254.169.254/32` are the
    same rule and must not be reported as a missing one.
    """
    if dest in present:
        return True
    try:
        wanted = ipaddress.ip_network(dest, strict=False)
    except ValueError:
        return False
    for candidate in present:
        try:
            if ipaddress.ip_network(candidate, strict=False) == wanted:
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Per-skill user overlays
# ---------------------------------------------------------------------------

#: How many offending files the detail names before it stops. A check's detail
#: is one line, and a tree with fifty broken overlays is one problem to go and
#: look at rather than fifty to read here.
_OVERLAY_REPORT_LIMIT = 5

#: How much of one filename the detail carries. A filename here is text the
#: *model* wrote — the directory is bound read-write into that user's sandbox —
#: and a name may be 255 bytes of anything but ``/`` and NUL. The detail is
#: printed to a terminal and rendered into the admin dashboard, so the count
#: limit above bounds the wrong axis on its own.
_OVERLAY_NAME_CHARS = 64

#: Why an overlay will never be loaded, as opposed to loading with something
#: worth saying about it. A denylisted name and a body past the cap are each a
#: misfiling that reaches no prompt, that nothing else would ever mention, and
#: that a person fixes by renaming, shrinking or removing the file.
#:
#: ``unknown_skill`` is deliberately **not** here, and is decided per file by
#: ``_overlay_near_miss`` instead. It is the one reason an ordinary task can
#: produce with a single ``touch``, because the directory is inside the tree
#: ``build_bwrap_cmd`` binds read-write into that user's sandbox — so a flat
#: FAIL on it is a deployment-scope alert any task can pin red and an operator
#: learns to skip past, which costs the signal the check was built to give
#: (ISSUE-340). A name one or two edits from a real skill is still FAIL, because
#: that is a typo and a typo is the case the check exists for.
_OVERLAY_FATAL_REASONS = frozenset({"denylisted", "over_cap"})

#: Reported against a user's overlay *directory* rather than against a file in
#: it: the directory resolves inside that user's own tree, so
#: `contained_overlay_dir` accepts it, but a component of the path is a symlink
#: and `open_overlay_dir` refuses to follow one (ISSUE-344). Every reader takes
#: the strict answer, so none of that user's overlays reaches a prompt. Not in
#: `_OVERLAY_FATAL_REASONS`, which is matched against an `inspect_overlay`
#: reason and never sees this one — it is appended to `dir_findings` directly,
#: which reports at WARN rather than FAIL (a task can produce one at will).
#: Names what was established rather than a cause: `open_overlay_dir` collapses
#: every `OSError` to a refusal, so an unreadable `config`, a regular file at
#: `skills` and an I/O error on the mount all arrive here too.
_OVERLAY_DIR_UNOPENABLE = "dir_not_openable"

#: The other directory-level refusal, from `contained_overlay_dir` rather than
#: from the descriptor walk: the path resolves *outside* the user's own tree.
#: Reported rather than skipped, because nothing else looks at this directory —
#: the loader degrades to no overlay, and the read verbs only ever run for one
#: user who asked. Skipping it left the most clear-cut plant of the set as the
#: only one nothing anywhere reported.
_OVERLAY_DIR_OUTSIDE_TREE = "dir_outside_user_tree"

#: Edits allowed between a filename and a real skill name before the two stop
#: being a plausible typo of each other, keyed on whether the *filename* is
#: short. Two edits out of four characters is most of the name, so at that
#: length the budget is what turns every scratch file into an alert; the same
#: two out of nine is a slip.
#: `_loader.OVERLAY_UNKNOWN_SKILL`, restated so the label helpers need no
#: import of a module whose graph is heavy; the two are pinned equal by
#: `tests/test_doctor.py`.
_UNKNOWN = "unknown_skill"

_OVERLAY_TYPO_SHORT_NAME_CHARS = 5
_OVERLAY_TYPO_BUDGET_SHORT = 1
_OVERLAY_TYPO_BUDGET_LONG = 2


#: How much of the *note* half of a label the detail carries. Larger than the
#: filename budget because the two are sized for different things: 64 is sized
#: against a 255-byte filename, while a note is a fixed sentence plus a skill
#: name, and cutting at 64 took the name off ``did you mean
#: a_very_long_operator_defined_skill...`` — marked as truncated, but no longer
#: something an operator can copy.
_OVERLAY_NOTE_CHARS = 120


def _overlay_safe_text(text: str, limit: int = _OVERLAY_NAME_CHARS) -> str:
    """One field of a reportable label, with the control characters taken out.

    A newline would forge a second line in a one-line detail, and an ANSI
    escape would repaint an operator's terminal. Neither is hypothetical: a
    filename is chosen by whatever wrote the file, and that is a sandboxed task
    as often as it is a person. The note is held to the same rule because it
    now carries a skill name read off disk rather than only literals, and so is
    the user id, which is a directory name read off the same mount.
    """
    safe = "".join(ch if ch.isprintable() else "?" for ch in text)
    if len(safe) > limit:
        safe = safe[:limit] + "..."
    return safe


def _overlay_label(user_id: str, name: str, note: str) -> str:
    """One reportable filename and what is wrong with it, every field sanitized."""
    return (
        f"{_overlay_safe_text(user_id)}/{_overlay_safe_text(name)}"
        f" ({_overlay_safe_text(note, _OVERLAY_NOTE_CHARS)})"
    )


def _edit_distance(a: str, b: str, budget: int) -> int | None:
    """Levenshtein distance between ``a`` and ``b``, or None if it exceeds ``budget``.

    Bounded rather than exact because the only question asked of it is "within
    ``budget``?", and the bound is what keeps a directory of long junk names
    from costing a full matrix each. Two rows, and the row minimum is a lower
    bound on every distance reachable from it, so a row that is already over
    budget can stop.
    """
    if abs(len(a) - len(b)) > budget:
        return None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (0 if ca == cb else 1),
                )
            )
        if min(current) > budget:
            return None
        previous = current
    return previous[-1] if previous[-1] <= budget else None


#: A suffix that marks a file as *derived from* another rather than named for a
#: skill: an editor backup, a numbered copy, a hand-made snapshot. Matched only
#: against what is left when it is stripped, and only when that remainder is an
#: exact skill name — so this reads ``notes~``, ``notes2`` and ``notes.bak`` as
#: copies of the ``notes`` overlay and leaves every other name to the distance
#: test. Without it those are all one or two edits from a real name and so all
#: FAIL, which is the largest hole in ISSUE-340's fix: ``<skill>2``,
#: ``<skill>-1`` and ``<skill>~`` are exactly what a task leaves behind, and a
#: person who copies an overlay has not misspelled anything. The ``v`` of a
#: version marker is only recognised after a separator: without that, ``kv2``
#: strips to ``k`` and the ``kv`` skill behind it is never seen.
_OVERLAY_DERIVED_SUFFIX_RE = re.compile(
    r"(?:~|[ _.\-]?(?:copy|backup|bak|tmp|temp|orig|old|new|save)"
    r"|[ _.\-]v\d+|[ _.\-]?\d+)$",
    re.IGNORECASE,
)

#: How many stacked suffixes to strip — ``notes.bak2`` is two. Bounded because
#: the stem is a model-chosen filename and the loop is over its own output.
_OVERLAY_MAX_DERIVED_SUFFIXES = 3


def _derived_copy_of(stem: str, known_skills: Collection[str]) -> str | None:
    """The skill ``stem`` is a copy of, or None when it is not a copy of one.

    Returns the name rather than a bool so the report can say *which* file it
    was copied from: an operator reading ``unknown_skill`` against ``notes2.md``
    is otherwise told, by the WARN remedy, that the name is "not close enough
    to a skill to be a typo" — which is arithmetically false, since it is one
    edit away. It is here because it is a copy, and the label now says so.


    ``notes.bak`` is not a misspelling of ``notes``; it is a copy of it, and
    whoever made it did not believe it was live. The distance test cannot tell
    the two apart — a suffix is one or two edits either way — so this runs
    first and takes the FAIL back to a WARN. The file is still reported: what
    changes is which status it holds.

    Only an *exact* remainder counts. ``develper2`` strips to ``develper``,
    which is not a skill, so it falls through and is reported as the typo it
    is.
    """
    remaining = stem
    for _ in range(_OVERLAY_MAX_DERIVED_SUFFIXES):
        match = _OVERLAY_DERIVED_SUFFIX_RE.search(remaining)
        if match is None or match.start() == 0:
            return None
        remaining = remaining[: match.start()]
        if remaining in known_skills:
            return remaining
    return None


#: Split a filename into the words a person would read in it. A skill whose
#: every word appears is *named* by that filename even when the edit distance
#: is large, which is the direction distance is blind in: the more deliberately
#: someone decorates a name — ``developer.local``, ``01-developer``,
#: ``developer-overlay`` — the further it gets from ``developer`` and the
#: quieter a distance-only rule becomes, while the author's belief that the
#: file was live only gets more obvious.
_OVERLAY_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")

#: Below this a skill name is too short to carry meaning as a token — ``kv``
#: would match any filename with a ``kv`` word in it.
_OVERLAY_MIN_TOKEN_CHARS = 4


def _overlay_denylist() -> frozenset[str]:
    from .skills._loader import OVERLAY_DENYLIST  # noqa: PLC0415 - heavy import graph

    return OVERLAY_DENYLIST


def _denylist_key(name: str) -> str:
    from .skills._loader import _denylist_key as key  # noqa: PLC0415 - heavy import graph

    return key(name)


def _names_a_skill(stem: str, known_skills: Collection[str]) -> str | None:
    """The skill ``stem`` names outright, or None.

    Every word of the skill has to appear as a word of the filename, so
    ``developer.local`` and ``01-developer`` name ``developer`` while
    ``develop`` does not name anything. The longest match wins, so
    ``sensitive_actions_old`` reports the two-word skill rather than a
    one-word skill that happens to share a token.

    The false positive is real and accepted: ``release-notes.md`` names
    ``notes`` and will FAIL. It is also a file sitting in the overlay directory
    that reaches no prompt, so the report is not wrong about it — only louder
    than that particular name deserves.
    """
    tokens = {t.casefold() for t in _OVERLAY_TOKEN_SPLIT_RE.split(stem) if t}
    if not tokens:
        return None
    best: str | None = None
    for skill in sorted(known_skills):
        if len(skill) < _OVERLAY_MIN_TOKEN_CHARS:
            continue
        words = [w for w in _OVERLAY_TOKEN_SPLIT_RE.split(skill) if w]
        if words and all(w.casefold() in tokens for w in words):
            if best is None or len(skill) > len(best):
                best = skill
    return best


def _classify_unknown_overlay(
    stem: str, known_skills: Collection[str]
) -> tuple[bool, str]:
    """``(fails, note)`` for a filename that is not a known skill name.

    One place, because the severity and the words shown to the operator have to
    agree: every earlier version of this had a label that stated a reason the
    branch above it had not actually used.
    """
    copy_of = _derived_copy_of(stem, known_skills)
    if copy_of is not None:
        return False, f"{_UNKNOWN}, a copy of {copy_of}.md"
    near = _overlay_near_miss(stem, known_skills)
    if near is not None:
        if _denylist_key(near) in _overlay_denylist():
            # Suggesting a rename here would walk the operator straight into
            # the next FAIL: that name takes no overlay, and the write path
            # refuses it too.
            return True, f"{_UNKNOWN}, closest is {near}, which takes no overlay"
        return True, f"{_UNKNOWN}, did you mean {near}?"
    named = _names_a_skill(stem, known_skills)
    if named is not None:
        return True, f"{_UNKNOWN}, names the {named} skill but is not {named}.md"
    return False, _UNKNOWN


def _overlay_near_miss(stem: str, known_skills: Collection[str]) -> str | None:
    """The skill ``stem`` was probably meant to be, or None if nothing was.

    This is the whole of what separates ``develper.md`` — a customization its
    author believes is live and that nothing but ``doctor`` would ever mention
    — from ``zzz.md``, which is a scratch file. The first is worth a FAIL and
    the second is not.

    A dropped plural is on the FAIL side by design: ``note.md`` for the
    ``notes`` skill, ``task.md`` for ``tasks``. Those read as scratch names,
    and they are also the most common way there is to misspell a skill — a
    file whose rules reach no prompt either way. ``_derived_copy_of`` carves
    off the class that is genuinely not a misspelling.

    Compared case-insensitively, because ``Developer.md`` is a misfiling by the
    same argument. The whole index is compared against,
    denylisted names included: a misspelling of ``sensitive_actions`` is still
    a file somebody wrote rules into believing they would load.

    Ties are broken by name so two runs over the same directory report the
    same suggestion. ``known_skills`` is the ``load_skill_index`` mapping, whose
    order is the order three discovery layers happened to produce, so iterating
    it is deterministic within a process and not across deployments — which is
    the same problem as an unordered set for anything an operator compares.

    A candidate is skipped only on an **exact** string match, not on a
    casefolded distance of zero: ``Developer.md`` folds onto ``developer`` at
    distance zero and is precisely the misfiling worth reporting, since the
    index that rejected it is case-sensitive. An exact match means the caller
    is asking about a name the index never rejected, which is not a typo of
    anything.
    """
    if not stem:
        return None
    budget = (
        _OVERLAY_TYPO_BUDGET_SHORT
        if len(stem) < _OVERLAY_TYPO_SHORT_NAME_CHARS
        else _OVERLAY_TYPO_BUDGET_LONG
    )
    lowered = stem.casefold()
    best: tuple[int, str] | None = None
    for name in sorted(known_skills):
        if name == stem:
            continue
        distance = _edit_distance(lowered, name.casefold(), budget)
        if distance is None:
            continue
        if best is None or distance < best[0]:
            best = (distance, name)
    return None if best is None else best[1]


def _overlay_dirs(mount: Path, bot_dir: str) -> list[tuple[str, Path, Path | None]]:
    """`(user_id, user dir, overlay dir)` for every user under the mount with one.

    A `None` overlay dir means the path resolved outside that user's own tree —
    reported by the caller rather than dropped, since nothing else looks at it.

    Walked rather than taken from ``config.users`` because a user whose config
    block was removed still has a tree on disk, and a file left there is exactly
    the kind of thing nobody would otherwise be told about.

    Hardened the way every other reader of this tree is, and for the same
    reason: ``{mount}/Users/{user_id}`` is bound **read-write** into that user's
    own sandbox, so every path component under it is model-plantable. Three
    consequences here specifically, because this walk crosses *all* users where
    the loader and the CLI each stay inside one:

    - a user entry is required to be a real directory rather than a symlink to
      one, so a link planted at another user's name cannot make this walk
      descend somewhere else and report a file against the wrong user;
    - the overlay directory is resolved and required to stay under its own
      user's tree, since ``config`` and ``skills`` are both ordinary entries a
      task can replace with a link. That rule is
      ``_loader.contained_overlay_dir``, shared with the loader, the memory CLI
      and the search reindex, and the **resolved** path is what is returned;
      the caller then opens it with ``_loader.open_overlay_dir`` and walks the
      descriptor, since the resolved path alone still leaves the check and the
      reads separated by a window in which the link can be swapped
      (ISSUE-344). The user directory comes back too, because that is the root
      the descriptor walk starts from;
    - nothing here opens a file. ``scandir`` stats, and the read that follows
      is ``inspect_overlay``'s, which refuses a FIFO — ``doctor`` runs on the
      daemon's start-up path, where a blocking ``open(2)`` has no timeout over
      it at all.
    """
    from .skills._loader import contained_overlay_dir  # noqa: PLC0415 - heavy import graph

    users_root = mount / "Users"
    try:
        entries = sorted(os.scandir(users_root), key=lambda e: e.name)
    except OSError:
        return []

    found: list[tuple[str, Path, Path]] = []
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        user_dir = Path(entry.path)
        resolved = contained_overlay_dir(user_dir / bot_dir / "config" / "skills", user_dir)
        if resolved is None:
            # Resolves outside the user's own tree. Reported rather than
            # skipped: nothing else reports this directory, so a link pointing
            # clean out of the mount — the most clear-cut plant of the set —
            # was the one case nothing anywhere named. `None` in the third slot
            # is what the caller reads as "refused before it was opened".
            found.append((entry.name, user_dir, None))
            continue
        try:
            if not resolved.is_dir():
                continue
        except OSError:
            continue
        found.append((entry.name, user_dir, resolved))
    return found


def check_skill_overlays(config: "Config", probe: bool) -> CheckResult:
    """Every per-skill user overlay on the mount either binds, or is named here.

    An overlay is ``config/skills/<skill-name>.md`` appended to that skill's
    body whenever the skill loads. Nothing else in the system ever says a word
    about one: a file named for a skill that does not exist is silently never
    read, and so is one for a skill that takes no overlay, and so is one past
    the loading cap. Each looks configured from ``ls``, and the user's rule is
    simply absent from every prompt with nothing anywhere reporting it. That is
    the same failure class as the missing watermark and the devbox ``command not
    found`` — the defect is the absence of a signal rather than the presence of
    a bug.

    No process, no read past the cap, and ``probe`` is unused: this is a
    ``scandir`` per user plus one bounded read per overlay file.

    The gates are ``_loader.inspect_overlay``, shared with the ``memory
    skills`` inventory, so the two surfaces cannot disagree about which files
    are live. Two differences are deliberate:

    - **a disabled skill is not reported.** Its overlay binds again the moment
      the operator switches the skill back on, so it is a fact about the
      configuration rather than a defect in the file. The inventory does say
      so, because a user asking "is my customization live?" wants that answer;
      an operator sweeping for problems does not.
    - **no overlay content is quoted.** This runs across every user's tree, and
      the same result is rendered into the admin dashboard, so a filename is
      the most that may leave one user's directory. A filename is itself text
      the model wrote, so it goes through ``_overlay_label`` rather than
      straight into the detail.

    **FAIL is reserved for a misfiling.** A name on the denylist and a body
    past the loading cap are each a file a person can fix by renaming,
    shrinking or removing it, and each is the case the check exists for.
    Everything else ``inspect_overlay`` can report — an empty file, one that is
    not UTF-8, one this process was refused — also loads as nothing, but a
    transient ``EACCES`` on one user's file is not a broken deployment and must
    not turn the one status that alerts red.

    **A name that is not a skill splits, and the split is the whole of
    ISSUE-340.** This directory is inside the tree ``build_bwrap_cmd`` binds
    read-write into that user's sandbox, so one ``touch zzz.md`` from any
    ordinary task produces ``unknown_skill`` — and a deployment-scope FAIL that
    a task reaches by accident goes red often and is skipped past, which is
    worth less than no alert. So a name within a typo's distance of a real skill
    (``_overlay_near_miss``) keeps FAIL and carries the suggestion, since a
    misspelled overlay is a customization its author believes is live and this
    is the only surface that would ever say otherwise; anything further away
    WARNs and is still named in the detail. Neither status is silence: what
    changed is which one alerts.
    """
    name = "config.skill_overlays"
    if not config.use_mount:
        return CheckResult(
            name, SKIP, "no workspace mount configured, so overlays are not read"
        )
    mount = Path(config.nextcloud_mount_path)
    if not (mount / "Users").is_dir():
        return CheckResult(name, SKIP, f"{mount}/Users does not exist yet")

    try:
        from .skills._loader import (  # noqa: PLC0415
            OVERLAY_UNKNOWN_SKILL,
            inspect_overlay,
            load_skill_index,
            open_overlay_dir,
        )
        known = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
    except Exception as exc:  # noqa: BLE001 - a check never raises
        return CheckResult(name, SKIP, f"the skill index could not be loaded: {exc}")

    dirs = _overlay_dirs(mount, config.bot_dir_name)
    total = 0
    dead: list[str] = []
    warned: list[str] = []
    # Directory-level refusals. Kept in their own list for two reasons. They
    # are not files, so measuring them against `total` rendered "1 of 0 overlay
    # file(s) are misfiled"; and they report at **WARN**, not FAIL, because a
    # sandboxed task can produce one at will — `ln -s /tmp config` inside its
    # own workspace — and a deployment-scope red an attacker can raise on
    # demand is the aimable alert ISSUE-340 split this check to avoid. That is
    # also the severity this module already gives a symlinked overlay *file*:
    # it loads as nothing and belongs in the report, but it is not the
    # misfiling a person fixes by renaming or shrinking, which is what
    # `_OVERLAY_FATAL_REASONS` is reserved for. Nothing about the safety half
    # turns on the severity — the link is refused either way, and no file from
    # behind it is opened or named.
    dir_findings: list[str] = []
    for user_id, user_dir, overlay_dir in dirs:
        if overlay_dir is None:
            dir_findings.append(
                _overlay_label(user_id, f"{config.bot_dir_name}/config/skills",
                               _OVERLAY_DIR_OUTSIDE_TREE)
            )
            continue
        # Opened one user at a time rather than all of them up front: this
        # walks every tree on the mount, and holding a descriptor per user
        # for the length of the sweep is a file-table cost for nothing.
        dir_fd = open_overlay_dir(user_dir, config.bot_dir_name, "config", "skills")
        if dir_fd is None:
            # `contained_overlay_dir` passed and this did not, so the two
            # disagree — deliberately: it accepts a symlink landing back inside
            # the user's own tree and the descriptor walk refuses one at any
            # component. Every reader now takes the strict answer, so this
            # user's overlays reach no prompt at all, which is exactly the
            # misfiling this check exists to name. The prompt loader degrades
            # silently and logs at `debug` (it runs once per eager skill per
            # task); this is the report that posture depends on (ISSUE-344).
            dir_findings.append(
                _overlay_label(user_id, f"{config.bot_dir_name}/config/skills",
                               _OVERLAY_DIR_UNOPENABLE)
            )
            continue
        try:
            # `scandir` on the descriptor rather than `overlay_dir.glob`, so the
            # listing comes from the directory that passed. The dotfile filter
            # `glob` applied is kept, and the asymmetry with the search reindex
            # and `skills overlays` is deliberate: those two attach no severity
            # to what they list, and this check does. `_classify_unknown_overlay`
            # reads `.developer.md` as a near-miss of `developer` and buckets it
            # `dead`, so listing dotfiles here would let any sandboxed task turn
            # a deployment-scope check red with one `touch` — the aimable alert
            # ISSUE-340 split this check to avoid.
            with os.scandir(dir_fd) as entries:
                names = sorted(
                    e.name for e in entries
                    if e.name.endswith(".md") and not e.name.startswith(".")
                )
        except OSError:
            os.close(dir_fd)
            continue
        try:
            for entry_name in names:
                total += 1
                path = overlay_dir / entry_name
                found = inspect_overlay(path, known_skills=known, dir_fd=dir_fd)
                if found.reason == OVERLAY_UNKNOWN_SKILL:
                    # The one reason a task produces with a single `touch`, so
                    # the severity turns on what the name looks like rather
                    # than on the reason alone. See ISSUE-340 and
                    # `_OVERLAY_FATAL_REASONS`.
                    fails, note = _classify_unknown_overlay(found.skill, known)
                    bucket = dead if fails else warned
                    bucket.append(_overlay_label(user_id, path.name, note))
                elif found.reason in _OVERLAY_FATAL_REASONS:
                    dead.append(_overlay_label(user_id, path.name, found.reason))
                elif found.reason is not None:
                    # Empty, not UTF-8, or a read this process was refused. Each
                    # loads as nothing and so belongs in the report, but none is
                    # a misfiling an operator acts on the way a renamed or
                    # shrunk file is, and a transient EACCES on one user's file
                    # must not turn a deployment-scope check red.
                    warned.append(_overlay_label(user_id, path.name, found.reason))
                elif found.warnings:
                    warned.append(
                        _overlay_label(user_id, path.name, ", ".join(found.warnings))
                    )
        finally:
            os.close(dir_fd)

    # `dir_findings` as well as `total`, because a refused directory contributes
    # no files: a check that returned OK here would report "no per-skill
    # overlays filed" for a user whose whole directory just stopped being
    # readable, which is the reassuring direction.
    if not total and not dead and not dir_findings:
        return CheckResult(
            name, OK,
            f"no per-skill overlays filed under {mount}/Users/*/{config.bot_dir_name}/config/skills",
        )

    #: A directory is not one of `total` overlay files, so it gets a clause of
    #: its own rather than a place in that fraction — counting it there read
    #: "1 of 0 overlay file(s) are misfiled" for a lone refused tree, and
    #: understated the ratio wherever one sat beside another user's good files.
    def _dir_clause() -> str:
        return (
            f"{len(dir_findings)} overlay director(y/ies) could not be read, so "
            f"none of those users' overlays loads: {_overlay_list(dir_findings)}"
        )

    if dead:
        detail = (
            f"{len(dead)} of {total} overlay file(s) are misfiled and will never "
            f"be loaded: {_overlay_list(dead)}"
        )
        if warned:
            # Never let a FAIL swallow the WARN list. Before ISSUE-340 split
            # `unknown_skill`, every one of these was fatal and so every one
            # was named; afterwards a single planted typo would have reported
            # "1 of 21" and hidden the other twenty — including a real overlay
            # sitting just under the loading cap — which is a count that reads
            # in the reassuring direction and an alert an attacker can aim.
            detail += (
                f"; {len(warned)} more reach no prompt or need a look: "
                f"{_overlay_list(warned)}"
            )
        if dir_findings:
            detail += f"; {_dir_clause()}"
        return CheckResult(
            name, FAIL, detail,
            remedy=(
                "A file here is only read when its name is a known skill that takes "
                "an overlay and its body is under the loading cap. A `did you mean` "
                "is a filename one or two characters off a real skill and a `names "
                "the X skill` is one built around a real name, so the rules in "
                "either reach no prompt at all — rename it to `X.md`. "
                + _OVERLAY_WARN_REMEDY
            ),
        )
    if warned or dir_findings:
        parts = []
        if warned:
            parts.append(
                f"{len(warned)} of {total} overlay file(s) need a look: "
                f"{_overlay_list(warned)}"
            )
        if dir_findings:
            parts.append(_dir_clause())
        return CheckResult(
            name, WARN, "; ".join(parts), remedy=_OVERLAY_WARN_REMEDY,
        )
    return CheckResult(
        name, OK, f"{total} overlay file(s) across {len(dirs)} user tree(s), all load"
    )


#: Shared by the WARN result and by the tail of the FAIL result, because a
#: FAIL now carries the warned files too and an operator reading it needs the
#: same glossary either way.
_OVERLAY_WARN_REMEDY = (
    "over_warn_bytes: the file is within a few KB of the loading cap, past "
    "which it stops reaching any prompt at all — shrink it, or move the rules "
    "that belong to another skill into that skill's own overlay. "
    "shallow_heading: a `# ` or `## ` heading is demoted to `#### ` at load "
    "time, because at its written level it would end the skill's own section. "
    "unknown_skill with `a copy of X.md`: an editor or a task left a backup "
    "beside the real overlay — delete it. unknown_skill on its own: the name "
    "resembles no skill, so it is most likely a scratch file — delete it, or "
    "rename it if it was meant to be an overlay. empty / overlay_not_utf8 / "
    "overlay_is_a_symlink / overlay_not_a_regular_file / overlay_unreadable: "
    "the file is there and contributes nothing to any prompt. "
    "dir_not_openable / dir_outside_user_tree: these name a directory rather "
    "than a file, and none of that user's overlays is read by anything. The "
    "path has to be a chain of plain directories inside the user's own tree: "
    "the usual cause is a symlink at `config` or `skills` (replace it with a "
    "real directory and move the files into it), but an unreadable directory "
    "or a regular file left at `skills` reads the same way, so check what is "
    "actually there before assuming a link. Run "
    "`istota-skill skills overlays` as that user for the per-file verdict."
)


def _overlay_list(items: list[str]) -> str:
    shown = ", ".join(items[:_OVERLAY_REPORT_LIMIT])
    if len(items) > _OVERLAY_REPORT_LIMIT:
        shown += f", and {len(items) - _OVERLAY_REPORT_LIMIT} more"
    return shown


# ---------------------------------------------------------------------------
# talk.signaling.*
# ---------------------------------------------------------------------------
#
# Four checks over the Talk signaling transport, and **none of them
# authenticates to the signaling server or joins a room**. That is a
# constraint rather than an economy: doctor runs on a scheduler interval, on
# the daemon's start-up path and behind the admin Health pane, and a check that
# opened a signaling session would have to POST `participants/active` for some
# room to do it — putting a phantom istota participant into a live room's
# member list every time somebody loaded a dashboard, and emitting a
# participant-list refresh to everyone in it.
#
# So `talk.signaling_reachable` reads the `welcome` frame, which the server
# sends *before* any hello, and closes. `talk.signaling_auth` reads
# `/cloud/capabilities`, which mints nothing. `talk.signaling_watchers` reads
# in-process counters and makes no request at all.
#
# One qualification, because the reachability check's own docstring would
# otherwise read as a stronger claim than it is: with `[talk.signaling] url`
# empty — the documented normal case — resolving the HPB URL means an
# authenticated `GET /v3/signaling/settings` as the bot, which mints a
# 60-second JWT that is read for its `server` field and never used. What none
# of these does is authenticate to the *signaling server* or create a Talk
# session; the memo below bounds the token to one per TTL.

# How long the whole reachability probe gets: the settings call, the connect
# and the first frame, against one deadline rather than one budget each. Its
# own name rather than `PROBE_TIMEOUT`, which is the budget for spawning a
# binary — these are network round trips to two different hosts, and coupling
# them would mean a subprocess timeout tuning decided how long a WebSocket
# handshake may take.
#
# **A deadline, because the legs are sequential and this runs on the daemon's
# start-up path** (`scheduler.run_startup_checks`) and behind the admin Health
# pane. Handing the same number to each leg made a slow Nextcloud followed by a
# slow HPB cost twice what the constant says, and inside the socket leg
# `open_timeout` plus a first-frame wait doubled it again. `talk.signaling_auth`
# spends its own budget on top, since it reads a different endpoint and is not
# covered by the memo, so the worst case an operator can meet is two of these.
SIGNALING_PROBE_TIMEOUT = 10

# The floor a leg is given once the deadline is nearly spent. A leg handed
# ~0s fails instantly with a timeout that says nothing about the host, so the
# probe would report "unreachable" for a server it never dialled.
_SIGNALING_MIN_LEG = 1.0

# How long a reachability answer is reused. One doctor run asks two checks the
# same question, and probing twice would double the OCS settings call and the
# connect on the hourly sweep and on every admin page load. Short enough that a
# rerun a minute later is a fresh answer, which is what an operator watching a
# server come back up expects of a diagnostic.
_SIGNALING_PROBE_TTL = 15.0

_signaling_probe_memo: "tuple[float, tuple, _SignalingProbe] | None" = None


# Why a probe did not answer, where the three reasons want three different
# findings. Inferring them from an empty URL instead collapses the first two,
# which are a deployment that refuses to boot and a deployment that boots and
# polls — opposite answers to "is anything wrong here".
BLOCKED_LIBRARY = "library"          # `enabled = true`, no websockets: a refusal
BLOCKED_UNAVAILABLE = "unavailable"  # no HPB registered, or Talk did not say
BLOCKED_UNREACHABLE = "unreachable"  # a URL that resolved and did not answer


@dataclass(frozen=True)
class _SignalingProbe:
    """What one unauthenticated look at the HPB established.

    ``error`` is the whole verdict and ``blocker`` is its class: a probe that
    could not import the library, could not resolve a URL or could not read a
    frame reports why, and ``features`` is empty. The fields are not
    independent — a caller must read ``error`` first, because "no features" is
    equally what an unreachable server and a server advertising nothing look
    like, and those want different findings.
    """

    url: str
    version: str
    features: tuple[str, ...]
    error: str
    blocker: str = ""


def reset_signaling_probe_memo() -> None:
    """Forget the cached reachability answer. Test-teardown helper."""
    global _signaling_probe_memo
    _signaling_probe_memo = None


def _signaling_gate(config: "Config") -> str:
    """"" when the signaling checks should run, else the reason to SKIP.

    Off is the default and is a supported shape rather than a misconfiguration
    — a deployment with no high-performance backend keeps the Talk poller,
    which is the capability floor — so this must never report a finding.
    """
    talk = getattr(config, "talk", None)
    if talk is None or not getattr(talk, "enabled", False):
        return "Talk is disabled"
    sig = getattr(talk, "signaling", None)
    if sig is None or not getattr(sig, "enabled", False):
        return "Talk signaling is disabled ([talk.signaling] enabled)"
    return ""


def _run_off_loop(make_coro, timeout: float):
    """Run one coroutine to completion on a private loop, from any context.

    doctor is reached from a plain thread (the CLI, the daemon's start-up) and
    from a worker thread (``asyncio.to_thread`` in the admin pane, in
    ``!check`` and in the heartbeat). A private loop on a private thread is
    correct from all of them and stays correct if a fourth caller ever runs on
    a loop, where ``asyncio.run`` would raise. It also keeps this off the
    persistent runtime, whose pooled ``TalkClient`` belongs to the scheduler's
    loop and refuses a foreign one outright.

    **The coroutine is bounded here, not only joined here.** Leaving the bound
    to the coroutine and joining for a bit longer is what leaks a thread: the
    socket leg's own limits are an ``open_timeout`` *plus* a first-frame wait
    *plus* a close, so a server that completes the handshake slowly and then
    sends nothing outlives any join sized for one of them — the caller gets a
    timeout naming the wrong number while the thread and its socket stay live.
    One ``wait_for`` around the whole body makes the bound the thing the caller
    asked for; the join is a second past it, as a backstop for a coroutine that
    cannot be cancelled.

    Returns ``(value, error)``. Never raises: the caller is a check.
    """
    import asyncio

    box: dict = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(
                asyncio.wait_for(make_coro(), timeout=timeout)
            )
        except BaseException as exc:  # noqa: BLE001 — reported, not propagated
            box["error"] = exc

    thread = threading.Thread(target=target, name="doctor-signaling", daemon=True)
    thread.start()
    thread.join(timeout + 1.0)
    if thread.is_alive():
        return None, f"did not return within {timeout:.0f}s"
    exc = box.get("error")
    if exc is not None:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return None, f"timed out after {timeout:.0f}s"
        return None, f"{type(exc).__name__}: {exc}"
    return box.get("value"), ""


def _signaling_settings(config: "Config", timeout: float):
    """Talk's signaling settings for the bot account, or the reason there are none.

    A short-lived ``TalkClient`` rather than the persistent singleton, which is
    bound to the scheduler's loop and would refuse this thread. One cheap OCS
    GET; it mints a 60-second token that is read for its ``server`` field, never
    used, and never logged.
    """
    from .transport.talk import signaling as sig

    async def _fetch():
        from .talk import TalkClient

        client = TalkClient(config, timeout=timeout)
        try:
            return await client.get_signaling_settings()
        finally:
            await client.aclose()

    payload, error = _run_off_loop(_fetch, timeout)
    if error:
        return None, error
    return sig.parse_settings(payload, nextcloud_url=config.nextcloud.url), ""


def _signaling_welcome_frame(ws_url: str, timeout: float):
    """Connect, read the first frame, close. No hello, so no session.

    The server has its own 2-second deadline for a hello after connecting
    (``hub.go:113``), so closing without one is the ordinary way a client that
    only wanted the feature list leaves.

    The per-stage limits are deliberately a fraction of the caller's budget
    rather than all of it: ``_run_off_loop`` bounds the whole coroutine, and a
    handshake and a first frame each given the full number would let the pair
    run to twice it before that outer bound noticed.

    No ``proxy`` argument, which is a decision rather than an omission. On
    ``websockets`` 15 and later the default follows the process's
    ``HTTPS_PROXY``/``ALL_PROXY`` — and the watcher this is a diagnostic for
    will use the same library with the same default, so overriding it here
    would have the check answer about a route the runtime does not take. It is
    also not a parameter on 14, which the floor still admits.
    """
    import asyncio
    import json

    from .transport.talk import signaling as sig

    handshake = max(timeout / 2.0, 1.0)

    async def _read():
        websockets = sig.require_websockets()
        async with websockets.connect(
            ws_url, open_timeout=handshake, close_timeout=2,
        ) as socket:
            raw = await asyncio.wait_for(socket.recv(), timeout=handshake)
        return json.loads(raw)

    frame, error = _run_off_loop(_read, timeout)
    if error:
        return {}, error
    return frame if isinstance(frame, dict) else {}, ""


def _signaling_capabilities(config: "Config", timeout: float):
    """``/cloud/capabilities``, or the reason it could not be read.

    Synchronous and off the signaling path entirely: it is the request the
    daemon already makes to find out what this Nextcloud supports, and it mints
    no credential of any kind.
    """
    from .nextcloud.capabilities import fetch_capabilities

    try:
        return fetch_capabilities(config, timeout=timeout), ""
    except Exception as exc:  # noqa: BLE001 — a check never raises
        return None, f"{type(exc).__name__}: {exc}"


def _signaling_probe(config: "Config") -> _SignalingProbe:
    """Resolve the HPB URL and read its ``welcome``, memoized for a few seconds."""
    global _signaling_probe_memo

    sig_config = config.talk.signaling
    # The account is in the key because with no configured URL the answer is
    # derived from a call authenticated as that account, so two configs sharing
    # a Nextcloud URL and differing in credentials do not share an answer.
    key = (
        sig_config.url,
        getattr(config.nextcloud, "url", ""),
        getattr(config.nextcloud, "username", ""),
    )

    memo = _signaling_probe_memo
    if memo is not None and memo[1] == key and (time.monotonic() - memo[0]) < _SIGNALING_PROBE_TTL:
        return memo[2]

    probe = _probe_signaling(
        config, sig_config, time.monotonic() + SIGNALING_PROBE_TIMEOUT,
    )
    _signaling_probe_memo = (time.monotonic(), key, probe)
    return probe


def _probe_signaling(config, sig_config, deadline: float) -> _SignalingProbe:
    from .transport.talk import signaling as sig

    def remaining() -> float:
        return max(deadline - time.monotonic(), _SIGNALING_MIN_LEG)

    # Before anything reaches the network: a deployment with `enabled = true`
    # and no library refuses to boot, so reporting "connection refused" here
    # would name the wrong fault and send an operator to look at a server that
    # is fine.
    try:
        sig.require_websockets()
    except sig.SignalingUnavailable as exc:
        return _SignalingProbe("", "", (), str(exc), BLOCKED_LIBRARY)

    if sig_config.url:
        source = sig_config.url
    else:
        settings, error = _signaling_settings(config, remaining())
        if error:
            return _SignalingProbe(
                "", "", (),
                f"Talk's signaling settings could not be read: {error}",
                BLOCKED_UNAVAILABLE,
            )
        reason = sig.hpb_unavailable_reason(settings)
        if reason is not None:
            return _SignalingProbe("", "", (), reason, BLOCKED_UNAVAILABLE)
        source = settings.server

    try:
        ws_url = sig.websocket_url(source)
    except ValueError as exc:
        return _SignalingProbe("", "", (), str(exc), BLOCKED_UNAVAILABLE)

    frame, error = _signaling_welcome_frame(ws_url, remaining())
    if error:
        return _SignalingProbe(
            ws_url, "", (), f"{ws_url}: {error}", BLOCKED_UNREACHABLE,
        )

    features = sig.parse_welcome(frame)
    welcome = frame.get("welcome") if isinstance(frame, dict) else None
    version = ""
    if isinstance(welcome, dict) and isinstance(welcome.get("version"), str):
        version = welcome["version"]
    if not features:
        return _SignalingProbe(
            ws_url, version, (),
            f"{ws_url} answered, but the frame carried no feature list",
            BLOCKED_UNREACHABLE,
        )
    return _SignalingProbe(ws_url, version, features, "")


def check_signaling_reachable(config: "Config", probe: bool) -> CheckResult:
    """Does the high-performance backend answer, and can we tell where it is.

    Unauthenticated: the ``welcome`` frame precedes the hello, so this opens a
    socket, reads one frame and closes without creating a signaling session or
    a Talk participant row. See the section header for why that matters.
    """
    name = "talk.signaling_reachable"
    reason = _signaling_gate(config)
    if reason:
        return CheckResult(name, SKIP, reason, scope=DEPLOYMENT)
    if not probe:
        return CheckResult(
            name, SKIP,
            "reachability cannot be observed without a network request "
            "(probe disabled)",
            scope=DEPLOYMENT,
        )

    result = _signaling_probe(config)
    if result.error:
        # A deployment with no backend registered has nothing for this check
        # to reach, and `talk.signaling_auth` already FAILs it with the remedy
        # that fixes it. A second FAIL here would name a configuration fault
        # under a check called "reachable" and page an operator twice for one
        # cause — the same reason the chat-relay check SKIPs rather than
        # asserting a fault it did not observe.
        if result.blocker == BLOCKED_UNAVAILABLE:
            return CheckResult(
                name, SKIP,
                f"{result.error}; there is no backend URL to reach. See "
                "talk.signaling_auth",
                scope=DEPLOYMENT,
            )

        # The missing library is the other startup refusal, so it is a fault
        # rather than a question this check could not answer.
        if result.blocker == BLOCKED_LIBRARY:
            return CheckResult(
                name, FAIL, result.error,
                remedy=(
                    "Install the signaling extra (`uv sync --extra "
                    "signaling`), or set [talk.signaling] enabled = false to "
                    "keep the Talk poller. The daemon refuses to start "
                    "as configured."
                ),
                scope=DEPLOYMENT,
            )

        return CheckResult(
            name, FAIL, result.error,
            remedy=(
                "Check that the nextcloud-spreed-signaling server is running "
                "and that this host can reach it, and that [talk.signaling] "
                "url — when set — names the route the daemon should take. "
                "Inbound Talk is not dead meanwhile: watchers retry on a "
                "backoff and the reconciliation pass keeps fetching the rooms "
                "that are behind, so messages arrive within "
                "[talk.signaling] room_sync_interval rather than within a "
                "second. Setting enabled = false returns the deployment to the "
                "Talk poller."
            ),
            scope=DEPLOYMENT,
        )

    version = result.version or "an unreported version"
    return CheckResult(
        name, OK,
        f"{result.url} answered a welcome frame ({version}, "
        f"{len(result.features)} features)",
        scope=DEPLOYMENT,
    )


def check_signaling_chat_relay(config: "Config", probe: bool) -> CheckResult:
    """Does the server relay chat payloads, or only bare refreshes.

    Read from the ``welcome`` frame rather than from a version string, because
    that is what the runtime does: the feature is what the server advertises,
    not what its release notes say. Talk treats it as optional
    (``Manager.php:210-215``), so an older server connects perfectly and simply
    never sends a comment — which is a working deployment, one fetch per
    message more expensive than it needs to be, and is therefore a warning
    rather than a failure.
    """
    name = "talk.signaling_chat_relay"
    reason = _signaling_gate(config)
    if reason:
        return CheckResult(name, SKIP, reason, scope=DEPLOYMENT)
    if not probe:
        return CheckResult(
            name, SKIP,
            "the advertised feature list cannot be read without a network "
            "request (probe disabled)",
            scope=DEPLOYMENT,
        )

    result = _signaling_probe(config)
    if result.error:
        # A server we could not reach advertises nothing we can read. Calling
        # that "no chat-relay" would name the wrong fault and send an operator
        # to upgrade a server that is merely down.
        return CheckResult(
            name, SKIP,
            "the server's feature list could not be read; see "
            "talk.signaling_reachable for why",
            scope=DEPLOYMENT,
        )

    from .transport.talk import signaling as sig

    if sig.CHAT_RELAY_FEATURE in result.features:
        return CheckResult(
            name, OK,
            f"{result.url} advertises chat-relay", scope=DEPLOYMENT,
        )

    return CheckResult(
        name, WARN,
        f"{result.url} does not advertise chat-relay, so every chat event "
        "arrives as a bare refresh and each one costs a fetch of the room",
        remedy=(
            "Upgrade the signaling server to 2.1.0 or later. Inbound Talk "
            "still works meanwhile — the latency win is unaffected, only the "
            "saved request is lost."
        ),
        scope=DEPLOYMENT,
    )


def _spreed_signaling_capabilities(payload) -> dict:
    node = payload
    for key in ("capabilities", "spreed", "config", "signaling"):
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return node if isinstance(node, dict) else {}


def check_signaling_auth(config: "Config", probe: bool) -> CheckResult:
    """Talk's own half: an external signaling mode, and a hello-v2 token key.

    Reads ``/cloud/capabilities`` and mints nothing — the settings endpoint
    would answer the mode question too and would mint a token to do it, once
    per doctor run for ever. The mode verdict comes from
    ``signaling.signaling_mode_reason``, the same predicate the startup refusal
    reads through the settings payload, so the warning an operator gets here
    and the refusal they get on the next restart cannot disagree.
    """
    name = "talk.signaling_auth"
    reason = _signaling_gate(config)
    if reason:
        return CheckResult(name, SKIP, reason, scope=DEPLOYMENT)
    if not probe:
        return CheckResult(
            name, SKIP,
            "Talk's capabilities cannot be read without a network request "
            "(probe disabled)",
            scope=DEPLOYMENT,
        )

    from .transport.talk import signaling as sig

    payload, error = _signaling_capabilities(config, SIGNALING_PROBE_TIMEOUT)
    if error:
        return CheckResult(
            name, WARN,
            f"Talk's capabilities could not be read: {error}",
            remedy=(
                "Check the Nextcloud URL and the bot account's credentials. "
                "Until this answers, whether the deployment has a "
                "high-performance backend at all is unknown, and "
                "[talk.signaling] enabled = true refuses to start without one."
            ),
            scope=DEPLOYMENT,
        )

    caps = _spreed_signaling_capabilities(payload)
    mode = caps.get("mode")

    # `capabilities.spreed.config.signaling.mode` was read off the deployment
    # this design was verified against, which is why the check asks it here
    # rather than paying for the settings call and its token. It is still a key
    # a *different* Talk version may not publish, and the difference between
    # "not published" and "internal" is the difference between a working
    # deployment and a broken one — so an absent key reports what it did not
    # find instead of falling through `signaling_mode_reason`'s unreadable-mode
    # arm and FAILing a deployment whose HPB is fine. The other reader of that
    # arm is the startup refusal, which reads the settings endpoint, where an
    # unreadable mode really is a fault.
    if mode is None:
        return CheckResult(
            name, WARN,
            "Talk's capabilities carry no spreed.config.signaling.mode, so "
            "whether a high-performance backend is registered cannot be "
            "answered without minting a token",
            remedy=(
                "Check talk.signaling_reachable, which reads the signaling "
                "settings and does resolve the mode, and confirm with "
                "occ talk:signaling:list."
            ),
            scope=DEPLOYMENT,
        )

    mode_reason = sig.signaling_mode_reason(mode)
    if mode_reason is not None:
        return CheckResult(
            name, FAIL, mode_reason,
            remedy=(
                "Register a signaling server with Talk "
                "(occ talk:signaling:add, then occ talk:signaling:list to "
                "confirm), or set [talk.signaling] enabled = false to keep the "
                "Talk poller."
            ),
            scope=DEPLOYMENT,
        )

    # Never echoed, only tested for presence. It is a *public* key, so this is
    # hygiene rather than a boundary — but a check's detail is one line saying
    # what was observed, and a base64 blob in the daemon log and on the admin
    # Health pane is not that.
    if not caps.get("hello-v2-token-key"):
        return CheckResult(
            name, WARN,
            f"Talk reports {mode} signaling but publishes no "
            "hello-v2-token-key, so the client falls back to the v1 ticket — "
            "which does not expire and is never rotated, so a leaked one is a "
            "permanent credential for the bot's signaling identity",
            remedy=(
                "Upgrade Talk, or check that spreed.config.signaling."
                "hello-v2-token-key appears in /cloud/capabilities. Inbound "
                "signaling works on the v1 ticket meanwhile."
            ),
            scope=DEPLOYMENT,
        )

    return CheckResult(
        name, OK,
        f"Talk reports {mode} signaling and publishes a hello-v2 token key",
        scope=DEPLOYMENT,
    )


def _as_count(value) -> int:
    """A counter off a supervisor's ``stats()``, coerced and never raising."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return max(0, int(value))
    except (ValueError, OverflowError):
        return 0


def check_signaling_watchers(config: "Config", probe: bool) -> CheckResult:
    """Every live room has a connected watcher, and no room is behind.

    **Four independent faults, deliberately not collapsed into a census.** A
    watcher can be down right now, never have been up at all, be up and
    relaying nothing, or be owed a fetch nobody is making — and only the first
    is visible from a socket count.

    **The rooms-behind count is the half that cannot be inferred from the
    others**, and it is why this check is not simply a watcher census. The
    reconciliation pass compares each room's ``lastMessage.id`` against its
    stored cursor and fetches only the rooms that are behind, so a signaling
    path that has silently stopped delivering still looks perfect from every
    other angle: the sockets are up, no reconnect is logged, and the messages
    arrive — late, by another route. A non-zero count is what says the event
    stream is not carrying the traffic.

    ``probe`` is accepted to satisfy the ``Check`` protocol and is unused: this
    reads in-process counters and makes no request.
    """
    name = "talk.signaling_watchers"
    reason = _signaling_gate(config)
    if reason:
        return CheckResult(name, SKIP, reason, scope=DEPLOYMENT)

    from .transport.talk import signaling as sig

    stats = sig.read_stats()
    if stats is None:
        # doctor also runs in the web process, in the CLI and behind `!check`,
        # where the supervisor lives in another process entirely. Reporting
        # every watcher down there would page an operator about a process that
        # was never meant to have any.
        return CheckResult(
            name, SKIP,
            "no signaling supervisor is running in this process",
            scope=DEPLOYMENT,
        )

    watchers = _as_count(stats.get("watchers"))
    connected = _as_count(stats.get("connected"))
    behind = _as_count(stats.get("rooms_behind"))
    stranded = _as_count(stats.get("stale_dirty"))
    # Type-guarded like the three counters beside it, and for a sharper reason:
    # `or []` on the *string* "abc123" iterates it into six single-character
    # tokens, each of which passes the filter, so the WARN would name six rooms
    # that do not exist.
    raw_disconnected = stats.get("disconnected")
    disconnected = [
        token for token in
        (raw_disconnected if isinstance(raw_disconnected, (list, tuple)) else [])
        if isinstance(token, str) and token
    ]
    raw_stuck = stats.get("never_connected")
    never_connected = [
        token for token in
        (raw_stuck if isinstance(raw_stuck, (list, tuple)) else [])
        if isinstance(token, str) and token
    ]

    findings = []
    # The count comparison stands on its own rather than behind the token
    # list, because a supervisor is free to report counters and no tokens — and
    # a watcher mid-reconnect is plausibly counted as not connected while
    # belonging on no "disconnected" list. Reading only the list there returns
    # OK with a detail saying "1 of 5 watchers connected", which is a check
    # contradicting itself in the one place it is meant to be authoritative.
    if connected < watchers:
        findings.append(f"{connected} of {watchers} watchers connected")
    if disconnected:
        findings.append(
            "disconnected: " + _overlay_list(disconnected)
        )
    # **Reported apart from `disconnected` rather than inside it** (ISSUE-416).
    # A watcher between reconnects is on that list for a second at a time and
    # is not a fault; one that has never once connected is a room nothing has
    # ever delivered for, and it reads identically to a healthy room on every
    # other number here. Collapsing the two is what made ISSUE-414 invisible
    # from outside for three investigations: the supervisor reported watchers
    # present, the reconciler was healthy, and one room was simply dark.
    if never_connected:
        findings.append(
            "never connected: " + _overlay_list(never_connected)
        )
    if behind:
        findings.append(
            f"{behind} room(s) were behind their cursor at the last "
            "reconciliation, so messages are arriving over the fallback fetch "
            "rather than over the event stream"
        )
    # The one failure no other counter can show. A room whose triggered fetch
    # raised keeps its dirty bit — deliberately, so the next event re-runs it
    # rather than coalescing into a fetch that already died — and until
    # something re-wakes the drain it is owed a fetch nobody is making. Its
    # watcher is connected, its socket is fine, and every other number here is
    # healthy, which is exactly why the supervisor counts it.
    if stranded:
        findings.append(
            f"{stranded} room(s) have been waiting on a triggered fetch for "
            "longer than one room_sync_interval, so a fetch failed and has "
            "not been retried"
        )

    if findings:
        return CheckResult(
            name, WARN, "; ".join(findings),
            remedy=(
                "Check talk.signaling_reachable and the daemon log for "
                "reconnect and error lines. A rooms-behind count that stays "
                "non-zero while every watcher reports connected means the "
                "server accepted the join and is relaying nothing. A room "
                "listed as never connected has been restarted and still could "
                "not reach a session, so look for what is refusing the join "
                "rather than for the socket."
            ),
            scope=DEPLOYMENT,
        )

    return CheckResult(
        name, OK,
        f"{connected} of {watchers} watchers connected, no room behind its "
        "cursor and none waiting on a failed fetch",
        scope=DEPLOYMENT,
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

# The name is part of the registry rather than only of the result, so `only=`
# can select *before* invoking. Filtering afterwards would mean running every
# check to discard most of them — which is exactly what the config-load path
# cannot afford.
CHECKS: tuple[tuple[str, Check], ...] = (
    ("runtime.platform", check_platform),
    ("runtime.bwrap", check_bwrap),
    ("runtime.model_cli", check_model_cli),
    ("runtime.tmux", check_tmux),
    ("runtime.native_brain", check_native_brain),
    ("runtime.framework_db", check_framework_db),
    ("runtime.task_failure_rate", check_task_failure_rate),
    ("runtime.writable_dirs", check_writable_dirs),
    ("runtime.mount_liveness", check_mount_liveness),
    ("runtime.session_log_dir", check_session_log_dir),
    ("runtime.task_control_dir", check_task_control_dir),
    ("runtime.subscription_usage", check_subscription_usage),
    ("runtime.model_execution", check_model_execution),
    ("security.skill_proxy", check_skill_proxy),
    ("security.sandbox_effective", check_sandbox_effective),
    ("security.sandbox_credentials", check_sandbox_credentials),
    ("security.skill_model_credential", check_skill_model_credential),
    ("security.secret_key", check_secret_key),
    ("security.devbox_netfilter", check_devbox_netfilter),
    ("developer.forge_binaries", check_forge_binaries),
    ("developer.forge_config_drift", check_forge_config_drift),
    ("developer.forge_wrapper_shadowing", check_forge_wrapper_shadowing),
    ("developer.forge_policy", check_forge_policy),
    ("developer.gitlab_reviewer", check_gitlab_reviewer),
    ("developer.forge_transport", check_forge_transport),
    ("developer.repos_layout", check_repos_layout),
    ("developer.container", check_developer_container),
    ("talk.signaling_reachable", check_signaling_reachable),
    ("talk.signaling_chat_relay", check_signaling_chat_relay),
    ("talk.signaling_auth", check_signaling_auth),
    ("talk.signaling_watchers", check_signaling_watchers),
    ("web.static", check_web_static),
    ("web.build_current", check_web_build_current),
    ("web.basemap", check_basemap),
    ("web.avatar_import", check_avatar_import),
    ("config.skill_overlays", check_skill_overlays),
    ("sandbox.masks", check_sandbox_masks),
)

# Checks that spawn a namespace and are therefore opt-in. Kept as a set beside
# the registry rather than as a flag on the tuple: the registry is a mapping of
# name to function and stays readable as one.
DEEP_CHECKS = frozenset({"sandbox.masks"})

LIVE_CHECKS = frozenset({"runtime.model_execution"})
"""Checks that reach a model, and therefore cost money.

Separate from ``DEEP_CHECKS``, which means "spawns a namespace". The two axes
look alike and are not: ``sandbox.masks`` is free and slow, this is billed and
slow, and no caller wants them selected by the same flag. ``web_app``'s deep
phase in particular runs every ``DEEP_CHECKS`` member under a budget sized for
one 30s check, and would bill a model call every time an admin opened the Health
pane. The vocabulary matches the ``live`` pytest marker, which draws the same
line for the same reason.
"""

# Each check's scope, so `scope=` can select *before* invoking. Filtering the
# results afterwards would mean `--scope image` in a volume-less `docker run`
# still opened the framework DB and stat'd a mount that isn't there — paying for
# the deployment-scoped checks in order to throw them away, in the one tier that
# exists because it is cheap. A check's results all carry its registry scope,
# and a unit test enforces that.
CHECK_SCOPES: dict[str, str] = {
    "runtime.platform": IMAGE,
    "runtime.bwrap": IMAGE,
    "runtime.model_cli": IMAGE,
    "runtime.tmux": IMAGE,
    # Deployment, not image: a credential is a property of an install, and the
    # per-user arm reads the install's own secrets table. A bare `docker run`
    # has neither and would report a missing key about nothing.
    "runtime.native_brain": DEPLOYMENT,
    "runtime.framework_db": DEPLOYMENT,
    # Deployment, not image: it queries the `tasks` table of an install's own
    # database, which a bare `docker run` has none of.
    "runtime.task_failure_rate": DEPLOYMENT,
    "runtime.writable_dirs": DEPLOYMENT,
    "runtime.mount_liveness": DEPLOYMENT,
    # Deployment, not image: it walks a directory on the install's own disk and
    # reads the last sweep's row out of the framework database, neither of which
    # a bare `docker run` has.
    "runtime.session_log_dir": DEPLOYMENT,
    # Deployment, not image: every question it asks is about a rendered
    # config's paths and about a tree on the install's own disk. A bare
    # `docker run` has neither a users table nor a temp dir anything wrote to.
    "runtime.task_control_dir": DEPLOYMENT,
    # Deployment, not image: it needs a credential and network egress, neither of
    # which a bare `docker run` has. Not in DEEP_CHECKS — it spawns no namespace.
    "runtime.subscription_usage": DEPLOYMENT,
    # Deployment, not image: it needs a credential, a configured user and
    # network egress. In LIVE_CHECKS, so `--scope image` never reaches it
    # anyway — the scope is stated for the same reason every other one is.
    "runtime.model_execution": DEPLOYMENT,
    "security.skill_proxy": DEPLOYMENT,
    # Deployment, not image: "can this host create a namespace" is a property of
    # the deployment — the container's `security_opt`, the host's sysctl — where
    # `runtime.bwrap`'s "is the binary installed and runnable" is a property of
    # the image. The image tier runs `--scope image` in a bare `docker run` with
    # no `security_opt` and asserts no check fails, so an IMAGE scope here would
    # fail every correct image. See `check_sandbox_effective`.
    "security.sandbox_effective": DEPLOYMENT,
    # Deployment, not image: it reaches `istota.executor` for the bwrap
    # capability probe, and the pairing it reports is a posture an operator
    # chose in a rendered config. The image tier asserts over `--scope image`
    # and must not go red for a deployment's own decision.
    "security.sandbox_credentials": DEPLOYMENT,
    "security.skill_model_credential": DEPLOYMENT,
    # Deployment, not image: a master key is a property of an install, and the
    # thing it unlocks is that install's own secrets table. A bare `docker run`
    # has neither and would report a missing key about nothing.
    "security.secret_key": DEPLOYMENT,
    "security.devbox_netfilter": DEPLOYMENT,
    "developer.forge_binaries": IMAGE,
    "developer.forge_config_drift": DEPLOYMENT,
    "developer.forge_wrapper_shadowing": IMAGE,
    "developer.forge_policy": DEPLOYMENT,
    "developer.gitlab_reviewer": DEPLOYMENT,
    "developer.forge_transport": DEPLOYMENT,
    # Deployment, not image: four of its five results need a running
    # container to reach, and the fifth reads the rendered config file.
    # Deployment: it is a fact about what is filed on this host.
    "developer.repos_layout": DEPLOYMENT,
    "developer.container": DEPLOYMENT,
    # Deployment, not image: three of the four reach a network — a running
    # signaling server and a running Nextcloud — and the fourth reads counters
    # only the scheduler process has. A bare `docker run` can answer none of
    # them.
    "talk.signaling_reachable": DEPLOYMENT,
    "talk.signaling_chat_relay": DEPLOYMENT,
    "talk.signaling_auth": DEPLOYMENT,
    "talk.signaling_watchers": DEPLOYMENT,
    "web.static": IMAGE,
    # Deployment, not image: it compares the bundle against the checkout it
    # was built from, and a bare `docker run` has no checkout.
    "web.build_current": DEPLOYMENT,
    # Deployment, not image: it reads the rendered config and reaches the
    # network. A bare `docker run` can answer neither.
    "web.basemap": DEPLOYMENT,
    # Deployment: every fact it reports is in the framework database or the
    # rendered config — the counts in `user_avatars` and what the last import
    # tick wrote down. A bare `docker run` has neither.
    "web.avatar_import": DEPLOYMENT,
    # Deployment: it walks the workspace mount, which a bare `docker run` has
    # none of.
    "config.skill_overlays": DEPLOYMENT,
    "sandbox.masks": DEPLOYMENT,
}


CONFIG_GATE = "config.loaded"
"""Name of the gate result below.

Deliberately in neither ``CHECKS`` nor ``CHECK_SCOPES``, and a test says so.
Every check in the registry answers a question about the host; this one answers
whether the run is about *this* host at all, and a run it says no to renders
nothing else — so it has no ``only=`` prefix to be selected by and nothing to
filter. Its ``scope`` is set explicitly on every result all the same, since a
machine consumer reads the same field off it as off any other row.
"""

#: Cap for a config path quoted into a ``detail``. Wider than
#: :func:`_probe_output_line`'s default because the whole value is the finding
#: here rather than an excerpt of somebody's stderr, and a truncation is marked
#: — an operator told that a path did not resolve must not be shown a different
#: path from the one that was tried.
_NAMED_PATH_CHARS = 200


def _named_path(raw: str) -> str:
    """One capped line for a config path, with a visible cut when it is capped."""
    flat = " ".join(raw.split())
    if len(flat) <= _NAMED_PATH_CHARS:
        return flat
    return flat[:_NAMED_PATH_CHARS] + "\u2026"


#: Names that mean "this process is a *task*, not the daemon". Either is
#: enough. ``ISTOTA_TASK_ID`` is the general one — `task_env.build_task_runtime`
#: sets it on every task env unconditionally — and ``ISTOTA_SANDBOXED`` is a
#: subset of it, kept so the answer does not depend on one name.
#:
#: **Not** ``_non_daemon_env_markers``, which is wider by one:
#: ``PRECOMMIT_SCANS_REQUIRED`` marks a cron ``command`` job and a heartbeat
#: shell command, both of which run unsandboxed as the daemon user with the
#: config directory in front of them. Telling those to "run it on the host as
#: the daemon user" would be telling them to do what they are already doing.
_TASK_ENV_MARKERS = ("ISTOTA_TASK_ID", "ISTOTA_SANDBOXED")


def _is_task_process() -> bool:
    """Was this process's environment built for a task by ``build_task_runtime``."""
    return any(os.environ.get(name) for name in _TASK_ENV_MARKERS)


def config_visibility(
    config: "Config", requested: Path | None = None, scope: str = ""
) -> CheckResult | None:
    """``None`` when this run can see the deployment's config; a result when it cannot.

    ``load_config`` returns a bare ``Config()`` when no candidate resolves, and
    says so nowhere. Every path-and-policy check then answers about defaults — a
    relative ``data/istota.db``, the default temp dir, a whole ``[security]``
    block the operator never wrote — while reading exactly like a run about the
    real deployment. Two findings from the run that produced ISSUE-412 are
    explained by that and by nothing else: ``FAIL runtime.task_failure_rate —
    data/istota.db: no such table: tasks`` and ``OK runtime.framework_db —
    data/istota.db: quick_check clean``, both about a stray zero-byte file an
    earlier probe left in the task's own workspace.

    **Inside a task this is unconditional.** ``build_clean_env`` exports
    ``ISTOTA_CONFIG_PATH`` naming a file under ``config/``, and ``config/`` is
    bound into no sandbox on purpose — it holds emissaries, persona and
    guidelines, which are prompt text. So the exported path never resolves in
    there and a sandboxed doctor run can never be about the real deployment.

    **The verdict splits on the same principle as ``_deployment_sandboxing``
    and not on the same predicate.** A boundary doing its job is not a fault —
    reporting the unreadable config as one is the ISSUE-381 shape 492a91e9
    existed to remove — so a *task* gets a ``SKIP``. But the question here is
    "is this process the daemon", not "would a bwrap probe from here be
    nested", and ``ISTOTA_SANDBOXED`` answers only the second: `task_env` sets
    it under ``skill_proxy_enabled and effective_sandboxing``, while
    ``ISTOTA_CONFIG_PATH`` is set on ``config_path is not None`` alone. So on a
    ``sandbox_enabled`` deployment with the proxy off — warned about at config
    load since ISSUE-393, and shipped — the marker is absent while the config
    directory is just as unreachable, and keying on it would ``FAIL`` a task
    with a remedy it cannot follow. ``_is_task_process`` is the right width.

    A task on an *unconfined* deployment reaches none of this: its exported path
    resolves and the gate opens. So the only cost of the wider predicate is that
    a genuinely broken install seen from inside a task reads as a ``SKIP``, and
    492a91e9 accepted exactly that trade for exactly this reason — every
    authoritative surface (the boot log, the interval sweep, the admin Health
    pane, the heartbeat) runs in the daemon, where no marker exists.

    Anything else that named a path it could not read is a ``FAIL``: a broken
    install, wrong permissions, a stale exported value, and a state nothing
    reported before. Naming nothing and finding nothing is a ``FAIL`` too, with
    its own reason and remedy — the exit code has to stay 1 there, because that
    invocation used to run 31 checks against defaults and exit 1 on the several
    that fail, and ``verdict``'s own docstring already draws this line for the
    empty list: a run that checked nothing must not read as a run that passed
    everything. Only the task arm exits 0, where a non-zero code every time a
    task runs doctor is noise about a boundary behaving correctly.

    **``--scope image`` is exempt**, and that is the one narrowing flag that is
    not just choosing which fiction to print. ``IMAGE`` is defined a few lines
    below ``OK, WARN, FAIL, SKIP`` as what "can be answered by a bare ``docker
    run`` with no volumes", which is precisely a host with no config on any
    search path; swallowing that scope would make a loaded config a precondition
    for the one scope declared not to need one. ``--only`` is *not* exempt: it
    selects by name across both scopes and carries no such declaration.

    **The caller renders this instead of the registry, rather than beside it.**
    Pushing three-state discipline down into every config-reading check would be
    faithful and would touch most of the 31; it would also leave a reader taking
    ``OK runtime.framework_db`` as a statement about the deployment, since one
    honest line among 31 fictional ones does not stop that. There is no subset
    of a config-less deployment-scoped run worth reading.

    The task arm's whole point has to live in ``detail``: ``render_text`` prints
    a remedy only for ``WARN`` and ``FAIL``, so a ``SKIP``'s is seen by
    ``--json`` and the admin pane and not by the operator or the model reading
    the terminal.

    Keyed on ``config.config_path``, which ``load_config`` sets to the file it
    actually opened, rather than on the environment: the environment says what
    was *asked for*, and a deployment can resolve a config without either
    variable being set. ``executor`` and ``scheduler`` already ask the same
    question the same way. Never raises — it runs on the CLI's own entry path,
    where a traceback replaces the diagnostic it was asked for.
    """
    if config.config_path is not None:
        return None
    if scope == IMAGE:
        return None

    # Branch on `source`, never on `named`: the rendered form of a whitespace
    # path collapses to empty, and reading that as "nothing was named" reports
    # the wrong cause with the wrong remedy for `-c "   "`.
    exported = os.environ.get("ISTOTA_CONFIG_PATH") or ""
    if requested is not None:
        source, named = "-c", _named_path(str(requested))
    elif exported:
        source, named = "ISTOTA_CONFIG_PATH", _named_path(exported)
    else:
        source, named = "", ""

    if _is_task_process():
        where = f" ({source} names {named}, which does not resolve here)" if source else ""
        return CheckResult(
            CONFIG_GATE,
            SKIP,
            (
                f"no config file was loaded{where}, and this process is a task "
                "rather than the daemon; the config directory is bound into no "
                "sandbox by design, so every check would answer about a default "
                "Config rather than about this deployment. Nothing else was run."
            ),
            remedy="Run `istota doctor` on the host as the daemon user.",
            scope=DEPLOYMENT,
        )

    if source:
        return CheckResult(
            CONFIG_GATE,
            FAIL,
            (
                f"{source} names {named or '(an empty path)'}, which does not "
                "resolve, so no config file was loaded and every check would "
                "answer about a default Config rather than about this "
                "deployment. Nothing else was run."
            ),
            remedy=(
                "Point it at the deployment's config.toml, or run this as a user "
                "that can read it."
            ),
            scope=DEPLOYMENT,
        )

    return CheckResult(
        CONFIG_GATE,
        FAIL,
        (
            "no config file was found in any of the standard locations, so every "
            "check would answer about a default Config rather than about a "
            "deployment. Nothing else was run."
        ),
        remedy=(
            "Pass `-c PATH`, or run this where one of `config/config.toml`, "
            "`~/.config/istota/config.toml` or `/etc/istota/config.toml` resolves."
        ),
        scope=DEPLOYMENT,
    )


def run_checks(
    config: "Config",
    *,
    only: tuple[str, ...] = (),
    skip: tuple[str, ...] = (),
    scope: str = "",
    deep: bool = False,
    live: bool = False,
    probe: bool = True,
) -> list[CheckResult]:
    """Run the registry, in order, and return every result.

    ``only`` selects by registry-name prefix (all checks when empty); ``skip``
    excludes by the same kind of prefix and wins over ``only``, for a caller
    that wants nearly everything. ``scope`` narrows to ``IMAGE`` or
    ``DEPLOYMENT``. ``deep`` opts into the checks that spawn a namespace, and
    ``live`` into the ones that reach a model and bill for it — two axes, not
    one, because no caller wants both from the same flag.
    ``probe=False`` forbids spawning anything: a check that would exec something
    answers from the filesystem alone and says so in its ``detail``.

    Every selector filters *before* invoking, so a narrowed run does not pay for
    the checks it discards.

    A check that raises is reported as ``FAIL`` with the exception text. Doctor
    never raises, because it runs on the daemon's start-up path — an exception
    there would turn a diagnostic into an outage.
    """
    results: list[CheckResult] = []
    for name, func in CHECKS:
        if name in DEEP_CHECKS and not deep:
            continue
        # Before invoking, like every other selector — a live check discarded
        # after the fact is a live check that already billed.
        if name in LIVE_CHECKS and not live:
            continue
        if only and not any(name.startswith(prefix) for prefix in only):
            continue
        if skip and any(name.startswith(prefix) for prefix in skip):
            continue
        if scope and CHECK_SCOPES.get(name) != scope:
            continue
        try:
            produced = func(config, probe)
        except Exception as exc:  # noqa: BLE001 - deliberate: see the docstring
            logger.debug("doctor check %s raised", name, exc_info=True)
            results.append(
                CheckResult(
                    name,
                    FAIL,
                    f"the check itself raised {type(exc).__name__}: {exc}",
                    remedy="This is a defect in the check, not necessarily in the deployment.",
                    scope=CHECK_SCOPES.get(name, DEPLOYMENT),
                )
            )
            continue
        produced_list = [produced] if isinstance(produced, CheckResult) else list(produced)
        results.extend(produced_list)
    return results


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def config_secrets(config: "Config") -> list[str]:
    """Every configured credential value, for the renderers' redaction pass.

    Reuses ``admin_config_view``'s field-level classification so there is one
    answer to "is this field a credential", rather than a second list here that
    drifts from the one the config page uses.
    """
    from .admin_config_view import is_secret_field

    found: list[str] = []

    def _consider(value, key: str, field_name: str, depth: int) -> None:
        """Classify one value, recursing through the containers config uses.

        Dicts and lists are traversed, not skipped: ``config.users`` is a
        ``dict[str, UserConfig]`` and every per-user credential lives under it,
        so a walk that only followed dataclass attributes would leave the
        largest group of secrets out of the redaction pass while claiming to
        reuse ``admin_config_view``'s classification.
        """
        if depth > 6:
            return
        if hasattr(value, "__dataclass_fields__"):
            _walk(value, f"{key}.", depth + 1)
            return
        if isinstance(value, dict):
            # A dict the classifier flags wholesale — `brain.native.extra_headers`
            # is the live case — has credential *contents* whatever its keys are
            # called. Harvest every string in it rather than re-asking about each
            # header name, or a spelling nobody anticipated is the one that
            # escapes redaction.
            if is_secret_field(key, field_name):
                for sub_value in value.values():
                    if isinstance(sub_value, str) and len(sub_value) >= _MIN_SECRET_LEN:
                        found.append(sub_value)
                return
            for sub_key, sub_value in value.items():
                # Otherwise a dict key is a name (a user id, a header name), so
                # classification travels with it as a field name would.
                _consider(sub_value, f"{key}.{sub_key}", str(sub_key), depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                _consider(item, key, field_name, depth + 1)
            return
        if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
            if is_secret_field(key, field_name):
                found.append(value)

    def _walk(obj, prefix: str, depth: int) -> None:
        if depth > 6:
            return
        for field_name in getattr(obj, "__dataclass_fields__", {}):
            value = getattr(obj, field_name, None)
            _consider(value, f"{prefix}{field_name}", field_name, depth)

    _walk(config, "", 0)
    return found


def _redact(text: str, secrets: Iterable[str] | None) -> str:
    """Replace every configured credential value in `text`.

    Check authors are forbidden from putting a credential in ``detail`` or
    ``remedy``. This does not take their word for it: ``detail`` carries
    observed paths and raw exception text, and both cross an HTTP boundary to
    the admin dashboard.
    """
    if not secrets:
        return text
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LEN and secret in text:
            text = text.replace(secret, _REDACTED)
    return text


def _redacted_results(
    results: list[CheckResult], secrets: Iterable[str] | None
) -> list[CheckResult]:
    if not secrets:
        return results
    secret_list = [s for s in secrets if s and len(s) >= _MIN_SECRET_LEN]
    if not secret_list:
        return results
    return [
        replace(
            r,
            detail=_redact(r.detail, secret_list),
            remedy=_redact(r.remedy, secret_list),
        )
        for r in results
    ]


def redact(results: list[CheckResult], config: "Config") -> list[CheckResult]:
    """Results with every configured credential value replaced.

    For the consumers that are not the renderers — the start-up log lines and
    the operator alert. Those cross boundaries too (a log file, a Talk room),
    and several checks interpolate raw exception text into ``detail``.
    """
    return _redacted_results(results, config_secrets(config))


def render_json(results: list[CheckResult], *, secrets: Iterable[str]) -> str:
    """A stable array of objects, for the image tests and the admin endpoint.

    Always valid JSON, including when checks failed — a machine consumer that
    has to distinguish "the run found problems" from "the run produced garbage"
    has already lost.

    ``secrets`` is required rather than defaulting to none, because this output
    crosses an HTTP boundary to the admin dashboard and a caller that simply
    forgot the argument would be fail-open. Pass ``config_secrets(config)``, or
    ``()`` to say deliberately that there is nothing to redact.
    """
    return json.dumps(check_payload(_redacted_results(results, secrets)), indent=2)


def check_payload(results: list[CheckResult]) -> list[dict]:
    """The wire shape of a result list — the one definition of it.

    Both the CLI's ``--json`` and the admin endpoint go through here, so a key
    added for the image test tier cannot reach one and miss the other. That
    divergence is invisible to tests that assert each surface against its own
    hardcoded dict, which is what they were doing.

    Does **not** redact: callers pass results that already have been. Redaction
    is not optional and so does not belong in a shape function, where an
    argument could be forgotten.
    """
    return [
        {
            "name": r.name,
            "status": r.status,
            "detail": r.detail,
            "remedy": r.remedy,
            "scope": r.scope,
        }
        for r in results
    ]


_STATUS_ORDER = {FAIL: 0, WARN: 1, OK: 2, SKIP: 3}


def render_text(results: list[CheckResult], *, secrets: Iterable[str]) -> str:
    """One line per check, grouped by prefix, with remedies indented beneath.

    Grouping is by the first dotted segment, in the order the registry produced
    them, so the output reads the same way twice running.

    ``secrets`` is required for the same reason as in :func:`render_json`:
    terminal output is where a pasted credential ends up in a bug report.
    """
    lines: list[str] = []
    current_group = ""
    for r in _redacted_results(results, secrets):
        group = r.name.split(".", 1)[0]
        if group != current_group:
            if lines:
                lines.append("")
            lines.append(f"{group}:")
            current_group = group
        lines.append(f"  {r.status.upper():<5} {r.name}  {r.detail}")
        if r.status in (WARN, FAIL) and r.remedy:
            lines.append(f"        -> {r.remedy}")
    return "\n".join(lines)


def exit_code(results: list[CheckResult]) -> int:
    """1 if any check failed, else 0. Warnings are not failures."""
    return 1 if any(r.status == FAIL for r in results) else 0


def summarize(results: list[CheckResult]) -> dict[str, int]:
    """Count by status — for a log line, and for the interval check's transition."""
    counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def verdict(results: list[CheckResult]) -> tuple[bool, str]:
    """``(healthy, one-line summary)`` — for a caller that reports pass/fail.

    ``healthy`` is False when any result is ``FAIL``, matching :func:`exit_code`
    exactly, so a caller reading the bool and a caller reading the int never
    disagree about the same deployment. A ``WARN`` is named in the summary and
    does not make the verdict unhealthy: a warning that pages someone is a
    failure wearing the wrong label. That is a deliberate change from
    ``heartbeat._check_self``, which appended its high-failure-rate finding to
    the same list as its real failures and so paged for it.

    Built on :func:`summarize`'s counts rather than re-walking the list, which
    is also what keeps the two in step.

    Named ``verdict`` rather than ``summarize`` because that name is taken and
    returns a different type, and rather than ``health`` because ``healthy`` is
    already the field it produces. On an empty list it says "no checks ran"
    rather than rendering four zeroes: a run that checked nothing must not read
    as a run that passed everything.
    """
    if not results:
        return True, "no checks ran"
    counts = summarize(results)
    summary = ", ".join(f"{counts.get(status, 0)} {status}" for status in (OK, WARN, FAIL, SKIP))
    return counts.get(FAIL, 0) == 0, summary


def failing(results: list[CheckResult]) -> list[CheckResult]:
    """Just the failures, sorted by name — what an alert names.

    By name rather than by severity: everything here is already ``FAIL``, so
    there is no severity left to order by, and a stable alphabetical order makes
    two alerts about the same set of problems read identically.
    """
    return sorted((r for r in results if r.status == FAIL), key=lambda r: r.name)
