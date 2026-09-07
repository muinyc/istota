"""Assembling one task's runtime environment: the model's env, the two proxies.

Extracted verbatim from ``execute_task``, where it was ~320 lines producing
seven locals the rest of that function read. What lives here is the model's
environment, the three-way credential split behind it, the two proxy objects
and the sandbox's read-only bind list.

Both context managers are **constructed** here and **entered by the caller**.
That is not a lifetime this module could own: the proxies have to be live
across the primary brain call, the reroute and the fallback call, which is
what the ``ExitStack`` in ``execute_task`` expresses. A ``with`` here would
close them before the brain ran.

Four orderings inside ``build_task_runtime`` are load-bearing and are
preserved as written:

1. ``proxy_base_env`` is snapshotted *before* ``ISTOTA_SANDBOXED`` is set,
   because the proxy runs skills host-side where that marker would be a lie.
2. ``_split_credential_env`` is called twice, proxy-only first, and the second
   call operates on the residue of the first.
3. ``HOOK_PATH_PREPEND_KEY`` is consumed after the ``hook_env`` merge, which
   skips it by name; it reaches neither ``env`` nor the proxy snapshot.
4. Both ISSUE-410 reachability top-ups run *after* both credential splits, so
   a name a manifest declared ``sensitive`` stays where the split put it
   rather than being read back out of the daemon's environment.

And two predicates that look like one and are not: the network-proxy gate
reads ``config.security.sandbox_enabled`` (what the operator asked for) while
the sandboxed marker reads ``effective_sandboxing`` (what they got). A reader
will want to unify them; they must not. The case where they differ —
``network.enabled`` on, bwrap unavailable — is a real deployment.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .claude_runtime_env import without_claude_runtime_env

if TYPE_CHECKING:
    from . import db
    from .config import Config
    from .network_proxy import NetworkProxy
    from .skill_proxy import SkillProxy

logger = logging.getLogger(__name__)


@dataclass
class TaskRuntime:
    """What one task needs to run, minus the prompt.

    Every field is read by ``execute_task`` after the call: ``env`` and the two
    sockets go onto the ``BrainRequest``, the two context managers are entered
    in its ``ExitStack``, and ``extra_ro_binds`` / ``authorized_skills`` are
    captured by the sandbox wrap closures.
    """

    env: dict[str, str]
    # Constructed, not entered. See the module docstring.
    proxy_ctx: SkillProxy | None
    proxy_sock: Path | None
    net_proxy_ctx: NetworkProxy | None
    net_proxy_sock: Path | None
    extra_ro_binds: list[Path]
    # The union of the selected skills and the ones credential presence
    # auto-authorizes — not ``selected_skills``. `build_bwrap_cmd`'s docstring
    # says why the distinction decides what gets bound.
    authorized_skills: frozenset[str]


def build_task_runtime(
    config: Config,
    task: db.Task,
    *,
    user_temp_dir: Path,
    control_dir: Path,
    task_attempt: int,
    selected_skills: list[str],
    skill_index: dict,
    is_admin: bool,
    user_resources: list,
    user_config: object | None,
    discovered_calendars: list | None = None,
) -> TaskRuntime:
    """Build the model's environment and the per-task proxies for one attempt.

    Raises whatever ``dispatch_setup_env_hooks`` and
    ``resolve_sandbox_cache_dir`` raise, as the inline block did. Nothing here
    is entered, started or bound, so a raise leaks nothing.
    """
    from .executor import (
        HOOK_PATH_PREPEND_KEY,
        SANDBOX_CACHE_NPM,
        SANDBOX_CACHE_UV,
        SKILL_MODEL_CALLERS,
        _build_network_allowlist,
        _split_credential_env,
        build_clean_env,
        derive_authorized_skills,
        derive_credential_set,
        derive_lookup_allowlist,
        derive_proxy_only_set,
        derive_skill_credential_map,
        effective_sandboxing,
        native_fs_confinement_active,
        resolve_sandbox_cache_dir,
        skill_cli_tls_env,
        skill_model_credentials,
        skill_model_reachability,
    )

    env = build_clean_env(config)
    env.update({
        "ISTOTA_TASK_ID": str(task.id),
        # `tasks transcript` reads this to decide which log is the one being
        # written right now. The row it used to read that from is bumped by
        # the liveness reaper underneath a worker the reaper wrongly
        # believes is dead, which handed that worker its own live
        # transcript (ISSUE-377). Withheld from the model — see
        # `_EXECUTOR_PROXY_ONLY_VARS`.
        "ISTOTA_TASK_ATTEMPT": str(task_attempt),
        "ISTOTA_USER_ID": task.user_id,
        "ISTOTA_BOT_DIR_NAME": config.bot_dir_name,
        "ISTOTA_CONVERSATION_TOKEN": task.conversation_token or "",
        "ISTOTA_DEFERRED_DIR": str(user_temp_dir),
        "ISTOTA_EXPERIMENTAL_FEATURES": ",".join(config.experimental.features),
    })

    # NEXTCLOUD_MOUNT_PATH is the real mount root for everyone. Every
    # consumer (the memory / memory_search skill CLIs, the schedules /
    # reminders skill docs) builds paths as `$NEXTCLOUD_MOUNT_PATH/Users/
    # <uid>/…`, so a "scoped" non-admin value (real/Users/<uid>) doubled the
    # Users/<uid> segment — a non-admin's USER.md write then landed at
    # real/Users/<uid>/Users/<uid>/… , a phantom path the auto-loader never
    # reads back (silent memory loss). Per-user filesystem isolation is
    # enforced by the bwrap bind (build_bwrap_cmd binds only the user's own
    # Users/<uid> dir, for admin and non-admin alike) and the CLIs self-scope
    # by ISTOTA_USER_ID, so the real root is safe here; the prompt still
    # shows non-admins their scoped path.
    env["NEXTCLOUD_MOUNT_PATH"] = (
        str(config.nextcloud_mount_path) if config.nextcloud_mount_path else ""
    )
    # Set for every user, admin or not, and then split out of Claude's env
    # into the proxy's below. It used to be admin-gated, which was never a
    # real boundary — the path is fixed and derivable from
    # ISTOTA_CONFIG_PATH — while it *did* break every framework-DB skill CLI
    # for non-admins (`tasks status`, `memory_search`, `kv` reads all
    # self-scope by ISTOTA_USER_ID and returned an error instead of that
    # user's own rows). The boundary is the SQL, plus the fact that the file
    # is not in the sandbox at all.
    if config.db_path:
        env["ISTOTA_DB_PATH"] = str(config.db_path)

    # Browser container credentials
    if config.browser.enabled:
        env["BROWSER_API_URL"] = config.browser.api_url
        env["BROWSER_VNC_URL"] = config.browser.vnc_url

    # Devbox: the agent's persistent dev container. The skill CLI speaks the
    # exec transport to a server inside it; only `reset` still runs
    # `docker`, host-side in the CLI's own process.
    #
    # No socket path is exported, and that is load-bearing rather than
    # tidiness (ISSUE-284, and Design 5 of the devbox transport). This
    # environment is the *model's*, so a path named here is a path the model
    # can replace: `ISTOTA_DEVBOX_EXEC_SOCKET=/tmp/mine` would buy an `ok`
    # acknowledgement and a fabricated exit 0 from a socket the model wrote.
    # The CLI reads its socket from the config file instead, in a host-side
    # process the model cannot reach.
    #
    # `ISTOTA_DEVBOX_EXEC_TIMEOUT` went with the 300-second default it
    # carried: the transport imposes no timeout, the task's own budget
    # governs, and a caller wanting a kill passes `--timeout`.
    if config.devbox.enabled:
        env["ISTOTA_DEVBOX_CONTAINER"] = (
            f"{config.devbox.container_prefix}{task.user_id}"
        )
        env["ISTOTA_DEVBOX_DOCKER_CLI"] = config.devbox.docker_cli
        env["ISTOTA_DEVBOX_MAX_OUTPUT_BYTES"] = str(
            config.devbox.max_output_bytes
        )

    # Declarative env vars from skill manifests
    from .skills._env import (
        EnvContext,
        build_identity_env,
        build_skill_env,
        dispatch_setup_env_hooks,
    )
    env_ctx = EnvContext(
        config=config,
        task=task,
        user_resources=user_resources,
        user_config=user_config,
        user_temp_dir=Path(user_temp_dir),
        is_admin=is_admin,
        discovered_calendars=list(discovered_calendars or []),
    )
    # Phase 3: resolve manifest env vars for ``authorized_skills`` —
    # the union of selected skills and skills auto-authorized via
    # credential presence. ``derive_authorized_skills`` walks each
    # skill's sensitive specs with ``fallbacks_disabled=True`` so
    # operator-set EnvironmentFile fallbacks cannot fan out to per-
    # user auto-authorization. Resolution itself (below) honors
    # fallbacks for the value path.
    # setup_env hooks self-gate; the dispatcher iterates the full
    # skill_index regardless of the argument it's given. Dispatched
    # *before* authorization because a hook-sourced credential is the
    # only auto-auth signal a ``source="setup_env"`` skill has.
    hook_env = dispatch_setup_env_hooks(selected_skills, skill_index, env_ctx)
    authorized_skills = derive_authorized_skills(
        selected_skills, skill_index, env_ctx, hook_env=hook_env,
    )
    skill_env = build_skill_env(authorized_skills, skill_index, env_ctx)
    # A menu-loaded skill (the model self-selects it at runtime via
    # ``skills show``) is neither eagerly selected nor credential-
    # authorized, so the call above skips it. Its pure-identity vars
    # (``source="user_id"``, e.g. ``MONEY_USER`` / ``FEEDS_USER``) are
    # non-sensitive and required for the skill to run at all — resolve
    # those over the full index so the proxied CLI isn't missing them
    # ("MONEY_USER not set"). Config/secret-derived vars stay gated on
    # ``authorized_skills`` (env minimisation for the untrusted model).
    for k, v in build_identity_env(skill_index, env_ctx).items():
        skill_env.setdefault(k, v)
    # Declarative env vars don't override hardcoded ones
    for k, v in skill_env.items():
        if k not in env:
            env[k] = v
    for k, v in hook_env.items():
        if k == HOOK_PATH_PREPEND_KEY:
            # Never merged into ``env``: see the application site below.
            continue
        if k not in env:
            env[k] = v

    # Credential isolation via skill proxy: strip secrets from Claude's env
    # and run skill CLIs through a Unix socket proxy that injects them.
    _proxy_ctx = None
    _proxy_sock = None
    # Third bucket alongside credentials and the clean env: non-secret
    # values (database paths) that belong to the host-side CLI and not to
    # the model. Split *outside* the proxy branch — an operator who turns
    # the proxy off has made skill CLIs unreachable, not made it acceptable
    # to hand the model a path to every user's data.
    proxy_only_env, env = _split_credential_env(
        env, derive_proxy_only_set(skill_index),
    )
    if config.security.skill_proxy_enabled:
        from .skill_proxy import SkillProxy
        # Phase 3: credential set is derived from the loaded skill
        # index; no hand-maintained constant. Same for the per-skill
        # credential map and the lookup-endpoint allowlist.
        credential_set = derive_credential_set(skill_index)
        credential_env, env = _split_credential_env(env, credential_set)
        # Started unconditionally. This used to be gated on
        # ``if credential_env:``, so a task whose authorized skills declared
        # no secret got no socket — and `istota-skill` then silently fell
        # back to running the skill module *inside* the sandbox, which is
        # the one place it must never run. On a Nextcloud deployment
        # NC_PASS made the gate true nearly always, so the fallback was
        # rare rather than absent; that is a property of the configuration,
        # not an invariant.
        #
        # Use /tmp for the socket path to stay within the AF_UNIX length
        # limit (~104 chars). build_bwrap_cmd() bind-mounts this file into
        # the sandbox. PID is included so concurrent processes (xdist test
        # workers, parallel scheduler instances on the same host) don't race
        # on the same path — task.id alone collides when each process has
        # its own DB.
        _proxy_sock = Path(tempfile.gettempdir()) / f"istota-proxy-{os.getpid()}-{task.id}.sock"
        env["ISTOTA_SKILL_PROXY_SOCK"] = str(_proxy_sock)
        allowed_creds = derive_lookup_allowlist(
            authorized_skills, skill_index,
        )
        skill_cred_map = derive_skill_credential_map(
            authorized_skills, skill_index,
        )
        cli_skills = frozenset(
            name for name, meta in skill_index.items() if meta.cli
        )
        logger.info(
            "proxy_authorization task_id=%d selected=%d authorized=%d "
            "selected_skills=%s authorized_skills=%s",
            task.id, len(selected_skills), len(authorized_skills),
            ",".join(sorted(selected_skills)),
            ",".join(authorized_skills),
        )
        # Snapshot, not the live dict: ``env`` picks up ISTOTA_SANDBOXED
        # below, and the proxy runs skills on the host where that would be
        # a lie. Everything else the CLIs rely on rides along — notably
        # ISTOTA_DEFERRED_DIR, whose absence is what makes a deferring skill
        # take its direct-write fallback instead.
        #
        # Minus the Claude runtime credential, which is the second route
        # ISSUE-390 had to close and the less obvious one. The token is
        # declared in no skill manifest, so neither `derive_credential_set`
        # nor `derive_proxy_only_set` takes it out of this snapshot, and the
        # model reaches every host-side skill CLI through the same Bash tool
        # the strips in `NativeBrain` just cleaned — and unlike a tool
        # subprocess these run *unsandboxed as the daemon user*, which is the
        # reason to be strict rather than a reason to relax.
        proxy_base_env = without_claude_runtime_env(
            {**env, **proxy_only_env}
        )
        # Plus where this host's TLS trust store is (ISSUE-410).
        # `build_clean_env` is an allowlist carrying none of those names, so a
        # host-side skill CLI on a deployment with a private CA had no way to
        # know it existed and failed at the handshake. Shared with every such
        # CLI because a trust store path can only *add* a CA — it redirects
        # nothing and carries no credential, so a skill with no use for it is
        # unaffected. The proxy triple is the opposite on both counts and is
        # scoped below instead; `SKILL_CLI_TLS_VARS` carries that reasoning.
        #
        # `credential_env` is passed too, so a name a manifest declared
        # `sensitive` — which the split just *moved* out of `env` — is not
        # read back out of the daemon's environment and handed to every CLI.
        proxy_base_env.update(
            skill_cli_tls_env(proxy_base_env, credential_env)
        )
        # One skill CLI is itself a model caller, and the strip above left it
        # unauthenticated: `code_review` spawns the `claude` binary per
        # reviewer, so from ISSUE-390 every review came back `review_failed`
        # on a deployment where that token is the only credential there is.
        # `skill_model_credentials` is where the reasoning lives, including
        # why this is a copy rather than a third `_split_credential_env`.
        #
        # Two sources, in order: the token is in `env` because
        # `build_clean_env` put it there, and the two API-key names are in no
        # task env by any route, so they come from the daemon's own.
        #
        # Injection only, never lookup: the map scopes a value to one skill's
        # subprocess, while `allowed_credentials` — which this deliberately
        # does not touch — is a union any holder of the socket can fetch from,
        # the model included. `_PROXY_LOOKUP_BLOCKED` says the same thing at
        # the endpoint, so the two do not depend on each other's ordering.
        model_creds = skill_model_credentials(env, os.environ)
        # And how to *reach* the provider, scoped the same way (ISSUE-410).
        # Not in `proxy_base_env` with the TLS names above, because a proxy
        # URL redirects traffic rather than merely sitting unread: `browse`
        # calls `BROWSER_API_URL` — loopback by default — over an httpx client
        # with `trust_env=True`, which honours `HTTP_PROXY` and does not
        # exempt loopback, so sharing the daemon's egress proxy would send an
        # internal call at it. It can also carry basic-auth userinfo, and a
        # skill CLI's stderr goes back to the model verbatim.
        model_reach = skill_model_reachability(proxy_base_env, credential_env)
        # One dict from here down because the injection is identical; they are
        # built apart because the empty-value rule differs, and named apart
        # because `scoped_for_model` is not all credentials any more.
        scoped_for_model = {**model_creds, **model_reach}
        if scoped_for_model:
            credential_env.update(scoped_for_model)
            for skill_name in SKILL_MODEL_CALLERS & set(authorized_skills):
                skill_cred_map.setdefault(skill_name, set()).update(
                    scoped_for_model
                )
        _proxy_ctx = SkillProxy(
            _proxy_sock, credential_env, proxy_base_env,
            timeout=config.security.skill_proxy_timeout,
            skill_timeouts=config.security.skill_proxy_timeouts,
            allowed_credentials=allowed_creds,
            skill_credential_map=skill_cred_map,
            allowed_skills=cli_skills,
            authorized_skills=frozenset(authorized_skills),
            task_id=task.id,
        )

    # Marks the env as one that will run under bwrap, so `istota-skill`
    # refuses to execute a skill module in-process rather than silently
    # doing it against databases that aren't there. Set after the proxy's
    # base env is snapshotted (the proxy runs skills on the host, where the
    # marker would be a lie), and only when the sandbox is really in
    # effect — on macOS / a container without CAP_SYS_ADMIN,
    # build_bwrap_cmd returns the command unwrapped.
    #
    # Gated on the proxy too. The marker means "the socket is how you run a
    # skill"; with the proxy off there is no socket, and setting it anyway
    # would turn a supported (if now discouraged) configuration into one
    # where every skill CLI fails — including the many that never open a
    # database. That combination gets a loud warning at config load instead.
    if config.security.skill_proxy_enabled and effective_sandboxing(config):
        env["ISTOTA_SANDBOXED"] = "1"

    # Package-manager caches, pointed at the disk-backed directory
    # `build_bwrap_cmd` binds RW from the same predicate (ISSUE-305).
    #
    # Here, not in `build_clean_env`, for two reasons. `proxy_base_env` was
    # snapshotted above and is what SkillProxy hands every host-side skill
    # CLI — a process running unsandboxed as the daemon user, which has no
    # business resolving a cache out of a directory the model can write;
    # that is the confused-deputy shape the ISTOTA_PATH_PREPEND comment
    # below spells out. And the cache is per user, which needs the task.
    #
    # Gated on effective sandboxing, matching the bind exactly: without
    # bwrap there is no root tmpfs and nothing to move off it.
    if native_fs_confinement_active(config):
        _cache_dir = resolve_sandbox_cache_dir(config, task.user_id)
        if _cache_dir is not None:
            env["UV_CACHE_DIR"] = str(_cache_dir / SANDBOX_CACHE_UV)
            env["XDG_CACHE_HOME"] = str(_cache_dir)
            # npm on Linux uses ~/.npm and ignores XDG, so XDG_CACHE_HOME
            # alone would leave it in RAM. Inert until ISSUE-304 opens the
            # registry, and one line now rather than a rediscovery later.
            env["npm_config_cache"] = str(_cache_dir / SANDBOX_CACHE_NPM)
            # HF_HOME defaults to $XDG_CACHE_HOME/huggingface, so moving XDG
            # would silently orphan the read-only `~/.cache/huggingface`
            # bind — a pre-warmed model cache every task would re-download.
            # Pin it back where the bind is.
            env["HF_HOME"] = str(
                Path(os.environ.get("HOME", "/tmp")) / ".cache" / "huggingface"
            )

    # PATH entries contributed by setup_env hooks — today the developer
    # skill's .developer dir, so the model can type `gh` and reach the
    # wrapper rather than the real binary.
    #
    # Applied *here*, after the proxy's base env was snapshotted above, and
    # never merged into ``env`` by the hook loop. That ordering is the
    # whole point and must not be tidied away:
    #
    #   ``proxy_base_env`` is what SkillProxy hands every host-side skill
    #   CLI, which runs outside bwrap as the daemon user. Some of those
    #   resolve a binary by bare name — google_workspace does
    #   ``os.execvp("gws", …)``, devbox does ``shutil.which("docker")``.
    #   A task-temp directory on that PATH would therefore be a host-side
    #   code-execution path, wide open to whatever the model can write
    #   into it. The sandbox re-binds .developer read-only precisely to
    #   stop that, but relying on a bind to contain a PATH entry that
    #   never needed to be there is the wrong way round.
    #
    # ``build_claude_env`` already set PATH, so a hook returning "PATH"
    # would be silently dropped by the ``if k not in env`` merge; this
    # reserved key is the explicit alternative. It is consumed here and
    # never reaches the model.
    _path_prepend = hook_env.get(HOOK_PATH_PREPEND_KEY, "")
    if _path_prepend:
        _entries = [p for p in _path_prepend.split(os.pathsep) if p]
        if _entries:
            env["PATH"] = os.pathsep.join([*_entries, env["PATH"]])

    # Network isolation via CONNECT proxy: outbound traffic restricted
    # to an allowlist of host:port pairs via --unshare-net + proxy.
    _net_proxy_ctx = None
    _net_proxy_sock = None
    if config.security.network.enabled and config.security.sandbox_enabled:
        from .network_proxy import NetworkProxy, write_bridge_script

        allowed_hosts = _build_network_allowlist(config, authorized_skills)

        # Write bridge script to .developer/ (RO inside sandbox)
        dev_dir = Path(user_temp_dir) / ".developer"
        dev_dir.mkdir(parents=True, exist_ok=True)
        write_bridge_script(dev_dir / "net-bridge")

        _net_proxy_sock = Path(tempfile.gettempdir()) / f"istota-net-{os.getpid()}-{task.id}.sock"
        _net_proxy_ctx = NetworkProxy(
            _net_proxy_sock, allowed_hosts,
        )

    # Collect extra paths to RO bind-mount into the sandbox.
    #
    # The task control directory is this parameter's production consumer,
    # and it is a *directory* rather than the composed system prompt file
    # it started as. A bind names one exact path and cannot express a
    # filename pattern, so the per-file entry guarded the standing
    # instructions and left `prompt.txt`, the briefing metadata and every
    # prepared image rendition beside it unguarded — each one a thing
    # somebody had to remember into this list by hand.
    #
    # `build_bwrap_cmd` applies these after every other bind, which is what
    # keeps a read-only entry read-only under any bind added later; the
    # `.developer` carve-out established the pattern. The directory is a
    # sibling of `user_temp_dir` rather than a child, so nothing binds over
    # it today either.
    #
    # Emitted under both profiles. Nothing inside a NATIVE namespace opens
    # the system prompt — NativeBrain reads it in the daemon — but both
    # backends put the prepared attachment paths into the prompt's
    # `Attached files:` section, so a model that decides to `Read` one has
    # to find it, and the tool server runs in there.
    #
    # This is half the protection: `Read`, `Write` and `Edit` run through
    # `ToolEnv` and enter no mount namespace on the unsandboxed shapes. The
    # other half is the pair of entries `executor.native_fs_roots` returns,
    # plus the unconditional seed beside them.
    _extra_ro_binds: list[Path] = [control_dir]

    return TaskRuntime(
        env=env,
        proxy_ctx=_proxy_ctx,
        proxy_sock=_proxy_sock,
        net_proxy_ctx=_net_proxy_ctx,
        net_proxy_sock=_net_proxy_sock,
        extra_ro_binds=_extra_ro_binds,
        authorized_skills=frozenset(authorized_skills),
    )
