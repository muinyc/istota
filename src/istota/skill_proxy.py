"""Unix socket proxy for skill CLI commands.

Runs skill CLI commands with credentials injected server-side, so the
Claude subprocess never sees secret env vars. The protocol is one JSON
request/response per connection, newline-terminated.
"""

import json
import logging
import os
import selectors
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from istota import skill_client

logger = logging.getLogger("istota.skill_proxy")

# A failing accept() is retried rather than treated as shutdown, but a
# listener that fails forever must not spin. Give up after this many in a row.
MAX_ACCEPT_FAILURES = 20
ACCEPT_RETRY_DELAY_S = 0.05

# How much longer than the subprocess budget a connection stays armed, so the
# handler outlives the command it is waiting on and can report the timeout
# rather than dropping the connection under it.
CONNECTION_SLACK_SECONDS = 10

# Headroom between the largest skill budget and the client's wait: the
# connection slack above, plus room for the proxy to serialize and send a
# response the size of a skill's whole stdout after the subprocess has already
# spent its full budget.
CLIENT_WAIT_MARGIN_SECONDS = 30

# The ceiling on any single skill's timeout, the global and a per-skill entry
# alike. Derived from what `skill_client` waits rather than chosen: the client
# arms its socket before it sends and cannot read a config, so a server budget
# past that wait means the client gives up first and the model reads a completed
# call as no answer at all.
MAX_SKILL_TIMEOUT_SECONDS = (
    skill_client.SKILL_CLIENT_WAIT_SECONDS - CLIENT_WAIT_MARGIN_SECONDS
)

# The shipped per-skill policy, in code rather than in the config default, and
# that placement is the point (ISSUE-448). `config_mapper` maps a `dict` field
# through `coerce_dict`, which passes the operator's table through **verbatim**
# — a dict field replaces its default, it does not merge — and Ansible's own
# hash behaviour is replace too. So a `default_factory` carrying `code_review`
# would be silently dropped by an operator who wrote
# `[security.skill_proxy_timeouts]` to set *some other* skill, taking the
# review's ceiling back to the global and reintroducing the exact bug this map
# exists to fix, with only a log line to say so. Here it is not something an
# operator's table can clobber: `security.skill_proxy_timeouts` defaults to
# empty and is consulted first, so naming `code_review` there still overrides
# this, and naming anything else leaves it alone.
DEFAULT_SKILL_TIMEOUTS: dict[str, int] = {
    # The only skill that drives model calls of its own, so the only one whose
    # work is measured in minutes. 540 leaves room for the 480s per-agent budget
    # plus the review's own assembly reserve.
    "code_review": 540,
}


def resolve_skill_timeout(default: int, overrides, skill: str) -> int:
    """Seconds this skill's subprocess gets: operator entry, shipped policy, global.

    `security.skill_proxy_timeout` is one number applied to every proxied call,
    and `code_review` is the only skill that drives model calls of its own — so
    the only lever on a review's budget was a limit on every other skill too,
    and the review's ceiling was that global minus an assembly reserve (240s at
    the shipped default). A per-skill entry is what lets one skill have minutes
    without handing them to the rest (ISSUE-448).

    An entry **replaces** the value below it rather than raising it. Narrowing
    one chatty skill is the same mechanism as widening the review, and reading
    the value as a floor would silently ignore half of what the map is for.

    Pure and silent. Nothing raises and nothing is trusted — `config_mapper`
    passes a table's values through uncoerced, so what arrives is whatever the
    TOML said, and this runs on the path of every skill call the deployment
    makes, where one malformed line must not be able to break them all. It does
    not log, because per-connection is the wrong cadence for a fact about the
    configuration: `describe_skill_timeouts` reports the same judgements once,
    at proxy construction, by asking this function rather than restating it.
    """
    resolved = default
    entry = _entry_seconds(overrides, skill)
    if entry is None:
        entry = _entry_seconds(DEFAULT_SKILL_TIMEOUTS, skill)
    if entry is not None:
        resolved = entry
    return min(resolved, MAX_SKILL_TIMEOUT_SECONDS)


def _entry_seconds(overrides, skill) -> int | None:
    """One usable positive entry from a mapping, or None for absent or unusable.

    `bool` is excluded before `int()` because `int(True)` is 1 and
    `code_review = true` is a plausible typo that would otherwise resolve to a
    one-second budget rather than falling through.
    """
    try:
        raw = overrides.get(skill) if overrides is not None else None
    except AttributeError:
        return None
    if raw is None or isinstance(raw, bool):
        return None
    try:
        candidate = int(raw)
    except (TypeError, ValueError):
        return None
    return candidate if candidate > 0 else None


def describe_skill_timeouts(default: int, overrides) -> list[str]:
    """Every configured timeout whose resolved value is not what was written.

    Reported once, at proxy construction, rather than from the resolver — that
    runs per connection, so a warning there repeats for the life of the
    deployment on every skill call, which is noise rather than a diagnosis. And
    it is computed by *calling* `resolve_skill_timeout` rather than restating
    its rules, so a message here can never describe a decision the resolver did
    not make.

    Covers the unusable entry (a string, a bool, a zero) that silently falls
    through, and any value clamped by `MAX_SKILL_TIMEOUT_SECONDS` — the global
    included, which is the one an operator who never wrote a per-skill table can
    still trip.
    """
    notes: list[str] = []
    if default > MAX_SKILL_TIMEOUT_SECONDS:
        notes.append(
            f"skill_proxy_timeout of {default}s is past the "
            f"{skill_client.SKILL_CLIENT_WAIT_SECONDS}s the sandboxed client "
            f"waits, so every skill is being given "
            f"{MAX_SKILL_TIMEOUT_SECONDS}s instead"
        )
    try:
        written = dict(overrides) if overrides is not None else {}
    except (TypeError, ValueError):
        notes.append(
            f"skill_proxy_timeouts is {type(overrides).__name__}, not a table, "
            f"so every skill is being given the {default}s global"
        )
        return notes
    for skill in sorted(written, key=str):
        resolved = resolve_skill_timeout(default, written, skill)
        if _entry_seconds(written, skill) is None:
            notes.append(
                f"skill_proxy_timeouts[{skill!r}] is {written[skill]!r}, which "
                f"is not a positive number of seconds, so {skill!r} is being "
                f"given {resolved}s"
            )
        elif resolved != written[skill]:
            notes.append(
                f"skill_proxy_timeouts[{skill!r}] of {written[skill]!r} is past "
                f"the {skill_client.SKILL_CLIENT_WAIT_SECONDS}s the sandboxed "
                f"client waits, so {skill!r} is being given {resolved}s"
            )
    return notes


class SkillProxy:
    """Unix socket server that proxies skill CLI commands with credentials.

    Usage::

        with SkillProxy(sock_path, credential_env, base_env) as proxy:
            # Claude subprocess runs here — calls istota-skill client
            ...

    The server accepts connections, reads a JSON request, runs the skill
    CLI with merged env (base_env + credential_env), and returns the result.
    """

    def __init__(
        self,
        socket_path: Path,
        credential_env: dict[str, str],
        base_env: dict[str, str],
        timeout: int = 300,
        skill_timeouts: dict | None = None,
        allowed_credentials: set[str] | None = None,
        skill_credential_map: dict[str, set[str]] | None = None,
        allowed_skills: frozenset[str] | None = None,
        authorized_skills: frozenset[str] | None = None,
        task_id: int | None = None,
    ):
        self.socket_path = socket_path
        self.credential_env = credential_env
        self.base_env = base_env
        self.timeout = timeout
        # Per-skill overrides of the timeout above. Resolved per *connection*
        # rather than per proxy: one proxy serves every skill a task can call,
        # so `code_review`'s minutes and `email`'s seconds have to come apart
        # inside the handler.
        self.skill_timeouts = skill_timeouts
        for note in describe_skill_timeouts(timeout, skill_timeouts):
            logger.warning("skill proxy: %s", note)
        self.allowed_credentials = allowed_credentials
        self.skill_credential_map = skill_credential_map
        self.allowed_skills = allowed_skills
        # Skills authorized for credential access this task. None = no filter
        # (back-compat for callers that don't pass it). Used purely for the
        # informative-rejection list returned to the client.
        self.authorized_skills = authorized_skills
        self.task_id = task_id
        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_r: socket.socket | None = None
        self._wake_w: socket.socket | None = None
        self._accept_failures = 0

    def start(self) -> None:
        # Clean up stale socket file
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._stop_event.clear()
        self._accept_failures = 0
        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(str(self.socket_path))
        os.chmod(str(self.socket_path), 0o600)
        self._server_sock.listen(8)
        self._server_sock.setblocking(False)
        # Closing a socket does not reliably wake a thread blocked in accept(),
        # so stop() nudges this pair instead of the loop polling on a timeout.
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)

        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="skill-proxy",
        )
        self._thread.start()
        logger.debug("Skill proxy started on %s", self.socket_path)

    def stop(self) -> None:
        self._stop_event.set()
        if self._wake_w:
            try:
                self._wake_w.sendall(b"\x00")
            except OSError:
                pass
        stuck = False
        if self._thread:
            self._thread.join(timeout=5)
            stuck = self._thread.is_alive()
        self._thread = None

        if stuck:
            # Never close a socket the accept loop may still be selecting on:
            # epoll and kqueue drop a closed fd from the interest set silently,
            # so the thread would block forever on numbers the OS is free to
            # hand to unrelated code. Leak them, and say so.
            logger.warning(
                "Skill proxy accept loop did not exit within 5s; leaving its "
                "sockets open rather than closing them underneath it",
            )
        else:
            for sock in (self._server_sock, self._wake_r, self._wake_w):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
        self._server_sock = self._wake_r = self._wake_w = None
        # Clean up socket file
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.debug("Skill proxy stopped")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _accept_loop(self) -> None:
        with selectors.DefaultSelector() as sel:
            sel.register(self._server_sock, selectors.EVENT_READ)
            sel.register(self._wake_r, selectors.EVENT_READ)
            while not self._stop_event.is_set():
                if not self._accept_once(sel):
                    break

    def _accept_once(self, sel: selectors.BaseSelector) -> bool:
        """Wait for one readiness event. False means the loop should stop."""
        events = sel.select()
        # Shutdown wins over a connection that became ready in the same call.
        # Accepting it here would give a handler thread — and the credentials
        # it injects — a lifetime past the stop() meant to end them.
        if any(key.fileobj is self._wake_r for key, _ in events):
            return False

        try:
            conn, _ = self._server_sock.accept()
        except BlockingIOError:
            return True
        except OSError as exc:
            # One failed accept must not end the proxy. The listening socket
            # stays bound either way, so a dead loop turns every later skill
            # call into a hang in the backlog instead of a clean refusal —
            # skill_client waits out its full timeout on that socket.
            self._accept_failures += 1
            if self._accept_failures > MAX_ACCEPT_FAILURES:
                logger.error("Skill proxy accept failed %d times, stopping: %s",
                             self._accept_failures, exc)
                return False
            logger.warning("Skill proxy accept failed: %s", exc)
            time.sleep(ACCEPT_RETRY_DELAY_S)
            return True

        self._accept_failures = 0
        # accept() on a non-blocking listener yields a non-blocking socket on
        # BSD/macOS and a blocking one on Linux. Normalize, so a handler never
        # depends on which platform it woke up on.
        conn.setblocking(True)
        # Handle each connection in a new thread so multiple skill
        # calls can run concurrently (e.g. Claude runs two Bash calls).
        try:
            threading.Thread(
                target=self._handle_connection, args=(conn,),
                daemon=True, name="skill-proxy-handler",
            ).start()
        except RuntimeError as exc:
            logger.error("Skill proxy could not start a handler thread: %s", exc)
            conn.close()
        return True

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            # `settimeout` bounds each *blocking operation*, not the connection,
            # so this is the budget for the request read below and — after the
            # re-arm further down — for sending the response back. It is not an
            # end-to-end deadline and cannot expire while the handler sits in
            # `subprocess.run`, which is not a socket operation.
            #
            # Armed at the global here because the skill is not known until the
            # request is parsed.
            conn.settimeout(self.timeout + CONNECTION_SLACK_SECONDS)
            data = self._recv_all(conn)
            if not data:
                return

            try:
                request = json.loads(data)
            except json.JSONDecodeError as e:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Invalid JSON request: {e}",
                    "returncode": 1,
                })
                return

            # Route by request type: "credential" for lookups, default for skill calls
            req_type = request.get("type")

            if req_type == "credential":
                name = request.get("name", "")
                # Scope check: if allowed_credentials is set, only return
                # credentials authorized for this task.
                if self.allowed_credentials is not None and name not in self.allowed_credentials:
                    logger.warning(
                        "proxy_rejected task_id=%s type=credential name=%s reason=not_authorized",
                        self.task_id, name,
                    )
                    self._send_response(conn, {
                        "error": f"Credential not authorized for this task: {name!r}",
                        "reason": "not_authorized_credential",
                        "name": name,
                    })
                    return
                if name not in self.credential_env:
                    logger.warning(
                        "proxy_rejected task_id=%s type=credential name=%s reason=not_present",
                        self.task_id, name,
                    )
                    self._send_response(conn, {
                        "error": f"Credential not present in environment: {name!r}",
                        "reason": "credential_not_present",
                        "name": name,
                    })
                    return
                self._send_response(conn, {"value": self.credential_env[name]})
                return

            skill = request.get("skill", "")
            args = request.get("args", [])

            # Validate skill name against CLI-capable skills from skill index
            if self.allowed_skills is not None and skill not in self.allowed_skills:
                logger.warning(
                    "proxy_rejected task_id=%s type=skill skill=%s reason=unknown_skill",
                    self.task_id, skill,
                )
                authorized_list = (
                    sorted(self.authorized_skills)
                    if self.authorized_skills is not None
                    else sorted(self.allowed_skills)
                )
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": (
                        f"Unknown skill: {skill!r}.\n"
                        f"Authorized skills for this task: {', '.join(authorized_list)}"
                    ),
                    "returncode": 1,
                    "reason": "unknown_skill",
                    "skill": skill,
                    "authorized_skills": authorized_list,
                })
                return


            # Scales the *response send* with this skill's own budget: a skill
            # allowed nine minutes may also take longer to hand back its stdout
            # than one allowed five, and the global is an unrelated number to
            # bound that by. The credential branch and the two rejections above
            # return before this deliberately — each answers from memory, so the
            # global is already more than any of them can need.
            skill_timeout = resolve_skill_timeout(
                self.timeout, self.skill_timeouts, skill
            )
            if skill_timeout != self.timeout:
                conn.settimeout(skill_timeout + CONNECTION_SLACK_SECONDS)

            # Build command
            cmd = [sys.executable, "-m", f"istota.skills.{skill}"] + args

            # Merge envs: base gets only the credentials this skill needs
            merged_env = dict(self.base_env)
            if self.skill_credential_map is not None:
                allowed_vars = self.skill_credential_map.get(skill, set())
                for var in allowed_vars:
                    if var in self.credential_env:
                        merged_env[var] = self.credential_env[var]
            else:
                # Backward compat: no map means all credentials
                merged_env.update(self.credential_env)

            try:
                result = subprocess.run(
                    cmd,
                    env=merged_env,
                    capture_output=True,
                    text=True,
                    timeout=skill_timeout,
                )
                self._send_response(conn, {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                })
            except subprocess.TimeoutExpired:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Skill command timed out after {skill_timeout}s",
                    "returncode": 124,
                })
            except Exception as e:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Failed to run skill: {e}",
                    "returncode": 1,
                })

        except Exception:
            logger.debug("Error handling proxy connection", exc_info=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _recv_all(conn: socket.socket) -> str:
        """Read until newline (protocol delimiter)."""
        chunks = []
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks).decode("utf-8", errors="replace").strip()

    @staticmethod
    def _send_response(conn: socket.socket, response: dict) -> None:
        """Send JSON response terminated by newline."""
        data = json.dumps(response) + "\n"
        conn.sendall(data.encode("utf-8"))
