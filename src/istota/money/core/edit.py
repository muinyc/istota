"""Stable transaction IDs and in-place ledger editing.

Beancount ledgers are plain text with no native stable identifier for a
transaction. This module gives every transaction an ``id:`` metadata line
(see :func:`new_txn_id`) so the web UI and CLI can locate and edit a
directive robustly — surviving edits to the date / payee / narration /
account / amount that the old ``(date, payee, narration, account, position)``
tuple could not.

Three pieces live here:

* :func:`new_txn_id` — UUID generator for the ``id:`` metadata.
* :func:`_ledger_lock` — exclusive flock serializing ledger writes, over the
  shared :func:`istota.file_lock.exclusive_lock` that
  :func:`istota.money.work._work_lock` also uses. The web editor and the
  scheduler's Monarch sync both mutate the ledger tree; without this they race.
* :func:`backfill_ledger_ids` / :func:`edit_transaction` (later stages).
"""

from __future__ import annotations

import re as _re
from contextlib import contextmanager
from pathlib import Path

from istota.atomic_write import write_text_atomic
from istota.file_lock import exclusive_lock

from .ids import new_txn_id
from .ledger import run_bean_check
from .transactions import backup_ledger

__all__ = [
    "LedgerLocked",
    "new_txn_id",
    "backfill_ledger_ids",
    "edit_transaction",
]


class LedgerLocked(RuntimeError):
    """Raised when the ledger write lock can't be acquired in time."""


@contextmanager
def _ledger_lock(ledger_path: Path, *, timeout_seconds: float = 10.0):
    """Serialize read-modify-write cycles on a ledger tree.

    The web process (transaction edit) and the scheduler/CLI (Monarch sync,
    invoice payment posting, manual add) all mutate beancount files under the
    same ledger directory. ``append_to_ledger`` historically just appended
    after a backup with no lock, so a web edit could interleave with a
    concurrent sync and one mutation be lost.

    Holds an exclusive flock on ``{ledger_dir}/.ledger.lock`` (a sibling
    anchor file, never a data file) for the duration of the context. Readers
    don't take the lock; atomic temp-file + ``os.replace`` writes keep each
    file individually consistent for them. Linux + macOS only.
    """
    lock_path = Path(ledger_path).parent / ".ledger.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        on_timeout=LedgerLocked,
    ):
        yield


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a staging file + ``os.replace``.

    A crash (or a half-written FUSE/rclone flush) mid-write must not leave a
    truncated ledger file.

    The staging name used to be ``.{name}.{pid}.tmp``, which is unique per
    *process* and not per call — and the writers here are not always separate
    processes. ``money/routes.py`` reaches the ledger through
    ``asyncio.to_thread`` at two sites, so two concurrent web requests are two
    threads of one process computing the identical staging path in the
    identical directory. Only the flock above this stood between that and a
    torn publish. ``atomic_write`` mints the name with ``mkstemp``, which is
    unique per call.
    """
    write_text_atomic(path, text)


def backfill_ledger_ids(ledger_path: Path) -> dict:
    """Stamp an ``id:`` metadata line on every transaction that lacks one.

    Walks the whole include tree via ``load_file`` (so transactions in
    ``transactions/*.beancount`` and ``imports/*.beancount`` get ids too) and
    inserts ``id: "<uuid>"`` as the first line under each transaction header.
    The insertion is purely additive — every other byte (comments, blank
    lines, other postings, metadata) is preserved.

    Idempotent: transactions that already carry an ``id:`` are skipped.

    Safety: the whole pass runs under the ledger lock; each touched file is
    backed up and written atomically; the root ledger is re-validated with
    ``bean-check`` afterward and, on failure, every touched file is restored
    byte-for-byte from an in-memory snapshot.

    Returns ``{"status": "ok", "stamped": N, "files": [...]}`` or an error
    envelope with ``validation_errors`` on a failed re-check.
    """
    from beancount.core.data import Transaction
    from beancount.loader import load_file

    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return {"status": "error", "error": f"Ledger not found: {ledger_path}"}

    entries, load_errors, _ = load_file(str(ledger_path))
    if load_errors:
        return {
            "status": "error",
            "error": "Ledger has validation errors; fix before backfilling ids",
            "validation_errors": [str(e.message) for e in load_errors[:10]],
        }

    # filename -> list of header linenos needing an id
    targets: dict[str, list[int]] = {}
    for e in entries:
        if not isinstance(e, Transaction) or e.meta.get("id"):
            continue
        fn = e.meta.get("filename")
        ln = e.meta.get("lineno")
        if fn is None or ln is None:
            continue
        targets.setdefault(fn, []).append(int(ln))

    if not targets:
        return {"status": "ok", "stamped": 0, "files": []}

    with _ledger_lock(ledger_path):
        snapshots: dict[Path, str] = {}
        stamped = 0
        for fn, linenos in targets.items():
            p = Path(fn)
            original = p.read_text(encoding="utf-8")
            snapshots[p] = original
            backup_ledger(p)  # audit trail

            lines = original.splitlines(keepends=True)
            # Descending so earlier insertions don't shift later linenos.
            for ln in sorted(linenos, reverse=True):
                lines.insert(ln, f'  id: "{new_txn_id()}"\n')
                stamped += 1
            _atomic_write(p, "".join(lines))

        ok, errors = run_bean_check(ledger_path)
        if not ok:
            # Restore atomically — a half-written FUSE flush during rollback
            # would truncate a known-bad file with no backup-of-the-backup.
            for p, original in snapshots.items():
                _atomic_write(p, original)
            return {
                "status": "error",
                "error": "Backfill produced an invalid ledger; rolled back",
                "validation_errors": errors[:10],
            }

        return {
            "status": "ok",
            "stamped": stamped,
            "files": [str(p) for p in snapshots],
        }


# Quoted-string token in a beancount header (handles escaped quotes).
_QUOTED = r'"(?:[^"\\]|\\.)*"'
_QUOTED_RE = _re.compile(_QUOTED)


def _quote(value: str) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def _rewrite_header(
    line: str,
    *,
    has_payee: bool,
    new_date: str | None,
    new_payee: str | None,
    new_narration: str | None,
) -> str:
    """Rewrite a transaction header line in place, preserving the flag, the
    trailing tags / links / inline comment, and spacing outside the edited
    tokens.

    ``has_payee`` reflects the parsed entry: a two-string header is
    ``"payee" "narration"``; a one-string header is narration-only.
    """
    newline = "\n" if line.endswith("\n") else ""
    body = line[: -len(newline)] if newline else line

    # Date is the fixed-width leading token.
    if new_date:
        body = new_date + body[10:]

    spans = [m.span() for m in _QUOTED_RE.finditer(body)]
    # Apply right-to-left so earlier spans keep their offsets.
    edits: list[tuple[int, int, str]] = []
    if has_payee and len(spans) >= 2:
        if new_payee is not None:
            edits.append((*spans[0], _quote(new_payee)))
        if new_narration is not None:
            edits.append((*spans[1], _quote(new_narration)))
    elif spans:
        # Narration-only header. The single string is the narration.
        if new_narration is not None:
            edits.append((*spans[0], _quote(new_narration)))
        if new_payee is not None:
            # Promote to a two-string header: insert the payee before narration.
            s, _e = spans[0]
            edits.append((s, s, _quote(new_payee) + " "))

    for start, end, repl in sorted(edits, key=lambda x: x[0], reverse=True):
        body = body[:start] + repl + body[end:]

    return body + newline


def _split_posting(line: str) -> tuple[str, str, str, str] | None:
    """Split a posting line into (indent, account, separator, amount).

    Returns ``None`` for non-posting lines (metadata ``key:`` lines, comments,
    blanks). A posting account starts with an uppercase root segment.
    """
    newline = "\n" if line.endswith("\n") else ""
    body = line[: -len(newline)] if newline else line
    stripped = body.lstrip()
    if not stripped or stripped.startswith(";"):
        return None
    indent = body[: len(body) - len(stripped)]
    if not indent:
        return None  # col-0 line — not a posting
    # Metadata: lowercase key immediately followed by a colon.
    if _re.match(r"^[a-z][\w-]*:", stripped):
        return None
    parts = stripped.split(None, 1)
    account = parts[0]
    amount = parts[1] if len(parts) > 1 else ""
    return indent, account, newline, amount


def _norm_amount(s: str) -> str:
    return " ".join((s or "").split())


def edit_transaction(
    ledger_path: Path,
    txn_id: str,
    *,
    old_account: str | None = None,
    old_position: str | None = None,
    new_date: str | None = None,
    new_payee: str | None = None,
    new_narration: str | None = None,
    new_account: str | None = None,
    new_position: str | None = None,
) -> dict:
    """Edit a transaction in place, located by its stable ``id:`` metadata.

    Performs a surgical text rewrite — the header (date / payee / narration)
    and the single posting identified by ``old_account`` (+ ``old_position``
    to pick the leg when an account repeats) are edited; every other line
    (other postings, ``id:``, comments, tags, links) is preserved. An
    ``edited: "<today>"`` metadata line is stamped so Monarch sync knows not
    to overwrite the change.

    Runs under the ledger lock; the file is backed up and written atomically,
    then the root ledger is re-validated with ``bean-check``. If validation
    fails (e.g. an amount edit unbalanced the entry), the file is restored
    byte-for-byte and the validation errors are returned.
    """
    from datetime import date as _date

    from beancount.core.data import Transaction
    from beancount.loader import load_file

    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return {"status": "error", "error": f"Ledger not found: {ledger_path}"}

    entries, _load_errors, _ = load_file(str(ledger_path))
    matches = [
        e for e in entries
        if isinstance(e, Transaction) and e.meta.get("id") == txn_id
    ]
    if not matches:
        return {"status": "error", "error": f"Transaction not found: {txn_id}"}
    if len(matches) > 1:
        # A duplicated id (crashed/re-run backfill, hand-copied block) would
        # otherwise let us silently edit the first match. Refuse instead.
        return {
            "status": "error",
            "error": f"Ambiguous id {txn_id!r}: {len(matches)} transactions share it",
        }
    target = matches[0]

    filename = target.meta.get("filename")
    lineno = target.meta.get("lineno")
    if filename is None or lineno is None:
        return {"status": "error", "error": f"Transaction {txn_id} has no source location"}

    file_path = Path(filename)
    # UTF-8 both ways — see `_atomic_write`; beancount files are UTF-8 and
    # an implicit reader would decode them with the locale's encoding.
    original = file_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    header_idx = int(lineno) - 1
    if header_idx < 0 or header_idx >= len(lines):
        return {"status": "error", "error": f"Bad source line for {txn_id}"}

    # Block body: indented lines following the header, up to a blank line / EOF.
    body_start = header_idx + 1
    body_end = body_start
    while body_end < len(lines):
        ln = lines[body_end]
        if ln.strip() == "" or not ln[:1].isspace():
            break
        body_end += 1

    old_summary = {
        "date": str(target.date),
        "payee": target.payee,
        "narration": target.narration,
        "account": old_account,
        "position": old_position,
    }

    # 1) Header.
    if new_date or new_payee is not None or new_narration is not None:
        lines[header_idx] = _rewrite_header(
            lines[header_idx],
            has_payee=target.payee is not None,
            new_date=new_date,
            new_payee=new_payee,
            new_narration=new_narration,
        )

    # 2) Posting (account / amount).
    posting_changed = old_account is not None and (
        (new_account is not None and new_account != old_account)
        or (new_position is not None and _norm_amount(new_position) != _norm_amount(old_position or ""))
    )
    if posting_changed:
        matched = False
        for idx in range(body_start, body_end):
            split = _split_posting(lines[idx])
            if not split:
                continue
            indent, account, newline, amount = split
            if account != old_account:
                continue
            if old_position and _norm_amount(amount) != _norm_amount(old_position):
                continue
            # Cost basis ({...}) and price annotations (@ / @@) live in the
            # amount token. The surgical rewrite re-emits new_position verbatim,
            # so an amount edit would silently drop the lot — and the entry can
            # still balance, so bean-check wouldn't catch it. Refuse rather than
            # corrupt the lot. (Account-only edits keep the original amount and
            # are fine.) See ISSUE-107.
            amount_changing = (
                new_position is not None
                and _norm_amount(new_position) != _norm_amount(amount)
            )
            if amount_changing and ("{" in amount or "@" in amount):
                return {
                    "status": "error",
                    "error": (
                        f"Posting {old_account!r} carries a cost/price annotation "
                        f"({amount!r}); editing its amount here would drop the lot. "
                        "Edit it directly in the ledger file."
                    ),
                }
            final_account = new_account or account
            final_amount = new_position if new_position is not None else amount
            rebuilt = indent + final_account
            if final_amount:
                rebuilt += "  " + _norm_amount(final_amount)
            lines[idx] = rebuilt + newline
            matched = True
            break
        if not matched:
            return {
                "status": "error",
                "error": (
                    f"Posting {old_account!r} ({old_position!r}) not found "
                    f"on transaction {txn_id}"
                ),
            }

    # 3) Stamp / refresh edited metadata.
    today = _date.today().isoformat()
    edited_idx = None
    for idx in range(body_start, body_end):
        if _re.match(r"^\s+edited:", lines[idx]):
            edited_idx = idx
            break
    if edited_idx is not None:
        lines[edited_idx] = f'  edited: "{today}"\n'
    else:
        lines.insert(header_idx + 1, f'  edited: "{today}"\n')

    new_text = "".join(lines)

    with _ledger_lock(ledger_path):
        backup_ledger(file_path)
        _atomic_write(file_path, new_text)
        ok, errors = run_bean_check(ledger_path)
        if not ok:
            _atomic_write(file_path, original)
            return {
                "status": "error",
                "error": "Edit produced an invalid ledger; rolled back",
                "validation_errors": errors[:10],
            }

    return {
        "status": "ok",
        "id": txn_id,
        "file": str(file_path),
        "old": old_summary,
        "new": {
            "date": new_date or old_summary["date"],
            "payee": new_payee if new_payee is not None else old_summary["payee"],
            "narration": new_narration if new_narration is not None else old_summary["narration"],
            "account": new_account or old_account,
            "position": new_position if new_position is not None else old_position,
        },
    }
