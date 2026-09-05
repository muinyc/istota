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
rule-engine exception would fail an import that is otherwise fine. Every step
that can raise — compiling a pattern, validating a write — happens at write
time or at load time instead, and `matches` answers False for anything it
cannot make sense of: a disabled rule, an empty `match_value`, a `field` or
`match_kind` outside its enum. Those are all shapes the API refuses, so they
arrive only from a hand-edited database, and there the safe answer is that the
rule matches nothing. The opposite reading is what makes them dangerous: an
empty `contains` needle matches every transaction, so reading it literally
would re-route a whole import on one bad row.

**Regex is case-insensitive**, because `iexact` and `contains` both are and a
vocabulary where one member silently differs on case is a trap. `(?-i:…)` is
the escape hatch, and it is what the editor should point at.

**Regex safety is bounded by input length, not by a timeout.** Python's `re`
has no timeout and a thread-based one on the sync path is worse than the
problem. So a pattern is capped at `MAX_MATCH_VALUE_CHARS` at write time and
every subject is truncated to `MAX_SUBJECT_CHARS` before matching. A category
or payee longer than that is already pathological; the residual is that a
hostile pattern can slow a sync, and the person who can write one is the person
whose sync it is.

stdlib-only leaf at import time. `validate_rule_fields` reaches
`config_store._check_map_account` through a function-scope import, so the one
beancount account check in the codebase stays the one this module uses without
the import graph growing a module-scope edge into the store.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .importers.base import NormalizedTransaction

logger = logging.getLogger(__name__)


FIELDS = ("category", "account", "payee", "notes", "tag")
MATCH_KINDS = ("exact", "iexact", "contains", "regex")
ACTIONS = ("posting_account", "contra_account", "skip")

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
    """A rule with its pattern compiled, which is the only per-rule work."""

    rule: Rule
    pattern: re.Pattern[str] | None = None

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
    """Compile one rule. Raises `ValueError` on a pattern `re` cannot parse."""
    pattern = None
    if rule.match_kind == "regex":
        pattern = compile_pattern(rule.match_value)
    return CompiledRule(rule=rule, pattern=pattern)


def compile_pattern(value: str) -> re.Pattern[str]:
    """Compile a rule pattern, case-insensitively.

    The message carries both the pattern and `re`'s own complaint, because the
    person reading it is the person who wrote the pattern.
    """
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid regex {value!r}: {exc}") from exc


def compile_rules(rules: Iterable[Rule]) -> list[CompiledRule]:
    """Compile a list, dropping any rule whose pattern no longer compiles.

    Order is preserved. A broken row can only come from a hand-edited database
    — the API compiles at write time — and one of them must not fail a sync, so
    it is logged by id and skipped. The pattern itself goes to DEBUG: a warning
    reaches the operator's log channel, and a `match_value` is the user's own
    financial data.
    """
    compiled: list[CompiledRule] = []
    for rule in rules:
        try:
            compiled.append(compile_rule(rule))
        except ValueError as exc:
            logger.warning(
                "transaction rule %s has an uncompilable pattern and was skipped",
                rule.id,
            )
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
    if not isinstance(subject, str) or not subject:
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
    if kind == "regex":
        return compiled.pattern is not None and compiled.pattern.search(subject) is not None
    return False


def matches(rule: CompiledRule, txn: NormalizedTransaction) -> bool:
    """Whether one rule matches one transaction. Never raises.

    An empty subject matches nothing, whatever the kind: `match_value` is
    required non-empty, so there is no rule an empty category could match.
    """
    stored = rule.rule
    if not stored.enabled or not stored.match_value:
        return False
    if stored.field == "tag":
        tags = getattr(txn, "tags", None) or []
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
            resolution.skip = True
            resolution.posting_account = None
            resolution.contra_account = None
            resolution.hits.append(RuleHit(stored.id, "skip", ""))
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

    for key, allowed in (("field", FIELDS), ("match_kind", MATCH_KINDS), ("action", ACTIONS)):
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
        _check_map_account("transaction-rule", value, target)

    priority = out["priority"]
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError(
            f"priority must be an integer between {MIN_PRIORITY} and {MAX_PRIORITY}",
        )
    if not MIN_PRIORITY <= priority <= MAX_PRIORITY:
        raise ValueError(
            f"priority {priority} is outside {MIN_PRIORITY}..{MAX_PRIORITY}",
        )

    if out["match_kind"] == "regex":
        compile_pattern(value)

    out["enabled"] = bool(out["enabled"])
    return {key: out[key] for key in RULE_FIELDS}
