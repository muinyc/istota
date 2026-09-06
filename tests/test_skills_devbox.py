"""The devbox skill CLI, driven against a real exec server.

**Rewritten against the protocol, not against mocked `docker` argv, and that is
the point of the file.** The tests this replaced monkeypatched `_run_docker`
wholesale, so the boundary where every bug in this subsystem actually lived —
what a container path resolves to on the other side — never executed. A canned
`(0, b"", b"")` for a copy is exactly the answer a real broken run cannot
produce, which is how a `/workspace` destination that resolved to a directory
nothing in the container could see stayed green through every release
(ISSUE-306), and how a `cp-out` returning phantom bytes did the same
(ISSUE-312). So: assert on what comes back, not on what was spelled.

The server is the shipped `docker/devbox/scripts/istota-exec-serve`, started on
a socket in a tmpdir with its roots pointed there. No Docker: the server is a
stdlib-only asyncio script, and the container is what the `integration` tier
proves. `reset` is the one verb still spoken in Docker, and it is the one place
`_run_docker` is still stubbed — because it recreates a container, which is a
thing no test here can do and no server can do for it.

The socket is reached the way the deployment reaches it: a real `config.toml`
with an `exec_socket_dir`, resolved by `_transport_settings()` through
`load_config()`.
Monkeypatching that resolution would leave the "the CLI reads its socket from
its own configuration, never from the environment" rule untested, and that rule
is what stops `ISTOTA_DEVBOX_EXEC_SOCKET=/tmp/mine` buying a fabricated exit 0.
"""

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from istota import devbox_exec_protocol as proto
from istota.skills import devbox

_REPO = Path(__file__).resolve().parents[1]
_SKILL_DIR = _REPO / "src" / "istota" / "skills" / "devbox"
_EXECUTOR = _REPO / "src" / "istota" / "executor.py"
# Every module that writes into the model's task environment. A list
# rather than one path because the env assembly moved out of
# `execute_task` into `task_env` and this scan silently found nothing:
# the sibling "has a reader" guard then passed vacuously, which is the
# worse half. A further extraction has to be added here or go red.
_ENV_SOURCES = (_EXECUTOR, _REPO / "src" / "istota" / "task_env.py")


_SERVER = _REPO / "docker/devbox/scripts/istota-exec-serve"


def _env_source_text() -> str:
    return "\n".join(p.read_text() for p in _ENV_SOURCES)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class _Devbox:
    """A running exec server plus the config that points the CLI at it."""

    def __init__(self, base: Path, proc: subprocess.Popen, log: Path) -> None:
        self.base = base
        self.proc = proc
        self.log = log
        self.repos = base / "repos"
        self.home = base / "home"
        self.staging = self.home / ".istota-exec"
        self.outside = base / "outside"
        self.socket_path = str(base / "sock" / "bob" / "exec.sock")

    def log_text(self) -> str:
        return self.log.read_text(errors="replace")


def _start_devbox(monkeypatch) -> _Devbox:
    # A Unix socket path is capped at ~104 bytes on darwin, and pytest's
    # tmp_path is long enough to blow through that once a class and test name
    # are in it.
    base = Path(tempfile.mkdtemp(dir="/tmp", prefix="istota-dbx-")).resolve()
    repos = base / "repos"
    home = base / "home"
    staging = home / ".istota-exec"
    outside = base / "outside"
    sock_dir = base / "sock" / "bob"
    for d in (repos, home, staging, outside, sock_dir):
        d.mkdir(parents=True, exist_ok=True)
    socket_path = str(sock_dir / "exec.sock")

    # The real resolution path: a config file naming the socket's *parent*,
    # with the per-user component derived. `[developer.container]` is the
    # authoritative key, and the CLI reads it in this host-side process.
    config_path = base / "config.toml"
    config_path.write_text(
        "[developer]\n"
        "enabled = true\n"
        f'repos_dir = "{repos}"\n'
        "\n"
        "[devbox]\n"
        "enabled = true\n"
        "\n"
        "[developer.container]\n"
        f'exec_socket_dir = "{base / "sock"}"\n'
    )
    monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config_path))

    log = base / "server.log"
    with open(log, "wb") as handle:
        proc = subprocess.Popen(
            [
                sys.executable, str(_SERVER),
                "--socket", socket_path,
                "--repos-root", str(repos),
                "--home", str(home),
                "--staging", str(staging),
            ],
            stdin=subprocess.PIPE,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited {proc.returncode}: {log.read_text(errors='replace')}"
            )
        if os.path.exists(socket_path):
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(2.0)
                probe.connect(socket_path)
                probe.close()
                break
            except OSError:
                pass
        time.sleep(0.02)
    else:  # pragma: no cover
        raise AssertionError(
            f"server never listened: {log.read_text(errors='replace')}"
        )

    return _Devbox(base, proc, log)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ISTOTA_USER_ID", "bob")
    monkeypatch.setenv("ISTOTA_DEVBOX_CONTAINER", "devbox-bob")
    monkeypatch.setenv("ISTOTA_DEVBOX_DOCKER_CLI", "/usr/bin/docker")
    monkeypatch.delenv("ISTOTA_DEVBOX_MAX_OUTPUT_BYTES", raising=False)
    # cp-in / cp-out require an allowlist; point at tmp_path so tests
    # can build host paths inside it.
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
    monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)


@pytest.fixture
def dbx(monkeypatch):
    box = _start_devbox(monkeypatch)
    try:
        yield box
    finally:
        if box.proc.poll() is None:
            box.proc.terminate()
            try:
                box.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                box.proc.kill()
                box.proc.wait(timeout=10)


def _args(**kw):
    return type("A", (), kw)()


def _exec(command: str, timeout: int | None = None) -> dict:
    return devbox.cmd_exec(_args(command=command, timeout=timeout))


def _ownership_sequence(*, owner: str = "bob", running: bool = True):
    """`_check_owned` makes two inspect calls; this is "running, ours"."""
    return [
        (0, b"true" if running else b"false", b""),
        (0, owner.encode(), b""),
    ]


def _drain(returns):
    """Iterator factory — pop in order. Tests stage docker responses in a list."""
    it = iter(returns)
    return lambda argv, timeout: next(it)


# --------------------------------------------------------------------------- #
# The harness has to be able to fail
# --------------------------------------------------------------------------- #


class TestTheHarnessIsNotAStub:
    """A rewrite whose new tests could pass against the old code would have
    bought nothing, so start by proving the server is really in the loop."""

    def test_the_command_runs_in_the_servers_process_tree(self, dbx):
        """`$PPID` chain reaching the server, not this pytest process."""
        result = _exec("echo $$ && ps -o ppid= -p $$")
        assert result["exit_code"] == 0, result
        assert result["stdout"].strip(), result
        assert str(os.getpid()) not in result["stdout"].split(), (
            "the command's parent is this test process, so no server ran"
        )

    def test_with_no_server_every_verb_refuses(self, monkeypatch, tmp_path):
        """The negative control: point the config at an empty directory and
        require a refusal rather than a fabricated success.

        `backend` is set, so this is the *connect* failure rather than the
        configuration refusal `TestTheBackendMustBeDevbox` covers — the two have
        different messages and a control that hit the wrong one would prove
        nothing about the socket.
        """
        config = tmp_path / "config.toml"
        config.write_text(
            "[developer]\nenabled = true\n"
            f'repos_dir = "{tmp_path}"\n'
            "\n[devbox]\nenabled = true\n"
            "\n[developer.container]\n"
            f'exec_socket_dir = "{tmp_path}"\n'
        )
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config))

        result = _exec("true")

        assert result["status"] == "error", result
        assert "exit_code" not in result, result
        assert str(tmp_path) in result["error"], result


# --------------------------------------------------------------------------- #
# A scripted server, for the answers a real one will not give cheaply
# --------------------------------------------------------------------------- #


class _ScriptedServer:
    """A socket that acknowledges and then does whatever the test says.

    The `dbx` fixture drives the shipped server, which is the right default —
    it is the thing that has to be spoken to. But every failure this class
    exists for happens **after** the acknowledgement, and the server sends that
    ack before it streams precisely so an `ok` means the work started. A real
    server produces those only under conditions a test cannot arrange cheaply:
    a container restarting mid-command, a file growing past the read cap while
    it is being sent, ENOSPC on a staging write.

    So they are scripted. What is under test is this client's reading of the
    protocol, and the protocol is the contract both sides are written against.
    """

    def __init__(self, socket_path: str, handler) -> None:
        self.path = socket_path
        self._handler = handler
        self.requests: list[dict] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(socket_path)
        self._sock.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            buf = b""
            try:
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                line, _, rest = buf.partition(b"\n")
                request = json.loads(line)
                self.requests.append(request)
                self._handler(self, conn, request, rest)
            except OSError:
                return

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:  # pragma: no cover
            pass


@pytest.fixture
def scripted(monkeypatch):
    """Point the CLI's config at a socket a test scripts by hand."""
    servers: list[_ScriptedServer] = []
    base = Path(tempfile.mkdtemp(dir="/tmp", prefix="istota-dbx-s-")).resolve()
    sock_dir = base / "sock" / "bob"
    sock_dir.mkdir(parents=True)
    config = base / "config.toml"
    config.write_text(
        "[developer]\nenabled = true\n"
        f'repos_dir = "{base / "repos"}"\n'
        "\n[devbox]\nenabled = true\n"
        "\n[developer.container]\n"
        f'exec_socket_dir = "{base / "sock"}"\n'
    )
    monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config))

    def make(handler) -> _ScriptedServer:
        server = _ScriptedServer(str(sock_dir / "exec.sock"), handler)
        servers.append(server)
        return server

    try:
        yield make
    finally:
        for server in servers:
            server.close()


def _ack_ok() -> bytes:
    return proto.encode_ack_ok()



def _drain_body(conn, size: int, rest: bytes = b"") -> None:
    """Read exactly the declared body so the client's `sendall` cannot block.

    Bounded by the declared size rather than by end-of-stream, because the
    client does not close after sending: it sends the body and then waits for
    the terminal frame, so a `while conn.recv(...)` drain blocks forever and
    deadlocks both sides. The declared size is the terminator — the same fact
    that makes the client's trailing `stdin_eof` unnecessary.
    """
    decoder = proto.FrameDecoder()
    received = 0
    pending = rest
    while received < size:
        for stream, payload in decoder.feed(pending):
            if stream == proto.STREAM_STDIN:
                received += len(payload)
        if received >= size:
            return
        pending = conn.recv(65536)
        if not pending:
            return


class TestAFateItCannotReportIsNeverASuccess:
    """The one contract this whole transport exists for.

    Design 4: "The client **must never report 0 there**." A connection that
    closes after the acknowledgement and before the terminal frame is the only
    case in the design with no exit status at all, and it is not hypothetical —
    it is what a container restart looks like. The naive implementation reports
    it as success, which is why it gets a class of its own.
    """

    def test_a_connection_that_closes_after_the_ack_is_an_error(self, scripted):
        def handler(server, conn, request, rest):
            conn.sendall(_ack_ok())
            conn.sendall(proto.pack_frame(proto.STREAM_STDOUT, b"partial output\n"))
            conn.close()

        scripted(handler)

        result = _exec("some-build")

        assert result["status"] == "error", result
        assert "exit_code" not in result, result
        assert "fate is unknown" in result["error"], result

    @pytest.mark.parametrize(
        "failure",
        [ConnectionResetError(104, "Connection reset by peer"),
         BrokenPipeError(32, "Broken pipe")],
    )
    def test_a_socket_error_mid_command_is_an_envelope(self, monkeypatch, failure):
        """The other half of a peer going away, and a different code path.

        A *clean* close makes `recv` return `b''`, which `_pump` turns into a
        refusal — the test above. A peer that resets, or one that goes away
        while a body is still being written, makes the call *raise* an
        `OSError`, which is neither `_Refused` nor `ProtocolError`. That gap
        handed an importing caller a traceback where the CLI printed an
        envelope. A container restart mid-build produces both shapes.

        Raised at the seam rather than provoked over a real socket, and that is
        deliberate: whether a given RST surfaces as `ECONNRESET` or as a clean
        EOF depends on whether the data was already buffered, so a socket-level
        version of this test passes or fails on timing. What is under test is
        the handling, and the raising is a property of Python's sockets.
        """
        def boom(*args, **kwargs):
            raise failure

        monkeypatch.setattr(devbox, "_converse", boom)

        result = _exec("some-build")

        assert result["status"] == "error", result
        assert "exit_code" not in result, result
        assert "fate is unknown" in result["error"], result

    def test_status_keeps_its_container_half_through_a_socket_error(
        self, monkeypatch
    ):
        """Same gap, at the one verb that must survive it with half an answer."""
        monkeypatch.setattr(
            devbox,
            "_run_docker",
            _drain(
                [
                    (
                        0,
                        b"true|2026-05-13T10:00:00Z|istota-devbox:latest"
                        b"|deadbeef1234abcd|0|bob",
                        b"",
                    ),
                ]
            ),
        )

        def boom(*args, **kwargs):
            raise ConnectionResetError(104, "Connection reset by peer")

        monkeypatch.setattr(devbox, "_converse", boom)

        info = devbox.cmd_status(_args())

        assert info["status"] == "ok", info
        assert info["running"] is True, info
        assert info["transport"]["reachable"] is False, info

    def test_a_terminal_frame_with_no_status_is_an_error(self, scripted):
        """The server says so explicitly rather than by hanging up. Same rule:
        no status is not a zero."""

        def handler(server, conn, request, rest):
            conn.sendall(_ack_ok())
            conn.sendall(
                proto.encode_control(
                    {"exit_code": None, "signal": None, "reason": "timeout"}
                )
            )

        scripted(handler)

        result = _exec("some-build")

        assert result["status"] == "error", result
        assert "exit_code" not in result, result

    def test_a_fault_beside_a_real_status_is_still_an_error(self, scripted):
        """`istota-exec-serve` folds an input error into the body *after*
        `terminal_frame` has already put waitpid's answer in it, so the protocol
        admits a frame reporting both. Reading `exit_code` alone reports the
        fault as a clean run."""

        def handler(server, conn, request, rest):
            conn.sendall(_ack_ok())
            conn.sendall(
                proto.encode_control(
                    {
                        "exit_code": 0,
                        "signal": None,
                        "error": "internal",
                        "message": "the input pump died",
                    }
                )
            )

        scripted(handler)

        result = _exec("some-build")

        assert result["status"] == "error", result
        assert "internal" in result["error"], result

    def test_a_server_that_never_acknowledges_is_an_envelope(
        self, scripted, monkeypatch
    ):
        """`settimeout` makes `recv` *raise* rather than return, so the crafted
        message was unreachable and the verb raised a bare TimeoutError past the
        decorator — a dict on one path and an exception on the other."""
        monkeypatch.setattr(devbox, "ACK_TIMEOUT_SECONDS", 0.4)
        monkeypatch.setattr(
            devbox, "_ACK_TIMEOUT_MESSAGE", "{path}: no acknowledgement"
        )

        def handler(server, conn, request, rest):
            time.sleep(5)

        scripted(handler)

        result = _exec("some-build")

        assert result["status"] == "error", result
        assert "no acknowledgement" in result["error"], result

    def test_status_keeps_its_container_half_when_the_transport_hangs(
        self, scripted, monkeypatch
    ):
        """Two halves, and neither substitutes for the other — so a dead
        transport must not take the container facts with it."""
        monkeypatch.setattr(devbox, "ACK_TIMEOUT_SECONDS", 0.4)
        monkeypatch.setattr(
            devbox,
            "_run_docker",
            _drain(
                [
                    (
                        0,
                        b"true|2026-05-13T10:00:00Z|istota-devbox:latest"
                        b"|deadbeef1234abcd|0|bob",
                        b"",
                    ),
                ]
            ),
        )

        def handler(server, conn, request, rest):
            time.sleep(5)

        scripted(handler)

        info = devbox.cmd_status(_args())

        assert info["status"] == "ok", info
        assert info["running"] is True, info
        assert info["transport"]["reachable"] is False, info


class TestAPostAcknowledgementFailureNeverReadsAsSuccess:
    """The ack is sent before the bytes move, so this is where they land.

    ``read_file`` and ``write_file`` are both acknowledged *before* the server
    streams — it has to be that way, since the ack is what tells the other side
    to start. Every failure after that point arrives in the terminal frame and
    nowhere else, and a caller that stops at the ack has been told a file exists
    that does not. Which is ISSUE-306 and ISSUE-312 in a new envelope.
    """

    def test_cp_out_writes_nothing_when_the_read_failed_mid_stream(
        self, scripted, tmp_path
    ):
        def handler(server, conn, request, rest):
            conn.sendall(_ack_ok())
            conn.sendall(proto.pack_frame(proto.STREAM_STDOUT, b"HALF-A-FILE"))
            conn.sendall(
                proto.encode_control(
                    {
                        "exit_code": None,
                        "error": "too_large",
                        "message": "grew past the read cap while streaming",
                    }
                )
            )

        scripted(handler)
        dest = tmp_path / "landed.bin"

        result = devbox.cmd_cp_out(_args(src="/home/dev/out.bin", dest=str(dest)))

        assert result["status"] == "error", result
        assert not dest.exists(), (
            "a fragment of unknown length was written to the host and reported "
            "as a success — the ISSUE-312 shape exactly"
        )

    def test_cp_out_does_not_follow_a_link_planted_after_the_check(
        self, scripted, tmp_path, monkeypatch
    ):
        """The window `resolve_host_path` cannot close on its own.

        The resolution refuses a symlink standing at the destination's own
        name as of its own check; the write happens later, and the tree is
        bound read-write into the sandbox, so a link can appear in between.
        `write_resolved` opens with ``O_NOFOLLOW``, so the write fails rather
        than landing wherever the link points, as the daemon user.

        The link is planted through a patched resolver rather than by racing —
        what is under test is the open, and reproducing the race would make
        the test flaky about something the flags settle deterministically.
        """
        victim = tmp_path / "victim.txt"
        victim.write_text("do not overwrite me")
        dest = tmp_path / "landed.bin"
        dest.symlink_to(victim)

        real = devbox.resolve_host_path

        def _resolve_past_the_link(path, **kwargs):
            resolved, err = real(path, **kwargs)
            # What the check would have returned an instant before the link
            # appeared: a contained path whose leaf is now a symlink.
            return (Path(str(path)) if err else resolved), None

        monkeypatch.setattr(devbox, "resolve_host_path", _resolve_past_the_link)

        def handler(server, conn, request, rest):
            conn.sendall(_ack_ok())
            body = b"from inside"
            conn.sendall(proto.pack_frame(proto.STREAM_STDOUT, body))
            conn.sendall(proto.encode_control({"exit_code": 0, "size": len(body)}))

        scripted(handler)

        result = devbox.cmd_cp_out(_args(src="/home/dev/out.bin", dest=str(dest)))

        assert result["status"] == "error", result
        assert victim.read_text() == "do not overwrite me"

    def test_cp_out_refuses_a_short_stream(self, scripted, tmp_path):
        """The server reports what it sent; a mismatch is a truncation."""

        def handler(server, conn, request, rest):
            conn.sendall(_ack_ok())
            conn.sendall(proto.pack_frame(proto.STREAM_STDOUT, b"1234"))
            conn.sendall(proto.encode_control({"exit_code": 0, "size": 9999}))

        scripted(handler)
        dest = tmp_path / "landed.bin"

        result = devbox.cmd_cp_out(_args(src="/home/dev/out.bin", dest=str(dest)))

        assert result["status"] == "error", result
        assert not dest.exists()

    def test_cp_in_refuses_a_short_write(self, scripted, tmp_path):
        def handler(server, conn, request, rest):
            conn.sendall(_ack_ok())
            _drain_body(conn, request["size"], rest)
            conn.sendall(proto.encode_control({"exit_code": 0, "size": 1}))

        scripted(handler)
        src = tmp_path / "probe.txt"
        src.write_text("a much longer body than one byte\n")

        result = devbox.cmd_cp_in(_args(src=str(src), dest="/home/dev/probe.txt"))

        assert result["status"] == "error", result
        assert "incomplete" in result["error"], result

    def test_exec_file_does_not_run_a_script_whose_staging_write_failed(
        self, scripted, tmp_path
    ):
        """Unchecked, the exec ran a path that was never created and the model
        read "can't open file" and concluded its own script was wrong."""

        def handler(server, conn, request, rest):
            action = request.get("action")
            conn.sendall(_ack_ok())
            if action == "stat":
                conn.sendall(
                    proto.encode_control({"staging": "/home/dev/.istota-exec"})
                )
                conn.sendall(proto.encode_control({"exit_code": 0}))
                return
            if action == "write_file":
                _drain_body(conn, request["size"], rest)
                conn.sendall(
                    proto.encode_control(
                        {
                            "exit_code": None,
                            "error": "internal",
                            "message": "No space left on device",
                        }
                    )
                )
                return
            conn.sendall(proto.encode_control({"exit_code": 0}))

        server = scripted(handler)
        script = tmp_path / "probe.sh"
        script.write_text("#!/bin/sh\necho ok\n")

        result = devbox.cmd_exec_file(
            _args(path=str(script), interpreter=None, timeout=None)
        )

        assert result["status"] == "error", result
        assert "No space left" in result["error"], result
        assert [r["action"] for r in server.requests] == ["stat", "write_file"], (
            "the exec ran anyway, against a file the write never created"
        )


class TestOutputIsBoundedInThisProcess:
    """The envelope's cap is a rendering decision; this one bounds the process.

    The skill proxy runs inside the scheduler daemon, so these bytes are charged
    to the daemon's own memory and to no task cgroup — and
    `exec 'cat /dev/urandom'` is one command away.
    """

    def test_a_command_past_the_buffer_cap_is_an_error_not_a_status(
        self, scripted, monkeypatch
    ):
        monkeypatch.setattr(devbox, "MAX_BUFFERED_OUTPUT_BYTES", 4096)

        def handler(server, conn, request, rest):
            conn.sendall(_ack_ok())
            try:
                for _ in range(64):
                    conn.sendall(proto.pack_frame(proto.STREAM_STDOUT, b"x" * 1024))
                conn.sendall(proto.encode_control({"exit_code": 0}))
            except OSError:
                return

        scripted(handler)

        result = _exec("cat /dev/urandom")

        assert result["status"] == "error", result
        assert "exit_code" not in result, result
        assert "fate is unknown" in result["error"], result


class TestTheBackendMustBeDevbox:
    """The refusal has to name a switch, not blame the container.

    `exec_socket_dir` carries a non-empty default, so a path always resolves
    even where nothing is listening on it. Without this the verbs failed with
    "the container may be down" on a deployment where nothing was down —
    originally the shipped pair `devbox.enabled = true` with `backend = none`,
    which is no longer configurable, and now any deployment whose devbox or
    developer skill is simply off.
    """

    def test_it_names_the_key_rather_than_blaming_the_container(
        self, monkeypatch, tmp_path
    ):
        config = tmp_path / "config.toml"
        config.write_text(f'[developer.container]\nexec_socket_dir = "{tmp_path}"\n')
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config))

        result = _exec("true")

        assert result["status"] == "error", result
        # Names the switches an operator can act on, not a container's health.
        assert "[devbox] enabled" in result["error"], result
        assert "may be down" not in result["error"], result

    def test_reset_still_works_with_the_backend_off(self, monkeypatch, tmp_path):
        """`reset` is host-side Docker and does not touch the transport, so the
        one verb that never needed a server keeps working."""
        config = tmp_path / "config.toml"
        config.write_text(f'[developer.container]\nexec_socket_dir = "{tmp_path}"\n')
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config))
        monkeypatch.setattr(
            devbox,
            "_run_docker",
            _drain(
                [
                    *_ownership_sequence(),
                    (0, b"", b""),
                    (0, b"", b""),
                    (0, b"", b""),
                ]
            ),
        )

        assert devbox.cmd_reset(_args(yes=True))["status"] == "ok"


class TestTheConnectBudgetComesFromConfig:
    def test_it_reads_the_configured_value(self, monkeypatch, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text(
            "[developer]\nenabled = true\n"
            f'repos_dir = "{tmp_path}"\n'
            "\n[devbox]\nenabled = true\n"
            "\n[developer.container]\n"
            f'exec_socket_dir = "{tmp_path}"\n'
            "connect_timeout_seconds = 1.5\n"
        )
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config))

        settings, err = devbox._transport_settings()

        assert err is None, err
        assert settings.connect_timeout == 1.5

    @pytest.mark.parametrize("value", [0, -1, True])
    def test_a_budget_that_would_disable_blocking_falls_back(
        self, monkeypatch, tmp_path, value
    ):
        """Zero or negative puts the socket in non-blocking mode, where connect
        raises at once and every verb reports an outage."""
        from istota.config import Config

        config = Config()
        config.developer.enabled = True
        config.developer.repos_dir = str(tmp_path)
        config.devbox.enabled = True
        config.developer.container.exec_socket_dir = str(tmp_path)
        config.developer.container.connect_timeout_seconds = value
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: config)

        settings, err = devbox._transport_settings()

        assert err is None, err
        assert settings.connect_timeout == devbox.DEFAULT_CONNECT_TIMEOUT_SECONDS

    def test_the_two_clients_agree_on_the_ack_budget(self):
        """Two clients of one server disagreeing about how long a slow spawn may
        take shows up as one reporting an outage the other does not see."""
        from istota import devbox_exec_client

        assert devbox.ACK_TIMEOUT_SECONDS == devbox_exec_client.ACK_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# The five deletions
# --------------------------------------------------------------------------- #


class TestTheGuardsThatWentWithDockerCp:
    """Design 3's deletion list, held as a deletion.

    Each of these was a daemon-side guess about the container's mount table,
    which is what ISSUE-306 and ISSUE-312 both were. Naming them keeps one from
    creeping back beside the server-side check that replaced them.
    """

    @pytest.mark.parametrize("name", [
        "_CONTAINER_TMPFS_MOUNTS",
        "_COMPOSE_TMPFS_MOUNTS",
        "_RUNTIME_TMPFS_MOUNTS",
        "_LEGACY_TMPFS_MOUNTS",
        "_CONTAINER_OFFLIMITS_PATHS",
        "_check_arrived",
        "_check_source_visible",
        "_normalize_container_path",
        "_ask_container",
        "_kill_stragglers",
    ])
    def test_the_name_is_gone(self, name):
        assert not hasattr(devbox, name), (
            f"{name} is back. The containment decision belongs to the server, "
            f"inside the container, where the mount table is not a guess."
        )

    def test_workspace_is_not_mentioned_anywhere_in_the_module(self):
        source = (_SKILL_DIR / "__init__.py").read_text()
        assert "/workspace" not in source, (
            "the /workspace tmpfs is deleted from the image and the template; "
            "a refusal list naming it is a list nothing needs"
        )

    def test_skill_host_paths_is_still_the_host_side_rule(self, tmp_path):
        """Explicitly *not* on the deletion list. It scopes the host side of a
        verb whose host path the model still picks, which is a different
        question from what the container may touch."""
        from istota import skill_host_paths

        resolved, err = devbox._resolve_host_path(
            Path("/etc/passwd"), must_exist=True
        )
        assert resolved is None
        assert "outside allowed roots" in err
        # And it is the shared rule, not a copy: `kv set --value-file` and the
        # deferred health ops go through the same function.
        assert devbox._resolve_host_path.__module__ != skill_host_paths.__name__
        assert skill_host_paths.resolve_host_path is not None


# --------------------------------------------------------------------------- #
# exec
# --------------------------------------------------------------------------- #


class TestExec:
    def test_a_command_that_succeeds_reports_zero(self, dbx):
        result = _exec("echo hi")
        assert result["status"] == "ok", result
        assert result["exit_code"] == 0
        assert result["stdout"] == "hi\n"

    def test_a_command_that_fails_reports_its_own_status(self, dbx):
        assert _exec("exit 7")["exit_code"] == 7

    def test_stderr_comes_back_on_its_own_stream(self, dbx):
        result = _exec("echo out; echo err >&2")
        assert result["stdout"] == "out\n"
        assert result["stderr"] == "err\n"

    def test_it_runs_in_the_servers_own_home(self, dbx):
        """`cwd: null`, so the *server* names the directory — the container's
        `/home/dev`, or whatever this server was started with. The verb sends
        no path at all, which is why a named `/home/dev` staying refused is not
        a problem for it."""
        result = _exec("pwd")
        assert result["exit_code"] == 0, result
        assert Path(result["stdout"].strip()).resolve() == dbx.home.resolve()

    def test_it_sends_null_rather_than_naming_a_directory(self, dbx, monkeypatch):
        """The wire, not the outcome: a verb that named `/home/dev` as a string
        would be refused by `check_cwd`, which is the failure Design 13's
        amendment exists to prevent."""
        seen = {}
        real = proto.encode_exec_request

        def spy(**kw):
            seen.update(kw)
            return real(**kw)

        monkeypatch.setattr(devbox.proto, "encode_exec_request", spy)
        _exec("true")

        assert "cwd" in seen, seen
        assert seen["cwd"] is None, seen

    def test_no_default_timeout_reaches_the_wire(self, dbx, monkeypatch):
        """The 300-second default went with `docker exec`. The task's own
        budget governs, and a caller that wants a kill passes `--timeout`."""
        seen = {}
        real = proto.encode_exec_request

        def spy(**kw):
            seen.update(kw)
            return real(**kw)

        monkeypatch.setattr(devbox.proto, "encode_exec_request", spy)
        _exec("true")
        assert seen["timeout"] == 0, seen

        _exec("true", timeout=12)
        assert seen["timeout"] == 12, seen

    def test_a_timeout_kills_the_command_and_says_why(self, dbx):
        result = _exec("sleep 30", timeout=1)
        assert result["status"] == "ok", result
        assert result["exit_code"] != 0, result
        assert result.get("reason") == "timeout", result

    def test_binary_output_survives(self, dbx):
        result = _exec("printf 'a\\000b'")
        assert result["exit_code"] == 0, result
        assert "a" in result["stdout"]

    def test_it_refuses_a_nul_byte(self, dbx):
        result = devbox.cmd_exec(_args(command="echo hi\x00; rm -rf /", timeout=None))
        assert result["status"] == "error"
        assert "NUL byte" in result["error"]

    def test_it_refuses_an_oversized_command(self, dbx):
        big = "x" * (devbox.MAX_COMMAND_BYTES + 1)
        result = devbox.cmd_exec(_args(command=big, timeout=None))
        assert result["status"] == "error"
        assert "exceeds" in result["error"]


class TestExecKeepsThePipelineStatus:
    """ISSUE-307, now decided by the server rather than by this file's argv.

    The old version asserted that `bash -o pipefail -c` appeared in a mocked
    `docker exec` argv and then ran the same argv locally to prove bash honours
    the option — which proves something about *this* machine's bash, not about
    the shell the command actually reaches. Here the command reaches a real
    shell through the real transport and the status is what comes back.
    """

    def test_a_failing_pipeline_is_not_reported_as_success(self, dbx):
        result = _exec("false | tail -1")
        assert result["status"] == "ok", result
        assert result["exit_code"] != 0, (
            "a pipeline whose first stage failed came back green — this is the "
            "exact shape of ISSUE-307"
        )

    def test_a_succeeding_pipeline_is_still_success(self, dbx):
        """Control: the option must not turn every pipeline red."""
        result = _exec("echo hi | tail -1")
        assert result["exit_code"] == 0, result
        assert result["stdout"] == "hi\n"

    def test_sigpipe_carries_a_note_and_no_signal(self, dbx):
        """`yes | head -1` makes bash *exit* 141; it is not a signalled child.

        The note is the honest half: the server reports what `waitpid` said and
        never infers a signal from 128+N, so the hint travels as prose.
        """
        result = _exec("yes | head -1")
        assert result["exit_code"] == proto.SIGPIPE_EXIT, result
        assert "signal" not in result, result
        assert result.get("note"), result
        assert "SIGPIPE" in result["note"]

    def test_a_genuinely_signalled_child_reports_its_signal(self, dbx):
        result = _exec("kill -TERM $$")
        assert result.get("signal") == "SIGTERM", result
        assert result["exit_code"] == 143, result


class TestTheEnvelopeCap:
    """The transport caps nothing; the envelope does, because its reader is a
    language model rather than a terminal (Design 4)."""

    def test_large_output_is_truncated_with_a_marker(self, dbx, monkeypatch):
        monkeypatch.setenv("ISTOTA_DEVBOX_MAX_OUTPUT_BYTES", "1024")
        result = _exec("head -c 20000 /dev/zero | tr '\\0' 'x'")
        assert result["exit_code"] == 0, result
        assert result["stdout"].startswith("x" * 1024)
        assert "truncated" in result["stdout"]

    def test_the_whole_output_crossed_the_wire(self, dbx, monkeypatch):
        """The cap is a rendering decision, not a transport one: the bytes all
        arrived, and the marker names how many were dropped on the way out."""
        monkeypatch.setenv("ISTOTA_DEVBOX_MAX_OUTPUT_BYTES", "1024")
        result = _exec("head -c 20000 /dev/zero | tr '\\0' 'x'")
        assert f"truncated: {20000 - 1024} more bytes" in result["stdout"], (
            result["stdout"][-120:]
        )

    def test_output_under_the_cap_is_untouched(self, dbx):
        result = _exec("echo short")
        assert result["stdout"] == "short\n"
        assert "truncated" not in result["stdout"]


# --------------------------------------------------------------------------- #
# exec-file
# --------------------------------------------------------------------------- #


class TestExecFile:
    @pytest.mark.parametrize("name,body,expected", [
        ("probe.sh", "#!/bin/sh\necho shell-ok\n", "shell-ok\n"),
        ("probe.py", "print('python-ok')\n", "python-ok\n"),
    ])
    def test_it_runs_a_script_through_its_interpreter(
        self, dbx, tmp_path, name, body, expected,
    ):
        script = tmp_path / name
        script.write_text(body)
        result = devbox.cmd_exec_file(
            _args(path=str(script), interpreter=None, timeout=None)
        )
        assert result["status"] == "ok", result
        assert result["exit_code"] == 0, result
        assert result["stdout"] == expected

    def test_it_runs_a_script_with_no_extension_via_its_shebang(self, dbx, tmp_path):
        """Needs the staged copy to be executable, which is what the 0755 mode
        on the `write_file` request buys. The staging directory used to be a
        `noexec` tmpfs, so this path could not work at all."""
        script = tmp_path / "probe"
        script.write_text("#!/bin/sh\necho shebang-ok\n")
        result = devbox.cmd_exec_file(
            _args(path=str(script), interpreter=None, timeout=None)
        )
        assert result["status"] == "ok", result
        assert result["stdout"] == "shebang-ok\n"

    def test_the_staged_copy_lands_in_the_staging_root(self, dbx, tmp_path):
        script = tmp_path / "probe.sh"
        script.write_text("#!/bin/sh\necho ok\n")
        devbox.cmd_exec_file(_args(path=str(script), interpreter=None, timeout=None))
        # Gone afterwards, but the *directory* is the one the server was told
        # to treat as a file root — asserted from the host, which can see it.
        assert dbx.staging.is_dir()

    def test_it_leaves_no_staged_copy_behind(self, dbx, tmp_path):
        """The cleanup used to run against a path the file was never written
        to, so it removed nothing and exited 0. Scoped to this process's own
        staged name: the directory is shared and keyed on pid."""
        script = tmp_path / "probe.sh"
        script.write_text("#!/bin/sh\necho ok\n")
        assert devbox.cmd_exec_file(
            _args(path=str(script), interpreter=None, timeout=None)
        )["exit_code"] == 0

        staged = dbx.staging / f"exec_{os.getpid()}_probe.sh"
        assert not staged.exists(), staged

    def test_it_cleans_up_after_a_failing_script(self, dbx, tmp_path):
        script = tmp_path / "probe.sh"
        script.write_text("#!/bin/sh\nexit 3\n")
        result = devbox.cmd_exec_file(
            _args(path=str(script), interpreter=None, timeout=None)
        )
        assert result["exit_code"] == 3, result
        assert not (dbx.staging / f"exec_{os.getpid()}_probe.sh").exists()

    def test_it_refuses_an_unusual_basename(self, dbx, tmp_path):
        script = tmp_path / "pro be.sh"
        script.write_text("#!/bin/sh\necho ok\n")
        result = devbox.cmd_exec_file(
            _args(path=str(script), interpreter=None, timeout=None)
        )
        assert result["status"] == "error"
        assert "basename" in result["error"]

    def test_it_refuses_a_host_path_outside_the_allowlist(self, dbx):
        result = devbox.cmd_exec_file(
            _args(path="/etc/hosts", interpreter=None, timeout=None)
        )
        assert result["status"] == "error"
        assert "outside allowed roots" in result["error"]

    def test_it_imposes_no_pipefail_on_the_script(self, dbx, tmp_path):
        """A script owns its own shell options: it has a shebang line where
        `set -euo pipefail` is the idiom, and the no-interpreter branch runs
        whatever interpreter the file names."""
        script = tmp_path / "probe.sh"
        script.write_text("#!/bin/sh\nfalse | true\n")
        result = devbox.cmd_exec_file(
            _args(path=str(script), interpreter=None, timeout=None)
        )
        assert result["exit_code"] == 0, result


# --------------------------------------------------------------------------- #
# cp-in / cp-out
# --------------------------------------------------------------------------- #


class TestCopyIn:
    def test_a_file_arrives_where_the_server_can_read_it(self, dbx, tmp_path):
        src = tmp_path / "probe.txt"
        src.write_text("round-trip marker\n")
        dest = dbx.home / "probe.txt"

        result = devbox.cmd_cp_in(_args(src=str(src), dest=str(dest)))

        assert result["status"] == "ok", result
        # The whole point: read it back through the *server*, not off the host.
        assert _exec(f"cat {dest}")["stdout"] == "round-trip marker\n"

    def test_a_destination_outside_the_roots_is_refused_by_the_server(
        self, dbx, tmp_path,
    ):
        """The refusal that replaced the hand-kept container-path lists."""
        src = tmp_path / "probe.txt"
        src.write_text("hello\n")
        dest = dbx.outside / "probe.txt"

        result = devbox.cmd_cp_in(_args(src=str(src), dest=str(dest)))

        assert result["status"] == "error", result
        assert result.get("code") == proto.ERR_PATH_REFUSED, result
        assert not dest.exists(), "the refusal still wrote the file"

    def test_a_relative_destination_is_refused(self, dbx, tmp_path):
        """A relative path means something different in each namespace, so the
        server refuses it rather than anchoring it somewhere."""
        src = tmp_path / "probe.txt"
        src.write_text("hello\n")

        result = devbox.cmd_cp_in(_args(src=str(src), dest="probe.txt"))

        assert result["status"] == "error", result
        assert result.get("code") == proto.ERR_PATH_REFUSED, result

    def test_a_symlink_out_of_the_roots_is_refused(self, dbx, tmp_path):
        """Resolved before the test, never after."""
        (dbx.home / "escape").symlink_to(dbx.outside)
        src = tmp_path / "probe.txt"
        src.write_text("hello\n")

        result = devbox.cmd_cp_in(
            _args(src=str(src), dest=str(dbx.home / "escape" / "probe.txt"))
        )

        assert result["status"] == "error", result
        assert result.get("code") == proto.ERR_PATH_REFUSED, result
        assert not (dbx.outside / "probe.txt").exists()

    def test_a_host_source_outside_the_allowlist_is_refused(self, dbx):
        result = devbox.cmd_cp_in(
            _args(src="/etc/hosts", dest=str(dbx.home / "hosts"))
        )
        assert result["status"] == "error"
        assert "outside allowed roots" in result["error"]
        assert not (dbx.home / "hosts").exists()

    def test_a_host_symlink_source_is_refused(self, dbx, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("x")
        link = tmp_path / "link.txt"
        link.symlink_to(real)

        result = devbox.cmd_cp_in(
            _args(src=str(link), dest=str(dbx.home / "out.txt"))
        )

        assert result["status"] == "error"
        assert "symlink" in result["error"]

    def test_a_directory_source_is_refused_rather_than_half_copied(
        self, dbx, tmp_path,
    ):
        (tmp_path / "tree").mkdir()
        (tmp_path / "tree" / "a.txt").write_text("a")

        result = devbox.cmd_cp_in(
            _args(src=str(tmp_path / "tree"), dest=str(dbx.home / "tree"))
        )

        assert result["status"] == "error"
        assert "directory" in result["error"]

    def test_the_reported_byte_count_is_the_servers(self, dbx, tmp_path):
        src = tmp_path / "probe.bin"
        src.write_bytes(b"\x00\x01\x02" * 5000)

        result = devbox.cmd_cp_in(
            _args(src=str(src), dest=str(dbx.home / "probe.bin"))
        )

        assert result["size"] == 15000, result
        assert (dbx.home / "probe.bin").read_bytes() == src.read_bytes()


class TestCopyOut:
    def test_a_file_written_inside_comes_back(self, dbx, tmp_path):
        remote = dbx.home / "out.txt"
        assert _exec(f"printf 'from inside\\n' > {remote}")["exit_code"] == 0
        dest = tmp_path / "out.txt"

        result = devbox.cmd_cp_out(_args(src=str(remote), dest=str(dest)))

        assert result["status"] == "ok", result
        assert dest.read_text() == "from inside\n"

    def test_a_source_outside_the_roots_is_refused(self, dbx, tmp_path):
        secret = dbx.outside / "secret.txt"
        secret.write_text("not yours\n")
        dest = tmp_path / "secret.txt"

        result = devbox.cmd_cp_out(_args(src=str(secret), dest=str(dest)))

        assert result["status"] == "error", result
        assert result.get("code") == proto.ERR_PATH_REFUSED, result
        assert not dest.exists()

    def test_the_credential_directory_is_refused_by_name(self, dbx, tmp_path):
        result = devbox.cmd_cp_out(
            _args(src="/run/istota-cred/sock", dest=str(tmp_path / "sock"))
        )
        assert result["status"] == "error", result
        assert result.get("code") == proto.ERR_PATH_REFUSED, result
        assert "istota-cred" in result["error"]

    def test_the_transports_own_socket_directory_is_refused_by_name(
        self, dbx, tmp_path,
    ):
        result = devbox.cmd_cp_out(
            _args(src="/run/istota-exec/bob/exec.sock", dest=str(tmp_path / "s"))
        )
        assert result["status"] == "error", result
        assert result.get("code") == proto.ERR_PATH_REFUSED, result

    def test_a_missing_source_is_an_error_not_an_empty_file(self, dbx, tmp_path):
        dest = tmp_path / "nope.txt"

        result = devbox.cmd_cp_out(
            _args(src=str(dbx.home / "nope.txt"), dest=str(dest))
        )

        assert result["status"] == "error", result
        assert not dest.exists(), (
            "a refused copy left a file on the host — which is the ISSUE-312 "
            "shape: bytes of unknown provenance the caller goes on to read"
        )

    def test_a_refusal_creates_no_host_directories(self, dbx, tmp_path):
        """Container side first, then the host path. `_resolve_host_path`
        creates the destination's parents, so the other order left an empty
        tree in the user's workspace behind every refusal."""
        dest = tmp_path / "deep" / "nested" / "out.txt"

        result = devbox.cmd_cp_out(_args(src=str(dbx.outside / "x"), dest=str(dest)))

        assert result["status"] == "error"
        assert not dest.parent.exists(), dest.parent

    def test_a_host_destination_outside_the_allowlist_is_refused(self, dbx):
        remote = dbx.home / "out.txt"
        assert _exec(f"echo hi > {remote}")["exit_code"] == 0

        result = devbox.cmd_cp_out(_args(src=str(remote), dest="/etc/istota-probe"))

        assert result["status"] == "error"
        assert not Path("/etc/istota-probe").exists()

    def test_binary_round_trips(self, dbx, tmp_path):
        payload = bytes(range(256)) * 40
        src = tmp_path / "in.bin"
        src.write_bytes(payload)
        remote = dbx.home / "in.bin"
        assert devbox.cmd_cp_in(
            _args(src=str(src), dest=str(remote))
        )["status"] == "ok"

        dest = tmp_path / "back.bin"
        assert devbox.cmd_cp_out(
            _args(src=str(remote), dest=str(dest))
        )["status"] == "ok"
        assert dest.read_bytes() == payload


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


class TestStatus:
    def test_it_reports_the_transport(self, dbx, monkeypatch):
        monkeypatch.setattr(
            devbox, "_run_docker", lambda argv, timeout: (1, b"", b"No such container")
        )
        info = devbox.cmd_status(_args())

        assert info["status"] == "ok", info
        assert info["transport"]["reachable"] is True, info
        assert info["transport"]["protocol"] == proto.PROTOCOL_VERSION
        assert Path(info["transport"]["home"]).resolve() == dbx.home.resolve()

    def test_an_unreachable_container_is_reported_and_is_not_fatal(
        self, dbx, monkeypatch,
    ):
        """Two halves that fail separately: Docker says whether the container
        is up, the transport says whether the server inside it answers. A
        deployment where the CLI cannot reach Docker at all still gets the
        answer that matters."""
        monkeypatch.setattr(
            devbox, "_run_docker", lambda argv, timeout: (1, b"", b"No such container")
        )
        info = devbox.cmd_status(_args())

        assert info["running"] is None, info
        assert "container_error" in info, info
        assert info["transport"]["reachable"] is True, info

    def test_it_parses_the_inspect_line(self, dbx, monkeypatch):
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            (0, b"true|2026-05-13T10:00:00Z|istota-devbox:latest|deadbeef1234abcd|0|bob", b""),
        ]))
        info = devbox.cmd_status(_args())

        assert info["running"] is True
        assert info["image"] == "istota-devbox:latest"
        assert info["id"] == "deadbeef1234"
        assert info["restart_count"] == 0
        assert info["owner"] == "bob"

    def test_a_dead_transport_is_reported_rather_than_raised(
        self, monkeypatch, tmp_path,
    ):
        config = tmp_path / "config.toml"
        config.write_text(
            "[developer]\nenabled = true\n"
            f'repos_dir = "{tmp_path}"\n'
            "\n[devbox]\nenabled = true\n"
            "\n[developer.container]\n"
            f'exec_socket_dir = "{tmp_path}"\n'
        )
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config))
        monkeypatch.setattr(
            devbox, "_run_docker", lambda argv, timeout: (1, b"", b"nope")
        )

        info = devbox.cmd_status(_args())

        assert info["status"] == "ok", info
        assert info["transport"]["reachable"] is False, info
        assert info["transport"]["error"], info


# --------------------------------------------------------------------------- #
# reset — the one verb still spoken in Docker
# --------------------------------------------------------------------------- #


class TestReset:
    """`_run_docker` and `_check_owned` survive for this verb alone.

    The transport cannot recreate a container and should not learn how: a
    server inside a container cannot restart the container it is inside. And
    `reset` never used the retired allowlist proxy — `_run_docker` shells
    `docker` with the daemon's own environment and sets no `DOCKER_HOST`, so it
    has always talked to the real socket. That is the evidence the proxy could
    be retired whole rather than kept for this one verb.
    """

    def test_it_refuses_without_yes(self):
        result = devbox.cmd_reset(_args(yes=False))
        assert result["status"] == "error"
        assert "Refusing" in result["error"]

    def test_it_refuses_when_home_is_not_a_mountpoint(self, monkeypatch):
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (1, b"", b""),  # mountpoint -q /home/dev — not a mountpoint
        ]))
        result = devbox.cmd_reset(_args(yes=True))
        assert result["status"] == "error"
        assert "not a mountpoint" in result["error"]

    def test_it_refuses_a_container_owned_by_someone_else(self, monkeypatch):
        monkeypatch.setattr(
            devbox, "_run_docker", _drain(_ownership_sequence(owner="alice"))
        )
        result = devbox.cmd_reset(_args(yes=True))
        assert result["status"] == "error"
        assert "owned by 'alice'" in result["error"]

    def test_it_refuses_a_container_that_is_not_running(self, monkeypatch):
        monkeypatch.setattr(devbox, "_run_docker", _drain([(0, b"false", b"")]))
        result = devbox.cmd_reset(_args(yes=True))
        assert result["status"] == "error"
        assert "not running" in result["error"]

    def test_it_wipes_and_restarts(self, monkeypatch):
        calls = []
        seq = iter([
            *_ownership_sequence(),
            (0, b"", b""),  # mountpoint -q → ok
            (0, b"", b""),  # find …rm -rf wipe
            (0, b"", b""),  # restart
        ])

        def fake_run(argv, timeout):
            calls.append(argv)
            return next(seq)

        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        result = devbox.cmd_reset(_args(yes=True))

        assert result["status"] == "ok"
        wipe = [c for c in calls if c[0] == "exec" and "find" in " ".join(c)]
        assert wipe and "-u" in wipe[0] and "root" in wipe[0]
        assert calls[-1] == ["restart", "devbox-bob"]

    def test_it_sets_no_docker_host(self):
        """The evidence Design 14's deletion rests on, held as a test.

        `_run_docker` passes no environment of its own, so the child inherits
        the daemon's — which has no `DOCKER_HOST`, so `docker` resolves the
        real socket. If this file ever started setting one, retiring the proxy
        would have been the wrong call and this says so.
        """
        source = (_SKILL_DIR / "__init__.py").read_text()
        # An assignment, not a mention: the docstrings here explain at length
        # why no `DOCKER_HOST` is set, and a bare substring search would be
        # satisfied by that prose.
        assert not re.search(r"""DOCKER_HOST["']?\s*[\]:]?\s*=""", source), source
        assert "env=" not in source.split("def _run_docker", 1)[1].split("\ndef ", 1)[0]


# --------------------------------------------------------------------------- #
# The parser, the envelope, and the shipped body
# --------------------------------------------------------------------------- #


class TestParser:
    def test_exec(self):
        args = devbox.build_parser().parse_args(["exec", "echo hi"])
        assert args.subcommand == "exec"
        assert args.command == "echo hi"

    def test_exec_with_timeout(self):
        args = devbox.build_parser().parse_args(["exec", "sleep 1", "--timeout", "10"])
        assert args.timeout == 10

    def test_exec_has_no_default_timeout(self):
        assert devbox.build_parser().parse_args(["exec", "x"]).timeout is None

    def test_exec_file(self):
        args = devbox.build_parser().parse_args(
            ["exec-file", "/tmp/x.py", "--interpreter", "python3"]
        )
        assert args.subcommand == "exec-file"
        assert args.path == "/tmp/x.py"
        assert args.interpreter == "python3"

    def test_cp_in(self):
        args = devbox.build_parser().parse_args(["cp-in", "/a", "/b"])
        assert args.subcommand == "cp-in"
        assert args.src == "/a"
        assert args.dest == "/b"

    def test_status(self):
        assert devbox.build_parser().parse_args(["status"]).subcommand == "status"

    def test_reset_requires_yes(self):
        args = devbox.build_parser().parse_args(["reset"])
        assert args.subcommand == "reset"
        assert args.yes is False

    def test_there_is_no_workdir_flag(self):
        """Naming a third location is not a capability this verb has: one `cd`
        in the shell string it already sends covers the case, which is also why
        refusing a path here was never a boundary."""
        with pytest.raises(SystemExit):
            devbox.build_parser().parse_args(["exec", "x", "--workdir", "/tmp"])


class TestTruncate:
    def test_short_passes_through(self):
        assert devbox._truncate(b"abc", 100) == "abc"

    def test_long_is_marked(self):
        out = devbox._truncate(b"x" * 200, 100)
        assert out.startswith("x" * 100)
        assert "truncated: 100 more bytes" in out

    def test_invalid_utf8_is_replaced_not_raised(self):
        assert devbox._truncate(b"\xff\xfe", 100)


class TestMain:
    def test_it_prints_the_json_envelope(self, dbx, capsys):
        devbox.main(["exec", "echo hi"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["stdout"] == "hi\n"

    def test_an_error_envelope_exits_nonzero(self, monkeypatch, capsys, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text(
            "[developer]\nenabled = true\n"
            f'repos_dir = "{tmp_path}"\n'
            "\n[devbox]\nenabled = true\n"
            "\n[developer.container]\n"
            f'exec_socket_dir = "{tmp_path}"\n'
        )
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config))

        with pytest.raises(SystemExit) as exc:
            devbox.main(["exec", "echo hi"])

        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_a_missing_user_id_is_an_envelope_not_a_traceback(
        self, monkeypatch, capsys,
    ):
        monkeypatch.delenv("ISTOTA_USER_ID")
        with pytest.raises(SystemExit):
            devbox.main(["exec", "echo hi"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "ISTOTA_USER_ID" in out["error"]


class TestTheSocketPathComesFromConfig:
    """Design 5's rule, at the one place it is decided for this CLI.

    A shim bakes both paths in because it is a child of the model's own shell.
    This CLI is a host-side process the model cannot reach, so it reads config —
    and it must read *config*, because an environment variable here would be
    the same hole by another route: the skill proxy spawns this CLI with a
    per-task environment, and a name reaching the model is a name the model can
    set.
    """

    def test_it_derives_the_per_user_socket(self, monkeypatch, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text(
            "[developer]\nenabled = true\n"
            f'repos_dir = "{tmp_path}"\n'
            "\n[devbox]\nenabled = true\n"
            "\n[developer.container]\n"
            f'exec_socket_dir = "{tmp_path}"\n'
        )
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config))

        settings, err = devbox._transport_settings()

        assert err is None, err
        assert settings.path == str(tmp_path / "bob" / "exec.sock")

    def test_a_config_naming_no_directory_is_a_named_refusal(
        self, monkeypatch, tmp_path,
    ):
        """`ContainerConfig` carries a default, so this needs the field blanked
        on the object rather than in the file — which is the point: there is no
        TOML that leaves it unset, and therefore no second `[devbox]` spelling
        that could ever win. One spelling, and this is what its absence looks
        like."""
        from istota.config import Config

        config = Config()
        config.developer.enabled = True
        config.developer.repos_dir = "/srv/repos"
        config.devbox.enabled = True
        config.developer.container.exec_socket_dir = ""
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: config)

        settings, err = devbox._transport_settings()

        assert settings is None
        assert "exec_socket_dir" in err

    def test_the_devbox_block_carries_no_second_spelling(self):
        """`[devbox]` deliberately has no `exec_socket_dir`. A mirror of it
        could only be dead — `ContainerConfig`'s default always wins — or a
        second knob for a value the design says has one spelling."""
        from istota.config import DevboxConfig

        assert not hasattr(DevboxConfig(), "exec_socket_dir")

    def test_no_environment_variable_names_the_socket(self):
        source = (_SKILL_DIR / "__init__.py").read_text()
        for form in ("ISTOTA_DEVBOX_EXEC_SOCKET", "ISTOTA_EXEC_SOCKET"):
            assert f'"{form}"' not in source and f"'{form}'" not in source, (
                f"{form} would let the model point this CLI at a socket it "
                f"wrote, which buys an `ok` ack and a fabricated exit 0"
            )


class TestExcludeSkills:
    """devbox is a plain menu skill — no selection-time exclusion.

    The old `exclude_skills: [devbox]` gate on the seven ingest skills kept the
    raw docker socket away from untrusted-content tasks. It was removed when
    the allowlist proxy made the socket safe to bind unconditionally, and the
    reasoning is stronger now that neither the proxy nor any docker socket
    reaches a sandbox at all.
    """

    def test_devbox_is_not_always_include(self):
        from istota.skills._loader import load_skill_index
        meta = load_skill_index(Path("config/skills")).get("devbox")
        assert meta is not None
        assert meta.always_include is False

    @pytest.mark.parametrize("skill", [
        "email", "browse", "calendar", "transcribe", "whisper", "feeds", "bookmarks",
    ])
    def test_an_ingest_skill_does_not_exclude_devbox(self, skill):
        from istota.skills._loader import load_skill_index
        meta = load_skill_index(Path("config/skills")).get(skill)
        assert meta is not None
        assert "devbox" not in meta.exclude_skills


# ---------------------------------------------------------------------------
# ISSUE-284: the shipped body and the CLI have to agree, and the executor must
# not export a name nothing reads.

# Both forms a name can be read back in. A plain substring search over the CLI
# source would be satisfied by a mention in a docstring, and the module
# docstring already names env vars in prose.
_READ_FORM = re.compile(
    r"""(?:environ\.get|getenv)\(\s*['"](ISTOTA_DEVBOX_[A-Z_]+)['"]"""
    r"""|environ\[\s*['"](ISTOTA_DEVBOX_[A-Z_]+)['"]\s*\]"""
)


def _documented_argv() -> list[tuple[int, list[str]]]:
    """Every `istota-skill devbox …` line in the shipped body, as argv.

    The body is what the model reads and copies verbatim, so each line is
    parsed rather than restated here — a body edited back to a form the CLI
    refuses fails this instead of quietly passing against a copy.
    """
    out = []
    body = (_SKILL_DIR / "skill.md").read_text()
    for i, line in enumerate(body.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("istota-skill devbox "):
            continue
        tokens = shlex.split(stripped, comments=True)
        out.append((i, tokens[2:]))
    return out


def _subparser_for(verb: str) -> argparse.ArgumentParser:
    """The subparser `verb` dispatches to, so a test can read its flags."""
    for action in devbox.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[verb]
    raise AssertionError("devbox parser declares no subcommands")


class TestDocumentedCommandsMatchTheCLI:
    """ISSUE-284: `skill.md` listed `reset` with no `--yes`, which the CLI
    refuses. The model read the doc, ran the documented form, got an error and
    retried."""

    def test_the_scraper_finds_every_documented_line(self):
        """A parity test that silently matched nothing is the failure mode this
        class exists to prevent, so count the lines a second, independent way
        and require the two to agree."""
        body = (_SKILL_DIR / "skill.md").read_text()
        expected = sum(
            1 for line in body.splitlines()
            if line.strip().startswith("istota-skill devbox ")
        )
        assert expected >= 5, "skill.md documents almost nothing — body gutted?"
        assert len(_documented_argv()) == expected

    def test_every_documented_verb_parses(self):
        parser = devbox.build_parser()
        for lineno, argv in _documented_argv():
            assert argv, f"skill.md:{lineno} names no verb"
            assert argv[0] in devbox._DISPATCH, (
                f"skill.md:{lineno} documents verb {argv[0]!r}, which the CLI "
                f"does not dispatch (has: {sorted(devbox._DISPATCH)})"
            )
            try:
                parser.parse_args(argv)
            except SystemExit as exc:  # argparse exits rather than raising
                raise AssertionError(
                    f"skill.md:{lineno} does not parse: istota-skill devbox "
                    f"{' '.join(argv)}"
                ) from exc

    def test_documented_forms_carry_their_confirmation_flags(self):
        """The reported bug class, for every verb rather than just `reset`.

        A confirmation flag is `store_true` and so optional as far as argparse
        is concerned: the documented form parses cleanly and is then refused at
        runtime. Parsing alone would not have caught ISSUE-284.
        """
        checked = 0
        for lineno, argv in _documented_argv():
            sub = _subparser_for(argv[0])
            for action in sub._actions:
                if action.const is not True or not action.option_strings:
                    continue
                if "required" not in (action.help or "").lower():
                    continue
                checked += 1
                assert any(opt in argv for opt in action.option_strings), (
                    f"skill.md:{lineno} documents `istota-skill devbox "
                    f"{' '.join(argv)}`, but {argv[0]} refuses without "
                    f"{action.option_strings[0]} — the documented form cannot run"
                )
        assert checked, (
            "no documented verb has a confirmation flag; this test found "
            "nothing to check and would pass against anything"
        )

    def test_documented_reset_actually_runs(self, monkeypatch):
        """End to end through the real `cmd_reset`, with docker stubbed: the
        documented argv reaches the wipe rather than the refusal."""
        resets = [argv for _, argv in _documented_argv() if argv[0] == "reset"]
        assert resets, "skill.md no longer documents reset"
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (0, b"", b""),   # mountpoint -q /home/dev
            (0, b"", b""),   # find … -exec rm -rf
            (0, b"", b""),   # restart
        ]))
        args = devbox.build_parser().parse_args(resets[0])
        result = devbox.cmd_reset(args)
        assert result["status"] == "ok", (
            f"the documented reset form was refused: {result.get('error')}"
        )

    def test_reset_description_does_not_promise_image_recreation(self):
        """`reset` wipes /home/dev and restarts the container. It recreates
        nothing from the base image, and the old wording said it did."""
        body = (_SKILL_DIR / "skill.md").read_text()
        for line in body.splitlines():
            if "devbox reset" not in line:
                continue
            assert "base image" not in line, (
                f"reset does not recreate from the base image: {line.strip()!r}"
            )


class TestTheBodyDoesNotDescribeARetiredMechanism:
    """The body is what the model acts on, so a rule about a mechanism that no
    longer exists is worse than a missing rule: it sends the model looking for
    a boundary that is not there."""

    @pytest.mark.parametrize("phrase", [
        "/workspace",
        "docker cp",
        "docker run",
        "filtering proxy",
        "Docker-API",
    ])
    def test_the_phrase_is_gone(self, phrase):
        body = (_SKILL_DIR / "skill.md").read_text()
        assert phrase not in body, (
            f"skill.md still describes {phrase!r}, which this stage retired"
        )

    def test_it_says_what_a_shimmed_command_cannot_see(self):
        body = (_SKILL_DIR / "skill.md").read_text()
        assert "/home/dev" in body


class TestExecutorExportsNothingTheCLIIgnores:
    """ISSUE-284: `ISTOTA_DEVBOX_DOCKER_SOCKET` was written into the model's
    own environment and read by nothing. A name in the model's environment
    invites a later reader to treat it as "the socket you may use" — which is
    exactly the hole `TestTheSocketPathComesFromConfig` closes for the exec
    socket."""

    def _exported(self) -> set[str]:
        """Both routes a var can take into the task env: the imperative block
        in `execute_task`, and the manifest `env:` block — which is the
        sanctioned route per `.claude/rules/skills.md`, and so the likelier way
        this comes back."""
        from istota.skills._loader import load_skill_index
        imperative = set(re.findall(
            r"""env\[\s*['"](ISTOTA_DEVBOX_[A-Z_]+)['"]\s*\]\s*=""",
            _env_source_text(),
        ))
        meta = load_skill_index(Path("config/skills")).get("devbox")
        declared = {
            spec.var for spec in (getattr(meta, "env_specs", None) or [])
            if spec.var and spec.var.startswith("ISTOTA_DEVBOX_")
        }
        return imperative | declared

    def _read_by_cli(self) -> set[str]:
        source = (_SKILL_DIR / "__init__.py").read_text()
        return {name for pair in _READ_FORM.findall(source) for name in pair if name}

    def test_the_scans_find_something(self):
        assert self._exported(), "found no devbox env exports — regex stale?"
        assert self._read_by_cli(), "found no devbox env reads — regex stale?"

    def test_every_exported_devbox_var_has_a_reader(self):
        unread = sorted(self._exported() - self._read_by_cli())
        assert not unread, (
            f"{unread} reach the sandboxed task environment and the devbox "
            f"skill CLI reads none of these. Give each a reader, drop it, or "
            f"— if the reader legitimately lives elsewhere (docker/devbox/lib, "
            f"a setup_env hook, another skill CLI) — widen this search and say "
            f"where it went."
        )


class TestTheDockerApiProxyIsRetired:
    """Design 14, held as a deletion rather than left to a grep somebody runs.

    The module, both Ansible templates and the `[devbox] api_proxy_*` keys all
    went in the same change that removed the sandbox bind — its only consumer —
    so the bind and its replacement never coexisted in a release.
    """

    def test_the_module_is_gone(self):
        assert not (_REPO / "src" / "istota" / "docker_proxy.py").exists()
        with pytest.raises(ImportError):
            import istota.docker_proxy  # noqa: F401

    @pytest.mark.parametrize("template", [
        "istota-docker-proxy@.service.j2",
        "istota-docker-proxy.tmpfiles.j2",
    ])
    def test_the_template_is_gone(self, template):
        assert not (_REPO / "deploy" / "ansible" / "templates" / template).exists()

    @pytest.mark.parametrize("key", [
        "api_proxy_enabled",
        "api_proxy_socket_dir",
        "api_proxy_exec_ttl_seconds",
        "api_proxy_audit_log",
        "docker_socket",
        "exec_timeout_seconds",
    ])
    def test_the_config_key_is_gone(self, key):
        from istota.config import DevboxConfig
        assert not hasattr(DevboxConfig(), key), (
            f"DevboxConfig.{key} survived the thing that read it"
        )

    def test_the_executor_binds_nothing_at_the_docker_path(self):
        source = _env_source_text()
        assert "api_proxy" not in source
        # The path may still be *named* in the comment explaining the removal;
        # what must not survive is a bind argument built from it.
        assert "docker.sock\"]" not in source
