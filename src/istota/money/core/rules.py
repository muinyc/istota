"""Transaction rules: one ordered pass that decides what an import posts to.

A rule says *in this scope, when this field matches this value, do this*. One
transaction is resolved by a single ordered pass over the rules in scope, with
three action slots — `skip`, `posting_account`, `contra_account`. A matching
rule fills its slot only if that slot is still empty, so first match wins **per
slot** and one transaction can take its posting account from one rule and its
contra account from another. A `skip` match ends the pass.

Slots left empty are the caller's problem, deliberately: `resolve` returns
`None` for an unfilled slot rather than inventing the fallback, because the two
import paths have different ones (`Expenses:Uncategorized:{slug}` for the API
sync, a per-file contra account for a CSV import).

Three properties are decisions rather than consequences.

**`resolve` never raises.** It runs per transaction on the sync path, where a
rule-engine exception would fail an import that is otherwise fine. Everything
that can raise happens at write time or at load time instead, and `matches`
answers False for anything it cannot make sense of: a disabled rule, an empty
`match_value`, a `field` or `match_kind` outside its enum. Those are all shapes
the API refuses, so they arrive only from a hand-edited database, and there the
safe answer is that the rule matches nothing. The opposite reading is what
makes them dangerous: an empty `contains` needle matches every transaction, so
reading it literally would re-route a whole import on one bad row.

**There is no `regex` kind, and its absence is load-bearing** (ISSUE-429). It
was specified, and dropped once measurement showed the bound the spec relied on
was not a bound: `(a+)+$` is six characters, so it passed the 200-character
pattern cap unchanged, and matching it against n `a`s followed by a `b` doubles
per character — 1.26s at n=24, about 25 hours at n=40, against a subject cap of
512. A stored pattern would wedge the sync worker on every run, and `resolve`'s
"never raises" would be satisfied by never returning. Re-adding the kind means
a linear-time engine, not a bound: `MAX_SUBJECT_CHARS` cannot be cut far enough
to help, since at n=64 the same pattern is 2^40 times worse than at n=24.
`exact`, `iexact` and `contains` cover every migrated rule and the whole seeded
map, so nothing existing needs it.

**Every subject is truncated to `MAX_SUBJECT_CHARS` before matching**, which
survives the kind that motivated it because it is still right: a category or
payee longer than that is pathological, and the truncation is safe in the only
direction that matters. It can make a `contains` rule stop matching and can
never make one start, and it cannot affect `exact` or `iexact` at all, since a
subject past the cap is longer than any `match_value` the 200-character cap
admits.

stdlib-only leaf at import time. `validate_rule_fields` reaches
`config_store._check_map_account` through a function-scope import, so the one
beancount account check in the codebase stays the one this module uses without
the import graph growing a module-scope edge into the store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .importers.base import NormalizedTransaction

logger = logging.getLogger(__name__)


FIELDS = ("category", "account", "payee", "notes", "tag")
MATCH_KINDS = ("exact", "iexact", "contains")
ACTIONS = ("posting_account", "contra_account", "skip")
# Provenance, and a rendered badge. `''` is the column default, so a row
# written before a caller had an opinion still validates.
ORIGINS = ("", "seed", "migrated", "user")

MAX_MATCH_VALUE_CHARS = 200
MAX_SUBJECT_CHARS = 512

MIN_PRIORITY = 0
MAX_PRIORITY = 9999
DEFAULT_PRIORITY = 100

# Which NormalizedTransaction attribute each scalar `field` reads. `tag` is
# absent because it matches against a list rather than a single subject.
_SUBJECT_ATTRS = {
    "category": "category",
    "account": "account_name",
    "payee": "payee",
    "notes": "notes",
}

# The columns a rule is written from. `id`, `created_at` and `updated_at` are
# the store's, never a caller's.
RULE_FIELDS = (
    "ledger",
    "source",
    "field",
    "match_kind",
    "match_value",
    "action",
    "target",
    "priority",
    "enabled",
    "origin",
    "note",
)

_REQUIRED_FIELDS = ("field", "match_value", "action")

_FIELD_DEFAULTS: dict[str, Any] = {
    "ledger": "",
    "source": "",
    "match_kind": "iexact",
    "target": "",
    "priority": DEFAULT_PRIORITY,
    "enabled": True,
    "origin": "",
    "note": "",
}


@dataclass(frozen=True)
class Rule:
    """One stored rule, exactly as the table holds it."""

    id: int
    ledger: str
    source: str
    field: str
    match_kind: str
    match_value: str
    action: str
    target: str
    priority: int
    enabled: bool
    origin: str
    note: str


@dataclass(frozen=True)
class CompiledRule:
    """A rule that has been through `compile_rule`.

    It wraps rather than adds, since with `regex` gone there is nothing left to
    precompute. It stays because it is the seam: `compile_rule` is where a row
    whose `match_kind` this release does not know is refused, so a list handed
    to `resolve` holds only rules `matches` can act on, and it is where a
    compiled form would go if a kind that needs one is ever added back.
    """

    rule: Rule

    @property
    def id(self) -> int:
        return self.rule.id

    @property
    def priority(self) -> int:
        return self.rule.priority


@dataclass
class RuleHit:
    """A rule that filled a slot. Never a rule that matched and was shadowed."""

    rule_id: int
    action: str
    target: str


@dataclass
class Resolution:
    skip: bool = False
    posting_account: str | None = None
    contra_account: str | None = None
    hits: list[RuleHit] = dataclass_field(default_factory=list)
    considered: int = 0


# =============================================================================
# Compilation
# =============================================================================


def compile_rule(rule: Rule) -> CompiledRule:
    """Prepare one rule for matching. Raises `ValueError` on a kind it cannot.

    The only way to reach the raise is a row this release has no code for — a
    `regex` row left by a hand edit or by a rollback from a release that has
    the kind back. `validate_rule_fields` refuses the same value at the write
    boundary, so nothing the API stores lands here.
    """
    if rule.match_kind not in MATCH_KINDS:
        raise ValueError(
            f"unknown match_kind {rule.match_kind!r}: expected one of "
            f"{', '.join(MATCH_KINDS)}",
        )
    return CompiledRule(rule=rule)


def compile_rules(rules: Iterable[Rule]) -> list[CompiledRule]:
    """Compile a list, dropping any rule `compile_rule` refuses.

    Order is preserved. A refused row can only come from a hand-edited database
    or a rollback — the API validates at write time — and one of them must not
    fail a sync, so it is logged by id and skipped. The reason goes to DEBUG: a
    warning reaches the operator's log channel, and it would otherwise carry
    the row's `match_value`, which is the user's own financial data.

    Dropping is not uniformly the safe direction, which is worth saying because
    every other refusal in this module is. A dropped `posting_account` rule
    costs a transaction its account and it lands in `Expenses:Uncategorized`,
    where it is visible. A dropped **`skip`** rule imports a transaction the
    user excluded on purpose, which is a wrong number rather than a missing
    one. Dropping still beats failing the whole sync, but a caller that can
    report it on the import result should.
    """
    compiled: list[CompiledRule] = []
    for rule in rules:
        try:
            compiled.append(compile_rule(rule))
        except ValueError as exc:
            logger.warning("transaction rule %s was skipped as unusable", rule.id)
            logger.debug("transaction rule %s: %s", rule.id, exc)
    return compiled


# =============================================================================
# Scope and order
# =============================================================================


def sort_key(rule: Rule | CompiledRule) -> tuple[int, int]:
    """The whole ordering story: an explicit priority, then the id."""
    return (rule.priority, rule.id)


def in_scope(rule: Rule, ledger: str, source: str) -> bool:
    """Whether a rule applies to a run. `''` on either column means "any"."""
    if rule.ledger and rule.ledger != ledger:
        return False
    if rule.source and rule.source != source:
        return False
    return True


def rules_in_scope(
    rules: Iterable[CompiledRule],
    ledger: str,
    source: str,
) -> list[CompiledRule]:
    """The enabled rules for one run, in evaluation order.

    Disabled rules are dropped here as well as at load time: the editor is the
    one surface that wants them, and it asks the store rather than this module.
    """
    scoped = [c for c in rules if c.rule.enabled and in_scope(c.rule, ledger, source)]
    scoped.sort(key=sort_key)
    return scoped


# =============================================================================
# Matching
# =============================================================================


def _subject_matches(compiled: CompiledRule, subject: Any) -> bool:
    """Match one subject. Takes `Any` because a row can hand it anything.

    A `notes` that came back JSON `null`, or a SQLite `NULL`, is not a string,
    and `resolve` runs on the sync path where a `TypeError` would fail an
    import that is otherwise fine.

    Truncation only ever removes a match. It can make a `contains` rule stop
    matching, and it cannot reach `exact` or `iexact` on any row the write
    boundary admits, since a subject past the cap is 512 characters and
    `match_value` stops at 200, so the two are unequal cut or uncut. That last
    clause is carried by `validate_rule_fields`, not by anything here: a
    hand-edited row whose `match_value` is the first 512 characters of a longer
    subject does match, which is the one direction the cut can add.
    """
    if not isinstance(subject, str):
        return False
    subject = subject[:MAX_SUBJECT_CHARS]
    kind = compiled.rule.match_kind
    value = compiled.rule.match_value
    if kind == "exact":
        return subject == value
    if kind == "iexact":
        return subject.lower() == value.lower()
    if kind == "contains":
        return value.lower() in subject.lower()
    return False


def matches(rule: CompiledRule, txn: NormalizedTransaction) -> bool:
    """Whether one rule matches one transaction. Never raises.

    **An empty subject matches nothing**, which is derived rather than
    enforced: `match_value` is required non-empty, and an empty subject is
    unequal to a non-empty value under `exact` and `iexact` and does not
    contain one. The guard that used to say so explicitly is gone with the
    kind that needed it — `^$` was the one thing that could match an empty
    subject. What is enforced, below, is the other half of that reasoning: an
    empty `match_value` is refused here, because `"" in subject` is True and a
    single such row would re-route every transaction in an import.

    Case folding is `.lower()`, not `.casefold()`, because that is what
    `map_monarch_category_with_config` uses and this has to answer the same
    account for the same input. So `STRASSE` does not match `Straße` here, and
    changing it would be a deliberate divergence from the mapping this
    replaces rather than a tidy-up. Nothing is normalized either, so NFC
    `Café` does not match NFD `Café` — also inherited.
    """
    stored = rule.rule
    if not stored.enabled or not stored.match_value:
        return False
    if stored.field == "tag":
        # A list, not any iterable: a bare string is a plausible shape off a
        # database column, and iterating it matches a `contains` rule against
        # single characters.
        tags = getattr(txn, "tags", None)
        if not isinstance(tags, (list, tuple)):
            return False
        return any(_subject_matches(rule, tag) for tag in tags)
    attr = _SUBJECT_ATTRS.get(stored.field)
    if attr is None:
        return False
    return _subject_matches(rule, getattr(txn, attr, ""))


# =============================================================================
# Resolution
# =============================================================================


def resolve(
    txn: NormalizedTransaction,
    rules: Sequence[CompiledRule],
) -> Resolution:
    """Run one ordered pass and report what filled each slot. Never raises.

    `rules` is evaluated in the order given — `rules_in_scope` is what
    establishes it — and the pass does not stop when both slots are full, so
    `considered` is the whole list unless a `skip` ended it early. That costs a
    few comparisons and is what lets a caller ask, afterwards, which rules
    matched into a slot somebody else had already filled.
    """
    resolution = Resolution()
    for compiled in rules:
        resolution.considered += 1
        if not matches(compiled, txn):
            continue
        stored = compiled.rule
        if stored.action == "skip":
            # The slots and the hits go together. A hit is a rule that filled a
            # slot, nothing is posted for a skipped transaction, and a trace
            # naming a posting account beside `skip=True` reads as though one
            # was. What a rule would have done is recoverable by re-running
            # `matches`, which is what the preview surface does.
            resolution.skip = True
            resolution.posting_account = None
            resolution.contra_account = None
            resolution.hits = [RuleHit(stored.id, "skip", "")]
            return resolution
        if stored.action == "posting_account":
            if resolution.posting_account is None:
                resolution.posting_account = stored.target
                resolution.hits.append(RuleHit(stored.id, stored.action, stored.target))
        elif stored.action == "contra_account":
            if resolution.contra_account is None:
                resolution.contra_account = stored.target
                resolution.hits.append(RuleHit(stored.id, stored.action, stored.target))
        # An action outside the enum is ignored, for the reason `matches`
        # refuses an unknown field: it can only have been hand-written.
    return resolution


# =============================================================================
# Write-time validation
# =============================================================================


def validate_rule_fields(fields: dict) -> dict:
    """Validate a complete set of rule fields and return it normalized.

    Complete, not partial: an update merges the stored row with the change and
    validates the result, so that a `skip` action and a target arriving in
    separate requests are still checked against each other.

    Raises `ValueError` — or `config_store.InvalidAccountError`, a subclass of
    it — naming the field at fault. Every caller is a write path.

    `match_value` is stored exactly as given. A value that is only whitespace
    is refused as empty, and otherwise the spaces are the user's and are
    significant to `exact`. So `"  Software  "` is a rule that saves and then
    matches only a category with those spaces, which the editor should show
    rather than silently trim.

    `ledger`, `source` and `note` are deliberately unbounded where
    `match_value` is capped. `match_value` is walked against every subject of
    every transaction in a run, and the cap is what pairs with the subject cap
    to make that cost knowable; these three are only ever compared for equality
    or displayed, the column is `TEXT`, and a ledger name is an operator's own
    configuration.
    """
    from istota.money.config_store import _check_map_account

    unknown = sorted(set(fields) - set(RULE_FIELDS))
    if unknown:
        raise ValueError("unknown rule field(s): " + ", ".join(unknown))
    missing = [key for key in _REQUIRED_FIELDS if key not in fields]
    if missing:
        raise ValueError("missing rule field(s): " + ", ".join(missing))

    out: dict[str, Any] = {**_FIELD_DEFAULTS, **fields}

    for key in ("ledger", "source", "origin", "note"):
        if not isinstance(out[key], str):
            raise ValueError(f"{key} must be a string")

    for key, allowed in (
        ("field", FIELDS),
        ("match_kind", MATCH_KINDS),
        ("action", ACTIONS),
        ("origin", ORIGINS),
    ):
        if out[key] not in allowed:
            raise ValueError(
                f"invalid {key} {out[key]!r}: expected one of {', '.join(allowed)}",
            )

    value = out["match_value"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("match_value must be a non-empty string")
    if len(value) > MAX_MATCH_VALUE_CHARS:
        raise ValueError(
            f"match_value is longer than {MAX_MATCH_VALUE_CHARS} characters",
        )

    target = out["target"]
    if not isinstance(target, str):
        raise ValueError("target must be a string")
    if out["action"] == "skip":
        if target:
            raise ValueError("a skip rule takes no target")
    else:
        # The label, never `value`: the message is rendered into an HTTP
        # response and onto a terminal, and `compile_rules` two blocks up
        # demotes a `match_value` to DEBUG for the same reason.
        _check_map_account("transaction-rule", "target", target)

    priority = out["priority"]
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError(
            f"priority must be an integer between {MIN_PRIORITY} and {MAX_PRIORITY}",
        )
    if not MIN_PRIORITY <= priority <= MAX_PRIORITY:
        raise ValueError(
            f"priority {priority} is outside {MIN_PRIORITY}..{MAX_PRIORITY}",
        )

    enabled = out["enabled"]
    if isinstance(enabled, bool):
        out["enabled"] = enabled
    elif isinstance(enabled, int) and enabled in (0, 1):
        out["enabled"] = bool(enabled)
    else:
        # Never `bool(value)`: the CLI and the skill hand through argparse
        # strings, and `bool("false")` is True — a rule the user switched off
        # would stay live, which is the one direction this must not fail in.
        raise ValueError("enabled must be a boolean")

    return {key: out[key] for key in RULE_FIELDS}
