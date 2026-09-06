"""File-based work entry storage using yearly TOML files.

Entries are stored in {data_dir}/invoices/work/{year}.toml files, sorted by date.
Display indices (1-based) are assigned across all loaded entries, sorted by date.

Two kinds of identity live here, and they are not interchangeable:

* ``entry.id`` — the 1-based display index, recomputed on every load. It is a
  presentation detail (the CLI's ``#N`` UX) and shifts whenever anything is
  inserted before an entry.
* ``entry.uid`` — a stable id stamped by every writer, mirroring the ``id:``
  metadata beancount transactions carry (see ``core/edit.py``). Programmatic
  callers that resolve an entry at one moment and mutate it at another — the
  web UI above all — must address it by ``uid`` via
  :func:`update_work_entry_by_uid` / :func:`remove_work_entry_by_uid`, which
  resolve *inside* the write lock. Reading never stamps a uid;
  :func:`backfill_work_ids` does, and runs from ``ensure_initialised``.

Round-trip caveat: these files are deliberately hand-editable, but a write
rewrites the whole year file from the serializer's output. Unrecognised keys
survive (they're captured into ``WorkEntry.extra``); **comments do not**, and
neither do nested tables. Preserving comments would need a comment-aware TOML
library, which this module doesn't carry.
"""

from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import tomli

from istota.atomic_write import write_text_atomic
from istota.file_lock import exclusive_lock
from istota.money.core.ids import new_txn_id
from istota.money.core.models import WorkEntry

logger = logging.getLogger(__name__)

# Fields the loader understands. Anything else in a year file is captured into
# WorkEntry.extra and written back verbatim.
_KNOWN_ENTRY_KEYS = frozenset({
    "uid", "date", "client", "service", "qty", "amount",
    "discount", "description", "entity", "invoice", "invoice_date", "paid_date",
})

# Fields a caller may never set through the generic update path.
_PROTECTED_FIELDS = frozenset({"uid", "id", "extra"})


class WorkStoreLocked(RuntimeError):
    """Raised when the work-entry write lock can't be acquired in time."""


class WorkFileQuarantined(RuntimeError):
    """Raised when a write would drop a row the loader couldn't read.

    A write rewrites the whole year file from the loaded list, so a row that
    was skipped on read would be silently deleted. Reads degrade (the other
    entries load); writes to that year fail until a human fixes the row.
    """


# Year files whose last read skipped at least one entry, and the reason. A read
# always precedes a write inside the lock, so this is current by the time
# _save_year consults it. Process-local; a stale entry only ever refuses a
# write it could have allowed, which is the safe direction.
_QUARANTINED_YEARS: dict[Path, str] = {}


@dataclass
class WorkMutationResult:
    """Outcome of a uid-addressed mutation.

    ``update_work_entry``/``remove_work_entry`` return a bare bool, which
    can't tell "no such entry" from "found it, but it's invoiced" — a
    distinction the web API needs to pick 404 vs 409.

    ``status`` is one of ``ok`` / ``not_found`` / ``invoiced`` / ``conflict``
    / ``no_fields``. ``entry`` carries the current server-side entry when one
    was resolved (so a conflict can show the caller what it actually looks like).
    """
    status: str
    entry: WorkEntry | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _work_dir(data_dir: Path) -> Path:
    d = data_dir / "invoices" / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _year_file(work_dir: Path, year: int) -> Path:
    return work_dir / f"{year}.toml"


@contextmanager
def _work_lock(data_dir: Path, *, timeout_seconds: float = 10.0):
    """Serialize read-modify-write cycles on the work-entry store.

    The web process (mark-paid / mark-pending) and the scheduler/CLI
    (invoice generate, invoice paid, add) both rewrite these yearly TOML
    files. Without a lock, two concurrent load→modify→save cycles are
    last-writer-wins on the whole file and one mutation is silently lost.

    Holds an exclusive flock on ``{work_dir}/.work.lock`` (a sibling anchor
    file, never the data files themselves) for the duration of the context.
    Readers don't take the lock; atomic per-file writes (see ``_save_year``)
    keep each file individually consistent for them. Linux + macOS only.
    """
    lock_path = _work_dir(data_dir) / ".work.lock"
    with exclusive_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        on_timeout=WorkStoreLocked,
    ):
        yield


def _parse_date(s: str) -> date:
    parts = s.split("-")
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


def _coerce_date(value) -> date | None:
    """Read a date field written as a TOML date, datetime, or quoted string."""
    if isinstance(value, datetime):
        # A TOML datetime is a `date` subclass whose isoformat() carries a time
        # component — narrow it so downstream string comparisons still work.
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return _parse_date(value.strip())
        except (ValueError, IndexError):
            return None
    return None


def _coerce_number(value) -> float | int | None:
    """Read a numeric field, tolerating a quoted number. None if unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce_text(value) -> str | None:
    """Read a string field, stringifying a bare scalar. None if unusable."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _format_num(value)
    return None


def _load_year(path: Path) -> list[WorkEntry]:
    """Load one year file, skipping rows the loader can't model.

    These files are hand-editable, and a plausible mistake — a quoted date,
    a quoted number — used to load as the wrong type and surface three layers
    later as an ``AttributeError`` from ``.isoformat()`` or a ``TypeError``
    from the sort, taking down every reader of the store. Coercible values are
    coerced; a row missing a usable date/client/service is skipped and its year
    marked quarantined, so the rest of the year stays readable and no write can
    silently delete the row we couldn't read.
    """
    if not path.exists():
        _QUARANTINED_YEARS.pop(path, None)
        return []
    # UTF-8 both ways. `_save_year` writes through `atomic_write`, which
    # encodes UTF-8 rather than taking the locale's encoding, so a reader
    # left implicit would decode a non-ASCII payee with whatever `LANG`
    # happened to say and mangle it on the next save.
    data = tomli.loads(path.read_text(encoding="utf-8"))
    entries = []
    skipped: list[str] = []
    for position, raw in enumerate(data.get("entries", []), 1):
        entry_date = _coerce_date(raw.get("date"))
        client = _coerce_text(raw.get("client"))
        service = _coerce_text(raw.get("service"))
        if entry_date is None or client is None or service is None:
            skipped.append(f"entry #{position} (uid={raw.get('uid') or '?'})")
            logger.error(
                "work_entry_unreadable file=%s position=%d date=%r client=%r service=%r — "
                "skipped; fix the row by hand (writes to this year are refused until then)",
                path.name, position, raw.get("date"), raw.get("client"), raw.get("service"),
            )
            continue
        # An unusable optional number is dropped rather than skipping the whole
        # row: a $0 line on the Work tab is something a human notices, a
        # billable entry that silently vanished is not.
        qty = _coerce_number(raw["qty"]) if raw.get("qty") is not None else None
        amount = _coerce_number(raw["amount"]) if raw.get("amount") is not None else None
        discount = _coerce_number(raw.get("discount", 0))
        entries.append(WorkEntry(
            date=entry_date,
            client=client,
            service=service,
            qty=qty,
            amount=amount,
            discount=0 if discount is None else discount,
            description=_coerce_text(raw.get("description", "")) or "",
            entity=_coerce_text(raw.get("entity", "")) or "",
            invoice=_coerce_text(raw.get("invoice", "")) or "",
            invoice_date=_coerce_date(raw.get("invoice_date")),
            paid_date=_coerce_date(raw.get("paid_date")),
            uid=_coerce_text(raw.get("uid", "")) or "",
            extra={k: v for k, v in raw.items() if k not in _KNOWN_ENTRY_KEYS},
        ))
    if skipped:
        _QUARANTINED_YEARS[path] = ", ".join(skipped)
    else:
        _QUARANTINED_YEARS.pop(path, None)
    return entries


def _format_num(n: float) -> str:
    if n == int(n):
        return str(int(n))
    return str(n)


_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}

# Bare TOML keys are letters, digits, underscores and dashes. Anything else —
# a space, a dot, a quote — has to be written as a quoted key or the file
# fails to parse on the next read.
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _escape(s: str) -> str:
    """Escape a string for a TOML basic string.

    Every control character has to be escaped, not just the newline: a bare
    CR (or tab, or NUL) inside a basic string makes the whole year file
    unparseable, which takes down every reader including invoicing. The web
    routes put arbitrary user strings on this path, so partial escaping was a
    persisted-corruption bug rather than a cosmetic one.
    """
    out = []
    for ch in s:
        esc = _TOML_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < "\x20" or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _format_toml_key(key: str) -> str:
    """Render an ``extra`` key, quoting it when it can't be written bare."""
    if _BARE_KEY_RE.match(key):
        return key
    return f'"{_escape(key)}"'


def _format_toml_value(value) -> str | None:
    """Render a value from ``WorkEntry.extra`` back to TOML, or None if we can't.

    Deliberately narrow: the serializer is hand-rolled string building, so a
    nested table or an arbitrary object has no place to go. Returning None
    drops the key rather than writing something that won't parse back — a
    hand edit must not be able to poison every subsequent save.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_num(value)
    if isinstance(value, str):
        return f'"{_escape(value)}"'
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        rendered = [_format_toml_value(v) for v in value]
        if any(r is None for r in rendered):
            return None
        return "[" + ", ".join(rendered) + "]"
    return None


def _serialize_entry(entry: WorkEntry) -> str:
    lines = ["[[entries]]"]
    if entry.uid:
        lines.append(f'uid = "{_escape(entry.uid)}"')
    lines.append(f"date = {entry.date.isoformat()}")
    lines.append(f'client = "{_escape(entry.client)}"')
    lines.append(f'service = "{_escape(entry.service)}"')
    if entry.qty is not None:
        lines.append(f"qty = {_format_num(entry.qty)}")
    if entry.amount is not None:
        lines.append(f"amount = {_format_num(entry.amount)}")
    if entry.discount:
        lines.append(f"discount = {_format_num(entry.discount)}")
    if entry.description:
        lines.append(f'description = "{_escape(entry.description)}"')
    if entry.entity:
        lines.append(f'entity = "{_escape(entry.entity)}"')
    if entry.invoice:
        lines.append(f'invoice = "{_escape(entry.invoice)}"')
    if entry.invoice_date is not None:
        lines.append(f"invoice_date = {entry.invoice_date.isoformat()}")
    if entry.paid_date is not None:
        lines.append(f"paid_date = {entry.paid_date.isoformat()}")
    for key in sorted(entry.extra):
        rendered = _format_toml_value(entry.extra[key])
        if rendered is None:
            logger.warning(
                "work_entry_extra_key_dropped key=%s type=%s — "
                "the work serializer only writes scalars and flat lists",
                key, type(entry.extra[key]).__name__,
            )
            continue
        lines.append(f"{_format_toml_key(key)} = {rendered}")
    return "\n".join(lines)


def entry_etag(entry: WorkEntry) -> str:
    """Content hash of an entry, for optimistic-concurrency checks.

    Derived from the serialized form, so it covers every persisted field
    (``extra`` included) and nothing transient — notably not the display
    index, which shifts whenever an earlier entry is inserted. Never stored.
    """
    return hashlib.sha256(_serialize_entry(entry).encode()).hexdigest()[:12]


def _render_year(entries: list[WorkEntry]) -> str:
    """Serialize a year's entries, sorted by date. Empty list renders as ''."""
    if not entries:
        return ""
    entries.sort(key=lambda e: e.date)
    return "\n\n".join(_serialize_entry(e) for e in entries) + "\n"


def _save_year(path: Path, entries: list[WorkEntry]) -> None:
    text = _render_year(entries)

    if _QUARANTINED_YEARS.get(path):
        # A write rewrites the whole file, so the row we couldn't read would be
        # deleted. A write that changes nothing visible (this year wasn't the
        # target — _save_entries rewrites every year it knows about) just skips
        # the file; one that would change something is refused.
        if text == _render_year(_load_year(path)):
            return
        raise WorkFileQuarantined(
            f"{path.name} has an entry this version can't read "
            f"({_QUARANTINED_YEARS.get(path)}); writing would delete it. "
            "Fix the row by hand and retry."
        )

    if not entries:
        if path.exists():
            path.unlink()
        return
    # Parse what we're about to write. The serializer is hand-rolled string
    # building, so a field it doesn't escape correctly produces a file that
    # loads back as a TOMLDecodeError — and by then it's on disk, taking down
    # every reader of that year until someone hand-repairs it. Failing the
    # write instead keeps the last good file and surfaces the bug at its source.
    try:
        tomli.loads(text)
    except tomli.TOMLDecodeError as e:
        raise ValueError(f"refusing to write unparseable work file {path.name}: {e}") from e
    # Atomic write: a crash (or a half-written FUSE/rclone flush) mid-write
    # must not leave a truncated or partial year file. The staging name is
    # unique per *call* — it was `.{name}.{pid}.tmp`, which two threads of one
    # process (the web routes reach the store through `asyncio.to_thread`)
    # compute identically, so only `_work_lock` above stood between that and a
    # torn publish.
    write_text_atomic(path, text)


def _load_all(data_dir: Path) -> list[WorkEntry]:
    # Reading must not create the store. ensure_initialised runs a backfill on
    # every money request, and that used to mkdir invoices/work/ (plus a lock
    # anchor) on the Nextcloud mount for users who never touch invoicing.
    wd = data_dir / "invoices" / "work"
    if not wd.is_dir():
        return []
    all_entries = []
    for f in sorted(wd.glob("*.toml")):
        try:
            int(f.stem)
        except ValueError:
            continue
        all_entries.extend(_load_year(f))
    all_entries.sort(key=lambda e: e.date)
    return all_entries


def _save_entries(data_dir: Path, entries: list[WorkEntry]) -> None:
    wd = _work_dir(data_dir)
    # Any entry touched by a write acquires a uid as a side effect, so the
    # store converges on full coverage even between explicit backfills.
    for entry in entries:
        if not entry.uid:
            entry.uid = new_txn_id()
    by_year: dict[int, list[WorkEntry]] = {}
    for entry in entries:
        by_year.setdefault(entry.date.year, []).append(entry)
    existing_years: set[int] = set()
    for f in wd.glob("*.toml"):
        try:
            existing_years.add(int(f.stem))
        except ValueError:
            pass
    for year in existing_years | by_year.keys():
        _save_year(_year_file(wd, year), by_year.get(year, []))


def load_work_entries(data_dir: Path) -> list[WorkEntry]:
    """Load all entries from all year files, sorted by date.
    Sets entry.id to 1-based display index."""
    entries = _load_all(data_dir)
    for i, entry in enumerate(entries, 1):
        entry.id = i
    return entries


def quarantined_years(data_dir: Path) -> dict[str, str]:
    """Year files under ``data_dir`` holding a row this version can't read.

    Maps file name to the reason recorded by the last read. A caller counting
    references has to consult this: a skipped row is invisible to
    ``load_work_entries``, so a reference living in one reads as zero and a
    guard built on that count fails *open* — which is how deleting the service
    an unreadable row names would unbill it once someone repairs the row.

    ``_QUARANTINED_YEARS`` is process-local and refreshed per year file on
    every read, so this is only meaningful immediately after a
    ``load_work_entries`` call against the same store.
    """
    wd = data_dir / "invoices" / "work"
    return {
        path.name: reason
        for path, reason in _QUARANTINED_YEARS.items()
        if path.parent == wd and reason
    }


def add_work_entry(
    data_dir: Path,
    entry_date: str,
    client: str,
    service: str,
    qty: float | None = None,
    amount: float | None = None,
    discount: float = 0,
    description: str = "",
    entity: str = "",
    invoice: str = "",
    invoice_date: str | date | None = None,
    uid: str = "",
) -> int:
    """Append entry to correct year file, return display index.

    Pass ``uid`` to choose the stable id up front — a caller that needs to
    find the entry it just created (the web API) generates one, passes it,
    and looks the entry back up by it, since the returned display index is
    already stale the moment another writer inserts something earlier.

    ``invoice_date`` is only meaningful alongside ``invoice`` — the one caller
    that pre-assigns a number (``invoice create``) passes the date it put on
    the PDF, so the record and the document agree. It defaults to today when a
    number is given without one, and is ignored entirely without a number.
    """
    d = _parse_date(entry_date)
    stamped = _coerce_stamp_date(invoice_date) if invoice else None
    new_entry = WorkEntry(
        date=d, client=client.lower(), service=service,
        qty=qty, amount=amount, discount=discount,
        description=description, entity=entity, invoice=invoice,
        invoice_date=stamped,
        uid=uid or new_txn_id(),
    )
    with _work_lock(data_dir):
        entries = _load_all(data_dir)
        entries.append(new_entry)
        entries.sort(key=lambda e: e.date)
        _save_entries(data_dir, entries)
    for i, e in enumerate(entries, 1):
        if e is new_entry:
            return i
    return len(entries)


def list_work_entries(
    data_dir: Path,
    client: str | None = None,
    invoiced: bool | None = None,
    period: str | None = None,
) -> list[WorkEntry]:
    """Filter and return entries."""
    entries = load_work_entries(data_dir)
    if client:
        client_lower = client.lower()
        entries = [e for e in entries if e.client.lower() == client_lower]
    if invoiced is True:
        entries = [e for e in entries if e.invoice]
    elif invoiced is False:
        entries = [e for e in entries if not e.invoice]
    if period:
        entries = [e for e in entries if e.date.isoformat().startswith(period)]
    return entries


def _coerce_stamp_date(value: str | date | None) -> date:
    """The issue date to stamp: what the caller passed, or today.

    Anything that isn't a date is refused here rather than assigned. The
    serializer calls ``.isoformat()`` on it, and ``_save_entries`` writes year
    files one at a time, so a bad value raises part-way through and leaves
    some years written and some not.
    """
    if value is None:
        return date.today()
    if isinstance(value, str):
        return _parse_date(value)
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, date):
        raise TypeError(f"invoice_date must be a date or ISO string, got {type(value).__name__}")
    return value


def _apply_fields(entry: WorkEntry, fields: dict) -> None:
    """Assign updatable fields onto an entry, coercing date/client as the CLI does."""
    for key, value in fields.items():
        if key in _PROTECTED_FIELDS:
            continue
        if key == "date" and isinstance(value, str):
            value = _parse_date(value)
        if key == "client" and isinstance(value, str):
            value = value.lower()
        if key == "invoice_date" and value is not None:
            value = _coerce_stamp_date(value)
        if hasattr(entry, key):
            setattr(entry, key, value)


def _sync_invoice_date(entry: WorkEntry, entries: list[WorkEntry], fields: dict) -> None:
    """Keep ``invoice_date`` tied to the number a hand-assignment just wrote.

    ``work update --invoice`` is the one path that stamps a number without
    going through an assign call, and what it should stamp depends on whether
    the number is new:

    * **An invoice that already went out** — inherit its date. Stamping today
      would drag the whole invoice forward (readers take the earliest stored
      date, but the entry would still disagree with the document) and could
      push the matcher's bound past a payment that really did settle it.
    * **An invoice raised before this field existed** — leave it unstamped, so
      the invoice keeps its legacy fallback. There is no date to inherit and a
      synthesized one would be a guess recorded as a fact.
    * **A number nothing else carries** — today, the same as any other
      first-time assignment.

    Clearing the number clears the date. That is unreachable today, since both
    update paths refuse an entry that already carries an invoice, but it keeps
    the two fields tied together here rather than resting on a caller's guard.
    """
    if "invoice" not in fields or "invoice_date" in fields:
        return
    if not entry.invoice:
        entry.invoice_date = None
        return
    siblings = [e for e in entries if e is not entry and e.invoice == entry.invoice]
    if not siblings:
        entry.invoice_date = _coerce_stamp_date(None)
        return
    stamped = [e.invoice_date for e in siblings if e.invoice_date is not None]
    entry.invoice_date = min(stamped) if stamped else None


def update_work_entry(data_dir: Path, index: int, **fields) -> bool:
    """Update fields on entry at 1-based display index. Only if uninvoiced.

    Index-addressed, so only safe when the caller read the list and acts on it
    immediately (the CLI). Anything holding a reference across time should use
    :func:`update_work_entry_by_uid`.
    """
    if not fields:
        return False
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        if index < 1 or index > len(entries):
            return False
        entry = entries[index - 1]
        if entry.invoice:
            return False
        _apply_fields(entry, fields)
        _sync_invoice_date(entry, entries, fields)
        _save_entries(data_dir, entries)
        return True


def _find_by_uid(entries: list[WorkEntry], uid: str) -> WorkEntry | None:
    if not uid:
        # An un-backfilled entry carries uid == "" — it must not be addressable
        # by an empty uid, or one bad request would hit an arbitrary row.
        return None
    for entry in entries:
        if entry.uid == uid:
            return entry
    return None


def update_work_entry_by_uid(
    data_dir: Path,
    uid: str,
    *,
    expect_etag: str | None = None,
    **fields,
) -> WorkMutationResult:
    """Update an entry addressed by its stable ``uid``. Only if uninvoiced.

    Resolve-and-mutate happens inside the write lock, so a concurrent insert
    can't make this land on a different entry. ``expect_etag`` adds an
    optimistic-concurrency check: a mismatch means the entry changed since the
    caller read it, and nothing is written.
    """
    if not fields:
        return WorkMutationResult("no_fields")
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        entry = _find_by_uid(entries, uid)
        if entry is None:
            return WorkMutationResult("not_found")
        if entry.invoice:
            return WorkMutationResult("invoiced", entry)
        if expect_etag and entry_etag(entry) != expect_etag:
            return WorkMutationResult("conflict", entry)
        _apply_fields(entry, fields)
        _sync_invoice_date(entry, entries, fields)
        _save_entries(data_dir, entries)
        # Re-read so the caller gets a correct display index (a date change
        # may have moved the entry) rather than the pre-save one.
        fresh = _find_by_uid(load_work_entries(data_dir), uid)
        return WorkMutationResult("ok", fresh or entry)


def remove_work_entry_by_uid(
    data_dir: Path,
    uid: str,
    *,
    expect_etag: str | None = None,
) -> WorkMutationResult:
    """Remove an entry addressed by its stable ``uid``. Only if uninvoiced."""
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        entry = _find_by_uid(entries, uid)
        if entry is None:
            return WorkMutationResult("not_found")
        if entry.invoice:
            return WorkMutationResult("invoiced", entry)
        if expect_etag and entry_etag(entry) != expect_etag:
            return WorkMutationResult("conflict", entry)
        entries.remove(entry)
        _save_entries(data_dir, entries)
        return WorkMutationResult("ok", entry)


def backfill_work_ids(data_dir: Path, *, timeout_seconds: float = 10.0) -> int:
    """Stamp a ``uid`` on every entry that lacks one. Returns the count stamped.

    Idempotent, and safe to run from ``ensure_initialised`` on every entry
    point: it only writes when something is actually missing an id, so a
    fully-backfilled store costs one lock-free read. That pre-check matters —
    ``ensure_initialised`` runs on every money web request and every skill
    invocation, so taking the exclusive write lock first put a plain
    ``GET /work`` in contention with ``invoice generate`` and made it wait out
    the lock timeout. Unlike the ledger's equivalent this carries no sentinel —
    a hand-added entry with no ``uid`` can appear at any time, and re-running
    is how that self-heals.
    """
    if not any(not e.uid for e in _load_all(data_dir)):
        return 0
    with _work_lock(data_dir, timeout_seconds=timeout_seconds):
        # Re-read under the lock: another writer may have stamped these while
        # we were deciding, and its entries are the ones we must not clobber.
        entries = load_work_entries(data_dir)
        missing = [e for e in entries if not e.uid]
        if not missing:
            return 0
        _save_entries(data_dir, entries)  # stamps every uid-less entry
        return len(missing)


def remove_work_entry(data_dir: Path, index: int) -> bool:
    """Remove entry at 1-based display index. Only if uninvoiced."""
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        if index < 1 or index > len(entries):
            return False
        entry = entries[index - 1]
        if entry.invoice:
            return False
        entries.pop(index - 1)
        _save_entries(data_dir, entries)
        return True


def get_uninvoiced_entries(
    data_dir: Path,
    client: str | None = None,
    period: str | None = None,
) -> list[WorkEntry]:
    """Get entries where invoice is not set."""
    entries = load_work_entries(data_dir)
    result = [e for e in entries if not e.invoice]
    if client:
        client_lower = client.lower()
        result = [e for e in result if e.client.lower() == client_lower]
    if period:
        year, month = map(int, period.split("-"))
        if month == 12:
            upper = date(year + 1, 1, 1)
        else:
            upper = date(year, month + 1, 1)
        result = [e for e in result if e.date < upper]
    return result


def assign_invoice_number_by_uids(
    data_dir: Path,
    uids: list[str],
    invoice_number: str,
    invoice_date: str | date | None = None,
) -> int:
    """Stamp an invoice number on the entries with these ``uid``s. Returns count.

    The uid-addressed form of :func:`assign_invoice_number`, and the one
    invoice generation uses. Generation resolves its billable entries, renders
    PDFs (seconds), and only then stamps — a window in which any other writer
    (the web Work tab above all) can insert an earlier-dated entry and shift
    every display index. An index-addressed stamp then lands on the wrong row:
    one client's entry takes another's invoice number, and the entry actually
    on the rendered PDF stays uninvoiced and is billed again next run.

    Entries that already carry an invoice are skipped, so a concurrent stamp
    wins rather than being overwritten.

    ``invoice_date`` is stamped in the same lock acquisition as the number, so
    the two cannot diverge. Pass the date the caller already committed to —
    invoice generation renders one onto the PDF before it stamps, and a fresh
    ``date.today()`` here would disagree with the document across midnight.
    """
    wanted = [u for u in uids if u]
    if not wanted:
        return 0
    stamp = _coerce_stamp_date(invoice_date)
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        count = 0
        seen: set[str] = set()
        for uid in wanted:
            if uid in seen:
                continue
            seen.add(uid)
            entry = _find_by_uid(entries, uid)
            if entry is None or entry.invoice:
                continue
            entry.invoice = invoice_number
            entry.invoice_date = stamp
            count += 1
        if count:
            _save_entries(data_dir, entries)
        return count


def assign_invoice_number(
    data_dir: Path,
    indices: list[int],
    invoice_number: str,
    invoice_date: str | date | None = None,
) -> int:
    """Stamp invoice number and issue date on entries at display indices.

    Index-addressed, so only safe when the caller resolved the indices and
    stamps immediately. Anything holding a reference across time — invoice
    generation, the web UI — must use :func:`assign_invoice_number_by_uids`.
    Returns the number of entries stamped.
    """
    if not indices:
        return 0
    stamp = _coerce_stamp_date(invoice_date)
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        count = 0
        for idx in indices:
            if idx < 1 or idx > len(entries):
                continue
            entry = entries[idx - 1]
            if entry.invoice:
                continue
            entry.invoice = invoice_number
            entry.invoice_date = stamp
            count += 1
        if count:
            _save_entries(data_dir, entries)
        return count


def record_invoice_payment(
    data_dir: Path,
    invoice_number: str,
    paid_date: str | date,
) -> int:
    """Set paid_date on all entries for an invoice. Returns count."""
    if isinstance(paid_date, str):
        paid_date = _parse_date(paid_date)
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        count = 0
        for entry in entries:
            if entry.invoice == invoice_number and entry.paid_date is None:
                entry.paid_date = paid_date
                count += 1
        if count:
            _save_entries(data_dir, entries)
        return count


def clear_invoice_payment(data_dir: Path, invoice_number: str) -> int:
    """Clear paid_date on all entries for an invoice, keeping the invoice number.

    The inverse of :func:`record_invoice_payment` — marks a paid invoice
    pending again without un-invoicing it (unlike :func:`void_invoice`).
    Returns the number of entries modified.
    """
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        count = 0
        for entry in entries:
            if entry.invoice == invoice_number and entry.paid_date is not None:
                entry.paid_date = None
                count += 1
        if count:
            _save_entries(data_dir, entries)
        return count


def get_entries_for_invoice(data_dir: Path, invoice_number: str) -> list[WorkEntry]:
    """Get all entries assigned to an invoice."""
    return [e for e in load_work_entries(data_dir) if e.invoice == invoice_number]


def invoice_issue_date(entries: list[WorkEntry]) -> date | None:
    """When an invoice was issued, or the closest sound estimate.

    Every path that stamps an invoice number now stamps the date with it, so
    for anything invoiced since that landed this is the real issue date.
    Invoices raised before it have no stored date and nothing can reconstruct
    one, so they fall back to the *latest* work billed — an invoice cannot
    predate the last work on it. That fallback is the legacy path, not the
    design: it is a lower bound, weeks early on a period invoiced in arrears,
    and it stays only because those records exist.

    Returns ``None`` for an empty list.
    """
    if not entries:
        return None
    stamped = [e.invoice_date for e in entries if e.invoice_date is not None]
    if stamped:
        # Entries on one invoice are stamped together, so these normally agree.
        # A hand-edited file can disagree with itself; take the earliest, since
        # this is a "cannot have existed before this" bound and the earliest is
        # the reading that cannot reject a payment the invoice really caused.
        return min(stamped)
    return max(e.date for e in entries)


def void_invoice(data_dir: Path, invoice_number: str) -> int:
    """Clear invoice, issue date and paid_date on all entries for an invoice.

    Returns the number of entries modified.
    """
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        count = 0
        for entry in entries:
            if entry.invoice == invoice_number:
                entry.invoice = ""
                entry.invoice_date = None
                entry.paid_date = None
                count += 1
        if count:
            _save_entries(data_dir, entries)
        return count


def get_invoice_numbers(data_dir: Path) -> list[str]:
    """Get distinct invoice numbers, sorted."""
    entries = load_work_entries(data_dir)
    return sorted(set(e.invoice for e in entries if e.invoice))
