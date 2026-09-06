"""Key-value store skill CLI.

Provides get/set/list/delete/namespaces commands against the istota_kv table.
Reads hit the DB directly; writes are deferred when ISTOTA_DEFERRED_DIR is set
(sandbox mode).

Two shapes of the interface are load-bearing and easy to get wrong:

* **A value passed as an argv element is capped at 128 KiB**, by the kernel
  rather than by the store (`MAX_ARG_STRLEN` = 32 x page size; `execve`
  returns E2BIG above it). `istota_kv.value` is bare TEXT with no CHECK, so
  the store itself is bounded only by SQLite's `SQLITE_MAX_LENGTH`. Values
  that grow without bound are maintained with the set ops, which never pass
  the accumulated array as an argument; `set --value-file` is the escape
  hatch for a genuinely large whole-value write.
* **Every call costs two interpreter starts**, because it crosses the skill
  proxy. Verbs that a caller would otherwise run in a loop take batches:
  `set-contains`, `set-add` and `set-remove` all accept many members.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from istota.kv_namespaces import is_reserved_namespace
from istota.skill_host_paths import resolve_host_path
from istota.skills._cli import fail as _fail, run_skill_cli

# `list` decodes and prints every value in a namespace. The natural command for
# orienting in a namespace should not be the one that dumps a 153 KB array into
# the model's context, so values are previewed by default. `get` and
# `set-members` are explicit requests for content and stay whole.
DEFAULT_MAX_VALUE_CHARS = 2048

# `--value-file` exists to get past the 128 KiB argv cap, not to remove every
# ceiling: the content is read whole, re-parsed by `json.loads`, and under the
# sandbox embedded into the deferred-ops file, which every later kv op in the
# task re-reads and re-serialises. 8 MiB is far above any real value and still
# bounds that quadratic. This caps one command's input; it is not a limit on
# the column, which stays unconstrained.
MAX_VALUE_FILE_BYTES = 8 * 1024 * 1024


def _get_conn():
    from istota import db

    db_path = os.environ.get("ISTOTA_DB_PATH", "")
    if not db_path:
        _fail("ISTOTA_DB_PATH not set")
    return db.get_db(db_path)


def _user_id():
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _fail("ISTOTA_USER_ID not set")
    return user_id


def _defer_op(entry: dict) -> bool:
    """Write a deferred KV operation for the scheduler to process.

    Returns True if the op was queued for deferred processing, False if no
    deferred dir is configured (caller should fall back to direct write).
    """
    deferred_dir = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    task_id = os.environ.get("ISTOTA_TASK_ID", "")
    if not deferred_dir or not task_id:
        return False

    from pathlib import Path

    path = Path(deferred_dir) / f"task_{task_id}_kv_ops.json"

    ops = []
    if path.exists():
        try:
            ops = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            ops = []

    ops.append(entry)
    path.write_text(json.dumps(ops), encoding="utf-8")
    return True


def _defer_write(
    operation: str, namespace: str, key: str, value: str | None = None,
    scope: str | None = None,
):
    entry: dict = {"op": operation, "namespace": namespace, "key": key}
    if value is not None:
        entry["value"] = value
    if scope:
        entry["scope"] = scope
    return _defer_op(entry)


def _load_config():
    """Load config for the direct-write admin gate.

    Reads ISTOTA_CONFIG_PATH (already set for skill subprocesses, like the email
    skill's scoping path). Returns None if config can't be loaded — the caller
    then fails closed on the shared-write gate.
    """
    try:
        from istota.config import load_config

        cfg_path = os.environ.get("ISTOTA_CONFIG_PATH", "")
        from pathlib import Path

        return load_config(Path(cfg_path) if cfg_path else None)
    except Exception:  # noqa: BLE001
        return None


def _shared_write_denied() -> bool:
    """Direct-write gate: print the error envelope + return True when the
    caller may not write shared content. Fail-closed if config can't load."""
    config = _load_config()
    user_id = _user_id()
    if config is not None and config.is_shared_kv_writer(user_id):
        return False
    print(json.dumps({
        "status": "error", "error": "shared KV writes require admin",
    }))
    return True


def _load_set(conn, user_id: str, namespace: str, key: str) -> tuple[list | None, bool]:
    """Load a set-shaped value. Returns (members, exists).

    - If the key doesn't exist: ([], False).
    - If it exists and is a JSON array: (members, True).
    - If it exists but isn't an array: prints error JSON and exits.
    """
    from istota import db

    row = db.kv_get(conn, user_id, namespace, key)
    if row is None:
        return [], False
    try:
        parsed = json.loads(row["value"])
    except json.JSONDecodeError:
        _fail(f"value at {namespace}/{key} is not valid JSON")
    if not isinstance(parsed, list):
        _fail(f"value at {namespace}/{key} is not a JSON array")
    return parsed, True


def cmd_get(args):
    from istota import db

    shared = getattr(args, "shared", False)
    user_id = None if shared else _user_id()
    with _get_conn() as conn:
        if shared:
            result = db.shared_kv_get(conn, args.namespace, args.key)
        else:
            result = db.kv_get(conn, user_id, args.namespace, args.key)
    if result is None:
        print(json.dumps({"status": "not_found"}))
    else:
        try:
            value = json.loads(result["value"])
        except json.JSONDecodeError:
            value = result["value"]
        print(json.dumps({"status": "ok", "value": value}))


def _resolve_set_value(args) -> str:
    """Return the value to store, from the positional argument or a file.

    ``--value-file`` exists because the positional form is capped at 128 KiB by
    `MAX_ARG_STRLEN` — the write dies in `execve` before any Python runs, at a
    size nothing in the store or the docs names. The file is read *host-side*,
    where this CLI runs, so it is scoped to the roots the sandboxed caller can
    itself write to. Without that scoping the flag would be an arbitrary
    host-file read whose result `kv get` hands straight back.
    """
    value = getattr(args, "value", None)
    value_file = getattr(args, "value_file", None)

    if value is not None and value_file:
        _fail("pass either a positional value or --value-file, not both")
    if value is None and not value_file:
        _fail("no value given: pass a JSON value or --value-file <path>")

    if value_file:
        resolved, err = resolve_host_path(
            Path(value_file), writable=False, operation="kv set --value-file",
        )
        if err:
            _fail(err)
        # Read the *resolved* path: reopening the original re-walks every
        # symlink in it, which is the window the check just closed.
        if not resolved.is_file():
            _fail(f"--value-file is not a regular file: {resolved}")
        # O_NOFOLLOW on the leaf: resolving stripped the symlinks that existed
        # at check time, but the final component could still be swapped for one
        # before this open. Size is taken from the same descriptor, so it
        # describes the file actually being read.
        try:
            fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            _fail(f"could not open --value-file: {e}")
        try:
            size = os.fstat(fd).st_size
            if size > MAX_VALUE_FILE_BYTES:
                _fail(
                    f"--value-file is {size} bytes, over the "
                    f"{MAX_VALUE_FILE_BYTES}-byte cap"
                )
            with os.fdopen(fd, encoding="utf-8") as fh:
                fd = -1  # fdopen owns it now
                value = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            _fail(f"could not read --value-file: {e}")
        finally:
            if fd >= 0:
                os.close(fd)

    try:
        json.loads(value)
    except json.JSONDecodeError:
        _fail("invalid JSON value")
    return value


def cmd_set(args):
    args.value = _resolve_set_value(args)

    shared = getattr(args, "shared", False)

    # Try deferred write first (sandbox mode). The shared-write gate is applied
    # at apply time against task.user_id (the trusted identity), so the deferred
    # op just carries scope:"shared" — it is not authorized here.
    if _defer_write("set", args.namespace, args.key, args.value,
                    scope="shared" if shared else None):
        print(json.dumps({"status": "ok", "deferred": True}))
        return

    # Direct write (outside sandbox)
    from istota import db

    if shared:
        if _shared_write_denied():
            sys.exit(1)
        with _get_conn() as conn:
            db.shared_kv_set(conn, args.namespace, args.key, args.value, _user_id())
        print(json.dumps({"status": "ok"}))
        return

    user_id = _user_id()
    with _get_conn() as conn:
        db.kv_set(conn, user_id, args.namespace, args.key, args.value)
    print(json.dumps({"status": "ok"}))


def cmd_delete(args):
    shared = getattr(args, "shared", False)

    # Try deferred write first (sandbox mode).
    if _defer_write("delete", args.namespace, args.key,
                    scope="shared" if shared else None):
        print(json.dumps({"status": "ok", "deferred": True}))
        return

    # Direct write (outside sandbox)
    from istota import db

    if shared:
        if _shared_write_denied():
            sys.exit(1)
        with _get_conn() as conn:
            deleted = db.shared_kv_delete(conn, args.namespace, args.key)
        print(json.dumps({"status": "ok", "deleted": deleted}))
        return

    user_id = _user_id()
    with _get_conn() as conn:
        deleted = db.kv_delete(conn, user_id, args.namespace, args.key)
    print(json.dumps({"status": "ok", "deleted": deleted}))


def render_list_entries(
    entries: list[dict], *, keys_only: bool, max_value_chars: int,
) -> int:
    """Decode each entry's value in place, bounding what gets printed.

    Returns the number of entries whose value was truncated. Shared with the
    operator CLI (`istota kv list`), which differs only in its default: a human
    piping to jq wants the whole value, the model orienting in a namespace
    does not.

    A truncated value is a *string* preview of the JSON text rather than the
    parsed object — there is no way to lop a prefix off a decoded dict — so
    `truncated: true` sits beside it to say the shape changed. `value_chars` is
    always the full length, which is the number a caller needs to decide
    whether to `get` it.
    """
    truncated = 0
    for entry in entries:
        raw = entry["value"]
        entry["value_chars"] = len(raw)
        if keys_only:
            del entry["value"]
            continue
        if max_value_chars and len(raw) > max_value_chars:
            entry["value"] = raw[:max_value_chars]
            entry["truncated"] = True
            truncated += 1
            continue
        try:
            entry["value"] = json.loads(raw)
        except json.JSONDecodeError:
            pass  # Not JSON — pass the raw text through, as this always has.
    return truncated


def cmd_list(args):
    from istota import db

    max_value_chars = getattr(args, "max_value_chars", DEFAULT_MAX_VALUE_CHARS)
    if max_value_chars < 0:
        _fail("--max-value-chars must be >= 0 (0 disables truncation)")

    shared = getattr(args, "shared", False)
    with _get_conn() as conn:
        if shared:
            entries = db.shared_kv_list(conn, args.namespace)
        else:
            entries = db.kv_list(conn, _user_id(), args.namespace)
    truncated = render_list_entries(
        entries,
        keys_only=getattr(args, "keys_only", False),
        max_value_chars=max_value_chars,
    )
    print(json.dumps({
        "status": "ok",
        "count": len(entries),
        "truncated_count": truncated,
        "entries": entries,
    }))


def cmd_namespaces(args):
    from istota import db

    shared = getattr(args, "shared", False)
    with _get_conn() as conn:
        if shared:
            namespaces = db.shared_kv_namespaces(conn)
        else:
            namespaces = db.kv_namespaces(conn, _user_id())
    # Reserved namespaces are refused by every other verb, so listing them
    # would only offer the caller a name that answers "reserved" — and it puts
    # framework internals in the model's context for no reader.
    namespaces = [n for n in namespaces if not is_reserved_namespace(n)]
    print(json.dumps({"status": "ok", "namespaces": namespaces}))


def cmd_shared_status(args):
    """Report whether this identity may write shared KV. Pure read.

    The gate is Config.is_shared_kv_writer, which is deliberately asymmetric to
    is_admin: a blank admins file authorizes NOBODY here (fail-closed). So this
    answers the deployment-specific question directly instead of the model
    inferring it from admin status or hunting through config/source.
    """
    user_id = _user_id()
    config = _load_config()
    can_write = bool(config is not None and config.is_shared_kv_writer(user_id))
    admins_configured = bool(config is not None and config.admin_users)
    print(json.dumps({
        "status": "ok",
        "user_id": user_id,
        "can_write_shared": can_write,
        "admins_configured": admins_configured,
    }))


def cmd_set_contains(args):
    """Check one or many members against the array at <ns>/<key>.

    Batching is the whole point: a run checking 50 fresh items against a stored
    set used to pay 50 spawns, 50 crossings of the skill proxy and 50 full
    parses of the same array. One call pays each once.

    A single member keeps the historical scalar `contains: bool`, because
    prompts and callers already read that shape; two or more return a map so
    the answers stay attached to their members. `batched` says which shape you
    got — a caller building the member list from a variable-length collection
    would otherwise have to guess, and gets the scalar exactly when the batch
    happens to hold one item.
    """
    user_id = _user_id()
    with _get_conn() as conn:
        members, _ = _load_set(conn, user_id, args.namespace, args.key)
    try:
        present = set(members)
    except TypeError:
        # The set ops document plain-string members, but `kv set` will store
        # any JSON array. A raw TypeError traceback would break the envelope
        # contract every other verb here keeps.
        _fail(
            f"value at {args.namespace}/{args.key} has non-string members; "
            "the set ops need an array of strings"
        )

    if len(args.members) == 1:
        print(json.dumps({
            "status": "ok",
            "contains": args.members[0] in present,
            "batched": False,
        }))
        return

    # dict.fromkeys keeps first-seen order while collapsing duplicates.
    result = {m: m in present for m in dict.fromkeys(args.members)}
    hits = sum(1 for v in result.values() if v)
    print(json.dumps({
        "status": "ok",
        "contains": result,
        "batched": True,
        "present": hits,
        "missing": len(result) - hits,
    }))


def cmd_set_size(args):
    user_id = _user_id()
    with _get_conn() as conn:
        members, _ = _load_set(conn, user_id, args.namespace, args.key)
    print(json.dumps({"status": "ok", "size": len(members)}))


def cmd_set_members(args):
    user_id = _user_id()
    with _get_conn() as conn:
        members, _ = _load_set(conn, user_id, args.namespace, args.key)
    offset = max(0, args.offset)
    limit = max(0, args.limit)
    page = members[offset:offset + limit]
    print(json.dumps({
        "status": "ok",
        "total": len(members),
        "offset": offset,
        "members": page,
    }))


def cmd_set_add(args):
    from istota import db

    user_id = _user_id()
    # Read current state to validate set shape and report an `added` count
    # reflecting the read-time view (deferred apply may see a fresher state).
    with _get_conn() as conn:
        current, _ = _load_set(conn, user_id, args.namespace, args.key)
    existing = set(current)
    added = 0
    for m in args.members:
        if m not in existing:
            existing.add(m)
            added += 1

    if _defer_op({
        "op": "set-add",
        "namespace": args.namespace,
        "key": args.key,
        "members": list(args.members),
    }):
        print(json.dumps({"status": "ok", "added": added, "deferred": True}))
        return

    new_members = list(current)
    seen = set(current)
    for m in args.members:
        if m not in seen:
            new_members.append(m)
            seen.add(m)
    with _get_conn() as conn:
        db.kv_set(conn, user_id, args.namespace, args.key, json.dumps(new_members))
    print(json.dumps({"status": "ok", "added": added}))


def cmd_set_remove(args):
    from istota import db

    user_id = _user_id()
    with _get_conn() as conn:
        current, _ = _load_set(conn, user_id, args.namespace, args.key)
    to_remove = set(args.members)
    removed = sum(1 for m in current if m in to_remove)

    if _defer_op({
        "op": "set-remove",
        "namespace": args.namespace,
        "key": args.key,
        "members": list(args.members),
    }):
        print(json.dumps({"status": "ok", "removed": removed, "deferred": True}))
        return

    new_members = [m for m in current if m not in to_remove]
    with _get_conn() as conn:
        db.kv_set(conn, user_id, args.namespace, args.key, json.dumps(new_members))
    print(json.dumps({"status": "ok", "removed": removed}))


def cmd_set_trim(args):
    """Keep the newest N members, dropping from the front.

    Trimming by *count* rather than by age is deliberate. The store has no TTL,
    `updated_at` is per-key rather than per-member, and a JSON array of bare
    strings carries no age information — anything date-based would need the
    caller to store timestamps alongside. What the array does carry reliably is
    insertion order: `set-add` appends (here and in the deferred replay), and
    `set-remove` preserves relative order, so oldest-first holds and "newest N"
    is well defined.
    """
    from istota import db

    if args.keep_newest < 0:
        _fail("--keep-newest must be >= 0")

    user_id = _user_id()
    with _get_conn() as conn:
        current, exists = _load_set(conn, user_id, args.namespace, args.key)

    # `current[len(current) - keep:]` is wrong: when keep > len the start index
    # goes negative and Python clamps it at -len, so a keep between len and
    # 2*len silently drops members (keep=4 on 3 members kept only the last one).
    # A plain negative index has no such window.
    kept = current[-args.keep_newest:] if args.keep_newest else []
    removed = len(current) - len(kept)

    # Queue on the read-time view like set-add/set-remove, *without* consulting
    # `exists` — a set-add queued earlier in this same task creates the key at
    # apply time, and short-circuiting here would silently drop the trim on the
    # create-then-cap run the docs recommend. The replay re-reads and skips a
    # genuinely absent key itself.
    if _defer_op({
        "op": "set-trim",
        "namespace": args.namespace,
        "key": args.key,
        "keep_newest": args.keep_newest,
    }):
        print(json.dumps({
            "status": "ok", "removed": removed, "size": len(kept), "deferred": True,
        }))
        return

    if not exists:
        # Direct path: nothing to trim, and trimming shouldn't conjure the key.
        print(json.dumps({"status": "ok", "removed": 0, "size": 0}))
        return

    with _get_conn() as conn:
        db.kv_set(conn, user_id, args.namespace, args.key, json.dumps(kept))
    print(json.dumps({"status": "ok", "removed": removed, "size": len(kept)}))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.kv",
        description="Key-value store for persistent runtime state",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    _shared_help = "Use the cross-user shared_kv store (writes admin-only)"

    p_get = sub.add_parser("get", help="Get a value")
    p_get.add_argument("namespace")
    p_get.add_argument("key")
    p_get.add_argument("--shared", action="store_true", help=_shared_help)

    p_set = sub.add_parser("set", help="Set a value (JSON)")
    p_set.add_argument("namespace")
    p_set.add_argument("key")
    p_set.add_argument("value", nargs="?", help="JSON value (max 128 KiB as an argument)")
    p_set.add_argument(
        "--value-file",
        help="Read the JSON value from this file instead of the argument, for "
             "values above the 128 KiB argv cap. Must live under "
             "$ISTOTA_DEFERRED_DIR or your workspace",
    )
    p_set.add_argument("--shared", action="store_true", help=_shared_help)

    p_del = sub.add_parser("delete", help="Delete a key")
    p_del.add_argument("namespace")
    p_del.add_argument("key")
    p_del.add_argument("--shared", action="store_true", help=_shared_help)

    p_list = sub.add_parser("list", help="List keys in a namespace")
    p_list.add_argument("namespace")
    p_list.add_argument(
        "--keys-only", action="store_true",
        help="Return keys and value sizes without the values themselves",
    )
    p_list.add_argument(
        "--max-value-chars", type=int, default=DEFAULT_MAX_VALUE_CHARS,
        help=f"Truncate each value to N characters (default {DEFAULT_MAX_VALUE_CHARS}; "
             "0 returns them whole)",
    )
    p_list.add_argument("--shared", action="store_true", help=_shared_help)

    p_ns = sub.add_parser("namespaces", help="List all namespaces")
    p_ns.add_argument("--shared", action="store_true", help=_shared_help)

    sub.add_parser(
        "shared-status",
        help="Report whether you may write shared_kv (admin-only, deployment-specific)",
    )

    # Set-ops accept --shared only so we can reject it with a clean JSON error
    # (curated shared content is whole-value writes, not incremental set
    # membership). Without the flag argparse would exit 2 with a stderr message.
    p_contains = sub.add_parser(
        "set-contains",
        help="Check one or more string members against the JSON-array value "
             "at <ns>/<key>",
    )
    p_contains.add_argument("namespace")
    p_contains.add_argument("key")
    p_contains.add_argument("members", nargs="+")
    p_contains.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    p_size = sub.add_parser(
        "set-size",
        help="Return the number of members in the JSON-array value at <ns>/<key>",
    )
    p_size.add_argument("namespace")
    p_size.add_argument("key")
    p_size.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    p_members = sub.add_parser(
        "set-members",
        help="Return a paginated slice of members in the JSON-array value at <ns>/<key>",
    )
    p_members.add_argument("namespace")
    p_members.add_argument("key")
    p_members.add_argument("--limit", type=int, default=100)
    p_members.add_argument("--offset", type=int, default=0)
    p_members.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    p_add = sub.add_parser(
        "set-add",
        help="Add one or more string members to the JSON-array value at <ns>/<key>",
    )
    p_add.add_argument("namespace")
    p_add.add_argument("key")
    p_add.add_argument("members", nargs="+")
    p_add.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    p_remove = sub.add_parser(
        "set-remove",
        help="Remove one or more string members from the JSON-array value at <ns>/<key>",
    )
    p_remove.add_argument("namespace")
    p_remove.add_argument("key")
    p_remove.add_argument("members", nargs="+")
    p_remove.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    p_trim = sub.add_parser(
        "set-trim",
        help="Keep only the newest N members of the JSON-array value at "
             "<ns>/<key>, dropping from the front",
    )
    p_trim.add_argument("namespace")
    p_trim.add_argument("key")
    p_trim.add_argument(
        "--keep-newest", type=int, required=True,
        help="Number of members to keep, counting from the end",
    )
    p_trim.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    return parser


_SET_OPS = frozenset({
    "set-contains", "set-size", "set-members", "set-add", "set-remove",
    "set-trim",
})


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    namespace = getattr(args, "namespace", None)
    if is_reserved_namespace(namespace):
        _fail(
            f"namespace {namespace!r} is reserved for framework state "
            f"and cannot be read or written through this CLI",
            namespace=namespace,
        )
    if args.command in _SET_OPS and getattr(args, "shared", False):
        _fail(
            "--shared is not supported for set-ops "
            "(shared scope is whole-value only)"
        )
    commands = {
        "get": cmd_get,
        "set": cmd_set,
        "delete": cmd_delete,
        "list": cmd_list,
        "namespaces": cmd_namespaces,
        "shared-status": cmd_shared_status,
        "set-contains": cmd_set_contains,
        "set-size": cmd_set_size,
        "set-members": cmd_set_members,
        "set-add": cmd_set_add,
        "set-remove": cmd_set_remove,
        "set-trim": cmd_set_trim,
    }
    # Every handler prints its own envelope through `_fail` or a direct
    # `print`, so the epilogue's job here is the exception envelope alone.
    run_skill_cli(commands, args, handlers_print=True)


if __name__ == "__main__":
    main()
