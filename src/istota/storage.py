"""Bot-managed Nextcloud storage operations."""

import logging
import os
import re
import shutil
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .atomic_write import write_text_atomic
from .rclone_client import (
    rclone_cat,
    rclone_mkdir,
    rclone_path_exists,
    rclone_rcat,
    rclone_run,
)

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.storage")

BOT_USER_BASE = "/Users"
CHANNEL_BASE = "/Channels"

WORKSPACE_README = """\
# Istota

This is a shared collaboration folder — both you and Istota have \
read/write access. Everything you interact with lives here.

## Files

Configuration files live in the `config/` subfolder:

- **config/USER.md** — Persistent memory
- **config/TASKS.md** — Task queue (`- [ ] do something`)
- **config/HEARTBEAT.md** — Health monitoring configuration
- **config/CRON.md** — Scheduled recurring jobs
- **config/PERSONA.md** — Bot personality (editable copy of global persona)

See `examples/` for detailed documentation and configuration reference.
"""

WORKSPACE_README_EXAMPLE = """\
# Istota

This is a shared collaboration folder — both you and Istota have \
read/write access. Everything you interact with lives here.

## Files

Configuration files live in the `config/` subfolder:

- **config/USER.md** — Persistent memory. Istota reads this at the start of every \
task and appends to it when you ask it to remember something.
- **config/skills/<skill>.md** — (Optional) Per-skill additions to a skill's \
instructions, read only when that skill loads. A development workflow for coding \
tasks usually goes in `config/skills/developer.md` — see `examples/WORKFLOW.md`.
- **config/TASKS.md** — Task queue. Write `- [ ] do something` and Istota picks \
it up automatically. Status updates are written back to the file.
- **config/HEARTBEAT.md** — (Optional) Health monitoring configuration. \
Set up periodic checks that alert you when something needs attention.
- **config/CRON.md** — (Optional) Scheduled recurring jobs. \
Configure tasks that run on a cron schedule with results delivered to Talk or email.
- **config/PERSONA.md** — (Optional) Bot personality. \
Edit this to customize how Istota behaves and communicates with you.

## Other content

Istota saves drafts, summaries, research, and anything else you ask it to \
produce in this folder. You can also drop files here for Istota to read \
in future conversations.

Additionally, you can share any of your own Nextcloud folders with Istota \
for direct access to your files.
"""

BRIEFINGS_EXAMPLE = """\
# Briefing Schedule

This file is no longer read.

Briefing schedules used to live here. They are now the operator's TOML config
plus your own entries in the database, which is what the settings page and the
`istota briefing` command write. Anything this file held was carried over
automatically the first time the new version started.

The file was retired because it silently won: a schedule set in the settings
page was overridden by whatever this file said, and the page went on showing
the value you had chosen.

## Changing a briefing

- In the web UI: Briefings, then Settings.
- Just ask, in any room: "move my morning briefing to 8am".

Your own `config/BRIEFINGS.md` (if you have one) is inert and can be deleted.
"""


# Template for initial HEARTBEAT.md file
HEARTBEAT_TEMPLATE = """\
# Heartbeat Monitoring

See `examples/HEARTBEAT.md` for all check types and options.

```toml
# [settings]
# conversation_token = "{conversation_token}"  # Talk room for alerts
# quiet_hours = ["22:00-07:00"]                # Suppress alerts during these hours
# default_cooldown_minutes = 60                # Time between repeat alerts

# [[checks]]
# name = "backup-fresh"
# type = "file-watch"
# path = "/Users/{user_id}/backups/latest.log"
# max_age_hours = 25
```
"""

HEARTBEAT_EXAMPLE = """\
# Heartbeat Monitoring

Configure periodic health checks that alert you when something needs attention.
HEARTBEAT.md is for monitoring — checking conditions and alerting on failures.
For running tasks on a schedule (including AI-powered checks), use CRON.md instead.

The scheduler evaluates these checks automatically — changes take effect within ~60 seconds.

## Example

```toml
[settings]
conversation_token = "abc123"          # Talk room for alerts
quiet_hours = ["22:00-07:00"]          # Suppress alerts during these hours
default_cooldown_minutes = 60          # Time between repeat alerts

[[checks]]
name = "backup-fresh"
type = "file-watch"
path = "/Users/alice/backups/latest.log"
max_age_hours = 25
cooldown_minutes = 120                 # Override default cooldown
interval_minutes = 15                  # Run every 15 min (default: every cycle)

[[checks]]
name = "disk-space"
type = "shell-command"
command = "df -h / | tail -1 | awk '{print $5}' | tr -d '%'"
condition = "< 90"
message = "Disk usage at {value}%"

[[checks]]
name = "api-health"
type = "url-health"
url = "https://api.example.com/health"
expected_status = 200
timeout = 10

[[checks]]
name = "schedule-conflicts"
type = "calendar-conflicts"
lookahead_hours = 24

[[checks]]
name = "overdue-tasks"
type = "task-deadline"
source = "file"
warn_hours_before = 24

[[checks]]
name = "system-health"
type = "self-check"
interval_minutes = 30                  # Run every 30 min (expensive: spawns Claude)
cooldown_minutes = 60

[checks.config]
execution_test = true                  # Test actual Claude CLI invocation
```

## Check Types

- **file-watch** — Check file age or existence (`path`, `max_age_hours`)
- **shell-command** — Run command, evaluate condition (`command`, `condition`, `message`, `timeout`)
- **url-health** — HTTP health check (`url`, `expected_status`, `timeout`)
- **calendar-conflicts** — Find overlapping events (`lookahead_hours`)
- **task-deadline** — Check for overdue tasks (`source`, `warn_hours_before`)
- **self-check** — System health diagnostics: Claude binary, bwrap, DB, failure rate, execution test (`execution_test`)

## Per-Check Fields

- `cooldown_minutes` — Override `default_cooldown_minutes` for this check
- `interval_minutes` — Run this check every N minutes instead of every cycle (~60s). Useful for expensive checks like `self-check`. Omit to run every cycle.

## Conditions (shell-command)

- `< N` / `> N` — Numeric comparison
- `== value` — Exact string match
- `contains:text` — Substring match
- `not-contains:text` — Negative substring match

## Quiet Hours

Time ranges like `22:00-07:00` suppress alert delivery, but checks still run.
Cross-midnight ranges are supported. When quiet hours end, the next failure triggers an alert.

## Cooldown

After an alert, no repeat alerts are sent until the cooldown expires.
Set `cooldown_minutes` per-check to override `default_cooldown_minutes`.
"""


def _build_heartbeat_seed(config: "Config", user_id: str) -> str:
    """Build seed HEARTBEAT.md content, filling conversation_token and user_id."""
    token = ""
    user_config = config.users.get(user_id)
    if user_config:
        for b in user_config.briefings:
            if b.conversation_token:
                token = b.conversation_token
                break
    return HEARTBEAT_TEMPLATE.format(conversation_token=token, user_id=user_id)




# Template for initial TASKS.md file
TASKS_FILE_TEMPLATE = """\
# Tasks
"""

TASKS_FILE_EXAMPLE = """\
# Tasks

Write a task as `- [ ] do something` and Istota picks it up automatically.
Status updates are written back to this file.

## Status Markers

- `[ ]` — Pending (Istota will pick this up)
- `[~]` — In progress (Istota is working on it)
- `[x]` — Completed
- `[!]` — Failed

## Examples

```markdown
- [ ] summarize my inbox
- [ ] check the weather forecast for this weekend
- [ ] draft a reply to the last email from Alice
```

Tasks are identified by content hash, so you can reorder freely.
Completed/failed tasks can be deleted or kept for reference.
"""

# Template for initial memory file.
#
# The HTML comment at the top is read by Claude when USER.md is loaded
# into the prompt. It's a hint, not enforcement — the structural fix
# is the runtime classification gate in the memory skill — but it
# survives parser round-trips (preamble) and can nudge the model when
# it's unsure where a memory belongs.
MEMORY_TEMPLATE = """<!-- agents: This file holds behavioral instructions and stable context only. Temporal events (purchases, decisions, status changes — anything you'd date-stamp) and stable factual claims (allergies, family, biography) belong in the knowledge graph via `istota-skill memory_search add-fact`. Append behavioral instructions only via `istota-skill memory append --heading "<existing heading>"`. Never use `echo >>` on this file. -->
# User Memory

This file contains remembered information about the user.
The bot can append to this file to remember things for future conversations.

## Notes

"""


def get_user_base_path(user_id: str) -> str:
    """Get the base path for a user's bot-managed directory."""
    return f"{BOT_USER_BASE}/{user_id}"


def get_user_memory_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's memory file (USER.md in bot dir config/)."""
    return f"{get_user_config_path(user_id, bot_dir)}/USER.md"


def get_user_memories_path(user_id: str) -> str:
    """Get the path to a user's dated memories directory."""
    return f"{get_user_base_path(user_id)}/memories"


def get_user_bot_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's bot directory (e.g. /Users/{uid}/istota/)."""
    return f"{get_user_base_path(user_id)}/{bot_dir}"


def get_user_config_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's bot config/ directory."""
    return f"{get_user_bot_path(user_id, bot_dir)}/config"



def get_user_tasks_file_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's TASKS.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/TASKS.md"


def get_user_shared_path(user_id: str) -> str:
    """Get the path to a user's shared folder (for auto-organized shared files)."""
    return f"{get_user_base_path(user_id)}/shared"


def get_user_scripts_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's scripts directory (inside bot dir)."""
    return f"{get_user_base_path(user_id)}/{bot_dir}/scripts"


def get_user_playbooks_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's learned-playbooks directory (inside bot dir)."""
    return f"{get_user_base_path(user_id)}/{bot_dir}/playbooks"


def get_user_briefings_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's retired ``BRIEFINGS.md``.

    Nothing reads the file as config any more. The one caller left is
    ``user_briefings.import_from_workspace_files``, which carries what it
    holds into ``briefing_configs`` once.
    """
    return f"{get_user_config_path(user_id, bot_dir)}/BRIEFINGS.md"


def get_user_heartbeat_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's HEARTBEAT.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/HEARTBEAT.md"




def get_user_cron_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's CRON.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/CRON.md"



def get_user_persona_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's PERSONA.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/PERSONA.md"


def get_user_skill_overlays_path(user_id: str, bot_dir: str) -> str:
    """Directory of per-skill user overlay files.

    One flat ``<skill-name>.md`` per skill, appended to that skill's bundled
    body whenever the skill is loaded. Distinct from the *operator* override at
    ``config/skills/<name>/skill.md``, which replaces the body outright — an
    overlay is additive, so upstream skill edits keep flowing under it.
    """
    return f"{get_user_config_path(user_id, bot_dir)}/skills"


def resolve_user_skill_overlays_dir(config: "Config", user_id: str) -> Path | None:
    """The on-disk overlay directory for a user, or None where there is none.

    Both load paths — the eager one in ``executor`` and ``skills show`` — call
    this rather than joining the mount themselves. That is the same argument as
    injecting inside ``load_skills``, one level up: two call sites deriving one
    path independently is a wrong ``bot_dir`` or a missing ``lstrip`` away from
    leaving one path silently inert while both test suites stay green.

    None without a mount. Overlays are filesystem reads, so an rclone-remote
    deployment has none — the condition ``load_persona`` already applies to a
    per-user ``PERSONA.md``.

    None as well when the directory leads outside the user's own tree.
    ``config`` and ``skills`` are ordinary entries under a root bound
    read-write into that user's sandbox, so either can be replaced with a
    symlink; the loader's ``O_NOFOLLOW`` covers only the overlay file itself,
    and the files at the far end of a redirected directory are ordinary
    regular files that pass every leaf-level guard. Returning None degrades to
    exactly the prompt the skill would have had with no overlay at all, which
    is what every other overlay failure path already does.

    The **resolved** path is what comes back, so a caller cannot re-walk by the
    unresolved name after the check.
    """
    if not config.use_mount:
        return None
    from .skills._loader import contained_overlay_dir  # noqa: PLC0415 - import cycle

    overlay_dir = _get_mount_path(
        config, get_user_skill_overlays_path(user_id, config.bot_dir_name)
    )
    user_root = _get_mount_path(config, f"Users/{user_id}")
    return contained_overlay_dir(overlay_dir, user_root)


def _contained_channel_dir(
    config: "Config", conversation_token: str
) -> Path | None:
    """``{mount}/Channels/{token}`` resolved, or None if it leads outside.

    The channel counterpart to ``resolve_user_config_dir``. That directory is
    bound read-write into the sandbox of every task in the room, so the token
    passing ``validate_conversation_token`` says nothing about where the
    directory it names actually resolves to.

    **Equality, not "under the root"**, which is where this differs from the
    user-tree rule. The looser rule there exists so a user who reorganised their
    own workspace still works, and it is safe because everything under their
    root is theirs. ``Channels/`` is bot-managed and holds every room, so "under
    the root" would let a link at ``Channels/{token}`` resolve into *another
    room's* directory and put that room's CHANNEL.md into this room's prompt.
    Nobody has a reason to reorganise this tree.
    """
    from .skills._loader import contained_overlay_dir  # noqa: PLC0415 - import cycle

    channels = _get_mount_path(config, "Channels")
    resolved = contained_overlay_dir(
        _get_mount_path(config, get_channel_base_path(conversation_token)),
        channels,
    )
    if resolved is None:
        return None
    try:
        expected = Path(os.path.realpath(channels)) / conversation_token
    except OSError:
        return None
    return resolved if resolved == expected else None


def _contained_under_user_root(
    config: "Config", user_id: str, path: Path
) -> Path | None:
    """``path`` resolved, or None if it leads outside ``{mount}/Users/{uid}``.

    The generic form of ``resolve_user_config_dir``, for the seeding path, which
    has to check several directories under one root. Same rule, same function
    behind it; a missing directory resolves fine and is the caller's to create.
    """
    from .skills._loader import contained_overlay_dir  # noqa: PLC0415 - import cycle

    return contained_overlay_dir(path, _get_mount_path(config, f"Users/{user_id}"))


def resolve_user_config_dir(config: "Config", user_id: str) -> Path | None:
    """The user's ``{bot_dir}/config`` directory, resolved, or None.

    One level up from ``resolve_user_skill_overlays_dir`` and for the same
    reason (ISSUE-339). ``config/`` holds USER.md, PERSONA.md and the seeded
    TASKS/CRON/HEARTBEAT files, and it is an ordinary entry under
    ``{mount}/Users/{user_id}``, which ``build_bwrap_cmd`` binds **read-write**
    into that user's own sandbox — so ``mv config config.real && ln -s
    /anywhere config`` is two commands from inside it. The daemon then reads
    those files host-side, in a filesystem view that includes its own home,
    ``/etc/istota/`` and other users' trees, and what it reads becomes prompt
    text on the next task.

    Containment is the same equality-under-a-known-root rule
    ``contained_overlay_dir`` states, and it is that function rather than a
    fifth copy of it. A link that stays *inside* the user's own tree passes:
    it leads nowhere they could not already reach, and refusing it would break
    someone who reorganised their own workspace.

    The **resolved** path comes back and callers must use it, since the check
    and the reads that follow are separated by at least one ``open(2)``.

    None without a mount — an rclone-remote deployment has no such directory,
    the condition ``load_persona`` already applies to a per-user PERSONA.md.
    """
    if not config.use_mount:
        return None
    return _contained_under_user_root(
        config,
        user_id,
        _get_mount_path(config, get_user_config_path(user_id, config.bot_dir_name)),
    )


#: Ceiling on any single file read out of a user's ``config/`` directory.
#:
#: Same purpose as ``OVERLAY_READ_CAP_BYTES`` — stop a multi-gigabyte file
#: planted at a fixed name from being pulled into the daemon — and deliberately
#: not the same number. An overlay over its cap is refused and the skill loses
#: a customization; USER.md over its cap is a person's whole memory, and it is a
#: file the *product* grows, one curated bullet at a time. 1 MiB was close
#: enough to a real one to be reachable by padding, which turned a refusal into
#: an erasure once the curator saw it (the curator now refuses to write on an
#: unreadable read, so this is depth rather than the guard).
#:
#: Not silent: every refusal logs, and ``read_user_memory_v2`` returning None
#: here means the prompt loses the user's memory, which is a condition an
#: operator has to be able to see.
USER_CONFIG_READ_CAP_BYTES = 16 * 1024 * 1024


def read_user_config_file(
    config: "Config", user_id: str, filename: str
) -> str | None:
    """Text of ``{bot_dir}/config/{filename}``, or None where there is none.

    The hardened read for every host-side reader of that directory. Containment
    on the directory, then ``read_overlay_bytes`` on the leaf — ``O_NOFOLLOW``
    so a symlink planted at the filename cannot put another file's bytes into
    the prompt, ``S_ISREG`` behind ``O_NONBLOCK`` so a FIFO left there is
    refused rather than blocking ``open(2)`` forever, and the size checked on
    the fd before the read.

    The FIFO half is the one with no timeout behind it: prompt assembly runs
    *before* the ``BrainRequest`` exists, so one ``mkfifo`` would wedge every
    later task for that user, silently and for good.

    ``None`` means *could not read it* — refused, outside the tree, or not
    UTF-8. A file that is simply not there comes back as ``""``, following
    ``read_overlay_bytes``' own contract. The two are worth keeping apart even
    though the prompt-assembly callers treat them alike: the nightly curator
    re-reads USER.md after the LLM call and aborts on a sha mismatch, so
    folding an emptied file into "unchanged" would let it clobber a runtime
    write that had just truncated the file.

    A refusal is logged rather than raised. Two callers run somewhere an
    exception has no home — prompt assembly, and the daemon's workspace
    seeding — and the never-raises contract is what lets both degrade to "no
    such file" instead of failing a task.
    """
    config_dir = resolve_user_config_dir(config, user_id)
    if config_dir is None:
        if config.use_mount:
            logger.warning(
                "user_config_dir_outside_user_tree user=%s file=%s", user_id, filename,
            )
        return None
    text, reason = read_regular_file(config_dir / filename)
    if reason is not None:
        logger.warning(
            "user_config_read_refused user=%s file=%s reason=%s",
            user_id, filename, reason,
        )
    return text


def read_regular_file(
    path: Path, *, max_bytes: int = USER_CONFIG_READ_CAP_BYTES
) -> tuple[str | None, str | None]:
    """``read_user_config_file`` for a caller that already holds a safe path.

    Returns ``(text, refusal_reason)``; exactly one is set, and a missing file
    is ``("", None)``. This is the leaf half on its own, and it exists because
    re-deriving a path is not a free way to re-read one: ``resolve_user_config_
    dir`` returns a resolved path precisely so a caller stops walking the
    unresolved name, and the nightly curator resolves once, holds the path
    across a whole LLM call, and then has to read the *same* file back to
    compare fingerprints. Going through ``read_user_config_file`` there would
    re-resolve ``config/`` from scratch, so the guard would compare whatever the
    directory points at now against a write going to the path resolved minutes
    earlier — an in-tree link swap during the brain call, which is permitted and
    which the model can perform, would make the anti-clobber check read a stale
    copy, pass, and let the curator overwrite the runtime write it exists to
    protect.

    Containment of the *ancestors* is the caller's, exactly as for
    ``write_regular_file``. What this covers is the last component.
    """
    # noqa: PLC0415 - import cycle
    from .skills._loader import OVERLAY_NOT_UTF8, read_overlay_bytes

    data, reason, _size = read_overlay_bytes(path, max_bytes=max_bytes)
    if reason is not None:
        return None, reason
    assert data is not None
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        # `_loader`'s published vocabulary, like every other reason this
        # returns. The prefix is wider than its name now that four surfaces
        # report these codes about USER.md, PERSONA.md and CHANNEL.md as well as
        # overlays — but one word for one condition is worth more than a name
        # that reads well in isolation, and a second spelling here would make
        # this the only surface saying something different about the same file.
        return None, OVERLAY_NOT_UTF8


def create_file_if_absent(path: Path, text: str) -> bool:
    """Create ``path`` holding ``text``, or leave whatever is already there.

    The seeding counterpart to ``read_user_config_file``, and it replaces the
    ``if not path.exists(): path.write_text(...)`` pattern rather than
    supplementing it: ``exists()`` *follows* a link, so a **dangling** symlink
    planted at one of the seeded names reads as absent and the write then lands
    at the far end of it — a file of the daemon's making, at a path of the
    model's choosing. ``O_CREAT | O_EXCL | O_NOFOLLOW`` answers both halves in
    one syscall, and closes the gap between the check and the write as well.

    False when something was already there (link included) or the write failed;
    True only when this call created the file. A refusal is **logged** — the
    "already there" case is the ordinary one on every call after the first, but
    an existing *non-regular* inode at a seeded name is a planted one, and
    ``O_EXCL | O_NOFOLLOW`` reports a symlink as ``EEXIST`` rather than
    ``ELOOP``, so without the ``lstat`` the hostile inode is refused correctly
    and reported by nothing, forever.

    Never raises. The callers are workspace seeding paths that must degrade
    rather than abort a start-up, so ``ValueError`` is caught beside ``OSError``
    — a lone surrogate in ``text`` raises ``UnicodeEncodeError``, which is not
    an ``OSError`` and would otherwise escape the contract.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        _warn_if_not_regular(path)
        return False
    except OSError as e:
        logger.warning("seed_file_refused path=%s errno=%s", path.name, e.errno)
        return False
    owned = False
    try:
        owned = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        return True
    except (OSError, ValueError) as e:
        logger.warning("seed_file_failed path=%s error=%s", path.name, type(e).__name__)
        return False
    finally:
        if not owned:
            os.close(fd)


def _warn_if_not_regular(path: Path) -> None:
    """Report a planted inode at a seeded name. Never raises."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            logger.warning(
                "seed_file_refused path=%s reason=not_a_regular_file", path.name,
            )
    except OSError:
        pass


def write_regular_file(path: Path, text: str) -> bool:
    """Replace ``path``'s contents, refusing anything that is not a plain file.

    For the writers that legitimately overwrite — ``init_user_memory_v2``, the
    ``examples/`` refresh and the nightly curator. ``O_NOFOLLOW`` refuses a
    symlink at the last component; the ``S_ISREG`` check on the fd refuses a
    FIFO or a device, and ``O_NONBLOCK`` keeps the open from blocking while it
    decides. Ancestor containment is the caller's, through
    ``resolve_user_config_dir``.

    **The probe and the write are separate files.** Truncating the target and
    writing into it means an ``OSError`` partway — ENOSPC, EDQUOT, or a FUSE
    fault on the mount this runs on — returns False with the file now zero
    bytes, and every caller reads False as "nothing was written". For the
    curator that is USER.md destroyed and reported as a no-op. So the target is
    opened only to *check* it, the content goes to a staging file in the same
    directory, and ``os.replace`` publishes it atomically — which is what
    ``atomic_write.write_text_atomic`` does. ``os.replace`` does not follow
    a symlink at the destination, so the probe is what keeps a planted link from
    being quietly replaced with a real file rather than being the thing that
    stops the write landing elsewhere.

    False on refusal or failure, never an exception: the curator runs
    unattended and its caller treats False as "nothing was written tonight".
    """
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o644,
        )
    except OSError as e:
        logger.warning("write_refused path=%s errno=%s", path.name, e.errno)
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            logger.warning("write_refused path=%s reason=not_a_regular_file", path.name)
            return False
    except OSError as e:
        logger.warning("write_refused path=%s errno=%s", path.name, e.errno)
        return False
    finally:
        os.close(fd)

    try:
        # 0o644 because this becomes a file the user reads over Nextcloud;
        # `atomic_write` applies it to the descriptor rather than the name,
        # which is what this directory being model-writable requires.
        write_text_atomic(path, text, mode=0o644)
        return True
    except (OSError, ValueError) as e:
        logger.warning("write_failed path=%s error=%s", path.name, type(e).__name__)
        return False


def open_user_skill_overlays(
    config: "Config", user_id: str
) -> tuple[Path | None, int | None]:
    """The user's overlay directory and an open descriptor on it.

    Every reader of this directory used to answer containment with
    ``resolve_user_skill_overlays_dir`` and then resolve the path by name again
    for each file it read. The comparison is only true for as long as nobody
    moves anything, and every component under ``{mount}/Users/{user_id}`` is
    model-writable — ``build_bwrap_cmd`` binds that tree read-write into the
    user's own sandbox — so ``mv config config.real && ln -s /anywhere config``
    lands in the middle of the window. ``open_overlay_dir`` walks each component
    with ``O_NOFOLLOW | O_DIRECTORY`` instead and hands back a descriptor
    pinned to an inode, which is containment by construction rather than by a
    comparison (ISSUE-341 item 3, ISSUE-344).

    **There are exactly two answers, and the path never comes back without the
    descriptor.** Either ``(path, fd)`` — the directory is open, ``path`` is the
    resolved spelling for messages and index keys, ``fd`` is what every read
    goes through, and **the caller closes it** — or ``(None, None)``, meaning
    there is nothing to read: no mount, no directory, a directory outside the
    user's own tree, or one that could not be opened without following a
    symlink.

    The first draft of this returned a third answer, ``(path, None)``, for the
    ordinary case of a directory that does not exist yet. That was the bug both
    reviews of ISSUE-344 found, and it reintroduced the very race the issue
    exists to close: the prompt loader took the path, had no descriptor to read
    through, and re-resolved it by name — so a task that created
    ``config/skills`` as a symlink between this function's ``is_dir`` and
    ``load_skills``' own put a file from outside the mount into the next
    prompt. Reproduced end to end before it was removed. A caller holding a
    usable path with no descriptor is the shape that produced this issue in the
    first place, and offering it as a documented return value only made it
    easier to reach. **Do not reintroduce it**: a surface that needs to tell an
    absent directory from a refused one (the ``skills`` read verbs do, to
    report an empty inventory rather than an error) asks that question for
    itself, where the answer is used for wording rather than for reaching a
    file.

    ``open_overlay_dir`` is deliberately stricter than ``contained_overlay_dir``
    — it refuses a symlink at any component, including one landing back inside
    the user's own tree, because refusing is the only answer that survives the
    path being rewritten underneath it. So the two can disagree, and on this
    layout the second answer is the one that stands.

    **The returned path is for display and for index keys, never for opening.**
    It is realpath'd *before* the walk, so under a concurrent swap the two can
    name different inodes; every consumer reaches files through the descriptor
    and uses the path only for ``path.name`` and for messages, which is what
    makes that harmless. Held by
    ``tests/test_overlay_dir_containment.py::TestTheReturnedPathIsDisplayOnly``
    rather than by convention.

    An operator ``bot_dir_name`` that is not a single plain filename component
    (a ``/``, ``.``, ``..`` or NUL) is refused by the walk while
    ``resolve_user_skill_overlays_dir`` accepts it, so such a deployment reads
    as "no overlays" rather than erroring. Fail-closed, and unreachable through
    a sanitized config.
    """
    from .skills._loader import open_overlay_dir  # noqa: PLC0415 - import cycle

    overlay_dir = resolve_user_skill_overlays_dir(config, user_id)
    if overlay_dir is None:
        return None, None
    # No `is_dir` precheck. The `openat` walk below answers "is there a
    # directory here" and "is it reachable without following a link" in one
    # act, and a stat first would only be a second, name-based resolution of a
    # model-writable path — which is the thing being removed, not a shortcut to
    # it. A missing directory fails the walk with ENOENT like anything else.
    user_root = _get_mount_path(config, f"Users/{user_id}")
    fd = open_overlay_dir(user_root, config.bot_dir_name, "config", "skills")
    if fd is None:
        return None, None
    return overlay_dir, fd


CRON_TEMPLATE = """\
# Scheduled Jobs

See `examples/CRON.md` for all options and cron format reference.

```toml
# [[jobs]]
# name = "daily-report"
# cron = "0 9 * * *"             # 9am daily (in your timezone)
# prompt = "Generate my daily report"
# target = "talk"                 # "talk", "email", or omit
# room = "{conversation_token}"   # Talk room token (required for target = "talk")
```
"""

CRON_EXAMPLE = """\
# Scheduled Jobs

CRON.md is for running tasks and commands on a schedule.
For monitoring conditions and alerting on failures, use HEARTBEAT.md instead.

Configure recurring tasks that run on a schedule.
The scheduler reads this file automatically — changes take effect within ~60 seconds.

## Example

```toml
[[jobs]]
name = "morning-briefing"
cron = "0 9 * * *"               # 9am daily (in your timezone)
prompt = "Generate my morning briefing"
target = "talk"                   # Post result to Talk room
room = "abc123"                   # Conversation token

[[jobs]]
name = "weekly-review"
cron = "0 18 * * 0"              # 6pm Sundays
prompt = "Generate weekly review of completed tasks"
target = "email"                  # Send result via email

[[jobs]]
name = "check-deadlines"
cron = "0 8 * * 1-5"             # 8am weekdays
prompt = "Check for any upcoming deadlines this week"
target = "talk"
room = "abc123"
silent_unless_action = true       # Only post if something needs attention
```

## Fields

- **name** — Unique identifier for the job (e.g., `daily-report`, `weekly-cleanup`)
- **cron** — Standard 5-field cron expression (minute hour day month weekday)
- **prompt** — The full prompt text that will be executed as a task. A backslash or a double quote must be escaped (`\\\\` and `\\"`), or the whole file stops being read and none of your jobs run — use **prompt_file** for anything long or awkward
- **target** — Where to deliver results: `"talk"` or `"email"` (omit for no delivery)
- **room** — Talk conversation token (required when target is `"talk"`)
- **enabled** — Set to `false` to pause the job (default: true)
- **silent_unless_action** — When true, only posts output if response starts with \
`ACTION:` (default: false)

## Runtime Control

Use `!cron` in Talk to manage jobs at runtime:

- `!cron` — List all jobs and their status
- `!cron enable <name>` — Re-enable a disabled job (resets failure count)
- `!cron disable <name>` — Disable a job

Jobs auto-disable after 5 consecutive failures. Use `!cron enable` to re-activate.

## Cron Format

Standard 5-field cron: `minute hour day-of-month month day-of-week`

- `0 9 * * *` — Every day at 9:00 AM
- `0 9 * * 1-5` — Weekdays at 9:00 AM
- `30 18 * * 0` — Sundays at 6:30 PM
- `0 */6 * * *` — Every 6 hours
- `0 8 1 * *` — First of every month at 8:00 AM

Evaluated in the user's configured timezone.
"""


WORKFLOW_EXAMPLE = """\
# Development workflow

There is no `config/WORKFLOW.md`. A development workflow is written in
`config/skills/developer.md`, in `config/USER.md`, or in a project room's
`CHANNEL.md`. This file is the vocabulary — what you can set, not what you
should set.

The developer skill ships a default for each decision below and yields to
whatever you write. Say nothing about a decision and its default applies, so a
workflow of three lines is a perfectly good one.

## Where to write it

- **config/skills/developer.md** — the usual home. It is read only when the
  `developer` skill loads, which is exactly when a workflow decision applies,
  and it costs nothing on the tasks that will never write code. Edit it as a
  file, then run `istota-skill skills overlays` and check it says `binds: true`.
- **config/USER.md** — applies to every task, coding or not. The right home for
  a rule that would still be wrong to ignore on a task where the `developer`
  skill did not load. Anything about what this machine can afford belongs here:
  an admin task can reach a checkout whenever the developer feature is on,
  whether or not the skill was selected, so "don't run the whole suite in the
  foreground on this box" has to be somewhere it will actually be read.
- **CHANNEL.md** in a project's room — applies to tasks from that room only.
- Where `USER.md` and a room's `CHANNEL.md` disagree, `CHANNEL.md` wins for a
  task from that room. It is the more specific statement, and the room is the
  project.
- The overlay says it outranks the `developer` skill's own defaults, and claims
  nothing about the other two files. So do not write the same decision into an
  overlay and into `USER.md` expecting a defined winner — there isn't one.
  Write each decision in one place.

## What you can set

- **Worktree per task** — whether each task cuts its own worktree, or works \
somewhere you name.
- **Change tiers** — how much process a change gets, and what decides which tier \
it is.
- **When a test gets written** — before the implementation, alongside it, or by \
a rule of your own.
- **When tests run, and which** — the scope of the pass. The default is the \
tests covering the change plus lint and typecheck over the whole repository; \
ask here for a whole suite if you want one.
- **Commit granularity** — coherent steps, one commit per task, or your own rule.
- **Whether a review runs** — at which tiers, and whether at all.
- **An MR or PR rather than a merge** — how work lands. Usually a property of \
the project rather than of you, so a room's `CHANNEL.md` is the better home.
- **Report shape** — the block a finished task reports in.

## What you cannot set

Deployment mechanics do not yield: the forge boundary and its refused verbs, the
network allowlist, the ceiling on how long one command may run, the credential
rules, where builds and tests run, the pre-submission checks, and every delete
path. An instruction that collides with one of those is reported back to you
rather than followed.

## Example

```markdown
## Development workflow

- Worktree per task, always.
- No review below Standard tier.
- Land as a merge request; never merge to the default branch yourself.
```

Every decision that block does not mention keeps its default.
"""


def _build_cron_seed(config: "Config", user_id: str) -> str:
    """Build seed CRON.md content, filling conversation_token from admin config."""
    token = ""
    user_config = config.users.get(user_id)
    if user_config:
        for b in user_config.briefings:
            if b.conversation_token:
                token = b.conversation_token
                break
    return CRON_TEMPLATE.format(conversation_token=token)



# The rclone API lives in `istota.rclone_client`, a stdlib-only leaf, because
# `skills/files/__init__.py` carried a byte-identical copy of it and neither
# module could import the other — that skill runs in a subprocess and this one
# pulls in the package. The private names are kept as aliases so this module's
# own callers, and the tests that import them, are unchanged. What that does
# not preserve is `_rclone_run` as one interception point: the four wrappers
# below now call the leaf's runner, so patching this name reaches only the
# one direct call left in this module (`rclone copyto`, in
# `upload_file_to_inbox`). Patch `rclone_client.rclone_run` for the rest.
_rclone_run = rclone_run
_rclone_mkdir = rclone_mkdir
_rclone_path_exists = rclone_path_exists
_rclone_cat = rclone_cat
_rclone_rcat = rclone_rcat


def ensure_user_directories(remote: str, user_id: str, bot_dir: str) -> bool:
    """
    Create the bot-managed directory structure for a user.

    Returns True if all directories were created or already exist.
    """
    base = get_user_base_path(user_id)
    subdirs = ["inbox", "memories", bot_dir, "shared"]

    success = True
    for subdir in subdirs:
        path = f"{base}/{subdir}"
        if not _rclone_mkdir(remote, path):
            # mkdir may fail if it already exists, so check existence
            if not _rclone_path_exists(remote, path):
                success = False

    # Create bot_dir subdirectories
    for sub in ["exports", "scripts", "notes"]:
        sub_path = f"{base}/{bot_dir}/{sub}"
        if not _rclone_mkdir(remote, sub_path):
            if not _rclone_path_exists(remote, sub_path):
                success = False

    return success


def user_directories_exist(remote: str, user_id: str, bot_dir: str) -> dict[str, bool]:
    """
    Check which user directories exist.

    Returns dict mapping directory name to existence status.
    """
    base = get_user_base_path(user_id)
    subdirs = ["inbox", "memories", bot_dir, "shared"]

    result = {}
    for subdir in subdirs:
        path = f"{base}/{subdir}"
        result[subdir] = _rclone_path_exists(remote, path)

    return result


def read_user_memory(remote: str, user_id: str, bot_dir: str) -> str | None:
    """
    Read the user's memory file.

    Returns the content of the memory file, or None if it doesn't exist or is empty.
    """
    memory_path = get_user_memory_path(user_id, bot_dir)
    content = _rclone_cat(remote, memory_path)

    if content is None or not content.strip():
        return None

    return content


def init_user_memory(remote: str, user_id: str, bot_dir: str) -> bool:
    """
    Initialize the user's memory file with a template.

    Returns True on success.
    """
    memory_path = get_user_memory_path(user_id, bot_dir)
    return _rclone_rcat(remote, memory_path, MEMORY_TEMPLATE)


def get_memory_line_count(remote: str, user_id: str, bot_dir: str) -> int | None:
    """
    Get the line count of a user's memory file.

    Returns None if file doesn't exist.
    """
    content = read_user_memory(remote, user_id, bot_dir)
    if content is None:
        return None
    return len(content.splitlines())


def get_user_inbox_path(user_id: str) -> str:
    """Get the path to a user's inbox directory."""
    return f"{get_user_base_path(user_id)}/inbox"


def upload_file_to_inbox(
    remote: str,
    user_id: str,
    local_path: Path,
    remote_filename: str | None = None,
) -> str | None:
    """
    Upload a local file to the user's inbox directory.

    Args:
        remote: rclone remote name
        user_id: User ID
        local_path: Local file path to upload
        remote_filename: Optional filename to use on remote (defaults to local filename)

    Returns:
        The remote path on success, None on failure.
    """
    if not local_path.exists():
        return None

    filename = remote_filename or local_path.name
    inbox_path = get_user_inbox_path(user_id)
    remote_path = f"{inbox_path}/{filename}"

    result = _rclone_run(["rclone", "copyto", str(local_path), f"{remote}:{remote_path}"])

    if result is None or result.returncode != 0:
        return None

    return remote_path


# =============================================================================
# Mount-aware storage functions
# =============================================================================


def _get_mount_path(config: "Config", path: str) -> Path:
    """Get the local mount path for a Nextcloud path."""
    return config.nextcloud_mount_path / path.lstrip("/")


def _migrate_old_layout(user_base: Path) -> None:
    """
    Migrate from old directory layout to new one.

    Old layout:
        context/memory.md → USER.md
        context/YYYY-MM-DD.md → memories/YYYY-MM-DD.md

    Only runs if context/ exists and target files don't. Safe to call repeatedly.
    """
    context_dir = user_base / "context"
    if not context_dir.is_dir():
        return

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

    # Migrate memory.md → USER.md
    old_memory = context_dir / "memory.md"
    new_memory = user_base / "USER.md"
    if old_memory.exists() and not new_memory.exists():
        shutil.copy2(old_memory, new_memory)
        logger.info("Migrated %s → %s", old_memory, new_memory)

    # Migrate dated files → memories/
    memories_dir = user_base / "memories"
    memories_dir.mkdir(exist_ok=True)
    for f in context_dir.iterdir():
        if f.is_file() and date_pattern.match(f.name):
            dest = memories_dir / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
                logger.info("Migrated %s → %s", f, dest)


def _migrate_notes_to_workspace(user_base: Path) -> None:
    """
    Migrate from notes/ to workspace/ directory.

    Only runs if notes/ exists and workspace/ doesn't. Safe to call repeatedly.
    """
    notes_dir = user_base / "notes"
    workspace_dir = user_base / "workspace"
    if notes_dir.is_dir() and not workspace_dir.exists():
        notes_dir.rename(workspace_dir)
        logger.info("Migrated %s → %s", notes_dir, workspace_dir)


def _migrate_workspace_files(user_base: Path) -> None:
    """
    Migrate USER.md and TASKS.md from user root into workspace/.

    Only runs if workspace/ already exists (from a previous migration).
    Does not create workspace/ — the bot_dir layout supersedes it.
    """
    workspace_dir = user_base / "workspace"
    if not workspace_dir.is_dir():
        return

    for filename in ("USER.md", "TASKS.md"):
        src = user_base / filename
        dst = workspace_dir / filename
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            logger.info("Migrated %s → %s", src, dst)


# Config files that live in bot_name/config/
_CONFIG_FILES = (
    "USER.md", "TASKS.md", "BRIEFINGS.md", "HEARTBEAT.md",
    "CRON.md",
)


def _migrate_workspace_to_bot_dir(user_base: Path, bot_dir: str) -> None:
    """
    Migrate from workspace/ to bot directory layout.

    1. If workspace/ exists and bot dir doesn't → rename workspace/ → bot_dir/
    2. Move config .md files from bot_dir/ root into bot_dir/config/

    Safe to call repeatedly.
    """
    workspace_dir = user_base / "workspace"
    bot_dir_path = user_base / bot_dir

    # Step 1: rename workspace/ → bot_dir/
    if workspace_dir.is_dir() and not bot_dir_path.exists():
        workspace_dir.rename(bot_dir_path)
        logger.info("Migrated %s → %s", workspace_dir, bot_dir_path)

    # Step 2: move config files from bot_dir/ root into bot_dir/config/
    if bot_dir_path.is_dir():
        config_dir = bot_dir_path / "config"
        config_dir.mkdir(exist_ok=True)
        for filename in _CONFIG_FILES:
            src = bot_dir_path / filename
            dst = config_dir / filename
            if src.is_file() and not dst.exists():
                shutil.move(str(src), str(dst))
                logger.info("Migrated %s → %s", src, dst)


def ensure_user_directories_v2(config: "Config", user_id: str) -> bool:
    """
    Create the bot-managed directory structure for a user (mount-aware).

    Returns True if all directories were created or already exist.
    """
    bot_dir = config.bot_dir_name
    if config.use_mount:
        base = _get_mount_path(config, get_user_base_path(user_id))

        # Containment runs **first**, before the migrations (ISSUE-339).
        #
        # The leaf-level `O_NOFOLLOW` in the seed writers below covers the last
        # path component only, so a symlink at `{bot_dir}/` or at `config/` sent
        # every seeded file out of the tree — measured, four files written into
        # a directory outside the mount as the daemon user — and
        # `mkdir(exist_ok=True)` follows such a link quite happily.
        #
        # The ordering is the part that is easy to get wrong and was: the check
        # sat after the migrations, and `_migrate_workspace_to_bot_dir` derives
        # `base / bot_dir / "config"` itself, `mkdir`s it and `shutil.move`s
        # into it. So a redirected `{bot_dir}/` was still followed — the
        # refusal fired, returned False, and a `config/` directory had already
        # appeared outside the tree. Nothing may touch these paths before they
        # are known to be contained.
        bot_dir_path = _contained_under_user_root(config, user_id, base / bot_dir)
        if bot_dir_path is None:
            logger.warning(
                "ensure_user_directories_refused user=%s reason=bot_dir_outside_user_tree",
                user_id,
            )
            return False
        config_dir = _contained_under_user_root(
            config, user_id, bot_dir_path / "config"
        )
        if config_dir is None:
            logger.warning(
                "ensure_user_directories_refused user=%s reason=config_dir_outside_user_tree",
                user_id,
            )
            return False

        # Run migrations before creating directories
        _migrate_old_layout(base)
        _migrate_notes_to_workspace(base)
        _migrate_workspace_files(base)
        _migrate_workspace_to_bot_dir(base, bot_dir)

        subdirs = ["inbox", "memories", bot_dir, "shared"]
        for subdir in subdirs:
            path = base / subdir
            path.mkdir(parents=True, exist_ok=True)
        logger.debug("Ensured user directories for %s via mount", user_id)

        config_dir.mkdir(exist_ok=True)
        exports_dir = bot_dir_path / "exports"
        exports_dir.mkdir(exist_ok=True)
        scripts_dir = bot_dir_path / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        notes_dir = bot_dir_path / "notes"
        notes_dir.mkdir(exist_ok=True)

        # Migrate scripts/ from user root into bot dir
        old_scripts = base / "scripts"
        if old_scripts.is_dir() and any(old_scripts.iterdir()):
            for item in old_scripts.iterdir():
                dst = scripts_dir / item.name
                if not dst.exists():
                    shutil.move(str(item), str(dst))
                    logger.info("Migrated %s → %s", item, dst)

        # Migrate old exports/ to bot_dir/exports/
        old_exports = base / "exports"
        if old_exports.is_dir() and any(old_exports.iterdir()):
            for item in old_exports.iterdir():
                dst = exports_dir / item.name
                if not dst.exists():
                    shutil.move(str(item), str(dst))
                    logger.info("Migrated %s → %s", item, dst)

        # Every seed below goes through `create_file_if_absent` rather than
        # `if not exists(): write_text(...)`. `exists()` follows a link, so a
        # dangling symlink planted at one of these names read as absent and the
        # seed landed wherever it pointed, written by the daemon (ISSUE-339).
        readme = bot_dir_path / "README.md"
        if create_file_if_absent(readme, WORKSPACE_README):
            logger.debug("Created %s README for %s", bot_dir, user_id)

        # Seed config/ with default files
        tasks_file = config_dir / "TASKS.md"
        if create_file_if_absent(tasks_file, TASKS_FILE_TEMPLATE):
            logger.debug("Created %s/config/TASKS.md for %s", bot_dir, user_id)

        # No BRIEFINGS.md is seeded. The file is retired as an input — briefings
        # are the TOML config plus the `briefing_configs` table the web UI and
        # `istota briefing` write. An existing one is left alone rather than
        # deleted (it is the user's file), and `examples/BRIEFINGS.md` says so.

        heartbeat_file = config_dir / "HEARTBEAT.md"
        if create_file_if_absent(
            heartbeat_file, _build_heartbeat_seed(config, user_id)
        ):
            logger.debug("Created %s/config/HEARTBEAT.md for %s", bot_dir, user_id)

        cron_file = config_dir / "CRON.md"
        if create_file_if_absent(cron_file, _build_cron_seed(config, user_id)):
            logger.debug("Created %s/config/CRON.md for %s", bot_dir, user_id)

        # Seed PERSONA.md from the global persona file. Read lazily, inside the
        # absence check: this function runs on every task, every scheduler pass
        # and every inbound email, and hoisting the read out of the guard made
        # an undecodable operator persona raise on all of them rather than once
        # at first seed. `encoding` is pinned because the seed is written UTF-8.
        persona_file = config_dir / "PERSONA.md"
        if not persona_file.exists():
            global_persona = config.skills_dir.parent / "persona.md"
            if global_persona.exists():
                try:
                    seed = global_persona.read_text(encoding="utf-8")
                except (OSError, ValueError) as e:
                    logger.warning(
                        "persona_seed_unreadable user=%s error=%s",
                        user_id, type(e).__name__,
                    )
                    seed = None
                if seed is not None and create_file_if_absent(persona_file, seed):
                    logger.debug(
                        "Created %s/config/PERSONA.md for %s", bot_dir, user_id,
                    )

        # Write example files (always overwrite to stay current). These are the
        # one place an overwrite is intended, and they were the last unhardened
        # write in this function: a plain `write_text` follows a link, so a
        # symlink planted at `examples/CRON.md` had the template written over a
        # victim of the model's choosing on *every* call — and a FIFO there
        # blocked prompt assembly, which is the wedge this whole issue is about
        # (measured both, ISSUE-339).
        examples_dir = _contained_under_user_root(
            config, user_id, bot_dir_path / "examples"
        )
        if examples_dir is None:
            logger.warning(
                "examples_refresh_refused user=%s reason=outside_user_tree", user_id,
            )
        else:
            examples_dir.mkdir(exist_ok=True)
            examples = {
                "README.md": WORKSPACE_README_EXAMPLE,
                "TASKS.md": TASKS_FILE_EXAMPLE,
                "BRIEFINGS.md": BRIEFINGS_EXAMPLE,
                "HEARTBEAT.md": HEARTBEAT_EXAMPLE,
                "CRON.md": CRON_EXAMPLE,
                "WORKFLOW.md": WORKFLOW_EXAMPLE,
            }
            for filename, content in examples.items():
                write_regular_file(examples_dir / filename, content)
            logger.debug("Updated %s examples for %s", bot_dir, user_id)

        # Auto-share bot dir back to the user (OCS). Skipped entirely when
        # Nextcloud is unconfigured (local install) — the OCS call is a no-op
        # there and would only log a spurious "Cannot share folder" warning.
        #
        # `auto_share_bot_dir` is the second guard, and it is a deployment
        # shape rather than a preference. On bare metal this share is how the
        # user gets the bot workspace at all. On the Docker shape
        # `provision-nc.sh` gives them a `files_external` mount over the very
        # same directory at first provisioning, so the share would put a second
        # copy of it in their file list under a different name.
        if config.nextcloud.url and config.nextcloud.auto_share_bot_dir:
            bot_path = get_user_bot_path(user_id, bot_dir)
            share_folder_with_user(config, bot_path, user_id)

        return True
    else:
        result = ensure_user_directories(config.rclone_remote, user_id, bot_dir)
        if result:
            logger.debug("Ensured user directories for %s via rclone", user_id)
        return result


def user_directories_exist_v2(config: "Config", user_id: str) -> dict[str, bool]:
    """
    Check which user directories exist (mount-aware).

    Returns dict mapping directory name to existence status.
    """
    if config.use_mount:
        base = _get_mount_path(config, get_user_base_path(user_id))
        subdirs = ["inbox", "memories", config.bot_dir_name, "shared"]
        return {subdir: (base / subdir).exists() for subdir in subdirs}
    else:
        return user_directories_exist(config.rclone_remote, user_id, config.bot_dir_name)


def read_user_memory_v2(config: "Config", user_id: str) -> str | None:
    """
    Read the user's memory file (mount-aware).

    Returns the content of the memory file, or None if it doesn't exist or is empty.

    USER.md reaches every task's prompt, and it lives in a directory bound
    read-write into that user's sandbox, so the read is hardened rather than a
    plain ``read_text`` — see ``read_user_config_file`` (ISSUE-339).
    """
    if config.use_mount:
        content = read_user_config_file(config, user_id, "USER.md")
        if content is None or not content.strip():
            return None
        return content
    else:
        return read_user_memory(config.rclone_remote, user_id, config.bot_dir_name)


def init_user_memory_v2(config: "Config", user_id: str) -> bool:
    """
    Initialize the user's memory file with a template (mount-aware).

    Returns True on success.

    The directory is resolved under the user's own tree and the write refuses
    to follow a link at ``USER.md`` itself. Both halves are needed: the caller
    reaches here precisely when the file reads as absent, and a *dangling*
    symlink is what that looks like (ISSUE-339).
    """
    if config.use_mount:
        config_dir = resolve_user_config_dir(config, user_id)
        if config_dir is None:
            logger.warning("init_user_memory_refused user=%s reason=config_dir", user_id)
            return False
        config_dir.mkdir(parents=True, exist_ok=True)
        return write_regular_file(config_dir / "USER.md", MEMORY_TEMPLATE)
    else:
        return init_user_memory(config.rclone_remote, user_id, config.bot_dir_name)


def ensure_workspace_for_user(config: "Config", user_id: str) -> bool:
    """Seed a user's full workspace (directories + memory template).

    Shared by ``istota setup`` (first-run) and the daemon startup path so both
    guarantee the same layout. Directory creation is idempotent; the USER.md
    memory template is written only when absent (never clobbers existing
    memory on a re-run). Returns True on success.
    """
    ok = ensure_user_directories_v2(config, user_id)
    if get_memory_line_count_v2(config, user_id) is None:
        init_user_memory_v2(config, user_id)
    return ok


def get_memory_line_count_v2(config: "Config", user_id: str) -> int | None:
    """
    Get the line count of a user's memory file (mount-aware).

    Returns None if file doesn't exist.
    """
    content = read_user_memory_v2(config, user_id)
    if content is None:
        return None
    return len(content.splitlines())


def upload_file_to_inbox_v2(
    config: "Config",
    user_id: str,
    local_path: Path,
    remote_filename: str | None = None,
) -> str | None:
    """
    Upload a local file to the user's inbox directory (mount-aware).

    Args:
        config: Application config
        user_id: User ID
        local_path: Local file path to upload
        remote_filename: Optional filename to use on remote (defaults to local filename)

    Returns:
        The remote path on success, None on failure.
    """
    if not local_path.exists():
        return None

    filename = remote_filename or local_path.name
    inbox_path = get_user_inbox_path(user_id)
    remote_path = f"{inbox_path}/{filename}"

    if config.use_mount:
        dst = _get_mount_path(config, remote_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_path), str(dst))
        return remote_path
    else:
        return upload_file_to_inbox(config.rclone_remote, user_id, local_path, remote_filename)


# Date pattern for dated memory files (YYYY-MM-DD.md)
_DATED_MEMORY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def read_dated_memories(
    config: "Config",
    user_id: str,
    max_days: int = 7,
    max_chars: int = 4000,
) -> str | None:
    """
    Read recent dated memory files from a user's memories directory.

    Scans /Users/{user_id}/memories/ for YYYY-MM-DD.md files within max_days,
    concatenates newest-first, and caps at max_chars.

    Returns concatenated content, or None if no dated files found.
    """
    if not config.use_mount:
        return None  # Only supported with mount

    context_dir = _get_mount_path(config, get_user_memories_path(user_id))
    if not context_dir.exists():
        return None

    # Cutoff is computed in the user's timezone so it lines up with the
    # filenames the sleep cycle writes (which are user-local YYYY-MM-DD).
    # Falling back to UTC matches the historical behavior for callers
    # without a configured user timezone.
    # Live DB timezone so the cutoff matches the user-local filenames the
    # sleep cycle writes, even after a web-UI tz change (ISSUE-099).
    tz_name = (
        config.resolve_user_timezone(user_id)
        if hasattr(config, "resolve_user_timezone")
        else "UTC"
    )
    try:
        user_tz = ZoneInfo(tz_name)
    except Exception:
        user_tz = ZoneInfo("UTC")
    cutoff = datetime.now(user_tz) - timedelta(days=max_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    # Find matching files. `is_file()` follows a link, so a symlink at
    # `memories/2026-08-29.md` used to put up to `max_chars` of any
    # daemon-readable file into the prompt — `memories/` is under the same
    # read-write sandbox bind as `config/` (ISSUE-339). `lstat` here and
    # `read_regular_file` below are the two halves; the FIFO case was already
    # closed by accident, since `is_file()` is False for one.
    dated_files = []
    for path in context_dir.iterdir():
        if not _DATED_MEMORY_PATTERN.match(path.name):
            continue
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
        except OSError:
            continue
        date_str = path.stem  # e.g. "2026-01-28"
        if date_str >= cutoff_str:
            dated_files.append((date_str, path))

    if not dated_files:
        return None

    # Sort newest-first
    dated_files.sort(key=lambda x: x[0], reverse=True)

    # Concatenate with headers, respecting max_chars
    parts = []
    total = 0
    for date_str, path in dated_files:
        text, reason = read_regular_file(path)
        if reason is not None:
            logger.warning(
                "dated_memory_read_refused user=%s file=%s reason=%s",
                user_id, path.name, reason,
            )
            continue
        content = (text or "").strip()
        if not content:
            continue
        entry = f"### {date_str}\n\n{content}\n"
        if total + len(entry) > max_chars:
            # Include partial if we have nothing yet
            if not parts:
                remaining = max_chars - total
                parts.append(entry[:remaining] + "...[truncated]")
            break
        parts.append(entry)
        total += len(entry)

    if not parts:
        return None

    return "\n".join(parts)


# =============================================================================
# Channel memory functions
# =============================================================================

CHANNEL_MEMORY_TEMPLATE = """# Channel Memory

This file contains remembered information about this channel/room.
The bot can append to this file to remember things relevant to all participants.

## Notes

"""


_TOKEN_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_conversation_token(token: str) -> str:
    """Validate that a conversation token is safe for filesystem use."""
    if not token or not _TOKEN_PATTERN.match(token):
        raise ValueError(f"Invalid conversation token: {token!r}")
    return token


def get_channel_base_path(conversation_token: str) -> str:
    """Get the base path for a channel's bot-managed directory."""
    validate_conversation_token(conversation_token)
    return f"{CHANNEL_BASE}/{conversation_token}"


def get_channel_memory_path(conversation_token: str) -> str:
    """Get the path to a channel's memory file."""
    return f"{get_channel_base_path(conversation_token)}/CHANNEL.md"


def get_channel_memories_path(conversation_token: str) -> str:
    """Get the path to a channel's dated memories directory."""
    return f"{get_channel_base_path(conversation_token)}/memories"


def ensure_channel_directories(config: "Config", conversation_token: str) -> bool:
    """
    Create the bot-managed directory structure for a channel (mount-aware).

    Creates /Channels/{token}/memories/

    Returns True if directory was created or already exists.
    """
    if config.use_mount:
        base = _get_mount_path(config, get_channel_base_path(conversation_token))
        memories_dir = base / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)

        # Migrate old layout: context/memory.md → CHANNEL.md
        old_memory = base / "context" / "memory.md"
        new_memory = base / "CHANNEL.md"
        if old_memory.exists() and not new_memory.exists():
            shutil.copy2(old_memory, new_memory)
            logger.info("Migrated channel memory %s → %s", old_memory, new_memory)

        logger.debug("Ensured channel directories for %s via mount", conversation_token)
        return True
    else:
        path = get_channel_memories_path(conversation_token)
        if not _rclone_mkdir(config.rclone_remote, path):
            if not _rclone_path_exists(config.rclone_remote, path):
                return False
        return True


def read_channel_memory(config: "Config", conversation_token: str) -> str | None:
    """
    Read the channel's memory file (mount-aware).

    Returns the content of the memory file, or None if it doesn't exist or is empty.

    Hardened on the same terms as USER.md, and for the same reason: CHANNEL.md
    goes into every prompt for a conversation, and ``{mount}/Channels/{token}``
    is bound read-write into the sandbox of every task in that room. That makes
    it the same vector byte for byte — a symlink puts another file's bytes in
    the prompt, and a FIFO blocks prompt assembly where no task timeout reaches
    it (ISSUE-339). Containment is under ``{mount}/Channels`` rather than a user
    root, since a room is not owned by one user; ``validate_conversation_token``
    bounds the *name*, which is not the same question as where it resolves to.

    ``read_regular_file`` decodes as UTF-8 explicitly, which the previous
    ``read_text(encoding="utf-8")`` also did deliberately: the web save hashes
    the content as UTF-8 to build its revision tag, so a locale-dependent decode
    here would make the same bytes hash two ways and every save read as a
    conflict.
    """
    if config.use_mount:
        channel_dir = _contained_channel_dir(config, conversation_token)
        if channel_dir is None:
            logger.warning(
                "channel_memory_read_refused token=%s reason=outside_channel_root",
                conversation_token,
            )
            return None
        content, reason = read_regular_file(channel_dir / "CHANNEL.md")
        if reason is not None:
            logger.warning(
                "channel_memory_read_refused token=%s reason=%s",
                conversation_token, reason,
            )
            return None
        if not content or not content.strip():
            return None
        return content
    else:
        memory_path = get_channel_memory_path(conversation_token)
        content = _rclone_cat(config.rclone_remote, memory_path)
        if content is None or not content.strip():
            return None
        return content


def write_channel_memory(
    config: "Config", conversation_token: str, content: str,
) -> bool:
    """Replace the channel's memory file with `content` (mount-aware).

    Returns False on a write that failed; raises `ValueError` on a token that
    isn't filesystem-safe.

    On the mount the write is tmp + `os.replace`: a reader — the executor
    loading the file into a prompt, or the sleep cycle re-indexing it — must
    never see a half-written file. The caller owns the read-modify-write window
    around it (`memory_md_lock` plus a revision check); this only guarantees the
    write itself is not observable half-done.

    **The staging name is unique per writer, not `CHANNEL.md.tmp`.** `os.replace`
    is atomic but the staging is not, and a fixed name is shared: the memory
    skill CLI computes the byte-identical path for the same target, and its lock
    anchor is per-user (`ISTOTA_DEFERRED_DIR`), so a web save by one member of a
    shared Talk room and a task write by another are genuinely concurrent under
    different locks. Two interleaved writes into one staging file publish a
    mixture of both, which the revision check cannot catch — it guards against a
    lost update, and the tearing happens after it. The per-call staging name
    `atomic_write` mints is what makes the promise above true rather than
    merely intended.

    The rclone branch is **not** atomic: `rclone rcat` streams the object, so a
    concurrent reader can observe a partial one. Nothing local exists to stage
    through there, and the caller's lock does not help because a remote is
    shared across hosts.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_channel_memory_path(conversation_token))
        try:
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            # 0o644: the file is about to become CHANNEL.md, which the user
            # reads over Nextcloud like any other file in their channel dir.
            # `atomic_write` unlinks its staging file on any failure, so no
            # stray dot-file is left sitting in the user's channel directory.
            write_text_atomic(memory_path, content, mode=0o644)
            return True
        except OSError as e:
            logger.warning("channel memory write failed for %s: %s", conversation_token, e)
            return False
    else:
        return _rclone_rcat(
            config.rclone_remote,
            get_channel_memory_path(conversation_token),
            content,
        )


def init_channel_memory(config: "Config", conversation_token: str) -> bool:
    """
    Initialize the channel's memory file with a template (mount-aware).

    Returns True on success.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_channel_memory_path(conversation_token))
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(CHANNEL_MEMORY_TEMPLATE)
        return True
    else:
        return _rclone_rcat(
            config.rclone_remote,
            get_channel_memory_path(conversation_token),
            CHANNEL_MEMORY_TEMPLATE,
        )


# =============================================================================
# Nextcloud OCS sharing functions
# =============================================================================


def share_folder_with_user(config: "Config", folder_path: str, user_id: str) -> bool:
    """
    Share a folder with a Nextcloud user via the OCS Sharing API.

    Creates a user share (shareType=0) with full permissions (read+write).
    Idempotent: checks existing shares first.

    Delegates to nextcloud_client.ocs_share_folder.
    """
    from .nextcloud_client import ocs_share_folder
    return ocs_share_folder(config, folder_path, user_id)
