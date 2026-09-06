"""ntfy push notification skill — POSTs to the user's configured ntfy server.

Credentials are injected by the skill proxy from the encrypted secrets table.
The skill never reads the secrets DB itself; everything arrives as env vars:

    NTFY_SERVER_URL  optional, defaults to https://ntfy.sh
    NTFY_TOPIC       default destination topic on the user's ntfy server
                     (required unless --topic is passed)
    NTFY_TOKEN       optional — bearer token (preferred when set)
    NTFY_USERNAME    optional — basic-auth username
    NTFY_PASSWORD    optional — basic-auth password (paired with username)

``--topic`` overrides ``NTFY_TOPIC`` for a single call, so one configured
server can route to many topics (per-device or per-category subscriptions)
without extra config. Auth and server stay shared; only the topic varies.

Usage:
    python -m istota.skills.ntfy send MESSAGE [--topic NAME] [--title T] [--priority N] [--tags ...] [--click URL]
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys

import httpx

from ... import ntfy_headers
from .._cli import emit, status_exit_code

DEFAULT_SERVER = "https://ntfy.sh"
REQUEST_TIMEOUT = 10.0

# ntfy topic names per the upstream server: ASCII letters, digits, dash,
# underscore; up to 64 chars. Strict to prevent path/query smuggling when
# we build f"{server}/{topic}".
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Authorization-shaped fragments we never want echoed back into stdout.
# Misconfigured ntfy servers sometimes reflect request headers in error
# bodies; the proxy returns our stdout to Claude, which gets indexed into
# memory_chunks — so leaks are durable.
_AUTH_LEAK_RE = re.compile(
    r"(?i)(authorization\s*:\s*)?(bearer\s+\S+|basic\s+[A-Za-z0-9+/=]+)"
)


def _redact(text: str) -> str:
    return _AUTH_LEAK_RE.sub("[redacted]", text)


def _emit(payload: dict) -> int:
    emit(payload, indent=None, ensure_ascii=True, exit_on_error=False)
    return status_exit_code(payload)


def _build_auth_header() -> str | None:
    token = (os.environ.get("NTFY_TOKEN") or "").strip()
    if token:
        return f"Bearer {token}"
    user = os.environ.get("NTFY_USERNAME") or ""
    if user:
        pw = os.environ.get("NTFY_PASSWORD") or ""
        creds = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return f"Basic {creds}"
    return None


def _scrub(value: str) -> str:
    """Strip newlines, and RFC 2047-encode anything non-ASCII.

    httpx serializes header values as ASCII, so an emoji/arrow/em-dash title
    would otherwise raise ``UnicodeEncodeError`` and lose the send; ntfy decodes
    the encoded-word back to the original glyphs (ISSUE-213). The helper is
    total, so this can't raise.
    """
    return ntfy_headers.encode_header_value(value)


def cmd_send(args: argparse.Namespace) -> int:
    # --topic overrides the configured default for this one call (ISSUE-166):
    # one server, many topics (per-device / per-category routing).
    topic = (getattr(args, "topic", None) or os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return _emit({
            "status": "error",
            "error": (
                "ntfy not configured for this user (no topic). "
                "Set a default at /istota/settings → Connected services → ntfy, "
                "or pass --topic."
            ),
        })
    if not _TOPIC_RE.match(topic):
        return _emit({
            "status": "error",
            "error": "ntfy topic is malformed (allowed: A-Z a-z 0-9 _ -, max 64 chars).",
        })

    server = (os.environ.get("NTFY_SERVER_URL") or DEFAULT_SERVER).rstrip("/")
    url = f"{server}/{topic}"

    headers: dict[str, str] = {}
    auth = _build_auth_header()
    if auth:
        headers["Authorization"] = auth
    if args.title:
        headers["Title"] = _scrub(args.title)
    if args.priority is not None:
        headers["Priority"] = str(args.priority)
    if args.tags:
        headers["Tags"] = _scrub(args.tags)
    if args.click:
        headers["Click"] = _scrub(args.click)
    if getattr(args, "markdown", False):
        headers["Markdown"] = "yes"

    try:
        resp = httpx.post(
            url,
            content=args.message,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = _redact(e.response.text or "")[:200]
        return _emit({
            "status": "error",
            "error": f"ntfy server returned {e.response.status_code}: {body}",
        })
    except httpx.HTTPError as e:
        return _emit({"status": "error", "error": f"ntfy request failed: {_redact(str(e))}"})
    except Exception as e:
        return _emit({"status": "error", "error": f"ntfy send crashed: {_redact(type(e).__name__)}"})

    return _emit({"status": "ok"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="istota-skill ntfy",
        description="Send push notifications via the user's ntfy server.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    send = sub.add_parser("send", help="Send a push notification")
    send.add_argument("message", help="Notification body")
    send.add_argument(
        "--topic", default=None,
        help="Destination topic (overrides the configured default for this call)",
    )
    send.add_argument("--title", default=None, help="Notification title")
    send.add_argument(
        "--priority", type=int, default=None, choices=[1, 2, 3, 4, 5],
        help="1=min, 3=default, 5=max",
    )
    send.add_argument("--tags", default=None, help="Comma-separated tags / emoji shortcodes")
    send.add_argument("--click", default=None, help="URL to open when the notification is tapped")
    send.add_argument(
        "--markdown", action="store_true",
        help="Render the body as markdown (ntfy web app only; a phone popup shows the source)",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "send":
        return cmd_send(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
