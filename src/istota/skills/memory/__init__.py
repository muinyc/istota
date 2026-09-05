"""Memory skill CLI — runtime writes to USER.md / CHANNEL.md.

Single write path through the curation ops engine (`apply_ops`). Used by the
always-included memory skill so durable memory writes don't bypass heading
routing, dedup, or the audit log.

Subcommands:
  append        Append a bullet under an existing `## heading` (optionally
                under one of its `### subheadings` via --subheading).
  add-heading   Add a new `## heading` with one or more bullets.
  remove        Remove a bullet (substring match, must be unique). Reaches
                into subsections.
  replace       Rewrite the single matching bullet in place.
  remove-heading Drop a whole `## ` section.
  remove-subheading Drop a whole `### ` subsection, including prose and
                 numbered items that `remove` cannot reach.
  show          Print USER.md or a CHANNEL.md (or one section of it).
  headings      List the `## ` heading names in order.

Each write subcommand can target the channel memory file by passing
`--channel TOKEN`. The TOKEN is validated against `ISTOTA_CONVERSATION_TOKEN`
when set, to refuse cross-channel writes from a runtime task that's
been scoped to a different conversation.

Both documents live under a directory `build_bwrap_cmd` binds **read-write**
into a sandbox, while this CLI runs host-side and unsandboxed with the daemon's
filesystem view. So neither the path nor the file at the end of it is trusted
(ISSUE-339): the directory is resolved and checked against the user's own root
(or `{mount}/Channels`) before use, and the read refuses a symlink, a FIFO or an
oversized file rather than following it. `_user_md_path` and `_read_text` carry
the reasoning.

Per-skill overlays are **not** written here (ISSUE-343). An overlay is skill
configuration the user authors as an ordinary file, not memory the model
accumulates one bullet at a time, and the bullet-op vocabulary reached about a
fifth of a real one — prose and fenced blocks not at all, since an appendable
line may not contain a newline. `istota-skill skills overlay` / `skills
overlays` read and inventory them; the loader is what enforces whether one
binds.

Env vars used:
  ISTOTA_USER_ID            User whose USER.md is targeted.
  NEXTCLOUD_MOUNT_PATH      Mount root.
  ISTOTA_BOT_DIR_NAME       Bot directory name (e.g. "istota").
  ISTOTA_TASK_ID            Optional, used in audit log entries.
  ISTOTA_CONVERSATION_TOKEN Optional, used to validate --channel.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import NamedTuple

from istota.atomic_write import write_text_atomic
from istota.user_scope import is_scopable_user_id
from istota.memory.curation.audit import (
    write_audit_log,
    write_last_seen,
)
from istota.memory.curation.file_lock import (
    MemoryMdLocked,
    deferred_lock_dir,
    memory_md_lock,
)
from istota.memory.curation.ops import apply_ops
from istota.memory.curation.parser import (
    parse_sectioned_doc,
    serialize_sectioned_doc,
)
from istota.skills._loader import (
    OVERLAY_NOT_UTF8,
    contained_overlay_dir,
    read_overlay_bytes,
)

#: Target kinds. `_resolve_target` used to answer a bool; two destinations
#: with different audit rules is one answer too many for one.
_USER = "user"
_CHANNEL = "channel"

#: Ceiling on what this CLI will read back before editing, matching the
#: daemon's own `storage.USER_CONFIG_READ_CAP_BYTES`.
#:
#: Restated rather than imported: this CLI is spawned per write and
#: deliberately avoids importing `istota.storage`, which pulls in subprocess,
#: shutil and the rclone paths for a process that runs in hundreds of
#: milliseconds. `tests/test_skill_memory_cli.py` holds the two equal — they
#: must be, or the daemon reads a file this CLI cannot edit, or this CLI writes
#: one the daemon will not load.
_MAX_USER_MD_READ_BYTES = 16 * 1024 * 1024

logger = logging.getLogger(__name__)


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("status") == "ok" else 1


def _err(msg: str, **extra) -> int:
    payload = {"status": "error", "error": msg}
    payload.update(extra)
    return _emit(payload)


def _user_id() -> str:
    """The calling task's user id, refused unless it can name a directory.

    Emptiness was the only thing checked, and two joins below build a
    *containment base* from the result — `{mount}/Users/{user_id}` — where a
    `.` collapses that base to `{mount}/Users` and admits every user's overlay
    directory (ISSUE-402). The CLI runs host-side under the skill proxy with
    the daemon's filesystem access, so the base is the boundary.
    """
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _err("ISTOTA_USER_ID not set")
        sys.exit(1)
    if not is_scopable_user_id(user_id):
        _err("ISTOTA_USER_ID does not name a per-user directory")
        sys.exit(1)
    return user_id


def _mount_path() -> Path:
    mount = os.environ.get("NEXTCLOUD_MOUNT_PATH", "")
    if not mount:
        _err("NEXTCLOUD_MOUNT_PATH not set")
        sys.exit(1)
    return Path(mount)


def _bot_dir() -> str:
    bot = os.environ.get("ISTOTA_BOT_DIR_NAME", "")
    if bot:
        return bot
    # Fallback for ad-hoc CLI use only — refuse to guess when more than
    # one bot dir exists (ISSUE-077: silent writes to wrong USER.md under
    # multi-bot tenancy or stale rename leftovers).
    user_id = _user_id()
    base = _mount_path() / "Users" / user_id
    candidates: list[str] = []
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "config" / "USER.md").is_file():
                candidates.append(child.name)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        _err(
            "ISTOTA_BOT_DIR_NAME not set and multiple bot dirs found — refusing to guess",
            user_id=user_id,
            candidates=candidates,
        )
        sys.exit(1)
    _err(
        "ISTOTA_BOT_DIR_NAME not set and could not infer from mount",
        user_id=user_id,
    )
    sys.exit(1)


def _user_config_dir() -> Path:
    return _mount_path() / "Users" / _user_id() / _bot_dir() / "config"


def _user_md_path() -> Path:
    """USER.md under a `config/` proven to be inside the user's own tree.

    `config` is an ordinary entry under `{mount}/Users/{user_id}`, which
    `build_bwrap_cmd` binds **read-write** into that user's sandbox, so `mv
    config config.real && ln -s /anywhere config` is two commands from inside
    it. This CLI runs host-side and unsandboxed, and the link's target is a
    string it resolves in the daemon's own filesystem view — so it need not
    exist in the namespace at all (ISSUE-339).

    The write half is the serious one. `_atomic_write` calls
    `mkdir(parents=True)` and `os.replace`, which between them create the
    missing directory at the far end of the link and put model-chosen content
    in it, as the daemon user. The read half leaks the other way: `show`
    returns whatever is at `<link>/USER.md`.

    Refusing rather than degrading, because a write must not silently land
    somewhere else and a `show` must not answer with a file the user did not
    name. A link that stays *inside* the user's own tree passes: it leads
    nowhere they could not already reach, and refusing it would break someone
    who reorganised their own workspace.

    The **resolved** path comes back so a caller cannot re-walk by the
    unresolved name — the check and the write are separated by a lock
    acquisition and a read.
    """
    d = _user_config_dir()
    resolved = contained_overlay_dir(d, _mount_path() / "Users" / _user_id())
    if resolved is None:
        _err("user_md_outside_user_tree", path=_mount_relative(d))
        sys.exit(1)
    return resolved / "USER.md"


def _channel_md_path(token: str) -> Path:
    if not token or "/" in token or "\\" in token or token.startswith("."):
        _err("invalid channel token", token=token)
        sys.exit(1)
    env_token = os.environ.get("ISTOTA_CONVERSATION_TOKEN", "")
    if not env_token:
        _err(
            "channel write requires ISTOTA_CONVERSATION_TOKEN",
            given=token,
        )
        sys.exit(1)
    if env_token != token:
        _err(
            "channel token mismatch — refusing cross-channel write",
            given=token, expected=env_token,
        )
        sys.exit(1)
    # The token checks above bound the *name*; they say nothing about where the
    # directory it names resolves to, and `{mount}/Channels/{token}` is bound
    # read-write into the sandbox of every task in that room. So containment is
    # checked as well as the token (ISSUE-339) — otherwise `mv` the directory
    # aside, drop a link in its place, and `_atomic_write`'s
    # `mkdir(parents=True)` builds the far end.
    #
    # **Equality, not "under the root"**, which is where this differs from
    # `_user_md_path`. The looser rule there exists so a user who reorganised
    # their own workspace still works, and it is safe because everything under
    # their root is theirs anyway. `Channels/` is bot-managed and holds every
    # room, so "under the root" would let a link at `Channels/{token}` point at
    # *another room's* directory — landing the write on that room's CHANNEL.md
    # and defeating the token equality check six lines above, which exists for
    # exactly that. Nobody has a reason to reorganise this tree.
    channels = _mount_path() / "Channels"
    d = channels / token
    resolved = contained_overlay_dir(d, channels)
    if resolved is None or resolved != Path(os.path.realpath(channels)) / token:
        _err("channel_dir_outside_channel_root", path=_mount_relative(d))
        sys.exit(1)
    return resolved / "CHANNEL.md"


class Target(NamedTuple):
    path: Path
    kind: str


def _resolve_target(args, *, verb: str) -> Target:
    """Resolve the write/read destination from `--channel`."""
    token = getattr(args, "channel", None)
    if token:
        return Target(_channel_md_path(token), _CHANNEL)
    return Target(_user_md_path(), _USER)


def _config_for_audit():
    """Build a minimal Config-like shim for `write_audit_log`/`write_last_seen`.

    Those write to the framework KV store, so the only field they read is
    `db_path`. `ISTOTA_DB_PATH` is set for every skill CLI by the proxy
    (`executor.py`, unconditionally, admin or not), and the file it names is in
    no sandbox at any path.

    Importing the real Config is heavy and pulls in TOML parsing for a CLI that
    runs hundreds of milliseconds end to end, which is why this is a shim
    rather than a load. `use_mount` is False so the shim can never be handed to
    `migrate_user_md_sidecars` and have it try to resolve mount paths that are
    not on it; the migration is the nightly daemon's job, with a real Config.
    """
    db_path = os.environ.get("ISTOTA_DB_PATH", "")

    class _Shim:
        use_mount = False

    _Shim.db_path = Path(db_path) if db_path else None
    return _Shim()


def _read_text(path: Path) -> str:
    """Read USER.md or CHANNEL.md, refusing anything that is not a plain file.

    Through `_loader.read_overlay_bytes`, which is where this hardening already
    lives for the surfaces that read the same tree (ISSUE-339). Both documents
    sit under a directory `build_bwrap_cmd` binds **read-write** into a
    sandbox, while this CLI runs host-side with the daemon's filesystem view,
    so a plain `read_text()` here is an arbitrary daemon-side file read:

    - `O_NOFOLLOW`, because a symlink planted at USER.md otherwise hands back a
      file of the daemon's choosing — which `show` prints, and which the op
      that follows then writes back over.
    - `S_ISREG` behind `O_NONBLOCK`, because a FIFO left at that name blocks
      `open(2)` until someone writes to it, wedging this CLI until the skill
      proxy's timeout kills it.
    - the size checked on the fd before the read, so a multi-gigabyte file
      planted at the path cannot be pulled into memory.

    A missing file is still `""`, because that is how a first write learns to
    create one; it must not read as a refusal.

    The refusal codes are `_loader`'s published `overlay_*` vocabulary, reused
    verbatim rather than forked — the `path` on the envelope is what says which
    document was refused.
    """
    raw, reason, _size = read_overlay_bytes(path, max_bytes=_MAX_USER_MD_READ_BYTES)
    if reason is not None:
        _err(reason, path=_mount_relative(path))
        sys.exit(1)
    assert raw is not None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _err(OVERLAY_NOT_UTF8, path=_mount_relative(path))
        sys.exit(1)


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` with `text` via a uniquely-named staging file.

    The staging name is per-writer rather than `<name>.tmp`, because that fixed
    name is shared with the web save path (`storage.write_channel_memory`) and
    the lock anchor is per-user — so two members of a shared Talk room writing
    the same CHANNEL.md hold different locks and would interleave into one
    staging file, publishing a mixture of both. `os.replace` is atomic; the
    staging is what had to be made unique. UTF-8 is explicit for the same reason
    the readers pin it: the revision tag the web save compares is a UTF-8 hash.

    The mode goes on the descriptor rather than the name, which `atomic_write`
    owns: the staging file is created inside a directory every entry of which
    is model-plantable, and the model is the party that invoked this CLI, so it
    knows when the window between the close and a path-based `chmod` opens.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, text, mode=0o644)


def _mount_relative(path: Path) -> str:
    """`path` as written from the mount root, falling back to the absolute form.

    The audit sidecar sits inside the tree this is relative to, so the absolute
    prefix would be the same on every entry and carry no information — and it
    would restate the deployment's filesystem layout in a file the user reads
    over Nextcloud.
    """
    try:
        return str(path.relative_to(_mount_path()))
    except ValueError:
        return str(path)


def _audit_for(args, op: dict, outcome_or_reason: str, *,
               target: Target, applied: bool) -> None:
    """Write a JSONL audit entry for a runtime CLI write.

    USER.md only. Channel-memory writes are not audited — CHANNEL.md has no
    nightly curator and the audit module only knows about USER.md paths.
    """
    if target.kind == _CHANNEL:
        return
    config = _config_for_audit()
    user_id = _user_id()
    size = None
    extra = None
    # `target.path` **is** the USER.md path, already resolved once by
    # `_resolve_target`. Calling `_user_md_path()` again here would re-walk
    # `config/` after the write has landed and after `_update_last_seen`
    # fingerprinted it — and it exits on a refusal, so a link swapped into that
    # window made a successful, recorded append report an error and exit 1,
    # which the model reads as a failure and retries. It is also a redundant
    # realpath walk on every single write (ISSUE-339).
    #
    # The read goes through the hardened reader like everything else here. A
    # refusal is recorded *as such* rather than silently leaving `size` None,
    # which is what a missing file records — the audit trail feeds
    # `detect_bypass_write`, so "absent" and "unreadable" must not collapse into
    # one value on exactly the files that were tampered with.
    raw, reason, _size = read_overlay_bytes(
        target.path, max_bytes=_MAX_USER_MD_READ_BYTES
    )
    if reason is not None:
        extra = {"user_md_read_refused": reason}
    elif raw:
        size = len(raw)
    entries = [{"op": op, "outcome": outcome_or_reason}] if applied else []
    rejects = [] if applied else [{"op": op, "reason": outcome_or_reason}]
    write_audit_log(
        config, user_id,
        applied=entries,
        rejected=rejects,
        user_md_size_bytes=size,
        source="runtime",
        extra=extra,
    )


def _update_last_seen(path: Path, text: str, target: Target) -> None:
    """Refresh the USER.md fingerprint the nightly bypass detector reads.

    Only for a USER.md write. Stamping it after a channel write would record a
    fingerprint of a file the detector never compares against, masking a real
    out-of-band edit to USER.md itself.
    """
    if target.kind != _USER:
        return
    import hashlib
    write_last_seen(
        _config_for_audit(), _user_id(),
        size_bytes=len(text.encode("utf-8")),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _lock_dir() -> Path | None:
    """Shared anchor dir for the runtime CLI.

    Use the per-user deferred dir (`ISTOTA_DEFERRED_DIR`) so the anchor is the
    same inode whether this CLI runs host-side under the skill proxy or inside
    the bwrap sandbox (the deferred dir is bind-mounted in), matching the
    nightly curator's anchor. Falls back to the system-temp default for ad-hoc
    CLI runs with no task env (no concurrent curator to coordinate with then).
    """
    deferred = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    return deferred_lock_dir(Path(deferred)) if deferred else None


def _do_op(args, op_dict: dict, *, verb: str) -> int:
    target = _resolve_target(args, verb=verb)
    path = target.path
    try:
        with memory_md_lock(path, timeout_seconds=5.0, lock_dir=_lock_dir()):
            current = _read_text(path)
            doc = parse_sectioned_doc(current)
            new_doc, applied, rejected = apply_ops(doc, [op_dict])
            if rejected:
                reason = rejected[0].get("reason", "rejected")
                # For heading-related rejects, surface the existing
                # heading list so the model can self-correct.
                extras = {}
                if reason in ("heading_missing", "heading_exists"):
                    extras["available_headings"] = [s.heading for s in doc.sections]
                _audit_for(args, op_dict, reason, target=target, applied=False)
                return _err(reason, **extras)

            entry = applied[0]
            outcome = entry.get("outcome", "applied")
            if outcome == "applied":
                new_text = serialize_sectioned_doc(new_doc)
                # Refused *before* the write: past the read cap this CLI can no
                # longer read the file back, so the write would leave a document
                # that `show`, `append` and `remove` all refuse — recoverable
                # only from a host shell. Reachable by one oversized `--line` or
                # by enough ordinary appends (ISSUE-339).
                size = len(new_text.encode("utf-8"))
                if size > _MAX_USER_MD_READ_BYTES:
                    _audit_for(args, op_dict, "would_exceed_read_cap",
                               target=target, applied=False)
                    return _err(
                        "would_exceed_read_cap",
                        bytes=size, cap=_MAX_USER_MD_READ_BYTES,
                    )
                _atomic_write(path, new_text)
                _update_last_seen(path, new_text, target)
            _audit_for(args, op_dict, outcome, target=target, applied=True)
            payload = {
                "status": "ok",
                "outcome": outcome,
                "heading": op_dict.get("heading"),
            }
            if "line" in op_dict:
                payload["line"] = op_dict["line"]
            return _emit(payload)
    except MemoryMdLocked:
        return _err("locked", path=str(path))


def _refuse_retired_skill_flag(args) -> None:
    """Answer a `--skill` that no longer does anything, and stop.

    A redirect rather than a silent removal: the caller asked to write an
    overlay, which is now a file edit, and the one useful thing to say is where
    it went. `_err` is the same envelope every other refusal here uses.
    """
    skill = getattr(args, "skill", None)
    if not skill:
        return
    _err(
        "overlay_writes_removed",
        skill=skill,
        hint=(
            "per-skill overlays are ordinary files now — edit "
            "config/skills/<name>.md directly, then run `istota-skill skills "
            "overlays` and check it says binds: true. Read one with "
            "`istota-skill skills overlay <name>`."
        ),
    )
    sys.exit(1)


def _require_heading(args) -> None:
    """Every op must name a `## ` section.

    Enforced here rather than by `required=True` so the refusal is the same
    JSON envelope every other refusal in this CLI is, instead of argparse's
    exit-2 usage dump on stderr — which the model, reading stdout, sees as an
    empty answer.
    """
    if not getattr(args, "heading", None):
        _err(
            "heading_required",
            hint="--heading names a `## ` section of USER.md",
        )
        sys.exit(1)


def cmd_append(args) -> int:
    _refuse_retired_skill_flag(args)
    _require_heading(args)
    op = {"op": "append", "heading": args.heading, "line": args.line}
    subheading = getattr(args, "subheading", None)
    if subheading:
        op["subheading"] = subheading
    return _do_op(args, op, verb="append")


def cmd_add_heading(args) -> int:
    _refuse_retired_skill_flag(args)
    return _do_op(
        args, {"op": "add_heading", "heading": args.heading, "lines": list(args.line)},
        verb="add-heading",
    )


def cmd_remove(args) -> int:
    _refuse_retired_skill_flag(args)
    _require_heading(args)
    return _do_op(args, {"op": "remove", "heading": args.heading, "match": args.match},
                  verb="remove")


def cmd_replace(args) -> int:
    _refuse_retired_skill_flag(args)
    _require_heading(args)
    return _do_op(
        args,
        {"op": "replace", "heading": args.heading, "match": args.match, "line": args.line},
        verb="replace",
    )


def cmd_remove_heading(args) -> int:
    _refuse_retired_skill_flag(args)
    return _do_op(args, {"op": "remove_heading", "heading": args.heading},
                  verb="remove-heading")


def cmd_remove_subheading(args) -> int:
    """Drop a whole `### ` subsection, including content `remove` cannot reach.

    `remove --match` targets bullets, so a subsection built out of prose,
    numbered items or bold sub-headers could not be removed at all — and
    `remove-heading` only drops `## ` sections, which is one level too coarse
    inside USER.md. This is the verb for both.
    """
    _refuse_retired_skill_flag(args)
    _require_heading(args)
    if not getattr(args, "subheading", None):
        _err(
            "subheading_required",
            verb="remove-subheading",
            hint="name the `### ` subsection to drop, e.g. --subheading 'Todo list'",
        )
        sys.exit(1)
    return _do_op(
        args,
        {"op": "remove_subheading", "heading": args.heading,
         "subheading": args.subheading},
        verb="remove-subheading",
    )


def cmd_show(args) -> int:
    _refuse_retired_skill_flag(args)
    target = _resolve_target(args, verb="show")
    text = _read_text(target.path)
    if args.heading:
        doc = parse_sectioned_doc(text)
        section = doc.find(args.heading)
        if section is None:
            return _err(
                "heading_missing",
                available_headings=[s.heading for s in doc.sections],
            )
        # Return only the section block (heading + body) using the parser
        # by re-serializing a doc that contains just this section. Keeps
        # output round-trippable.
        from istota.memory.curation.types import SectionedDoc
        sub = SectionedDoc(preamble=[], sections=[section])
        body = serialize_sectioned_doc(sub)
        print(body, end="" if body.endswith("\n") else "\n")
        return 0
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_headings(args) -> int:
    _refuse_retired_skill_flag(args)
    target = _resolve_target(args, verb="headings")
    text = _read_text(target.path)
    doc = parse_sectioned_doc(text)
    print(json.dumps(
        {"status": "ok", "headings": [s.heading for s in doc.sections]},
        ensure_ascii=False,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.memory",
        description="Runtime memory writes (USER.md / CHANNEL.md)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_channel_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--channel",
            help="Target /Channels/<token>/CHANNEL.md instead of USER.md.",
        )

    def _add_retired_skill_flag(p: argparse.ArgumentParser) -> None:
        """Accept `--skill` only to refuse it in the CLI's own envelope.

        Overlay writes are gone (ISSUE-343), but the command that made them is
        durable in a way a code change does not reach: it is written into live
        `config/skills/*.md` overlays, into `USER.md`, into conversation
        history, and it was in `docs/development/development-rules-user.md`
        until this change edited it. Something will run it.

        Off the parser entirely, that is argparse exit 2 with a usage dump on
        **stderr** and nothing on stdout — which is exactly the failure
        `_require_heading` exists to avoid, since the model reads stdout and
        sees an empty answer. So the flag stays, purely to answer.
        """
        p.add_argument("--skill", help=argparse.SUPPRESS)

    # `--heading` is deliberately not `required=True`: `_require_heading`
    # refuses in the CLI's own JSON envelope rather than argparse's stderr
    # usage dump, which the model, reading stdout, sees as an empty answer.
    p_app = sub.add_parser("append", help="Append a bullet under an existing heading.")
    p_app.add_argument("--heading")
    p_app.add_argument("--line", required=True)
    p_app.add_argument(
        "--subheading",
        help="Append under this `### subheading` of the heading instead of the top region.",
    )
    _add_channel_flag(p_app)
    _add_retired_skill_flag(p_app)

    p_add = sub.add_parser("add-heading", help="Add a new heading with one or more bullets.")
    p_add.add_argument("--heading", required=True)
    p_add.add_argument("--line", action="append", required=True,
                       help="Bullet line; pass multiple times for multiple bullets.")
    _add_channel_flag(p_add)
    _add_retired_skill_flag(p_add)

    p_rm = sub.add_parser("remove", help="Remove a bullet under a heading (unique substring).")
    p_rm.add_argument("--heading")
    p_rm.add_argument("--match", required=True)
    _add_channel_flag(p_rm)
    _add_retired_skill_flag(p_rm)

    p_rep = sub.add_parser("replace", help="Rewrite the single matching bullet in place.")
    p_rep.add_argument("--heading")
    p_rep.add_argument("--match", required=True)
    p_rep.add_argument("--line", required=True)
    _add_channel_flag(p_rep)
    _add_retired_skill_flag(p_rep)

    p_rmh = sub.add_parser("remove-heading", help="Drop a whole `## ` section.")
    p_rmh.add_argument("--heading", required=True)
    _add_channel_flag(p_rmh)
    _add_retired_skill_flag(p_rmh)

    p_rms = sub.add_parser(
        "remove-subheading",
        help="Drop a whole `### ` subsection and everything under it.",
    )
    p_rms.add_argument("--heading")
    p_rms.add_argument("--subheading")
    _add_channel_flag(p_rms)
    _add_retired_skill_flag(p_rms)

    p_show = sub.add_parser("show", help="Print USER.md or a CHANNEL.md, optionally filtered to one heading.")
    p_show.add_argument("--heading")
    _add_channel_flag(p_show)
    _add_retired_skill_flag(p_show)

    p_h = sub.add_parser("headings", help="List the `## ` heading names.")
    _add_channel_flag(p_h)
    _add_retired_skill_flag(p_h)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "append": cmd_append,
        "add-heading": cmd_add_heading,
        "remove": cmd_remove,
        "replace": cmd_replace,
        "remove-heading": cmd_remove_heading,
        "remove-subheading": cmd_remove_subheading,
        "show": cmd_show,
        "headings": cmd_headings,
    }
    rc = commands[args.command](args)
    if rc:
        sys.exit(rc)


if __name__ == "__main__":
    main()
