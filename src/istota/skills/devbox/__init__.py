"""Devbox skill — the ad-hoc entrance to the user's own development container.

Every verb but ``reset`` speaks the **exec transport** (``devbox_exec_protocol``)
to a server running inside the container, over the per-user Unix socket at
``{exec_socket_dir}/{user_id}/exec.sock``. Nothing here shells out to ``docker``
any more except ``reset``, which recreates a container and is a thing the
transport cannot do and should not learn.

Why the transport and not ``docker exec``
-----------------------------------------

``docker exec`` through the Docker-API allowlist proxy ran the command and lost
its exit status: the CLI's flow is create → start → **inspect**, ``ExitCode``
lives in the inspect response, and the proxy evicted the exec id before relaying
the start so a replay would be denied. Every command came back ``rc 1``
regardless of what it did. Repairing that meant giving an exec id a lifecycle in
the one component whose value was being a stateless allow/deny — so the
transport changed instead, and the proxy was retired with the bind that was its
only consumer.

What that deleted from this file
--------------------------------

``_CONTAINER_TMPFS_MOUNTS``, ``_CONTAINER_OFFLIMITS_PATHS``, ``_check_arrived``,
``_check_source_visible``, ``_normalize_container_path`` and ``_kill_stragglers``
— five guards and a reaper, all of them working around ``docker cp``'s habit of
resolving a container path against the container's *rootfs* rather than against
what the container can see. A daemon-side list of container paths is a guess
about the container's mount table, and that guess is what ISSUE-306 and
ISSUE-312 both were. The server does one ``realpath``-under-root test inside the
container, where the answer is not a guess.

**``skill_host_paths`` is not part of that deletion and is unchanged.** It scopes
the *host* side of ``cp-in`` / ``cp-out``: this CLI runs host-side with the
daemon's filesystem view and the model still picks the host path, which is a
different question from what the container may touch.

The remaining hardening, all of it for ``reset``
------------------------------------------------

* Container name matches ``^[a-zA-Z0-9_.-]+$`` before every docker call.
* ``_check_owned`` reads the ``com.istota.user_id`` label and refuses to proceed
  unless it equals ``ISTOTA_USER_ID`` — against name reuse and stale containers
  from a prior tenant.
* ``reset --yes`` requires ``/home/dev`` to be a real mountpoint inside the
  container before wiping it, so a mis-attached volume does not take a baked-in
  image layer with it.

Usage:
    python -m istota.skills.devbox exec "<command>" [--timeout 300]
    python -m istota.skills.devbox exec-file /local/script [--interpreter python3] [--timeout 300]
    python -m istota.skills.devbox cp-in  /local/path /container/path
    python -m istota.skills.devbox cp-out /container/path /local/path
    python -m istota.skills.devbox status
    python -m istota.skills.devbox reset --yes
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

from istota import devbox_exec_client as _client
from istota import devbox_exec_protocol as proto
from istota.skill_host_paths import resolve_host_path, write_resolved
from istota.skills._cli import error_envelope, run_skill_cli

DEFAULT_MAX_OUTPUT_BYTES = 102_400
MAX_COMMAND_BYTES = 32 * 1024  # `bash -o pipefail -c` argv length cap
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")
_OWNER_LABEL = "com.istota.user_id"

# The two connect-path budgets, both taken from the exec client rather than
# restated. There are two clients of one server now — the shims run
# `devbox_exec_client`, this CLI speaks the wire itself — and two clients
# disagreeing about how long a slow spawn may take shows up as one of them
# reporting a devbox outage the other does not see.
#
# `[developer.container] connect_timeout_seconds` overrides the first, because
# it is "the client's connect budget, and the only timeout on the connect path"
# and the shims already bake it in. This CLI already loads the `Config` that
# carries it to resolve the socket, so reading it costs nothing.
DEFAULT_CONNECT_TIMEOUT_SECONDS = _client.DEFAULT_CONNECT_TIMEOUT_SECONDS
ACK_TIMEOUT_SECONDS = _client.ACK_TIMEOUT_SECONDS

# Neither of the two bounds the command. The *transport* imposes no timeout by
# design — the task's own budget governs — but this process is not the
# transport: the skill proxy runs it as a buffered `subprocess.run(timeout=…)`
# under `security.skill_proxy_timeout`, and on expiry it is killed with its
# envelope unprinted and every byte of output lost. That ceiling is the one a
# caller has to plan around, so `skill.md` names it rather than repeating the
# transport's "no timeout" at a reader for whom it is not true.
#
# How much output is held in memory while waiting. The envelope's cap is a
# rendering decision applied at the end; this is the one that bounds the
# *process*, which matters because the skill proxy runs inside the scheduler
# daemon, so these bytes are charged to the daemon's own memory and to no task
# cgroup. Generous, because the point of leaving `docker exec` is that a build
# log arrives whole — but not unbounded, because `exec 'cat /dev/urandom'` is
# one command away.
MAX_BUFFERED_OUTPUT_BYTES = 64 * 1024 * 1024

# Both re-exported from `istota.devbox_exec_protocol`, which is the vendored
# module the server also reads them from. A second copy of the sentence is a
# second thing to keep in step, and this one is user-facing text.
#
# The note covers the one case `pipefail` newly colours that has a fixed code —
# a downstream `head` or `grep -q` closing the pipe on a producer that was doing
# nothing wrong. The other cannot be recognised and is named in skill.md
# instead: a non-final stage exiting non-zero to *report* something rather than
# to fail, so `grep -c x f | wc -l` now returns 1 where it returned 0.
_SIGPIPE_EXIT = proto.SIGPIPE_EXIT
_SIGPIPE_NOTE = proto.SIGPIPE_NOTE

# Where `exec-file` stages the script it is about to run, when the server does
# not say. One of the server's three file roots, created by the container's
# supervisor at start, and on the `/home/dev` volume rather than on scratch — so
# a staged script is executable and the cleanup `rm -f` removes a file that
# exists.
#
# **The server is asked first** (`stat` reports `staging`) and this is only the
# fallback, which is the same rule the deleted mount lists broke: a container
# path decided on this side of the boundary is a guess about the container's
# filesystem, and the process inside it knows. A server built with a different
# `--staging` would otherwise refuse every `exec-file` with `path_refused`.
_EXEC_STAGING_DIR = "/home/dev/.istota-exec"


def _err(msg: str, **extra) -> dict:
    return error_envelope(msg, **extra)


def _docker_cli() -> str:
    return os.environ.get("ISTOTA_DEVBOX_DOCKER_CLI") or shutil.which("docker") or "docker"


def _user_id() -> str | None:
    uid = os.environ.get("ISTOTA_USER_ID", "").strip()
    return uid or None


def _container_name() -> str | None:
    """Resolve and validate the per-user container name. ``reset`` only."""
    name = os.environ.get("ISTOTA_DEVBOX_CONTAINER", "").strip()
    if not name:
        uid = _user_id()
        if not uid:
            return None
        name = f"devbox-{uid}"
    if not _NAME_PATTERN.match(name):
        return None
    return name


def _max_output_bytes() -> int:
    raw = os.environ.get("ISTOTA_DEVBOX_MAX_OUTPUT_BYTES", "")
    if not raw:
        return DEFAULT_MAX_OUTPUT_BYTES
    try:
        return max(1024, int(raw))
    except ValueError:
        return DEFAULT_MAX_OUTPUT_BYTES


def _truncate(data: bytes, cap: int) -> str:
    if len(data) <= cap:
        return data.decode("utf-8", "replace")
    head = data[:cap].decode("utf-8", "replace")
    return f"{head}\n…[truncated: {len(data) - cap} more bytes]"


# ---- Where the socket is ---------------------------------------------------


class _Transport:
    """Where the socket is, and how long to wait for it."""

    def __init__(self, path: str, connect_timeout: float) -> None:
        self.path = path
        self.connect_timeout = connect_timeout


def _transport_settings() -> "tuple[_Transport | None, str | None]":
    """``(settings, error)`` — the exec socket for this task's user.

    Read from **configuration in this host-side process**, never from the
    environment. The distinction is the one the shims are built on: a shim is a
    child of the model's own shell, so an env-supplied socket path is a path the
    model chooses — ``ISTOTA_DEVBOX_EXEC_SOCKET=/tmp/mine`` would get an ``ok``
    acknowledgement and a fabricated exit 0 from a socket the model wrote. This
    CLI is spawned by the skill proxy outside the sandbox and loads the same
    config file the daemon did, which is not something the model can reach.

    **The backend is checked here, and it is a refusal rather than a connect
    failure on purpose.** ``exec_socket_dir`` carries a non-empty default, so a
    path always resolves — but the Ansible role provisions the socket directory
    and sets ``ISTOTA_EXEC_SOCKET`` on the container only under
    ``backend = devbox``. On the shipped pair ``devbox.enabled = true`` with
    ``backend = none``, every verb but ``reset`` would otherwise fail with "the
    container may be down", naming neither the cause nor the fix — on a
    deployment where nothing is down and where this skill worked before the
    transport existed. So it names the key to set instead.
    """
    uid = _user_id()
    if not uid:
        return None, (
            "No user id in the environment (ISTOTA_USER_ID), so there is no "
            "per-user devbox socket to resolve. This CLI must run under a task."
        )
    try:
        from istota import config as config_module

        config = config_module.load_config()
        backend = config_module.container_backend(config)
        path = config_module.exec_socket_path(config, uid)
        budget = getattr(
            getattr(getattr(config, "developer", None), "container", None),
            "connect_timeout_seconds",
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — JSON envelope is the contract
        return None, f"could not resolve the devbox exec socket from config: {e}"
    if backend != config_module.CONTAINER_BACKEND_DEVBOX:
        return None, (
            "This deployment runs development work on the host, so no exec "
            "server is provisioned inside the devbox and every verb but `reset` "
            "has nothing to talk to. That is derived from [devbox] enabled "
            "together with developer.enabled and developer.repos_dir; turn them "
            "on and re-run the deploy. This is a deployment setting; a task "
            "cannot change it."
        )
    if path is None:
        return None, (
            "No exec socket directory is configured "
            "([developer.container] exec_socket_dir)."
        )
    # A zero or negative budget would put the socket in non-blocking mode, where
    # `connect` raises at once and every verb reports an outage. Not a timeout
    # anybody meant to ask for, so it reads as "use the default".
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        budget = DEFAULT_CONNECT_TIMEOUT_SECONDS
    return _Transport(str(path), float(budget)), None


# ---- The conversation ------------------------------------------------------


class _Refused(Exception):
    """The server acknowledged an error, or this side could not get that far."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class _Reply:
    """What one request/response exchange produced."""

    def __init__(self) -> None:
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.control: list[dict] = []
        self.terminal: dict = {}


_ACK_TIMEOUT_MESSAGE = (
    "{path}: the server accepted the connection and did not acknowledge within "
    f"{ACK_TIMEOUT_SECONDS:g}s; whether anything ran is unknown"
)


def _read_ack(sock: socket.socket, path: str) -> tuple[dict, bytes]:
    """Read the acknowledgement line and hand back whatever followed it.

    A deadline across the whole line rather than a per-``recv`` timeout:
    ``settimeout`` restarts on every call, so a peer sending one non-newline
    byte inside each window would hold this open indefinitely.
    """
    deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
    buf = b""
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Refused(_ACK_TIMEOUT_MESSAGE.format(path=path))
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(proto.CHUNK_BYTES)
        except TimeoutError as e:
            # `settimeout` makes `recv` *raise* on expiry rather than return, so
            # the deadline branch above only ever fires when the clock ran out
            # between two reads. Without this the ordinary hang — a server that
            # accepted the connection and never wrote — escaped as a bare
            # `TimeoutError`, and the crafted message was unreachable.
            raise _Refused(_ACK_TIMEOUT_MESSAGE.format(path=path)) from e
        if not chunk:
            raise _Refused(
                f"{path}: the server closed the connection without acknowledging"
            )
        buf += chunk
        if len(buf) > proto.MAX_REQUEST_BYTES:
            raise _Refused(f"{path}: acknowledgement line exceeds the request cap")
    line, _, rest = buf.partition(b"\n")
    return proto.decode_ack(line), rest


def _send_body(sock: socket.socket, body: bytes) -> None:
    """Stream a ``write_file`` body as stream-0 frames.

    **No trailing ``stdin_eof``, and that is a fix rather than an omission.**
    The declared ``size`` is the terminator: the server reads exactly that many
    bytes, stops reading, renames the file, sends its terminal frame and closes.
    A marker sent after the last body byte therefore races that close and
    arrives as ``EPIPE`` — intermittently, on a small file, where the whole
    exchange fits in one round trip. The marker exists for a body that ends
    *early*, which this function never produces because it sends exactly what it
    declared.
    """
    view = memoryview(body)
    while view:
        chunk = view[: proto.CHUNK_BYTES]
        sock.sendall(proto.pack_frame(proto.STREAM_STDIN, bytes(chunk)))
        view = view[len(chunk) :]


def _pump(sock: socket.socket, leftover: bytes, path: str) -> _Reply:
    """Collect frames until the terminal one.

    Accumulation is bounded by ``MAX_BUFFERED_OUTPUT_BYTES``. Past it the
    connection is dropped rather than the read continuing — which is what makes
    the bound real: this process is the skill proxy's child and the skill proxy
    runs inside the scheduler daemon, so the bytes are charged to the daemon's
    memory and to no task cgroup. Dropping the connection is also the server's
    reap signal, so the command that was producing them stops too.

    A truncation here is recorded on the reply and surfaces as an *error*
    envelope, never as a status. What the command went on to do is unknown at
    that point, and the whole premise of this transport is not reporting a
    status it does not have.
    """
    reply = _Reply()
    decoder = proto.FrameDecoder()
    pending = leftover
    while True:
        for stream, payload in decoder.feed(pending):
            if stream == proto.STREAM_STDOUT:
                reply.stdout.extend(payload)
            elif stream == proto.STREAM_STDERR:
                reply.stderr.extend(payload)
            elif stream == proto.STREAM_CONTROL:
                body = proto.decode_control(payload)
                if proto.is_terminal(body):
                    reply.terminal = body
                    return reply
                reply.control.append(body)
            else:
                raise _Refused(
                    f"{path}: the server sent stream {stream}, which travels "
                    "the other way"
                )
        if len(reply.stdout) + len(reply.stderr) > MAX_BUFFERED_OUTPUT_BYTES:
            raise _Refused(
                f"{path}: the command produced more than "
                f"{MAX_BUFFERED_OUTPUT_BYTES // (1024 * 1024)} MiB of output, "
                f"which is more than this process will hold; the connection was "
                f"dropped and the command killed with it, so its fate is unknown"
            )
        pending = sock.recv(proto.CHUNK_BYTES)
        if not pending:
            # The one case with no exit status at all, and it is not
            # hypothetical: it is what a container restart looks like. Never
            # reported as success — the command may well have completed, and
            # saying so is the honest answer.
            raise _Refused(
                f"{path}: the connection ended before the command reported a "
                f"status; its fate is unknown and it may have completed"
            )


def _require_transport() -> "_Transport":
    """The transport settings, or a refusal naming what could not be resolved."""
    settings, err = _transport_settings()
    if err:
        raise _Refused(err)
    return settings


def _converse(
    request: bytes,
    *,
    body: bytes | None = None,
    transport: "_Transport | None" = None,
) -> _Reply:
    """One request, one reply. Raises ``_Refused`` on anything short of that.

    ``transport`` is optional so a verb making several calls resolves it once —
    ``exec-file`` is four exchanges, and re-reading the config file for each is
    four answers where one is wanted.
    """
    settings = transport or _require_transport()
    path = settings.path
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as e:  # pragma: no cover - AF_UNIX is always available here
        raise _Refused(f"cannot create a socket: {e}") from e
    with sock:
        try:
            sock.settimeout(settings.connect_timeout)
            sock.connect(path)
            sock.sendall(request)
        except OSError as e:
            raise _Refused(
                f"could not reach the devbox exec transport at {path}: {e}. "
                f"The container may be down, or this deployment may not run one."
            ) from e

        ack, leftover = _read_ack(sock, path)

        if ack.get("status") != "ok":
            # The ack is sent *after* the path checks and after a successful
            # spawn, so an error here means nothing ran.
            raise _Refused(
                f"{ack.get('code')}: {ack.get('message')}", code=ack.get("code")
            )
        if not proto.supported_protocol(ack.get("protocol")):
            raise _Refused(
                f"the devbox speaks protocol {ack.get('protocol')!r}, which this "
                f"build does not know (it speaks "
                f"{sorted(proto.SUPPORTED_PROTOCOLS)})"
            )

        # Off for everything past the acknowledgement, and **before** the body
        # goes: a build can go minutes without a byte, the server's own idle
        # backstop is what catches a connection whose peer went away, and
        # `_read_ack` leaves behind whatever slice of its deadline was still
        # unspent — which would then bound a multi-megabyte `sendall`.
        sock.settimeout(None)

        if body is not None:
            _send_body(sock, body)

        return _pump(sock, leftover, path)


def _terminal_fault(terminal: dict) -> str | None:
    """The fault a terminal frame carries, or None.

    Two shapes, and both have to be read. The server acknowledges ``exec``,
    ``read_file`` and ``write_file`` *before* it streams — that ordering is what
    makes an ``ok`` ack mean the command is running — so every failure after
    that point arrives here rather than in the acknowledgement. It may come with
    no ``exit_code`` at all, and it may come *alongside* a real one, because
    ``istota-exec-serve`` folds an input error into the body after
    ``terminal_frame`` has already put ``waitpid``'s answer in it.

    So a caller cannot read ``exit_code`` alone. A frame carrying ``error`` is a
    fault whatever the status says.
    """
    error = terminal.get("error")
    if error:
        message = terminal.get("message") or ""
        return f"{error}: {message}".strip().rstrip(":").strip()
    if terminal.get("exit_code") is None:
        return (
            "the devbox reported no exit status for this command; its fate is "
            "unknown and it may have completed"
        )
    return None


def _envelope(reply: _Reply, started: float) -> dict:
    """The JSON envelope a language model reads.

    The transport caps neither output nor time; this does cap output, because
    the caller here is a model rather than a terminal and a two-gigabyte stdout
    inlined into a prompt is its own failure.
    """
    cap = _max_output_bytes()
    code = reply.terminal.get("exit_code")
    result: dict = {
        "status": "ok",
        "exit_code": code,
        "stdout": _truncate(bytes(reply.stdout), cap),
        "stderr": _truncate(bytes(reply.stderr), cap),
        "duration_ms": reply.terminal.get(
            "duration_ms", int((time.monotonic() - started) * 1000)
        ),
    }
    for key in ("signal", "reason", "truncated"):
        value = reply.terminal.get(key)
        if value:
            result[key] = value
    note = reply.terminal.get("note")
    if not note and code == _SIGPIPE_EXIT and not reply.terminal.get("signal"):
        note = _SIGPIPE_NOTE
    if note:
        result["note"] = note
    fault = _terminal_fault(reply.terminal)
    if fault:
        # Never an `exit_code`, and never `status: "ok"` beside a fault. The
        # output stays, because it is what a reader has to go on.
        return _err(fault, stdout=result["stdout"], stderr=result["stderr"])
    return result


# ---- Docker, for `reset` alone ---------------------------------------------


def _run_docker(args: list[str], timeout: int) -> tuple[int, bytes, bytes]:
    """Run ``docker …`` and return ``(rc, stdout, stderr)``. Raises on timeout.

    Survives for ``reset``, which recreates a container. It shells ``docker``
    with the daemon's own environment and sets no ``DOCKER_HOST``, so it has
    always talked to the real socket rather than to the allowlist proxy — which
    is why retiring that proxy costs this verb nothing.
    """
    cmd = [_docker_cli(), *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _inspect(container: str, template: str, *, timeout: int = 10) -> tuple[int, str]:
    rc, out, _ = _run_docker(
        ["inspect", "-f", template, container], timeout=timeout,
    )
    return rc, out.decode("utf-8", "replace").strip()


def _check_owned(container: str) -> str | None:
    """Return None when the container exists, is running, and is owned by
    the current user — otherwise return an error string.

    Ownership is encoded as a Docker label (``com.istota.user_id=<user_id>``)
    written by the Ansible-rendered compose template. Containers without
    the label are accepted only when ``ISTOTA_USER_ID`` is unset (CLI
    smoke-tests on dev machines that don't deploy the label).
    """
    rc, running = _inspect(container, "{{.State.Running}}")
    if rc != 0:
        return f"Devbox container '{container}' does not exist."
    if running != "true":
        return f"Devbox container '{container}' is not running."
    uid = _user_id()
    if not uid:
        return None
    rc2, label = _inspect(container, "{{index .Config.Labels \"" + _OWNER_LABEL + "\"}}")
    if rc2 != 0:
        # Inspect already succeeded above; missing label means the container
        # was provisioned outside Ansible. Accept but don't enforce.
        return None
    if not label:
        return None  # legacy / hand-built container — same lenient stance
    if label != uid:
        return (
            f"Devbox container '{container}' is owned by '{label}', not '{uid}'. "
            "Refusing to operate."
        )
    return None


# ---- Host paths ------------------------------------------------------------


def _resolve_host_path(p: Path, *, must_exist: bool) -> tuple[Path | None, str | None]:
    """Validate a host path and hand back the one to actually operate on.

    The rule itself lives in ``istota.skill_host_paths`` — ``kv set
    --value-file`` needs the identical scoping, and two copies of a boundary
    check drift. **Use the returned path**: acting on the caller-supplied one
    re-walks its symlinks and reopens the window the check closed.

    This is the host side, and it is unchanged by the move onto the transport.
    The container side is the server's business, decided inside the container;
    this one is about a path the model picked for a CLI running host-side with
    the daemon's whole filesystem view, which is a different question.
    """
    return resolve_host_path(
        p, writable=not must_exist, operation="cp-in/cp-out",
    )


def _confirm_write(reply: _Reply, path: str, expected: int) -> str | None:
    """Why a ``write_file`` did not land, or None.

    **The reply is not optional and dropping it is the ISSUE-306 shape in a new
    envelope.** ``istota-exec-serve`` acknowledges ``write_file`` *before* it
    reads the body — it has to, since the ack is what tells the caller to start
    sending — so ENOSPC on the write, a failed ``chmod`` or ``replace``, and a
    body longer than declared all arrive in the terminal frame and nowhere else.
    A caller that looks only at the ack has been told a file exists that does
    not.

    Two things are checked because they fail separately: the frame may carry a
    fault, and it carries the count the server actually wrote.
    """
    fault = _terminal_fault(reply.terminal)
    if fault:
        return f"the devbox refused the write to {path}: {fault}"
    written = reply.terminal.get("size")
    if written != expected:
        return (
            f"the devbox reported writing {written} of {expected} bytes to "
            f"{path}; treat the destination as incomplete"
        )
    return None


def _reports_refusals(fn):
    """Turn a refusal into the JSON envelope, at the verb rather than at ``main``.

    ``main`` catches these too, and that stays as the backstop for anything a
    verb does not. This is here so a verb *is* its envelope: a caller — a test,
    or anything importing the module — gets the same answer as the CLI prints,
    rather than a dict on one path and an exception on the other.
    """
    @functools.wraps(fn)
    def wrapper(args):
        try:
            return fn(args)
        except _Refused as e:
            return _err(e.message, **({"code": e.code} if e.code else {}))
        except proto.ProtocolError as e:
            return _err(str(e), code=e.code)
        except OSError as e:
            # The case Design 4 calls "not hypothetical": a container restart
            # mid-command. `_converse` guards only `connect` and the request
            # `sendall`; a peer that vanishes later raises `BrokenPipeError` out
            # of `_send_body` or `ConnectionResetError` out of `_pump`'s `recv`,
            # both `OSError` and neither of the two above. Without this the
            # answer was a traceback for an importing caller and a `main`
            # fallback line for the CLI — two answers to one question, which is
            # what this decorator exists to stop. Never an `exit_code`: the
            # command may well have completed.
            return _err(
                f"the devbox connection failed mid-command: {e}; the command's "
                f"fate is unknown and it may have completed"
            )
    return wrapper


def _validate_command(command: str) -> str | None:
    if "\x00" in command:
        return "NUL byte in command — refusing."
    if len(command.encode("utf-8", "replace")) > MAX_COMMAND_BYTES:
        return f"Command exceeds {MAX_COMMAND_BYTES}-byte cap — refusing."
    return None


# ---- Verbs -----------------------------------------------------------------


@_reports_refusals
def cmd_exec(args) -> dict:
    err = _validate_command(args.command)
    if err:
        return _err(err)
    # `cwd: null`, never a path. "Install something, no repository involved" has
    # no repository to stand in, and every ambiguous name — `/tmp`, `/home/…`,
    # `/usr/src` — exists in both namespaces meaning different things. So the
    # server names the directory (its own `/home/dev`), which is where
    # `docker exec -w /home/dev` already put this verb. A caller wanting
    # somewhere else writes one `cd` into the shell string it already sends.
    #
    # `timeout: 0` — no default. The task's own budget governs, and a caller
    # that wants a kill passes `--timeout`, which the server enforces on the
    # process group.
    request = proto.encode_exec_request(
        shell=args.command, cwd=None, stdin=False, timeout=args.timeout or 0,
    )
    started = time.monotonic()
    return _envelope(_converse(request), started)


@_reports_refusals
def cmd_exec_file(args) -> dict:
    local, path_err = _resolve_host_path(Path(args.path), must_exist=True)
    if path_err:
        return _err(path_err)
    if not local.is_file():
        return _err(f"Script not found: {local}")

    # Keyed on the script name + pid so parallel exec-file calls do not collide.
    # The basename passes the same regex as the container name, so a hostile
    # filename cannot escape the staging directory.
    base = local.name
    if not _NAME_PATTERN.match(base):
        return _err(f"Refusing unusual script basename: {base!r}")

    try:
        body = local.read_bytes()
    except OSError as e:
        return _err(f"could not read {local}: {e}")

    # One resolution for all four exchanges below.
    transport = _require_transport()
    remote = f"{_staging_dir(transport)}/exec_{os.getpid()}_{base}"

    # 0755 on the way in. The server applies the mode with an explicit chmod
    # that defeats the umask, so both branches below are covered — the
    # interpreter one needs read, the no-interpreter one needs execute, and
    # granting only the second was the shape of an older bug.
    staged = _converse(
        proto.encode_write_file_request(path=remote, size=len(body), mode=0o755),
        body=body,
        transport=transport,
    )
    # Checked, not assumed. Unchecked, a staging write that failed after the ack
    # left the exec below running a path that was never created — and the model
    # read "can't open file" and concluded its own script was wrong. Worse on a
    # reused pid, where a stale copy from an earlier call is what runs.
    write_err = _confirm_write(staged, remote, len(body))
    if write_err:
        return _err(write_err)

    interpreter = args.interpreter or _guess_interpreter(local)
    argv = [interpreter, remote] if interpreter else [remote]
    started = time.monotonic()
    try:
        # `argv`, not `shell`: nothing here is a shell string, and a script
        # owns its own shell options. `exec-file` deliberately imposes no
        # `pipefail` — the file has a shebang line where `set -euo pipefail` is
        # the idiom, and the no-interpreter branch runs whatever interpreter the
        # file names.
        reply = _converse(
            proto.encode_exec_request(
                argv=argv, cwd=None, stdin=False, timeout=args.timeout or 0,
            ),
            transport=transport,
        )
    finally:
        # Scratch copies, cleaned up whatever happened. Best-effort: a devbox
        # that went away between the two calls has nothing to clean, and saying
        # so would replace the real error.
        try:
            _converse(
                proto.encode_exec_request(
                    argv=["rm", "-f", remote], cwd=None, stdin=False, timeout=30,
                ),
                transport=transport,
            )
        except Exception:  # noqa: BLE001 — best-effort cleanup, never raises
            pass
    return _envelope(reply, started)


def _staging_dir(transport: "_Transport") -> str:
    """The directory the *server* stages into, asked rather than assumed.

    ``stat`` reports it, so a server started with a different ``--staging``
    still works instead of refusing every ``exec-file`` with ``path_refused``.
    The fallback is the compiled-in default, for a server too old to say.
    """
    reply = _converse(proto.encode_stat_request(), transport=transport)
    stat = reply.control[0] if reply.control else {}
    staging = stat.get("staging")
    if isinstance(staging, str) and staging.startswith("/"):
        return staging.rstrip("/")
    return _EXEC_STAGING_DIR


def _guess_interpreter(path: Path) -> str | None:
    suffix = path.suffix.lower()
    return {
        ".py": "python3",
        ".sh": "bash",
        ".bash": "bash",
        ".js": "node",
        ".rb": "ruby",
    }.get(suffix)


@_reports_refusals
def cmd_cp_in(args) -> dict:
    src, path_err = _resolve_host_path(Path(args.src), must_exist=True)
    if path_err:
        return _err(path_err)
    if src.is_dir():
        return _err(
            f"{src} is a directory. The transport moves one file per call — "
            "tar it on the host and copy the archive in, or run the copy as a "
            "command in the devbox."
        )
    try:
        body = src.read_bytes()
    except OSError as e:
        return _err(f"could not read {src}: {e}")
    if len(body) > proto.MAX_WRITE_FILE_BYTES:
        return _err(
            f"{src} is {len(body)} bytes, over the "
            f"{proto.MAX_WRITE_FILE_BYTES // (1024 * 1024)} MiB write cap."
        )
    # No arrival read-back and no container-path list. The server resolves the
    # destination with `realpath` inside the container and refuses anything
    # outside its file roots, so a write that is acknowledged landed where the
    # container can see it — which is what the read-back was approximating from
    # the wrong side of the boundary.
    reply = _converse(
        proto.encode_write_file_request(path=args.dest, size=len(body)),
        body=body,
    )
    write_err = _confirm_write(reply, args.dest, len(body))
    if write_err:
        return _err(write_err)
    return {"status": "ok", "src": str(src), "dest": args.dest, "size": len(body)}


@_reports_refusals
def cmd_cp_out(args) -> dict:
    # The container side first, then the host path. `_resolve_host_path`
    # creates the destination's parents (inside the allowlist, after its own
    # containment check), so resolving before the source is known to be
    # readable left an empty tree in the user's workspace behind every refusal.
    reply = _converse(proto.encode_read_file_request(path=args.src))

    # **Checked before anything reaches the host disk.** The server
    # acknowledges `read_file` *before* it streams, so a failure after that
    # point — the file growing past the read cap mid-stream, an OSError on the
    # read, an internal fault — arrives only in the terminal frame. A caller
    # reading `reply.stdout` and stopping there writes half a tarball to the
    # host and reports success, which is the ISSUE-312 shape exactly: bytes of
    # unknown provenance the caller goes on to read. The count is compared as
    # well as the fault, since the server reports what it actually sent.
    fault = _terminal_fault(reply.terminal)
    if fault:
        return _err(
            f"the devbox could not finish reading {args.src}: {fault}. Nothing "
            f"was written to the host — the bytes that did arrive are a "
            f"fragment of unknown length."
        )
    sent = reply.terminal.get("size")
    if sent != len(reply.stdout):
        return _err(
            f"the devbox reported sending {sent} bytes of {args.src} and "
            f"{len(reply.stdout)} arrived; nothing was written to the host."
        )

    dest, path_err = _resolve_host_path(Path(args.dest), must_exist=False)
    if path_err:
        return _err(path_err)
    try:
        # `write_resolved`, not `dest.write_bytes`: the resolution refused a
        # symlink standing at the leaf as of its own check, and a plain open
        # follows one planted after it — out of the workspace, as the daemon
        # user. This is the writer `skill_host_paths` names when it explains
        # why that check exists.
        write_resolved(dest, bytes(reply.stdout))
    except OSError as e:
        return _err(f"could not write {dest}: {e}")
    return {
        "status": "ok",
        "src": args.src,
        "dest": str(dest),
        "size": len(reply.stdout),
    }


@_reports_refusals
def cmd_status(args) -> dict:
    """Container facts from Docker, plus what the transport says about itself.

    Two halves, and neither substitutes for the other: `docker inspect` says the
    container is running, and a `stat` over the socket says the *server inside
    it* is answering, which is the thing every other verb depends on.
    """
    info: dict = {"status": "ok"}
    container = _container_name()
    if container:
        fmt = (
            "{{.State.Running}}|{{.State.StartedAt}}|{{.Config.Image}}|"
            "{{.Id}}|{{.RestartCount}}|{{index .Config.Labels \""
            + _OWNER_LABEL + "\"}}"
        )
        rc, out, _ = _run_docker(["inspect", "-f", fmt, container], timeout=10)
        if rc == 0:
            parts = out.decode("utf-8", "replace").strip().split("|")
            while len(parts) < 6:
                parts.append("")
            running, started_at, image, cid, restart_count, owner = parts[:6]
            info.update({
                "container": container,
                "running": running == "true",
                "started_at": started_at,
                "image": image,
                "id": cid[:12],
                "restart_count": _to_int(restart_count),
                "owner": owner or None,
            })
        else:
            info["container"] = container
            info["running"] = None
            info["container_error"] = f"could not inspect '{container}'"

    try:
        reply = _converse(proto.encode_stat_request())
    except (_Refused, proto.ProtocolError, OSError) as e:
        # All three, because this verb's whole point is that the two halves fail
        # separately. `OSError` covers a server that accepted the connection and
        # then hung: with only `_Refused` caught, a dead transport took the
        # container facts down with it and the verb raised — the opposite of
        # what the docstring above promises.
        message = getattr(e, "message", None) or str(e)
        info["transport"] = {"reachable": False, "error": message}
        return info

    stat = reply.control[0] if reply.control else {}
    info["transport"] = {"reachable": True, **stat}
    return info


def _to_int(s: str) -> int | None:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def cmd_reset(args) -> dict:
    """The one verb spoken entirely in Docker, because it restarts a container.

    It wipes `/home/dev` and `docker restart`s the box — it does not *create*
    one, so it is not the way to pick up a rebuilt image. The transport cannot
    do even the restart and should not learn how: a server inside a container
    cannot restart the container it is inside. So `_check_owned` survives for
    this verb and nowhere else; `_run_docker` also survives for `cmd_status`,
    which asks Docker about the container beside what it asks the server.
    """
    container = _container_name()
    if not container:
        return _err("No devbox configured.")
    if not args.yes:
        return _err(
            "Refusing to reset without --yes. This wipes /home/dev for the user."
        )
    ownership_err = _check_owned(container)
    if ownership_err:
        return _err(ownership_err)
    # Refuse to wipe /home/dev unless it's actually a mountpoint — otherwise
    # we'd be wiping a baked-in image layer the container couldn't restore
    # from a `docker restart`.
    rc_mp, _, _ = _run_docker(
        ["exec", "-u", "root", container, "mountpoint", "-q", "/home/dev"],
        timeout=10,
    )
    if rc_mp != 0:
        return _err(
            "/home/dev is not a mountpoint inside the container — refusing "
            "to wipe (the volume is likely misconfigured)."
        )
    rc, _, stderr = _run_docker(
        ["exec", "-u", "root", container, "sh", "-c",
         "find /home/dev -mindepth 1 -maxdepth 1 -exec rm -rf {} +"],
        timeout=120,
    )
    if rc != 0:
        return _err(stderr.decode("utf-8", "replace").strip() or "wipe failed")
    rc2, _, stderr2 = _run_docker(["restart", container], timeout=60)
    if rc2 != 0:
        return _err(stderr2.decode("utf-8", "replace").strip() or "restart failed")
    return {"status": "ok", "container": container, "reset": True}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m istota.skills.devbox",
        description="Per-user devbox container — exec, copy, inspect.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_exec = sub.add_parser("exec", help="Run a command inside the devbox")
    p_exec.add_argument("command", help="Shell command to run (executed via bash -o pipefail -c)")
    p_exec.add_argument("--timeout", type=int, help="Per-exec timeout (s); none by default")

    p_xf = sub.add_parser("exec-file", help="Copy a local script in and run it")
    p_xf.add_argument("path", help="Local file path")
    p_xf.add_argument("--interpreter", help="Interpreter (python3, bash, node, ruby). Default: guess from suffix")
    p_xf.add_argument("--timeout", type=int)

    p_in = sub.add_parser("cp-in", help="Copy a file into the devbox")
    p_in.add_argument("src", help="Local path")
    p_in.add_argument("dest", help="Path inside the container")

    p_out = sub.add_parser("cp-out", help="Copy a file out of the devbox")
    p_out.add_argument("src", help="Path inside the container")
    p_out.add_argument("dest", help="Local path")

    sub.add_parser("status", help="Devbox state, image, uptime, transport liveness")

    p_reset = sub.add_parser("reset", help="Wipe /home/dev and restart container")
    p_reset.add_argument("--yes", action="store_true", help="Required confirmation flag")

    return p


_DISPATCH = {
    "exec": cmd_exec,
    "exec-file": cmd_exec_file,
    "cp-in": cmd_cp_in,
    "cp-out": cmd_cp_out,
    "status": cmd_status,
    "reset": cmd_reset,
}


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    def describe(exc: BaseException) -> dict:
        if isinstance(exc, _Refused):
            return _err(exc.message, **({"code": exc.code} if exc.code else {}))
        if isinstance(exc, proto.ProtocolError):
            return _err(str(exc), code=exc.code)
        if isinstance(exc, FileNotFoundError):
            # docker CLI not on PATH — `reset` and `status` only.
            return _err(f"Docker CLI not available: {exc}")
        return _err(f"{type(exc).__name__}: {exc}")

    run_skill_cli(
        _DISPATCH, args, command=args.subcommand, indent=None,
        on_exception=describe, error_ensure_ascii=False,
    )
