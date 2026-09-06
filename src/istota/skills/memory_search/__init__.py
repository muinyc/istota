"""Memory search skill — search conversations and memory files.

CLI:
    python -m istota.skills.memory_search search "query" [--limit 10] [--source-type TYPE]
    python -m istota.skills.memory_search index conversation TASK_ID
    python -m istota.skills.memory_search index file PATH [--source-type TYPE]
    python -m istota.skills.memory_search reindex [--lookback-days 90]
    python -m istota.skills.memory_search stats

Env vars: ISTOTA_DB_PATH, ISTOTA_USER_ID, NEXTCLOUD_MOUNT_PATH
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from istota.skills._cli import run_skill_cli
from istota.user_scope import scoped_user_dir


def _get_conn() -> sqlite3.Connection:
    """Get DB connection from env var."""
    db_path = os.environ.get("ISTOTA_DB_PATH", "")
    if not db_path:
        print(json.dumps({"status": "error", "error": "ISTOTA_DB_PATH not set"}))
        sys.exit(1)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _get_user_id() -> str:
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        print(json.dumps({"status": "error", "error": "ISTOTA_USER_ID not set"}))
        sys.exit(1)
    return user_id


def _get_channel_user_ids() -> list[str] | None:
    """Build include_user_ids from ISTOTA_CONVERSATION_TOKEN env var."""
    token = os.environ.get("ISTOTA_CONVERSATION_TOKEN", "")
    if token:
        return [f"channel:{token}"]
    return None


def cmd_search(args) -> dict:
    """Search memory chunks."""
    from istota.memory.search import search

    conn = _get_conn()
    user_id = _get_user_id()

    source_types = [args.source_type] if args.source_type else None
    include_user_ids = _get_channel_user_ids()
    since = getattr(args, "since", None)
    if not isinstance(since, str):
        since = None
    topics = [args.topic] if getattr(args, "topic", None) else None
    entities_arg = getattr(args, "entity", None)
    entities = [entities_arg] if entities_arg else None
    results = search(conn, user_id, args.query, limit=args.limit, source_types=source_types,
                     include_user_ids=include_user_ids, since=since, topics=topics, entities=entities)
    conn.close()

    return {
        "status": "ok",
        "query": args.query,
        "count": len(results),
        "results": [
            {
                "chunk_id": r.chunk_id,
                "content": r.content,
                "score": round(r.score, 6),
                "source_type": r.source_type,
                "source_id": r.source_id,
                "bm25_rank": r.bm25_rank,
                "vec_rank": r.vec_rank,
            }
            for r in results
        ],
    }


def cmd_index_conversation(args) -> dict:
    """Index a specific conversation by task ID."""
    from istota.memory.search import index_conversation

    conn = _get_conn()
    user_id = _get_user_id()

    row = conn.execute(
        "SELECT prompt, result FROM tasks WHERE id = ? AND user_id = ?",
        (args.task_id, user_id),
    ).fetchone()

    if not row:
        conn.close()
        return {"status": "error", "error": f"Task {args.task_id} not found for user {user_id}"}

    n = index_conversation(conn, user_id, args.task_id, row[0] or "", row[1] or "")
    conn.close()

    return {"status": "ok", "task_id": args.task_id, "chunks_inserted": n}


def _indexable_roots(user_id: str) -> list[Path]:
    """Directories `index file` may read from: this user's own data, only.

    The CLI runs host-side under the skill proxy, so the path argument is
    evaluated with the daemon's filesystem access rather than the sandbox's —
    an unbounded read here would let a task index the config file (or another
    user's workspace) and then retrieve the contents through `search`, walking
    straight past both the sandbox masks and the credential proxy. The scoping
    that makes the other subcommands safe is in their SQL; a filesystem
    argument needs its own.
    """
    roots: list[Path] = []
    mount = os.environ.get("NEXTCLOUD_MOUNT_PATH", "")
    if mount:
        # Scoped, not joined: a `.` or an absolute `ISTOTA_USER_ID` collapses
        # this root to `{mount}/Users`, which is exactly the "another user's
        # workspace" the docstring above says must not be indexable
        # (ISSUE-402). No root rather than a wider one — an empty allowlist
        # refuses everything.
        own = scoped_user_dir(Path(mount) / "Users", user_id)
        if own is not None:
            roots.append(own)
    token = os.environ.get("ISTOTA_CONVERSATION_TOKEN", "")
    if mount and token:
        roots.append(Path(mount) / "Channels" / token)
    deferred = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    if deferred:
        roots.append(Path(deferred))
    return [r.resolve() for r in roots]


def cmd_index_file(args) -> dict:
    """Index a file."""
    from istota.memory.search import index_file

    conn = _get_conn()
    user_id = _get_user_id()

    path = Path(args.path)
    # Resolve before comparing: the check is worthless against `../` or a
    # symlink planted in the workspace otherwise.
    resolved = path.resolve()
    roots = _indexable_roots(user_id)
    if not roots or not any(
        resolved == root or resolved.is_relative_to(root) for root in roots
    ):
        conn.close()
        return {
            "status": "error",
            "error": (
                f"Refusing to index {path}: it is outside your own workspace. "
                "index file reads with the daemon's filesystem access, so it is "
                "restricted to your user directory, the current channel "
                "directory, and the task's working directory."
            ),
        }
    if not resolved.is_file():
        conn.close()
        return {"status": "error", "error": f"File not found: {path}"}

    content = resolved.read_text()
    source_type = args.source_type or "memory_file"
    n = index_file(conn, user_id, str(path), content, source_type)
    conn.close()

    return {"status": "ok", "path": str(path), "source_type": source_type, "chunks_inserted": n}


def cmd_reindex(args) -> dict:
    """Reindex all conversations and memory files.

    The config is the **real** one, not a `SimpleNamespace` carrying the mount
    alone. `reindex_all` reads `bot_dir_name` to reach `config/USER.md`, and
    `skills_dir` / `bundled_skills_dir` to know which per-skill overlays bind,
    so a stand-in built from one env var raised `AttributeError` before the
    first of those blocks ran — every `reindex` against a mounted deployment
    died there, reported as a traceback with no JSON envelope. This verb is a
    bulk reindex, so a TOML parse is not a cost worth avoiding; the rest of
    this CLI reads env vars because it runs per search.
    """
    from istota.config import load_config
    from istota.memory.search import reindex_all

    conn = _get_conn()
    user_id = _get_user_id()

    try:
        config = load_config()
    except Exception as e:  # noqa: BLE001 - the envelope contract is the point
        conn.close()
        return {"status": "error", "error": f"could not load config: {e}"}

    stats = reindex_all(conn, config, user_id, lookback_days=args.lookback_days)
    conn.close()

    return {"status": "ok", **stats}


def cmd_stats(args) -> dict:
    """Get memory search stats."""
    from istota.memory.search import get_stats
    from istota.memory.knowledge_graph import ensure_table, get_fact_count

    conn = _get_conn()
    user_id = _get_user_id()

    include_user_ids = _get_channel_user_ids()
    stats = get_stats(conn, user_id, include_user_ids=include_user_ids)

    ensure_table(conn)
    fact_counts = get_fact_count(conn, user_id)
    stats["knowledge_facts"] = fact_counts

    # USER.md size — phase A observability for #3 (USER.md growth). Reported
    # in bytes so growth curves over time are easy to plot. None when the
    # file doesn't exist yet, which is distinct from 0 bytes.
    user_md_size = _user_md_size_bytes(user_id)
    stats["user_md_size_bytes"] = user_md_size

    conn.close()

    return {"status": "ok", **stats}


def _user_md_size_bytes(user_id: str) -> int | None:
    """Return USER.md size in bytes via the mount, or None if unavailable.

    Prefers ``ISTOTA_BOT_DIR_NAME`` when set. Otherwise falls back to a
    sole-candidate match — refuses to guess when multiple bot dirs exist
    (ISSUE-077: would otherwise silently report the wrong file's size).
    """
    mount = os.environ.get("NEXTCLOUD_MOUNT_PATH")
    if not mount:
        return None
    base = os.path.join(mount, "Users", user_id)
    bot = os.environ.get("ISTOTA_BOT_DIR_NAME", "")
    if bot:
        config_md = os.path.join(base, bot, "config", "USER.md")
        if os.path.isfile(config_md):
            try:
                return os.path.getsize(config_md)
            except OSError:
                return None
        return None
    if not os.path.isdir(base):
        return None
    candidates = [
        d for d in sorted(os.listdir(base))
        if os.path.isfile(os.path.join(base, d, "config", "USER.md"))
    ]
    if len(candidates) != 1:
        return None
    config_md = os.path.join(base, candidates[0], "config", "USER.md")
    try:
        return os.path.getsize(config_md)
    except OSError:
        return None


def cmd_facts(args) -> dict:
    """Query knowledge graph facts."""
    from istota.memory.knowledge_graph import (
        ensure_table, get_current_facts, get_facts_as_of,
    )

    conn = _get_conn()
    user_id = _get_user_id()
    ensure_table(conn)

    if args.as_of:
        facts = get_facts_as_of(conn, user_id, args.as_of, subject=args.subject)
    else:
        facts = get_current_facts(conn, user_id, subject=args.subject,
                                   predicate=args.predicate)
    conn.close()

    return {
        "status": "ok",
        "count": len(facts),
        "facts": [
            {
                "id": f.id,
                "subject": f.subject,
                "predicate": f.predicate,
                "object": f.object,
                "valid_from": f.valid_from,
                "valid_until": f.valid_until,
                "temporary": f.temporary,
                "source_type": f.source_type,
                "source_task_id": f.source_task_id,
            }
            for f in facts
        ],
    }


def cmd_timeline(args) -> dict:
    """Get entity timeline."""
    from istota.memory.knowledge_graph import ensure_table, get_entity_timeline

    conn = _get_conn()
    user_id = _get_user_id()
    ensure_table(conn)

    facts = get_entity_timeline(conn, user_id, args.subject)
    conn.close()

    return {
        "status": "ok",
        "subject": args.subject,
        "count": len(facts),
        "facts": [
            {
                "id": f.id,
                "predicate": f.predicate,
                "object": f.object,
                "valid_from": f.valid_from,
                "valid_until": f.valid_until,
                "temporary": f.temporary,
                "source_type": f.source_type,
            }
            for f in facts
        ],
    }


def _defer_kg_op(entry: dict) -> bool:
    """Append a deferred knowledge-graph op for the scheduler to process.

    Returns True when the op was queued (sandbox mode, where the DB is
    read-only). Caller falls back to a direct write if False.
    """
    deferred_dir = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    task_id = os.environ.get("ISTOTA_TASK_ID", "")
    if not deferred_dir or not task_id:
        return False

    from pathlib import Path

    path = Path(deferred_dir) / f"task_{task_id}_kg_ops.json"

    ops = []
    if path.exists():
        try:
            ops = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            ops = []
    ops.append(entry)
    path.write_text(json.dumps(ops), encoding="utf-8")
    return True


def cmd_add_fact(args) -> dict:
    """Manually add a fact. Deferred under sandbox; direct write otherwise."""
    entry = {
        "op": "add_fact",
        "subject": args.subject,
        "predicate": args.predicate,
        "object": args.object,
        "valid_from": args.valid_from,
        "source_type": "user_stated",
    }
    if _defer_kg_op(entry):
        return {"status": "ok", "deferred": True}

    from istota.memory.knowledge_graph import ensure_table, add_fact

    conn = _get_conn()
    user_id = _get_user_id()
    ensure_table(conn)

    fact_id = add_fact(
        conn, user_id, args.subject, args.predicate, args.object,
        valid_from=args.valid_from, source_type="user_stated",
    )
    conn.commit()
    conn.close()

    if fact_id is None:
        return {"status": "ok", "message": "Duplicate fact, skipped"}

    return {"status": "ok", "fact_id": fact_id}


def cmd_invalidate_fact(args) -> dict:
    """Invalidate a fact. Deferred under sandbox; direct write otherwise."""
    entry = {
        "op": "invalidate",
        "fact_id": args.fact_id,
        "ended": args.ended,
    }
    if _defer_kg_op(entry):
        return {"status": "ok", "deferred": True}

    from istota.memory.knowledge_graph import ensure_table, invalidate_fact

    conn = _get_conn()
    ensure_table(conn)

    result = invalidate_fact(conn, args.fact_id, ended=args.ended)
    conn.commit()
    conn.close()

    if not result:
        return {"status": "error", "error": f"Fact {args.fact_id} not found or already invalidated"}

    return {"status": "ok", "fact_id": args.fact_id}


def cmd_delete_fact(args) -> dict:
    """Hard delete a fact. Deferred under sandbox; direct write otherwise."""
    entry = {"op": "delete", "fact_id": args.fact_id}
    if _defer_kg_op(entry):
        return {"status": "ok", "deferred": True}

    from istota.memory.knowledge_graph import ensure_table, delete_fact

    conn = _get_conn()
    ensure_table(conn)

    result = delete_fact(conn, args.fact_id)
    conn.commit()
    conn.close()

    if not result:
        return {"status": "error", "error": f"Fact {args.fact_id} not found"}

    return {"status": "ok", "fact_id": args.fact_id}


def cmd_fact_history(args) -> dict:
    """Show audit history for knowledge graph mutations."""
    from istota.memory.knowledge_graph import ensure_table, get_fact_history

    user_id = os.environ.get("ISTOTA_USER_ID", "default")
    conn = _get_conn()
    ensure_table(conn)
    rows = get_fact_history(
        conn, user_id,
        entity=args.entity,
        since=args.since,
        limit=args.limit,
    )
    conn.close()
    return {
        "status": "ok",
        "user_id": user_id,
        "count": len(rows),
        "rows": [
            {
                "id": r.id,
                "fact_id": r.fact_id,
                "op": r.op,
                "ts": r.ts,
                "source_task_id": r.source_task_id,
                "source_type": r.source_type,
                "before": r.before_json,
                "after": r.after_json,
            }
            for r in rows
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.memory_search",
        description="Memory search skill",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search command
    search_p = sub.add_parser("search", help="Search memory chunks")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    search_p.add_argument("--source-type", help="Filter by source type")
    search_p.add_argument("--since", help="Only results on or after this date (YYYY-MM-DD)")
    search_p.add_argument("--topic", help="Filter by topic (work, tech, personal, finance, admin, learning, meta)")
    search_p.add_argument("--entity", help="Filter by entity name")

    # index command with subcommands
    index_p = sub.add_parser("index", help="Index content")
    index_sub = index_p.add_subparsers(dest="index_command", required=True)

    conv_p = index_sub.add_parser("conversation", help="Index a conversation by task ID")
    conv_p.add_argument("task_id", type=int, help="Task ID to index")

    file_p = index_sub.add_parser("file", help="Index a file")
    file_p.add_argument("path", help="File path")
    file_p.add_argument("--source-type", help="Source type (default: memory_file)")

    # reindex command
    reindex_p = sub.add_parser("reindex", help="Reindex all content")
    reindex_p.add_argument("--lookback-days", type=int, default=90, help="Days to look back (default: 90)")

    # stats command
    sub.add_parser("stats", help="Show memory search stats")

    # facts command (knowledge graph query)
    facts_p = sub.add_parser("facts", help="Query knowledge graph facts")
    facts_p.add_argument("--subject", help="Filter by entity subject")
    facts_p.add_argument("--predicate", help="Filter by predicate type")
    facts_p.add_argument("--as-of", help="Historical query at date (YYYY-MM-DD)")

    # timeline command
    timeline_p = sub.add_parser("timeline", help="Get entity timeline")
    timeline_p.add_argument("subject", help="Entity name")

    # add-fact command
    add_fact_p = sub.add_parser("add-fact", help="Manually add a fact")
    add_fact_p.add_argument("subject", help="Entity subject")
    add_fact_p.add_argument("predicate", help="Relationship predicate")
    add_fact_p.add_argument("object", help="Object value")
    add_fact_p.add_argument("--from", dest="valid_from", help="Valid from date (YYYY-MM-DD)")

    # invalidate command
    inv_p = sub.add_parser("invalidate", help="Mark a fact as ended")
    inv_p.add_argument("fact_id", type=int, help="Fact ID")
    inv_p.add_argument("--ended", help="End date (YYYY-MM-DD, default: today)")

    # delete-fact command
    del_p = sub.add_parser("delete-fact", help="Hard delete a fact")
    del_p.add_argument("fact_id", type=int, help="Fact ID")

    # fact-history command — audit log of KG mutations
    hist_p = sub.add_parser(
        "fact-history",
        help="Show audit history of knowledge graph mutations",
    )
    hist_p.add_argument(
        "--entity",
        help="Filter rows whose snapshot mentions this entity (subject or object)",
    )
    hist_p.add_argument("--since", help="ISO date/datetime; rows on or after this ts")
    hist_p.add_argument("--limit", type=int, default=50, help="Max rows (default: 50)")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "search": cmd_search,
        "index": lambda a: cmd_index_conversation(a) if a.index_command == "conversation" else cmd_index_file(a),
        "reindex": cmd_reindex,
        "stats": cmd_stats,
        "facts": cmd_facts,
        "timeline": cmd_timeline,
        "add-fact": cmd_add_fact,
        "invalidate": cmd_invalidate_fact,
        "delete-fact": cmd_delete_fact,
        "fact-history": cmd_fact_history,
    }

    run_skill_cli(commands, args)


if __name__ == "__main__":
    main()
