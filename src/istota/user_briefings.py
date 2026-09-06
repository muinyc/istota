"""User briefing store (Phase 7b of the Docker onboarding spec).

Per-user briefings (cron-scheduled summaries delivered to a Talk room or
email) live in the ``briefing_configs`` table. This replaces the
``[[briefings]]`` blocks that used to live in per-user TOML files.

Resolution order at config-load time:
    1. ``briefing_configs`` table        (web UI / ``istota briefing ensure``)
    2. ``[[briefings]]`` in TOML         (operator-managed; fallback)

Briefings stored in the DB are merged into ``UserConfig.briefings`` by
``_apply_user_briefings`` at the tail of ``load_config``. The runtime read
path (``get_briefings_for_user`` in ``skills/briefing``) returns that result
as it stands — there is no third layer on top of it.

The DB row, when present, replaces a TOML row of the same name. New
briefings only present in the DB simply add to the user's list.

There used to be a third source: a workspace ``BRIEFINGS.md``, applied over
both of the above at read time. It is retired, because it beat the row the
settings page had just written while that page went on showing the value the
user had chosen. ``import_from_workspace_files`` at the bottom of this module
carries what those files held into the table, once.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import sqlite_util
from .toml_fence import find_toml_block

logger = logging.getLogger(__name__)

# The fenced TOML block in a workspace ``BRIEFINGS.md``. Lifted from the
# retired ``skills.briefing._load_workspace_briefings``; the import below is
# the only thing that reads that shape now. Where the fence starts and ends
# is `toml_fence`'s to say (ISSUE-386): the expression that used to live here
# anchored neither marker, so a backtick run anywhere after the fence opened
# ended the block early and dropped every entry below it.

# One sentinel per user, in ``_migration_state``. Per user rather than
# per deployment because a user whose mount was unreadable on the boot that
# ran the migration would otherwise never be looked at again, and their
# schedule lives only in that file.
_IMPORT_SENTINEL = "briefings_md_import_v1"


@dataclass
class UserBriefing:
    """A briefing config row.

    Mirrors :class:`istota.config.BriefingConfig` plus the DB-only
    ``id``/``user_id``/``enabled`` columns.
    """

    id: int
    user_id: str
    name: str
    cron: str
    # Display title; blank = derive from ``name`` at render time.
    title: str = ""
    conversation_token: str = ""
    output: str = "talk"
    components: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with 30s timeout, matching db.get_db semantics."""
    with sqlite_util.open_db(
        db_path, busy_timeout_ms=None, foreign_keys=False, commit=True,
    ) as conn:
        yield conn


def _decode_components(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("briefing_configs.components contained invalid JSON; defaulting to {}")
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _row_key(row: sqlite3.Row, key: str) -> Any:
    """Read a column that may be absent on a not-yet-migrated row."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _row_to_briefing(row: sqlite3.Row) -> UserBriefing:
    # The DB column is named ``cron_expression`` for legacy reasons; the
    # in-memory ``BriefingConfig`` uses ``cron`` (matches the TOML key).
    components = _decode_components(row["components"])
    # ``output`` is a real column now. Defensively fall back to a legacy
    # ``__output__`` component key when reading a mid-migration row whose
    # column read yields the default while the key is still present.
    raw_output = _row_key(row, "output")
    output = raw_output if isinstance(raw_output, str) and raw_output.strip() else "talk"
    legacy_output = components.pop("__output__", None)
    if output == "talk" and isinstance(legacy_output, str) and legacy_output.strip():
        output = legacy_output
    raw_title = _row_key(row, "title")
    return UserBriefing(
        id=int(row["id"]),
        user_id=row["user_id"],
        name=row["name"],
        cron=row["cron_expression"] or "",
        title=raw_title if isinstance(raw_title, str) else "",
        conversation_token=row["conversation_token"] or "",
        output=output,
        components=components,
        enabled=bool(row["enabled"]),
    )


def list_briefings(db_path: Path, user_id: str | None = None) -> list[UserBriefing]:
    """Return briefing rows. When ``user_id`` is set, scope to that user."""
    if not Path(db_path).exists():
        return []
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM briefing_configs WHERE user_id = ? ORDER BY name",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM briefing_configs ORDER BY user_id, name"
            ).fetchall()
    return [_row_to_briefing(r) for r in rows]


def get_briefing(db_path: Path, user_id: str, name: str) -> UserBriefing | None:
    """Return a single briefing or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM briefing_configs WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
    return _row_to_briefing(row) if row else None


def ensure_briefing(
    db_path: Path,
    *,
    user_id: str,
    name: str,
    cron: str,
    title: str = "",
    conversation_token: str = "",
    output: str = "talk",
    components: dict[str, Any] | None = None,
    enabled: bool = True,
) -> tuple[UserBriefing, str]:
    """Idempotent upsert. Returns ``(briefing, state)``.

    ``state`` is one of ``"created"``, ``"updated"``, ``"noop"`` — same
    contract as ``istota user ensure`` / ``istota resource ensure``.
    """
    if not name:
        raise ValueError("briefing name cannot be empty")
    if not cron:
        raise ValueError("briefing cron cannot be empty")
    # Accept any output_target descriptor (talk/email/both/all/ntfy/talk:<tok>/
    # comma lists). Unknown surfaces are warn-and-dropped at delivery, not here.
    from .transport import parse_output_target
    if not parse_output_target(output):
        raise ValueError(
            f"briefing output must be a valid delivery descriptor, got {output!r}"
        )

    components = dict(components or {})
    components_json = json.dumps(components, sort_keys=True)
    enabled_int = 1 if enabled else 0
    title = title or ""

    existing = get_briefing(db_path, user_id, name)
    if existing is None:
        state = "created"
    else:
        same = (
            existing.cron == cron
            and existing.title == title
            and existing.conversation_token == (conversation_token or "")
            and existing.output == output
            and existing.components == components
            and existing.enabled == enabled
        )
        state = "noop" if same else "updated"

    if state == "noop":
        return existing, state  # type: ignore[return-value]

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO briefing_configs
                (user_id, name, cron_expression, title, conversation_token,
                 components, output, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, name) DO UPDATE SET
                cron_expression = excluded.cron_expression,
                title = excluded.title,
                conversation_token = excluded.conversation_token,
                components = excluded.components,
                output = excluded.output,
                enabled = excluded.enabled
            """,
            (user_id, name, cron, title, conversation_token or "",
             components_json, output, enabled_int),
        )

    fresh = get_briefing(db_path, user_id, name)
    assert fresh is not None
    return fresh, state


def delete_briefing(db_path: Path, user_id: str, name: str) -> bool:
    """Remove a briefing by (user_id, name). Returns True if a row was removed."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM briefing_configs WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        return cur.rowcount > 0


def delete_briefing_by_id(db_path: Path, user_id: str, briefing_id: int) -> bool:
    """Delete a briefing by id, scoped to user_id (web UI safety).

    The user_id scope prevents one user from deleting another user's
    briefing by guessing IDs from the URL. Returns True if a row was
    removed.
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM briefing_configs WHERE id = ? AND user_id = ?",
            (briefing_id, user_id),
        )
        return cur.rowcount > 0


# --- Migration: TOML → DB --------------------------------------------------

def import_from_user_configs(
    db_path: Path,
    user_configs: "dict[str, object]",
) -> int:
    """Seed ``briefing_configs`` rows from loaded TOML user configs.

    Walks ``user_config.briefings`` for each user; inserts any
    ``(user_id, name)`` pair that doesn't already have a DB row.
    Idempotent across restarts.
    """
    if not Path(db_path).exists():
        return 0

    written = 0
    with _connect(db_path) as conn:
        existing_keys = {
            (r["user_id"], r["name"])
            for r in conn.execute(
                "SELECT user_id, name FROM briefing_configs"
            ).fetchall()
        }

        for user_id, user_config in user_configs.items():
            briefings = getattr(user_config, "briefings", None) or []
            for b in briefings:
                key = (user_id, getattr(b, "name", ""))
                if not key[1] or key in existing_keys:
                    continue

                cron = getattr(b, "cron", "") or ""
                if not cron:
                    continue

                output = getattr(b, "output", "talk") or "talk"
                title = getattr(b, "title", "") or ""
                token = getattr(b, "conversation_token", "") or ""
                comps = dict(getattr(b, "components", {}) or {})

                try:
                    conn.execute(
                        """
                        INSERT INTO briefing_configs
                            (user_id, name, cron_expression, title,
                             conversation_token, components, output, enabled)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            user_id,
                            key[1],
                            cron,
                            title,
                            token,
                            json.dumps(comps, sort_keys=True),
                            output,
                        ),
                    )
                    written += 1
                    logger.info(
                        "briefing imported from TOML user=%s name=%s",
                        user_id, key[1],
                    )
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning(
                        "briefing import failed user=%s name=%s: %s",
                        user_id, key[1], e,
                    )

    if written:
        logger.info("user_briefings migration: wrote %d new row(s) from TOML", written)
    return written


def _sentinel_name(user_id: str) -> str:
    return f"{_IMPORT_SENTINEL}:{user_id}"


def _sentinel_seen(conn: sqlite3.Connection, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM _migration_state WHERE name = ?",
        (_sentinel_name(user_id),),
    ).fetchone()
    return row is not None


def _sentinel_set(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO _migration_state (name) VALUES (?)",
        (_sentinel_name(user_id),),
    )


def parse_briefings_md(text: str) -> list[dict] | None:
    """The ``[[briefings]]`` entries in a workspace ``BRIEFINGS.md``.

    ``None`` means the file could not be understood — unparseable TOML, or a
    shape that is not a list of tables (``[briefings]`` written as one table
    rather than ``[[briefings]]`` lands here). An empty list means it was read
    fine and named no briefings, which is what the shipped (fully commented
    out) seed produced. The caller treats those two differently: only the
    second is grounds for marking a user done.
    """
    import tomli

    span = find_toml_block(text)
    if span is None:
        return []
    try:
        data = tomli.loads(text[span[0]:span[1]])
    except Exception:
        return None

    entries = data.get("briefings", [])
    if not isinstance(entries, list):
        return None
    return [e for e in entries if isinstance(e, dict)]


def _workspace_is_live(config: "Any", mount: Path, user_id: str, bot_dir: str) -> bool:
    """Positive evidence that this user's workspace is really readable.

    An absent ``BRIEFINGS.md`` means one of two very different things, and the
    sentinel must only be burned for the first: the user genuinely had no file,
    or the tree it lives in is not there to read. An rclone mount that has not
    come up leaves a plain empty directory behind — every path check reports it
    as fine and every read returns nothing — and ``ensure_user_directories_v2``
    runs earlier in the same boot and will happily create the workspace on the
    underlying disk, so even the directory's existence is not enough on its own.

    ``ismount`` is the cheap discriminator, and it is gated on
    ``storage_is_nextcloud`` for the same reason ``doctor.check_mount_liveness``
    gates it: the local single-user install points ``nextcloud_mount_path`` at a
    plain directory under the user's home that nothing ever mounts, so requiring
    a mount point there would mean the import never converged.
    """
    try:
        if getattr(config, "storage_is_nextcloud", False) and not os.path.ismount(mount):
            return False
        from .storage import get_user_config_path
        return (mount / get_user_config_path(user_id, bot_dir).lstrip("/")).is_dir()
    except OSError:
        return False


@dataclass
class _PendingImport:
    """One user's file, read off the mount before any DB lock is taken."""

    user_id: str
    entries: list[dict]
    mark_done: bool


def _read_one_user(
    config: "Any", mount: Path, bot_dir: str, user_id: str,
) -> _PendingImport | None:
    """Read and parse one user's file. ``None`` means do nothing for them now.

    No database handle reaches this function, deliberately — see
    ``import_from_workspace_files`` for why.
    """
    from .storage import get_user_briefings_path

    path = mount / get_user_briefings_path(user_id, bot_dir).lstrip("/")
    try:
        present = path.exists()
    except OSError as e:
        logger.warning("BRIEFINGS.md unreadable for %s: %s", user_id, e)
        return None

    if not present:
        if not _workspace_is_live(config, mount, user_id, bot_dir):
            # The tree is not there to read. Saying "no file" here would burn
            # the sentinel and lose a schedule that exists only in that file.
            logger.warning(
                "BRIEFINGS.md: %s's workspace is not readable; leaving the "
                "import for a later start", user_id,
            )
            return None
        return _PendingImport(user_id=user_id, entries=[], mark_done=True)

    try:
        # `errors="replace"` rather than the locale default: a container with
        # LANG unset decodes as ASCII, and UnicodeDecodeError is a ValueError,
        # so a single non-ASCII character in the user's own prose above the
        # fence would fail every boot for ever without ever being imported.
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        # Transient fact about this boot; leave the sentinel unset and retry.
        logger.warning("BRIEFINGS.md unreadable for %s: %s", user_id, e)
        return None

    entries = parse_briefings_md(text)
    if entries is None:
        logger.warning(
            "BRIEFINGS.md for %s could not be parsed; leaving it for the next "
            "start rather than dropping what it holds", user_id,
        )
        return None

    return _PendingImport(user_id=user_id, entries=entries, mark_done=True)


def _row_from_entry(entry: dict, user_id: str) -> tuple[str, str, str, str] | None:
    """Validate one file entry into row values, or reject it.

    Types are checked rather than coerced. ``str()`` on a TOML array writes its
    Python repr into a column — ``"['talk', 'email']"`` as an output descriptor
    is a briefing that delivers nowhere, recorded permanently — and this file is
    writable by the user and by the model in that user's sandbox.
    """
    name = entry.get("name")
    cron = entry.get("cron")
    if not isinstance(name, str) or not name.strip():
        logger.warning(
            "BRIEFINGS.md entry for %s has no usable name; skipped", user_id,
        )
        return None
    name = name.strip()
    if not isinstance(cron, str) or not cron.strip():
        # This used to *suppress* the briefing: the old loader built an entry
        # with an empty cron, it replaced the config one by name, and
        # `check_briefings` skips a briefing with no cron. Skipping the entry
        # here leaves the existing row's own cron in place, so a briefing the
        # user had switched off this way starts running again. Not inferred as
        # a disable — an absent cron is as likely half-finished as deliberate —
        # but named loudly, because it is the one silent resumption.
        logger.warning(
            "BRIEFINGS.md: %s's entry %r has no cron and was not imported; if "
            "a briefing of that name is configured elsewhere it will now run",
            user_id, name,
        )
        return None

    token = entry.get("conversation_token", "")
    if not isinstance(token, str):
        logger.warning(
            "BRIEFINGS.md: %s's entry %r has a non-string conversation_token; "
            "dropped", user_id, name,
        )
        token = ""

    output = entry.get("output", "talk")
    if not isinstance(output, str) or not _output_is_deliverable(output):
        # `ensure_briefing` refuses an output descriptor that resolves to no
        # destination, so storing one here writes a row the web UI cannot
        # re-save. "none" and whitespace both resolve to nothing.
        logger.warning(
            "BRIEFINGS.md: %s's entry %r has an unusable output %r; "
            "importing as talk", user_id, name, output,
        )
        output = "talk"

    return name, cron.strip(), token, output


def _output_is_deliverable(output: str) -> bool:
    try:
        from .transport import parse_output_target
        return bool(parse_output_target(output))
    except Exception:  # pragma: no cover - defensive
        return bool(output.strip())


def import_from_workspace_files(db_path: Path, config: "Any") -> int:
    """One-shot: carry each user's ``BRIEFINGS.md`` into ``briefing_configs``.

    ``BRIEFINGS.md`` is retired as an input. Until this ran, the file sat on
    top of everything — ``get_briefings_for_user`` applied it over
    ``UserConfig.briefings``, which the DB overlay had already written into —
    so it was the live authority for ``cron``, ``conversation_token`` and
    ``output``.

    Those three are what the file wins here, over an existing row, exactly
    once, because the file's value is what was actually running and a
    retirement should not change the schedule as it lands. **Two columns are
    deliberately left with the row, and both are behaviour changes at the
    switch rather than exceptions to the rule.** ``enabled``: a row disabled in
    the web UI is dropped from ``UserConfig.briefings`` by
    ``config._apply_user_briefings``, and the old file merge then added the
    briefing straight back, so a briefing the user had switched off kept
    running — re-enabling it here would make that bug durable instead of
    ending it. ``title``: the file's entry replaced the row wholesale with a
    blank title, so a title typed into the web UI was being ignored at render
    time; keeping the row's is the same correction. After the sentinel is set
    the table is the only authority and a later edit stands.

    ``components`` is not carried over either. The read path has discarded the
    file's since blocks became the content model, so importing them would feed
    ``briefings/_migrate.py`` and hand the user content they have not had for
    releases. An existing row's components are left alone, which is a narrower
    claim than "components are not imported": ``_apply_user_briefings``
    reattaches them, so a briefing whose module DB has not yet been initialised
    can still seed blocks from them.

    **Every file is read before the connection is opened.** The obvious shape —
    one connection wrapping the loop — takes a write lock on the framework DB
    at the first user and holds it across every remaining user's stat and read
    against the rclone mount, on the daemon's foreground boot path. This file's
    sibling ``import_from_user_configs`` uses that shape safely because it does
    no I/O inside it; ``scheduler.run_daemon`` moved the startup doctor onto a
    thread for exactly this reason, a hung FUSE mount blocking uninterruptibly.

    Best-effort and never raises: this runs on the scheduler's boot path.
    Returns the number of rows written.
    """
    if config is None or not Path(db_path).exists():
        return 0

    mount = getattr(config, "nextcloud_mount_path", None)
    if not getattr(config, "use_mount", False) or mount is None:
        return 0

    bot_dir = getattr(config, "bot_dir_name", "istota")
    user_ids = list(getattr(config, "users", {}) or {})
    if not user_ids:
        return 0

    # 1. Which users are still outstanding. A short read, no write lock.
    try:
        with _connect(db_path) as conn:
            pending_ids = [u for u in user_ids if not _sentinel_seen(conn, u)]
    except Exception as e:  # noqa: BLE001
        logger.warning("BRIEFINGS.md import skipped: %s", e)
        return 0

    if not pending_ids:
        return 0

    # 2. Read the mount with no database handle held.
    reads: list[_PendingImport] = []
    for user_id in pending_ids:
        try:
            pending = _read_one_user(config, Path(mount), bot_dir, user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("BRIEFINGS.md read failed user=%s: %s", user_id, e)
            continue
        if pending is not None:
            reads.append(pending)

    if not reads:
        return 0

    # 3. Write. Each user gets a savepoint, so a failure part-way through one
    #    leaves no rows behind to disagree with the sentinel it never set.
    written = 0
    try:
        with _connect(db_path) as conn:
            for pending in reads:
                try:
                    conn.execute("SAVEPOINT briefings_md_import")
                    written += _write_one_user(conn, pending)
                    conn.execute("RELEASE briefings_md_import")
                except Exception as e:  # noqa: BLE001
                    conn.execute("ROLLBACK TO briefings_md_import")
                    conn.execute("RELEASE briefings_md_import")
                    logger.warning(
                        "BRIEFINGS.md import failed user=%s: %s", pending.user_id, e,
                    )
    except Exception as e:  # noqa: BLE001
        logger.warning("BRIEFINGS.md import skipped: %s", e)
        return 0

    if written:
        logger.info(
            "BRIEFINGS.md retirement: imported %d briefing(s) into briefing_configs",
            written,
        )
    return written


def _write_one_user(conn: sqlite3.Connection, pending: _PendingImport) -> int:
    user_id = pending.user_id
    written = 0
    for entry in pending.entries:
        row = _row_from_entry(entry, user_id)
        if row is None:
            continue
        name, cron, token, output = row
        conn.execute(
            """
            INSERT INTO briefing_configs
                (user_id, name, cron_expression, title, conversation_token,
                 components, output, enabled)
            VALUES (?, ?, ?, '', ?, '{}', ?, 1)
            ON CONFLICT (user_id, name) DO UPDATE SET
                cron_expression = excluded.cron_expression,
                conversation_token = excluded.conversation_token,
                output = excluded.output
            """,
            (user_id, name, cron, token, output),
        )
        written += 1
        # The whole row, not just the name: this promotes model-writable file
        # content into durable framework state, and the audit trail for that is
        # this line.
        logger.info(
            "briefing imported from BRIEFINGS.md user=%s name=%s cron=%r "
            "token=%r output=%r",
            user_id, name, cron, token, output,
        )

    if pending.mark_done:
        _sentinel_set(conn, user_id)
    return written
