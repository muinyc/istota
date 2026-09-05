"""Developer skill — setup_env hook.

Creates the task's own subtree of ``repos_dir`` (``{repos_dir}/{user_id}`` —
the only part of that tree the sandbox binds) and sweeps it for credentials
embedded in remote URLs, stripping them (:mod:`istota.git_remote_scrub`,
ISSUE-270) before generating anything. Then generates, inside the task's user
temp directory:

- the credential-fetch helper and the per-platform git-credential-helper
  scripts, plus the ``GIT_CONFIG_*`` vars that point git at them;
- ``gh`` and ``glab``, copies of :mod:`istota.forge_cli` that wrap the real
  binaries, and the policy file they read;
- a seeded, read-only config directory per CLI.

Everything lands in ``{user_temp_dir}/.developer``, which ``build_bwrap_cmd``
re-binds read-only inside the sandbox and which ``native_fs_roots`` excludes
from the native brain's write roots. Those two together are what stop the
model's *own file tools* rewriting the wrapper, the policy or gh's alias
table, which is the level of protection an accident guard needs.

It is not an absolute. ``user_temp_dir`` is also the deferred directory, which
``skill_host_paths`` admits as a host-side write root, so a determined model
has paths to that directory that neither the bind nor the deny root covers.
Same posture as the policy itself: it stops a mistake, not a decision. The
boundary that does the real work is the forge token's own scope.

Static env vars (GITLAB_URL, GITHUB_URL, the optional
namespace/owner/reviewer/credit knobs, GITLAB_TOKEN, GITHUB_TOKEN) come
from the manifest's ``env:`` block — this hook only handles the parts
that aren't expressible as static EnvSpecs. ``DEVELOPER_REPOS_DIR`` is one
of those parts now: it is the task's own subtree of ``developer.repos_dir``
rather than the configured value, so the hook owns it and the manifest
entry is ``from: setup_env``.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from pathlib import Path
from urllib.parse import urlsplit

from istota import config as istota_config
from istota.atomic_write import write_text_atomic

# The forge-binary resolution rule lives in a stdlib-only leaf so `doctor` can
# reach it without importing `istota.skills` (whose __init__ star-imports every
# skill, ~190ms) on the `load_config` path. Re-exported under the old private
# names because this module's own call sites and the tests use them.
from istota.forge_bin import FALLBACK_BIN as _FALLBACK_BIN  # noqa: F401 - re-export
from istota.forge_bin import IMAGE_BIN as _IMAGE_BIN  # noqa: F401 - re-export
from istota.forge_bin import resolve_real_bin as _resolve_real_bin
from istota.forge_cli import FORGE_GITHUB, FORGE_GITLAB, build_policy
from istota.git_remote_scrub import scrub_and_report

logger = logging.getLogger("istota.skills.developer")

#: The package-cache directory's name inside a user's repos subtree.
#:
#: ``executor.SANDBOX_CACHE_ROOT_NAME``, restated for the same reason
#: ``_user_repos_dir`` restates the layout rule: a skill module cannot import
#: the executor that imports it (``istota.skills`` star-imports every skill, so
#: the executor's import graph would ride along on every path touching one).
#: ``tests/test_sandbox.py::TestPerUserReposDir`` holds the two equal.
SANDBOX_CACHE_ROOT_NAME = ".package-caches"

# Where the canonical wrapper lives, for copying into the task's .developer.
_FORGE_CLI_SOURCE = Path(__file__).resolve().parents[2] / "forge_cli.py"

# The exec transport's client and the protocol module it imports. Two files,
# not one: a single standalone script would put the wire format in three places
# (module, vendored container copy, client) with `scripts/sync-devbox-lib.sh`
# covering only one of them.
_EXEC_CLIENT_SOURCE = Path(__file__).resolve().parents[2] / "devbox_exec_client.py"
_EXEC_PROTOCOL_SOURCE = Path(__file__).resolve().parents[2] / "devbox_exec_protocol.py"

# What the client is installed as. The shims exec it by absolute path, so the
# name is only what an operator sees in a listing.
_EXEC_CLIENT_NAME = "devbox-exec"
# The protocol module's filename is *not* cosmetic: the client resolves it as
# the file beside itself (`import devbox_exec_protocol`), so this has to be the
# module name.
_EXEC_PROTOCOL_NAME = "devbox_exec_protocol.py"

# Where the shims live. A directory of their own inside `.developer`, rather
# than beside the forge wrappers, for two reasons. A shim whose command left
# `shim_commands` has to be *removed*, and sweeping a directory that also holds
# the wrapper, the policy and the credential helpers means deciding which files
# are shims from their contents. And `ISTOTA_PATH_PREPEND` is os.pathsep-
# separated, so a second entry costs nothing — with `.developer` first, so the
# forge wrappers win any collision.
_SHIM_DIR_NAME = "exec-shims"


def _atomic_write(dest: Path, data: str, mode: int) -> Path:
    """Write via a temp file in the same directory, then rename.

    Tasks for one user share ``user_temp_dir`` and the worker pool runs them
    concurrently, so a plain truncate-then-write can be read half-finished by
    a wrapper another task is running right now. ``os.replace`` is atomic and
    leaves an already-open process on its own inode.

    **The temp name has to be unique per call, not per process.** It used to be
    ``.{name}.tmp{os.getpid()}``, and the worker pool is threads in one process
    (``scheduler.UserWorker``) — so two concurrent tasks for one user produced
    the *identical* path in the identical directory, and the interleaving
    write / write / chmod / replace / chmod ends in ``FileNotFoundError`` for
    the second. The docstring above claimed the atomicity that the rename does
    have and the name did not.

    The consequence is worse than a lost file: ``dispatch_setup_env_hooks``
    keeps only what a hook returned, so the loser's whole ``setup_env`` output
    is discarded and its task runs with no shims and no credential helper — a
    host-side ``npm ci`` that 403s at the CONNECT proxy, which reads as a flaky
    network. The dot prefix is load-bearing too: ``_remove_shims`` skips it, so
    one writer's sweep cannot delete another's in-flight temp.

    ``atomic_write`` is where all of that now lives; this stays as a named
    function because the mode is per-call and every caller here passes one.
    """
    write_text_atomic(dest, data, mode=mode)
    return dest


def _write_forge_cli(dev_bin: Path, name: str) -> Path:
    """Install the wrapper under one of the names it dispatches on.

    A copy rather than a symlink: ``forge_from_argv0`` reads ``argv[0]``, and
    the sandbox's view of a symlink's target is one more thing to get wrong
    for no benefit.
    """
    return _atomic_write(
        dev_bin / name, _FORGE_CLI_SOURCE.read_text(), 0o700,
    )


def _plain_http_host_entry(forge_url: str) -> str:
    """glab's config for a forge reached over plain HTTP, or "" for anything else.

    glab discards the scheme inside ``GITLAB_HOST`` and keeps the port, so a
    deployment configured against ``http://gitlab.internal:8080`` forces https
    and every call dies with "tls: first record does not look like a TLS
    handshake". Measured on glab 1.114.0, the version ``docker/devbox`` pins.
    ``GITLAB_API_PROTOCOL`` is not read either; the one lever glab offers is a
    per-host ``api_protocol`` in its own config file.

    That file is the one ``_seed_cli_config_dir`` truncates on every task, so
    the entry has to be written by the same code that empties it — a caller
    cannot seed it and have it survive.

    Returns "" for https, for an unset or unparseable URL, and for one carrying
    userinfo (see below) — which keeps the file empty wherever it does not have
    to carry something. Whatever is in it is honoured by both CLIs before
    dispatch, so it is not a surface to grow idly.

    The key is the lowercased host, its port if non-default, and the URL path.
    All three are measured rather than assumed: glab lowercases its lookup key,
    and it derives that key from the whole of ``GITLAB_HOST``, which
    ``build_invocation`` sets to the whole URL because a sub-path install is a
    supported shape.
    """
    if not forge_url:
        return ""
    try:
        parts = urlsplit(forge_url if "://" in forge_url else f"https://{forge_url}")
    except ValueError:
        # An unparseable URL is the operator's problem and doctor's to report.
        # This is a setup path; raising here would take the whole hook's return
        # value with it (`dispatch_setup_env_hooks` keeps only what it returned),
        # leaving a task that looks fine and cannot authenticate.
        return ""
    if parts.scheme != "http":
        return ""

    # A URL carrying userinfo gets nothing, deliberately. Measured on glab
    # 1.114.0: its lookup key *includes* the userinfo, so an entry that actually
    # matched `http://user:token@host` would have to carry the password — and
    # this file lives under `.developer`, which is bound readable into the
    # sandbox. That would hand the model a credential in order to support a
    # shape that should not exist: the token belongs in `gitlab_token`, and
    # `git_remote_scrub` exists to strip exactly this out of URLs. Better to
    # leave the call failing the way it already did and let
    # `doctor.check_forge_transport` say why.
    if "@" in (parts.netloc or ""):
        return ""

    # `hostname`, not `netloc`: `hostname` is already lowercased and free of
    # userinfo, and glab's lookup key is lowercased too — measured, an entry
    # filed under `LOCALHOST:8080` is never found and the call forces https.
    host = parts.hostname or ""
    if not host:
        return ""
    if parts.port:
        host = f"{host}:{parts.port}"
    # The path belongs in the key. `build_invocation` puts the whole URL in
    # GITLAB_HOST because a subpath install is a supported shape, and glab
    # derives its key from that — so an entry under the bare netloc is never
    # consulted for `http://forge.internal/gitlab`.
    path = (parts.path or "").rstrip("/")
    if path:
        host = f"{host}{path}"

    # Quoted, because a key carrying a port or a path is a YAML mapping key
    # containing a colon or a slash — unquoted, that is not the key it looks
    # like.
    return (
        "hosts:\n"
        f'  "{host}":\n'
        "    api_protocol: http\n"
        f'    api_host: "{host}"\n'
    )


def _seed_cli_config_dir(
    dev_bin: Path, name: str, *, forge: str = "", forge_url: str = ""
) -> Path:
    """A pre-seeded CLI config directory, at the modes each CLI will accept.

    ``config.yml`` is mode 0600, not 0400, because glab refuses to start on
    anything else ("has the permissions 400, but glab requires 600"); gh is
    happy with either. The file being owner-writable does not matter: the
    model reaches this path only through the sandbox, where ``.developer`` is
    a read-only bind, and that is what actually holds it down.

    Seeding an empty file rather than leaving the directory bare is
    deliberate. gh expands ``aliases`` from ``config.yml`` *before* command
    dispatch, so an absent file is one the model could otherwise supply.

    ``forge`` and ``forge_url`` together decide whether anything is written at
    all — see :func:`_plain_http_host_entry`. **Only glab gets an entry**, and
    the rule lives here rather than at the call site because it is not merely
    useless for gh, it is harmful: on finding a ``hosts:`` block gh runs its
    multi-account migration and writes a ``hosts.yml`` beside the config. (It
    would also buy nothing. The entry exists to reach a forge over plain HTTP,
    and gh refuses a scheme in ``GH_HOST`` outright. The *port* half is a
    different question and is handled — ``forge_cli._gh_host`` keeps a
    non-default one, ISSUE-279.) This function truncates ``config.yml`` and
    nothing else, and ``user_temp_dir`` persists across tasks — so that file
    would survive every later run, in a directory whose whole design is that
    nothing does.
    """
    cfg = dev_bin / name
    cfg.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_yml = cfg / "config.yml"
    # Rewritten on every run, not seeded once, and *replaced* rather than
    # appended to. user_temp_dir persists across tasks, so a "write it only if
    # absent" guard would make the seed a one-time act: anything that got an
    # alias table in there once — a deployment without bwrap, or a host-side
    # write — would have it honoured by every later task. The wrapper and the
    # policy are both rewritten unconditionally; this is the file that most
    # needs to be. Everything written here is derived from config, so a
    # non-empty body is as reproducible as the empty one it replaced.
    entry = _plain_http_host_entry(forge_url) if forge == FORGE_GITLAB else ""
    _atomic_write(config_yml, entry, 0o600)
    return cfg


def _pinned_data_dir(dev_bin: Path, name: str) -> Path:
    """An empty directory to point XDG_DATA_HOME at.

    gh dispatches an unknown first argument to ``gh-<name>`` in
    ``$XDG_DATA_HOME/gh/extensions``, and the argv rules cannot see that — the
    argv is ``gh <name>`` and matches nothing. Left unset, gh derives the path
    from HOME, whose ``.local/share`` is writable inside the sandbox. Verified
    against gh 2.98: a planted extension runs, and pinning the variable at an
    empty directory stops it.
    """
    data = dev_bin / name
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    return data


def _ctx_user_id(ctx) -> str:
    """The task's user id, or "" — never an AttributeError.

    `dispatch_setup_env_hooks` wraps each hook in try/except and keeps only what
    it returned, so a raise anywhere in here silently discards the credential
    helper and the whole forge wiring, leaving a task that looks fine and cannot
    authenticate. Read defensively for the same reason the config reads are.
    """
    return getattr(getattr(ctx, "task", None), "user_id", "") or ""


def _shim_body(
    command: str,
    client: Path,
    socket_path: Path,
    connect_timeout: float,
) -> str:
    """One shim: exec the client, with both paths written in.

    **Neither path is read from the environment, and that is the whole point.**
    This script runs as a child of the model's own shell, so an env-supplied
    path is a path the model chooses — the same reasoning the forge wrapper
    already pays for. With the socket in a variable,
    ``ISTOTA_DEVBOX_EXEC_SOCKET=/tmp/mine npm ci`` would get an ``ok``
    acknowledgement and a fabricated ``exit 0`` from a socket the model wrote,
    with plausible output. Both values are interpolated as single-quoted
    literals so a path carrying a space or a ``$`` cannot be re-read either.

    **There is no ``--cwd``.** The client takes the working directory from
    ``os.getcwd()``: ``$PWD`` is the *logical* path the parent shell recorded,
    so a ``cd`` through a symlink yields a string whose meaning differs from the
    directory the process is in — and the server's containment test is a
    ``realpath``.

    **Stdin is forwarded whenever it is not a terminal.** ``[ -t 0 ]`` tests
    exactly the condition the server's refusal was written for: keeping a child
    off the operator's tty under ``istota serve``. A pipe, a file and
    ``/dev/null`` all forward, the last as an immediate EOF, which is correct.
    Never setting it silently breaks every pipeline into a shimmed command
    (``git diff | npx prettier --check -``) and, under the native Bash tool's
    ``pipefail``, colours the result 141.
    """
    client_q = shlex.quote(str(client))
    socket_q = shlex.quote(str(socket_path))
    timeout_q = shlex.quote(f"{connect_timeout:g}")
    command_q = shlex.quote(command)
    return (
        "#!/bin/sh\n"
        f"# {command} — run in this user's devbox over the exec transport.\n"
        "#\n"
        "# The client path and the socket path are written in at task setup and\n"
        "# neither is read from the environment: this runs as a child of the\n"
        "# model's own shell, so an env-supplied path is a path the model picks.\n"
        "#\n"
        "# stdin is forwarded whenever it is not a terminal, so a pipeline into\n"
        "# this command works. No --cwd: the client sends the physical working\n"
        "# directory, which is what the server's realpath check wants.\n"
        "if [ -t 0 ]; then\n"
        f"  exec {client_q} --socket {socket_q}"
        f" --connect-timeout {timeout_q} -- {command_q} \"$@\"\n"
        "fi\n"
        f"exec {client_q} --socket {socket_q}"
        f" --connect-timeout {timeout_q} --stdin -- {command_q} \"$@\"\n"
    )


def _install_exec_transport(ctx, dev, dev_bin: Path) -> str:
    """Write the client, the protocol module and one shim per command.

    Returns the PATH entry for the shim directory, or ``""`` when this
    deployment is not routing development work into a container.

    **Gated on configuration alone** — ``developer.enabled``, a non-empty
    ``repos_dir``, and ``backend = devbox`` — which is this hook's existing
    self-gate plus one key. Not on skill *selection*: ``developer`` declares no
    ``always_include`` and no ``source_types``, so it reaches
    ``selected_skills`` only through sticky skills, which is to say on the
    **second** turn of a conversation and not the first. A gate on selection
    would leave the shims absent on a fresh "work on repo X", ``npm ci`` would
    run host-side and 403 at the CONNECT proxy, and the whole feature would read
    as flakiness. And ``authorized_skills`` cannot be asked for here either:
    ``dispatch_setup_env_hooks`` runs *before* ``derive_authorized_skills`` by
    design, because a hook-sourced credential is the only auto-auth signal a
    ``source='setup_env'`` skill has.

    The security half of the gate is elsewhere and is a different predicate:
    ``build_bwrap_cmd`` binds the socket only when ``"developer" in
    authorized_skills``, byte for byte what already decides whether this task's
    CONNECT allowlist gets the package registries. So a task that is not
    authorized has shims on ``PATH`` and no socket behind them, and one invoked
    anyway exits 120 naming what it could not reach — the same class of loud
    refusal a host-side ``npm ci`` gets from the CONNECT proxy today.

    **It contacts nothing.** No ping, no socket, no I/O beyond writing files
    into a directory this hook is already writing to. ``setup_env`` runs for
    every skill on every task, so a round trip here would sit in front of every
    Talk reply, every briefing, every cron row and every heartbeat tick, and a
    devbox outage would become a failed briefing — the ISSUE-288 shape.
    Liveness is a property of the deployment, so it lives in ``doctor``.
    """
    shim_dir = dev_bin / _SHIM_DIR_NAME
    if not istota_config.devbox_container_backend(ctx.config):
        # Backend off (or turned off since the last task on this user's temp
        # directory, which persists). Take the shims away rather than leaving
        # them to route a build into a socket nobody is serving.
        _remove_shims(shim_dir, keep=set())
        return ""

    socket_path = istota_config.exec_socket_path(ctx.config, _ctx_user_id(ctx))
    if socket_path is None:
        # No user to scope to — the heartbeat builds a task with no user id.
        # There is no per-user socket to name, so there is nothing to write.
        _remove_shims(shim_dir, keep=set())
        return ""

    container = dev.container
    commands = [c for c in container.shim_commands if c]
    if not commands:
        _remove_shims(shim_dir, keep=set())
        return ""

    try:
        client = _atomic_write(
            dev_bin / _EXEC_CLIENT_NAME, _EXEC_CLIENT_SOURCE.read_text(), 0o755,
        )
        # Beside the client, under its module name: the client prefers the copy
        # that travelled with it over any installed package, because that is the
        # framing it was tested against.
        _atomic_write(
            dev_bin / _EXEC_PROTOCOL_NAME, _EXEC_PROTOCOL_SOURCE.read_text(), 0o755,
        )
        shim_dir.mkdir(parents=True, exist_ok=True)
        for command in commands:
            _atomic_write(
                shim_dir / command,
                _shim_body(
                    command, client, socket_path, container.connect_timeout_seconds
                ),
                0o755,
            )
    except OSError as exc:
        # `dispatch_setup_env_hooks` keeps only what a hook returned, so raising
        # here would discard the credential helper and the forge wiring with it.
        logger.error("developer: could not install the exec shims: %s", exc)
        return ""

    _remove_shims(shim_dir, keep=set(commands))
    return str(shim_dir)


def _remove_shims(shim_dir: Path, keep: set[str]) -> None:
    """Drop shims for commands this task is not routing.

    ``user_temp_dir`` persists across tasks, so a command taken out of
    ``shim_commands`` — or a whole deployment flipped back to ``backend =
    none`` — would otherwise leave a file on the model's PATH that still execs
    the client. Removing rather than rewriting is the only honest answer: a
    shim that is not wanted must not be *reachable*, and the model's shell
    resolves by name.

    **A dotfile is never swept**, and that is the other half of `_atomic_write`'s
    concurrency fix rather than tidiness: tasks for one user run as threads in
    one process and share this directory, so a sweep that unlinked everything
    outside `keep` would delete another writer's in-flight temp file and make
    its `os.replace` raise — costing that task its whole `setup_env` output.

    Never raises: this runs on the setup path.
    """
    try:
        entries = list(shim_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name in keep or entry.name.startswith("."):
            continue
        try:
            entry.unlink()
        except OSError as exc:
            logger.warning("developer: could not remove stale shim %s: %s", entry, exc)


def setup_env(ctx) -> dict[str, str]:
    """Write helper scripts and return the GIT_CONFIG_* / forge-CLI env vars.

    Self-gates on ``config.developer.enabled`` and a non-empty
    ``repos_dir`` — the hook is invoked for every skill in the index, so
    skills must opt themselves out when their config isn't ready.
    """
    config = ctx.config
    dev = getattr(config, "developer", None)
    if dev is None or not dev.enabled or not dev.repos_dir:
        return {}

    env: dict[str, str] = {}
    user_temp_dir = Path(ctx.user_temp_dir)
    dev_bin = user_temp_dir / ".developer"
    dev_bin.mkdir(parents=True, exist_ok=True)

    use_proxy = config.security.skill_proxy_enabled
    cred_fetch_cmd = ""
    if use_proxy:
        cred_fetch = dev_bin / "credential-fetch"
        cred_fetch.write_text(
            "#!/usr/bin/env python3\n"
            "import json, socket, sys\n"
            "import os\n"
            "sock_path = os.environ.get('ISTOTA_SKILL_PROXY_SOCK', '')\n"
            "if not sock_path:\n"
            "    print('ISTOTA_SKILL_PROXY_SOCK not set', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "s.connect(sock_path)\n"
            "s.sendall(json.dumps({'type': 'credential', 'name': sys.argv[1]}).encode() + b'\\n')\n"
            "d = b''\n"
            "while b'\\n' not in d:\n"
            "    c = s.recv(4096)\n"
            "    if not c: break\n"
            "    d += c\n"
            "s.close()\n"
            "r = json.loads(d)\n"
            "if 'error' in r:\n"
            "    print(r['error'], file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print(r.get('value', ''), end='')\n"
        )
        cred_fetch.chmod(0o700)
        cred_fetch_cmd = str(cred_fetch)

    def _token_expr(var_name: str) -> str:
        # Quoted: git's credential protocol wants the value verbatim, and an
        # unquoted expansion is word-split by sh and rejoined by echo on
        # single spaces. No PAT format has whitespace today; this costs two
        # characters and removes a way for a future one to fail unreadably.
        if use_proxy:
            return f'"$({cred_fetch_cmd} {var_name})"'
        return f'"${var_name}"'

    git_config_index = 0

    if dev.gitlab_token:
        gitlab_host = dev.gitlab_url.rstrip("/")

        git_cred = dev_bin / "git-credential-helper"
        git_cred.write_text(
            "#!/bin/sh\n"
            '[ "$1" = "get" ] || exit 0\n'
            f"echo username={dev.gitlab_username}\n"
            f"echo password={_token_expr('GITLAB_TOKEN')}\n"
        )
        git_cred.chmod(0o700)
        env[f"GIT_CONFIG_KEY_{git_config_index}"] = f"credential.{gitlab_host}.helper"
        env[f"GIT_CONFIG_VALUE_{git_config_index}"] = str(git_cred)
        git_config_index += 1

    if dev.github_token:
        github_host = dev.github_url.rstrip("/")
        gh_username = dev.github_username or "x-access-token"

        gh_cred = dev_bin / "git-credential-helper-github"
        gh_cred.write_text(
            "#!/bin/sh\n"
            '[ "$1" = "get" ] || exit 0\n'
            f"echo username={gh_username}\n"
            f"echo password={_token_expr('GITHUB_TOKEN')}\n"
        )
        gh_cred.chmod(0o700)
        env[f"GIT_CONFIG_KEY_{git_config_index}"] = f"credential.{github_host}.helper"
        env[f"GIT_CONFIG_VALUE_{git_config_index}"] = str(gh_cred)
        git_config_index += 1

    if git_config_index > 0:
        env["GIT_CONFIG_COUNT"] = str(git_config_index)

    # --- Forge CLIs -------------------------------------------------------
    #
    # Installed whenever either token is configured. Both names go on PATH
    # regardless: `glab` with no GitLab token exits 5 with the proxy's own
    # message, which is a clearer failure than "command not found" leading
    # the model to the real binary and an unauthenticated call.
    if dev.gitlab_token or dev.github_token:
        state_dir = user_temp_dir / ".forge-state"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        def _section(forge: str, url: str, real_bin: str) -> dict:
            section = build_policy(
                forge,
                extra_denied=list(getattr(dev, "forge_cli_extra_denied", [])),
                permit=list(getattr(dev, "forge_cli_permit", [])),
            )
            section["url"] = url
            section["real_bin"] = real_bin
            section["config_dir"] = str(
                _seed_cli_config_dir(
                    dev_bin, f"{forge}-config", forge=forge, forge_url=url
                )
            )
            section["data_dir"] = str(_pinned_data_dir(dev_bin, f"{forge}-data"))
            section["state_dir"] = str(state_dir)
            # With the skill proxy off there is no socket to ask, and the
            # token is legitimately in the environment rather than having
            # escaped a stripping step. Saying so here rather than in an env
            # var matters: this file is the one input the model cannot
            # redirect, so it is the only safe place to grant that permission.
            section["direct_token"] = not use_proxy
            return section

        policy = {
            FORGE_GITHUB: _section(
                FORGE_GITHUB,
                dev.github_url,
                _resolve_real_bin(getattr(dev, "gh_bin_path", ""), "gh"),
            ),
            FORGE_GITLAB: _section(
                FORGE_GITLAB,
                dev.gitlab_url,
                _resolve_real_bin(getattr(dev, "glab_bin_path", ""), "glab"),
            ),
        }
        _atomic_write(
            dev_bin / "forge-policy.json",
            json.dumps(policy, indent=2, sort_keys=True),
            0o600,
        )

        _write_forge_cli(dev_bin, "gh")
        _write_forge_cli(dev_bin, "glab")
        # The retired names, so a cached habit or an old CRON.md job gets the
        # one-line explanation rather than "command not found".
        _write_forge_cli(dev_bin, "github-api")
        _write_forge_cli(dev_bin, "gitlab-api")

        # The only var the wrapper needs from the environment. Everything else
        # it might have read from there — the policy, the real binary, the
        # config and data dirs, the forge URL — now travels in the policy file,
        # because the wrapper runs as a child of the model's own shell and an
        # env-supplied path is a path the model chooses.
        #
        # Reserved key: the executor prepends this to the *model's* PATH only,
        # after snapshotting the environment it gives host-side skill CLIs.
        # See executor.HOOK_PATH_PREPEND_KEY — that ordering is a security
        # property, not housekeeping.
        env["ISTOTA_PATH_PREPEND"] = str(dev_bin)

    # --- The exec transport -----------------------------------------------
    #
    # Independent of the forge tokens above: a deployment routing builds into
    # the devbox does so whether or not it has a forge configured. So the PATH
    # entry is composed rather than assigned, and `.developer` stays first —
    # the forge wrappers win any name collision with a shim.
    shim_path = _install_exec_transport(ctx, dev, dev_bin)
    if shim_path:
        existing = env.get("ISTOTA_PATH_PREPEND", "")
        env["ISTOTA_PATH_PREPEND"] = (
            os.pathsep.join([existing, shim_path]) if existing else shim_path
        )

    # ISSUE-270: strip any credential embedded in a git config under the repos
    # tree before the model can read one. That tree is bound read-write into the
    # sandbox a few steps from here, every worktree inherits its bare clone's
    # remotes, and `git remote -v` prints a URL in full — so a token in one
    # reaches the model's context as a matter of routine, around the helper
    # registered above. Nothing here writes such a config; this catches one
    # that arrived by hand.
    #
    # **The per-user root, not the global one**, and that direction is the one
    # that matters rather than the obvious one: handed `repos_dir` whole, one
    # user's task would walk and *rewrite git configs in* every other user's
    # tree, on every developer-enabled task. Handed `{repos_dir}/{user_id}`,
    # `git_remote_scrub._MAX_DEPTH = 4` needs no change at all — that constant
    # is measured from the root it is given, and the documented layout sits at
    # depth 2 below the per-user root exactly as it used to sit at depth 2 below
    # `repos_dir`. A reviewer checking it against the global root will conclude
    # the margin halved; it does not.
    #
    # Last, and after `env` is complete, deliberately. `dispatch_setup_env_hooks`
    # wraps each hook in `try/except` and keeps only what it returned, so an
    # exception raised here would not fail the task — it would silently discard
    # the credential helper, GIT_CONFIG_COUNT and the forge-CLI wiring, leaving
    # a task that looks fine and cannot authenticate. `scrub_and_report` holds a
    # never-raises contract of its own; this ordering is the second guard.
    #
    # That all-or-nothing return now costs `DEVELOPER_REPOS_DIR` too, since the
    # manifest is no longer a second source of it, and moving the assignment
    # earlier would not help — the hook's whole return value goes, wherever in
    # it the raise happened. What is worth knowing is that the two new
    # consequences are quiet: `$DEVELOPER_REPOS_DIR/<ns>/<project>.git` expands
    # to an absolute path on the sandbox root tmpfs, and `code_review` takes its
    # `repos_root_unavailable` skip, which is exit 0 and non-blocking by design.
    # So a raise anywhere above lands work unreviewed rather than failing. The
    # fix for that is in `dispatch_setup_env_hooks`, which should not be
    # swallowing a setup failure into an empty dict at all.
    # The package caches sit *inside* the subtree this walks —
    # `{repos_dir}/{user_id}/.package-caches`, derived by
    # `executor.resolve_sandbox_cache_dir` — and hold one directory per
    # unpacked wheel, none of them a repository. So the skip is not an
    # optimization that happens to be inert: with the derivation in place the
    # walk reaches the cache on every task, and `git_remote_scrub`'s depth
    # budget would be spent on wheels. Named from the constant rather than from
    # `security.sandbox_cache_dir`, which the resolver does not read while
    # `repos_dir` is set.
    repos_root = _user_repos_dir(dev, ctx)
    cache_root = repos_root / SANDBOX_CACHE_ROOT_NAME if repos_root else None
    if repos_root is not None:
        # The one place that knows the layout. `DEVELOPER_REPOS_DIR` is the
        # task's own subtree, exactly the path `build_bwrap_cmd` binds, so the
        # documented clone recipe (`$DEVELOPER_REPOS_DIR/<ns>/<project>.git`)
        # lands inside the namespace instead of on bwrap's root tmpfs. It goes
        # to the model *and*, through `proxy_base_env`, to every host-side skill
        # CLI — `code_review` contains a model-named worktree against it.
        #
        # Emitted here rather than resolved from `developer.repos_dir` by the
        # manifest, and the manifest entry is `from: setup_env` (metadata only)
        # rather than `from: config`, because the two cannot both name it: the
        # merge in `execute_task` applies `build_skill_env` first and the hooks
        # second, both with `if k not in env`, so a `from: config` entry wins
        # and this value would be dropped without a word. Measured, not read —
        # `tests/test_developer_repos_env.py::TestManifestOutranksSetupEnv`.
        #
        # Absent for a non-admin, matching `_user_repos_dir`'s gate and the
        # bind's: a variable with no bind behind it names a directory on the
        # root tmpfs, which is the defect this stage closes, one gate out.
        #
        # **The authorization gate is gone, and that is deliberate.** The
        # manifest resolved this through `build_skill_env(authorized_skills,
        # …)`, so a deployment with `[developer]` configured but no forge token
        # had the config key and no variable. A hook is dispatched over the
        # whole skill index whatever the task selected, so every admin task on
        # a developer-enabled deployment now carries it. That widens what the
        # model is *told*, not what it can reach: `build_bwrap_cmd` gates the
        # bind on `is_admin and config.developer.enabled` alone — never on
        # skill selection — so the directory is already in the namespace of
        # exactly this set of tasks, and naming it adds nothing. Two things did
        # depend on the old gate and were corrected with this change: the smoke
        # tier's authorization control (`tests/smoke/test_secret_isolation.py`,
        # which now reads `GITLAB_URL`, a var that is still manifest-resolved
        # and so still gated) and the pre-commit hook's unattended-shell marker,
        # whose meaning shifted from "authorized for the developer skill" to
        # "an admin task on a developer-enabled deployment" — see AGENTS.md.
        env["DEVELOPER_REPOS_DIR"] = str(repos_root)
        scrub_and_report(repos_root, skip=[cache_root] if cache_root else [])

    return env


def _user_repos_dir(dev, ctx) -> Path | None:
    """``{repos_dir}/{user_id}``, created if it is not there, or None.

    The layout rule is ``executor.get_user_repos_dir``, including its
    containment check; this is the same rule written a second time because a
    skill module cannot import the executor that imports it (``istota.skills``
    star-imports every skill, so the executor's import graph would ride along
    on every path that touches one). ``tests/test_sandbox.py::TestPerUserReposDir``
    holds the two equal, so a change to either without the other goes red.

    Created here, at 0700, because ``build_bwrap_cmd``'s ``_bind`` skips a path
    that does not exist. Without it a user's first developer task binds nothing
    at all, the model's first ``mkdir -p`` under ``$DEVELOPER_REPOS_DIR`` lands
    on bwrap's root tmpfs, and the clone it then spends minutes on disappears
    when the task ends — a working first task and a confusing one differ by
    this directory. ``mkdir`` + an explicit ``chmod`` is the idiom
    ``resolve_sandbox_cache_dir`` uses for the per-user cache directory, for
    the reason that ``mkdir``'s mode is umask-dependent. Only the idiom is
    shared: that function validates its root through half a dozen guards this
    one does not repeat, because the root here is an operator-set path the
    sandbox has bound since the developer skill shipped.

    **A failed ``chmod`` must not cancel the scrub.** They are two failures and
    only one of them is disqualifying: ``mkdir(exist_ok=True)`` succeeds on a
    directory another uid owns and ``chmod`` then raises ``EPERM``, while
    ``build_bwrap_cmd`` binds that directory regardless — its gate is the
    path's existence, not this function's return value. Returning None on the
    chmod would bind an unscrubbed tree, which is ISSUE-270 back, on the one
    shape (a migrator or an operator made the directory) where it is most
    likely.

    Never raises. This runs late in a hook whose exceptions
    ``dispatch_setup_env_hooks`` swallows along with everything the hook
    returned, so a failure here has to be reported rather than thrown — a task
    that cannot clone is better than one that silently cannot authenticate.
    """
    # The bind is admin-gated, so a non-admin's subtree would be created, mode
    # reset and walked on every task and every heartbeat tick for a directory
    # no sandbox ever binds. The two gates agree instead.
    if not getattr(ctx, "is_admin", False):
        return None

    user_id = getattr(getattr(ctx, "task", None), "user_id", "") or ""
    if not user_id:
        # The fallback would be the shared root, which is the cross-user reach
        # the per-user layout exists to remove. Fail closed and say so.
        logger.warning(
            "developer: no user id on the task; not creating or scrubbing a "
            "repos subtree under %s", dev.repos_dir,
        )
        return None

    root = Path(dev.repos_dir)
    repos_root = root / user_id
    try:
        contained = (
            repos_root.parent == root
            and repos_root.resolve() == root.resolve() / user_id
        )
    except OSError:
        contained = False
    if not contained:
        # A symlink left in the root by a task from the shared-tree era, or a
        # user id that is not one path component. Either way this is not that
        # user's subtree, and `mkdir`, `chmod` and the scrub's rewrites would
        # all follow it. See `executor.get_user_repos_dir`.
        logger.warning(
            "developer: %s is not the subtree named by user id %r; not "
            "creating, chmodding or scrubbing it", repos_root, user_id,
        )
        return None

    try:
        repos_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "developer: could not create the repos subtree %s (%s); the task "
            "will have no writable repos directory inside the sandbox",
            repos_root, exc,
        )
        return None
    try:
        os.chmod(repos_root, 0o700)
    except OSError as exc:
        # Reported, not fatal — see the docstring. The scrub still runs.
        logger.warning(
            "developer: could not set 0700 on the repos subtree %s (%s)",
            repos_root, exc,
        )
    return repos_root
