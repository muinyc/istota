"""Transaction operations: category mapping, beancount formatting, ledger writes, config parsing.

Import/sync logic lives in core.importers.* — this module re-exports the public
functions for backward compatibility with CLI, API, and tests.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING


from .dedup import compute_transaction_hash, parse_ledger_transactions
from .ids import new_txn_id
from .models import (
    MonarchConfig,
    MonarchCredentials,
    MonarchProfile,
    MonarchSyncSettings,
    MonarchTagFilters,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    # `importers/__init__` imports this module at module scope, so a runtime
    # import of anything under `.importers` here would be a cycle. Every
    # runtime use below is function-scope for that reason.
    from .importers.base import NormalizedTransaction


# =============================================================================
# Monarch category to beancount account mapping
# =============================================================================

MONARCH_CATEGORY_MAP = {
    # Income
    "Income": "Income:Salary",
    "Paycheck": "Income:Salary",
    "Interest": "Income:Interest",
    "Dividends": "Income:Dividends",
    "Investment Income": "Income:Investment",
    "Refund": "Income:Refunds",
    # Food
    "Groceries": "Expenses:Food:Groceries",
    "Restaurants": "Expenses:Food:Restaurants",
    "Food & Drink": "Expenses:Food:Other",
    "Coffee Shops": "Expenses:Food:Coffee",
    # Transport
    "Gas": "Expenses:Transport:Gas",
    "Parking": "Expenses:Transport:Parking",
    "Auto Insurance": "Expenses:Transport:Insurance",
    "Auto Payment": "Expenses:Transport:CarPayment",
    "Public Transit": "Expenses:Transport:Transit",
    "Rideshare": "Expenses:Transport:Rideshare",
    "Transportation": "Expenses:Transport:Other",
    # Housing
    "Rent": "Expenses:Housing:Rent",
    "Mortgage": "Expenses:Housing:Mortgage",
    "Utilities": "Expenses:Housing:Utilities",
    "Internet": "Expenses:Housing:Internet",
    "Phone": "Expenses:Housing:Phone",
    "Home Improvement": "Expenses:Housing:Improvement",
    "Home Insurance": "Expenses:Housing:Insurance",
    # Shopping
    "Shopping": "Expenses:Shopping",
    "Clothing": "Expenses:Shopping:Clothing",
    "Electronics": "Expenses:Shopping:Electronics",
    "Amazon": "Expenses:Shopping:Amazon",
    # Entertainment
    "Entertainment": "Expenses:Entertainment",
    "Streaming": "Expenses:Entertainment:Streaming",
    "Movies": "Expenses:Entertainment:Movies",
    "Games": "Expenses:Entertainment:Games",
    # Health
    "Health": "Expenses:Health",
    "Doctor": "Expenses:Health:Doctor",
    "Pharmacy": "Expenses:Health:Pharmacy",
    "Health Insurance": "Expenses:Health:Insurance",
    # Travel
    "Travel": "Expenses:Travel",
    "Hotels": "Expenses:Travel:Hotels",
    "Flights": "Expenses:Travel:Flights",
    # Other
    "Education": "Expenses:Education",
    "Books": "Expenses:Education:Books",
    "Subscriptions": "Expenses:Subscriptions",
    "Gifts": "Expenses:Gifts",
    "Charity": "Expenses:Charity",
    "Fees": "Expenses:Fees",
    "Bank Fee": "Expenses:Fees:Bank",
    "ATM Fee": "Expenses:Fees:ATM",
    "Transfer": "Equity:Transfers",
    "Credit Card Payment": "Liabilities:CreditCard",
}


# Beancount's own ACCOUNT_RE is Unicode-aware, so the slug keeps Unicode
# letters and digits; `\w` covers those but also underscore, which it rejects.
_ACCOUNT_COMPONENT_DISALLOWED = re.compile(r"[^\w-]|_")

# A script with no case of its own still needs an uppercase-or-digit initial.
_UNCASED_INITIAL = "X"


def account_component(name: str) -> str:
    """Slug an arbitrary string into a valid beancount account component.

    A component may hold only letters, digits and dashes and must start with an
    uppercase letter or a digit, so a category like "Internet Services
    (Reimbursed)" has to be stripped before it can go in an account name — an
    invalid one is not rejected at import, it lands in the ledger and breaks the
    parser for every later read.

    Deleting a disallowed character rather than replacing it means two names
    differing only in punctuation collapse together — "Food & Drink" and "Food
    Drink" are both `FoodDrink`. That is inherited from the `replace(" ", "")`
    this replaced, and kept deliberately: substituting a dash instead would give
    every multi-word category a new account name and split its balance across
    the old one and the new at the next sync.

    What is not inherited is a shared fallback for a name with nothing to slug.
    A name in an uncased script gets an initial rather than `Unknown`, since
    merging unrelated categories into one account is a wrong number in a report
    rather than a parse error somebody notices.

    Only a *leading* dash is stripped. Beancount's component class allows a
    trailing one, so removing it would rename an account that already worked.
    """
    slug = _ACCOUNT_COMPONENT_DISALLOWED.sub("", name).lstrip("-")
    if not slug:
        return "Unknown"
    if not (slug[0].isupper() or slug[0].isdigit()):
        slug = slug[0].upper() + slug[1:]
    if not (slug[0].isupper() or slug[0].isdigit()):
        slug = _UNCASED_INITIAL + slug
    return slug


def map_monarch_category(category: str) -> str:
    """Map a Monarch category to a beancount account."""
    if category in MONARCH_CATEGORY_MAP:
        return MONARCH_CATEGORY_MAP[category]

    for key, value in MONARCH_CATEGORY_MAP.items():
        if key.lower() == category.lower():
            return value

    return f"Expenses:Uncategorized:{account_component(category)}"


def map_monarch_category_with_config(category: str, config: MonarchConfig) -> str:
    """Map a Monarch category, checking config overrides first."""
    if category in config.categories:
        return config.categories[category]

    for key, value in config.categories.items():
        if key.lower() == category.lower():
            return value

    return map_monarch_category(category)


def map_monarch_account(account_name: str, config: MonarchConfig) -> str:
    """Map a Monarch account name to a beancount account."""
    if account_name in config.accounts:
        return config.accounts[account_name]

    for key, value in config.accounts.items():
        if key.lower() == account_name.lower():
            return value

    return config.sync.default_account


# =============================================================================
# The rules engine on the import path
# =============================================================================


def _normalize_monarch_txn(txn: dict) -> NormalizedTransaction | None:
    """One Monarch API payload as a `NormalizedTransaction`.

    The seam the rules engine matches against, and the shape a Fidelity
    transactions source will mirror. Extracted from the inline `txn.get(...)`
    block in `sync_monarch`, so every default here is that block's and not
    `NormalizedTransaction`'s — **`category` in particular**, which defaults to
    `"Uncategorized"` rather than `""`, because that string reaches both the
    narration and the posting account and changing it would move every
    category-less transaction to a different account.

    Returns `None` for a payload whose date will not parse, which is the
    `continue` this replaced: the row is unbookable and the sync drops it.
    """
    from .importers.base import NormalizedTransaction

    txn_date_str = txn.get("date", "") or ""
    try:
        txn_date = datetime.strptime(txn_date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None

    merchant = txn.get("merchant", {}).get("name", "") or txn.get("name", "Unknown")
    return NormalizedTransaction(
        date=txn_date,
        amount=float(txn.get("amount", 0)),
        payee=merchant,
        category=txn.get("category", {}).get("name", "") or "Uncategorized",
        account_name=txn.get("account", {}).get("displayName", ""),
        notes=txn.get("notes", "") or "",
        tags=[t.get("name", "") for t in txn.get("tags", [])],
        source_id=txn.get("id", ""),
        raw=txn,
    )


def load_import_rules(
    db_path,
    ledger: str,
    source: str,
) -> tuple[list | None, list[int]]:
    """The compiled rules for one import run, and the ids that would not compile.

    One helper for all four loading sites — `sync_all_profiles`' two branches,
    `cli._sync_monarch_ledgers`' two — so the compile and the drop report are
    written once. `db_path` may be `None`, which is a deployment with no money
    DB reachable: the answer is `(None, [])` rather than `([], [])`, since
    those two are opposite instructions to `sync_monarch`. An empty list means
    the store answered and had nothing in scope; `None` means there was no
    store to ask, and today's dict path stands.

    **Call this before the caller's own connection has written anything.**
    `load_rules_for_run` calls `init_db`, which is a write transaction on a
    second connection to the same file, and the sync's `db_conn` holds its
    write lock open until it commits. `config_store._connect` sets no busy
    timeout, so a load issued mid-sync is an immediate `database is locked`
    rather than a wait — see `load_import_rules_for_ledgers`, which is what a
    caller syncing several ledgers in one pass takes for exactly this reason.
    """
    from istota.money import config_store
    from . import rules as rule_engine

    if db_path is None:
        return None, []
    stored = config_store.load_rules_for_run(db_path, ledger, source)
    if stored is None:
        # The store says its table is not authoritative — a migration that did
        # not complete. Passing its seed rows on would run the import against
        # a rule set carrying none of the user's own map while the
        # `MonarchConfig` beside it was still served from the legacy tables.
        return None, []
    return rule_engine.compile_rules_reporting(stored)


def load_import_rules_for_ledgers(db_path, ledger_names, source: str) -> dict:
    """`load_import_rules` per ledger for a **profile** loop, keyed by name.

    A pre-pass, and the ordering is the whole point rather than an
    optimization: a sync loop writes to `db_conn` as it goes and holds that
    write transaction until it commits, so loading a later ledger's rules
    from inside the loop opens a second connection against a lock it cannot
    wait on. Every ledger's rules are read before the first row is written.

    Deduplicated by name, since two profiles can share a ledger and the scope
    is the ledger.

    **A profile whose ledger is empty gets the dict path, never the global
    scope.** This is `config_store._rule_scope`'s refusal reached by a second
    route, and it has to be repeated because this function derives the scope
    from `MonarchProfile.ledger` rather than asking the store. An empty ledger
    resolves to `''`, which the engine reads as "any" — so that profile would
    be scored against the *global* map while its own `MonarchConfig` was
    served from the legacy tables it still lives in. `upsert_monarch_profile`
    refuses to blank a ledger, but `save_monarch` writes one through
    `_upsert_profile_row` unchecked and a stored row predating that guard is
    never revisited, so the shape is reachable rather than hypothetical.

    Only profile loops call this. The three ledger-less callers — a CSV import
    with no ledger name, and the no-profiles sync branch — are asking for the
    global scope on purpose and take `load_import_rules` directly.
    """
    loaded: dict = {}
    for name in ledger_names:
        if name in loaded:
            continue
        loaded[name] = (None, []) if not name else load_import_rules(
            db_path, name, source,
        )
    return loaded


def annotate_rule_drops(result: dict, dropped: list[int]) -> dict:
    """Put a compile drop on an import result, in place.

    A dropped rule is not symmetric with the other refusals in this feature:
    dropping a `posting_account` rule lands a transaction in
    `Expenses:Uncategorized`, where it is visible, but dropping a **`skip`**
    imports a transaction the user excluded on purpose. That widens the run
    rather than narrowing it, so it has to reach whoever reads the result and
    not only the log.

    Absent keys on a clean run, so nothing has to learn a zero.
    """
    if dropped:
        result["rule_drop_count"] = len(dropped)
        result["dropped_rule_ids"] = list(dropped)
    return result


def _resolve_with_rules(txn: dict, rules):
    """`(normalized, resolution)` for one Monarch payload, or `None`.

    Called twice per transaction — once in the pre-filter to answer the skip
    question before dedup, once in the entry loop for the accounts. The dedup
    passes between them carry raw API dicts, so caching would mean keying a
    side table on a payload's identity or on an id that can be empty; a second
    ordered pass over a short list is the cheaper thing to be right about.
    """
    from . import rules as rule_engine

    normalized = _normalize_monarch_txn(txn)
    if normalized is None:
        return None
    return normalized, rule_engine.resolve(normalized, rules)


def _accounts_from_resolution(resolution, category: str, config: MonarchConfig):
    """`(posting, contra)` for one resolution, with the unfilled slots filled.

    The fallbacks are exactly what the two mapping functions end on, minus the
    tiers the rule list already carries: the config's own maps are the
    compatibility view of these same rows, and `MONARCH_CATEGORY_MAP` is
    seeded at priority 900. Consulting either again would apply one tier twice.
    """
    contra = (
        resolution.contra_account
        if resolution.contra_account is not None
        else config.sync.default_account
    )
    posting = (
        resolution.posting_account
        if resolution.posting_account is not None
        else f"Expenses:Uncategorized:{account_component(category)}"
    )
    return posting, contra


def _reconciled_posting_account(
    current_txn: dict,
    rules,
    category: str,
    config: MonarchConfig,
    stored_account: str,
) -> str:
    """What a *previously synced* transaction resolves to under today's rules.

    Two arms answer with `stored_account`, and both are the conservative
    direction on purpose: the caller books a correcting ledger entry whenever
    this differs from what is stored, so an unanswerable question must read as
    "nothing changed" rather than as a change.

    A payload whose date will not parse cannot be resolved at all. And a
    resolution that comes back `skip` has no posting account to offer, so a
    skip rule added later does not reverse a transaction already booked —
    **except on `field='tag'`, and that exception is the inertness contract
    rather than an oversight.** Reversal is what an exclude tag has always
    done, through `still_has_business_tag`, and after Stage 2
    `config_store._load_tag_filters` builds `MonarchTagFilters.exclude` out of
    the `field='tag', action='skip'` rules themselves — so a tag skip rule
    still reaches that path and still reverses, exactly as the config value it
    replaced did. A `payee` or `category` skip has no such predecessor, and
    making one retroactive would turn a rule edit into a rewrite of the user's
    booked history. So two rules differing only in `field` do differ here, and
    the asymmetry is the shape of what each one is compatible with.
    """
    resolved = _resolve_with_rules(current_txn, rules)
    if resolved is None or resolved[1].skip:
        return stored_account
    return _accounts_from_resolution(resolved[1], category, config)[0]


# =============================================================================
# Tag filtering
# =============================================================================


def parse_tags(tags_str: str) -> list[str]:
    """Parse comma-separated tags from Monarch CSV Tags column."""
    if not tags_str or not tags_str.strip():
        return []
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def filter_by_tags(
    tags: list[str],
    include_tags: list[str] | None,
    exclude_tags: list[str] | None,
) -> bool:
    """Check if transaction passes tag filters.

    Returns True if transaction passes filters.
    """
    if include_tags:
        if not any(t in include_tags for t in tags):
            return False

    if exclude_tags:
        if any(t in exclude_tags for t in tags):
            return False

    return True


# =============================================================================
# TOML/Markdown config parsing
# =============================================================================


def parse_monarch_config(
    config_path: Path,
    secrets: dict | None = None,
) -> MonarchConfig:
    """Parse Monarch Money config file (TOML or MONARCH.md) into MonarchConfig.

    Args:
        config_path: Path to monarch.toml or MONARCH.md
        secrets: Optional dict from secrets file. If present, secrets["monarch"]
                 fields override credentials from the config file.
    """
    from istota.money._config_io import read_toml_config
    data = read_toml_config(config_path)

    monarch = data.get("monarch", {})

    # Merge secrets overlay onto credentials.
    secret_creds = (secrets or {}).get("monarch", {})
    credentials = MonarchCredentials(
        session_id=secret_creds.get("session_id") or monarch.get("session_id"),
        csrftoken=secret_creds.get("csrftoken") or monarch.get("csrftoken"),
    )

    sync_data = monarch.get("sync", {})
    sync = MonarchSyncSettings(
        lookback_days=sync_data.get("lookback_days", 30),
        default_account=sync_data.get("default_account", "Assets:Bank:Checking"),
        recategorize_account=sync_data.get("recategorize_account", "Expenses:Personal-Expense"),
    )

    accounts = monarch.get("accounts", {})
    categories = monarch.get("categories", {})

    tags_data = monarch.get("tags", {})
    tags = MonarchTagFilters(
        include=tags_data.get("include", []),
        exclude=tags_data.get("exclude", []),
    )

    # Parse per-ledger profiles
    profiles = []
    for profile_name, profile_data in monarch.get("profiles", {}).items():
        profile_sync_data = profile_data.get("sync", {})
        profile_sync = MonarchSyncSettings(
            lookback_days=profile_sync_data.get("lookback_days", sync.lookback_days),
            default_account=profile_data.get("default_account", profile_sync_data.get("default_account", sync.default_account)),
            recategorize_account=profile_data.get("recategorize_account", profile_sync_data.get("recategorize_account", sync.recategorize_account)),
        )

        profile_tags_data = profile_data.get("tags", {})
        profile_tags = MonarchTagFilters(
            include=profile_tags_data.get("include", []),
            exclude=profile_tags_data.get("exclude", []),
        )

        # Profile accounts/categories inherit from top-level if not set
        profile_accounts = profile_data.get("accounts", None)
        if profile_accounts is None:
            profile_accounts = dict(accounts)
        profile_categories = profile_data.get("categories", None)
        if profile_categories is None:
            profile_categories = dict(categories)

        profiles.append(MonarchProfile(
            name=profile_name,
            ledger=profile_data.get("ledger", profile_name),
            sync=profile_sync,
            accounts=profile_accounts,
            categories=profile_categories,
            tags=profile_tags,
        ))

    return MonarchConfig(
        credentials=credentials,
        sync=sync,
        accounts=accounts,
        categories=categories,
        tags=tags,
        profiles=profiles,
    )


# =============================================================================
# Beancount formatting
# =============================================================================


def format_beancount_transaction(
    txn_date: date,
    payee: str,
    narration: str,
    posting_account: str,
    contra_account: str,
    amount: float,
    currency: str = "USD",
    metadata: dict[str, str] | None = None,
) -> str:
    """Format a single beancount transaction.

    ``metadata`` renders as indented ``key: "value"`` lines between the
    header and the postings (e.g. ``{"id": ...}`` / ``{"monarch-id": ...}``).
    """
    payee = payee.replace('"', '\\"')
    narration = narration.replace('"', '\\"')

    lines = [f'{txn_date.isoformat()} * "{payee}" "{narration}"']

    if metadata:
        for key, value in metadata.items():
            escaped = str(value).replace('"', '\\"')
            lines.append(f'  {key}: "{escaped}"')

    if amount < 0:
        lines.append(f'  {posting_account}  {abs(amount):.2f} {currency}')
        lines.append(f'  {contra_account}')
    else:
        lines.append(f'  {contra_account}  {amount:.2f} {currency}')
        lines.append(f'  {posting_account}')

    return "\n".join(lines)


def format_recategorization_entry(
    txn_date: date,
    merchant: str,
    posted_account: str,
    contra_account: str | None,
    amount: float,
    recategorize_account: str = "Expenses:Personal-Expense",
    currency: str = "USD",
) -> str | None:
    """Format a ledger entry that handles a #business tag being removed in Monarch.

    For income postings, emits a true reversal of the original entry — income
    that was synced as business income is undone entirely. Requires
    `contra_account` from the original entry; returns None when it's missing
    (legacy rows synced before contra_account was tracked) so the caller can
    fall back or surface a warning.

    For expense postings, emits a category swap: the cash already left the
    contra account, only the expense bucket changes from posted_account to
    recategorize_account.
    """
    if posted_account.startswith("Income:"):
        if not contra_account:
            return None
        return format_beancount_transaction(
            txn_date=txn_date,
            payee=merchant,
            narration="Reversal: business tag removed in Monarch",
            posting_account=posted_account,
            contra_account=contra_account,
            amount=-amount,
            metadata={"id": new_txn_id()},
        )

    merchant = merchant.replace('"', '\\"')
    lines = [f'{txn_date.isoformat()} * "{merchant}" "Recategorized: business tag removed in Monarch"']
    lines.append(f'  id: "{new_txn_id()}"')
    lines.append(f'  {recategorize_account}  {abs(amount):.2f} {currency}')
    lines.append(f'  {posted_account}  -{abs(amount):.2f} {currency}')
    return "\n".join(lines)


def format_category_change_entry(
    txn_date: date,
    merchant: str,
    old_account: str,
    new_account: str,
    amount: float,
    currency: str = "USD",
) -> str:
    """Format a ledger entry that moves a transaction from one category to another.

    For expense categories the entry is `DR new / CR old` — the cash leg is
    untouched, only the expense bucket changes. For income categories the signs
    flip: `DR old / CR new` cancels the original income credit and re-credits
    the new income account.
    """
    merchant = merchant.replace('"', '\\"')
    lines = [f'{txn_date.isoformat()} * "{merchant}" "Recategorized in Monarch"']
    lines.append(f'  id: "{new_txn_id()}"')
    if old_account.startswith("Income:") or new_account.startswith("Income:"):
        lines.append(f'  {old_account}  {abs(amount):.2f} {currency}')
        lines.append(f'  {new_account}  -{abs(amount):.2f} {currency}')
    else:
        lines.append(f'  {new_account}  {abs(amount):.2f} {currency}')
        lines.append(f'  {old_account}  -{abs(amount):.2f} {currency}')
    return "\n".join(lines)


# =============================================================================
# Ledger file operations
# =============================================================================


def backup_ledger(ledger_path: Path, max_backups: int = 10) -> Path | None:
    """Create a timestamped backup of the ledger file before modification."""
    if not ledger_path.exists():
        return None

    backups_dir = ledger_path.parent / "backups"
    backups_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"{ledger_path.name}.{timestamp}"

    shutil.copy2(ledger_path, backup_path)

    existing = sorted(backups_dir.glob(f"{ledger_path.name}.*"), reverse=True)
    for old_backup in existing[max_backups:]:
        old_backup.unlink()

    return backup_path


def _append_entries_unlocked(ledger_path: Path, entries: list[str]) -> None:
    """Backup + append entries to the main ledger. Caller must hold the lock.

    Split out so a caller that already holds ``_ledger_lock`` (add_transaction,
    which keeps the lock through a post-write bean-check) can append without
    re-acquiring it — the flock is not reentrant across separate open file
    descriptions, so a nested ``_ledger_lock`` would deadlock.
    """
    if not entries:
        return
    backup_ledger(ledger_path)
    with open(ledger_path, "a") as f:
        for entry in entries:
            f.write(f"\n{entry}\n")


def append_to_ledger(ledger_path: Path, entries: list[str]) -> None:
    """Append beancount entries to the main ledger file with backup.

    Held under the ledger lock so a Monarch sync append can't interleave with
    a concurrent web edit (ISSUE-104). Import is function-local to avoid the
    ``edit`` <-> ``transactions`` module import cycle.
    """
    if not entries:
        return
    from .edit import _ledger_lock

    with _ledger_lock(ledger_path):
        _append_entries_unlocked(ledger_path, entries)


# =============================================================================
# CSV import — delegates to core.importers
# =============================================================================


def parse_monarch_csv(
    file_path: Path,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
) -> list[dict]:
    """Parse a Monarch Money CSV export.

    Backward-compatible wrapper: returns list[dict] (not NormalizedTransaction).
    """
    from .importers.monarch_csv import parse_monarch_csv as _parse

    normalized = _parse(file_path, include_tags, exclude_tags)
    return [
        {
            "date": txn.date,
            "merchant": txn.payee,
            "category": txn.category,
            "account_name": txn.account_name,
            "original_statement": txn.raw.get("original_statement", ""),
            "amount": txn.amount,
            "notes": txn.notes,
            "tags": txn.tags,
            "owner": txn.raw.get("owner", ""),
        }
        for txn in normalized
    ]


def import_csv(
    ledger_path: Path,
    file_path: Path,
    account: str,
    db_conn=None,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    db_path=None,
    ledger_name: str = "",
) -> dict:
    """Import transactions from Monarch Money CSV export.

    Delegates to the modular import pipeline via core.importers.

    ``db_path`` is what turns the rules engine on, and ``ledger_name`` is what
    scopes it — the pipeline needs a ledger *name*, and ``ledger_path`` is a
    file. An empty name is not an error: rules written at ``ledger=''`` are in
    scope for any run, so an unnamed ledger gets the global tier and the seed
    and nothing ledger-specific.

    Without ``db_path`` the shipped ``MONARCH_CATEGORY_MAP`` is still passed as
    the map, which is what a caller with no money DB gets and is the reason the
    constant is a non-goal to retire.
    """
    from .importers import import_transactions
    from .importers.monarch_csv import parse_monarch_csv as _parse

    if not file_path.exists():
        return {"status": "error", "error": f"File not found: {file_path}"}

    try:
        transactions = _parse(file_path, include_tags, exclude_tags)
    except Exception as e:
        return {"status": "error", "error": f"Failed to parse CSV: {e}"}

    if not transactions:
        filter_msg = ""
        if include_tags or exclude_tags:
            filter_msg = " (after applying tag filters)"
        return {"status": "error", "error": f"No valid transactions found in CSV{filter_msg}"}

    rules, dropped = load_import_rules(db_path, ledger_name, "monarch-csv")

    return annotate_rule_drops(
        import_transactions(
            ledger_path=ledger_path,
            transactions=transactions,
            source_name="monarch-csv",
            contra_account=account,
            category_map=MONARCH_CATEGORY_MAP,
            db_conn=db_conn,
            source_file=file_path.name,
            rules=rules,
        ),
        dropped,
    )


# =============================================================================
# Monarch API sync
# =============================================================================


async def fetch_monarch_transactions(
    config: MonarchConfig,
    lookback_days: int,
) -> list[dict]:
    """Fetch transactions from Monarch Money API."""
    from .importers.monarch_api import fetch_monarch_transactions as _fetch
    return await _fetch(config, lookback_days)


async def fetch_transactions_by_ids(
    config: MonarchConfig,
    transaction_ids: list[str],
) -> dict[str, dict]:
    """Fetch specific transactions from Monarch by ID."""
    from .importers.monarch_api import fetch_transactions_by_ids as _fetch
    return await _fetch(config, transaction_ids)


def _ledger_has_posting(ledger_path: Path, synced_txn, expected_account: str) -> bool:
    """Check whether the ledger still contains a transaction matching
    ``synced_txn`` that posts to ``expected_account``.

    Used by the reconciliation loop to detect when the DB tracking record
    is stale relative to the actual ledger state — e.g. because a human
    or the bot edited the original posting directly. Without this check
    the next sync would generate a phantom category-change entry that
    "corrects" something already correct, double-counting the change.

    Match is by (date, payee). When multiple transactions share the same
    date and payee, we return True if any of them still posts to the
    expected account — preferring a false positive (emit the change) over
    a false negative (silently swallow a legitimate change).

    Conservative fallback: if the ledger file is missing or the synced
    record is missing date/merchant, return True so the original behavior
    holds and we don't suppress legitimate changes.
    """
    if not ledger_path.exists():
        return True
    if not synced_txn.txn_date or not synced_txn.merchant:
        return True

    try:
        text = ledger_path.read_text()
    except OSError:
        return True

    target_date = synced_txn.txn_date
    target_merchant = synced_txn.merchant.strip()

    # Beancount transaction header: YYYY-MM-DD * "payee" "narration"
    header_re = re.compile(r'^(\d{4}-\d{2}-\d{2})\s+[*!]\s+"([^"]*)"', re.MULTILINE)

    for m in header_re.finditer(text):
        if m.group(1) != target_date:
            continue
        if m.group(2).strip() != target_merchant:
            continue
        # Posting block runs until the next blank line or next transaction.
        body = text[m.end():].split("\n\n", 1)[0]
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(";"):
                continue
            # Posting lines start with the account name (followed by amount).
            if stripped.startswith(expected_account + " ") or stripped == expected_account:
                return True

    return False


def _load_ledger_monarch_index(ledger_path: Path) -> dict[str, dict]:
    """Map ``monarch-id`` → ``{edited, posted_account}`` for ledger entries.

    Used by the sync reconciler to (a) skip auto-recategorizing entries the
    user edited by hand — those carrying an ``edited:`` metadata line — and
    (b) read the entry's current category account so the DB can be reconciled
    to the manual choice. Matching by the stamped ``monarch-id`` is exact,
    unlike the ``(date, payee)`` text scan in ``_ledger_has_posting``.
    """
    if not ledger_path.exists():
        return {}
    try:
        from beancount.core.data import Transaction
        from beancount.loader import load_file

        entries, _errors, _ = load_file(str(ledger_path))
    except Exception:
        return {}

    index: dict[str, dict] = {}
    for e in entries:
        if not isinstance(e, Transaction):
            continue
        mid = e.meta.get("monarch-id")
        if not mid:
            continue
        posted = None
        for p in e.postings:
            if p.account.startswith(("Expenses:", "Income:")):
                posted = p.account
                break
        index[mid] = {"edited": bool(e.meta.get("edited")), "posted_account": posted}
    return index


def sync_monarch(
    ledger_path: Path,
    config: MonarchConfig,
    db_conn=None,
    dry_run: bool = False,
    transactions: list[dict] | None = None,
    profile: str = "",
    rules=None,
) -> dict:
    """Sync transactions from Monarch Money API and reconcile tag changes.

    Args:
        ledger_path: Path to ledger file
        config: Monarch configuration
        db_conn: Optional database connection for dedup tracking
        dry_run: If True, preview without writing files or tracking
        transactions: Pre-fetched transactions list. If None, fetches from API.
        profile: Profile name for DB tracking (empty string for no profile).
        rules: Compiled transaction rules in evaluation order, or ``None``.

    ``rules=None`` is the compatibility contract, and it is not the same as
    ``rules=[]``. ``None`` means *no rule store was consulted*, and the
    function then takes the dict path exactly as it did before this feature
    existed — ``map_monarch_account`` and ``map_monarch_category_with_config``,
    the config's own exclude tags in the pre-filter, no ``rule_ids`` written.
    Every direct caller and every test that predates the rules engine passes
    nothing and gets byte-identical ledger entries. ``[]`` means the store
    *was* asked and had nothing in scope, so the dict tiers no longer apply and
    both slots fall back — which on a migrated deployment cannot happen, since
    the seed alone fills the list.
    """
    import asyncio

    # Fetch transactions from API if not pre-fetched
    if transactions is None:
        try:
            transactions = asyncio.run(fetch_monarch_transactions(
                config, config.sync.lookback_days,
            ))
        except Exception as e:
            return {"status": "error", "error": f"Failed to fetch transactions: {e}"}

    # Build lookup of all fetched transactions by ID for reconciliation
    all_txn_by_id = {txn.get("id"): txn for txn in transactions if txn.get("id")}

    # Filter out pending transactions and by tags if configured.
    #
    # ISSUE-160: Monarch doesn't mutate a pending row in place when it
    # settles — it drops the pending transaction and creates a fresh one
    # with a new id and the final (post-tip) amount. Since our dedup keys
    # on monarch-id and a content hash of date+amount+merchant, the pending
    # and settled twins both look new, and the pending copy is left stale in
    # the ledger once its settled sibling lands in a later sync. Skipping
    # pending rows at the source means the ghost is never booked; the settled
    # row becomes the only entry that ever lands (1–3 day settle lag is
    # acceptable for a reconciliation-first ledger).
    #
    # The include gate runs ahead of the rules pass and stays a config read
    # either way: include semantics are a gate over the whole set — *if any
    # include tags are configured, the row must carry one* — which no
    # per-row rule can express. Exclusion is the half that moves: with rules
    # in hand it is a `skip` rule, counted, rather than a silent drop here.
    pending_skipped_count = 0
    rule_skipped_count = 0
    filtered_transactions = []
    for txn in transactions:
        if txn.get("pending"):
            pending_skipped_count += 1
            continue
        txn_tags = [t.get("name", "") for t in txn.get("tags", [])]
        if not filter_by_tags(
            txn_tags,
            config.tags.include if config.tags.include else None,
            None if rules is not None else (
                config.tags.exclude if config.tags.exclude else None
            ),
        ):
            continue
        if rules is not None:
            # Before dedup, so a skipped transaction is neither booked nor
            # tracked — and so removing a skip rule later re-imports it.
            resolved = _resolve_with_rules(txn, rules)
            if resolved is not None and resolved[1].skip:
                rule_skipped_count += 1
                continue
        filtered_transactions.append(txn)

    # Deduplicate against previously synced transactions and ledger content
    new_transactions = []
    skipped_count = 0
    content_skipped_count = 0

    ledger_hashes = parse_ledger_transactions(ledger_path)

    if db_conn is not None:
        from istota.money.db import (
            is_content_hash_synced,
            is_monarch_transaction_synced,
            get_active_monarch_synced_transactions,
        )

        for txn in filtered_transactions:
            txn_id = txn.get("id", "")
            if txn_id and is_monarch_transaction_synced(db_conn, txn_id, profile=profile):
                skipped_count += 1
                continue

            merchant = txn.get("merchant", {}).get("name", "") or txn.get("name", "Unknown")
            amount = float(txn.get("amount", 0))
            txn_date_str = txn.get("date", "")[:10]
            content_hash = compute_transaction_hash(txn_date_str, abs(amount), merchant)

            if content_hash in ledger_hashes or is_content_hash_synced(db_conn, content_hash):
                content_skipped_count += 1
                continue

            new_transactions.append(txn)
    else:
        for txn in filtered_transactions:
            merchant = txn.get("merchant", {}).get("name", "") or txn.get("name", "Unknown")
            amount = float(txn.get("amount", 0))
            txn_date_str = txn.get("date", "")[:10]
            content_hash = compute_transaction_hash(txn_date_str, abs(amount), merchant)

            if content_hash in ledger_hashes:
                content_skipped_count += 1
            else:
                new_transactions.append(txn)

    # Build beancount entries for new transactions
    entries = []
    synced_data = []
    # ISSUE-083: what this run actually booked, in ledger sign convention
    # (positive = money in). The invoice matcher reads this rather than
    # re-parsing the staging file to find the new credits.
    imported: list[dict] = []

    for txn in new_transactions:
        txn_date_str = txn.get("date", "")
        try:
            txn_date = datetime.strptime(txn_date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue

        merchant = txn.get("merchant", {}).get("name", "") or txn.get("name", "Unknown")
        category = txn.get("category", {}).get("name", "") or "Uncategorized"
        account_name = txn.get("account", {}).get("displayName", "")
        amount = float(txn.get("amount", 0))
        notes = txn.get("notes", "") or ""
        txn_id = txn.get("id", "")
        txn_tags = [t.get("name", "") for t in txn.get("tags", [])]

        rule_ids = None
        if rules is None:
            contra_account = map_monarch_account(account_name, config)
            posting_account = map_monarch_category_with_config(category, config)
        else:
            resolution = _resolve_with_rules(txn, rules)[1]
            posting_account, contra_account = _accounts_from_resolution(
                resolution, category, config,
            )
            rule_ids = json.dumps([hit.rule_id for hit in resolution.hits])

        entry_metadata = {"id": new_txn_id()}
        if txn_id:
            entry_metadata["monarch-id"] = txn_id

        entry = format_beancount_transaction(
            txn_date=txn_date,
            payee=merchant,
            narration=notes or category,
            posting_account=posting_account,
            contra_account=contra_account,
            amount=amount,
            metadata=entry_metadata,
        )
        entries.append(entry)
        imported.append({
            "date": txn_date.isoformat(),
            "amount": amount,
            "payee": merchant,
        })

        if txn_id:
            synced_data.append({
                "id": txn_id,
                "tags_json": json.dumps(txn_tags),
                "amount": amount,
                "merchant": merchant,
                "posted_account": posting_account,
                "contra_account": contra_account,
                "txn_date": txn_date.isoformat(),
                "content_hash": compute_transaction_hash(txn_date.isoformat(), abs(amount), merchant),
                # What the mapping consumed, so the coverage surface needn't
                # read a category back out of a slugged account name.
                # `rule_ids` stays NULL where no engine ran — `None` and `[]`
                # are different facts, and the card renders them differently.
                "src_category": category,
                "src_account": account_name,
                "src_source": "monarch-api",
                "rule_ids": rule_ids,
            })

    # Reconciliation: check for tag/category changes on previously synced transactions
    recategorized_entries = []
    recategorized_ids = []
    recat_skipped_legacy: list[str] = []
    category_change_entries = []
    category_change_updates = []
    phantom_skipped: list[str] = []  # ISSUE-071: stale DB tracking, ledger already updated
    edited_respected: list[str] = []  # entries the user edited by hand — left alone

    if db_conn is not None:
        active_synced = get_active_monarch_synced_transactions(db_conn, profile=profile)

        # Index ledger entries by monarch-id so we can honor manual edits.
        ledger_monarch_index = (
            _load_ledger_monarch_index(ledger_path) if active_synced else {}
        )

        if active_synced:
            for synced_txn in active_synced:
                current_txn = all_txn_by_id.get(synced_txn.monarch_transaction_id)
                if current_txn is None:
                    continue

                current_tags = [t.get("name", "") for t in current_txn.get("tags", [])]
                still_has_business_tag = filter_by_tags(
                    current_tags,
                    config.tags.include if config.tags.include else None,
                    config.tags.exclude if config.tags.exclude else None,
                )

                if not still_has_business_tag:
                    if (
                        synced_txn.amount is not None
                        and synced_txn.posted_account
                        and synced_txn.merchant
                        and synced_txn.txn_date
                    ):
                        recat_entry = format_recategorization_entry(
                            txn_date=date.today(),
                            merchant=synced_txn.merchant,
                            posted_account=synced_txn.posted_account,
                            contra_account=synced_txn.contra_account,
                            amount=synced_txn.amount,
                            recategorize_account=config.sync.recategorize_account,
                        )
                        if recat_entry is None:
                            recat_skipped_legacy.append(synced_txn.monarch_transaction_id)
                        else:
                            recategorized_entries.append(recat_entry)
                            recategorized_ids.append(synced_txn.monarch_transaction_id)
                    continue

                if (
                    synced_txn.amount is not None
                    and synced_txn.posted_account
                    and synced_txn.merchant
                    and synced_txn.txn_date
                ):
                    current_category = current_txn.get("category", {}).get("name", "") or "Uncategorized"
                    if rules is None:
                        new_posted_account = map_monarch_category_with_config(
                            current_category, config,
                        )
                    else:
                        # The same answer the ingest loop would give, or the
                        # stored account. Recomputing through the dict path
                        # while the ingest ran on rules makes the two disagree
                        # on any rule the compatibility view cannot express —
                        # a `contains`, a `payee` match — and the disagreement
                        # is not silent: it books a category-change entry on
                        # every sync, for ever, double-counting the move.
                        new_posted_account = _reconciled_posting_account(
                            current_txn, rules, current_category, config,
                            synced_txn.posted_account,
                        )

                    if new_posted_account != synced_txn.posted_account:
                        # Respect manual edits: a ledger entry carrying an
                        # ``edited:`` marker is hands-off. Don't fight the
                        # user's category choice — skip the correction and
                        # reconcile the DB to the entry's actual category so we
                        # don't re-detect a "change" on every future sync.
                        ledger_entry = ledger_monarch_index.get(
                            synced_txn.monarch_transaction_id
                        )
                        if ledger_entry and ledger_entry["edited"]:
                            edited_respected.append(synced_txn.monarch_transaction_id)
                            actual = ledger_entry["posted_account"]
                            if actual and actual != synced_txn.posted_account:
                                category_change_updates.append({
                                    "monarch_transaction_id": synced_txn.monarch_transaction_id,
                                    "posted_account": actual,
                                })
                            continue

                        # ISSUE-071: the DB tracking record may be stale if a
                        # human or the bot edited the original posting in the
                        # ledger directly. If the ledger no longer contains
                        # the OLD account, the change was already made
                        # out-of-band — skip the entry to avoid double-counting,
                        # but still update the DB so future syncs see reality.
                        if not _ledger_has_posting(
                            ledger_path, synced_txn, synced_txn.posted_account,
                        ):
                            phantom_skipped.append(synced_txn.monarch_transaction_id)
                            category_change_updates.append({
                                "monarch_transaction_id": synced_txn.monarch_transaction_id,
                                "posted_account": new_posted_account,
                            })
                            continue

                        cat_entry = format_category_change_entry(
                            txn_date=date.today(),
                            merchant=synced_txn.merchant,
                            old_account=synced_txn.posted_account,
                            new_account=new_posted_account,
                            amount=synced_txn.amount,
                        )
                        category_change_entries.append(cat_entry)
                        category_change_updates.append({
                            "monarch_transaction_id": synced_txn.monarch_transaction_id,
                            "posted_account": new_posted_account,
                        })

    # Prepare result
    ledger_dir = ledger_path.parent
    imports_dir = ledger_dir / "imports"
    imports_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    result = {
        "status": "ok",
        "transaction_count": len(entries),
        "skipped_count": skipped_count,
        "content_skipped_count": content_skipped_count,
        "pending_skipped_count": pending_skipped_count,
        "recategorized_count": len(recategorized_entries),
        "recat_skipped_legacy_count": len(recat_skipped_legacy),
        "category_changed_count": len(category_change_entries),
        "edited_respected_count": len(edited_respected),
        "dry_run": dry_run,
        "imported": imported,
    }
    if rules is not None:
        # Absent rather than zero on the dict path, so a caller can tell "no
        # engine ran" from "the engine ran and skipped nothing".
        result["rule_skipped_count"] = rule_skipped_count
    if edited_respected:
        result["edited_respected_ids"] = edited_respected
    if recat_skipped_legacy:
        result["recat_skipped_legacy_ids"] = recat_skipped_legacy
    if phantom_skipped:
        result["phantom_change_skipped_count"] = len(phantom_skipped)
        result["phantom_change_skipped_ids"] = phantom_skipped

    if dry_run:
        result["message"] = f"Would import {len(entries)} transactions"
        if entries:
            result["sample_entries"] = entries[:3]
        if recategorized_entries:
            result["sample_recategorizations"] = recategorized_entries[:3]
        if category_change_entries:
            result["sample_category_changes"] = category_change_entries[:3]
        return result

    # Write new transactions to staging file
    if entries:
        staging_file = imports_dir / f"monarch_sync_{timestamp}.beancount"
        header = f"; Synced from Monarch Money API on {datetime.now().isoformat()}\n"
        header += f"; Lookback days: {config.sync.lookback_days}\n"
        header += f"; Transaction count: {len(entries)}\n"
        if skipped_count > 0:
            header += f"; Skipped (already synced): {skipped_count}\n"
        if content_skipped_count > 0:
            header += f"; Skipped (already in ledger): {content_skipped_count}\n"
        header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"
        staging_file.write_text(header + "\n\n".join(entries) + "\n")
        result["staging_file"] = str(staging_file)

    # Write recategorizations to separate staging file
    if recategorized_entries:
        recat_file = imports_dir / f"monarch_recategorize_{timestamp}.beancount"
        header = f"; Recategorizations from Monarch Money on {datetime.now().isoformat()}\n"
        header += "; These transactions had their business tag removed in Monarch\n"
        header += f"; Recategorization count: {len(recategorized_entries)}\n"
        header += f"; Target account: {config.sync.recategorize_account}\n"
        header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"
        recat_file.write_text(header + "\n\n".join(recategorized_entries) + "\n")
        result["recategorize_file"] = str(recat_file)

    # Write category changes to separate staging file
    if category_change_entries:
        cat_change_file = imports_dir / f"monarch_category_change_{timestamp}.beancount"
        header = f"; Category changes from Monarch Money on {datetime.now().isoformat()}\n"
        header += "; These transactions were recategorized in Monarch\n"
        header += f"; Category change count: {len(category_change_entries)}\n"
        header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"
        cat_change_file.write_text(header + "\n\n".join(category_change_entries) + "\n")
        result["category_change_file"] = str(cat_change_file)

    # Append to main ledger
    append_to_ledger(ledger_path, entries + recategorized_entries + category_change_entries)

    # Track in DB
    if db_conn is not None:
        from istota.money.db import (
            track_monarch_transactions_batch,
            mark_monarch_transaction_recategorized,
            update_monarch_transaction_posted_account,
        )
        if synced_data:
            track_monarch_transactions_batch(db_conn, synced_data, profile=profile)
        for txn_id in recategorized_ids:
            mark_monarch_transaction_recategorized(db_conn, txn_id)
        for update in category_change_updates:
            update_monarch_transaction_posted_account(
                db_conn,
                update["monarch_transaction_id"],
                update["posted_account"],
            )

    # Build message
    messages = []
    if entries:
        messages.append(f"Synced {len(entries)} new transactions")
    if recategorized_entries:
        messages.append(f"Created {len(recategorized_entries)} recategorization entries")
    if recat_skipped_legacy:
        messages.append(
            f"Skipped {len(recat_skipped_legacy)} income recat(s) — legacy rows "
            "missing contra_account; reverse manually"
        )
    if category_change_entries:
        messages.append(f"Updated {len(category_change_entries)} categories")
    if not entries and not recategorized_entries and not category_change_entries:
        messages.append("No changes")
    if pending_skipped_count:
        messages.append(
            f"Skipped {pending_skipped_count} pending (not yet settled)"
        )

    result["message"] = ". ".join(messages)
    return result


def sync_all_profiles(
    config: MonarchConfig,
    ledgers: list[dict],
    db_conn=None,
    dry_run: bool = False,
    db_path=None,
) -> dict:
    """Sync Monarch transactions across all configured profiles.

    If no profiles are defined, falls back to syncing with the first ledger
    using the flat config (backward compatible).

    Fetches transactions from Monarch once and passes them to each profile's sync.

    ``db_path`` is what turns the rules engine on. Rules are scoped by ledger,
    so they are loaded once per profile — not once per run — and both branches
    below load their own: the no-profiles branch is the only way a deployment
    without profiles syncs at all, and dropping it would leave that shape on
    the dict path for good. ``None`` leaves every branch on the dict path,
    which is what a direct caller and every pre-existing test get.
    """
    import asyncio

    if not config.profiles:
        # Backward compatible: no profiles, sync to default ledger
        if not ledgers:
            return {"status": "error", "error": "No ledgers configured"}
        rules, dropped = load_import_rules(
            db_path, ledgers[0].get("name", ""), "monarch-api",
        )
        return annotate_rule_drops(
            sync_monarch(
                ledgers[0]["path"], config, db_conn=db_conn, dry_run=dry_run,
                rules=rules,
            ),
            dropped,
        )

    # Fetch transactions once for all profiles
    lookback = max(p.sync.lookback_days for p in config.profiles)
    try:
        all_transactions = asyncio.run(fetch_monarch_transactions(config, lookback))
    except Exception as e:
        return {"status": "error", "error": f"Failed to fetch transactions: {e}"}

    # Build ledger lookup
    ledger_by_name = {entry["name"].lower(): entry["path"] for entry in ledgers}

    # Ahead of the loop, not inside it: the loop writes through `db_conn` and
    # holds that write transaction, so a load from inside it opens a second
    # connection against a lock it cannot wait on.
    rules_by_ledger = load_import_rules_for_ledgers(
        db_path, [p.ledger for p in config.profiles], "monarch-api",
    )

    profile_results = []
    for profile in config.profiles:
        ledger_path = ledger_by_name.get(profile.ledger.lower())
        if ledger_path is None:
            profile_results.append({
                "name": profile.name,
                "ledger": profile.ledger,
                "status": "error",
                "error": f"Ledger '{profile.ledger}' not found",
            })
            continue

        # Build a profile-scoped config with the profile's settings
        # but sharing credentials from the top-level config
        profile_config = MonarchConfig(
            credentials=config.credentials,
            sync=profile.sync,
            accounts=profile.accounts,
            categories=profile.categories,
            tags=profile.tags,
        )

        rules, dropped = rules_by_ledger[profile.ledger]
        result = annotate_rule_drops(
            sync_monarch(
                ledger_path, profile_config,
                db_conn=db_conn, dry_run=dry_run,
                transactions=all_transactions,
                profile=profile.name,
                rules=rules,
            ),
            dropped,
        )
        result["name"] = profile.name
        result["ledger"] = profile.ledger
        profile_results.append(result)

    return {"status": "ok", "profiles": profile_results}


# =============================================================================
# Add transaction
# =============================================================================


def add_transaction(
    ledger_path: Path,
    txn_date: date,
    payee: str,
    narration: str,
    debit: str,
    credit: str,
    amount: float,
    currency: str = "USD",
) -> dict:
    """Add a transaction to the ledger."""
    if amount <= 0:
        return {"status": "error", "error": "Amount must be positive"}

    payee_escaped = payee.replace('"', '\\"')
    narration_escaped = narration.replace('"', '\\"')

    txn = f'{txn_date} * "{payee_escaped}" "{narration_escaped}"\n'
    txn += f'  id: "{new_txn_id()}"\n'
    txn += f'  {debit}  {amount:.2f} {currency}\n'
    txn += f'  {credit}\n'

    from .edit import _ledger_lock
    from .ledger import run_bean_check

    # Append to the main ledger — the same file sync_monarch / import_csv write
    # to and the same file bean-check validates. Writing to a transactions/
    # subdir that no ledger include()s let bean-check pass vacuously while the
    # entry stayed invisible to every query and balance (ISSUE-158).
    # Held under the lock through the post-write bean-check so the validation
    # sees a tree no concurrent writer is mutating (ISSUE-104). Uses the
    # unlocked append helper because the flock is not reentrant.
    with _ledger_lock(ledger_path):
        _append_entries_unlocked(ledger_path, [txn])
        success, errors = run_bean_check(ledger_path)
    if not success:
        return {
            "status": "error",
            "error": "Transaction added but ledger validation failed",
            "validation_errors": errors[:5],
            "file": str(ledger_path),
        }

    return {
        "status": "ok",
        "date": txn_date.isoformat(),
        "payee": payee,
        "amount": amount,
        "currency": currency,
        "debit": debit,
        "credit": credit,
        "file": str(ledger_path),
    }
