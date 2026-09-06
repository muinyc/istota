"""Web browsing skill - thin CLI client to the browser container API.

Usage:
    python -m istota.skills.browse get "https://example.com" [--keep-session] [--timeout 30]
    python -m istota.skills.browse render "https://example.com" [--mode full|article]
    python -m istota.skills.browse screenshot "https://example.com" [--output <workspace path>]
    python -m istota.skills.browse extract "https://example.com" --selector "article"
    python -m istota.skills.browse interact <session_id> --click ".button" --fill "#input=value"
    python -m istota.skills.browse links "https://example.com" [--selector "nav a"]
    python -m istota.skills.browse close <session_id>

Reads BROWSER_API_URL env var for the container endpoint.
"""

import argparse
import os
import re
import time
from pathlib import Path

import httpx

from istota.image_sniff import SNIFF_BYTES, sniff_raster
from istota.skill_host_paths import (
    resolve_host_path,
    user_workspace_root,
    write_resolved,
)
from istota.skills._cli import error_envelope, run_skill_cli
from istota.user_scope import scoped_user_dir

DEFAULT_API_URL = "http://localhost:9223"
# Where a screenshot lands with no `--output`, under the caller's own
# workspace: `{mount}/Users/{uid}/{bot dir}/screenshots/`. A subdirectory
# rather than the bot dir itself, so a task taking twenty captures does not
# bury the config, exports and notes directories the user reads.
SCREENSHOT_SUBDIR = "screenshots"
# How many derived names one capture will try before giving up. Only reached
# when that many captures land in the same UTC second, so it is a bound on a
# loop rather than a capacity.
MAX_NAME_ATTEMPTS = 100
# One extension per media type `image_sniff` admits, for the derived default
# name. Deriving it rather than always writing `.png` keeps the name honest
# about the bytes; `/chat/files` sniffs and would serve it either way.
_SUFFIX_FOR_MEDIA_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
REQUEST_TIMEOUT = 120.0  # HTTP client timeout (longer than page timeout)
MAX_BODY_EXCERPT = 400  # chars of an undecodable body to quote back
MAX_BODY_READ = 8192  # bytes of it to decode in the first place

# Characters that must not reach the excerpt. C0 and C1 cover the ANSI escapes
# (`\x1b`, and the C1 CSI at `\x9b`). The rest are invisible or reorder what
# follows them: U+202E and its neighbours reverse display order, so a body
# could otherwise render as a different message inside a line the model reads
# as this tool's own voice. U+2028/U+2029 are absent deliberately — the
# whitespace collapse below already takes them.
_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]",
)


def get_api_url():
    return os.environ.get("BROWSER_API_URL", DEFAULT_API_URL)


def _body_excerpt(resp, limit=MAX_BODY_EXCERPT):
    """A short, printable slice of a response body we could not decode.

    Returns the excerpt, `""` for a genuinely empty body, or None when the body
    could not be read at all — the caller renders those three apart, because
    "empty response" and "40 KB of something unreadable" are different outages
    and reporting the second as the first is a false statement rather than a
    missing detail.

    The body is whatever the container or an intermediary produced, so it is
    untrusted: `_CONTROL_RE` above says what is stripped and why, and runs of
    whitespace collapse, so a Flask HTML page or a proxy's error page reports
    as one readable line. Bounded before it is decoded rather than after, so an
    oversized error page costs one 8 KB copy instead of several full ones.
    Reading it must not raise, since this runs on the path that reports a
    failure — decoding with `errors="replace"` off a `bytes` that is already
    resident is what makes that true rather than hopeful.
    """
    try:
        raw = bytes(resp.content or b"")[:MAX_BODY_READ]
    except Exception:
        return None
    try:
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
    except Exception:
        # A bogus charset in the Content-Type is a LookupError, which is the
        # intermediary's mistake rather than a reason to report nothing.
        text = raw.decode("utf-8", errors="replace")
    text = _CONTROL_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _decode(resp):
    """Return the endpoint's JSON object, or an error naming what came instead.

    Every verb goes through this. The check is on the *body*, not the status:
    the API reports its own failures as JSON with a non-2xx status
    (`{"error": "url is required"}` with 400, `Chrome unavailable` with 503),
    and those bodies carry the diagnosis, so a `raise_for_status()` would throw
    away the useful half of the answer.

    What has to be caught is a body that is not a JSON object at all — Flask's
    default HTML 500 page, a 502 from something in front of the container, an
    empty or truncated response, or well-formed JSON of the wrong shape, which
    every caller turns into an `AttributeError` one frame later. Before
    ISSUE-383 the decode error reached `main`'s catch-all and printed as
    "Expecting value: line 1 column 1 (char 0)", naming no status, no URL and
    no part of the body, so an outage could not be told apart from an empty
    reply without reading the container's logs.

    A body the API *did* report an error in is also normalized here, because
    the API has two spellings for one thing: its 500s and 503s say
    `{"status": "error", ...}`, while its argument and lookup failures say a
    bare `{"error": ...}` with no `status` key at all — nine such paths, two of
    them 500s. Every caller in this module branches on `status`, so without the
    stamp the commonest server-side rejection (bad arguments, an expired
    session) reads as a success: `main` exits 0 and `cmd_links` treats it as a
    page. It also made two verbs disagree about one server response, since
    `cmd_render` rewrites that shape by hand for its own 404 and nothing else
    did. Stamping it here makes that rewrite the general rule.
    """
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        if data.get("error") and "status" not in data:
            return {"status": "error", **data}
        return data

    excerpt = _body_excerpt(resp)
    if excerpt is None:
        shown = "(unreadable)"
    elif not excerpt:
        shown = "(empty)"
    else:
        shown = excerpt
    try:
        where = f" for {resp.url}"
    except Exception:
        where = ""
    error = (
        f"Browser API returned HTTP {resp.status_code}{where} "
        f"with a body that is not a JSON object: {shown}"
    )
    if resp.status_code == 503:
        # The message the unreachable `except httpx.HTTPStatusError` arm in
        # `main` used to hold, on a path that can actually be reached. The
        # API's own 503 is JSON and returns above with a better one.
        error += " — the browser may be restarting inside the container, retry in a few seconds."
    return {"status": "error", "error": error}


def cmd_get(args):
    """Browse a URL and return page content."""
    url = get_api_url()
    payload = {
        "url": args.url,
        "timeout": args.timeout,
        "keep_session": args.keep_session,
    }
    if args.session:
        payload["session_id"] = args.session
    if args.wait_for:
        payload["wait_for"] = args.wait_for
    if args.skip_behavior:
        payload["skip_behavior"] = True
    if args.max_chars:
        payload["max_chars"] = args.max_chars
    if args.max_links:
        payload["max_links"] = args.max_links

    resp = httpx.post(f"{url}/browse", json=payload, timeout=REQUEST_TIMEOUT)
    return _decode(resp)


def cmd_render(args):
    """Render a page to markdown, keeping headings and links together."""
    url = get_api_url()
    if not args.url and not args.session:
        return {
            "status": "error",
            "error": "render needs a URL or --session <id>",
        }

    payload = {
        "mode": args.mode,
        "timeout": args.timeout,
        "keep_session": args.keep_session,
    }
    if args.url:
        payload["url"] = args.url
    if args.session:
        payload["session_id"] = args.session
    if args.wait_for:
        payload["wait_for"] = args.wait_for
    if args.max_chars:
        payload["max_chars"] = args.max_chars
    if args.skip_behavior:
        payload["skip_behavior"] = True

    resp = httpx.post(f"{url}/render", json=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        # Two unrelated failures share this status. The endpoint's own
        # "session not found or expired" is a JSON body with an `error` key —
        # routine, since sessions expire after 10 minutes and the scroll-then-
        # re-render recipe re-uses one. A container predating the renderer
        # answers Flask's HTML route-miss instead, and only that one means the
        # feature is absent. Reporting both as absent would send the agent back
        # to the flattened-text path this command exists to replace.
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("error"):
            return {"status": "error", "error": data["error"]}
        return {
            "status": "error",
            "error": "This browser container has no render endpoint — use `browse get` instead.",
        }
    return _decode(resp)


def screenshot_dir():
    """Where a screenshot goes with no ``--output``: ``(directory, reason)``.

    ``{mount}/Users/{uid}/{bot dir}/screenshots``. Every component is derived
    rather than configured, and the derivation is the point of the directory:
    the old default was ``/tmp/screenshot.png``, which on a sandboxed
    deployment named a file on the far side of the boundary. The skill CLI is
    spawned host-side by the proxy while the model's ``/tmp`` is the sandbox's
    own ``--tmpfs``, so the write landed on the host and the model was handed a
    path it could not open. The workspace is the one root that is bound into
    the sandbox read-write *and* reachable through ``/chat/files``, which is
    what lets a task read its own capture back and the reply embed it.

    Two independent things can stop it resolving and the caller has to be able
    to tell them apart, so the reason comes back beside the answer rather than
    being reconstructed from a single `None` — naming a variable that is set
    when a different one is the fault is the misreport `doctor` states the rule
    against. Falling back to ``/tmp`` is the bug this replaces.

    ``ISTOTA_BOT_DIR_NAME`` is required rather than defaulted, matching what
    the two ``memory`` skills already do with it. Guessing ``istota`` on a
    deployment whose bot is called something else files the capture in a
    directory beside the real bot dir, where it still serves and so reports
    nothing.
    """
    workspace = user_workspace_root()
    if workspace is None:
        return None, (
            "No workspace resolved for this task (NEXTCLOUD_MOUNT_PATH / "
            "ISTOTA_USER_ID), so there is nowhere to put a screenshot. Pass "
            "--output with a path inside your workspace."
        )
    bot_dir = os.environ.get("ISTOTA_BOT_DIR_NAME", "").strip()
    if not bot_dir:
        return None, (
            "ISTOTA_BOT_DIR_NAME is not set, so the workspace directory to "
            "write into cannot be derived. Pass --output with a path inside "
            "your workspace."
        )
    # The generic "names a plain child of this root" rule, reached for here
    # because the variable lands in a path. `Config.bot_dir_name` already
    # sanitizes to `[a-z0-9_-]`, so this is defence behind that rather than the
    # boundary — and it refuses rather than substituting a name of its own,
    # since a silent substitution would put the file somewhere the reply's URL
    # does not name.
    scoped = scoped_user_dir(workspace, bot_dir)
    if scoped is None:
        return None, (
            "ISTOTA_BOT_DIR_NAME does not name a plain directory inside the "
            "workspace, so there is nowhere to put a screenshot. Pass "
            "--output with a path inside your workspace."
        )
    return scoped / SCREENSHOT_SUBDIR, None


def _write_derived_capture(directory, media_type, content):
    """Write the capture under a name nothing else holds: ``(path, error)``.

    ``screenshot-<utc timestamp>.<ext>``, timestamped rather than a fixed
    ``screenshot.png`` because a task that captures two charts would otherwise
    embed the second one twice.

    **The uniqueness is the open's, not a prior `exists()` check.** Two tasks
    of one user can derive the same name inside one second, the scheduler runs
    a worker pool, and a check-then-write would let the second silently
    overwrite the first — both reporting `ok`, one with a `size` describing
    bytes that are no longer there. `O_EXCL` collapses the check and the create
    into one step; a collision is a `FileExistsError` and the next candidate is
    tried. Exhausting the bound is an error rather than a fallback to a name
    already known to be taken, which is the overwrite wearing a different hat.
    """
    suffix = _SUFFIX_FOR_MEDIA_TYPE.get(media_type, ".png")
    stem = time.strftime("screenshot-%Y%m%d-%H%M%S", time.gmtime())
    for n in range(1, MAX_NAME_ATTEMPTS + 1):
        name = f"{stem}{suffix}" if n == 1 else f"{stem}-{n}{suffix}"
        resolved, err = resolve_host_path(
            directory / name, writable=True, operation="browse screenshot",
        )
        if err:
            return None, err
        try:
            write_resolved(resolved, content, exclusive=True)
        except FileExistsError:
            continue
        except OSError as e:
            return None, f"could not write {resolved}: {e}"
        return resolved, None
    return None, (
        f"could not find an unused name under {directory} after "
        f"{MAX_NAME_ATTEMPTS} attempts; nothing was written."
    )


def _workspace_relative(path):
    """``/Users/{uid}/…`` for a resolved capture, or None.

    The ``?path=`` value ``/chat/files`` takes and the guidelines teach, handed
    back beside the host path so the reply's URL is a copy rather than a
    reconstruction.

    **Built against the workspace root, not the mount, because that is the
    confinement the consumer applies.** `_resolve_chat_file` serves
    `/Users/{uid}` and nothing else, while `allowed_host_roots` also admits the
    task's own `{mount}/Channels/{token}` as a destination — so a `--output`
    there is a legitimate write whose mount-relative spelling is a URL the
    endpoint refuses by design. None for anything outside the workspace, which
    covers that case and the deferred dir with it.
    """
    root = user_workspace_root()
    if root is None:
        return None
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    try:
        relative = path.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    # `user_workspace_root` already established that the user id is one plain
    # component, so this join cannot walk anywhere.
    return f"/Users/{user_id}/{relative}"


def cmd_screenshot(args):
    """Take a screenshot, into the caller's own workspace.

    The destination is settled **before** the capture, so a path outside the
    allowlist costs no browser time and nothing is written; the name is
    settled after, because the derived default takes its extension from what
    the bytes turn out to be. An *admitted* path's parent directory is created
    by that early check, so a capture that then fails can leave an empty
    directory inside the workspace — inside the allowlist either way, and the
    cost of refusing before spending two minutes on a page.
    """
    directory = None
    if not args.output:
        directory, reason = screenshot_dir()
        if directory is None:
            return {"status": "error", "error": reason}
    else:
        # Resolved twice for the `--output` case: once here to refuse early,
        # and once below on the same value. The second is not redundant — the
        # first happens before a capture that takes up to two minutes, and a
        # check that old is not the one to write behind.
        _, path_err = resolve_host_path(
            Path(args.output), writable=True, operation="browse screenshot --output",
        )
        if path_err:
            return {"status": "error", "error": path_err}

    url = get_api_url()
    payload = {
        "timeout": args.timeout,
        "full_page": args.full_page,
    }
    if args.url:
        payload["url"] = args.url
    if args.session:
        payload["session_id"] = args.session

    resp = httpx.post(f"{url}/screenshot", json=payload, timeout=REQUEST_TIMEOUT)

    # The status is checked as well as the content type, because this is the
    # one verb that reports success off a body it never parses: an intermediary
    # answering 502 while labelling it `image/png` used to have its error page
    # written to disk as a .png and reported `status: ok`. A zero-length body
    # is refused for the same reason — `size: 0` reads as a screenshot.
    is_image = resp.headers.get("content-type", "").startswith("image/")
    if resp.status_code == 200 and is_image and resp.content:
        content = bytes(resp.content)
        # The content type is the container's claim about the body; this is the
        # body. Same predicate `/chat/files` sniffs the file with, so a capture
        # that would come back as a download rather than an image is refused
        # here instead of being embedded as a broken one — and a 200 carrying
        # an `image/png` label over an HTML error page is caught one level
        # deeper than the status check above catches it.
        media_type = sniff_raster(content[:SNIFF_BYTES])
        if media_type is None:
            return {
                "status": "error",
                "error": (
                    f"The browser API returned {len(content)} bytes labelled "
                    f"{resp.headers.get('content-type', 'nothing')} that are "
                    f"not a PNG, JPEG, GIF or WebP — not a screenshot, and "
                    f"nothing was written."
                ),
            }
        if args.output:
            resolved, path_err = resolve_host_path(
                Path(args.output), writable=True,
                operation="browse screenshot --output",
            )
            if path_err:
                return {"status": "error", "error": path_err}
            try:
                write_resolved(resolved, content)
            except OSError as e:
                return {
                    "status": "error",
                    "error": f"could not write {resolved}: {e}",
                }
        else:
            resolved, path_err = _write_derived_capture(
                directory, media_type, content,
            )
            if path_err:
                return {"status": "error", "error": path_err}
        result = {
            "status": "ok",
            "path": str(resolved),
            "size": len(content),
            "media_type": media_type,
        }
        workspace_path = _workspace_relative(resolved)
        if workspace_path:
            result["workspace_path"] = workspace_path
        return result
    if is_image:
        return {
            "status": "error",
            "error": (
                f"Browser API returned HTTP {resp.status_code} with "
                f"{len(resp.content)} bytes labelled "
                f"{resp.headers.get('content-type', 'nothing')} — not a screenshot."
            ),
        }
    return _decode(resp)


def cmd_extract(args):
    """Extract content by CSS selector."""
    url = get_api_url()
    payload = {
        "selector": args.selector,
        "timeout": args.timeout,
    }
    if args.url:
        payload["url"] = args.url
    if args.session:
        payload["session_id"] = args.session
    if args.max_chars:
        payload["max_chars"] = args.max_chars
    if args.limit:
        payload["limit"] = args.limit

    resp = httpx.post(f"{url}/extract", json=payload, timeout=REQUEST_TIMEOUT)
    return _decode(resp)


def cmd_interact(args):
    """Interact with an existing session."""
    url = get_api_url()
    actions = []

    if args.click:
        for selector in args.click:
            actions.append({"type": "click", "selector": selector})
    if args.fill:
        for fill_spec in args.fill:
            if "=" in fill_spec:
                selector, value = fill_spec.split("=", 1)
                actions.append({"type": "fill", "selector": selector, "value": value})
    if args.scroll:
        actions.append({"type": "scroll", "direction": args.scroll, "amount": args.scroll_amount})

    payload = {
        "session_id": args.session_id,
        "actions": actions,
    }

    resp = httpx.post(f"{url}/interact", json=payload, timeout=REQUEST_TIMEOUT)
    return _decode(resp)


def _links_from_extract(data):
    """Extract links from /extract response elements.

    Prefers the 'href' attribute returned directly on each element
    (set when the matched element is itself a link). Falls back to
    parsing <a href> tags from inner HTML for nested links.
    """
    links = []
    for el in data.get("elements", []):
        href = el.get("href")
        if href:
            # Element itself is a link — use its text and href directly
            links.append({"text": el.get("text", "").strip(), "href": href})
        else:
            # Search for <a> tags inside the element's inner HTML
            html = el.get("html", "")
            for match in re.finditer(
                r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            ):
                h, text = match.group(1), match.group(2)
                text = re.sub(r"<[^>]+>", "", text).strip()
                links.append({"text": text, "href": h})
    return links


def cmd_links(args):
    """Fetch a page and return only the links."""
    url = get_api_url()

    if args.selector and args.session:
        # Extract links from specific elements in existing session
        payload = {"selector": args.selector, "timeout": args.timeout}
        payload["session_id"] = args.session
        resp = httpx.post(f"{url}/extract", json=payload, timeout=REQUEST_TIMEOUT)
        data = _decode(resp)
        if data.get("status") != "ok":
            return data
        links = _links_from_extract(data)
        return {
            "status": "ok",
            "url": data.get("url", ""),
            "count": len(links),
            "links": links,
        }
    elif args.selector:
        # Fetch page then extract links from selector
        payload = {"url": args.url, "timeout": args.timeout, "keep_session": False}
        resp = httpx.post(f"{url}/browse", json=payload, timeout=REQUEST_TIMEOUT)
        browse_data = _decode(resp)
        if browse_data.get("status") != "ok":
            return browse_data
        session_id = browse_data.get("session_id")
        # Extract from selector
        ext_payload = {"selector": args.selector, "timeout": args.timeout}
        if session_id:
            ext_payload["session_id"] = session_id
        else:
            ext_payload["url"] = args.url
        ext_resp = httpx.post(f"{url}/extract", json=ext_payload, timeout=REQUEST_TIMEOUT)
        data = _decode(ext_resp)
        # Clean up session if we got one
        if session_id:
            try:
                httpx.delete(f"{url}/sessions/{session_id}", timeout=5.0)
            except Exception:
                pass
        if data.get("status") != "ok":
            return data
        links = _links_from_extract(data)
        return {
            "status": "ok",
            "url": browse_data.get("url", args.url),
            "count": len(links),
            "links": links,
        }
    else:
        # Simple: fetch page, return only links
        payload = {"url": args.url, "timeout": args.timeout, "keep_session": False}
        if args.session:
            payload["session_id"] = args.session
        resp = httpx.post(f"{url}/browse", json=payload, timeout=REQUEST_TIMEOUT)
        data = _decode(resp)
        if data.get("status") != "ok":
            return data
        links = data.get("links", [])
        return {
            "status": "ok",
            "url": data.get("url", args.url),
            "count": len(links),
            "links": links,
        }


def cmd_close(args):
    """Close a session."""
    url = get_api_url()
    resp = httpx.delete(f"{url}/sessions/{args.session_id}", timeout=30.0)
    return _decode(resp)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.browse",
        description="Web browsing via headless browser container",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # get
    p_get = sub.add_parser("get", help="Browse a URL")
    p_get.add_argument("url", help="URL to browse")
    p_get.add_argument("--keep-session", action="store_true", help="Keep session alive for follow-up")
    p_get.add_argument("--session", help="Reuse existing session ID")
    p_get.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds")
    p_get.add_argument("--wait-for", help="CSS selector to wait for after load")
    p_get.add_argument("--skip-behavior", action="store_true",
                       help="Skip simulated mouse/scroll after load (for DataDome-protected sites)")
    p_get.add_argument("--max-chars", type=int, help="Page text budget (default 50000)")
    p_get.add_argument("--max-links", type=int, help="Link budget (default 100)")

    # render
    p_render = sub.add_parser(
        "render", help="Render a page to markdown (keeps headings + links together)",
    )
    p_render.add_argument("url", nargs="?", help="URL to render")
    p_render.add_argument("--mode", choices=["full", "article"], default="full",
                          help="full = whole page (hubs/indexes); article = main content only")
    p_render.add_argument("--session", help="Existing session ID")
    p_render.add_argument("--keep-session", action="store_true", help="Keep session alive")
    p_render.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds")
    p_render.add_argument("--wait-for", help="CSS selector to wait for after load")
    p_render.add_argument("--max-chars", type=int, help="Markdown budget (default 100000)")
    p_render.add_argument("--skip-behavior", action="store_true",
                          help="Skip simulated mouse/scroll after load")

    # screenshot
    p_ss = sub.add_parser("screenshot", help="Take a screenshot")
    p_ss.add_argument("url", nargs="?", help="URL to screenshot")
    p_ss.add_argument("--session", help="Existing session ID")
    p_ss.add_argument(
        "--output", "-o",
        help=(
            "Output file path, absolute and inside your own workspace. "
            "Defaults to {bot dir}/screenshots/ in your workspace."
        ),
    )
    p_ss.add_argument("--full-page", action="store_true", help="Capture full page")
    p_ss.add_argument("--timeout", type=int, default=30)

    # extract
    p_ext = sub.add_parser("extract", help="Extract content by CSS selector")
    p_ext.add_argument("url", nargs="?", help="URL to extract from")
    p_ext.add_argument("--selector", "-s", required=True, help="CSS selector")
    p_ext.add_argument("--session", help="Existing session ID")
    p_ext.add_argument("--timeout", type=int, default=30)
    p_ext.add_argument("--max-chars", type=int,
                       help="Per-element text/HTML budget (default 25000)")
    p_ext.add_argument("--limit", type=int, help="Max matched elements (default 20)")

    # interact
    p_int = sub.add_parser("interact", help="Interact with existing session")
    p_int.add_argument("session_id", help="Session ID")
    p_int.add_argument("--click", action="append", help="CSS selector to click")
    p_int.add_argument("--fill", action="append", help="selector=value to fill")
    p_int.add_argument("--scroll", choices=["up", "down"], help="Scroll direction")
    p_int.add_argument("--scroll-amount", type=int, default=500, help="Scroll pixels")

    # links
    p_links = sub.add_parser("links", help="Fetch a page and return only links")
    p_links.add_argument("url", nargs="?", help="URL to fetch links from")
    p_links.add_argument("--selector", "-s", help="CSS selector to extract links from")
    p_links.add_argument("--session", help="Existing session ID")
    p_links.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds")

    # close
    p_close = sub.add_parser("close", help="Close a session")
    p_close.add_argument("session_id", help="Session ID to close")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "get": cmd_get,
        "render": cmd_render,
        "screenshot": cmd_screenshot,
        "extract": cmd_extract,
        "interact": cmd_interact,
        "links": cmd_links,
        "close": cmd_close,
    }

    def describe(exc: BaseException) -> dict:
        if isinstance(exc, httpx.ConnectError):
            # Nothing answered at all, which is a different fact from the
            # container answering badly and stays a separate message.
            return error_envelope(
                f"Cannot connect to browser API at {get_api_url()}. "
                "Is the container running?"
            )
        # `str(exc)` alone is the same defect ISSUE-383 fixed one layer down:
        # httpx's ReadTimeout stringifies to "timed out" and several of its
        # siblings to the empty string, naming no verb, no URL and no class.
        # That is reachable on the ordinary path, since REQUEST_TIMEOUT sits
        # above the container's own 90s watchdog.
        detail = str(exc).strip()
        return error_envelope(
            f"browse {args.command} against {get_api_url()} failed: "
            f"{type(exc).__name__}{': ' + detail if detail else ''}"
        )

    # A failure the endpoint reported in a well-formed body is still a failure,
    # which `run_skill_cli` is what enforces. `_decode` turned what used to be
    # an uncaught exception into a return value, so without that a 500 would
    # report on exit 0 where the decode error at least exited 1. Only "error"
    # counts: "closed" and "not_found" are answers.
    run_skill_cli(commands, args, on_exception=describe)


if __name__ == "__main__":
    main()
