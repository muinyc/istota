"""Configuration loading for istota."""

import logging
import math
import os
import re
from dataclasses import dataclass, field, replace as _dc_replace
from pathlib import Path
from typing import TYPE_CHECKING

import tomli

from .config_mapper import (
    Hook,
    _KEEP,
    _warn,
    apply_section,
    coerce_float,
    coerce_int,
    report_unknown,
)

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger("istota.config")


@dataclass
class LoggingConfig:
    """Logging configuration."""
    level: str = "INFO"           # INFO or DEBUG
    output: str = "console"       # console, file, or both
    file: str = ""                # log file path
    rotate: bool = True           # enable rotation
    max_size_mb: int = 10         # max file size before rotation
    backup_count: int = 5         # rotated files to keep


@dataclass
class NextcloudConfig:
    url: str = ""
    username: str = ""
    app_password: str = ""
    #: Default expiry applied by `nextcloud share link`. A handed-out link
    #: that never expires is the thing worth avoiding; 0 opts out.
    share_default_expire_days: int = 14
    #: Where the daemon's storage root sits inside the bot account's own
    #: Nextcloud file tree. Empty on bare metal, where they are the same
    #: directory: the rclone remote points at `remote.php/dav/files/<bot>/` and
    #: is mounted at `nextcloud_mount_path`, so `/Users/alice` on disk is
    #: `/Users/alice` over DAV. On the Docker shape `/mnt/shared` is an ordinary
    #: volume that Nextcloud serves through a `files_external` mount, so the
    #: same directory is `/<mount point>/Users/alice` to the bot and every
    #: request that skips the POSIX mount 404s without this.
    #:
    #: Applied in the request layer (`nextcloud/_http.py`), never to
    #: `storage.BOT_USER_BASE` — `_get_mount_path` builds on-disk paths from
    #: the same helper — and never inside `resolve_scoped_path`, which is the
    #: confinement boundary and keeps speaking logical `/Users/{uid}` paths.
    dav_prefix: str = ""
    #: Whether `ensure_user_directories_v2` shares the bot workspace back to
    #: the user over OCS on every boot. True on bare metal, where that share is
    #: how the user gets the directory at all. The Docker shape sets it false:
    #: `provision-nc.sh` already gives the user a `files_external` mount over
    #: the very same directory, and the share would hand them a second copy of
    #: it under a different name.
    auto_share_bot_dir: bool = True


@dataclass
class TalkSignalingConfig:
    """Inbound Talk over the standalone signaling server (the HPB).

    Five keys, one of which an operator normally sets, and **no credential** —
    that absence is the design rather than an omission. istota authenticates
    to the signaling server as its own Nextcloud user, so everything the
    connection needs is minted on demand by Talk: the HPB URL, the hello token
    and the per-room Talk session id all come from calls the bot account can
    already make. The alternative the protocol offers is an *internal client*
    authenticated with the signaling server's shared secret, which joins any
    room on the instance and is rejected for that reason; a field here holding
    that secret would also need an ``_env_secret_overrides`` entry and an
    ``admin_config_view`` redaction, neither of which exists because neither is
    needed.

    ``enabled = true`` on a deployment that cannot do it **refuses to boot**,
    rather than falling back to the poller: Talk in ``internal`` signaling mode
    (no HPB registered) and the ``websockets`` library absent are both refusals,
    because a daemon that quietly polls while an operator believes push is live
    is worse than one that does not start.
    """

    # Off unless the operator has a high-performance backend. A deployment
    # without one keeps today's poll loop, which is the capability floor.
    enabled: bool = False
    # HPB base URL override. Empty is the normal case and means "read `server`
    # from Talk's own signaling settings"; an explicit value exists for a
    # deployment where the daemon must reach the HPB by a different route than
    # the one Nextcloud advertises to browsers.
    url: str = ""
    # Seconds between room reconciliations. This pass is also the safety net —
    # it compares every room's `lastMessage.id` against its stored cursor and
    # fetches only the rooms that are behind — so it bounds recovery when the
    # event stream is down, and it is the worst-case latency for the first
    # message in a brand-new room.
    room_sync_interval: int = 300
    # Reconnect backoff ceiling, seconds. The nginx ingress in front of the HPB
    # drops every connection hourly regardless of traffic, so this path is
    # exercised routinely rather than only on a fault.
    reconnect_backoff_max: int = 60
    # Consume the relayed `chat.comments` payload directly instead of
    # refetching the room. The diff this was gated on is done: measured
    # field-for-field identical on Nextcloud 34 / Talk 24.0.4, across four
    # message shapes, each read both as the bot and as the human. Still off by
    # default, because it is the only part of this design that can be wrong
    # about message *content* rather than about timing, and Talk relays a
    # comment at all only from about Talk 21 — below that every event is a bare
    # refresh, so switching this on changes nothing.
    payload_direct: bool = False


@dataclass
class TalkConfig:
    enabled: bool = True
    bot_username: str = "istota"  # istota's Nextcloud username (to filter own messages)
    signaling: TalkSignalingConfig = field(default_factory=TalkSignalingConfig)


@dataclass
class EmailConfig:
    enabled: bool = False
    # IMAP settings (for receiving)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    # SMTP settings (for sending) - defaults to IMAP credentials if empty
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # Polling settings
    poll_folder: str = "INBOX"
    bot_email: str = ""  # bot's email address (to skip own messages)
    # What an own-address claim buys. SMTP `From:` is unauthenticated, so "this
    # came from the user's own address" is a claim, not evidence (ISSUE-227).
    # Three states, weakest first:
    #
    #   off     the claim is proof. Sound only because something upstream —
    #           normally DMARC enforcement at the receiving MTA — rejected the
    #           forgery before the poller ever saw the folder.
    #   verify  the claim is proof when the receiving MTA's own stamp says so:
    #           a verified, aligned `dmarc=pass` from `authserv_id` proceeds,
    #           anything else is held for an out-of-band yes/no (ISSUE-249).
    #   gate    the claim is never proof. Every self-sent email is held.
    #
    # `off` is the default and is what every deployment ran before `verify`
    # existed; the legacy booleans still load, `false` as `off` and `true` as
    # `gate`. `verify` is the setting that makes the gate usable at all — `gate`
    # is noisy by construction because nothing in a plain SMTP message
    # distinguishes the user from someone claiming to be them, and the MTA's
    # verdict is the signal that finally does.
    #
    # `verify` requires `authserv_id`, and `_validate_confirm_sender_match`
    # refuses to load without it: unscoped, the verdict comes from whichever
    # header sat on top, which in the case that matters is the sender's own. A
    # gate keyed on a value the attacker writes is not a gate.
    confirm_sender_match: str = "off"
    # DMARC canary (ISSUE-228). With `confirm_sender_match` off, the default,
    # the bot treats a `From:` naming one of the user's own addresses as proof
    # that user sent the mail — sound only because something upstream rejected
    # forgeries first. This watches the receiving MTA's own Authentication-Results
    # stamp and warns when that stops holding. Monitoring, not a control: an
    # attacker who forges the topmost header silences it, and the MTA is the
    # boundary regardless.
    dmarc_canary: bool = True
    # Whether *absence* of a DMARC verdict warns. Off by default because a mail
    # path that stamps nothing would otherwise warn on every message. Turn it on
    # when the MTA is known to stamp — that is the only way the "mailbox moved to
    # a provider that does not evaluate DMARC" drift case is visible.
    dmarc_canary_warn_on_missing: bool = False
    # The receiving MTA's own authserv-id — the first field of an RFC 8601
    # Authentication-Results header, before the semicolon. It is what separates
    # our MTA's stamp from one the sender wrote (ISSUE-249). Blank, the default,
    # selects the topmost header and trusts that "topmost" means "ours" — sound
    # while the MTA stamps, and inverted the moment it stops, since element 0 is
    # then whatever the sender put there and a forged `dmarc=pass` reads as a
    # healthy path.
    #
    # Setting it is the operator's statement that their MTA stamps with this id,
    # so a message carrying no header of ours warns on its own, without
    # `dmarc_canary_warn_on_missing`. That flag stays scoped to what it always
    # meant: our stamp is there but carries no DMARC verdict.
    #
    # Blank changes which header is *selected*, and nothing else — but note that
    # the alignment check ISSUE-249 added runs on whichever header is selected,
    # scoped or not. So a deployment on the default config does gain one new
    # warning class on upgrade: a `dmarc=pass` whose `header.from` is not the
    # `From:` domain the mail routed on. That is a real finding rather than
    # noise, and it is why this is not "blank means nothing changed".
    authserv_id: str = ""
    imap_timeout_seconds: int = 30  # socket timeout for IMAP connections (0/unset → 30)
    # Outbound approval floor: the weakest policy any user may run. One of
    # `off` (no holds), `untrusted` (hold unless every recipient is explicitly
    # trusted), `all` (hold unless every recipient is one of the user's own
    # addresses). A user may tighten past the floor but never loosen below it,
    # so this is the operator's minimum rather than a default. `untrusted` on a
    # fresh install: the gate exists because prose rules against committing on
    # the principal's behalf lose to context pressure, and an install that ships
    # with it off has no gate at all. See istota.outbound_policy.
    outbound_approval_floor: str = "untrusted"

    @property
    def effective_smtp_user(self) -> str:
        return self.smtp_user or self.imap_user

    @property
    def effective_smtp_password(self) -> str:
        return self.smtp_password or self.imap_password


@dataclass
class BrowserConfig:
    """Browser container configuration."""
    enabled: bool = False
    api_url: str = "http://localhost:9223"
    vnc_url: str = ""  # external noVNC URL for user access


@dataclass
class DevboxConfig:
    """Per-user devbox container — persistent Linux workbench.

    The ``istota-skill devbox`` CLI speaks the **exec transport** to a server
    running inside ``devbox-<user_id>``, over the per-user socket at
    ``{exec_socket_dir}/{user_id}/exec.sock``. Everything else (image, network,
    volume) is provisioned by Ansible.

    **Nothing here reaches the Docker API from a sandbox any more.** The
    allowlist proxy that used to be bound in at ``/var/run/docker.sock`` is
    retired with its only consumer: ``docker exec`` through it could not return
    an exit status, which is what forced the transport, and once nothing in a
    build needed the socket the bind went and the proxy had no consumer left.
    The one verb still spoken in Docker is ``reset``, which recreates a
    container from this host-side CLI process using the daemon's own
    environment and no ``DOCKER_HOST`` — the real socket, as it always was.

    **``enabled`` is one switch doing two jobs, on purpose.** It offers the
    devbox skill *and* it is what :func:`devbox_container_backend` derives the
    developer skill's execution target from. They used to be separate, and the
    separation only ever produced states nobody wanted: ``enabled = true`` with
    ``[developer.container] backend = "none"`` gave the model a devbox skill
    whose every verb but ``reset`` refused, and the reverse asked the developer
    skill to reach a container the role had not built. A deployment that runs a
    devbox and does not develop in it is not a shape worth a config key.
    """
    enabled: bool = False
    container_prefix: str = "devbox-"           # container name = f"{prefix}{user_id}"
    docker_cli: str = "/usr/bin/docker"         # host path to the Docker CLI binary (`reset` only)
    max_output_bytes: int = 102_400             # stdout/stderr cap per stream in the JSON envelope
    #
    # **There is deliberately no `exec_socket_dir` here.** The skill CLI reads
    # `[developer.container] exec_socket_dir` through `config.exec_socket_path`,
    # the same helper the executor's bwrap bind and the `doctor` transport check
    # use, so there is one spelling of `/run/istota-exec` in the tree. A mirror
    # of it in this block could only ever be dead — `ContainerConfig` carries a
    # non-empty default, so its value always wins — or a second knob for a value
    # the design says must have one.


@dataclass
class ConversationConfig:
    enabled: bool = True
    lookback_count: int = 25
    selection_model: str = "fast"  # role alias — resolves to HAIKU by default; operator-overridable
    selection_timeout: float = 30.0
    skip_selection_threshold: int = 3  # Include all messages if history ≤ this
    use_selection: bool = True  # If False, include all messages without LLM selection
    always_include_recent: int = 5  # Always include this many recent messages without selection
    context_truncation: int = 0  # Max chars per bot response in context (0 to disable)
    context_recency_hours: float = 0  # Include older messages only if within this window (0 to disable)
    context_min_messages: int = 10  # Always include at least this many recent messages regardless of age
    previous_tasks_count: int = 3  # Number of recent unfiltered tasks to inject into context
    talk_context_limit: int = 100  # Messages to fetch from Talk API for context (max 200)


@dataclass
class SchedulerConfig:
    # 5, not 2, because 5 is what every deployment actually runs: the Ansible
    # template, the Docker render, `config.example.toml` and `istota setup` all
    # write it. The dataclass said 2 and the loader's own `.get()` said 5, and
    # nothing reconciled them -- so the value depended on whether a `[scheduler]`
    # header happened to be present, and the local install (whose wizard writes
    # that header with one unrelated key under it) got 5. Resolving to 2 would
    # have quietly put every laptop install on 2.5x the database polling.
    poll_interval: int = 5  # seconds between task queue checks
    dispatch_interval: float = 0.5  # seconds between pending-task dispatch scans within a poll tick (0 or >= poll_interval = legacy single dispatch per tick)
    email_poll_interval: int = 60  # seconds between email polls
    email_poll_batch_size: int = 50  # messages one poll tick will walk. A batch boundary, not a window: the remainder is left for the next tick and drains, rather than falling off the end (ISSUE-250)
    # The inbound email volume budget (ISSUE-250). `bot+{user_id}@domain` is
    # public by construction — it is the From: on every mail the bot sends on a
    # user's behalf — so any past correspondent can turn one SMTP transaction
    # into a paid model invocation on someone else's account. These bound how
    # much of that a user's account will pay for. Over-budget mail is *filed*
    # (`routing_method="throttled"`, left in the mailbox, reachable with
    # `email from-senders`), never dropped. 0 on either count disables it.
    email_rate_limit_messages: int = 60  # email-origin tasks per user per window
    email_sender_rate_limit_messages: int = 20  # …and per (user, sender), so one loud correspondent throttles alone
    email_rate_limit_window_seconds: int = 3600  # the sliding window both counts run over
    # Which queue inbound mail lands on. Background by default: email is the one
    # surface an unauthenticated stranger can create work on, and the one whose
    # latency expectation is loosest (the poll interval alone is 60s), so it
    # should not compete with a live Talk or web-chat turn for the interactive
    # worker slots. "foreground" restores the pre-ISSUE-250 behaviour.
    email_task_queue: str = "background"
    email_confirmation_prompts_per_window: int = 3  # untrusted-sender prompts per (user, sender) per window before they collapse into one notice; 0 = never collapse
    email_max_body_chars: int = 32000  # the body is interpolated whole into the prompt, so one large message is its own amplification; truncated with a marker past this
    email_max_attachment_bytes: int = 26214400  # 25 MiB downloaded+uploaded per message
    email_max_attachment_bytes_per_poll: int = 104857600  # 100 MiB per poll tick, across every message in the batch
    briefing_check_interval: int = 60  # seconds between briefing checks
    tasks_file_poll_interval: int = 30  # seconds between TASKS.md file polls
    shared_file_check_interval: int = 120  # seconds between shared file organization checks
    heartbeat_check_interval: int = 60  # seconds between heartbeat checks
    db_health_check_interval: int = 86400  # seconds between SQLite quick_check sweeps over per-user DBs
    # Seconds between `istota doctor` sweeps. Matters more than the boot run:
    # the drift we actually see happens *after* boot — the auto-update cron
    # pulls code and restarts services every two minutes without running
    # Ansible, so what is installed changes under a config the daemon already
    # loaded. A boot-only check is blind to exactly that. 0 disables the sweep.
    doctor_check_interval: int = 3600
    # Seconds between developer-worktree reaping sweeps (ISSUE-288, 0 = off).
    # A periodic job rather than a task setup hook, deliberately: setup_env
    # hooks are dispatched for every skill in the index regardless of what the
    # task selected, so a sweep there ran before every Talk reply and every
    # heartbeat tick. A delete path belongs on a stated cadence. Six hours is
    # well under the default 24-hour retention window, so nothing waits long
    # after becoming eligible, and well over the cost of a sweep.
    worktree_reap_interval: int = 21600
    # Seconds between package-cache sweeps (ISSUE-317, 0 = off). Inert unless
    # `security.sandbox_cache_dir` is set, since with no configured root there
    # is no cache on disk to bound. Six hours, matching the worktree reap beside
    # it: the two answer the same disk, and a cache that went over its ceiling
    # is not urgent — it is over budget, not broken.
    sandbox_cache_sweep_interval: int = 21600
    # Seconds between Nextcloud profile-picture import ticks (0 = off). Six
    # hours, and the number is a compromise the spec names rather than a round
    # figure: the import runs on a cadence rather than at login (a 10-second
    # Nextcloud timeout in front of authentication) or on render (the live proxy
    # the Nextcloud decoupling is unwinding), so the cost is that a user who has
    # just signed in for the first time sees the initial chip until the next
    # tick. Daily would make that a whole day.
    avatar_import_interval: int = 21600
    # Seconds between per-skill overlay memory-search reindex passes (ISSUE-343,
    # 0 = off). An overlay is a file the *user* writes, so there is no write path
    # to index from — the memory CLI's per-write reindex went with the overlay
    # write verbs, and it never covered a text-editor edit over Nextcloud
    # anyway, which is the authoring mode the file is for. A full directory pass
    # is the only seam that covers every route. Here rather than in the sleep
    # cycle, which is where it first went: `check_sleep_cycles` returns early
    # when `sleep_cycle.enabled` is false and again when the primary brain's
    # breaker is open, and a reindex that makes no brain call has no business
    # behind either gate. Six hours, matching the two sweeps above.
    skill_overlay_reindex_interval: int = 21600
    db_backup_enabled: bool = True  # checkpoint + snapshot local DBs (framework + per-user modules) to the mount so they stay off-host durable now that they've left Nextcloud-synced workspaces
    db_backup_interval: int = 86400  # seconds between DB backup snapshots (default daily)
    db_backup_dir: str = ""  # snapshot destination; empty = {nextcloud_mount}/istota-db-backups. Backup requires a resolvable destination on durable (off-host) storage
    db_backup_retention: int = 7  # number of newest dated snapshot dirs to keep (0 = keep all). Older dirs are pruned, but any dir holding the newest good copy of a DB is protected from pruning
    scheduler_stats_interval: int = 60  # seconds between scheduler_stats health-line emits (0 = disabled)
    loop_stall_alert_seconds: int = 180  # alert if the main dispatch loop hasn't ticked in this long (0 = disabled)
    # Host memory-pressure instrumentation (host_pressure.py). The breadcrumb is
    # a fixed-cadence one-line record of MemAvailable / Shmem / SwapFree / PSI /
    # per-tmpfs usage, written whether or not anything is wrong. The 2026-08-20
    # outage accumulated ~35 MB/hour of unreclaimable shmem for five days and
    # never crossed a threshold until the day it became fatal, so a
    # threshold-gated record could not have seen it — hence unconditional, and
    # hence not gated on a delta either (the flat stretches are what bound when
    # an accumulation started). 288 lines a day at the default interval.
    host_pressure_enabled: bool = True  # master switch for host-pressure sampling
    host_pressure_breadcrumb_interval_seconds: int = 300  # cadence of the breadcrumb line (0 = disabled)
    # Sampling for the admission gate and the threshold snapshot. Separate from
    # the breadcrumb: the breadcrumb is a series and wants a slow, regular
    # cadence, while the gate wants a reading recent enough to act on.
    host_pressure_sample_interval_seconds: int = 30  # cadence of the gate/snapshot sample (0 = disabled)
    host_pressure_psi_threshold: float = 40.0  # `memory some avg10` above this counts as pressure
    host_pressure_alert_cooldown_seconds: int = 900  # min gap between snapshots + admin notifications
    # Third snapshot trigger, from the production series rather than from
    # theory. On 2026-08-21 the host took 1.52 GB of shmem in under five
    # minutes with `memory some avg10` peaking at 0.07 and MemAvailable never
    # below 2.9 GB — zram absorbed it exactly as intended, and both of the
    # triggers above were right not to fire. But that burst is the one event in
    # 24 hours whose attribution anyone would want, and without this key the
    # snapshot could never fire on it. Deliberately not wired into the
    # admission gate: a residue is a reason to collect evidence, not a reason
    # to refuse work. 0 disables. Baseline residue on that host is ~80 MB.
    host_pressure_shmem_unaccounted_alert_mb: int = 1024
    # Read-only GET handle, used only to ask Docker which pid a container has
    # so its tmpfs can be read through /proc/<pid>/root. Named here rather than
    # borrowed from [devbox] because the browser container matters to this
    # module whether or not devbox is enabled. Empty disables container lookup.
    host_pressure_docker_socket: str = "/var/run/docker.sock"
    # Admission gate (C2). Below this, dispatch spawns no new worker and pending
    # tasks stay pending until the next tick. A floor, not a predictor: it makes
    # no estimate of what the new task would need. Running tasks are never
    # touched — the gate is on admission only, never on eviction.
    min_available_memory_mb: int = 768
    # Per-task containment (A6). The gate above refuses *new* work when the
    # host is already squeezed; this bounds what a task that did start can do,
    # which is the half that would have prevented the incident — its trigger was
    # one task's test suite, admitted onto a host with room for it.
    #
    # Inert wherever `Delegate=` has not been applied: `task_cgroup.create`
    # returns None, logs the reason once, and the task spawns exactly as it did
    # before. That is the deployment shape for anything not running the Ansible
    # unit files, so "on by default" here does not mean "enforced by default".
    task_cgroup_enabled: bool = True
    # `memory.max` for a task cgroup. A tree past this is OOM-killed inside its
    # own cgroup: one failed task rather than a global OOM that picks an
    # unrelated victim. 0 leaves memory unbounded (writes `max`), which keeps
    # the cgroup and its other limits.
    task_memory_max_mb: int = 2048
    task_pids_max: int = 512  # `pids.max` — bounds a fork storm
    # `cpu.max` as a percentage of one core (200 = two cores). 0 leaves CPU
    # unbounded and writes no file at all. CPU was never the binding constraint
    # on 2026-08-20 (`cpu full avg10=0`), so this is the original reading of
    # ISSUE-257 rather than a fix for the observed failure.
    task_cpu_max_percent: int = 200
    talk_poll_interval: int = 10  # seconds between Talk polls
    talk_poll_timeout: int = 30  # long-poll timeout for Talk API
    talk_poll_wait: float = 2.0  # max seconds to wait for all rooms before processing available results
    # How often every Talk room is polled regardless of the `lastMessage` gate.
    # Between sweeps only a room the room list says has something new is
    # long-polled. `0` makes every cycle a full sweep, i.e. no gate at all.
    talk_poll_full_sweep_interval: int = 300
    # Progress / event streaming. The event log (task_events table) is the
    # shared bus for all output surfaces; progress_show_* gate whether the
    # executor adapter emits tool_* / progress_text events at all.
    progress_updates: bool = True          # master toggle for Talk progress
    progress_show_tool_use: bool = True    # emit tool_start / tool_end events
    progress_show_text: bool = False       # emit progress_text events (noisy)
    event_log_enabled: bool = True         # write events to task_events table (kill-switch)
    # Narration gate for streamed answer text (stream surfaces — web/repl). A
    # text run emits no text_delta until it crosses this many chars without an
    # intervening tool call; lead-in narration ("Let me check…") stays under it
    # and is discarded at the tool boundary. Higher = fewer narration leaks but
    # short answers token-stream less (they still arrive whole via `result`);
    # lower = more answers animate but longer narration can leak. Watch the
    # `stream_gate:` logs to tune. 0 disables the gate (legacy: deltas stream
    # immediately, narration can leak).
    stream_text_gate_chars: int = 280
    push_notification_threshold_seconds: int = 30  # min task duration before push fires
    push_notification_sources: list[str] = field(default_factory=list)  # source_types that trigger a push; empty = ntfy opt-in only (never a default surface)
    task_timeout_minutes: int = 30  # kill task execution after this
    # Robustness settings
    confirmation_timeout_minutes: int = 120  # auto-cancel pending_confirmation after this
    stale_pending_warn_minutes: int = 30  # log warning for tasks pending longer than this
    stale_pending_fail_hours: int = 2  # auto-fail tasks pending longer than this
    max_retry_age_minutes: int = 60  # don't retry stuck tasks older than this
    worker_heartbeat_seconds: int = 60  # running worker pings liveness this often (0 disables)
    worker_stuck_minutes: int = 10  # reclaim a heartbeating worker's task after this much heartbeat silence (higher = fewer false-dead reclaims of a slow-but-alive worker, slower genuine-crash recovery)
    task_retention_days: int = 7  # delete completed/failed/cancelled tasks older than this
    usage_retention_days: int = 180  # prune token/cost rows after N days, 0 to disable. Deliberately far above task_retention_days — the whole point of a separate table is that spend outlives the task. 180 rather than a year because db_backup snapshots the framework DB into dated dirs and keeps several, so every row is duplicated on the backup target
    email_retention_days: int = 7  # delete emails older than N days from IMAP, 0 to disable
    processed_email_retention_days: int = 90  # prune the processed_emails dedup ledger after N days, 0 to disable. Never applied below email_retention_days — a row is what stops a message still in the mailbox from being re-ingested
    temp_file_retention_days: int = 7  # delete temp files older than N days, 0 to disable
    worker_idle_timeout: int = 10    # cumulative-idle seconds a worker lingers (re-checking) before exiting
    worker_idle_poll_interval: float = 0.5  # idle re-check cadence (0 or >= worker_idle_timeout = legacy single coarse wait + recheck)
    main_loop_read_timeout_ms: int = 2000  # busy_timeout for the dispatch scan's read-only queries; a lock past this skips the tick instead of blocking the loop 30s (0 = keep the 30s default)
    max_foreground_workers: int = 5  # instance-level foreground (interactive) worker cap
    max_background_workers: int = 3  # instance-level background (scheduled/briefing) worker cap
    user_max_foreground_workers: int = 2  # global per-user fg worker default
    user_max_background_workers: int = 1  # global per-user bg worker default
    # Elapsed-time slot reclassification (C1). A *running* foreground task older
    # than the threshold stops counting against the user's interactive cap and
    # counts against a separate long allowance instead; the task itself is not
    # touched. Reactive rather than predictive on purpose — nothing observable
    # at enqueue time separates "flex the developer skill on a worktree" from
    # "what time is my meeting", and the task that caused the 2026-08-20
    # head-of-line block arrived as an ordinary chat message. No completed
    # foreground task crossed ten minutes in the seven days to 2026-08-20, so
    # the default cannot misfire on an ordinary turn. 0 disables.
    long_task_threshold_minutes: int = 10
    # Per-user cap on *discounted* long tasks. Additive: the per-user foreground
    # thread ceiling becomes user_max_foreground_workers + this. It bounds
    # discounts, not long tasks — a task becomes long while already running, so
    # the cap cannot refuse it retroactively, and long tasks beyond it keep
    # counting as interactive occupancy. 0 disables.
    user_max_long_workers: int = 1
    # Instance-wide budget of discounts, partitioned *inside*
    # max_foreground_workers rather than added to it: total foreground threads
    # stay capped exactly as before, with at most this many of them discounted.
    # The box's worst-case memory exposure is the subject of this whole feature
    # and must not grow to buy per-user fairness. 0 disables, and like the two
    # above it skips dispatch's per-tick query rather than discarding its result.
    max_long_workers: int = 2
    scheduled_job_max_consecutive_failures: int = 5  # auto-disable after N failures (0 = never)
    # Insertion-time staleness gate for cron-driven tasks. When the daemon
    # comes back from a long outage, jobs and briefings whose computed
    # next_run is older than this threshold are skipped (last_run_at bumped
    # to now so the schedule resumes cleanly) instead of all firing on the
    # first tick. 0 = unlimited (legacy unconditional catch-up).
    cron_max_staleness_minutes: int = 60
    max_subtasks_per_task: int = 10  # cap deferred subtask creations per task (prompt-injection blast radius)
    max_subtask_depth: int = 3  # reject deferred subtask creation when parent chain is this deep (0 = unlimited)
    max_subtask_prompt_chars: int = 8000  # skip deferred subtasks whose prompt exceeds this (0 = unlimited)
    talk_cache_max_per_conversation: int = 200  # max cached talk messages per conversation
    location_ping_retention_days: int = 365  # delete location pings older than this (0 = unlimited)
    log_channel_show_skills: bool = True  # include selected skills in log channel messages


@dataclass
class SleepCycleConfig:
    """Sleep cycle (nightly memory extraction) configuration."""
    enabled: bool = True
    cron: str = "0 2 * * *"  # 2am in user's timezone
    memory_retention_days: int = 0  # 0 = unlimited retention
    lookback_hours: int = 24
    auto_load_dated_days: int = 3  # auto-load N days of dated memories into prompts (0 = disabled)
    curate_user_memory: bool = False  # nightly USER.md curation from dated memories
    curation_log_summary: bool = True  # post one-line summary to user's log_channel after applied ops
    extraction_model: str = "general"  # role alias — resolves to SONNET by default; operator-overridable
    curation_model: str = "general"  # role alias — resolves to SONNET by default; operator-overridable
    # Independent of memory_retention_days so default deployments still
    # prune the audit table — KG audit rows are tiny but accumulate
    # several per night per user. 0 = unlimited.
    knowledge_graph_audit_retention_days: int = 365


@dataclass
class ChannelSleepCycleConfig:
    """Channel-level sleep cycle (memory extraction from shared conversations)."""
    enabled: bool = True
    cron: str = "0 3 * * *"  # UTC (after user sleep cycles)
    lookback_hours: int = 24
    memory_retention_days: int = 0  # 0 = unlimited retention
    extraction_model: str = "general"  # role alias — resolves to SONNET by default; operator-overridable


@dataclass
class BriefingConfig:
    """Briefing configuration."""
    name: str
    cron: str  # cron expression, evaluated in user's timezone
    # Display title for the rendered briefing (email subject, archive entry,
    # ntfy title). Blank = derive from ``name`` (``morning`` → "Morning
    # Briefing"). The run date is appended by the renderer, so this is the
    # stable part only. See ``briefings.generate.resolve_briefing_title``.
    title: str = ""
    conversation_token: str = ""  # Talk room to post to
    output: str = "talk"  # delivery surface(s): talk / email / ntfy or a comma list
    components: dict = field(default_factory=dict)
    # Config-authored rich block/source shape (``[[users.X.briefings.blocks]]``).
    # In-memory only — never persisted to ``briefing_configs``; consumed once by
    # the module-DB seeder (``briefings/_migrate``) as an editable baseline.
    # ``compare=False`` keeps it out of ``ensure_briefing`` no-op equality (block
    # edits must not perturb the framework row); ``repr=False`` keeps it terse.
    blocks: list[dict] = field(default_factory=list, repr=False, compare=False)
    # Marks entries appended by ``_apply_user_briefings`` from the DB. The
    # web listing endpoint skips these so post-delete in-memory staleness
    # cannot resurface a removed briefing as "managed=config".
    from_db: bool = field(default=False, repr=False, compare=False)


@dataclass
class BriefingSharedBlock:
    """A module-owned shared briefing block (shared-kv-curated-content spec).

    A one-block briefing generated *once globally* (no user) under the reserved
    ``__system__`` identity, whose rendered content is written into ``shared_kv``
    at namespace ``briefing_shared_blocks`` / key ``name``. Per-user briefings
    reference it via a ``shared_block`` (or ``kv``) source, collapsing N-way
    duplicate fetch + synthesis to one generation total.

    ``sources`` is a list of ``{"kind": ..., "config": {...}}`` dicts. Only
    user-agnostic kinds (``browse``/``markets``/``email``) are usable; others
    (notably ``rss``, which needs a real feeds user, and the personal built-ins)
    are dropped at generation time. In-memory config only (like
    ``default_briefings``); never persisted to a per-user table — shared blocks
    are global.
    """
    name: str
    # Cron evaluated in the configured shared-block timezone
    # (``[briefings] shared_block_timezone``, default UTC) — global, no
    # per-user timezone.
    cron: str
    title: str = ""
    directive: str | None = None
    render_mode: str = "synthesis"
    enabled: bool = True
    # Whether the generated content renders un-wrapped in a consuming briefing.
    # Set by the admin/writer, honored from the stored shared_kv value — never by
    # a consuming user. Default False → content is untrusted-wrapped, protecting
    # every reader from an injection riding in via one admin's web-derived block.
    trusted: bool = False
    sources: list[dict] = field(default_factory=list)


# Batteries-included canonical shared blocks. Seeded when config declares no
# ``[[briefing_shared_blocks]]`` (parity with how ``[[default_briefings]]`` is
# operator/Ansible-provided). ``world-headlines`` needs the headless browser
# (soft-degrades to an omitted section when it's off); ``markets-summary`` needs
# only yfinance. Both regenerate twice daily in the configured shared-block
# timezone (default UTC), ~15 min before the default 06:00 / 18:00 briefing
# windows, so morning and evening briefings each read a fresh copy; a consuming
# briefing reads them via a ``shared_block`` source with a freshness window,
# so a stale/not-yet-generated block degrades to an omitted section
# (harmless). Kept in step with the Ansible defaults
# (``istota_briefing_shared_blocks`` in ``deploy/ansible/defaults/main.yml``).
DEFAULT_SHARED_BLOCKS: list[dict] = [
    {
        "name": "world-headlines",
        "cron": "45 5,17 * * *",
        "title": "🌍 Headlines",
        "directive": (
            "Synthesize the frontpages into ~8 top world stories, lead with "
            "what's new. Neutral wire-service tone."
        ),
        "render_mode": "synthesis",
        "enabled": True,
        "sources": [
            {"kind": "browse", "config": {"preset": "ap"}},
            {"kind": "browse", "config": {"preset": "reuters"}},
        ],
    },
    {
        "name": "markets-summary",
        "cron": "50 5,17 * * *",
        "title": "📈 Markets",
        # Structured/verbatim: the markets source already emits a formatted
        # emoji quote table; store it as-is with zero LLM passes (no directive).
        # Trusted — pure numbers, no free-text injection surface — so it renders
        # un-wrapped. Every other default/seeded block stays untrusted.
        "directive": "",
        "render_mode": "structured",
        "enabled": True,
        "trusted": True,
        "sources": [
            {"kind": "markets", "config": {}},
        ],
    },
]


@dataclass
class ResourceConfig:
    """User resource configuration (per-user TOML ``[[resources]]`` blocks).

    After the Resources sunset only ``folder`` (an out-of-workspace sandbox
    mount) and ``shared_file`` (internal organizer state) are live types.
    Obsolete credential types (karakeep, monarch, overland, ...) survive only
    in the load-time migration window (``_allow_obsolete=True``) so their
    credentials can be absorbed into the secrets table; calendar /
    email_folder / notes_folder rows are inert and auto-cleaned. todo_file /
    reminders_file are inert too but are *not* auto-cleaned: the fetcher that
    read reminders_file was deleted once the briefings module took over
    reminder selection, and nothing ever read todo_file. They are parsed and
    stored so an existing row survives a config load untouched, and removing
    them is a data migration rather than dead-code cleanup. Service
    credentials live in ``extra`` (read by ``secrets_store``) or the encrypted
    secrets table, not flat fields.
    """
    type: str
    path: str = ""
    name: str = ""
    permissions: str = "read"
    # Arbitrary extra fields (unrecognized TOML keys, incl. base_url/api_key
    # for the obsolete credential migration, land here).
    extra: dict = field(default_factory=dict)
    # Marks entries appended by ``_apply_user_resources`` from the DB. The
    # web listing endpoint skips these so post-delete in-memory staleness
    # cannot resurface a removed row as "managed=config".
    from_db: bool = field(default=False, repr=False, compare=False)
    # Set by the TOML/DB loaders so the migration step can construct obsolete
    # types in flight (it absorbs their credentials into the secrets table
    # and then drops the rows). Tests that bypass load_config see the guard.
    _allow_obsolete: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._allow_obsolete:
            return
        from . import db as _db
        if self.type in _db._OBSOLETE_RESOURCE_TYPES:
            raise ValueError(
                f"ResourceConfig type {self.type!r} was retired (modules "
                f"refactor or Resources sunset). Live data flows through "
                f"is_module_enabled (feeds, money, location), the encrypted "
                f"secrets table (karakeep, monarch, overland), or workspace "
                f"conventions (todo/reminders/notes). Update the fixture or "
                f"pass _allow_obsolete=True if this is a load-time migration "
                f"path."
            )


@dataclass
class UserConfig:
    """Per-user configuration."""
    display_name: str = ""  # friendly name for prompts
    email_addresses: list[str] = field(default_factory=list)  # for email-to-user mapping
    timezone: str = "UTC"  # user's timezone for briefing scheduling
    briefings: list[BriefingConfig] = field(default_factory=list)
    resources: list[ResourceConfig] = field(default_factory=list)
    log_channel: str = ""  # Talk room token for verbose task execution logs
    alerts_channel: str = ""  # Talk room token for confirmations and alerts
    max_foreground_workers: int = 0  # per-user fg worker override (0 = use global default)
    max_background_workers: int = 0  # per-user bg worker override (0 = use global default)
    disabled_skills: list[str] = field(default_factory=list)  # skills to exclude from selection
    trusted_email_senders: list[str] = field(default_factory=list)  # patterns for trusted senders
    quiet_email_senders: list[str] = field(default_factory=list)  # patterns whose mail is filed silently (no task)
    disabled_modules: list[str] = field(default_factory=list)  # modules to disable (default-on otherwise)
    routing: dict[str, str] = field(default_factory=dict)  # purpose -> output_target descriptor
    default_destination: str = "talk"  # fallback delivery descriptor
    email_reply_routing: str = "origin+thread"  # origin+thread | origin | thread
    # Outbound approval policy: "" (unset — follow the operator floor) | off |
    # untrusted | all. Unset rather than a concrete default so that raising
    # [email] outbound_approval_floor reaches every user who never touched it.
    outbound_approval: str = ""
    # How an external-origin turn renders in web chat: full | collapsed | hidden.
    # Body only — the turn itself is always in the transcript, because a bot
    # answer with no question above it is the defect the inbound mirror exists
    # to fix (ISSUE-136).
    external_turn_display: str = "collapsed"
    default_briefings: bool = True  # seed the shared [[default_briefings]] set into this user
    briefing_email_html: bool = True  # briefing email as multipart/alternative (HTML + plain)
    timezone_follow_location: bool = False  # follow the GPS timezone on travel (opt-in; ISSUE-096)


@dataclass
class MemorySearchConfig:
    """Memory search configuration."""
    enabled: bool = True
    auto_index_conversations: bool = True
    auto_index_memory_files: bool = True
    auto_recall: bool = False  # BM25 search using task prompt as query
    auto_recall_limit: int = 5  # max results for auto-recall
    # ISSUE-109 #1 — half-life (days) for recency decay applied to recall
    # results so a dense old cluster can't dominate on mass. 0 = no decay.
    recency_half_life_days: float = 180.0


@dataclass
class PlaybooksConfig:
    """Learned-playbook (procedural memory) configuration (Part B).

    A playbook is a per-user markdown procedure distilled by the sleep cycle
    from a successful multi-step task, recalled by relevance through the memory
    search path. Off by default; the master gate is ``enabled``.
    """
    enabled: bool = False           # master gate for Part B
    recall_limit: int = 3           # top-K playbooks injected per task
    min_tool_calls: int = 4         # a task must use >= this many tools to qualify
    retention_days: int = 90        # 0 = keep forever; >0 = age-prune by last-use
    max_chars: int = 0              # 0 = share the global max_memory_chars budget


@dataclass
class ReviewConfig:
    """`[developer.review]` — the code_review CLI's models, caps and budget.

    Distinct from `skills.code_review.engine.ReviewConfig`, which carries only
    the sizing and caps the engine works from. Keeping them apart is what lets
    every engine function be tested without importing this module; the CLI
    builds the engine's from this one.

    Defaults are chosen so an operator who sets nothing gets a working review on
    a repository they have already enabled `[developer]` for. `enabled = false`
    is the off switch — there is no separate feature flag, because the skill is
    already gated by `developer.enabled` and an admin check.
    """

    enabled: bool = True
    # Role aliases. A `:effort` modifier is honoured: the CLI splits it off with
    # `split_effort` and passes it as `BrainRequest.effort`, because
    # `resolve_model_name` strips the modifier and keeps only the base, so a
    # value handed to it whole would silently run at default effort.
    conformance_model: str = "general"
    bughunt_model: str = "smart:high"
    both_agents_threshold_lines: int = 150
    # Matched against changed paths as case-insensitive substrings. A hit puts
    # both reviewers on the diff however small it is.
    boundary_patterns: list[str] = field(default_factory=lambda: [
        "auth", "secret", "credential", "token", "password",
        "migration", "schema.sql", "billing", "payment", "money",
        "crypto", "sandbox", "proxy", "deploy", "ansible",
    ])
    max_diff_chars: int = 200_000
    max_context_chars: int = 60_000
    # Per changed file, for whole-body inclusion; over it, that file falls back
    # to its own hunks.
    max_file_chars: int = 20_000
    max_callers_per_symbol: int = 8
    # Files a reviewer may request on the one re-invocation; 0 disables the
    # round trip, and the offer is then kept out of the reviewer's prompt rather
    # than made and refused. A round trip spends a second model round, so it is
    # also withheld when `max_calls_per_task` has no room for one.
    max_need_files: int = 6
    # Per agent. Both agents run concurrently, so this is wall time and not half
    # of it, and a fast reviewer finishing early buys the slow one nothing.
    #
    # 480 rather than 120 (ISSUE-448). `bughunt_model` is `smart:high` and
    # `size_review` only puts that reviewer on diffs over
    # `both_agents_threshold_lines`, so the expensive reviewer runs exclusively
    # on the large diffs — and 240 was measured killing it on every real one.
    # The two errors are not symmetric: a budget that is too large costs wall
    # time on a reviewer that was going to fail anyway, while one that is too
    # small guarantees the call is paid for and discarded. So this is set
    # generously and bounded by the proxy ceiling rather than tuned. There is
    # no per-agent split: once the ceiling stops binding, one budget large
    # enough for bughunt is large enough for conformance, and the agents are
    # concurrent so conformance finishing early costs nothing.
    timeout_seconds: int = 480
    # Review rounds per task, where a round is one *wave* of model calls rather
    # than one `code_review run`: a run charges 1, or 2 when a reviewer took its
    # `max_need_files` round trip. A wave is up to four invocations, since each
    # of two agents may retry a malformed answer once — so one run is at most
    # two rounds and six invocations. Guard refusals and breaker skips are free
    # and a malformed-output retry rides on the round that provoked it, because
    # a run that spends every call and parses none must still charge, or a
    # reviewer stuck answering in prose loops past a cap that never moves.
    # 0 or less permits
    # no reviews at all, matching `max_need_files` above rather than reading as
    # "unlimited"; use `enabled = false` to switch the feature off. At the cap
    # the review degrades to `skipped` rather than erroring, because a blocking
    # cap would stop a task that had already finished its work from landing it.
    max_calls_per_task: int = 8


#: The commands the container backend fronts with a shim. Named, never
#: inferred, and two absences are deliberate.
#:
#: ``python3`` is not here: the sandbox launches its own network bridge as
#: ``python3 {bridge_path}`` inside the namespace, several recipes in
#: ``developer/skill.md`` parse forge output with ``python3 -c``, and the exec
#: client is itself a Python script. Shimming it routes all three into a
#: container that has none of them.
#:
#: ``make`` is not here either, and that one is a routing argument rather than
#: a plumbing one. Shimming a *driver* inverts routing for everything beneath
#: it: once ``make`` runs in the container, nothing it invokes routes back,
#: because the shim directory exists only in the sandbox's ``PATH``. A Makefile
#: calling ``git``, ``gh``, ``python3`` or ``istota-skill`` would get the
#: container's copies — no credential helper, no forge policy wrapper, possibly
#: no interpreter. Left on the host, each ``npm`` or ``cargo`` it invokes routes
#: individually, which is the correct semantics; it breaks only where a Makefile
#: calls ``./node_modules/.bin/foo`` by path, which is loud and narrow. The key
#: is configurable for an operator who knows their Makefiles.
DEFAULT_SHIM_COMMANDS: tuple[str, ...] = (
    "npm", "npx", "pnpm", "yarn", "node",
    "uv", "uvx", "pip", "pip3",
    "cargo", "rustc", "rustup",
    "go", "bundle", "gem",
)

#: Labels for where development work runs. **Derived, never configured** —
#: see :func:`container_backend`. They were the values of a
#: ``[developer.container] backend`` key, which is retired: a deployment could
#: hold ``[devbox] enabled = true`` alongside ``backend = "none"``, which
#: offered the model a devbox skill whose every verb refused, and the reverse
#: pairing asked the developer skill to reach a container the role never built.
#: Neither is a shape anyone wants, so the pair is now one switch.
CONTAINER_BACKEND_NONE = "none"
CONTAINER_BACKEND_DEVBOX = "devbox"
CONTAINER_BACKENDS = (CONTAINER_BACKEND_NONE, CONTAINER_BACKEND_DEVBOX)


@dataclass
class ContainerConfig:
    """``[developer.container]`` — where project code builds and runs.

    A **deploy-time** choice, not a runtime one. Within a deployment there is
    exactly one place a build happens, which is what keeps the property a
    per-command fallback would cost: nothing on the host ever consumes an
    environment the container built, so no parity rule has to hold.

    **Whether** the commands in ``shim_commands`` are routed into the user's
    devbox over the exec transport is not settable here. It is derived, by
    :func:`container_backend`, from ``[devbox] enabled`` together with
    ``developer.enabled`` and a non-empty ``developer.repos_dir``. This table
    configures the transport; it does not decide that there is one.

    **It is not, however, "nothing changed".** ``developer.repos_dir`` became a
    per-user root in the same change, and that applies on every backend —
    including this one — because it is what closes cross-user worktree access
    rather than anything to do with containers. An upgraded host has to move its
    existing clones down a level (the Ansible role does it) before the developer
    skill can see them again.

    ``exec_socket_dir`` is the **parent**; the socket is
    ``{exec_socket_dir}/{user_id}/exec.sock``, and only the per-user
    subdirectory is ever mounted into a container — mounting the parent would
    put every user's socket in every user's container, which is arbitrary
    command execution against another user's repositories.
    """
    exec_socket_dir: str = "/run/istota-exec"
    # The client's connect budget, and the only timeout on the connect path.
    connect_timeout_seconds: float = 5.0
    # Server-side backstop reap for a connection with no traffic in either
    # direction. Deliberately longer than most values of
    # `scheduler.task_timeout_minutes`, so in practice it only ever fires on an
    # orphan whose task is already gone. That is the right shape for a backstop:
    # do not "fix" it to something aggressive enough to kill a long link step.
    idle_timeout_seconds: int = 3600
    shim_commands: list[str] = field(
        default_factory=lambda: list(DEFAULT_SHIM_COMMANDS)
    )


@dataclass
class DeveloperConfig:
    """Developer skill configuration for git + GitLab/GitHub workflows."""
    enabled: bool = False
    # Base directory for repo clones/worktrees. **A per-user root**: the daemon
    # derives `{repos_dir}/{user_id}` (`executor.get_user_repos_dir`) and
    # everything that scopes a task — the bwrap bind, the native file-tool write
    # root, the `DEVELOPER_REPOS_DIR` the developer skill's `setup_env` emits,
    # `git_remote_scrub` — is handed that
    # rather than this. One rule closing three holes: a devbox mounting the
    # global root would give user B write access to user A's worktrees; a
    # non-admin with a devbox would reach past the admin-only bwrap bind
    # through the transport; and a shared uv cache under it would re-create the
    # cross-user unpacked-wheel path `resolve_sandbox_cache_dir` was written to
    # remove.
    repos_dir: str = ""
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""        # API token (read_api + write_repository scope recommended)
    gitlab_username: str = ""     # GitLab username for HTTPS auth
    gitlab_default_namespace: str = ""  # Default namespace for resolving short repo names (e.g., "myorg")
    # The reviewer `glab mr create --reviewer` is given. It resolves by
    # username, so this is a username (ISSUE-289).
    gitlab_reviewer: str = ""     # GitLab username to assign as MR reviewer
    # The same person's numeric user id. Nothing reads it: it is here because
    # operators have it recorded, the REST paths that want it may come back,
    # and dropping the key would silently discard the value on the next
    # Ansible run. Its name was the whole bug — it used to be the field the
    # skill consumed, so operators dutifully put a number where `glab` needed
    # a name and every MR opened unassigned.
    gitlab_reviewer_id: str = ""  # GitLab numeric user id, recorded not consumed
    github_url: str = "https://github.com"
    github_token: str = ""        # Personal access token (repo scope recommended)
    github_username: str = ""     # GitHub username for HTTPS auth (defaults to x-access-token if empty)
    github_default_owner: str = ""  # Default org/user for resolving short repo names
    github_reviewer: str = ""     # GitHub username to request as PR reviewer
    author_credit: str = ""       # Appended to every commit message (e.g., "Co-Authored-By: Name <email>")
    # Forge CLI wrapper (src/istota/forge_cli.py). The real `gh` and `glab`
    # run behind a wrapper that injects the token and checks the argv against
    # a policy. The policy is code-owned rather than config-owned because it
    # is a safety default, not a preference; these two knobs extend and
    # puncture it. Entries are written as they would be typed —
    # "gh repo view" — and an entry with no binary name applies to both.
    forge_cli_extra_denied: list[str] = field(default_factory=list)
    # Removes a baseline entry. Documented as turning off an accident guard,
    # because that is what it does. An entry matching no baseline rule and no
    # forge_cli_extra_denied entry is warned about at startup: a hatch that
    # silently stopped matching reads exactly like one that is still open.
    forge_cli_permit: list[str] = field(default_factory=list)
    # Where the wrapper execs the real binary. The Ansible role renders these
    # from what it installed (the Debian archive puts both in /usr/bin); these
    # defaults are the conventional manual-install location for everything
    # else. Neither is authoritative — `developer._resolve_real_bin` falls back
    # when the configured path does not exist, so a host whose code and
    # config.toml are out of step still works: first to the location the docker
    # image installs (`/usr/local/lib/istota_forge/`, off PATH so the wrapper
    # stays the only one resolvable by name), then to the daemon's own PATH.
    # The docker entrypoint also renders these two keys, but only on a first
    # boot with a fresh volume, which is why the probe exists rather than the
    # rendered value being relied on.
    gh_bin_path: str = "/usr/local/bin/gh"
    glab_bin_path: str = "/usr/local/bin/glab"
    # Devbox credential proxy. See src/istota/devbox_proxy.py + the
    # `devbox-credential-proxy` spec for the design. It answers two things
    # for the container: a git credential (injected server-side, so git
    # never holds the token) and, for `gh` / `glab`, the forge token itself
    # — those run the real binaries behind forge_cli.py and need it in
    # their own environment.
    devbox_proxy_enabled: bool = True
    devbox_proxy_socket_dir: str = "/var/run/istota"
    devbox_proxy_audit_log: str = ""   # empty = journal only; set to a path for file fan-out
    # Worktree reaping (ISSUE-288, src/istota/worktree_reaper.py). Nothing used
    # to remove a task's worktree, so `repos_dir` accumulated gigabyte
    # checkouts with no owner. The sweep runs from the *scheduler*, on
    # `scheduler.worktree_reap_interval` — not from the developer skill's
    # `setup_env`, because `dispatch_setup_env_hooks` calls every skill's hook
    # whatever the task selected, so a sweep there fired before every Talk
    # reply, cron job and heartbeat tick. It removes only a worktree that is
    # clean, unlocked, idle for the retention window and carrying no commit
    # that is not already upstream.
    worktree_reap_enabled: bool = True
    # Hours of no activity before a worktree is a candidate. This is what
    # protects a *concurrently running* task: tasks for one user run in
    # parallel and none knows the others exist, so recent activity is the only
    # evidence available that a checkout is in use. Clamped to a one-hour
    # floor — a shorter window reaps the checkout a task is still setting up.
    worktree_retention_hours: float = 24.0
    review: ReviewConfig = field(default_factory=ReviewConfig)
    container: ContainerConfig = field(default_factory=ContainerConfig)


@dataclass
class LocationReceiverConfig:
    """Location receiver (Overland GPS) configuration."""
    enabled: bool = False
    webhooks_port: int = 8765
    accuracy_threshold_m: float = 100.0  # drop pings with accuracy worse than this from place matching
    visit_exit_minutes: float = 5.0       # continuous "away" time before a visit is closed
    reconcile_enabled: bool = True         # re-derive closed visits from pings periodically
    reconcile_lookback_hours: float = 6.0  # reconcile pings within this window
    reconcile_buffer_minutes: float = 10.0  # don't reconcile pings newer than this (safety margin)
    reconcile_grace_minutes: float = 10.0  # time away before an unassigned ping closes a visit
    reconcile_min_pings: int = 3            # minimum at-place pings to count as a visit
    reconcile_min_dwell_sec: int = 60       # minimum duration (sec) to count as a visit


@dataclass
class WebChatConfig:
    """In-app web chat surface (``[web.chat]``).

    Always-on companion to Talk — there is no per-user opt-out. Knobs cap
    prompt size, attachment size, and the per-user message rate; the poll
    intervals tune the SSE generator cadence and the client polling fallback.
    """
    max_prompt_chars: int = 32000
    max_attachment_mb: int = 25
    # `webm` is what MediaRecorder produces on Chrome/Firefox for the
    # composer's voice-message button (iOS records audio/mp4, which we name
    # `.m4a`). Audio lands as an ordinary attachment and is transcribed by
    # `executor._pre_transcribe_attachments` on the way into the prompt.
    #
    # `heic` is what an iPhone photo actually is. The mobile shell's gallery
    # picker re-encodes to JPEG so one normally never arrives, but that is the
    # picker's behaviour rather than a guarantee — a HEIC dragged in from a Mac,
    # or a shell whose picker changed underneath it, would otherwise be refused
    # for its extension with no hint that the format was the problem.
    attachment_extensions: list[str] = field(default_factory=lambda: [
        "pdf", "png", "jpg", "jpeg", "heic", "webp", "gif", "txt", "md",
        "csv", "wav", "mp3", "m4a", "ogg", "webm", "docx", "xlsx",
    ])
    rate_limit_messages: int = 30
    rate_limit_window_seconds: int = 300
    sse_poll_interval_ms: int = 200
    client_poll_interval_ms: int = 1500
    # Live room-event stream (GET /chat/stream). One user-scoped SSE connection
    # per open tab carries every room the user is a member of, so a Talk turn /
    # routed alert / background-room message reaches the browser in about a
    # second instead of riding a 5s client poll.
    #
    # The server-side poll cadence is deliberately slower than
    # `sse_poll_interval_ms`: these are message-level events, and 200ms would
    # quintuple idle cost for no perceptible gain.
    room_stream_poll_interval_ms: int = 1000
    # SSE comment frame cadence. A session-lived stream can idle for hours; the
    # deployed nginx allows 3600s but a corporate proxy / mobile network drops a
    # silent connection far sooner, and the drop is invisible client-side until
    # data goes missing. Also gives `request.is_disconnected()` a regular chance
    # to observe a vanished client.
    room_stream_keepalive_seconds: int = 20
    # Server-side resource guard (the "gap threshold"). A far-behind cursor —
    # `?since_id=0`, or a tab resumed from suspend — must not dump every message
    # the user can see. `max_batch` is the outer LIMIT that stops the query
    # pulling megabytes; `max_bytes` is an accumulate-and-truncate budget during
    # serialization (a joined assistant row carries `execution_trace`, so row
    # count is a poor proxy for cost). Tripping either emits a `gap` frame and
    # the client reloads instead of replaying the backlog.
    room_stream_max_batch: int = 500
    room_stream_max_bytes: int = 2_000_000
    # Cadence for the per-connection room-metadata diff (rename / model / effort
    # / room added or removed elsewhere). Runs off the message cursor, so it
    # costs one membership join per connection per interval. 0 disables.
    room_stream_room_check_seconds: int = 10
    # Talk→web read-state pull cadence (seconds). At most one Nextcloud
    # conversation-list fetch per user per interval, piggybacked on the web
    # rooms poll. 0 disables the pull (web→Talk push is unaffected). Only
    # active when [web] token_storage = "encrypted" and the web token key
    # is provisioned.
    talk_read_sync_interval: int = 60


@dataclass
class WebMapConfig:
    """Where the map surfaces get their background tiles.

    A seam rather than a literal (ISSUE-334): the location maps used to name
    CARTO's tile host in the frontend bundle, and when CARTO started
    watermarking keyless requests there was no way to change it without a code
    edit. Resolution lives in `istota.map_basemap`, which the web endpoint and
    the `web.basemap` doctor check both read, so the checker cannot pass while
    the map is blank.

    `api_key` is disclosed by construction — MapLibre puts it in the tile URL,
    so it ships to every browser that loads a map. Treat it as public. It is
    accepted here for an operator who prefers one deployment-wide value in
    config; the per-user key set from the location settings page is stored in
    the encrypted `secrets` table and wins over this one.
    """
    # openfreemap (default, keyless) | carto | osm | custom.
    provider: str = "openfreemap"
    # carto only. Public — it travels in the tile URL to every browser.
    api_key: str = ""
    # custom only: MapLibre style URLs the operator serves themselves. One is
    # enough; it then covers both themes.
    dark_style: str = ""
    light_style: str = ""
    attribution: str = ""


@dataclass
class WebConfig:
    """Authenticated web interface configuration.

    Auth uses Nextcloud's built-in OAuth2 provider (no extra NC apps required).
    Auth-only flow: code exchange → identity check via OCS → discard token.
    """
    enabled: bool = False
    port: int = 8766
    # Authentication mode. "nextcloud" (default) uses the NC OAuth2 flow below.
    # "none" bypasses auth entirely for a single-user local install — every
    # request is the one configured local user, who is always admin. no-auth
    # is only permitted on a loopback bind (the web app refuses to start
    # otherwise). Overridable by ISTOTA_WEB_AUTH.
    auth: str = "nextcloud"
    # `oauth2_provider` is the user-facing NC URL — what the browser hits to
    # authorize. `oauth2_token_endpoint` and `oauth2_userinfo_endpoint` are
    # server-to-server; in Docker they typically point at the internal
    # service URL while `oauth2_provider` points at the host-mapped URL.
    # Empty endpoint overrides default to derivations from `oauth2_provider`.
    oauth2_provider: str = ""
    oauth2_client_id: str = ""
    oauth2_client_secret: str = ""
    oauth2_token_endpoint: str = ""
    oauth2_userinfo_endpoint: str = ""
    oauth2_redirect_uri: str = ""       # explicit override; otherwise derived from request
    # "ephemeral" (default): the OAuth pair is discarded after login.
    # "encrypted": retain it in web_user_tokens, encrypted with the web-only
    # ISTOTA_WEB_TOKEN_KEY — enables post-as-user Talk mirroring + read sync.
    # The Docker path renders "encrypted" instead, because it is the one shape
    # that mints ISTOTA_WEB_TOKEN_KEY for itself; every other shape leaves that
    # key to the operator and so cannot assume it exists (ISSUE-430).
    token_storage: str = "ephemeral"
    session_secret_key: str = ""
    # Byte cap on a profile-picture upload, in KB. Enforced twice by the avatar
    # routes — on the declared Content-Length before the body is read, and
    # again on the running total as the stream arrives — because
    # `UploadFile.read()` materializes whatever nginx let through, and nginx is
    # sized for chat attachments (100 MB), not for a 192px avatar. The two
    # limits are different numbers on purpose and neither substitutes for the
    # other.
    max_avatar_kb: int = 4096
    # Whether the scheduler imports users' Nextcloud profile pictures at all.
    # Only a *custom* avatar is ever imported: Nextcloud generates a coloured
    # letter for a user who has set none, and importing that would swap our own
    # initial chip for Nextcloud's version of the same idea, with nothing
    # downstream able to tell them apart. Inert on a local storage backend,
    # where there is no Nextcloud to ask.
    avatar_import_from_nextcloud: bool = True
    chat: WebChatConfig = field(default_factory=WebChatConfig)
    map: WebMapConfig = field(default_factory=WebMapConfig)


@dataclass
class SiteConfig:
    """The deployment's public DNS name.

    Used by the web app for OAuth2 redirect derivation, origin/CSRF checks,
    and webhook URLs.

    The agent-writable static web root (``enabled`` / ``base_path``) was
    removed in ISSUE-194: a publicly-served directory the agent could write
    to with an ordinary ``cp`` was an outbound egress channel the
    confirmation model classified as a benign local write, so any private
    data the agent could read could be published to a public URL without
    tripping a gate. Serving static assets is now entirely outside istota.
    """
    hostname: str = ""        # e.g. "istota.example.com"


@dataclass
class CaldavConfig:
    """Explicit CalDAV override (``[caldav]``).

    When any field is set, it overrides the value derived from ``[nextcloud]``
    (see ``Config.caldav_*`` properties). Lets a local install point calendar
    at an external CalDAV server (Radicale, Fastmail, Google) without a
    Nextcloud. All-blank (default) → fall back to the NC derivation, so server
    deployments are unaffected.
    """
    url: str = ""
    username: str = ""
    password: str = ""


@dataclass
class NetworkConfig:
    """Network isolation via CONNECT proxy (requires sandbox)."""
    enabled: bool = True  # --unshare-net + proxy; false keeps current open-network behavior
    allow_pypi: bool = True  # add pypi.org + files.pythonhosted.org to allowlist
    extra_hosts: list[str] = field(default_factory=list)  # operator-specific additions


@dataclass
class SecurityConfig:
    """Security hardening configuration."""
    sandbox_enabled: bool = True  # bwrap filesystem isolation per user
    skill_proxy_enabled: bool = True  # proxy skill CLI calls via Unix socket
    skill_proxy_timeout: int = 300  # timeout for proxied skill commands (seconds)
    # Operator overrides of the timeout above, `{skill_name: seconds}`. An entry
    # replaces the value below it for that skill rather than raising it, so the
    # map narrows as readily as it widens (ISSUE-448).
    #
    # **Empty, and the shipped policy is not here**: `code_review` gets its own
    # ceiling from `skill_proxy.DEFAULT_SKILL_TIMEOUTS`. A `dict` field replaces
    # its default rather than merging with it — `config_mapper` hands the
    # operator's table straight through, and Ansible's hash behaviour is replace
    # too — so a shipped entry would be silently dropped by anyone who wrote
    # this table to configure a *different* skill, taking the review's ceiling
    # back to the global and reintroducing the bug the map exists to fix.
    # Naming `code_review` here still overrides the shipped value.
    skill_proxy_timeouts: dict[str, int] = field(default_factory=dict)
    passthrough_env_vars: list[str] = field(default_factory=lambda: [
        "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    ])
    # Extra RO bind-mounts inside the sandbox, for co-located services the
    # agent genuinely needs to read. Empty by default: the entry that used to
    # be here ("/srv/app") was added for a co-located moneyman install that no
    # longer exists, and on the reference deployment it contained istota_home
    # — so it exposed the framework DB and every user's module DB to every
    # task. build_bwrap_cmd masks the database directories after applying this
    # list, so a re-added broad path can't undo that, but keep entries narrow.
    sandbox_ro_paths: list[str] = field(default_factory=list)
    # Root of a disk-backed directory for the package managers' caches. Each
    # user gets `{root}/{user_id}`, bound RW into their sandbox. Empty (the
    # default) keeps the pre-ISSUE-305 behaviour: `$HOME/.cache` inside the
    # namespace exists only as a parent directory bwrap created on its own root
    # tmpfs, so a `uv sync` unpacks into RAM that `host_pressure.read_tmpfs_usage`
    # cannot see — the mount lives in the task's namespace and appears in no
    # table the host reads — and the whole cache is discarded at task exit.
    #
    # Put the root *under* `developer.repos_dir` if that is set. uv populates a
    # venv by hardlinking out of its cache, and `link(2)` returns EXDEV across a
    # mount boundary even on one device; a cache on any other mount makes every
    # worktree pay for a full copy instead of sharing one byte set.
    #
    # Ignored, with one warning, when it is relative, missing, unwritable, under
    # a database directory, or at or *above* anything the sandbox already mounts
    # — the cache bind is emitted late, so a destination above an earlier mount
    # would cover it. `executor.resolve_sandbox_cache_dir` owns every one of
    # those rules and never raises.
    sandbox_cache_dir: str = ""
    # Bounding what the key above creates (ISSUE-317, src/istota/sandbox_cache_sweeper.py).
    # Moving the caches onto disk is what makes them *persist*, and nothing
    # pruned them: the sweep runs from the scheduler on
    # `scheduler.sandbox_cache_sweep_interval` and gives each per-user cache the
    # package managers' own cheap reclaim (`uv cache prune`, `npm cache verify`),
    # escalating to a full clean only for one still over its ceiling afterwards.
    # A ceiling in bytes rather than an age window, because one
    # `uv sync --all-extras` writes about 1.8 GB at once and blows any sane
    # window immediately. Inert while `sandbox_cache_dir` is empty.
    sandbox_cache_sweep_enabled: bool = True
    # Per user, in gibibytes. Clamped to a 1 GiB floor by the sweeper: below
    # that the ceiling is under a single dependency resolution's working set, so
    # every sweep would wipe a cache that is doing its job.
    sandbox_cache_max_gb: float = 10.0
    network: NetworkConfig = field(default_factory=NetworkConfig)


@dataclass
class GoogleWorkspaceConfig:
    """Google Workspace CLI integration (OAuth-based)."""
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = field(default_factory=lambda: [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/documents.readonly",
    ])


@dataclass
class WebFetchConfig:
    """Native-brain daemon-side WebFetch tool ([brain.native.web_fetch]).

    All fields defaulted to safe values, so an absent block enables the tool
    with HTTPS-only, size/time-capped, SSRF-hardened behaviour. Every field but
    ``admin_only`` maps 1:1 onto ``session.tools.WebFetchPolicy``. See
    ``.claude/rules/brain.md``.

    ``admin_only`` is the one that is not a fetch policy, and it is read by
    ``executor.build_allowed_tools`` rather than by the tool: it decides whether
    the tool is registered at all, so it can never reach the object that
    performs a fetch. It is the operator's way back to the posture ISSUE-389
    shipped and ISSUE-449 retired — the tool withheld from a non-admin whatever
    ``enabled`` says, on the grounds that a fetch from the daemon's own network
    namespace is egress the same user's CLI-brain task does not have.

    **Off by default, which is a widening on upgrade and is deliberate.** The
    identity gate was standing in for an egress policy this block already
    carries: ``allow_hosts``, ``block_hosts``, ``extra_blocked_cidrs``,
    ``allowed_ports``, ``allow_http``, the built-in private/reserved IP
    blocklist and ``require_url_provenance`` bound *what* may be reached, and
    they bind every caller identically. An identity gate bounds *who*, and on
    this question that answered the wrong axis: it left an admin's egress
    unbounded by anything the gate did, while a non-admin asking for a web page
    got nothing at all and no reason why. A deployment that wants who-scoping
    back sets this; a deployment that wants the egress narrowed for everybody
    was always meant to set the fields above.
    """

    enabled: bool = True
    admin_only: bool = False
    timeout_seconds: float = 20.0
    max_bytes: int = 5_000_000
    max_content_chars: int = 100_000
    max_redirects: int = 5
    allow_http: bool = False
    allowed_ports: list[int] = field(default_factory=lambda: [80, 443])
    user_agent: str = "IstotaBot/1.0"
    allow_hosts: list[str] = field(default_factory=list)
    block_hosts: list[str] = field(default_factory=list)
    extra_blocked_cidrs: list[str] = field(default_factory=list)
    require_url_provenance: bool = False


@dataclass
class SessionLogConfig:
    """Native-brain session transcripts ([brain.native.session_log]).

    One append-only JSONL file per *task attempt*, holding the run the way it
    happened: the assembled prompt, the system prompt, every assistant turn
    with its thinking and tool calls, every tool result, every compaction,
    every injected steer, and a terminal record. `tasks.execution_trace` keeps
    tool *labels* and no output at all, so a native task that answered wrongly
    could not be reconstructed; this is the artifact that answers "what did the
    model actually see and do".

    Every field is defaulted, so an absent block is the shipping behaviour.
    ``enabled = false`` switches the writer off: it is constructed with no root
    and every method is a no-op. The retention sweep still applies to files
    already on disk until both retention rules are set to zero.

    ``dir`` is the one field that is not used as written. Blank — the default —
    resolves to ``{db_path.parent}/logs`` via
    :func:`istota.session.session_log.resolve_session_log_dir`, which is local
    disk on every shipped shape and behind the sandbox's database mask on two
    of the three. A value set here is taken literally; nothing expands ``~``,
    matching every other path in this file. A *relative* value is honoured and
    then followed against each process's own working directory, so an absolute
    path is what an operator should write; a value naming no directory of its
    own (``/``, ``.``, ``..``) is refused back to the default, because the
    resolved directory is what the retention sweep deletes under.

    **Beyond that refusal the value is trusted**, the way
    ``security.sandbox_cache_dir`` is: the sweep carries no containment rule
    bounding an operator-set root against an ancestor, so it treats every
    subdirectory of whatever is written here as a user's and unlinks the
    ``*.jsonl`` under it. That is settled rather than pending — the reasoning is
    in :func:`istota.session.session_log.sweep_session_logs`, and
    ``config.example.toml`` states it beside the setting so an operator meets it
    before they set one.

    The numbers below are restated from ``session/session_log.py``'s
    ``DEFAULT_*`` constants rather than imported, so ``config.py`` stays below
    the session layer in the import graph — it is loaded by the daemon, the web
    app, the webhook receiver, every CLI invocation and every host-side skill
    CLI the proxy spawns per call. ``tests/test_config_native_session_log.py``
    is what holds the two copies equal.
    """

    enabled: bool = True
    # "" → {db_path.parent}/logs. See resolve_session_log_dir.
    dir: str = ""
    # Age rule, for privacy. 0 keeps everything by age; the ceiling still runs.
    retention_days: int = 14
    # Size ceiling, for the disk, across *every* user. 0 drops the ceiling; the
    # age rule still runs. Clamped to a 0.5 GB floor by the sweep.
    max_total_gb: float = 2.0
    # Per text / thinking block, head-and-tail truncated over the cap. 0 here
    # means NO cap rather than "off" as it does on the two limits above: an
    # uncapped block is one raw tool result at whatever size it came back.
    max_content_chars: int = 32768
    # Per tool-call arguments object, replaced by an honest marker over the
    # cap. 0 = no cap, as above.
    max_args_chars: int = 8192
    # Thinking blocks in the written log. Independent of `tasks transcript`,
    # where thinking stays off by default behind --thinking.
    include_thinking: bool = True


@dataclass
class NativeBrainConfig:
    """Settings for the native harness (``brain.kind = "native"``).

    The native brain runs istota's own agent loop in-process against an
    ``LLMProvider``. ``provider`` selects the backend; the rest configure it.

    - ``provider`` — ``"openai_compat"``: any OpenAI chat-completions endpoint
      (Anthropic, OpenRouter, Ollama, …). The only provider; the field stays so
      the layer can grow new backends without a config break.
    - ``model`` — explicit model id (``openai_compat`` does no aliasing).
    - ``base_url`` / ``api_key`` / ``extra_headers`` — for ``openai_compat``.
      ``api_key`` is populated from the ``ISTOTA_BRAIN_NATIVE_API_KEY`` env
      override (kept out of the TOML file).
    - ``context_window`` — 0 resolves from the live-fetched (OpenRouter) catalog
      or the conservative 200k default; set to override per deployment (the
      documented contract for a non-OpenRouter native endpoint).
    - ``max_turns`` — hard cap on assistant turns per task (loop backstop).
    - ``max_tokens`` — per-completion output cap.
    """

    provider: str = "openai_compat"  # only "openai_compat"
    model: str = ""
    effort: str = ""  # native-brain default effort: low/medium/high/xhigh/max (empty = none)
    base_url: str = "https://api.anthropic.com/v1"
    api_key: str = ""  # from ISTOTA_BRAIN_NATIVE_API_KEY at load time
    extra_headers: dict = field(default_factory=dict)
    context_window: int = 0  # 0 = resolve from fetched catalog / 200k default
    max_turns: int = 100
    max_tokens: int = 16384
    # Per-model capability/window overrides ([brain.native.model_overrides]).
    # Maps a model id to a partial ModelInfo (context_window, supports_thinking,
    # supports_vision, max_output_tokens, prices). Lets a non-Anthropic
    # reasoning/vision model or a small-window local model no live catalog
    # knows declare its real capabilities instead of being degraded to the
    # conservative default (NB-4). Also corrects a single wrong field on a
    # fetched (OpenRouter) model.
    model_overrides: dict = field(default_factory=dict)
    # Compaction sizing. 0 = derive from the model's context window (so a small-
    # window local model compacts sensibly instead of using the Anthropic-sized
    # 16k/20k constants). See istota.session.compaction (NB-14).
    compaction_reserve_tokens: int = 0
    compaction_keep_recent_tokens: int = 0
    # Opt-in cache_control breakpoints (Anthropic/OpenRouter). Tri-state: ``None``
    # (the operator set no explicit value) derives the default from base_url in
    # make_provider — on for api.anthropic.com, off elsewhere. An explicit
    # ``True``/``False`` always wins, whether it came from the TOML or was set
    # directly on the dataclass.
    prompt_caching: bool | None = None
    # Daemon-side WebFetch tool ([brain.native.web_fetch]). Enabled by default
    # with safe caps; the tool is native-only (added to build_default_tools).
    web_fetch: WebFetchConfig = field(default_factory=WebFetchConfig)
    # When Bash output exceeds the per-tool cap, spill the full captured output
    # to a temp file under ISTOTA_DEFERRED_DIR and name it in the result so the
    # model can Read it, instead of silently dropping the tail. Default-on;
    # set false to keep the cap-only truncation behaviour.
    bash_spill_full_output: bool = True
    # Turn-budget awareness nudge (ISSUE-187 defect 3). The loop injects an
    # environment notice as the run approaches ``max_turns`` so the model paces
    # itself and delivers a partial answer instead of getting capped mid-plan.
    # Fires once at ``turn_budget_nudge_early_percent`` of the cap (a "~halfway,
    # keep this in mind" reminder), then once each as absolute steps-remaining
    # crosses each value in ``turn_budget_nudge_remaining`` (escalating urgency).
    # The budget is framed as a *shrinking* resource ("~N remaining"), never as
    # an upfront allotment, to avoid anchoring the count as a target. Counted
    # from assistant turns (monotonic across compaction); each threshold fires at
    # most once. Off via ``turn_budget_nudge=false`` for models/deployments that
    # mishandle meta-instructions.
    turn_budget_nudge: bool = True
    turn_budget_nudge_early_percent: int = 50
    turn_budget_nudge_remaining: list[int] = field(default_factory=lambda: [15, 5])
    # Soft wall-clock deadline (ISSUE-373). The three limits governing a native
    # run — `max_turns`, `scheduler.task_timeout_minutes` and the nudge ladder —
    # are all sized against a brain that answers in a couple of seconds. On a
    # slow fallback the clock arrives first, and the clock is the stop that
    # *discards* the model's work where `max_turns` delivers it under a marker.
    # So the loop stops itself at this percentage of the task timeout, on a
    # stop_reason that preserves the partial answer. The remaining slack is what
    # the hard deadline still covers: a turn that hangs past the soft stop.
    # 0 (or >= 100) turns it off and the hard clock is the only backstop again.
    soft_deadline_percent: int = 90
    # Live model-catalog enrichment from OpenRouter (ISSUE-182). When the brain
    # talks to an OpenRouter endpoint (``base_url`` contains ``openrouter.ai``),
    # it fetches OpenRouter's public model list once per process (disk-cached
    # with the TTL below) and installs the real window/capabilities/prices into
    # the catalog. No effect for non-OpenRouter endpoints — those resolve
    # metadata from ``model_overrides`` / ``context_window`` / the conservative
    # default. Fetch failure is never fatal (falls back to stale cache, then
    # default). Off via ``model_catalog_fetch = false``.
    model_catalog_fetch: bool = True
    model_catalog_cache_ttl_hours: float = 24.0
    # Per-attempt JSONL transcript of the run ([brain.native.session_log]).
    # Native-only: the other two brains already get one from the `claude` CLI.
    session_log: SessionLogConfig = field(default_factory=SessionLogConfig)


@dataclass
class TmuxBrainConfig:
    """Settings for the tmux-driven interactive-TUI brain (``brain.kind =
    "tmux_claude"``). All fields default to the values the prototype hardcoded,
    so an empty (or absent) ``[brain.tmux]`` block behaves exactly like the
    pre-config prototype.

    Operability knobs (``fallback_*``) drive the §4 circuit breaker; the marker
    lists let a ``claude`` CLI reword be a config hotfix rather than a code
    release (the readiness / dialog / error heuristics are pane-text substring
    matches pinned to a CLI version — see ``cli_version_pin``).

    ``model`` and ``effort`` are this brain's own defaults, on the same footing
    as ``[brain.claude_code]``'s and ``[brain.native]``'s (ISSUE-418). This
    brain shares ``ClaudeCodeBrain``'s ``anthropic`` namespace and runs the same
    binary, so the two blocks accept the same values and the retired top-level
    keys migrate onto **both** — which is what makes the migration exactly
    behaviour-preserving for a deployment running either CLI brain. They are
    still separate fields rather than one shared with ``[brain.claude_code]``:
    an operator running both (a ``tmux_claude`` primary with a ``claude_code``
    fallback is a shipped pairing) has no way to say "the same model" that does
    not also mean "and I can never differ".
    """

    # This brain's own default model / effort, used when the task pins none.
    model: str = ""
    effort: str = ""  # low/medium/high/xhigh/max (empty = the model's own default)
    # Operability / fallback (§4)
    fallback_trip_threshold: int = 5          # consecutive launch failures before the circuit opens
    fallback_cooldown_seconds: float = 300.0  # how long the circuit stays open
    ready_timeout_seconds: float = 30.0       # REPL-ready deadline
    tmux_command_timeout: float = 10.0        # per-tmux-subprocess timeout
    # CLI pinning (§6) — mismatch logs a WARNING at brain construction.
    cli_version_pin: str = "2.1.168"
    # Dialog / readiness / error markers (§6). Defaults match the pinned CLI.
    ready_markers: list[str] = field(
        default_factory=lambda: ["bypass permissions on", "? for shortcuts", "for shortcuts"]
    )
    trust_markers: list[str] = field(
        default_factory=lambda: ["trust this folder", "Is this a project you"]
    )
    theme_markers: list[str] = field(
        default_factory=lambda: ["Choose the text style", "run /theme"]
    )
    bypass_warning_marker: str = "Bypass Permissions mode"
    bypass_accept_marker: str = "Yes, I accept"
    # "session limit reached" is intentionally not here — it's a usage-limit
    # signal handled by usage_limit_markers (checked first), not a generic error.
    error_markers: list[str] = field(
        default_factory=lambda: ["API Error", "Context low"]
    )
    # Pane substrings that mark a subscription/quota usage limit (checked before
    # error_markers so a limit hit that aborts the turn before a transcript is
    # written classifies as stop_reason="usage_limit" — reroutes to the fallback
    # brain — rather than a generic error). CLI-version-pinned like the others.
    # The real TUI text is "You've hit your <scope> limit"; "session limit" is
    # the stable session substring, and the weekly/Opus scopes are caught by the
    # is_usage_limit_error(pane) fallback in _wait_for_completion.
    usage_limit_markers: list[str] = field(
        default_factory=lambda: [
            "session limit",
            "usage limit reached",
            "Claude usage limit",
        ]
    )


@dataclass
class ClaudeCodeBrainConfig:
    """Settings for the headless ``claude -p`` brain (``brain.kind =
    "claude_code"``, the default).

    ``model`` and ``effort`` are **this brain's** defaults, applied when nothing
    pins one for the task. They used to be the top-level ``model`` / ``effort``,
    a vestige of there having been one brain: sitting at the root they read as a
    deployment-wide default, and the executor treated them as one, filling every
    request with them whatever brain was about to run. That shadowed the other
    brains' own configured defaults, so a room pinned to ``native`` with
    ``[brain.native] model`` set ran the *Claude Code* model against the native
    endpoint — billed per token, and a hard failure rather than a wrong bill
    anywhere the endpoint does not happen to serve Anthropic ids (ISSUE-418).
    The top-level keys still load and are migrated onto this block and
    ``[brain.tmux]`` by ``_apply_legacy_brain_defaults``, with a warning; they
    are not read anywhere else. Per-brain rather than deployment-wide is what
    ``[brain.native]`` already did, and the ``or`` chain in each brain is now
    the single place a default is applied.

    Beyond those two the block is the **subscription usage poll**. On a
    subscription deployment the dashboard's cost column is deliberately blank (a
    plan-equivalent list price is not spend), so the real budget is the
    rate-limit windows Anthropic reports at ``GET /api/oauth/usage``.
    ``istota.subscription_usage`` fetches them; the doctor check, the ``/admin``
    card and ``!usage`` render them.

    Every field is defaulted, so an absent ``[brain.claude_code]`` block is the
    shipping behaviour. The poll is read-only: the credential is never written
    and never refreshed. ``subscription_usage = false`` makes the doctor check
    ``SKIP`` and the admin card **absent** — the section key is omitted rather
    than carrying ``available: false`` with a reason. The reason existed while a
    missing reading meant something was wrong; it stopped being right once the
    endpoint turned out to answer the long-lived setup token both server shapes
    deploy with a persistent 429, which made the note permanent and left it
    naming nothing an operator could do.

    ``_validate_claude_code_brain`` (config load) corrects a configuration that
    would make the feature misbehave — see its docstring for the three rules.
    """

    # This brain's own default model / effort, used when the task pins none.
    # Accepts everything `model` has always accepted: a canonical id, a
    # shortcut, a role tier, any of them plus a `:effort` modifier. Resolved
    # through this brain's alias table at the point of use, not here.
    model: str = ""
    effort: str = ""  # low/medium/high/xhigh/max (empty = the model's own default)
    # Poll api.anthropic.com for plan utilization at all.
    subscription_usage: bool = True
    # One deployment-wide fetch per this window; every surface reads the same
    # disk cache, so the dashboard's 60s auto-refresh costs nothing.
    subscription_usage_cache_ttl_seconds: int = 1800
    # Matches doctor.PROBE_TIMEOUT.
    subscription_usage_timeout_seconds: float = 10.0
    # Doctor WARNs and the tile turns amber at or above warn; the tile turns red
    # at or above high. Never a FAIL at any utilization — a busy plan is a fact
    # about the plan, not a defect in the host.
    subscription_usage_warn_percent: float = 80.0
    subscription_usage_high_percent: float = 95.0
    # A stale-cache reading older than this is reported as SKIP rather than as
    # a current one: a reading this old means the fetches are failing, which on
    # a server shape is the steady state rather than a fault, so there is
    # nothing to check. Reporting it as WARN named nothing actionable.
    subscription_usage_stale_after_seconds: int = 3600


@dataclass
class BrainConfig:
    """Selects which brain implementation handles model invocation.

    ``"claude_code"`` (default) wraps the ``claude`` CLI subprocess.
    ``"native"`` runs istota's own agent loop in-process; its settings live in
    the nested ``native`` block (``[brain.native]`` in TOML).
    ``"tmux_claude"`` drives the interactive ``claude`` TUI via tmux; its
    settings live in ``[brain.tmux]``. ``[brain.claude_code]`` holds the
    subscription usage poll, which is read regardless of ``kind`` — a native
    primary with a ``claude_code`` fallback burns the same plan.

    ``source_type_overrides`` maps a task ``source_type`` (``scheduled``,
    ``heartbeat``, ``talk``, …) to a brain kind, overriding ``kind`` for
    matching tasks. This is the gradual-rollout knob: move cron/heartbeat to
    the native brain while interactive tasks stay on ``claude_code``. Set in
    TOML as ``[brain.source_type_overrides]``.

    ``room_selectable`` names the brain kinds a room may pin for itself. Empty
    (the default) means no room may override the brain, so the feature ships
    inert and an operator opts in by naming kinds. It is a gate rather than a
    preference: a brain kind selects an isolation posture, and a change to an
    enforcement mechanism should not arrive switched on by an upgrade. Set in
    TOML as ``[brain] room_selectable``.
    """
    kind: str = "claude_code"
    native: NativeBrainConfig = field(default_factory=NativeBrainConfig)
    tmux: TmuxBrainConfig = field(default_factory=TmuxBrainConfig)
    claude_code: ClaudeCodeBrainConfig = field(default_factory=ClaudeCodeBrainConfig)
    source_type_overrides: dict[str, str] = field(default_factory=dict)
    room_selectable: list[str] = field(default_factory=list)
    # Availability failover (brain-fallback spec). When the primary brain is
    # unavailable (usage limit / not_found / tmux launch failure), the task runs
    # on ``fallback`` with that brain's own configured settings. "" = no
    # fallback, for every brain kind — no implicit target (ISSUE-362; see
    # brain._fallback.effective_fallback_kind).
    fallback: str = ""
    # Include a persistent transient_api_error in the reroute trigger set.
    # On by default (ISSUE-212): a capacity signal that survived the primary's
    # own in-brain retries is exactly what the fallback exists to absorb, and
    # the alternative is handing the user a raw provider error. Not a hair
    # trigger — the primary has already burned API_RETRY_MAX_ATTEMPTS by then.
    fallback_on_transient: bool = True
    # Availability-breaker cooldown: once the primary reports a persistent
    # unavailability, subsequent tasks skip it for this long. 0 disables
    # stickiness (every task probes the primary first).
    fallback_cooldown_seconds: int = 900


@dataclass
class BriefingsModuleConfig:
    """Module-level config for the first-class briefings module (``[briefings]``).

    Governs the module's per-user content store + archive.

    ``archive_retention_days`` — prune archived briefings older than this per
    insert (0 = keep forever). ``default_lookback_hours`` seeds the email/rss
    source lookback window when a source omits it. ``max_source_chars`` caps a
    single source's gathered text — for the list-shaped ``todos`` source it is
    spent item by item and an item is never split, since a cut mid-line would
    render as a todo the file never contained.

    ``max_browse_chars`` is the same cap for a ``browse`` source, which is
    separate because that source gathers *markdown* rather than flattened
    text: the URLs it keeps (the whole point of ISSUE-192) cost characters,
    and a frontpage spends its first couple of thousand on masthead chrome
    before the headline grid starts, so the text budget would cut above the
    content. Both are per-source caps an operator can lower; a source's own
    ``max_chars`` still wins over either.
    """
    archive_retention_days: int = 90
    default_lookback_hours: int = 12
    max_source_chars: int = 5000
    max_browse_chars: int = 20000
    # Inline `[anchor](url)` links preserved per newsletter body. Newsletters are
    # link soup (unsubscribe / social / tracking chrome), so the converter
    # filters hard and this caps what survives the filter — an over-cap anchor
    # keeps its text and loses only the destination. 0 = unlimited.
    newsletter_max_links_per_source: int = 20
    # Timezone shared briefing blocks evaluate their cron in. Shared blocks are
    # global (generated once, no per-user timezone) so this is a single
    # operator-chosen zone — typically the operator's local zone so the
    # morning/evening regeneration windows line up with their day. Defaults to
    # "UTC" (the historical behaviour). Invalid names fall back to UTC at run
    # time (see scheduler.check_shared_blocks). Set via
    # ``[briefings] shared_block_timezone`` (Ansible ``istota_briefing_shared_tz``).
    shared_block_timezone: str = "UTC"


@dataclass
class HealthModuleConfig:
    """Module-level config for the health module (``[health]``).

    ``max_document_bytes`` caps a single stored document (scan, discharge
    summary, vaccination card). ``0`` means unlimited — the documented
    escape hatch for a user whose scanner produces genuinely large files.
    """
    max_document_bytes: int = 25 * 1024 * 1024


@dataclass
class MoneyModuleConfig:
    """Module-level config for the money module (``[money]``).

    ``autoclass_lookup`` gates the portfolio module's ticker-metadata lookup,
    which sends every newly imported symbol to a third-party quote API. Held
    symbols are private financial data and the call runs in the unsandboxed
    daemon/web process — outside the CONNECT allowlist, so
    ``[security.network]`` cannot restrain it. Default on (the classification
    it buys is the feature); off keeps the offline description heuristics,
    which need no network at all.
    """
    autoclass_lookup: bool = True


@dataclass
class ModelsConfig:
    """Operator-controlled model alias registry (``[models.aliases]``).

    One operator-visible table covering **both** the portable tiers
    (``fast``/``general``/``smart``) and the provider shortcuts
    (``opus``/``sonnet``/``haiku``). The shipped default set lives on the active
    brain in ``brain.claude_code.DEFAULT_ALIASES`` as the overridable floor;
    operators overlay any name here.

    Each alias value is the **raw parsed structure** — either a bare string
    (legacy flat, namespace-agnostic) or a per-namespace table
    (``{anthropic: "opus", openai_compat: {model = "...", effort = "high"}}``)
    so one definition covers every brain family. A reserved ``portable = true``
    key inside a table marks a custom alias as a cross-brain intent.
    Normalization into ``RoleTarget`` objects happens once in
    ``brain._roles.set_alias_overrides``; this field carries the raw shape so the
    namespace-aware validation loop can inspect it. Effort is an orthogonal
    ``:effort`` modifier on any reference, never baked into an alias name.
    """

    aliases: dict[str, str | dict] = field(default_factory=dict)


@dataclass
class ExperimentalConfig:
    """Operator-scoped feature flags. See ``src/istota/experimental.py``."""

    features: list[str] = field(default_factory=list)

    def is_enabled(self, feature: str) -> bool:
        return feature in self.features


@dataclass
class Config:
    namespace: str = "istota"  # Install namespace (drives /etc/{namespace}/, /srv/app/{namespace}/, etc.)
    bot_name: str = "Istota"  # User-facing name (used in chat, emails, folder names)
    emissaries_enabled: bool = True  # Include config/emissaries.md in system prompt (`istota setup` writes false for local installs)
    # DEPRECATED (ISSUE-418): these were the *claude_code* brain's defaults
    # living at the root, where they read as deployment-wide and were applied to
    # every brain — shadowing each other brain's own configured default. They
    # are migrated onto `[brain.claude_code]` and `[brain.tmux]` by
    # `_apply_legacy_brain_defaults` and are read nowhere else. Kept as fields
    # because every existing deployment sets them; a rollback has to load a
    # config written by the newer version and vice versa.
    model: str = ""  # deprecated alias for [brain.claude_code] / [brain.tmux] model
    effort: str = ""  # deprecated alias for [brain.claude_code] / [brain.tmux] effort
    advisor_model: str = ""  # Advisor model ID or alias (anthropic-namespace brains only); resolved through the alias table like `model`, but carries no effort. Empty = no advisor. Dropped whenever a task pins its own model (see executor._resolve_advisor). Must resolve to a model capable of *being* an advisor — a weak/cheap tier (e.g. haiku) fails every task the advisor runs on; Istota does not validate this (no pairing table — see the spec's "Not doing")
    max_memory_chars: int = 0  # cap total memory in prompts (0 = unlimited)
    max_knowledge_facts: int = 50  # cap knowledge graph facts per prompt (0 = unlimited)
    db_path: Path = field(default_factory=lambda: Path("data/istota.db"))
    nextcloud: NextcloudConfig = field(default_factory=NextcloudConfig)
    talk: TalkConfig = field(default_factory=TalkConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    devbox: DevboxConfig = field(default_factory=DevboxConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    briefings: BriefingsModuleConfig = field(default_factory=BriefingsModuleConfig)
    health: HealthModuleConfig = field(default_factory=HealthModuleConfig)
    money: MoneyModuleConfig = field(default_factory=MoneyModuleConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    memory_search: MemorySearchConfig = field(default_factory=MemorySearchConfig)
    playbooks: PlaybooksConfig = field(default_factory=PlaybooksConfig)
    sleep_cycle: SleepCycleConfig = field(default_factory=SleepCycleConfig)
    channel_sleep_cycle: ChannelSleepCycleConfig = field(default_factory=ChannelSleepCycleConfig)
    developer: DeveloperConfig = field(default_factory=DeveloperConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    site: SiteConfig = field(default_factory=SiteConfig)
    caldav: CaldavConfig = field(default_factory=CaldavConfig)
    location: LocationReceiverConfig = field(default_factory=LocationReceiverConfig)
    google_workspace: GoogleWorkspaceConfig = field(default_factory=GoogleWorkspaceConfig)
    web: WebConfig = field(default_factory=WebConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    experimental: ExperimentalConfig = field(default_factory=ExperimentalConfig)
    users: dict[str, UserConfig] = field(default_factory=dict)  # nc_username -> UserConfig
    # Canonical shared briefing set, seeded into each opted-in user (retire-
    # legacy-briefing-components spec). Parsed from the top-level
    # ``[[default_briefings]]`` TOML section; merged by name into a user's
    # briefings (user wins) in ``_apply_user_briefings`` when their
    # ``default_briefings`` flag is true.
    default_briefings: list[BriefingConfig] = field(default_factory=list)
    # Module-owned shared briefing blocks (shared-kv-curated-content spec):
    # generated once globally and written into ``shared_kv``; per-user briefings
    # read them via a ``shared_block`` source. In-memory only (global; never in a
    # per-user table). ``load_config`` seeds ``DEFAULT_SHARED_BLOCKS`` when the
    # config declares no ``[[briefing_shared_blocks]]``; a directly-constructed
    # Config (tests) has none unless set.
    briefing_shared_blocks: list[BriefingSharedBlock] = field(default_factory=list)
    admin_users: set[str] = field(default_factory=set)  # users with full system access
    rclone_remote: str = "nextcloud"  # rclone remote name
    nextcloud_mount_path: Path | None = None  # If set, use mount instead of rclone CLI
    skills_dir: Path = field(default_factory=lambda: Path("config/skills"))
    bundled_skills_dir: Path | None = None  # Override bundled skills dir (for testing)
    disabled_skills: list[str] = field(default_factory=list)  # instance-wide skills to exclude
    custom_system_prompt: bool = False  # Use config/system-prompt.md instead of Claude Code's default
    temp_dir: Path = field(default_factory=lambda: Path("/tmp/istota"))
    # Local-disk root for per-user module SQLite DBs (feeds/health/location/money).
    # These live OFF the Nextcloud mount so they can use WAL safely — WAL's
    # mmap'd -shm SIGBUSes on the rclone FUSE mount (ISSUE-157). User-facing
    # workspace files (health uploads, money ledgers, feeds exports) stay on the
    # mount; only the .db relocates here. None (default) derives
    # ``{db_path.parent}/modules`` — i.e. alongside the framework DB, which is
    # already local. Set explicitly to override (guarded against the mount).
    module_data_dir: Path | None = None
    config_path: Path | None = None  # Set by load_config() to the file actually loaded

    @property
    def bot_dir_name(self) -> str:
        """Lowercase bot name used for Nextcloud folder names.

        Spaces replaced with underscores, non-ASCII/non-alphanumeric chars stripped.
        e.g. "Mister Jones" -> "mister_jones", "My-Bot 2" -> "my-bot_2"
        """
        import re
        name = self.bot_name.lower().strip()
        name = re.sub(r'\s+', '_', name)
        name = re.sub(r'[^a-z0-9_\-]', '', name)
        return name or "istota"

    @property
    def use_mount(self) -> bool:
        """Whether to use local mount instead of rclone CLI."""
        return self.nextcloud_mount_path is not None

    @property
    def storage_is_nextcloud(self) -> bool:
        """Whether a Nextcloud server backs the file workspace.

        Keyed on the presence of a Nextcloud URL — deliberately NOT
        ``is_standalone`` (which folds in web auth, an axis orthogonal to file
        storage). A URL means the files are Nextcloud whether reached via mount
        or rclone; no URL means a plain local folder.
        """
        return bool(self.nextcloud.url)

    @property
    def storage_backend(self) -> str:
        """Canonical storage backend label: ``"nextcloud"`` | ``"local"``.

        Single source of truth for prompt/skill storage vocabulary.
        """
        return "nextcloud" if self.storage_is_nextcloud else "local"

    @property
    def storage_label(self) -> str:
        """Short noun for prose. ``"Nextcloud"`` when Nextcloud-backed, else
        ``"your workspace"`` (a mid-sentence noun phrase, not a proper noun)."""
        return "Nextcloud" if self.storage_is_nextcloud else "your workspace"

    def workspace_root(self, user_id: str | None = None) -> Path | None:
        """On-disk root of the workspace (mount mode only; ``None`` under rclone).

        Scoped to the user's ``/Users/{user_id}`` subtree when ``user_id`` is
        given, else the bare mount root. A de-duplication of the
        ``mount / "Users" / uid`` idiom inlined across the codebase — not a
        storage abstraction (no I/O, no backend switch).
        """
        if self.nextcloud_mount_path is None:
            return None
        root = self.nextcloud_mount_path
        return root / "Users" / user_id if user_id else root

    def module_db_root(self) -> Path:
        """Local-disk root holding every user's per-module SQLite DBs.

        When ``module_data_dir`` is unset (None) the root derives as
        ``{db_path.parent}/modules`` — alongside the framework DB, which is
        already local, so it needs no guard. An *explicitly* configured
        ``module_data_dir`` is refused if it resolves under
        ``nextcloud_mount_path`` — a WAL DB there would SIGBUS the process, so
        a misconfigured value fails loud rather than at runtime.

        Split out of ``module_db_path`` because the sandbox needs the root on
        its own: ``build_bwrap_cmd`` masks it, and ``_validate_workspace_dir``
        refuses a REPL workspace that would bind it back in. Deriving that root
        in three places is how it went unmasked in the first place.
        """
        if self.module_data_dir is None:
            return Path(self.db_path).parent.resolve() / "modules"
        root = Path(self.module_data_dir).resolve()
        mount = self.nextcloud_mount_path
        if mount is not None and root.is_relative_to(Path(mount).resolve()):
            raise ValueError(
                f"module_data_dir {root} is under nextcloud_mount_path "
                f"{mount}; per-module DBs must live on local disk (WAL -shm "
                "SIGBUSes on the FUSE mount — ISSUE-157)"
            )
        return root

    def module_db_path(self, user_id: str, module: str) -> Path:
        """Local-disk path for a user's per-module SQLite DB.

        ``{module_db_root()}/{user_id}/{module}.db``. Module DBs live on local
        disk (not the Nextcloud mount) so they can use WAL — WAL's mmap'd -shm
        SIGBUSes on the rclone FUSE mount (ISSUE-157). The module loaders pass
        the result as the ``db_path`` override into their workspace synth, so
        the user-facing ``data_dir`` (health uploads, money ledgers, feeds
        exports) stays on the mount while only the ``.db`` relocates.
        """
        return self.module_db_root() / user_id / f"{module}.db"

    def get_user(self, nc_username: str) -> UserConfig | None:
        """Get user config by Nextcloud username. Returns None if user not configured."""
        return self.users.get(nc_username)

    def find_user_by_email(self, email_address: str) -> str | None:
        """Find user_id by email address. Returns None if not found."""
        email_lower = email_address.lower()
        for user_id, user_config in self.users.items():
            if email_lower in [e.lower() for e in user_config.email_addresses]:
                return user_id
        return None

    def is_trusted_email_sender(
        self, user_id: str, sender_email: str, conn: "sqlite3.Connection | None" = None,
        *, include_own_addresses: bool = True,
    ) -> bool:
        """Check if sender is trusted for the given user.

        Trusted = user's own email addresses OR matches trusted_email_senders
        config patterns OR exists in runtime trusted_email_senders DB table.

        ``include_own_addresses=False`` drops the first of those. A caller that
        is *asking about* the own-address claim itself — the sender-match
        confirmation gate, whose route is defined by that same match — would
        otherwise get a circular True and never fire (ISSUE-227). The remaining
        two branches still answer: both are trust the operator or the user
        granted deliberately, rather than trust the unauthenticated ``From:``
        header asserted for itself.
        """
        from fnmatch import fnmatch

        user = self.users.get(user_id)
        if not user:
            return False

        sender_lower = sender_email.lower()

        if include_own_addresses and sender_lower in [e.lower() for e in user.email_addresses]:
            return True

        for pattern in user.trusted_email_senders:
            if fnmatch(sender_lower, pattern.lower()):
                return True

        # Check runtime-managed trusted senders in DB
        if conn is not None:
            from . import db
            if db.is_sender_trusted_in_db(conn, user_id, sender_lower):
                return True

        return False

    def is_quiet_email_sender(
        self, user_id: str, sender_email: str, conn: "sqlite3.Connection | None" = None,
    ) -> bool:
        """Check whether ``sender_email`` is on the user's quiet-senders list.

        Quiet = mail is filed silently (marked processed, left in INBOX), with
        no task and no session. fnmatch over the user's ``quiet_email_senders``
        patterns.

        Deliberately unlike :meth:`is_trusted_email_sender`: it does NOT
        implicitly match the user's own addresses (you never want your own mail
        silently dropped) and has no *dedicated* runtime table (there is no
        ``add_quiet_sender`` equivalent).

        It DOES read the user's ``quiet_email_senders`` from the live
        ``user_profiles`` row when a DB is configured — same as
        :meth:`is_module_enabled` — so a pattern added via ``/settings`` or
        ``istota user ensure`` takes effect on the next poll without a daemon
        restart. Falls back to the in-memory ``UserConfig`` on the init/test
        paths or an unseeded row. ``conn`` is reused for the read when given.
        """
        from fnmatch import fnmatch

        patterns: list[str] | None = None
        if self.db_path is not None and Path(self.db_path).exists():
            try:
                from . import user_profiles as _up
                profile = _up.get_profile(Path(self.db_path), user_id, conn=conn)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("is_quiet_email_sender DB read failed: %s", e)
                profile = None
            if profile is not None:
                patterns = profile.quiet_email_senders or []

        if patterns is None:
            user = self.users.get(user_id)
            if not user:
                return False
            patterns = user.quiet_email_senders

        sender_lower = sender_email.lower()
        for pattern in patterns:
            if fnmatch(sender_lower, pattern.lower()):
                return True
        return False

    def email_reply_routing_for(self, user_id: str) -> str:
        """Per-user mirror policy for email replies to messages we sent.

        One of ``origin+thread`` (default — deliver to the origin surface AND
        continue the email thread), ``origin`` (origin surface only), or
        ``thread`` (email only). An unrecognized stored value falls back to the
        default and logs a warning.
        """
        valid = ("origin+thread", "origin", "thread")
        user = self.users.get(user_id)
        value = (getattr(user, "email_reply_routing", "") or "").strip() if user else ""
        if not value:
            return "origin+thread"
        if value not in valid:
            logger.warning(
                "Unknown email_reply_routing %r for user %s; using 'origin+thread'",
                value, user_id,
            )
            return "origin+thread"
        return value

    def briefing_email_html_for(self, user_id: str) -> bool:
        """Whether this user's briefing email is sent as HTML + plain multipart.

        Default on — the point of the feature is clickable article links, and
        the plain-text part is always present so nothing is lost for a
        plain-only client. Off restores the pre-feature single-part plain
        delivery. Unknown users default on (matches the docker auto-seed path,
        as with :meth:`is_module_enabled`).
        """
        user = self.users.get(user_id)
        if user is None:
            return True
        return bool(getattr(user, "briefing_email_html", True))

    @property
    def caldav_url(self) -> str:
        """CalDAV base URL.

        An explicit ``[caldav] url`` wins (external CalDAV server); otherwise
        derived from the Nextcloud URL. Empty when neither is configured, which
        makes the calendar gate a no-op (graceful).
        """
        if self.caldav.url:
            return self.caldav.url.rstrip("/")
        if not self.nextcloud.url:
            return ""
        base = self.nextcloud.url.rstrip("/")
        return f"{base}/remote.php/dav"

    @property
    def caldav_username(self) -> str:
        """CalDAV username — explicit ``[caldav]`` override else the NC username."""
        if self.caldav.username:
            return self.caldav.username
        return self.nextcloud.username

    @property
    def caldav_password(self) -> str:
        """CalDAV password — explicit ``[caldav]`` override else the NC app password."""
        if self.caldav.password:
            return self.caldav.password
        return self.nextcloud.app_password

    @property
    def is_standalone(self) -> bool:
        """Whether this instance is running the slimmed-down local single-user shape.

        Config-derived (no stored flag): blank ``[nextcloud] url`` AND
        ``[web] auth == "none"``. This is the single home for the rule so the
        admin standalone-mode notice and any other caveat surface agree.
        """
        return (not self.nextcloud.url) and self.web.auth == "none"

    @property
    def local_user_id(self) -> str:
        """The single local user id for the no-auth (standalone) shape.

        Prefers the sole configured user, else the sole admin user, else
        ``"local"``. Only meaningful in no-auth mode where there is exactly one
        user by construction.
        """
        if len(self.users) == 1:
            return next(iter(self.users))
        if self.users:
            # Deterministic pick if somehow multiple are configured.
            return sorted(self.users)[0]
        if self.admin_users:
            return sorted(self.admin_users)[0]
        return "local"

    def effective_user_max_fg_workers(self, user_id: str) -> int:
        """Effective max fg workers for a user (per-user override > global default)."""
        uc = self.get_user(user_id)
        if uc and uc.max_foreground_workers > 0:
            return uc.max_foreground_workers
        return self.scheduler.user_max_foreground_workers

    def effective_user_max_bg_workers(self, user_id: str) -> int:
        """Effective max bg workers for a user (per-user override > global default)."""
        uc = self.get_user(user_id)
        if uc and uc.max_background_workers > 0:
            return uc.max_background_workers
        return self.scheduler.user_max_background_workers

    def is_module_enabled(
        self,
        user_id: str,
        module: str,
        *,
        conn: "sqlite3.Connection | None" = None,
    ) -> bool:
        """Check whether a module is enabled for a user.

        Modules are on by default. Returns False only when the user has an
        explicit ``disabled_modules`` entry for this module. Unknown users
        default to True so docker auto-seeding doesn't block first-login
        access.

        Reads from the ``user_profiles`` DB row when a DB is configured so
        that web / scheduler / skill subprocesses all see the same value
        without waiting for a config reload. Falls back to the in-memory
        ``UserConfig`` for the init / test paths where the DB may not exist
        yet, or when the row hasn't been seeded.

        Pass ``conn`` to reuse an existing framework-DB connection — hot
        per-tick loops in the scheduler already hold one and would
        otherwise open a fresh sqlite connection per call (the FD churn
        that produced "unable to open database file" / EMFILE).
        """
        from .modules import EXPERIMENTAL_MODULES, MODULE_NAMES, module_available
        if module not in MODULE_NAMES:
            logger.debug("is_module_enabled: unknown module %r", module)
            return False

        # Dependency gate: a module whose optional extra isn't installed (e.g.
        # `money` without beancount) is unavailable — hidden from the web UI and
        # skipped by the scheduler — so a lean install that omits the extra
        # doesn't show a broken tab or crash on first use. Runs before the DB
        # read so a missing extra short-circuits without a lookup.
        if not module_available(module):
            return False

        # Experimental modules stay dark until the operator opts in via
        # `[experimental] features = ["module_<name>"]`. This runs before
        # the per-user opt-out check so a disabled experimental module
        # short-circuits without a DB lookup.
        flag = EXPERIMENTAL_MODULES.get(module)
        if flag and not self.experimental.is_enabled(flag):
            return False

        if self.db_path is not None and Path(self.db_path).exists():
            try:
                from . import user_profiles as _up
                profile = _up.get_profile(Path(self.db_path), user_id, conn=conn)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("is_module_enabled DB read failed: %s", e)
                profile = None
            if profile is not None:
                return module not in (profile.disabled_modules or [])

        uc = self.users.get(user_id)
        if uc is None:
            return True
        return module not in (uc.disabled_modules or [])

    def resolve_user_timezone(
        self,
        user_id: str,
        *,
        conn: "sqlite3.Connection | None" = None,
    ) -> str:
        """Return the user's timezone string (IANA name), never empty.

        Prefers the live ``user_profiles`` DB row over the in-memory
        ``UserConfig`` so web-UI timezone edits take effect on the next task
        without a scheduler restart (ISSUE-099). Falls back to the in-memory
        config, then to ``"UTC"``. Mirrors ``is_module_enabled``'s DB-read
        pattern, including the optional ``conn`` for hot loops that already
        hold a framework-DB connection (avoids per-call FD churn on the
        FUSE-backed mount).

        Does NOT validate the zone name — callers that need a ``tzinfo`` wrap
        the result in ``ZoneInfo`` and own the invalid-name fallback, so this
        helper stays usable by code that only needs the string.
        """
        tz_str = ""
        if self.db_path is not None and Path(self.db_path).exists():
            try:
                from . import user_profiles as _up
                profile = _up.get_profile(Path(self.db_path), user_id, conn=conn)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("resolve_user_timezone DB read failed: %s", e)
                profile = None
            if profile is not None:
                tz_str = profile.timezone or ""

        if not tz_str:
            uc = self.users.get(user_id)
            tz_str = (uc.timezone if uc else "") or "UTC"
        return tz_str

    def available_capabilities(self) -> set[str]:
        """Backing-service capabilities currently available in this deployment.

        The single map from a capability name to the config flag that enables
        it. A skill declaring ``requires_capability: [name]`` in its frontmatter
        is folded into the effective disabled set when its capability isn't
        listed here (see skills._loader.effective_disabled_skills). These
        default off — notably in the standalone install, which deploys neither
        the headless browser nor the devbox container — so their skills drop out
        of selection and the on-demand menu automatically. Adding a new
        service-backed skill = declare the capability here + in its frontmatter.
        """
        caps: set[str] = set()
        if self.browser.enabled:
            caps.add("browser")
        if self.devbox.enabled:
            caps.add("devbox")
        # Keyed on the URL for the same reason storage_is_nextcloud is: a
        # standalone local install has no Nextcloud at all, and every verb of
        # the nextcloud skill would fail there.
        if self.nextcloud.url:
            caps.add("nextcloud")
        return caps

    def is_admin(self, user_id: str) -> bool:
        """Check if user has admin privileges.

        Returns True if no admins file exists (empty set = all users are admin
        for backward compatibility), or if user_id is in the admin set.
        """
        if not self.admin_users:
            return True
        return user_id in self.admin_users

    def is_shared_kv_writer(self, user_id: str) -> bool:
        """Authoritative gate for shared_kv writes. Fail-closed.

        Deliberately asymmetric to :meth:`is_admin`: an empty admin allowlist
        authorizes NOBODY here (mirroring ``web_app._user_is_web_admin``),
        rather than everyone. Content written to ``shared_kv`` flows into other
        users' briefing prompts (a prompt-injection surface), so a blank admins
        file must not silently make every user a shared-content writer.
        Returns True iff ``admin_users`` is non-empty and ``user_id`` is in it.
        Do not collapse this into ``is_admin``.

        One exception, in the spirit of the one ``web_app._user_is_web_admin``
        carries: on the standalone shape the single local user is a shared
        writer even though no admins file names them. Without it a standalone
        install could not write a shared briefing block at all, since the
        wizard wrote no admins file to be named in. The wizard writes one now,
        so on a fresh install this never fires — it is strictly the backstop
        for an install made before that, and the three conditions are what
        keep it that way rather than making it the primary route.

        ``not self.admin_users`` is the first, and it is what makes the
        sentence above true: an install with a real admins file is decided by
        that file, so a standalone operator who edits themselves *out* of it is
        refused rather than silently overridden. Without this clause the
        exemption answers first on every fresh install and the allowlist is
        never consulted for the local user — the opposite of a backstop.

        ``len(self.users) <= 1`` is the second. :attr:`local_user_id` falls
        back to ``sorted(self.users)[0]`` when several are configured, and its
        own docstring says it is "only meaningful in no-auth mode where there
        is exactly one user by construction" — so on a two-user local
        deployment the exemption would hand shared-write authority to whichever
        id sorts first. Non-web surfaces (email, cron, skill CLIs) reach this
        gate without passing through no-auth web login, so "everyone is the
        local user anyway" does not cover it.

        :attr:`is_standalone` is the third — blank ``[nextcloud] url`` **and**
        ``[web] auth == "none"`` — rather than the ``[web] auth`` axis alone
        the way ``_user_is_web_admin``'s is. That is deliberate and narrower: a
        deployment with Nextcloud storage and auth switched off is not the
        single-user shape, it has other users' content in it, and it must not
        silently gain a shared-content writer.
        """
        if (
            not self.admin_users
            and len(self.users) <= 1
            and self.is_standalone
            and user_id == self.local_user_id
        ):
            return True
        return bool(self.admin_users) and user_id in self.admin_users


def load_admin_users(path: str | None = None) -> set[str]:
    """Load admin user IDs from a plain-text file.

    File format: one user ID per line, # comments, blank lines ignored.
    Returns empty set if file doesn't exist. Empty-set semantics are
    asymmetric: Config.is_admin treats empty as "all users admin" for
    legacy back-compat, while the web admin dashboard fails closed.

    Args:
        path: Override file path. If None, checks ISTOTA_ADMINS_FILE env var,
              then falls back to /etc/istota/admins. The default path is
              wrong for renamed-namespace installs (e.g. /etc/mybot/admins);
              such deploys must set ISTOTA_ADMINS_FILE in every entry-point
              systemd unit. A WARNING is logged when the resolved path is
              missing so silent fail-closed admin in the web UI is visible
              in the journal.
    """
    explicit_path = path is not None
    env_var_set = "ISTOTA_ADMINS_FILE" in os.environ
    if path is None:
        path = os.environ.get("ISTOTA_ADMINS_FILE", "/etc/istota/admins")
    admins_path = Path(path)
    if not admins_path.exists():
        if not explicit_path:
            if env_var_set:
                logger.warning(
                    "admins_file_missing path=%s (ISTOTA_ADMINS_FILE set but file absent — "
                    "web admin dashboard will fail closed)",
                    path,
                )
            else:
                # DEBUG, not INFO: fires on every subprocess config load
                # (feeds/money facades call load_config()) where
                # ISTOTA_ADMINS_FILE isn't propagated. The env-var-set-but-
                # missing case above stays WARNING — that's a real misconfig.
                logger.debug(
                    "admins_file_default_missing path=%s (ISTOTA_ADMINS_FILE not set; "
                    "no web admins will be recognized)",
                    path,
                )
        return set()
    admins = set()
    for line in admins_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            admins.add(line)
    return admins


def _parse_briefing_specs(entries: "list | None") -> list[BriefingConfig]:
    """Parse ``[[briefings]]`` / ``[[default_briefings]]`` TOML entries.

    Shared by ``_parse_user_data`` (per-user briefings) and the top-level
    ``[[default_briefings]]`` parse so both accept the same
    name/cron/output/blocks shape.

    Legacy ``components =`` authoring is retired (retire-legacy-briefing-
    components spec): a stray ``components`` key is ignored with a one-line
    warning. The in-memory ``components`` field survives only as a
    migration-read carrier populated from the DB, never from TOML.
    """
    briefings: list[BriefingConfig] = []
    for b in entries or []:
        if not isinstance(b, dict):
            continue
        if "components" in b:
            logger.warning(
                "briefing %r: TOML `components =` authoring is retired and ignored; "
                "use `blocks` instead",
                b.get("name", "?"),
            )
        raw_blocks = b.get("blocks", [])
        briefings.append(BriefingConfig(
            name=b.get("name", ""),
            cron=b.get("cron", ""),
            title=b.get("title", ""),
            conversation_token=b.get("conversation_token", ""),
            output=b.get("output", "talk"),
            # Raw parsed dict passthrough — normalisation is the seeder's job so
            # the config layer stays decoupled from module internals.
            blocks=list(raw_blocks) if isinstance(raw_blocks, list) else [],
        ))
    return briefings


def _parse_shared_block_specs(entries: "list | None") -> list[BriefingSharedBlock]:
    """Parse ``[[briefing_shared_blocks]]`` TOML entries into dataclasses.

    Fail-soft: a non-dict entry, or one missing ``name``/``cron``, is skipped
    with a warning. ``sources`` is a raw ``[{kind, config}]`` passthrough —
    validation (allowed kinds) happens at generation time, matching how per-user
    briefing blocks defer normalisation to the seeder.
    """
    blocks: list[BriefingSharedBlock] = []
    for b in entries or []:
        if not isinstance(b, dict):
            logger.warning("[[briefing_shared_blocks]] entry is not a table; ignoring")
            continue
        name = b.get("name", "")
        cron = b.get("cron", "")
        if not name or not cron:
            logger.warning(
                "briefing_shared_blocks entry %r missing name/cron; ignoring",
                name or "?",
            )
            continue
        raw_sources = b.get("sources", [])
        directive = b.get("directive")
        blocks.append(BriefingSharedBlock(
            name=name,
            cron=cron,
            title=b.get("title", ""),
            directive=directive if isinstance(directive, str) and directive else None,
            render_mode=b.get("render_mode", "synthesis"),
            enabled=bool(b.get("enabled", True)),
            trusted=bool(b.get("trusted", False)),
            sources=list(raw_sources) if isinstance(raw_sources, list) else [],
        ))
    return blocks


def _parse_user_data(user_data: dict, user_id: str) -> UserConfig:
    """Parse a user data dict (from main config or per-user file) into UserConfig."""
    # Parse briefings
    briefings = _parse_briefing_specs(user_data.get("briefings", []))

    # Parse resources. After the Resources sunset only ``folder`` (and the
    # internal ``shared_file``) are live; obsolete credential types
    # (karakeep, monarch, ...) are absorbed into the secrets table by
    # _migrate_obsolete_resources, and ``base_url`` / ``api_key`` flow into
    # ``extra`` rather than flat fields.
    _resource_known_keys = {"type", "path", "name", "permissions"}
    resources = []
    for r in user_data.get("resources", []):
        extra = {k: v for k, v in r.items() if k not in _resource_known_keys}
        resources.append(ResourceConfig(
            type=r.get("type", ""),
            path=r.get("path", ""),
            name=r.get("name", ""),
            permissions=r.get("permissions", "read"),
            extra=extra,
            _allow_obsolete=True,
        ))

    # Backward-compat: migrate reminders_file string to a resource
    reminders_file = user_data.get("reminders_file", "")
    if reminders_file:
        resources.append(ResourceConfig(
            type="reminders_file",
            path=reminders_file,
            name="Reminders",
            permissions="read",
        ))

    # Parse credential sections as resources (server-side only, not synced to Nextcloud)
    monarch_data = user_data.get("monarch", {})
    if monarch_data.get("session_token"):
        resources.append(ResourceConfig(
            type="monarch",
            name="Monarch Money",
            extra={k: v for k, v in monarch_data.items()},
            _allow_obsolete=True,
        ))

    return UserConfig(
        display_name=user_data.get("display_name", user_id),
        email_addresses=user_data.get("email_addresses", []),
        timezone=user_data.get("timezone", "UTC"),
        briefings=briefings,
        resources=resources,
        log_channel=user_data.get("log_channel", ""),
        alerts_channel=user_data.get("alerts_channel", ""),
        max_foreground_workers=user_data.get("max_foreground_workers", 0),
        max_background_workers=user_data.get("max_background_workers", 0),
        disabled_skills=user_data.get("disabled_skills", []),
        trusted_email_senders=user_data.get("trusted_email_senders", []),
        quiet_email_senders=user_data.get("quiet_email_senders", []),
        disabled_modules=user_data.get("disabled_modules", []),
        routing=dict(user_data.get("routing", {}) or {}),
        default_destination=user_data.get("default_destination", "talk") or "talk",
        email_reply_routing=user_data.get("email_reply_routing", "origin+thread") or "origin+thread",
        # No `or` fallback: "" is a meaningful value here (follow the floor),
        # not an absent one.
        outbound_approval=str(user_data.get("outbound_approval", "") or ""),
        external_turn_display=str(
            user_data.get("external_turn_display", "collapsed") or "collapsed",
        ),
        briefing_email_html=bool(user_data.get("briefing_email_html", True)),
        timezone_follow_location=bool(
            user_data.get("timezone_follow_location", False)
        ),
    )


#: What a `shim_commands` entry may look like. A shim's name becomes a filename
#: in a directory on the model's PATH.
_SHIM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

#: Names `shim_commands` may not carry, whatever an operator writes, because
#: shimming one breaks machinery the *sandbox* depends on rather than merely
#: routing a build somewhere unexpected. See the refusal's own comment.
#:
#: `make` is deliberately absent: it is a routing judgement about Makefiles, and
#: an operator who knows theirs may make it.
_UNSHIMMABLE_COMMANDS = frozenset({
    # The sandbox starts its own network bridge with the interpreter, inside the
    # namespace, with the model's PATH in force.
    "python", "python3",
    # The shells and `env` are how every wrapped command is invoked in the first
    # place — the bridge is `/bin/sh -c`, and a shimmed `env` would route the
    # `exec env HTTPS_PROXY=… "$@"` that follows it.
    "sh", "bash", "env",
    # Host-side by design, each with credential machinery the container has no
    # copy of: git's credential helper is registered per task via
    # `GIT_CONFIG_KEY_*`, `gh` and `glab` on PATH are the policy wrapper, and
    # `istota-skill` is the skill proxy's client.
    "git", "gh", "glab", "istota-skill",
})

#: A versioned interpreter is the same refusal. `python3.12` would otherwise
#: walk past the literal names above, and it is the same binary.
_UNSHIMMABLE_RE = re.compile(r"^python\d+(\.\d+)*$")


def _parse_container_block(raw: object) -> dict:
    """Coerce ``[developer.container]`` into ``ContainerConfig`` kwargs.

    Corrects rather than refuses, one WARNING per correction, because
    ``load_config`` runs in the scheduler, the web app, the webhook receiver and
    every host-side skill CLI the proxy spawns per call — a typo here must not
    stop any of them from starting.

    ``backend`` is **retired** and a file still carrying it gets a warning
    rather than silence. Ignoring a key that used to decide where every build
    ran would be the worst of the three options: an operator who wrote
    ``backend = "none"`` to keep work on the host had that honoured until this
    release, and on the next deploy their devbox starts taking the work. Saying
    so on every start is the least this can do, and ``doctor``'s
    ``developer.container.backend`` check repeats it where someone will look.
    """
    if not isinstance(raw, dict):
        if raw:
            logger.warning(
                "[developer.container] is not a table; ignoring it. Where "
                "development work runs is derived from [devbox] enabled.",
            )
        return {}

    kwargs: dict = {}

    if "backend" in raw:
        logger.warning(
            "[developer.container] backend=%r is retired and ignored. Where "
            "development work runs is now derived from [devbox] enabled "
            "together with developer.enabled and developer.repos_dir, so the "
            "two switches cannot disagree. Delete the key; if you set it to "
            "%r to keep builds on the host, turn [devbox] enabled off instead.",
            raw["backend"], CONTAINER_BACKEND_NONE,
        )

    if "exec_socket_dir" in raw:
        value = raw["exec_socket_dir"]
        if isinstance(value, str) and value.strip().startswith("/"):
            kwargs["exec_socket_dir"] = value.strip().rstrip("/") or "/"
        else:
            logger.warning(
                "[developer.container] exec_socket_dir=%r is not an absolute "
                "path; using the default. A relative socket directory would "
                "anchor on whatever directory the daemon was started in.",
                value,
            )

    for name, caster, floor in (
        ("connect_timeout_seconds", float, 0.1),
        ("idle_timeout_seconds", int, 1),
    ):
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            logger.warning(
                "[developer.container] %s=%r is not a number; using the default",
                name, value,
            )
            continue
        if not math.isfinite(value):
            # `int(float("inf"))` raises OverflowError and `int(float("nan"))`
            # raises ValueError, and TOML spells both.
            logger.warning(
                "[developer.container] %s=%r is not finite; using the default",
                name, value,
            )
            continue
        kwargs[name] = caster(max(value, floor))

    if "shim_commands" in raw:
        value = raw["shim_commands"]
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            # A bare string iterates as characters, which would install a shim
            # called `n` and one called `p`. Same failure `sandbox_ro_paths`
            # had, so it gets the same explicit refusal.
            logger.warning(
                "[developer.container] shim_commands=%r is not a list; using "
                "the default set", value,
            )
        else:
            commands: list[str] = []
            for entry in value:
                name = str(entry).strip() if isinstance(entry, str) else ""
                # A shim is a file written into a directory on the model's PATH,
                # so the name has to be a bare command and nothing else. A `/`
                # would write outside `{dev_bin}`; a dot-prefix or a shell
                # metacharacter is not a command anyone means.
                if not name or not _SHIM_NAME_RE.match(name):
                    logger.warning(
                        "[developer.container] shim_commands entry %r is not a "
                        "plain command name; skipping it", entry,
                    )
                    continue
                if name in _UNSHIMMABLE_COMMANDS or _UNSHIMMABLE_RE.match(name):
                    # Not a preference. Each of these is machinery the *sandbox*
                    # itself runs, so shimming one breaks tasks that never touch
                    # a build. `build_bwrap_cmd` starts the network bridge as
                    # `python3 {bridge_path}` inside the namespace, through
                    # `/bin/sh -c` and `exec env`, with the model's PATH in
                    # force; git's credential helper is registered per task and
                    # exists only on the host; `gh` and `glab` on PATH are the
                    # policy wrapper; `istota-skill` is the skill proxy's
                    # client. The exec client is itself a Python script, and
                    # several `developer/skill.md` recipes parse forge output
                    # with `python3 -c`.
                    logger.warning(
                        "[developer.container] shim_commands entry %r is refused: "
                        "shimming it would route the sandbox's own machinery into "
                        "the container, which has no copy of it. Leave it on the "
                        "host.",
                        name,
                    )
                    continue
                if name not in commands:
                    commands.append(name)
            kwargs["shim_commands"] = commands

    return kwargs


def container_backend(config: "Config") -> str:
    """Where development work runs on this deployment: ``devbox`` or ``none``.

    The label form of :func:`devbox_container_backend`, for a log line or a
    ``doctor`` detail. Derived from three settings and nothing else, so the
    answer cannot contradict what the Ansible role built.
    """
    return (
        CONTAINER_BACKEND_DEVBOX if devbox_container_backend(config)
        else CONTAINER_BACKEND_NONE
    )


def devbox_container_backend(config: "Config") -> bool:
    """Is this deployment routing development work into the devbox?

    **Configuration alone, and deliberately never availability.** Asking
    whether the container is actually up would make a stopped devbox silently
    reroute builds onto the host — the same commands, a different containment
    posture, and no error anywhere. A configured-but-unreachable transport has
    to fail loudly instead, which it does: the shims exit 120 and say why.

    It also says nothing about whether a *task* may reach the container. That
    is ``"developer" in authorized_skills``, decided in the executor, which is
    where a security decision belongs.

    ``developer.repos_dir`` is in the conjunction because the exec server takes
    it as its containment root; there is nothing to mount and nothing to
    contain without one.
    """
    dev = getattr(config, "developer", None)
    if not getattr(dev, "enabled", False):
        return False
    # Stripped, so a whitespace-only value reads as absent. `doctor`'s
    # re-derivation strips too, and a mismatch between the two produces a
    # permanent false drift FAIL telling an operator to restart a daemon that
    # is already running the right answer. Whitespace is worse than useless
    # here on its own terms as well: the exec server takes this as its
    # containment root.
    if not str(getattr(dev, "repos_dir", "") or "").strip():
        return False
    return bool(getattr(getattr(config, "devbox", None), "enabled", False))


def exec_socket_dir(config: "Config", user_id: str) -> Path | None:
    """The per-user directory holding this user's exec socket, or None.

    The *directory*, never the socket file, is what gets mounted and bound: a
    server restart unlinks and recreates the inode, and a bind mount of the file
    itself strands the other side against a dead target. Same precedent as
    ``devbox_proxy._default_socket_path``.

    **One spelling, and this is it.** Three callers resolve the socket through
    here — the executor's bwrap bind, ``doctor``'s transport check, and the
    devbox skill CLI, which reads it in its own host-side process rather than
    from an environment variable a model could set. ``[devbox]`` carries no
    mirror of the key for that reason.
    """
    dev = getattr(config, "developer", None)
    container = getattr(dev, "container", None)
    parent = getattr(container, "exec_socket_dir", "")
    if not parent or not user_id:
        return None
    return Path(parent) / user_id


def exec_socket_path(config: "Config", user_id: str) -> Path | None:
    """``{exec_socket_dir}/{user_id}/exec.sock``, or None."""
    directory = exec_socket_dir(config, user_id)
    return None if directory is None else directory / EXEC_SOCKET_NAME


#: The socket's filename inside the per-user directory. A constant rather than a
#: setting: both sides compose the path themselves and there is nothing to
#: negotiate, and the shims bake the result in.
EXEC_SOCKET_NAME = "exec.sock"


#: ``executor.CONTROL_DIR_NAME``, restated. `config.py` sits below the executor
#: and is loaded by the daemon, the web app, the webhook receiver, every CLI
#: invocation and every host-side skill CLI the proxy spawns per call — so an
#: import of `istota.executor` here would pull that whole graph (`.brain`
#: included) onto all of them for one string. Held equal by
#: ``tests/test_task_control_dir.py``, the same way `sandbox_cache_sweeper`
#: restates the cache subdirectory names.
_CONTROL_DIR_NAME = ".control"

#: Said once per process per entry, not once per ``load_config`` — same
#: mechanism as ``_TMUX_NO_FALLBACK_NOTICE_SAID`` below, and for a sharper
#: reason: the skill proxy spawns a host-side CLI per model tool call and each
#: one loads the config. Keyed on the entry as written, so an operator who
#: narrows one of two overlapping entries still hears about the other.
_RO_PATH_CONTROL_TREE_WARNED: set[str] = set()


def _warn_ro_paths_over_control_tree(config: "Config") -> None:
    """Warn where a ``sandbox_ro_paths`` entry would bind the control tree.

    ``sandbox_ro_paths`` is bound verbatim, and one broad entry has cost this
    project every database once: ``["/srv/app"]`` was the shipped default,
    ``db_path`` and ``module_data_dir`` lived under it, and a single read-only
    bind that mentioned no database exposed the framework DB, every user's
    module DB and the local backups. The masks were the fix for that.

    ``{temp_dir}/.control`` has no mask behind it and cannot have one: a task
    has to *read* its own control directory, which is what the per-task
    ``extra_ro_binds`` entry is for. So the only thing between a broad entry
    and every task of every user's assembled prompt — retrieved memory,
    knowledge facts, conversation history, the request itself — is that nobody
    writes one. This says so at load rather than letting it be found later.

    A warning rather than a refusal, on the same reasoning
    ``_validate_sandbox_ro_paths`` refuses only ``/``: the operator's own
    directory layout is theirs, an entry can overlap for reasons this cannot
    see, and refusing to load would take the daemon down over a bind that is
    otherwise working. Gated on the *requested* ``sandbox_enabled`` flag, not
    the effective one, for the reason the credential pairing above is: nothing
    is bound at all with the sandbox off, and ``effective_sandboxing`` spawns.

    **Once per process per entry**, the way ``_validate_brain_fallback`` is
    and for the same reason: ``load_config`` runs on every host-side skill CLI
    the proxy spawns, which is once per model tool call, and a multi-line
    warning on each of those is noise on a path the model reads rather than a
    notice anybody acts on.

    A **relative** ``temp_dir`` is out of scope and is skipped rather than
    guessed at: ``Path.resolve()`` would answer against the calling process's
    cwd, which differs between the daemon, the web app and a skill CLI, so the
    same config would warn in one process and not another. Nothing forces the
    field absolute and both deploy paths render one.
    """
    if not config.security.sandbox_enabled:
        return
    if not config.security.sandbox_ro_paths:
        return
    if not Path(config.temp_dir).is_absolute():
        logger.debug(
            "[security] sandbox_ro_paths: not checked against the task control "
            "tree, because temp_dir (%s) is relative and would resolve against "
            "this process's working directory", config.temp_dir,
        )
        return
    try:
        control_root = Path(config.temp_dir).resolve() / _CONTROL_DIR_NAME
    except (OSError, ValueError):  # pragma: no cover - an unresolvable temp_dir
        return
    for entry in config.security.sandbox_ro_paths:
        try:
            resolved = Path(entry).resolve()
        except (OSError, ValueError):
            continue
        # Both directions. Above the root binds every user's whole tree; inside
        # it binds one user's, or one task's, which is smaller and no more
        # acceptable — and is the shape a well-meaning "let the model read its
        # own control dir" edit takes.
        overlaps = (
            resolved == control_root
            or control_root.is_relative_to(resolved)
            or resolved.is_relative_to(control_root)
        )
        if overlaps:
            if entry in _RO_PATH_CONTROL_TREE_WARNED:
                continue
            _RO_PATH_CONTROL_TREE_WARNED.add(entry)
            logger.warning(
                "[security] sandbox_ro_paths entry %r would bind the task "
                "control tree (%s) into every sandbox: the framework writes "
                "each task's assembled prompt, briefing metadata and prepared "
                "image attachments there, and a task could then read every "
                "other task's. Narrow the entry to the directory you actually "
                "need; the control directory a task needs is bound for it "
                "already.",
                entry, control_root,
            )


def _validate_sandbox_ro_paths(raw: object) -> list[str]:
    """Coerce ``[security] sandbox_ro_paths`` to a safe list of absolute paths.

    This key went from inert to live (nothing read it from TOML before), so a
    value that used to be harmless now becomes bind mounts. The failure mode
    that matters is a bare string: ``sandbox_ro_paths = "/srv/app"`` is a
    plausible typo, and ``for p in "/srv/app"`` iterates *characters*, so
    ``build_bwrap_cmd`` would ro-bind ``/`` — the entire host, including the
    config file and every other user's data — into the sandbox. Reject rather
    than guess: silently wrapping a string would also mean silently accepting
    the next malformed shape.

    ``/`` is refused outright for the same reason; a mount that broad defeats
    every other bind decision in the function.
    """
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        logger.error(
            "[security] sandbox_ro_paths must be a list of paths, got %s — "
            "ignoring it. A bare string would be iterated character by "
            "character and bind-mount the host root.",
            type(raw).__name__,
        )
        return []
    cleaned: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            logger.error(
                "[security] sandbox_ro_paths: ignoring non-path entry %r", entry,
            )
            continue
        resolved = Path(entry).resolve()
        if resolved == Path("/"):
            logger.error(
                "[security] sandbox_ro_paths: refusing to bind the host root "
                "(%r) into the sandbox", entry,
            )
            continue
        cleaned.append(entry)
    return cleaned


CONFIRM_SENDER_MATCH_POLICIES = ("off", "verify", "gate")


def _validate_confirm_sender_match(raw: object, authserv_id: str) -> str:
    """Validate ``[email] confirm_sender_match``, raising on anything unusable.

    Accepts the legacy booleans as well as the three names, because this key
    shipped as a bool and both deploy paths still render one: TOML ``false`` and
    ``true`` map to ``off`` and ``gate``, and so do the strings ``"false"`` and
    ``"true"``, which is what Ansible produces when a YAML boolean reaches a
    quoted template slot. Existing deployments therefore keep their exact
    behaviour with no edit.

    Raises rather than warning and falling back, matching
    ``outbound_approval_floor`` and for the same reason: every wrong answer here
    is unsafe in one direction or the other and there is no neutral one to pick.
    Falling back to ``off`` would disable a gate the operator asked for; falling
    back to ``gate`` would hold every self-sent message on an instance that
    deliberately wrote ``off``.

    ``verify`` additionally requires ``authserv_id``. Without it the verdict is
    read off whichever `Authentication-Results` header sat on top, and in the
    case the gate exists for — the MTA no longer stamping — that header is the
    sender's own. Letting `verify` run unscoped would gate on a value the
    attacker writes, which is worse than not gating at all, because it reads as
    protection. Refusing at load is how it "requires" rather than "prefers".
    """
    if isinstance(raw, bool):
        return "gate" if raw else "off"

    value = raw.strip().lower() if isinstance(raw, str) else ""
    if value in ("true", "false"):
        return "gate" if value == "true" else "off"

    if value not in CONFIRM_SENDER_MATCH_POLICIES:
        raise ValueError(
            f"[email] confirm_sender_match = {raw!r} is not valid. Use one of "
            f"{', '.join(CONFIRM_SENDER_MATCH_POLICIES)} (the legacy true/false "
            "still load, as gate and off)."
        )

    if value == "verify" and not authserv_id:
        raise ValueError(
            "[email] confirm_sender_match = \"verify\" requires [email] "
            "authserv_id to be set. Without it the DMARC verdict is read from "
            "whichever Authentication-Results header arrived on top, which the "
            "sender can write — so the gate would be keyed on a value the "
            "sender chooses. Set authserv_id to your receiving MTA's own "
            "authserv-id, or use \"gate\" to hold every self-addressed message."
        )

    return value


def _validate_authserv_id(raw: object) -> str:
    """Validate ``[email] authserv_id``, warning rather than raising.

    An authserv-id is a single token. The operator is told to copy it off a real
    ``Authentication-Results`` header, where the very next thing after it may be
    an RFC 8601 version number (``mx.example.com 1;``), so pasting a whole prefix
    is the plausible mistake. Nothing matches such a value and every message then
    reads as ``unstamped`` — loud, so no message is silently mishandled, but with
    nothing pointing at the config value that caused it. This says so once at
    load.

    A warning, not a raise, unlike ``outbound_approval_floor``: that one is a
    security floor whose every wrong answer is unsafe, while a bad value here
    fails toward noise. Trimmed but otherwise passed through, so an operator who
    genuinely has an unusual id keeps it.
    """
    value = raw if isinstance(raw, str) else ""
    value = value.strip()
    if value and any(ch.isspace() or ch in ';"(' for ch in value):
        logger.warning(
            "[email] authserv_id = %r contains whitespace or a delimiter. It must "
            "be the single token before the semicolon in your MTA's "
            "Authentication-Results header, without any version number that "
            "follows it. Nothing will match this value, so every message will "
            "report as unstamped.",
            value,
        )
    return value


def _validate_outbound_approval_floor(raw: object) -> str:
    """Validate ``[email] outbound_approval_floor``, raising on anything else.

    Deliberately a hard failure rather than the warn-and-fall-back other
    enum-ish keys use (``[web] token_storage``, ``email_reply_routing``). Every
    wrong answer here is unsafe in one direction or the other and there is no
    neutral one to pick: falling back to ``off`` disables a gate the operator
    asked for, and silently falling back to ``untrusted`` overrides an operator
    who deliberately wrote ``off``. A typo in a security floor should stop the
    process, not pick a policy on the operator's behalf.
    """
    from .outbound_policy import VALID_POLICIES

    value = raw if isinstance(raw, str) else ""
    value = value.strip()
    if value not in VALID_POLICIES:
        raise ValueError(
            f"[email] outbound_approval_floor={raw!r} is not valid. "
            f"Use one of: {', '.join(VALID_POLICIES)}."
        )
    return value


def _valid_task_queue(raw: object) -> str:
    """Validate ``[scheduler] email_task_queue``, warning and defaulting on junk.

    A typo here is invisible and expensive: ``queue`` goes into `tasks` verbatim
    with no CHECK constraint, while `claim_task` and both dispatch scans filter
    on the literal ``'foreground'``/``'background'``. So ``email_task_queue =
    "backgroud"`` produces pending rows no worker is ever spawned for and no
    claim ever matches — every inbound message on the instance sits until
    `fail_ancient_pending_tasks` fails it hours later and tells the user their
    task was cancelled.

    Warn-and-default rather than raise (unlike `outbound_approval_floor`,
    which is a security floor with no safe fallback): both values here are
    safe, and the default is the one this feature exists to choose.
    """
    value = raw.strip() if isinstance(raw, str) else ""
    if value in ("foreground", "background"):
        return value
    logger.warning(
        "[scheduler] email_task_queue=%r is not a queue; using 'background'. "
        "Valid values are 'foreground' and 'background'.",
        raw,
    )
    return "background"


# Dotted keys the dataclass walk must not touch, each because parsing it takes
# a decision the walk has no way to make. Grouped by what the decision is.
#
# Keeping this list explicit is the whole discipline: a key here is a claim that
# something below reads it, and `tests/test_config_mapper.py` holds the two
# halves together by requiring every entry to name a real field or section.
_PARSED_BY_HAND = frozenset({
    # Collections of dataclasses, built by their own parsers from list-of-table
    # TOML rather than from a field of a declared type.
    "users",
    "default_briefings",
    "briefing_shared_blocks",

    # Cross-field: `confirm_sender_match = "verify"` is only meaningful with an
    # authserv-id to scope the verdict to, so the validator has to see both.
    "email.confirm_sender_match",
    "email.authserv_id",
    "email.outbound_approval_floor",

    # Sections whose sub-structure is not a plain field tree: `[models.aliases]`
    # values are kept verbatim in either of two shapes for the namespace-aware
    # validation loop below, and `[experimental] features` is checked against
    # KNOWN_FEATURES.
    "models",
    "experimental",
})
"""Dotted keys the walk must not touch because something below parses them.

Every entry names a real field on the dataclass tree, which is what
``tests/test_config_mapper.py`` checks -- an entry that stops naming one is a
key nothing reads any more, and the walk would have been the thing to notice.
"""

_RETIRED = frozenset({
    "skills",
    "ntfy",
    "site.enabled",
    "site.base_path",
    "security.sandbox_admin_db_write",
    # Still honoured, by `_apply_renamed_keys`, which warns in its own terms.
    "scheduler.istota_file_poll_interval",
})
"""Keys that are no longer fields at all, each with its own migration warning.

Held out of the walk so an operator gets the specific line naming what replaced
the key rather than a generic "unrecognised" beside it. The opposite of
:data:`_PARSED_BY_HAND`: an entry here that *does* name a live field is the
error, and the same test checks that direction too.
"""

_NOT_CONFIGURATION = frozenset({
    # Set by the loader itself from the path it resolved, and exported as
    # `ISTOTA_CONFIG_PATH` to every task, cron `command:` job, heartbeat shell
    # command and host-side skill CLI. A file naming a different path would
    # have the daemon read one config and every subprocess read another --
    # breaking the invariant `executor.py` states where it does the export --
    # and `scheduler` derives the triggers directory from it as well.
    "config_path",
    # A test seam deciding where every skill body and skill CLI module is
    # resolved from. `admin_config_view` deliberately excludes it from the
    # config pane as "meaningless to an operator", so an operator who set it
    # would have no surface anywhere showing they had.
    "bundled_skills_dir",
    # Overwritten by `load_admin_users()` a few lines below, so setting it here
    # never did anything. That makes it exactly the false negative the
    # unknown-key report exists to remove, on an authorization-shaped key.
    "admin_users",
})
"""Declared fields that are not settings, and must not be writable from TOML.

Rejected rather than skipped: being a real field is what would otherwise keep
these out of the unknown-key report, so a skip would hand an operator silence
in the one case where nothing else will ever tell them.
"""

_HANDWRITTEN = _PARSED_BY_HAND | _RETIRED

_RENAMED_KEYS = {
    # The one key in the file that had two accepted spellings. The old loader
    # carried it as a nested `get` fallback, which a walk over field names
    # cannot express -- a hook is keyed on the *new* name and never sees the
    # old one. Kept working rather than retired: silently reverting to the
    # default is the failure this whole change is about.
    ("scheduler", "istota_file_poll_interval"): "tasks_file_poll_interval",
}


def _apply_renamed_keys(data: dict, config: "Config") -> None:
    """Honour a key's previous spelling, and say that it moved.

    Runs after the walk so the current spelling always wins where both are
    present. Warns either way, because a config carrying the old name will
    otherwise carry it for years -- the file is rewritten by Ansible on every
    deploy but not on the Docker or hand-written shapes.
    """
    for (section, old), new in _RENAMED_KEYS.items():
        block = data.get(section)
        if not isinstance(block, dict) or old not in block:
            continue
        logger.warning(
            "[%s] %s was renamed to %s. The old name still works and will be "
            "removed; rename the key.", section, old, new,
        )
        if new in block:
            continue
        target = getattr(config, section, None)
        value = coerce_int(block[old], f"{section}.{old}")
        if target is not None and value is not _KEEP:
            setattr(target, new, value)


_LEGACY_BRAIN_DEFAULT_TARGETS = ("claude_code", "tmux")
"""The blocks the retired top-level ``model`` / ``effort`` migrate onto.

Both, not just ``claude_code``, and that is what makes the migration exactly
behaviour-preserving rather than approximately. The top-level value *was* the
default for whichever brain ran, and both of these are the same ``anthropic``
namespace running the same ``claude`` binary, so the value is equally valid in
either. A deployment with ``kind = "tmux_claude"`` and a top-level ``model``
would otherwise lose its model on upgrade — silently, since an empty model is a
legal request meaning "the CLI's own default".

``native`` is deliberately absent. That is the whole defect: the top-level value
is written in the Anthropic vocabulary and cannot carry to an
``openai_compat`` endpoint, so migrating it there would re-create ISSUE-418
inside the fix.
"""


def _apply_legacy_brain_defaults(data: dict, config: "Config") -> None:
    """Migrate the retired top-level ``model`` / ``effort`` onto the CLI brains.

    Runs after the walk, so a block that sets its own value keeps it — the new
    spelling always wins, and only an unset field is filled. Warns whenever the
    old key is present at all, because the file is rewritten by Ansible on every
    deploy but not on the Docker or hand-written shapes, so an unwarned config
    carries the old name for years.

    Keyed on the key's **presence in the file**, not on its resolved value: the
    dataclass default and an explicit ``model = ""`` are the same value, so
    presence is the only way to tell an operator who wrote the retired key from
    one who has already migrated. An explicitly empty key therefore warns too,
    which is right — it is still the retired spelling sitting in the file. The
    *value* comes off ``config``, so it has been through the walk's own type
    coercion.
    """
    for old in ("model", "effort"):
        if old not in data:
            continue
        value = getattr(config, old, "")
        targets = [
            name for name in _LEGACY_BRAIN_DEFAULT_TARGETS
            if not getattr(getattr(config.brain, name, None), old, "")
        ]
        logger.warning(
            "[config] top-level `%s` is deprecated: it is the claude_code "
            "brain's default, not a deployment-wide one, and setting it here "
            "shadowed every other brain's own (ISSUE-418). Move it to "
            "[brain.claude_code] %s and/or [brain.tmux] %s.", old, old, old,
        )
        if not value:
            continue
        for name in targets:
            block = getattr(config.brain, name, None)
            if block is not None:
                setattr(block, old, value)
    _warn_native_lost_its_only_model(data, config)


def _warn_native_lost_its_only_model(data: dict, config: "Config") -> None:
    """Name the one upgrade this change can leave with no model at all.

    A deployment that runs the native brain and set its model *only* at the top
    level used to work, because the executor substituted that value into every
    request — which is exactly the ISSUE-418 defect, an Anthropic id on an
    ``openai_compat`` endpoint, but it did resolve to something. The migration
    deliberately refuses to carry that value onto ``[brain.native]``, so such a
    deployment now sends an empty model, which most endpoints reject outright.

    Silence would be the worst outcome here: the failure is at the provider, per
    task, and names nothing an operator could connect to an upgrade. So this
    says it once at load, naming the key to set. It cannot be a refusal — a
    config that fails to load takes the whole daemon down, and this is a
    deployment that may also reach native only as a fallback, where the primary
    is fine.

    Reachability is asked the way ``_validate_brain_fallback`` asks it: kind,
    fallback, or any ``source_type_overrides`` target. A deployment that cannot
    reach native has nothing to warn about.
    """
    if "model" not in data or not getattr(config, "model", ""):
        return
    if getattr(config.brain.native, "model", ""):
        return
    targets = {config.brain.kind, config.brain.fallback}
    targets |= set((config.brain.source_type_overrides or {}).values())
    if "native" not in targets:
        return
    logger.warning(
        "[config] the native brain is reachable but [brain.native] model is "
        "empty, and the top-level `model` is no longer applied to it "
        "(ISSUE-418) — an Anthropic model id cannot carry to an openai_compat "
        "endpoint. Native tasks will send an empty model, which most endpoints "
        "reject. Set [brain.native] model.",
    )


def _advisor_model(raw: object, key: str) -> object:
    """The advisor model name, which must be a string or nothing.

    Keeps its own message rather than taking the generic one: this field is
    resolved through the alias table like `model`, and an operator who wrote a
    table here has made a different mistake from one who wrote a number.
    """
    if isinstance(raw, str):
        return raw
    logging.getLogger("istota.config").warning(
        "advisor_model must be a string, got %s; ignoring", type(raw).__name__,
    )
    return _KEEP


def _non_negative_int(raw: object, key: str) -> object:
    """A count where zero is meaningful and a negative value is a typo."""
    value = coerce_int(raw, key)
    if value is _KEEP:
        return _KEEP
    if value < 0:
        logger.warning(
            "[config] %s=%r must be >= 0; keeping the default", key, raw,
        )
        return _KEEP
    return value


def _positive_float(raw: object, key: str) -> object:
    """A number that must be greater than zero to mean anything.

    The generic float coercion already refuses a bool and a non-finite value --
    NaN compares false against every threshold, so it would switch off the
    comparison it feeds rather than failing. This adds the sign, for a ceiling
    where zero or negative would mean the same thing.
    """
    value = coerce_float(raw, key)
    if value is _KEEP:
        return _KEEP
    if value <= 0:
        logger.warning(
            "[config] %s=%r is not a positive number; keeping the default", key, raw,
        )
        return _KEEP
    return value


def _positive_int(raw: object, key: str) -> object:
    """A duration or count where zero is not a smaller value but a different mode.

    ``_non_negative_int`` beside this one exists for the settings where ``0``
    means *unlimited*; this is for the ones where it means *broken*.
    """
    value = coerce_int(raw, key)
    if value is _KEEP:
        return _KEEP
    if value <= 0:
        logger.warning(
            "[config] %s=%r must be greater than 0; keeping the default", key, raw,
        )
        return _KEEP
    return value


def _forge_cli_list(raw: object, key: str) -> object:
    """A forge-CLI policy list, tolerating the bare-string hand-edit."""
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, (list, tuple)):
        return [str(entry) for entry in raw]
    logger.warning(
        "[config] %s=%r is not a list of strings; keeping the default", key, raw,
    )
    return _KEEP


def _one_of(*valid: str) -> Hook:
    """A hook for a field whose value must come from a closed vocabulary.

    Warns naming the valid values and keeps the dataclass default, which is the
    established handling for these: an unknown mode is a typo, and picking the
    safe default beats both guessing and refusing to boot. The two settings
    where a wrong value is a *security* decision rather than a mode --
    `email.outbound_approval_floor` and `email.confirm_sender_match` --
    deliberately do not use this and raise instead.
    """

    def hook(raw: object, key: str) -> object:
        if raw in valid:
            return raw
        logger.warning(
            "[config] %s=%r is not a known value (expected %s); using the default",
            key, raw, " or ".join(repr(v) for v in valid),
        )
        return _KEEP

    return hook


# Per-key parsing that is more than a type coercion but less than a section.
# A hook takes the raw TOML value and the dotted key, and returns the value to
# set -- or `_KEEP` to leave the dataclass default standing.
_CONFIG_HOOKS: dict[str, Hook] = {
    "web.auth": _one_of("nextcloud", "none"),
    "web.token_storage": _one_of("ephemeral", "encrypted"),
    # Normalised from three historical spellings, and the retired `backend` key
    # is warned about rather than ignored -- an operator who wrote
    # `backend = "none"` to keep builds off the container had that honoured
    # until it was retired.
    "developer.container": lambda raw, key: ContainerConfig(
        **_parse_container_block(raw)
    ),
    # A bare string is the plausible hand-edit, and the generic list coercion
    # would iterate it into one rule per character -- eighteen entries that
    # match nothing and warn about nothing. Read it as the single entry it was
    # meant to be.
    "developer.forge_cli_extra_denied": lambda raw, key: _forge_cli_list(raw, key),
    "developer.forge_cli_permit": lambda raw, key: _forge_cli_list(raw, key),
    # An empty string means "unset" here, not a relative path of `.`.
    "security.sandbox_cache_dir": lambda raw, key: str(raw or ""),
    "security.sandbox_cache_max_gb": _positive_float,
    # Each of these validates against a closed vocabulary or a path policy and
    # warns in its own terms. They are the validators the walk exists to leave
    # alone.
    "security.sandbox_ro_paths": lambda raw, key: _validate_sandbox_ro_paths(raw),
    "scheduler.email_task_queue": lambda raw, key: _valid_task_queue(raw),
    # A zero-second long-poll is a round trip that cannot carry news, and it
    # used to be worse than useless: the same number was the `asyncio.wait`
    # deadline for the whole cycle, so `0` cancelled every room mid-flight and
    # turned Talk inbound into a silent no-op (ISSUE-399).
    "scheduler.talk_poll_timeout": _positive_int,
    # Load-bearing arithmetic since ISSUE-399: the cycle's own deadline is
    # `talk_poll_timeout + talk_poll_wait`, so a negative here puts the deadline
    # back *inside* the server's hold and restores the defect that pair exists
    # to remove.
    "scheduler.talk_poll_wait": _positive_float,
    # 0 is a mode here (every cycle a full sweep, i.e. no gate), which is why
    # this one is non-negative rather than positive. A negative is a typo that
    # would read as that mode without saying so.
    "scheduler.talk_poll_full_sweep_interval": _non_negative_int,
    # Positive rather than non-negative, unlike the sweep interval above, and
    # the difference is not stylistic. `0` there is a *mode* (every cycle a
    # full sweep, i.e. no gate); here it is a loop. The reconciliation pass
    # re-runs `list_conversations` — the endpoint that enumerates every
    # conversation and computes `lastMessage` for each, the expensive one this
    # whole design exists to stop calling six times a minute — so a zero
    # interval reissues it as fast as the loop can schedule it.
    "talk.signaling.room_sync_interval": _positive_int,
    # At `0` the ceiling clamps every reconnect delay to zero rather than
    # crashing: `backoff_delay` raises its floor to meet the ceiling, so a
    # watcher failing at connect (bad URL, refused TLS) reconnects as fast as
    # the loop can schedule it — which is the one thing the jittered floor is
    # there to prevent.
    "talk.signaling.reconnect_backoff_max": _positive_int,
    "advisor_model": _advisor_model,
    # 0 means unlimited here, so a negative value cannot be clamped to 0 -- that
    # reads as the opposite of what someone typing one meant.
    "health.max_document_bytes": _non_negative_int,
    # A brain name is compared literally downstream, so surrounding whitespace
    # in a rendered config is a name that matches nothing.
    "brain.fallback": lambda raw, key: (
        raw.strip() if isinstance(raw, str) else _KEEP
    ),
    # Both sides are stringified: a TOML table can key on a bare word and value
    # it with a number, and every consumer of this map indexes it with strings.
    "brain.source_type_overrides": lambda raw, key: (
        {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else _KEEP
    ),
    # Same reason as `brain.fallback`, one level down: each entry is compared
    # literally against the buildable kinds, so a stray space in a rendered
    # config is a name that matches nothing and grants nothing — and this list
    # is the gate on which brains a room may pin, so a silently inert entry
    # reads to an operator as a feature that does not work. An empty string
    # survives the generic list coercion and would sit in the admin config view
    # as a kind, so blanks go too. Stringified for the reason the override
    # map's values are: TOML will hold a bare number quite happily.
    "brain.room_selectable": lambda raw, key: (
        [name for name in (str(entry).strip() for entry in raw) if name]
        if isinstance(raw, (list, tuple))
        else _warn(key, raw, "a list of brain kinds")
    ),
}


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from TOML file."""
    if config_path is None:
        # `ISTOTA_CONFIG_PATH` lets a parent process (e.g. the scheduler)
        # propagate its loaded config to subprocesses whose cwd no longer
        # contains the relative `config/config.toml` candidate.
        env_path = os.environ.get("ISTOTA_CONFIG_PATH")
        if env_path:
            candidate = Path(env_path)
            if candidate.exists():
                config_path = candidate

    if config_path is None:
        # Look for config in standard locations
        candidates = [
            Path("config/config.toml"),
            Path.home() / "src/config/config.toml",
            Path.home() / ".config/istota/config.toml",
            Path("/etc/istota/config.toml"),
        ]
        for candidate in candidates:
            try:
                if candidate.exists():
                    config_path = candidate
                    break
            except PermissionError:
                continue

    if config_path is None or not config_path.exists():
        # Return default config
        return Config()

    with open(config_path, "rb") as f:
        data = tomli.load(f)

    config = Config()
    config.config_path = config_path

    # The mechanical half of the load: every field whose TOML key is its own
    # name and whose only requirement is its declared type. See
    # `istota.config_mapper` for why this is a walk over the dataclasses rather
    # than a line per key -- three classes of defect came out of the second copy
    # of the schema this replaces.
    #
    # Sections carrying real judgement are named in `_HANDWRITTEN` and parsed by
    # hand below; the walk neither maps nor reports those.
    unknown: list[str] = []
    apply_section(
        config, data, hooks=_CONFIG_HOOKS, unknown=unknown,
        skip=_HANDWRITTEN, reject=_NOT_CONFIGURATION,
    )
    _apply_renamed_keys(data, config)
    # After the walk, so a `[brain.claude_code] model` in the same file wins
    # over the top-level key it replaces, and before anything reads a brain
    # block: `_validate_alias_overrides` below resolves names through the brain.
    _apply_legacy_brain_defaults(data, config)
    report_unknown(unknown, config_path)

    if "users" in data:
        for nc_username, user_data in data["users"].items():
            config.users[nc_username] = _parse_user_data(user_data, nc_username)

    if "email" in data:
        # The one genuinely cross-field parse in the file, and the reason it
        # cannot be a hook: `confirm_sender_match = "verify"` is only meaningful
        # with an authserv-id to scope the verdict to -- unscoped, the verdict
        # comes off whichever header arrived on top, which the sender writes.
        # So the validator has to see both, which means it runs after the walk
        # has settled `authserv_id` rather than while it is setting fields.
        #
        # Both of these raise rather than warning-and-defaulting. There is no
        # safe value to fall back to: one names an inbound authentication
        # policy and the other an outbound approval floor, and picking either
        # direction overrides an operator who meant the other.
        email = data["email"]
        config.email.authserv_id = _validate_authserv_id(
            email.get("authserv_id", config.email.authserv_id)
        )
        config.email.confirm_sender_match = _validate_confirm_sender_match(
            email.get("confirm_sender_match", config.email.confirm_sender_match),
            config.email.authserv_id,
        )
        config.email.outbound_approval_floor = _validate_outbound_approval_floor(
            email.get("outbound_approval_floor", config.email.outbound_approval_floor)
        )

    if "ntfy" in data:
        logger.warning(
            "[ntfy] block in config.toml is no longer used — ntfy is now per-user "
            "and configured via the secrets table (web settings or `istota secret`)."
        )

    if "skills" in data:
        # The [skills] section is obsolete: progressive disclosure collapsed
        # into the single-axis selection model (selected ⇒ eager, else ⇒ menu),
        # so there are no skill-routing knobs left. Warn but don't fail so an
        # operator's stale config keeps loading.
        logger.warning(
            "[skills] block in config.toml is no longer used — skill disclosure "
            "is now intrinsic (no progressive_disclosure / always_eager / "
            "auto_lazy_threshold_chars knobs)."
        )

    # [models] table — operator-controlled alias registry. The mapping is
    # parsed here, then applied globally below via brain._roles.set_alias_overrides
    # after every other config layer has settled. Each brain consults the
    # global override table inside its own resolve_alias() call. The old
    # [models.roles] key is a HARD RENAME — no longer read; a stale one still
    # present logs a one-time migration WARNING (detection only, not honoured).
    if "models" in data:
        models_section = data["models"]
        if not isinstance(models_section, dict):
            models_section = {}
        aliases = models_section.get("aliases", {})
        if not isinstance(aliases, dict):
            aliases = {}
        if "roles" in models_section:
            logging.getLogger("istota.config").warning(
                "[models.roles] is ignored — rename it to [models.aliases]. "
                "The old key no longer configures anything."
            )
        # Preserve each alias value verbatim: a bare string (legacy flat) or a
        # per-namespace table stays as-is. set_alias_overrides normalizes both
        # shapes (and drops malformed values with a warning) — keeping the raw
        # structure here lets the namespace-aware validation loop below inspect it.
        config.models = ModelsConfig(aliases={str(k): v for k, v in aliases.items()})

    if "experimental" in data:
        exp = data["experimental"]
        feats = exp.get("features", []) if isinstance(exp, dict) else []
        if not isinstance(feats, list):
            feats = []
        config.experimental = ExperimentalConfig(features=[str(f) for f in feats])
        from .experimental import KNOWN_FEATURES
        for f in config.experimental.features:
            if f not in KNOWN_FEATURES:
                logger.warning(
                    "[experimental] unknown feature %r — typo or stale flag", f,
                )

    if "default_briefings" in data:
        # The canonical shared briefing set. Same name/cron/output/blocks shape
        # as ``[[users.X.briefings]]`` — parsed via the shared helper (fail-soft:
        # a bad entry is skipped with a warning inside _parse_briefing_specs).
        raw_defaults = data["default_briefings"]
        if isinstance(raw_defaults, list):
            config.default_briefings = _parse_briefing_specs(raw_defaults)
        else:
            logger.warning("[[default_briefings]] must be a list of tables; ignoring")

    # Module-owned shared briefing blocks. An explicit ``[[briefing_shared_blocks]]``
    # (operator/Ansible) replaces the batteries-included defaults wholesale; an
    # absent section seeds ``DEFAULT_SHARED_BLOCKS`` (batteries-included). An
    # explicit empty list opts out entirely.
    if "briefing_shared_blocks" in data:
        raw_shared = data["briefing_shared_blocks"]
        if isinstance(raw_shared, list):
            config.briefing_shared_blocks = _parse_shared_block_specs(raw_shared)
        else:
            logger.warning(
                "[[briefing_shared_blocks]] must be a list of tables; ignoring"
            )
    else:
        config.briefing_shared_blocks = _parse_shared_block_specs(DEFAULT_SHARED_BLOCKS)

    if "site" in data:
        s = data["site"]
        # The agent-writable static web root was removed (ISSUE-194). A stale
        # block from a pre-removal deploy must not fail load, but the operator
        # needs to know nothing is serving that directory on istota's behalf
        # any more — and that nginx may still be.
        retired = [k for k in ("enabled", "base_path") if k in s]
        if retired:
            logger.warning(
                "[site] %s no longer supported and ignored — the agent-writable "
                "static web root was removed as an ungated exfiltration channel. "
                "Remove the key(s); if a web server still serves that directory, "
                "take it down separately.",
                ", ".join(retired),
            )

    if "security" in data:
        sec = data["security"]
        # Two things the walk cannot say. The first is a key that was removed
        # rather than renamed: the framework database is bound into no sandbox
        # for anyone now, so there is no bind left to widen and a file still
        # carrying this needs telling rather than a generic "unrecognised".
        if "sandbox_admin_db_write" in sec:
            logger.warning(
                "[security] sandbox_admin_db_write is no longer supported and is "
                "being ignored. The framework database is not bound into the "
                "sandbox at all any more (for admins or anyone else); writes go "
                "through skill CLIs and deferred ops. Remove the key."
            )
        # The second is a pair of settings that are each individually valid and
        # wrong together. The credential half leads, because it is the half an
        # operator will not otherwise find out about: `_split_credential_env`
        # removes the secret variables only under the proxy branch in
        # `execute_task`, so with the proxy off they stay in the environment
        # handed to the model — and with the sandbox on there is a real boundary
        # for them to sit inside, which is the opposite of what switching a
        # sandbox on is for. The masked-database half stays because it is what
        # an operator whose skill CLIs have stopped working needs to read; it
        # fails loudly on its own, which is why it does not lead.
        #
        # Both switches off together is a different shape and is deliberately
        # not warned about: `setup_wizard` writes that pair for the single-user
        # install, the task then runs unconfined as the daemon user, and there is
        # no boundary for an environment variable to cross. See ISSUE-393.
        #
        # "inside the sandbox" is the *requested* flag, not the effective one:
        # `effective_sandboxing` reads false where the bwrap probe fails, which
        # is the shipped Docker stack, and it may not be consulted here because
        # it spawns and this path runs on every CLI invocation. The credential
        # half of the message is true on that shape too — the variables are in
        # the task environment either way — and an operator who asked for a
        # sandbox is the right person to tell. `doctor` is where the difference
        # between asked-for and in-force gets reported: the
        # `security.sandbox_credentials` check says the same thing with the
        # bwrap probe's answer attached (ISSUE-396). It is deliberately not in
        # `CONFIG_LOAD_CHECKS`, so this warning stays the only one on this path
        # rather than being logged a second time by `_validate_forge_clis`.
        if config.security.sandbox_enabled and not config.security.skill_proxy_enabled:
            logger.warning(
                "[security] sandbox_enabled with skill_proxy_enabled = false: "
                "every configured service credential stays in the task "
                "environment, readable by the model from inside the sandbox "
                "rather than injected per call; and skill CLIs will run inside "
                "the sandbox, where the databases they read are masked out. "
                "Enable the skill proxy, or disable the sandbox for a trusted "
                "single-user install."
            )

    # Read off the loaded config rather than off `data`, so it fires whichever
    # of the two keys the file names and whichever it inherits.
    _warn_ro_paths_over_control_tree(config)

    config.admin_users = load_admin_users()

    # Environment variable overrides for secrets (allows EnvironmentFile= usage).
    # Naming convention: ISTOTA_<SECTION>_<FIELD>, matching the config dataclass
    # path. Same convention as docker-compose env vars — single source of truth
    # for "where does this credential come from" across all deploy paths.
    _env_secret_overrides = [
        ("ISTOTA_NEXTCLOUD_APP_PASSWORD", "nextcloud", "app_password"),
        # `[caldav]` is the section written for the shape with no Nextcloud, so
        # this is that shape's calendar credential — and it was the only one in
        # the tree reachable by no route but the config file. The standalone
        # wizard had to write it into `config.toml` under a generated header
        # whose own second line says secrets live in the sibling `istota.env`
        # and never here. Redaction needs no companion entry: `password` is
        # caught by `admin_config_view.SECRET_NAME_PATTERNS` on its name.
        ("ISTOTA_CALDAV_PASSWORD", "caldav", "password"),
        ("ISTOTA_EMAIL_IMAP_PASSWORD", "email", "imap_password"),
        ("ISTOTA_EMAIL_SMTP_PASSWORD", "email", "smtp_password"),
        ("ISTOTA_DEVELOPER_GITLAB_TOKEN", "developer", "gitlab_token"),
        ("ISTOTA_DEVELOPER_GITHUB_TOKEN", "developer", "github_token"),
        ("ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET", "google_workspace", "client_secret"),
        ("ISTOTA_WEB_OAUTH2_CLIENT_SECRET", "web", "oauth2_client_secret"),
        ("ISTOTA_WEB_SESSION_SECRET_KEY", "web", "session_secret_key"),
    ]
    for env_var, section, field_name in _env_secret_overrides:
        val = os.environ.get(env_var)
        if val:
            setattr(getattr(config, section), field_name, val)

    # Docker-path override for the web token-storage mode (not a secret, but it
    # rides the same env channel as the other web knobs so the compose file can
    # default the demo stack to encrypted without templating TOML).
    _token_storage_env = os.environ.get("ISTOTA_WEB_TOKEN_STORAGE", "").strip()
    if _token_storage_env:
        if _token_storage_env in ("ephemeral", "encrypted"):
            config.web.token_storage = _token_storage_env
        else:
            logger.warning(
                "ISTOTA_WEB_TOKEN_STORAGE=%r is not a known value; ignoring",
                _token_storage_env,
            )

    # Web auth-mode override (local single-user installs set ISTOTA_WEB_AUTH=none
    # instead of templating TOML). Same validation as the TOML parse.
    _web_auth_env = os.environ.get("ISTOTA_WEB_AUTH", "").strip()
    if _web_auth_env:
        if _web_auth_env in ("nextcloud", "none"):
            config.web.auth = _web_auth_env
        else:
            logger.warning(
                "ISTOTA_WEB_AUTH=%r is not a known value (expected 'nextcloud' "
                "or 'none'); ignoring",
                _web_auth_env,
            )

    # Native-brain API key lives two levels deep (brain.native.api_key), so it
    # doesn't fit the flat section/field table above.
    _native_key = os.environ.get("ISTOTA_BRAIN_NATIVE_API_KEY")
    if _native_key:
        config.brain.native.api_key = _native_key

    # Phase 6: overlay profile fields from the user_profiles table.
    # DB rows replace the matching scalar fields on TOML-loaded UserConfig
    # entries; briefings stay TOML-owned. Users that exist only
    # in the DB (no TOML entry) get a synthesised UserConfig.
    _apply_user_profiles(config)

    # Phase 7a: overlay user_resources rows onto config.users[*].resources.
    # DB rows win over TOML for matching (type, path); distinct (type, path)
    # pairs coexist. Existing call sites (executor merge, webhook_receiver,
    # money/feeds loaders, secrets_store import) keep reading
    # ``config.users[uid].resources`` unchanged.
    _apply_user_resources(config)

    # Modules refactor: absorb credentials from `[[resources]]` blocks for
    # types that have been retired (karakeep base_url, overland.ingest_token,
    # etc.) into the secrets table, then drop those rows from user_resources
    # and from the in-memory ``uc.resources`` lists so the rest of the load
    # cycle sees the post-cleanup state.
    _migrate_obsolete_resources(config)

    # Phase 7b: overlay briefing_configs rows onto config.users[*].briefings.
    # DB rows replace TOML rows of the same ``name``; distinct names coexist.
    # ``check_briefings`` and ``get_briefings_for_user`` keep reading
    # ``user_config.briefings`` unchanged.
    _apply_user_briefings(config)

    # admin-shared-briefing-blocks: overlay shared_block_configs rows onto
    # config.briefing_shared_blocks. DB wins by name (admin edits survive
    # operator re-runs); ``check_shared_blocks`` then reads DB-authoritative
    # definitions.
    _apply_shared_blocks(config)

    # Apply operator role-alias overrides globally so every downstream call
    # to ``brain.resolve_model_name`` / ``brain.resolve_alias`` picks up the
    # operator's mapping. Done last so it sees any TOML edits.
    #
    # Per-entry semantic validation is delegated to the active brain (it
    # knows its own provider alias namespace) so operators see typos
    # surfaced at startup rather than at task time.
    from .brain import make_brain, set_alias_overrides
    from .brain._roles import LEGACY_NAMESPACE, PORTABLE_KEY
    if config.models.aliases:
        _logger = logging.getLogger("istota.config")
        _active_brain = make_brain(config.brain)
        from .brain.claude_code import ClaudeCodeBrain
        _cli_brain = ClaudeCodeBrain()  # the "anthropic"-namespace validator

        def _validator_for(namespace):
            # Pick the brain whose alias table validates this namespace. The
            # legacy flat ("*") is resolved by whichever brain runs the task, so
            # validate it against the active brain (preserves the pre-per-namespace
            # shadow/typo warnings). "anthropic" always validates against the CLI
            # brain even when the active brain is native. Any namespace with no
            # constructible alias-table brain (openai_compat when native isn't the
            # active brain) is skipped — native has no alias table anyway.
            if namespace == LEGACY_NAMESPACE:
                return _active_brain
            if namespace == "anthropic":
                return _cli_brain
            if namespace == _active_brain.model_namespace:
                return _active_brain
            return None

        def _validate_target(name, namespace, model_str):
            if not isinstance(model_str, str) or not model_str.strip():
                return
            brain = _validator_for(namespace)
            if brain is None:
                return
            for _msg in brain.validate_alias_override(name, model_str):
                if namespace == LEGACY_NAMESPACE:
                    _logger.warning("[models.aliases] %s", _msg)
                else:
                    _logger.warning("[models.aliases] (%s) %s", namespace, _msg)

        for _name, _value in config.models.aliases.items():
            if isinstance(_value, str):
                _validate_target(_name, LEGACY_NAMESPACE, _value)
            elif isinstance(_value, dict):
                for _ns, _target in _value.items():
                    # ``portable = true`` is a reserved flag, not a namespace —
                    # skip it in the validation loop (set_alias_overrides records it).
                    if str(_ns).lower() == PORTABLE_KEY:
                        continue
                    if isinstance(_target, str):
                        _validate_target(_name, str(_ns), _target)
                    elif isinstance(_target, dict):
                        _validate_target(_name, str(_ns), _target.get("model"))
    set_alias_overrides(config.models.aliases)

    # Per-model capability/window overrides for the native brain (NB-4). Global
    # like the role overrides — every get_model_info consumer (compaction sizing,
    # capability gates, usage pricing) picks them up.
    try:
        from .llm.catalog import set_model_overrides
        set_model_overrides(config.brain.native.model_overrides)
    except Exception:  # pragma: no cover - defensive; never fail config load
        logging.getLogger("istota.config").warning(
            "failed to apply [brain.native.model_overrides]", exc_info=True
        )

    _validate_brain_fallback(config)
    _validate_room_selectable(config)
    _validate_claude_code_brain(config)
    _validate_advisor_model(config)
    _validate_forge_clis(config)

    return config


CONFIG_LOAD_CHECKS = (
    "developer.forge_binaries",
    "developer.forge_policy",
    "security.skill_proxy",
)
"""The doctor checks that run inside every ``load_config``.

Named here rather than written inline in :func:`_validate_forge_clis` because
this tuple *is* the config-load hot path, and the property that it stays cheap
needs something to point at. ``load_config`` runs in the daemon, the web app,
the webhook receiver, every CLI invocation, and every host-side skill CLI
subprocess the skill proxy spawns *per call*, so a heavy import reached from
one of these checks is paid on all of them.
``tests/test_doctor.py::TestConfigLoadPathStaysCheap`` asserts against this
tuple in a fresh interpreter.

Deliberately not ``("developer.", "security.")`` or any other prefix, and the
six ``developer.*`` checks it leaves out are left out for two different reasons.
``repos_layout`` reaches ``istota.executor`` — and through it the whole skill
package — for a path derivation, and ``container`` opens a socket per user;
both are correct as they stand, because they run from the ``doctor`` CLI, the
daemon's boot run and the hourly sweep, none of which is a hot path and all of
which have ``executor`` loaded anyway. The remaining four are excluded for the
repetition reason :func:`_validate_forge_clis` gives below: this path runs per
skill-CLI call, so a warning from it repeats for as long as the condition holds.
"""


def _validate_forge_clis(config: "Config") -> None:
    """Warn about a forge CLI setup that will only fail later, and worse.

    Reduced to a call into :mod:`istota.doctor` so there is one implementation
    of each of these facts. The checks themselves, their gating and their
    wording all live there; this is the config-load-time delivery of them.

    ``probe=False`` is load-bearing and not a tuning knob. This runs inside
    every ``load_config`` — the daemon, the web app, the webhook receiver, every
    CLI invocation, and every host-side skill CLI the skill proxy spawns *per
    call*. The work it replaced was ``os.path.exists``, which is free; five
    ``--version`` spawns per skill-CLI invocation would not be.

    The selection is today's warning set **minus the stale-path case**, and
    deliberately no more. The registry has grown four checks this path does not
    deliver (config drift, wrapper shadowing, version skew, proxy
    resolvability), because this runs once per process for the daemon but once
    per *call* for a skill CLI: a warning from here repeats for as long as the
    condition holds, while the same warning from the boot run and the hourly
    sweep is said once and alerted on.

    The narrowing that is a real behaviour change, stated plainly: the old code
    warned whenever the *configured* path did not exist, checking it directly.
    ``check_forge_binaries`` instead asks ``_resolve_real_bin`` what will
    actually be exec'd, so a deployment whose ``config.toml`` names a stale path
    while resolution successfully falls back — the ``30bb7c83`` shape — no
    longer warns here. It has not stopped being reported: that is precisely what
    ``developer.forge_config_drift`` says, and drift is a post-boot condition by
    nature (the auto-update cron changes what is installed under a config the
    daemon already loaded), so the interval sweep is where it belongs.

    The skill-proxy posture warning and the new resolvability check share one
    registry entry, so that pair is split by result name rather than by prefix:
    ``only=`` filters on registry names, and a sub-result's name is not one.
    """
    from .doctor import FAIL, WARN, run_checks

    _logger = logging.getLogger("istota.config")
    try:
        results = run_checks(config, only=CONFIG_LOAD_CHECKS, probe=False)
    except Exception:  # pragma: no cover - never fail config load over a warning
        _logger.warning("forge CLI validation failed", exc_info=True)
        return

    for result in results:
        if result.name == "security.skill_proxy":
            continue  # resolvability: boot path and interval sweep only, see above
        if result.status in (WARN, FAIL):
            # No literal namespace prefix: `result.name` already carries one,
            # and not every result reaching here is a `developer.*` one — the
            # skill-proxy posture warning is a `security.*` check.
            _logger.warning(
                "%s: %s%s",
                result.name,
                result.detail,
                f" — {result.remedy}" if result.remedy else "",
            )


# Anthropic-namespace brain kinds — the only ones the advisor tool can ever
# reach (it's an Anthropic Messages beta tool with no wire over openai_compat).
_ANTHROPIC_BRAIN_KINDS = frozenset({"claude_code", "tmux_claude"})


def _validate_advisor_model(config: "Config") -> None:
    """Warn-and-ignore checks for ``advisor_model`` — never fails load, matching
    ``[models]``. Three independent traps, each a WARNING only:

    1. A ``:effort`` modifier — the CLI's ``--advisor`` flag takes no effort,
       so it's silently dropped at resolution time (``resolve_model_name``).
    2. Set under a non-anthropic ``brain.kind`` (``native``) — the advisor tool
       can never run for this task. The executor only ever resolves `advisor`
       for the *primary* brain when its namespace is anthropic; a configured
       fallback brain doesn't change that (`_run_fallback` only ever *drops* an
       inherited advisor crossing into native, it never adds one crossing back
       out), so this warns regardless of `fallback`.
    3. A value that resolves to no concrete model (the ``"default"`` alias:
       ``DEFAULT_ALIASES["default"] = (None, None)``) — the CLI hard-errors on
       ``--advisor default`` rather than degrading, so this is worth flagging
       before the first task hits it.
    """
    raw = (config.advisor_model or "").strip()
    if not raw:
        return
    _logger = logging.getLogger("istota.config")

    from .brain._aliases import split_effort

    base, suffix_effort = split_effort(raw)
    if suffix_effort:
        _logger.warning(
            "advisor_model=%r carries a :%s effort modifier; the --advisor CLI "
            "flag takes no effort, so it is dropped at resolution time",
            raw, suffix_effort,
        )

    if config.brain.kind not in _ANTHROPIC_BRAIN_KINDS:
        _logger.warning(
            "advisor_model=%r is set but brain.kind=%r is not an anthropic-"
            "namespace brain; the advisor tool is Anthropic-only and will "
            "never run for this task",
            raw, config.brain.kind,
        )

    from .brain.claude_code import ClaudeCodeBrain

    pair = ClaudeCodeBrain().resolve_alias(base)
    if pair is not None and pair[0] is None:
        _logger.warning(
            "advisor_model=%r resolves to no concrete model (e.g. the "
            '"default" alias); the CLI will reject it as an --advisor value',
            raw,
        )


# Said once per process, not once per `load_config` — see the notice at the end
# of `_validate_brain_fallback` for why.
_TMUX_NO_FALLBACK_NOTICE_SAID = False


def _fallback_scoped_kinds(config: "Config") -> set[str]:
    """The brain kinds ``[brain] fallback`` still governs.

    ``kind`` plus the ``source_type_overrides`` targets, minus any target
    ``resolve_brain_kind`` would log and ignore — an override naming a kind that
    does not exist runs on ``kind``, so it must not count as a second brain.

    This is **not** every kind a task can run under, which is
    ``brain.reachable_brain_kinds``: a room may pin any kind in
    ``[brain] room_selectable``, and those are deliberately absent here. An
    admitted room override clears ``fallback``, so a pinned room has no failover
    — counting those kinds would keep a fallback alive that no task could use,
    and would fire the tmux notice below recommending one for a shape that is
    meant to have none. Widen this and both guards start answering a question
    neither caller asked.
    """
    from .brain import KNOWN_BRAIN_KINDS

    overrides = (config.brain.source_type_overrides or {}).values()
    return {config.brain.kind} | {v for v in overrides if v in KNOWN_BRAIN_KINDS}


def _validate_brain_fallback(config: "Config") -> None:
    """Neutralize a misconfigured ``[brain] fallback`` so it can't wedge tasks.

    Two guards, each logs one WARNING and blanks ``fallback``, which since
    ISSUE-362 means what it says — no failover, whatever the primary kind:
    1. Unknown kind — ``fallback`` not in ``KNOWN_BRAIN_KINDS``.
    2. Self-fallback — the configured fallback is the *only* kind the deployment
       runs, so no task could benefit. Deliberately not the bare
       ``fallback == kind`` comparison it used to be: a ``source_type_overrides``
       entry routing to another kind makes the same value a real target for those
       tasks, and that combination is the only way to spell "route scheduled work
       to tmux and fail it over to the CLI". The per-task half of the rule lives
       in ``brain._fallback.effective_fallback_kind``, which sees the resolved
       config and returns None where the fallback equals its kind.

    Then one INFO line, once per process, where ``tmux_claude`` runs with no
    fallback. That combination was unconfigurable before ISSUE-362 (it always
    resolved to ``claude_code``), so an operator who upgrades past it and keeps
    an empty field loses failover; the notice is informational, not a
    correction, since "no failover" is now a legitimate thing to want.

    Two things about it are deliberate. It covers a ``source_type_overrides``
    entry routing to tmux as well as a tmux primary, because
    ``resolve_brain_kind`` returns a ``replace(brain_config, kind=target)`` that
    inherits ``fallback`` — so a ``claude_code`` primary routing ``scheduled``
    to tmux is the same lost failover with nothing in ``kind`` to see it by, and
    the Ansible default that writes the target back in covers that shape too.
    And it fires once per process rather than per call: ``load_config`` runs in
    every CLI invocation and in every host-side skill CLI the proxy spawns, so a
    per-call line would be one per skill call rather than one per start-up.
    """
    from .brain import KNOWN_BRAIN_KINDS

    _logger = logging.getLogger("istota.config")
    fb = (config.brain.fallback or "").strip()
    if fb and fb not in KNOWN_BRAIN_KINDS:
        _logger.warning(
            "[brain] fallback=%r is not a known brain kind %s; disabling fallback",
            fb, sorted(KNOWN_BRAIN_KINDS),
        )
        config.brain.fallback = ""
    elif fb and _fallback_scoped_kinds(config) == {fb}:
        # Only where *nothing* the deployment can run would benefit. A
        # `source_type_overrides` entry routing to another kind makes this a real
        # target for those tasks even though it equals `kind`, and blanking it
        # here would take that away with no way to express it — the resolved
        # config is the only thing that can tell the two apart, which is why
        # `effective_fallback_kind` carries the per-task half of this rule.
        _logger.warning(
            "[brain] fallback=%r is the only brain kind this deployment runs; a "
            "self-fallback can't help — disabling it",
            fb,
        )
        config.brain.fallback = ""
    global _TMUX_NO_FALLBACK_NOTICE_SAID
    if (
        "tmux_claude" in _fallback_scoped_kinds(config)
        and not (config.brain.fallback or "").strip()
        and not _TMUX_NO_FALLBACK_NOTICE_SAID
    ):
        _TMUX_NO_FALLBACK_NOTICE_SAID = True
        _logger.info(
            'tmux_claude runs with no [brain] fallback configured: a tmux launch '
            "failure or usage limit fails the task rather than rerouting. Set "
            'fallback = "claude_code" to keep the behaviour it had before '
            "ISSUE-362.",
        )


def _validate_room_selectable(config: "Config") -> None:
    """Warn about a ``[brain] room_selectable`` entry no brain answers to.

    The entry is left on the field rather than corrected, unlike
    ``_validate_brain_fallback``'s two guards: ``room_selectable_kinds`` filters
    it out at every read anyway, so the value can stay visible in the admin
    config view as what the operator wrote while granting nothing.

    A warning rather than nothing, because this list is a gate and a name that
    matches no kind is the "typo that did nothing" shape — it is offered to no
    user and refused by nothing, so without a line here the operator sees a
    feature that does not work and no reason why. ``resolve_brain_kind``'s own
    refusal cannot cover it: that fires when a room *pins* a kind, and a name
    the picker never offered is a name no room can pin.

    Once per load, like the unknown-``fallback`` warning beside it, and only
    where an entry is actually wrong.
    """
    from .brain import KNOWN_BRAIN_KINDS

    unknown = [
        name for name in (config.brain.room_selectable or [])
        if name not in KNOWN_BRAIN_KINDS
    ]
    if unknown:
        logging.getLogger("istota.config").warning(
            "[brain] room_selectable names %s, which %s not a known brain kind "
            "%s; no room will be offered %s",
            ", ".join(repr(name) for name in unknown),
            "are" if len(unknown) > 1 else "is",
            sorted(KNOWN_BRAIN_KINDS),
            "them" if len(unknown) > 1 else "it",
        )


def _validate_claude_code_brain(config: "Config") -> None:
    """Correct a ``[brain.claude_code]`` block that would misreport the plan.

    Three rules, each logging one WARNING and correcting in place. None of them
    can refuse to load, and none of them does any I/O — this runs inside every
    ``load_config``, and the subscription poll itself is deliberately reached
    only from a diagnostic path.

    1. Both percentages clamp to ``[0, 100]``. A threshold outside the range the
       endpoint reports is either unreachable or always tripped.
    2. ``warn > high`` (after clamping) is corrected to ``warn = high``. An
       inverted pair makes the amber band unreachable, which is more likely a
       typo than an intent.
    3. ``cache_ttl_seconds`` and ``timeout_seconds`` floor at 1. A zero TTL
       would fetch on every dashboard poll — the cache exists precisely so the
       whole deployment pays for one fetch per window.

    A non-finite value is substituted with the shipping default rather than
    clamped or floored, on both the percentage and the timeout paths. Clamping
    a NaN percentage would land it at 0.0, i.e. WARN at every utilization for
    ever, on a check whose whole point is that it does not cry wolf; and an
    ``inf`` timeout is both an unbounded socket read and a value the admin
    config pane cannot serialize. The loader's own parse already rejects these
    at the point they enter, so this is the guard for a ``Config`` assembled
    some other way.

    ``stale_after_seconds`` is deliberately not floored: zero there means "treat
    any stale reading as too old", which is a coherent thing to ask for.
    """
    cc = config.brain.claude_code
    _logger = logging.getLogger("istota.config")

    def _clamp_percent(name: str, raw: float, default: float) -> float:
        # A non-finite value is not "out of range", it is "not a number", and
        # clamping it would land it at 0.0 — which means WARN at every
        # utilization, forever, on a check whose whole point is that it is not
        # alarming. Substitute the shipping default instead. (NaN compares
        # False against everything, so min/max would silently keep the 0.0.)
        if not math.isfinite(raw):
            _logger.warning(
                "[brain.claude_code] %s=%r is not a finite number; using %r",
                name, raw, default,
            )
            return default
        clamped = min(100.0, max(0.0, float(raw)))
        if clamped != raw:
            _logger.warning(
                "[brain.claude_code] %s=%r is outside [0, 100]; using %r",
                name, raw, clamped,
            )
        return clamped

    _defaults = ClaudeCodeBrainConfig()
    cc.subscription_usage_warn_percent = _clamp_percent(
        "subscription_usage_warn_percent",
        cc.subscription_usage_warn_percent,
        _defaults.subscription_usage_warn_percent,
    )
    cc.subscription_usage_high_percent = _clamp_percent(
        "subscription_usage_high_percent",
        cc.subscription_usage_high_percent,
        _defaults.subscription_usage_high_percent,
    )

    if cc.subscription_usage_warn_percent > cc.subscription_usage_high_percent:
        _logger.warning(
            "[brain.claude_code] subscription_usage_warn_percent=%r is above "
            "subscription_usage_high_percent=%r, which leaves no amber band; "
            "lowering warn to %r",
            cc.subscription_usage_warn_percent,
            cc.subscription_usage_high_percent,
            cc.subscription_usage_high_percent,
        )
        cc.subscription_usage_warn_percent = cc.subscription_usage_high_percent

    # `not (x >= 1)` rather than `x < 1`, so a NaN that reached the dataclass
    # some way other than the loader's own parse is corrected instead of
    # sailing through — every comparison against NaN is False, which is exactly
    # how a floor written the obvious way lets one past.
    if not (cc.subscription_usage_cache_ttl_seconds >= 1):
        _logger.warning(
            "[brain.claude_code] subscription_usage_cache_ttl_seconds=%r would "
            "fetch on every read; using 1",
            cc.subscription_usage_cache_ttl_seconds,
        )
        cc.subscription_usage_cache_ttl_seconds = 1

    if not (cc.subscription_usage_timeout_seconds >= 1):
        _logger.warning(
            "[brain.claude_code] subscription_usage_timeout_seconds=%r is below "
            "one second; using 1",
            cc.subscription_usage_timeout_seconds,
        )
        cc.subscription_usage_timeout_seconds = 1.0
    elif not math.isfinite(cc.subscription_usage_timeout_seconds):
        # An unbounded socket timeout on a diagnostic path, and a value the
        # admin config pane cannot serialize — starlette renders JSON with
        # allow_nan=False, so one `inf` here 500s GET /api/admin/config for the
        # whole instance.
        _logger.warning(
            "[brain.claude_code] subscription_usage_timeout_seconds=%r is not "
            "finite; using %r",
            cc.subscription_usage_timeout_seconds,
            _defaults.subscription_usage_timeout_seconds,
        )
        cc.subscription_usage_timeout_seconds = _defaults.subscription_usage_timeout_seconds


def _apply_user_profiles(config: "Config") -> None:
    """Merge ``user_profiles`` rows into ``config.users``.

    Best-effort: a missing/unreadable DB does not fail config loading
    (callers like ``istota init`` run before the DB exists). The DB wins for
    profile-shaped fields; TOML keeps resources and briefings.
    """
    try:
        from . import user_profiles as _up  # avoid import cycles at module load
    except Exception:  # pragma: no cover - defensive
        return

    db_path = config.db_path
    if db_path is None or not Path(db_path).exists():
        return

    try:
        rows = _up.list_profiles(Path(db_path))
    except Exception as e:  # pragma: no cover - defensive
        # WARNING, not DEBUG: the overlay now carries security-relevant fields.
        # A user who tightened `outbound_approval` past the operator floor loses
        # that tightening when this is skipped — it never falls *below* the
        # floor, so the operator's contract holds, but the user's own choice is
        # silently undone and at DEBUG nothing records it.
        logger.warning(
            "user_profiles overlay skipped (%s); per-user settings fall back to "
            "TOML values and operator defaults for this load", e,
        )
        return

    for user_id, profile in rows.items():
        existing = config.users.get(user_id)
        if existing is None:
            existing = UserConfig(display_name=profile.display_name or user_id)
            config.users[user_id] = existing
        _up.merge_into_user_config(profile, existing)


def _apply_user_resources(config: "Config") -> None:
    """Merge ``user_resources`` rows into ``config.users[*].resources``.

    DB rows are appended as ``ResourceConfig`` entries so every existing call
    site that walks ``user_config.resources`` (executor merge,
    webhook_receiver, money/feeds loaders, secrets_store import) sees
    DB-managed resources transparently. Dedup key is ``(type, path)`` — DB
    wins, matching the user_profiles precedence rule.

    Best-effort: a missing DB does not fail config loading.
    """
    try:
        from . import db as _db
    except Exception:  # pragma: no cover - defensive
        return

    db_path = config.db_path
    if db_path is None or not Path(db_path).exists():
        return

    user_ids: set[str] = set(config.users.keys())
    try:
        with _db.get_db(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM user_resources"
            ).fetchall()
            user_ids.update(r["user_id"] for r in rows)

            for user_id in user_ids:
                db_resources = _db.get_user_resources(conn, user_id)
                if not db_resources:
                    continue
                user_config = config.users.get(user_id)
                if user_config is None:
                    user_config = UserConfig(display_name=user_id)
                    config.users[user_id] = user_config

                # Drop TOML rows that the DB also owns (same type+path).
                db_keys = {(r.resource_type, r.resource_path) for r in db_resources}
                user_config.resources = [
                    rc for rc in user_config.resources
                    if (rc.type, rc.path) not in db_keys
                ]

                # Append DB rows as ResourceConfig entries. Pull credentials
                # the loader normally splits out (base_url, api_key) into
                # the dataclass's flat fields so secrets_store._IMPORT_MAP
                # and Karakeep's loader keep working unchanged.
                # _allow_obsolete: a stale obsolete-type row may still exist
                # on first startup after the modules refactor; the next
                # _migrate_obsolete_resources pass absorbs and deletes it.
                for r in db_resources:
                    rc = ResourceConfig(
                        type=r.resource_type,
                        path=r.resource_path,
                        name=r.display_name or "",
                        permissions=r.permissions or "read",
                        extra=dict(r.extras or {}),
                        _allow_obsolete=True,
                    )
                    rc.from_db = True
                    user_config.resources.append(rc)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("user_resources overlay skipped: %s", e)


def _migrate_obsolete_resources(config: "Config") -> None:
    """Absorb obsolete resource credentials into secrets, then drop the rows.

    Sequence:

    1. ``secrets_store.import_from_user_configs`` — copies credentials out of
       ``[[resources]]`` extras for the retired types (karakeep base_url,
       overland.ingest_token, monarch session_token, etc.) into the
       encrypted secrets table. Idempotent; rows already in the table are
       not overwritten.
    2. ``db.cleanup_obsolete_resources`` — deletes the matching rows from
       the ``user_resources`` DB table so they stop being merged into
       ``uc.resources`` on future loads.
    3. Filter ``uc.resources`` in memory so the rest of this load cycle
       sees the post-cleanup state (the executor merge, scheduler hooks,
       etc. all read this list).

    Best-effort: a missing/unreadable DB or unset ``ISTOTA_SECRET_KEY`` is
    not fatal — startup continues and the operator sees the warning.
    """
    try:
        from . import db as _db  # noqa: PLC0415
        from . import secrets_store as _ss  # noqa: PLC0415
    except Exception:  # pragma: no cover - defensive
        return

    db_path = config.db_path
    if db_path is None or not Path(db_path).exists():
        return

    try:
        _ss.import_from_user_configs(db_path, config.users)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("secrets import failed: %s", e)

    try:
        removed = _db.cleanup_obsolete_resources(db_path)
        if removed:
            logger.info(
                "dropped %d obsolete resource row(s) (types: %s)",
                removed, ", ".join(_db._OBSOLETE_RESOURCE_TYPES),
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("obsolete resource cleanup failed: %s", e)

    # Filter uc.resources in memory so the rest of this load cycle sees the
    # post-cleanup state (the executor merge, scheduler hooks, etc. all read
    # this list).
    obsolete = set(_db._OBSOLETE_RESOURCE_TYPES)
    for uc in config.users.values():
        uc.resources = [rc for rc in uc.resources if rc.type not in obsolete]


def _merge_default_briefings(config: "Config") -> None:
    """Seed the shared ``[[default_briefings]]`` set into each opted-in user.

    For each user whose ``default_briefings`` flag is true, append every
    default briefing whose ``name`` the user does not already define (an
    explicit user briefing wins). Seed-once + edit-preservation fall out of
    the downstream machinery for free: ``import_from_user_configs`` never
    overwrites an existing ``briefing_configs`` row and the block seeder is
    one-time, so after the first seed the user's edits survive an Ansible
    re-run. Runs before the DB overlay so a seeded default's config-authored
    blocks are captured and ride onto its DB-sourced entry.
    """
    if not config.default_briefings:
        return
    for user_config in config.users.values():
        if not getattr(user_config, "default_briefings", True):
            continue
        existing = {b.name for b in user_config.briefings}
        for default in config.default_briefings:
            if default.name and default.name not in existing:
                # Copy so a per-user edit can't mutate the shared template.
                user_config.briefings.append(_dc_replace(default))
                existing.add(default.name)


def _apply_user_briefings(config: "Config") -> None:
    """Merge ``briefing_configs`` rows into ``config.users[*].briefings``.

    DB rows replace TOML rows of the same ``name``; distinct names coexist.
    Disabled DB rows (enabled=0) drop the matching TOML name without adding
    a replacement, so an operator can switch a TOML-templated briefing off
    via the web UI without re-templating.

    Also seeds the shared ``[[default_briefings]]`` set into opted-in users
    (by name, user wins) before the DB overlay.

    Best-effort: a missing DB does not fail config loading.
    """
    _merge_default_briefings(config)

    try:
        from . import user_briefings as _ub  # avoid import cycles at module load
    except Exception:  # pragma: no cover - defensive
        return

    db_path = config.db_path
    if db_path is None or not Path(db_path).exists():
        return

    try:
        rows = _ub.list_briefings(Path(db_path))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("user_briefings overlay skipped: %s", e)
        return

    by_user: dict[str, list] = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row)

    for user_id, db_rows in by_user.items():
        user_config = config.users.get(user_id)
        if user_config is None:
            user_config = UserConfig(display_name=user_id)
            config.users[user_id] = user_config

        db_names = {r.name for r in db_rows}
        # Config-authored blocks live only in TOML (never in the DB row). Capture
        # them before the drop so they can ride on the surviving DB-sourced entry;
        # otherwise the module-DB seeder would never see them.
        blocks_by_name = {
            b.name: b.blocks for b in user_config.briefings if b.blocks
        }
        # Drop TOML briefings whose names are claimed by DB rows.
        user_config.briefings = [
            b for b in user_config.briefings if b.name not in db_names
        ]
        # Append enabled DB rows as BriefingConfig entries.
        for r in db_rows:
            if not r.enabled:
                continue
            bc = BriefingConfig(
                name=r.name,
                cron=r.cron,
                title=r.title,
                conversation_token=r.conversation_token,
                output=r.output,
                components=dict(r.components),
                blocks=blocks_by_name.get(r.name, []),
            )
            bc.from_db = True
            user_config.briefings.append(bc)


def _apply_shared_blocks(config: "Config") -> None:
    """Overlay ``shared_block_configs`` rows onto ``config.briefing_shared_blocks``.

    DB wins by ``name`` (an admin's web edit survives operator re-runs); a DB row
    replaces a config/TOML block of the same name, and DB-only rows are appended.
    A disabled DB row still overlays (present-but-muted) — ``check_shared_blocks``
    already skips a block that isn't ``enabled``.

    Best-effort: a missing/unreadable DB leaves the config-only blocks in place,
    so the server still runs on TOML/DEFAULT definitions.
    """
    db_path = config.db_path
    if db_path is None or not Path(db_path).exists():
        return

    try:
        from . import db as _db
        with _db.get_db(Path(db_path)) as conn:
            rows = _db.list_shared_block_configs(conn)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("shared_block_configs overlay skipped: %s", e)
        return

    if not rows:
        return

    def _from_row(r) -> BriefingSharedBlock:
        return BriefingSharedBlock(
            name=r.name,
            cron=r.cron,
            title=r.title or "",
            directive=r.directive if r.directive else None,
            render_mode=r.render_mode or "synthesis",
            enabled=bool(r.enabled),
            trusted=bool(r.trusted),
            sources=list(r.sources or []),
        )

    db_by_name = {r.name: r for r in rows}
    merged: list[BriefingSharedBlock] = []
    seen: set[str] = set()
    for block in config.briefing_shared_blocks:
        if block.name in db_by_name:
            merged.append(_from_row(db_by_name[block.name]))
        else:
            merged.append(block)
        seen.add(block.name)
    for r in rows:
        if r.name not in seen:
            merged.append(_from_row(r))
    config.briefing_shared_blocks = merged
