"""Interactive first-run installer for the local single-user shape (``istota setup``).

Writes a working ``config.toml`` + secrets ``istota.env`` to the standard
config search path, initializes the DB, seeds the workspace, and prints next
steps. Idempotent and re-runnable (``--force`` to overwrite an existing
config); a non-interactive ``--yes`` mode takes defaults + flags for scripted
installs.

The wizard logic is split from I/O for testability: prompts go through an
injectable ``input_fn`` and ``claude`` detection through ``which_fn``; the
config/env renderers are pure functions over an ``Answers`` object.
"""

from __future__ import annotations

import getpass
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("istota.setup")


DEFAULT_WORKSPACE = "~/.istota"
DEFAULT_PORT = 8766
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "istota" / "config.toml"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"


@dataclass
class Answers:
    workspace: Path = field(default_factory=lambda: Path(DEFAULT_WORKSPACE).expanduser())
    user_id: str = "local"
    display_name: str = ""
    timezone: str = "UTC"
    web_port: int = DEFAULT_PORT
    brain_kind: str = "claude_code"          # "claude_code" | "native"
    native_base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    native_model: str = ""
    native_api_key: str = ""                 # written to the env file, never TOML
    email_enabled: bool = False
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""
    smtp_host: str = ""
    location_enabled: bool = False
    # Opt-out modules: installed, working with no extra setup, so on by default.
    money_enabled: bool = True
    health_enabled: bool = True
    feeds_enabled: bool = True
    briefings_enabled: bool = True
    #: An external CalDAV server, in place of the [nextcloud] derivation a
    #: standalone install has no Nextcloud for. All three blank => no [caldav].
    caldav_url: str = ""
    caldav_username: str = ""
    caldav_password: str = ""
    session_secret: str = ""                 # generated; written to the env file
    #: The secrets store's master Fernet key. Generated here and *preserved*
    #: across a ``--force`` re-run; see ``_carry_forward_secrets``.
    secret_key: str = ""
    #: ``web_tokens.py``'s separate Fernet key, for the ``web_user_tokens``
    #: rows under ``[web] token_storage = "encrypted"``. The wizard never
    #: *generates* one — that shape is opt-in and standalone does not use it —
    #: but it is carried forward when an existing env file has one, since the
    #: rewrite would otherwise delete a key with no recovery.
    web_token_key: str = ""
    #: Absolute path of the admins file, filled in by ``run_setup`` once the
    #: config directory is known. Named in the env file as ISTOTA_ADMINS_FILE.
    admins_file: str = ""

    @property
    def disabled_modules(self) -> list[str]:
        """Modules turned off in setup. Everything ships installed; a module is
        on unless listed here (mirrors the server's ``disabled_modules`` model).

        Four of ``modules.MODULE_NAMES``; ``location`` is deliberately absent
        because it is gated by its own ``[location] enabled`` key rather than
        by the module list, and putting it in both would give one answer two
        homes that can disagree.

        The set comes off ``_OPT_OUT_MODULES``, which is also what the prompts
        walk, so the two cannot drift; sorted rather than left in prompt order
        so two runs with the same answers render the same line, whatever order
        the questions end up being asked in.
        """
        return sorted(
            module
            for field_name, module, _question in _OPT_OUT_MODULES
            if not getattr(self, field_name)
        )

    @property
    def db_path(self) -> Path:
        # Keep the framework DB inside the workspace so the whole install is one
        # folder to back up / move; module DBs derive from db_path.parent.
        return self.workspace / "istota.db"

    @property
    def db_backup_dir(self) -> Path:
        return self.workspace / "db-backups"

    @property
    def temp_dir(self) -> Path:
        return self.workspace / "tmp"


# ---------------------------------------------------------------------------
# Pure renderers
# ---------------------------------------------------------------------------


def _toml_str(value: str) -> str:
    """TOML basic-string escaping: backslash, double-quote, control characters.

    The control-character arm is not decoration. TOML 1.0 forbids raw
    U+0000–U+0008, U+000A–U+001F and U+007F in a basic string, and every value
    reaching here came off a terminal prompt — where a *pasted* credential is
    the realistic carrier, since a line-oriented read cannot deliver a newline
    but happily delivers an ESC or a DEL. Emitting one produces a
    ``config.toml`` that will not parse, and the failure lands in
    ``_bootstrap``'s ``load_config`` *after* the config, the env file, the
    admins file and the database have all been written — as a bare
    ``TOMLDecodeError``, which ``cli.cmd_setup`` does not catch.

    Tab is left alone: TOML permits it raw, and escaping it would be a
    gratuitous difference from what the user typed.
    """
    out = [
        "\\\\" if ch == "\\"
        else '\\"' if ch == '"'
        else f"\\u{ord(ch):04x}" if (ch < " " or ch == "\x7f") and ch != "\t"
        else ch
        for ch in value
    ]
    return '"' + "".join(out) + '"'


def render_config_toml(a: Answers) -> str:
    """Render the local ``config.toml`` for these answers (pure)."""
    lines: list[str] = [
        "# Istota local single-user install — generated by `istota setup`.",
        "# Re-run `istota setup --force` to regenerate. Secrets live in the",
        "# sibling istota.env file, never here.",
        "",
        "bot_name = \"Istota\"",
        "emissaries_enabled = false  # constitutional principles doc; off for local single-user",
        f"db_path = {_toml_str(str(a.db_path))}",
        f"nextcloud_mount_path = {_toml_str(str(a.workspace))}",
        f"temp_dir = {_toml_str(str(a.temp_dir))}",
        "",
        "[web]",
        "enabled = true",
        "auth = \"none\"        # single-user local: no login (loopback bind only)",
        f"port = {a.web_port}",
        "",
        "[talk]",
        "enabled = false",
        "",
        "[email]",
        f"enabled = {'true' if a.email_enabled else 'false'}",
    ]
    if a.email_enabled:
        lines += [
            f"imap_host = {_toml_str(a.imap_host)}",
            f"imap_user = {_toml_str(a.imap_user)}",
            f"smtp_host = {_toml_str(a.smtp_host or a.imap_host)}",
            "# IMAP/SMTP passwords come from istota.env "
            "(ISTOTA_EMAIL_IMAP_PASSWORD / ISTOTA_EMAIL_SMTP_PASSWORD).",
        ]
    lines += [
        "",
        "[location]",
        f"enabled = {'true' if a.location_enabled else 'false'}",
    ]
    if a.caldav_url:
        lines += [
            "",
            "# An external CalDAV server (Radicale, Fastmail, Google) in place of",
            "# the [nextcloud] derivation, which a standalone install has no",
            "# Nextcloud for. Any field set here wins over that derivation.",
            "[caldav]",
            f"url = {_toml_str(a.caldav_url)}",
            f"username = {_toml_str(a.caldav_username)}",
            "# Password comes from istota.env (ISTOTA_CALDAV_PASSWORD).",
        ]
    lines += [
        "",
        "# Trusted single-user posture: no sandbox / proxies. "
        "See docs/getting-started/local-install.md.",
        "[security]",
        "sandbox_enabled = false",
        "skill_proxy_enabled = false",
        "",
        "[security.network]",
        "enabled = false",
        "",
        "[scheduler]",
        f"db_backup_dir = {_toml_str(str(a.db_backup_dir))}",
        "",
        "[brain]",
        f"kind = {_toml_str(a.brain_kind)}",
    ]
    if a.brain_kind == "native":
        lines += [
            "",
            "[brain.native]",
            f"base_url = {_toml_str(a.native_base_url)}",
            f"model = {_toml_str(a.native_model)}",
            "# API key comes from istota.env (ISTOTA_BRAIN_NATIVE_API_KEY).",
        ]
    lines += [
        "",
        f"[users.{a.user_id}]",
        f"display_name = {_toml_str(a.display_name or a.user_id)}",
        f"timezone = {_toml_str(a.timezone)}",
    ]
    if a.disabled_modules:
        rendered = ", ".join(_toml_str(m) for m in a.disabled_modules)
        lines.append(f"disabled_modules = [{rendered}]")
    lines.append("")
    return "\n".join(lines)


def render_env_file(a: Answers) -> str:
    """Render the sibling secrets ``istota.env`` (pure)."""
    lines = [
        "# Istota local secrets — generated by `istota setup`. Sourced by",
        "# `istota serve`. Keep this file private (chmod 600).",
        "",
        "# Master key for the encrypted secrets store (Garmin, Monarch, ntfy,",
        "# Google Workspace tokens, …). Everything stored is encrypted with it,",
        "# so replacing it makes every stored credential permanently",
        "# unreadable. `istota setup --force` preserves whatever is here.",
        f"ISTOTA_SECRET_KEY={a.secret_key}",
        "",
        "# Web runs over plain http on loopback; sessions are unused in no-auth mode.",
        "ISTOTA_WEB_INSECURE_COOKIES=1",
        f"ISTOTA_WEB_SESSION_SECRET_KEY={a.session_secret}",
    ]
    if a.web_token_key:
        # Never generated here — only carried forward from an existing file, so
        # a re-run cannot delete a key the web token store depends on.
        lines += [
            "",
            "# Separate key for encrypted web user tokens ([web] token_storage).",
            f"ISTOTA_WEB_TOKEN_KEY={a.web_token_key}",
        ]
    if a.admins_file:
        lines += [
            "",
            "# Who may write shared content (shared briefing blocks) and reach the",
            "# admin dashboard. One user id per line; # comments.",
            f"ISTOTA_ADMINS_FILE={a.admins_file}",
        ]
    if a.brain_kind == "native" and a.native_api_key:
        lines.append(f"ISTOTA_BRAIN_NATIVE_API_KEY={a.native_api_key}")
    if a.email_enabled and a.imap_password:
        lines.append(f"ISTOTA_EMAIL_IMAP_PASSWORD={a.imap_password}")
        lines.append(f"ISTOTA_EMAIL_SMTP_PASSWORD={a.imap_password}")
    if a.caldav_url and a.caldav_password:
        lines += [
            "",
            "# External CalDAV server ([caldav] in config.toml holds the url and",
            "# username; only the password lives here).",
            f"ISTOTA_CALDAV_PASSWORD={a.caldav_password}",
        ]
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interactive collection
# ---------------------------------------------------------------------------


def _ask(input_fn, prompt: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input_fn(f"{prompt}{suffix}: ").strip()
    return raw or default


def _ask_yes_no(input_fn, prompt: str, default: bool, *, out=None, attempts: int = 3) -> bool:
    """A yes/no prompt. Empty takes the default; an unrecognised answer re-asks.

    Re-asking rather than reading anything unrecognised as "no", which is what
    this did. On a default-*No* prompt that reads as the default and so is
    invisible; on a default-*Yes* one it flips **away** from the answer the
    ``[Y/n]`` it just printed promised, so ``1``, ``true`` or ``yeah``
    silently disabled a module and wrote it into both the TOML and the profile
    row. The opt-out module prompts made that four questions rather than one.

    Bounded, and the last attempt takes the default rather than looping: a
    non-interactive ``input_fn`` that keeps returning the same value would
    otherwise spin forever, and a wizard that cannot be exited is worse than
    one that falls back to the answer it already printed.
    """
    d = "Y/n" if default else "y/N"
    for attempt in range(attempts):
        raw = input_fn(f"{prompt} [{d}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        if out is not None and attempt < attempts - 1:
            out(f"  Please answer y or n (Enter takes {'yes' if default else 'no'}).")
    return default


def _ask_port(input_fn, prompt: str, default: int, *, out=None, attempts: int = 3) -> int:
    """A port prompt. Empty takes the default; anything unusable re-asks.

    ``int(_ask(...))`` was the whole of this, so a typo, a pasted URL or an
    answer given one prompt out of step raised ``ValueError`` out of
    ``collect_answers``. Nothing is half-written when that happens — it is
    ahead of every write — but a first-run installer ending in a stack trace
    is a poor advertisement for the thing being installed, and this is the
    only other prompt that parses its answer. ``_ask_yes_no`` took the same
    treatment for the same class of input.

    The range is checked as well as the syntax, because the value reaches a
    bind: ``0`` and ``70000`` both parse and both fail later, at ``istota
    serve``, a long way from the question that produced them.

    Bounded rather than looping, exactly as ``_ask_yes_no`` is: a
    non-interactive ``input_fn`` that keeps returning the same value would
    otherwise spin forever, and the default is a working answer.
    """
    for attempt in range(attempts):
        raw = _ask(input_fn, prompt, str(default))
        try:
            port = int(raw)
        except (TypeError, ValueError):
            reason = f"'{raw}' is not a whole number"
        else:
            if 1 <= port <= 65535:
                return port
            reason = f"{port} is not a usable port"
        if out is not None and attempt < attempts - 1:
            out(f"  {reason}; enter a number between 1 and 65535 (Enter takes {default}).")
    if out is not None:
        out(f"  Falling back to {default}.")
    return default


def _flush_terminal_input() -> None:
    """Discard any pending terminal input before a secret prompt.

    A pasted value (e.g. the model id) can leave stray newlines queued in the
    terminal's input buffer; without this they auto-answer the next prompt with
    an empty line. Best-effort — a no-op on a non-tty / non-POSIX stdin.
    """
    try:
        import sys
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:  # pragma: no cover - platform / non-tty dependent
        pass


#: The re-prompt tail for the native brain's API key. A parameter rather than a
#: literal in the loop because every secret the wizard reads is read here now,
#: and "re-run with --native-api-key" is wrong advice for the other two.
_NATIVE_KEY_HINT = (
    "for the native brain — please enter it "
    "(or Ctrl-C and re-run with --native-api-key)."
)


def _read_secret(
    getpass_fn, label: str, out, *, attempts: int = 3, hint: str = _NATIVE_KEY_HINT,
) -> str:
    """Read a required secret from the terminal (no echo), re-prompting if empty.

    Flushes buffered terminal input first so a stray newline can't silently
    accept an empty value, and reads via ``getpass_fn`` so the secret isn't
    echoed. Returns "" only if the user gives up (empty every attempt, or
    EOF); the caller's validation then surfaces the clear "no API key" error.

    **``KeyboardInterrupt`` propagates**, and is the difference between the
    hint below being true and being a lie. Swallowing it returned "" and the
    wizard carried on to write a complete install around a blank credential:
    for the IMAP password that was a regression, since the plain ``input()``
    this replaced let Ctrl-C out to ``cli.cmd_setup``, which catches it and
    exits 1 with "Setup cancelled". EOF is still swallowed, because the two
    mean different things — Ctrl-C is "stop", a closed stdin is "there is no
    more input", which is the give-up case this function's retry is for.
    """
    _flush_terminal_input()
    for attempt in range(attempts):
        try:
            value = getpass_fn(f"{label}: ").strip()
        except EOFError:
            return ""
        if value:
            return value
        if attempt < attempts - 1:
            out(f"{label} is required {hint}")
    return ""


def _is_valid_timezone(name: str) -> bool:
    """True if ``name`` is a zone ``ZoneInfo`` accepts (a real IANA name).

    Abbreviations like ``PDT`` / ``EST`` are NOT valid — ``ZoneInfo`` only
    knows names like ``America/Los_Angeles``. Storing an abbreviation makes
    the executor's ``_resolve_user_tz`` silently fall back to UTC.
    """
    if not name:
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:
        return False


def _default_timezone() -> str:
    """Best-effort IANA zone name for the host.

    ``datetime.now().astimezone().tzinfo`` is a fixed-offset ``datetime.timezone``
    on many systems (notably macOS), whose ``str()`` is an abbreviation like
    ``PDT`` — not a name ``ZoneInfo`` accepts. Resolve the real IANA name from
    ``TZ`` or the ``/etc/localtime`` symlink (which points into the zoneinfo
    tree on both Linux and macOS), validating every candidate before returning.
    """
    # 1. TZ env var, if it names a real zone.
    tz_env = os.environ.get("TZ", "").strip()
    if _is_valid_timezone(tz_env):
        return tz_env

    # 2. /etc/localtime symlink → .../zoneinfo/<Area>/<Location>.
    try:
        real = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in real:
            name = real.split("zoneinfo/", 1)[1]
            if _is_valid_timezone(name):
                return name
    except Exception:  # pragma: no cover - defensive
        pass

    # 3. A ZoneInfo-backed local tzinfo (Linux where astimezone yields one).
    try:
        from datetime import datetime

        key = getattr(datetime.now().astimezone().tzinfo, "key", None)
        if key and _is_valid_timezone(key):
            return key
    except Exception:  # pragma: no cover - defensive
        pass

    return "UTC"


def collect_answers(args, *, input_fn, which_fn, out, getpass_fn, prior_caldav=None) -> Answers:
    """Build an ``Answers`` from flags + (unless ``--yes``) interactive prompts.

    ``prior_caldav`` is the ``[caldav]`` block already in the config this run
    is about to overwrite, if any; see :func:`_collect_caldav`.
    """
    interactive = not getattr(args, "yes", False)
    a = Answers()

    # 1. Workspace
    ws = getattr(args, "workspace", None)
    if not ws and interactive:
        ws = _ask(input_fn, "Workspace directory", DEFAULT_WORKSPACE)
    a.workspace = Path(ws or DEFAULT_WORKSPACE).expanduser().resolve()

    # 2. Brain
    _collect_brain(
        a, args, interactive=interactive, input_fn=input_fn,
        which_fn=which_fn, out=out, getpass_fn=getpass_fn,
    )

    # 3. User identity
    os_user = getpass.getuser() or "local"
    uid = getattr(args, "user", None)
    if not uid and interactive:
        uid = _ask(input_fn, "User id", os_user)
    a.user_id = uid or os_user
    dn = getattr(args, "display_name", None)
    if not dn and interactive:
        dn = _ask(input_fn, "Display name", a.user_id)
    a.display_name = dn or a.user_id
    tz_default = _default_timezone()
    tz = getattr(args, "timezone", None)
    if not tz and interactive:
        tz = _ask(input_fn, "Timezone", tz_default)
    tz = tz or tz_default
    # An abbreviation ("PDT") or typo isn't a name ZoneInfo accepts; storing it
    # makes every task's clock silently fall back to UTC. Reject it up front.
    if not _is_valid_timezone(tz):
        out(
            f"  '{tz}' is not a valid IANA timezone (use e.g. America/Los_Angeles);"
            f" falling back to {tz_default if _is_valid_timezone(tz_default) else 'UTC'}."
        )
        tz = tz_default if _is_valid_timezone(tz_default) else "UTC"
    a.timezone = tz

    # 4. Web port. `--port` is `type=int` at the parser, so only the prompt
    # can carry something unparseable — see `_ask_port`.
    port = getattr(args, "port", None)
    if port is None and interactive:
        port = _ask_port(input_fn, "Web port", DEFAULT_PORT, out=out)
    a.web_port = int(port or DEFAULT_PORT)

    # 5. Modules & surfaces. Everything ships installed; here we only choose
    # what's *enabled*. The default follows a simple rule: on when a module works
    # with no extra setup (money), off when it needs external configuration
    # (location webhooks, email credentials). Grouped so the "which pieces"
    # decisions live in one place instead of being split across the installer.

    # Location (GPS tracking) — off unless asked; it needs an Overland ingest
    # token to actually receive pings, so an enabled-but-unconfigured tab is empty.
    if getattr(args, "location", False):
        a.location_enabled = True
    elif interactive:
        a.location_enabled = _ask_yes_no(
            input_fn, "Enable GPS/location tracking?", False, out=out,
        )

    # The rest of `modules.MODULE_NAMES`: each is installed and works out of the
    # box, so each is on by default and the prompt is an opt-out, matching the
    # server's module model. `location` is not in this loop — it is gated by
    # `[location] enabled` above rather than by `disabled_modules`.
    _collect_modules(a, args, interactive=interactive, input_fn=input_fn, out=out)

    # Email (IMAP/SMTP) — off unless asked; needs credentials to do anything.
    if getattr(args, "email", False):
        a.email_enabled = True
    elif interactive:
        a.email_enabled = _ask_yes_no(
            input_fn, "Enable email (IMAP/SMTP)?", False, out=out,
        )
    if a.email_enabled:
        if interactive:
            a.imap_host = _ask(input_fn, "IMAP host", a.imap_host)
            a.imap_user = _ask(input_fn, "IMAP user", a.imap_user)
            # Asked rather than forced equal to the IMAP host: a submission
            # service on another hostname is the ordinary case, and defaulting
            # to the IMAP host keeps the common one a single keystroke.
            a.smtp_host = _ask(input_fn, "SMTP host", a.imap_host)
            # Read last, and through the same no-echo reader as the API key:
            # `input_fn` echoes it to the terminal and leaves it in shell
            # history when the wizard is driven from a pipe.
            a.imap_password = _read_secret(
                getpass_fn, "IMAP password", out,
                hint="— please enter it, or Ctrl-C to abort.",
            )
        else:
            a.smtp_host = a.smtp_host or a.imap_host

    _collect_caldav(
        a, prior_caldav or {}, interactive=interactive, input_fn=input_fn,
        getpass_fn=getpass_fn, out=out,
    )

    # Stable session secret so restarts don't invalidate cookies (unused in
    # no-auth but written for cleanliness / any residual cookie use).
    a.session_secret = secrets.token_hex(32)
    # Master key for the encrypted secrets store. 64 hex chars, comfortably
    # over secrets_store._MIN_KEY_LEN. Both of these are replaced by whatever
    # an existing istota.env already holds; see `_carry_forward_secrets`.
    a.secret_key = secrets.token_hex(32)
    return a


#: The opt-out modules, in the order they are asked about and rendered:
#: ``(Answers field, module name, question)``. ``location`` is absent — see
#: ``Answers.disabled_modules``.
_OPT_OUT_MODULES: tuple[tuple[str, str, str], ...] = (
    ("money_enabled", "money", "Enable the money module (double-entry accounting)?"),
    ("health_enabled", "health", "Enable the health module (body stats, bloodwork, documents)?"),
    ("feeds_enabled", "feeds", "Enable the feeds module (RSS/Atom/Tumblr reader)?"),
    ("briefings_enabled", "briefings", "Enable the briefings module (scheduled digests)?"),
)


def read_existing_caldav(config_path: Path, env_path: Path | None = None) -> dict[str, str]:
    """The ``[caldav]`` settings an existing install already holds.

    **Two files, because the block is split across two by design.** The url and
    the username are ordinary config and live in ``config.toml``; the password
    is a credential and lives in the sibling ``istota.env`` as
    ``ISTOTA_CALDAV_PASSWORD``, which ``load_config``'s ``_env_secret_overrides``
    table resolves onto ``caldav.password`` — the same channel every other
    credential in the tree uses. Reading only the TOML would recover a server
    it has no password for, and ``_collect_caldav`` would then write a
    ``[caldav]`` block that cannot authenticate.

    The environment outranks the env file, matching ``_carry_forward_secrets``
    and, behind it, ``serve.load_env_file``'s non-clobbering resolution: an
    exported value is the one the daemon is actually using.

    A password still sitting in the TOML is honoured as a last resort. That is
    a migration path rather than a supported location — ``render_config_toml``
    no longer writes one — and without it a hand-written ``[caldav]`` block,
    which is what the docs told standalone users to add, would lose its
    password to the first ``--force``.

    Best-effort throughout: an absent, unreadable or unparseable file is no
    values, since the caller's fallback is to ask. Non-string members are
    coerced, because this is read from a file a person may have edited by hand.
    """
    import tomllib  # noqa: PLC0415 - only this path needs it

    try:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    block = data.get("caldav")
    if not isinstance(block, dict):
        return {}
    found = {key: str(block.get(key) or "") for key in ("url", "username", "password")}

    from_env = os.environ.get("ISTOTA_CALDAV_PASSWORD", "").strip()
    from_file = ""
    if env_path is not None:
        from_file = _read_env_values(env_path).get("ISTOTA_CALDAV_PASSWORD", "").strip()
    if from_env or from_file:
        found["password"] = from_env or from_file
    return found


def _collect_caldav(a: Answers, prior, *, interactive, input_fn, getpass_fn, out) -> None:
    """Decide the ``[caldav]`` block: keep the existing one, set one up, or none.

    The section exists for exactly this install shape and no generator has ever
    offered it, so a standalone user has had to add it by hand. Opt-in, since
    without it calendar derives from ``[nextcloud]``, which a standalone
    install does not have.

    **A re-run over an install that already has one asks a different question,
    and that is a data-loss fix rather than a nicety.** ``--force`` rewrites
    ``config.toml`` wholesale and ``render_config_toml`` is a pure function of
    ``Answers``, so a run that collected nothing emits no block — and because
    the password's only home is that file (there is no ``[caldav]`` entry in
    ``load_config``'s ``_env_secret_overrides`` table), dropping the block
    destroys a credential with no copy anywhere else. ``_carry_forward_secrets``
    exists for precisely this hazard on the env file; this is the same rule for
    the one secret that does not live there. So the question becomes "keep the
    one you have", defaulting to yes, and declining it falls through to setting
    up a different server — which is what makes the block editable rather than
    merely preserved.

    Non-interactively there is nothing to ask and no flag to answer with, so an
    existing block is carried forward unconditionally. ``--yes`` resetting a
    *preference* to its default is what ``--yes`` means; silently deleting a
    credential is not.

    A URL with no password is not written at all. It would override the
    ``[nextcloud]`` derivation with something that cannot authenticate, which
    breaks calendar more thoroughly than leaving the section out.
    """
    prior_url = (prior or {}).get("url", "")
    keep = bool(prior_url)
    if keep and interactive:
        keep = _ask_yes_no(
            input_fn, f"Keep the configured CalDAV server ({prior_url})?", True, out=out,
        )
    if keep:
        a.caldav_url = prior_url
        a.caldav_username = prior.get("username", "")
        a.caldav_password = prior.get("password", "")
        return

    asked = interactive and _ask_yes_no(
        input_fn, "Point calendar at an external CalDAV server?", False, out=out,
    )
    if asked:
        a.caldav_url = _ask(input_fn, "CalDAV URL", "")
        if a.caldav_url:
            a.caldav_username = _ask(input_fn, "CalDAV username", "")
            a.caldav_password = _read_secret(
                getpass_fn, "CalDAV password", out,
                hint="— please enter it, or Ctrl-C to abort.",
            )
            if not a.caldav_password:
                out(
                    "  No CalDAV password given; leaving [caldav] out rather "
                    "than writing one that cannot authenticate."
                )
                a.caldav_url = ""
                a.caldav_username = ""
        else:
            out("  No CalDAV URL given; leaving [caldav] out of the config.")
    if prior_url and not a.caldav_url:
        out(
            f"  Dropping the existing [caldav] block ({prior_url}); the stored "
            f"password goes with it."
        )


def _collect_modules(a: Answers, args, *, interactive, input_fn, out) -> None:
    """Ask about each opt-out module, honouring its ``--no-<name>`` flag.

    **A module whose install extra is missing is neither asked about nor
    recorded**, and the second half is the one that matters. ``money`` is the
    only entry in ``modules.MODULE_DEPENDENCIES`` today; without ``beancount``
    ``module_available()`` already hides it everywhere, so the prompt would be
    a question with no good answer — and writing ``money`` into
    ``disabled_modules`` because of it would turn a transient install state
    into a stored decision that survives a later ``uv tool install
    'istota[money]'``, with nothing on the machine saying why the module is
    still dark. The explicit flag still wins, because that is a decision the
    operator made rather than one derived from the environment.
    """
    from .modules import module_available  # noqa: PLC0415 - keep the import graph lean

    for field_name, module, question in _OPT_OUT_MODULES:
        if getattr(args, f"no_{module}", False):
            setattr(a, field_name, False)
            continue
        if not module_available(module):
            out(
                f"  The {module} module's optional dependencies are not "
                f"installed; leaving it out of setup (it stays hidden until "
                f"they are)."
            )
            continue
        if interactive:
            setattr(a, field_name, _ask_yes_no(input_fn, question, True, out=out))


def _collect_brain(a: Answers, args, *, interactive, input_fn, which_fn, out, getpass_fn) -> None:
    """Pick the model backend. Flags win; else detect ``claude`` and offer it."""
    forced = getattr(args, "brain", None)
    if forced in ("claude_code", "native"):
        a.brain_kind = forced
        if forced == "native":
            _collect_native(
                a, args, interactive=interactive, input_fn=input_fn,
                getpass_fn=getpass_fn, out=out,
            )
        return

    claude_path = which_fn("claude")
    if not interactive:
        # Non-interactive: prefer claude if present, else require native + key.
        if claude_path:
            a.brain_kind = "claude_code"
        else:
            a.brain_kind = "native"
            _collect_native(
                a, args, interactive=False, input_fn=input_fn,
                getpass_fn=getpass_fn, out=out,
            )
        return

    if claude_path:
        use_it = _ask_yes_no(
            input_fn,
            "Detected the Claude CLI. Use your Claude Code subscription for the "
            "model backend?",
            True,
        )
        if use_it:
            a.brain_kind = "claude_code"
            out(
                "Using the Claude CLI. (If it isn't logged in yet, run `claude` "
                "once to authenticate.)"
            )
            return
    else:
        out("No Claude CLI detected on PATH.")

    # Fall to native.
    a.brain_kind = "native"
    _collect_native(
        a, args, interactive=interactive, input_fn=input_fn,
        getpass_fn=getpass_fn, out=out,
    )


def _collect_native(a: Answers, args, *, interactive, input_fn, getpass_fn, out) -> None:
    base = getattr(args, "native_base_url", None)
    model = getattr(args, "native_model", None)
    key = getattr(args, "native_api_key", None) or os.environ.get("ISTOTA_BRAIN_NATIVE_API_KEY", "")
    if interactive:
        base = base or _ask(input_fn, "API base URL", DEFAULT_ANTHROPIC_BASE_URL)
        model = model or _ask(input_fn, "Model id", "claude-sonnet-4-6")
        if not key:
            # Read as a secret (no echo) and re-prompt on empty — a stray newline
            # from the pasted model id must not silently leave the key blank.
            key = _read_secret(getpass_fn, "API key", out)
    a.native_base_url = base or DEFAULT_ANTHROPIC_BASE_URL
    a.native_model = model or ""
    a.native_api_key = key or ""


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class SetupError(RuntimeError):
    """A setup-blocking condition, reported to the user."""


def _read_env_values(path: Path) -> dict[str, str]:
    """Parse an existing ``istota.env`` into a mapping.

    Mirrors ``serve.load_env_file``'s grammar (``export`` prefix, ``#``
    comments, optional quoting) so a file written by hand and sourced at boot
    is read the same way here. Best-effort: an unreadable or non-UTF-8 file is
    no values, since the caller's fallback is to generate a fresh key.

    **First occurrence wins, and that is load-bearing rather than arbitrary.**
    ``load_env_file`` skips a name already in ``os.environ``, so the first line
    for a name is the one the daemon ends up using and every later line is
    dead. A last-wins parse here would read a duplicated ``ISTOTA_SECRET_KEY``
    — the exact shape an operator produces by appending the line a remedy told
    them to add to a file that already had one — as the *other* key, and the
    caller would then rewrite the file with it, orphaning everything encrypted
    under the one the daemon was actually using.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            values.setdefault(key, value.strip().strip('"').strip("'"))
    return values


def _carry_forward_secrets(a: Answers, env_path: Path, out=print) -> None:
    """Keep the generated secrets an existing install already holds.

    ``--force`` rewrites the whole file, and for a Fernet key that is a
    destructive operation rather than a regeneration: everything encrypted
    under the old one is orphaned with no way back. Docker draws the same line
    — ``entrypoint.sh`` generates once into ``/data/.secret_key`` and never
    overwrites.

    Three names, and the first two are keys with no recovery.
    ``ISTOTA_SECRET_KEY`` is the secrets store's master key (Garmin, Monarch,
    ntfy, the Google Workspace tokens). ``ISTOTA_WEB_TOKEN_KEY`` is a
    *separate* key with identical semantics — ``web_tokens.py`` derives its own
    Fernet from it for the ``web_user_tokens`` rows under ``[web]
    token_storage = "encrypted"`` — and the wizard has never written it, so it
    is only ever here because an operator added it and would be deleted by the
    rewrite. ``ISTOTA_WEB_SESSION_SECRET_KEY`` is carried for consistency
    rather than for harm: losing it only invalidates cookies. The rule is the
    same for all three — a re-run is a config rewrite, not a key rotation.

    **The environment outranks the file, because that is the order the daemon
    resolves them in.** ``serve.load_env_file`` is non-clobbering, so an
    exported value wins over the file's and is the key actually in use;
    preserving the file's instead would write a value the daemon ignores, and
    the install would silently switch keys the day the export went away.

    Only a *usable* value is preserved. A blank or truncated line is the broken
    state this fixes, and pinning it would make the guard the bug — safe to
    discard, since nothing below the floor can have encrypted anything. The
    floor is the secrets store's own, never a second copy of it.

    What it deliberately does **not** do is preserve arbitrary unrecognised
    lines. ``render_env_file`` is a pure function of ``Answers``, and carrying
    unknown names through would resurrect variables the wizard has stopped
    writing; the file's own comment says only that these are preserved.
    """
    from .secrets_store import _MIN_KEY_LEN

    existing = _read_env_values(env_path)

    def resolve(var: str) -> str:
        from_env = os.environ.get(var, "").strip()
        from_file = existing.get(var, "").strip()
        if from_env and from_file and from_env != from_file:
            # Not a failure — the daemon has an unambiguous answer — but the
            # operator is about to have the losing value deleted, so say which
            # one survived. Names only, never values.
            out(
                f"  {var} differs between your environment and {env_path};"
                f" keeping the environment's, which is the one the daemon uses."
            )
        return from_env or from_file

    for field_name, var in (
        ("secret_key", "ISTOTA_SECRET_KEY"),
        ("web_token_key", "ISTOTA_WEB_TOKEN_KEY"),
        ("session_secret", "ISTOTA_WEB_SESSION_SECRET_KEY"),
    ):
        prior = resolve(var)
        if len(prior) >= _MIN_KEY_LEN:
            setattr(a, field_name, prior)

    # The CalDAV password is deliberately *not* here, even though it is carried
    # across a re-run for the same reason. It travels with the url and the
    # username in `read_existing_caldav`, because `_collect_caldav` has to
    # decide all three together — a block kept with no password recovered would
    # name a server it cannot authenticate to — and that decision is made
    # inside `collect_answers`, which runs before this does.


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, never visible at a wider mode than 0600.

    ``write_text`` then ``chmod`` leaves the file world-readable with secrets
    in it for the interval between, which is a window on a multi-user host.

    Two narrowing steps, because ``O_CREAT``'s mode applies **only** to a file
    this call creates. On the ``--force`` re-run — the one path where the file
    pre-exists, and the path this change adds — the mode argument is ignored
    entirely, so an ``istota.env`` sitting at 0644 (hand-made, or written by a
    wizard older than this) would be truncated and refilled with the master key
    at its original mode and only narrowed afterwards: the same window, on the
    only shape that has it. ``fchmod`` on the open descriptor closes it before
    any byte is written. The ``path.chmod`` after is the fallback for a
    platform without ``fchmod``.

    UTF-8 explicitly: the rendered text carries an em dash, and ``os.fdopen``
    would otherwise encode with the locale's codec and raise under ``LC_ALL=C``
    *after* ``O_TRUNC`` had already emptied the file.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass


def _ensure_admins_file(path: Path, user_id: str, out=print) -> None:
    """Create ``path`` naming ``user_id``, and never modify one that exists.

    An empty allowlist is read as "everyone is admin" by ``Config.is_admin``
    and as "nobody" by ``is_shared_kv_writer`` and the web admin gate, so a
    standalone install with no file at all could not write a shared briefing
    block. This gives it a real, editable authorization artifact instead of
    leaning on the exemption in ``is_shared_kv_writer``.

    **It only ever creates.** An existing file is left byte for byte alone,
    even when it does not name the user — appending would be a silent
    authorization widening, and the path is derived from ``config_path.parent``
    with nothing asserting the standalone shape, so ``istota setup -c
    /etc/istota/config.toml --force`` would append the wizard's user to the
    *server's* production allowlist (``load_admin_users`` defaults to exactly
    ``/etc/istota/admins``). That widening would survive the operator restoring
    ``config.toml`` from Ansible, since the play manages the two files
    separately. Refusing costs a standalone user nothing they cannot fix in one
    edit, and the line printed here tells them what to add.

    Membership is asked of ``load_admin_users`` rather than re-parsed, so the
    writer and the reader of this file cannot drift on what a line means.
    """
    if path.exists():
        from .config import load_admin_users  # noqa: PLC0415

        if user_id not in load_admin_users(str(path)):
            out(
                f"  {path} exists and does not name '{user_id}'; leaving it "
                f"untouched. Add that line yourself to allow shared-content "
                f"writes and the admin dashboard."
            )
        return
    path.write_text(
        "# Istota admin user ids - one per line, # comments.\n"
        "# Written by `istota setup`; edit freely.\n"
        f"{user_id}\n",
        encoding="utf-8",
    )


def _validate(a: Answers) -> None:
    if a.brain_kind == "native":
        if not a.native_model:
            raise SetupError(
                "Native brain selected but no model was given. Re-run with "
                "--brain native --native-model <id> --native-api-key <key>."
            )
        if not a.native_api_key:
            raise SetupError(
                "Native brain selected but no API key was given (set "
                "ISTOTA_BRAIN_NATIVE_API_KEY or pass --native-api-key)."
            )


def run_setup(args, *, input_fn=input, which_fn=None, out=print, getpass_fn=None) -> int:
    """Run the setup wizard. Returns a process exit code (0 = success)."""
    import shutil as _shutil

    if which_fn is None:
        which_fn = _shutil.which
    if getpass_fn is None:
        # getpass reads from /dev/tty with echo off — the right way to collect a
        # secret, and robust in the curl-pipe / reattached-stdin install path.
        getpass_fn = getpass.getpass

    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else DEFAULT_CONFIG_PATH
    env_path = config_path.parent / "istota.env"

    # Clobber guard.
    if config_path.exists() and not getattr(args, "force", False):
        if getattr(args, "yes", False):
            raise SetupError(
                f"A config already exists at {config_path}. Re-run with --force "
                "to overwrite it."
            )
        update = _ask_yes_no(
            input_fn, f"A config already exists at {config_path}. Update it in place?", False,
        )
        if not update:
            out("Setup aborted; existing config left untouched.")
            return 1

    # Read before the rewrite: both files this run is about to replace are the
    # only copies of the [caldav] settings, and they are split across the two —
    # url and username in the TOML, password in istota.env.
    a = collect_answers(
        args, input_fn=input_fn, which_fn=which_fn, out=out, getpass_fn=getpass_fn,
        prior_caldav=read_existing_caldav(config_path, env_path),
    )
    _validate(a)

    admins_path = config_path.parent / "admins"
    a.admins_file = str(admins_path)
    # A re-run must not replace the master key: every stored credential is
    # encrypted under it. Read before anything is written.
    _carry_forward_secrets(a, env_path, out=out)

    # Create workspace + config dirs.
    a.workspace.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Write config + env. The env file is created 0600 rather than chmod'd
    # afterwards, so it is never on disk world-readable holding secrets.
    #
    # config.toml holds no credential at all and stays at the umask default, so
    # an operator can read it without sudo. It used to take a private write on
    # the one shape that carried a [caldav] password; that password now has an
    # `_env_secret_overrides` row and goes to istota.env with the rest, so the
    # exception is gone rather than merely unused — one rule, not two.
    config_path.write_text(render_config_toml(a), encoding="utf-8")
    _write_private(env_path, render_env_file(a))
    _ensure_admins_file(admins_path, a.user_id, out=out)

    # Bootstrap: DB, user profile row, workspace directories + memory.
    config = _bootstrap(a, config_path)

    # Confirm the install works rather than only that it was written. After the
    # bootstrap, because half of what doctor reads is what the bootstrap made.
    _run_self_check(config, config_path, out)

    _print_next_steps(a, config_path, out)
    return 0


def _bootstrap(a: Answers, config_path: Path):
    """Initialize the DB, upsert the user profile, seed the workspace.

    Returns the freshly-loaded ``Config``, so the closing self-check reads the
    same object this function already paid to load rather than loading it a
    second time.
    """
    from . import db
    from . import user_profiles
    from .config import load_config
    from .storage import ensure_workspace_for_user

    a.db_path.parent.mkdir(parents=True, exist_ok=True)
    # Both of these are named in the config this run just wrote and were
    # created by nothing: the scheduler's backup pass and the executor's
    # per-task control tree each make their own on first use, so a fresh
    # install had two configured paths that did not exist — which reads as a
    # broken install to an operator and to `doctor`'s writable-dirs checks.
    #
    # 0700 rather than the umask default, for what they hold. `temp_dir` is the
    # parent of `.control/{user}/task_{id}/`, whose own 0700 `execute_task`
    # sets on the three levels it creates but not on this one; it also holds
    # every task's prepared attachment renditions. `db_backup_dir` holds whole
    # database snapshots, and `db_backup` writes 0700/0600 for that reason.
    # `mkdir(mode=...)` is masked by the umask and ignored outright when the
    # directory exists, so the mode is set explicitly afterwards — but never
    # through a symlink. `mkdir(exist_ok=True)` succeeds on one pointing at a
    # directory and `Path.chmod` follows it, so a re-run over an install where
    # the operator symlinked `db-backups` at a shared volume would silently
    # narrow that volume instead. The surrounding code is careful about this
    # in the same way (`_write_private`'s fchmod, `execute_task`'s O_NOFOLLOW).
    for directory in (a.temp_dir, a.db_backup_dir):
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            continue
        try:
            directory.chmod(0o700)
        except OSError:  # pragma: no cover - platform / ownership dependent
            pass
    db.init_db(a.db_path)

    user_profiles.ensure_profile(
        a.db_path, a.user_id, display_name=a.display_name, timezone=a.timezone,
    )
    # disabled_modules must land on the profile row too: is_module_enabled reads
    # the DB row before the TOML [users.X] block, so the row is the effective one.
    user_profiles.update_profile(
        a.db_path, a.user_id, display_name=a.display_name, timezone=a.timezone,
        disabled_modules=a.disabled_modules,
    )

    # Load the freshly-written config so the workspace seeder sees the real
    # paths (mount + bot_dir), then seed directories + memory.
    os.environ["ISTOTA_CONFIG_PATH"] = str(config_path)
    config = load_config(config_path)
    ensure_workspace_for_user(config, a.user_id)
    return config


def _run_self_check(config, config_path: Path, out) -> None:
    """Run doctor against the install this run just wrote, and print failures.

    This is what turns setup from "wrote some files" into "wrote some files and
    confirmed they work". Four decisions, each of which is easy to get wrong in
    a way that makes the run worse than not doing it:

    **Doctor's own entry point, not a subprocess.** ``istota doctor`` is
    ``cli.cmd_doctor``, which is exactly this sequence; shelling out would be a
    second way to run doctor, with its own environment and its own answer.
    ``failing()`` is what gives results to filter, and ``render_text`` renders
    the same lines the command does — including its redaction pass, which
    matters here because the config may hold a CalDAV password.

    **Explicit config path.** ``config_visibility`` is the gate that stops a
    run with no config resolved from reporting on a default ``Config`` while
    reading exactly like a run about this deployment. It is asked with
    ``requested=config_path`` for the same reason ``cmd_doctor`` asks it: a
    config that failed to load must say so rather than answer about defaults.

    **``probe=False``.** A probing run spawns a subprocess per binary check
    with a 10s ceiling each, and the operator is sitting at a prompt. Nothing
    that matters on a fresh install needs a spawn — the paths, the database,
    the secret key, the control directory and the static build are all read
    from the filesystem — and a check that would exec says so in its detail.
    ``deep`` and ``live`` are left off by their defaults for the same reason,
    doubled: one spawns a namespace and the other bills for a model call.

    **A failure never fails the install.** ``run_setup`` returns a process exit
    code and setup did succeed: the files are written and the database is
    initialized. A red check is information the operator needs, not a reason to
    unwind a working install, so it is printed prominently and the exit code is
    unchanged. The whole call is also wrapped — a diagnostic that raises must
    not be the thing that breaks setup.
    """
    from . import doctor  # noqa: PLC0415 - a heavy import, and only this path needs it

    try:
        gate = doctor.config_visibility(config, requested=config_path)
        results = [gate] if gate is not None else doctor.run_checks(config, probe=False)
        _, summary = doctor.verdict(results)
        failures = doctor.failing(results)
        secrets = doctor.config_secrets(config)
        out("")
        out(f"Self-check ({summary}):")
        if not failures:
            # Only failures are printed, so a warning count with nothing under
            # it would be a number the operator cannot act on. Name where the
            # rest is instead of either hiding the count or printing warnings
            # that are expected on this shape — a closing check that always
            # has something in it teaches the operator to skip the block.
            warned = doctor.summarize(results).get(doctor.WARN, 0)
            tail = f", {warned} warning{'' if warned == 1 else 's'}" if warned else ""
            out(f"  no failures{tail}.")
            if warned:
                out(f"  Full report: istota -c {config_path} doctor")
            return
        out(doctor.render_text(failures, secrets=secrets))
        out("")
        out(
            "  Setup itself succeeded — the config, secrets and database are "
            "written. The checks above are what still needs a look."
        )
        out(f"  Full report: istota -c {config_path} doctor")
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not fail the install
        logger.debug("setup self-check raised", exc_info=True)
        # Redacted for the same reason `render_text` demands `secrets`: an
        # exception out of a check can carry a config value in its message (a
        # URL with userinfo, a path), and terminal output is where a pasted
        # credential ends up in a bug report. Best-effort — this is already the
        # failure path, so a redaction that itself raises must not replace one
        # unhelpful line with a traceback.
        detail = f"{type(exc).__name__}: {exc}"
        try:
            detail = doctor._redact(detail, doctor.config_secrets(config))
        except Exception:  # noqa: BLE001 - see above
            detail = type(exc).__name__
        out("")
        out(
            f"Self-check could not run ({detail}); setup itself succeeded. "
            f"Try `istota -c {config_path} doctor`."
        )


def _print_next_steps(a: Answers, config_path: Path, out) -> None:
    out("")
    out("Setup complete.")
    out(f"  Config:    {config_path}")
    out(f"  Workspace: {a.workspace}")
    out(f"  User:      {a.user_id}")
    out(f"  Brain:     {a.brain_kind}")
    out("")
    out("  Start it:  istota serve")
    out(f"  Then open: http://127.0.0.1:{a.web_port}/istota")
    out("")
    out(
        "  Trust model: this is a single-user, unsandboxed install — the agent "
        "runs with your account's full privileges. Only give it content and "
        "instructions you trust."
    )
