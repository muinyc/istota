"""Modular import system for transaction sources.

Each source provides a parser that produces NormalizedTransaction objects.
The shared import_transactions() pipeline handles dedup, beancount formatting,
staging files, and ledger writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from istota.money.core.dedup import compute_transaction_hash, parse_ledger_transactions
from istota.money.core.ids import new_txn_id
from istota.money.core.transactions import (
    MONARCH_CATEGORY_MAP,
    format_beancount_transaction,
    lookup_mapping,
    uncategorized_account,
    append_to_ledger,
)

from .base import NormalizedTransaction


@dataclass
class ImportSource:
    """Registry entry for an import source."""
    name: str
    source_type: str  # "csv" or "api"
    detect: Callable[[Path], bool] | None = None
    kind: str = "transactions"  # "transactions" | "positions"


IMPORT_SOURCES: dict[str, ImportSource] = {}


def register_source(source: ImportSource) -> None:
    IMPORT_SOURCES[source.name] = source


def _register_builtin_sources() -> None:
    from .fidelity_positions import detect_fidelity_positions_csv
    from .fina_history import detect_fina_history_csv
    from .monarch_csv import detect_monarch_csv

    register_source(ImportSource(
        name="monarch-csv",
        source_type="csv",
        detect=detect_monarch_csv,
    ))
    register_source(ImportSource(
        name="monarch-api",
        source_type="api",
        detect=None,
    ))
    register_source(ImportSource(
        name="fidelity-positions-csv",
        source_type="csv",
        detect=detect_fidelity_positions_csv,
        kind="positions",
    ))
    register_source(ImportSource(
        name="fina-history-csv",
        source_type="csv",
        detect=detect_fina_history_csv,
        kind="positions",
    ))


_register_builtin_sources()


def detect_source(file_path: Path) -> str | None:
    """Auto-detect import source from file headers.

    Returns source name or None if no match.
    """
    for name, source in IMPORT_SOURCES.items():
        if source.detect is not None and source.detect(file_path):
            return name
    return None


def parse_positions_file(file_path: Path, source_name: str | None = None):
    """Detect + parse a positions-kind file into ``list[ParsedSnapshot]``.

    Raises :class:`PositionParseError` (never a stack trace to the user) on
    an unknown/undetectable source or a transactions-kind match.
    """
    from .fidelity_positions import parse_fidelity_positions_csv
    from .fina_history import parse_fina_history_csv
    from .positions_base import PositionParseError

    parsers = {
        "fidelity-positions-csv": parse_fidelity_positions_csv,
        "fina-history-csv": parse_fina_history_csv,
    }
    if source_name is None:
        source_name = detect_source(file_path)
        if source_name is None:
            tried = sorted(
                name for name, src in IMPORT_SOURCES.items() if src.detect is not None
            )
            raise PositionParseError(
                f"Could not detect the file format (tried: {', '.join(tried)})"
            )
    source = IMPORT_SOURCES.get(source_name)
    if source is None:
        raise PositionParseError(f"Unknown import source: {source_name}")
    if source.kind != "positions":
        raise PositionParseError(
            f"{source_name} is a transactions source — use `import-csv` for "
            "transaction files"
        )
    return parsers[source_name](file_path)


def import_transactions(
    ledger_path: Path,
    transactions: list[NormalizedTransaction],
    source_name: str,
    contra_account: str,
    category_map: dict[str, str] | None = None,
    db_conn=None,
    source_file: str | None = None,
    rules=None,
) -> dict:
    """Shared import pipeline for all file-based sources.

    Steps:
    1. Content-based dedup against ledger and DB
    2. Resolve the two posting accounts, and the skip decision
    3. Format beancount entries
    4. Write staging file
    5. Append to ledger
    6. Track hashes in DB

    ``rules`` is compiled transaction rules in evaluation order, or ``None``.
    ``None`` leaves the ``category_map`` path exactly as it was, which is what
    every existing caller gets. When rules are given they replace it whole —
    ``category_map`` is ignored rather than layered, because the map's own
    tiers are already in the rule list (the user's map is the compatibility
    view of these rows, and ``MONARCH_CATEGORY_MAP`` is seeded at priority
    900), so consulting both would apply one tier twice.

    ``contra_account`` likewise becomes the *fallback* rather than the answer:
    a CSV import gets per-transaction contra accounts for the first time, and
    the file's ``--account`` fills the slot no rule did. That is a behaviour
    change and the point of the stage — a CSV import and an API sync of the
    same transaction now resolve identically.
    """
    if not transactions:
        return {"status": "error", "error": "No transactions to import"}

    from istota.money.core import rules as rule_engine

    ledger_hashes = parse_ledger_transactions(ledger_path)

    entries = []
    content_hashes = []
    content_skipped_count = 0
    rule_skipped_count = 0

    for txn in transactions:
        # The skip decision comes first, ahead of dedup, matching
        # `sync_monarch`'s ordering. A row the user excluded is not a row that
        # was "already in the ledger", and counting it that way would make the
        # two paths' `rule_skipped_count` mean different things — on a stage
        # whose claim is that a CSV import and an API sync of the same
        # transaction now resolve identically.
        resolution = None
        if rules is not None:
            resolution = rule_engine.resolve(txn, rules)
            if resolution.skip:
                rule_skipped_count += 1
                continue

        content_hash = compute_transaction_hash(
            txn.date.isoformat(), abs(txn.amount), txn.payee,
        )

        if content_hash in ledger_hashes:
            content_skipped_count += 1
            continue

        if db_conn is not None:
            from istota.money.db import is_content_hash_synced
            if is_content_hash_synced(db_conn, content_hash):
                content_skipped_count += 1
                continue

        entry_contra = contra_account
        if resolution is None:
            if txn.category:
                # Was two arms, both resolving the same way for the builtin map
                # every caller passes (ISSUE-426).
                posting_account = lookup_mapping(
                    txn.category,
                    category_map or MONARCH_CATEGORY_MAP,
                    uncategorized_account,
                )
            else:
                posting_account = "Expenses:Uncategorized"
        else:
            if resolution.contra_account is not None:
                entry_contra = resolution.contra_account
            if resolution.posting_account is not None:
                posting_account = resolution.posting_account
            elif txn.category:
                # The bare fallback the two mapping functions end on. Both
                # forms are preserved: a slug for a category, and the
                # unsuffixed account for a source that gave none.
                posting_account = uncategorized_account(txn.category)
            else:
                posting_account = "Expenses:Uncategorized"

        entry = format_beancount_transaction(
            txn_date=txn.date,
            payee=txn.payee,
            narration=txn.notes or txn.category or "Imported transaction",
            posting_account=posting_account,
            contra_account=entry_contra,
            amount=txn.amount,
            metadata={"id": new_txn_id()},
        )
        entries.append(entry)
        content_hashes.append(content_hash)

    if not entries:
        # Name both reasons. Attributing a rule skip to ledger dedup sends the
        # user looking for a duplicate that is not there — and this dict is
        # what the CLI prints and what the skill hands to the model.
        reasons = [f"{content_skipped_count} already in ledger"]
        if rule_skipped_count:
            reasons.append(f"{rule_skipped_count} skipped by rules")
        result = {
            "status": "ok",
            "transaction_count": 0,
            "content_skipped_count": content_skipped_count,
            "message": f"No new transactions to import ({', '.join(reasons)})",
        }
        if rules is not None:
            result["rule_skipped_count"] = rule_skipped_count
        return result

    # Write staging file
    ledger_dir = ledger_path.parent
    imports_dir = ledger_dir / "imports"
    imports_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging_file = imports_dir / f"{source_name}_import_{timestamp}.beancount"

    header = f"; Imported via {source_name} on {datetime.now().isoformat()}\n"
    if source_file:
        header += f"; Source: {source_file}\n"
    header += f"; Transaction count: {len(entries)}\n"
    if content_skipped_count > 0:
        header += f"; Skipped (already in ledger): {content_skipped_count}\n"
    header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"

    staging_file.write_text(header + "\n\n".join(entries) + "\n")

    append_to_ledger(ledger_path, entries)

    # Track imported hashes in DB
    if content_hashes and db_conn is not None:
        from istota.money.db import track_csv_transactions_batch
        track_csv_transactions_batch(db_conn, content_hashes, source_file)

    result = {
        "status": "ok",
        "transaction_count": len(entries),
        "content_skipped_count": content_skipped_count,
        "staging_file": str(staging_file),
        "message": f"Imported {len(entries)} transactions to ledger",
    }
    if rules is not None:
        # Absent rather than zero on the map path, so a caller can tell "no
        # engine ran" from "the engine ran and skipped nothing".
        result["rule_skipped_count"] = rule_skipped_count
    return result
