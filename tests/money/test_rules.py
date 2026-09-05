"""Tests for money.core.rules — the pure transaction rule engine.

The module this covers is stdlib-only and has no callers yet (spec Stage 1), so
everything here is a direct call. The last class is the one that matters most:
it asserts that a set of rules emitted the way the migration will emit them
resolves every input to the same account `map_monarch_category_with_config`
resolves it to today.
"""

from __future__ import annotations

import logging
import random
import unicodedata
from datetime import date

import pytest

from istota.money.config_store import InvalidAccountError
from istota.money.core.importers.base import NormalizedTransaction
from istota.money.core.models import (
    MonarchConfig,
    MonarchCredentials,
    MonarchSyncSettings,
    MonarchTagFilters,
)
from istota.money.core.rules import (
    ACTIONS,
    ORIGINS,
    FIELDS,
    MATCH_KINDS,
    MAX_MATCH_VALUE_CHARS,
    MAX_SUBJECT_CHARS,
    CompiledRule,
    Resolution,
    Rule,
    RuleHit,
    compile_rule,
    compile_rules,
    matches,
    resolve,
    rules_in_scope,
    sort_key,
    validate_rule_fields,
)
from istota.money.core.transactions import (
    MONARCH_CATEGORY_MAP,
    account_component,
    map_monarch_category_with_config,
)


_RULE_DEFAULTS = {
    "id": 1,
    "ledger": "",
    "source": "",
    "field": "category",
    "match_kind": "iexact",
    "match_value": "Groceries",
    "action": "posting_account",
    "target": "Expenses:Food:Groceries",
    "priority": 100,
    "enabled": True,
    "origin": "user",
    "note": "",
}

# The NormalizedTransaction attribute each scalar `field` reads.
SUBJECT_ATTR = {
    "category": "category",
    "account": "account_name",
    "payee": "payee",
    "notes": "notes",
}


def mkrule(**kw) -> Rule:
    return Rule(**{**_RULE_DEFAULTS, **kw})


def mkcompiled(**kw):
    return compile_rule(mkrule(**kw))


def mktxn(**kw) -> NormalizedTransaction:
    base = {"date": date(2026, 1, 15), "amount": -12.50, "payee": "Acme Grocery"}
    return NormalizedTransaction(**{**base, **kw})


class TestVocabulary:
    def test_the_three_enums_are_what_the_spec_names(self):
        assert FIELDS == ("category", "account", "payee", "notes", "tag")
        assert MATCH_KINDS == ("exact", "iexact", "contains")
        assert ACTIONS == ("posting_account", "contra_account", "skip")

    def test_the_two_bounds(self):
        assert MAX_MATCH_VALUE_CHARS == 200
        assert MAX_SUBJECT_CHARS == 512

    def test_every_scalar_field_has_a_subject(self):
        assert set(SUBJECT_ATTR) | {"tag"} == set(FIELDS)


@pytest.mark.parametrize("field_name,attr", sorted(SUBJECT_ATTR.items()))
class TestScalarFieldsAgainstEveryMatchKind:
    def test_exact(self, field_name, attr):
        rule = mkcompiled(field=field_name, match_kind="exact", match_value="Software")
        assert matches(rule, mktxn(**{attr: "Software"}))
        assert not matches(rule, mktxn(**{attr: "software"}))
        assert not matches(rule, mktxn(**{attr: "Software Inc"}))

    def test_iexact(self, field_name, attr):
        rule = mkcompiled(field=field_name, match_kind="iexact", match_value="Software")
        assert matches(rule, mktxn(**{attr: "Software"}))
        assert matches(rule, mktxn(**{attr: "SOFTWARE"}))
        assert not matches(rule, mktxn(**{attr: "Software Inc"}))

    def test_contains(self, field_name, attr):
        rule = mkcompiled(field=field_name, match_kind="contains", match_value="soft")
        assert matches(rule, mktxn(**{attr: "Software"}))
        assert matches(rule, mktxn(**{attr: "MICROSOFT"}))
        assert not matches(rule, mktxn(**{attr: "Hardware"}))

    @pytest.mark.parametrize(
        "kind,value",
        [
            ("exact", "Software"),
            ("iexact", "Software"),
            ("contains", "soft"),
        ],
    )
    def test_an_empty_subject_matches_nothing(self, field_name, attr, kind, value):
        """Derived from the non-empty `match_value`, not from a guard.

        No mutation of `_subject_matches` can turn this red now that `regex` is
        gone — `^$` was the only kind that could match an empty subject. It
        pins the behaviour the fallback depends on, not a line of code.
        """
        rule = mkcompiled(field=field_name, match_kind=kind, match_value=value)
        assert not matches(rule, mktxn(**{attr: ""}))

    def test_a_rule_on_one_field_ignores_the_others(self, field_name, attr):
        rule = mkcompiled(field=field_name, match_kind="iexact", match_value="Software")
        others = {a: "Software" for a in SUBJECT_ATTR.values() if a != attr}
        assert not matches(rule, mktxn(**others))


class TestTagField:
    def test_any_tag_matches(self):
        rule = mkcompiled(field="tag", match_kind="iexact", match_value="Personal")
        assert matches(rule, mktxn(tags=["Business", "Personal"]))
        assert matches(rule, mktxn(tags=["personal"]))
        assert not matches(rule, mktxn(tags=["Business", "Reimbursed"]))

    def test_an_empty_tag_list_matches_nothing(self):
        for kind, value in (
            ("exact", "Personal"),
            ("iexact", "Personal"),
            ("contains", "pers"),
        ):
            rule = mkcompiled(field="tag", match_kind=kind, match_value=value)
            assert not matches(rule, mktxn(tags=[]))
            assert not matches(rule, mktxn(tags=["", ""]))

    def test_exact_on_a_tag_is_case_sensitive(self):
        rule = mkcompiled(field="tag", match_kind="exact", match_value="Personal")
        assert matches(rule, mktxn(tags=["Business", "Personal"]))
        assert not matches(rule, mktxn(tags=["personal"]))

    def test_contains_applies_per_tag(self):
        contains = mkcompiled(field="tag", match_kind="contains", match_value="imb")
        assert matches(contains, mktxn(tags=["Reimbursed"]))
        assert not matches(contains, mktxn(tags=["Business"]))

    def test_an_empty_tag_string_is_not_a_wildcard(self):
        rule = mkcompiled(field="tag", match_kind="contains", match_value="a")
        assert not matches(rule, mktxn(tags=["", ""]))


class TestMatchesRefusals:
    """A hand-edited row must never match everything. `resolve` never raises."""

    def test_a_disabled_rule_never_matches(self):
        rule = mkcompiled(match_value="Groceries", enabled=False)
        assert not matches(rule, mktxn(category="Groceries"))

    def test_an_empty_match_value_matches_nothing(self):
        for kind in MATCH_KINDS:
            rule = mkcompiled(match_kind=kind, match_value="")
            assert not matches(rule, mktxn(category="Groceries"))
            assert not matches(rule, mktxn(category=""))

    @pytest.mark.parametrize("subject", [None, 5, b"Groceries", ["Groceries"]])
    @pytest.mark.parametrize("kind", MATCH_KINDS)
    def test_a_subject_that_is_not_a_string(self, subject, kind):
        """What a JSON null or a SQLite NULL hands the sync path."""
        for attr in SUBJECT_ATTR.values():
            rule = mkcompiled(field="category", match_kind=kind, match_value="Groceries")
            txn = mktxn()
            setattr(txn, attr, subject)
            assert matches(rule, txn) is False

    @pytest.mark.parametrize("tags", ["Personal", None, 5, {"Personal": 1}])
    def test_tags_that_are_not_a_list(self, tags):
        """A bare string is iterable, and `contains` would match a character."""
        txn = mktxn()
        txn.tags = tags
        assert not matches(mkcompiled(field="tag", match_value="Personal"), txn)
        assert not matches(
            mkcompiled(field="tag", match_kind="contains", match_value="a"), txn
        )

    def test_an_empty_match_value_would_otherwise_capture_every_transaction(self):
        """The consequence, not just the predicate: one row, every posting."""
        rules = rules_in_scope(
            compile_rules(
                [
                    mkrule(
                        id=1,
                        priority=10,
                        match_kind="contains",
                        match_value="",
                        target="Expenses:Everything",
                    ),
                    mkrule(
                        id=2,
                        priority=100,
                        match_value="Groceries",
                        target="Expenses:Food:Groceries",
                    ),
                ]
            ),
            "acme",
            "monarch-api",
        )
        res = resolve(mktxn(category="Groceries"), rules)
        assert res.posting_account == "Expenses:Food:Groceries"
        assert [hit.rule_id for hit in res.hits] == [2]

    def test_an_unknown_field_matches_nothing(self):
        rule = mkcompiled(field="amount", match_kind="contains", match_value="1")
        assert not matches(rule, mktxn(category="Groceries", notes="12.50"))

    def test_an_unknown_match_kind_matches_nothing(self):
        """`compile_rule` refuses the row, so `matches` is the second line."""
        rule = CompiledRule(rule=mkrule(match_kind="glob", match_value="Groc*"))
        assert not matches(rule, mktxn(category="Groceries"))


class TestSubjectTruncation:
    def test_a_subject_is_truncated_before_matching(self):
        long_subject = "a" * MAX_SUBJECT_CHARS + "NEEDLE"
        rule = mkcompiled(match_kind="contains", match_value="NEEDLE")
        assert not matches(rule, mktxn(category=long_subject))

    def test_the_last_character_inside_the_bound_still_matches(self):
        subject = "a" * (MAX_SUBJECT_CHARS - 1) + "N"
        rule = mkcompiled(match_kind="contains", match_value="N")
        assert matches(rule, mktxn(category=subject))

    def test_truncation_applies_per_tag(self):
        rule = mkcompiled(field="tag", match_kind="contains", match_value="NEEDLE")
        assert not matches(rule, mktxn(tags=["a" * MAX_SUBJECT_CHARS + "NEEDLE"]))
        assert matches(rule, mktxn(tags=["a" * (MAX_SUBJECT_CHARS - 6) + "NEEDLE"]))

    @pytest.mark.parametrize("kind", MATCH_KINDS)
    def test_truncation_only_ever_removes_a_match(self, kind):
        """A needle past the cut is lost, and nothing is gained.

        `exact` and `iexact` cannot be reached by the cut at all on a row the
        write boundary admits: a subject past the cap is 512 characters and
        `match_value` stops at 200, so the two are unequal cut or uncut.
        """
        subject = "a" * MAX_SUBJECT_CHARS + "NEEDLE"
        value = "NEEDLE" if kind == "contains" else "a" * MAX_MATCH_VALUE_CHARS
        assert not matches(mkcompiled(match_kind=kind, match_value=value), mktxn(category=subject))

    def test_the_value_cap_is_what_makes_that_true_of_exact(self):
        """The one row where the cut could start a match, and what refuses it.

        Reachable only by hand-editing the table: the truncated subject is
        exactly the stored value, so `exact` matches something the untruncated
        subject is not. `validate_rule_fields` is the guard.
        """
        long_value = "a" * MAX_SUBJECT_CHARS
        rule = CompiledRule(rule=mkrule(match_kind="exact", match_value=long_value))
        assert matches(rule, mktxn(category=long_value + "b"))
        with pytest.raises(ValueError):
            validate_rule_fields(
                {
                    "field": "category",
                    "match_kind": "exact",
                    "match_value": long_value,
                    "action": "posting_account",
                    "target": "Expenses:Long",
                }
            )


class TestCompilation:
    @pytest.mark.parametrize("kind", MATCH_KINDS)
    def test_every_kind_in_the_enum_compiles(self, kind):
        assert compile_rule(mkrule(match_kind=kind)).rule.match_kind == kind

    @pytest.mark.parametrize("kind", ["regex", "glob", ""])
    def test_a_kind_this_release_does_not_know_is_refused(self, kind):
        """`regex` is the live case: a hand edit, or a rollback (ISSUE-429)."""
        with pytest.raises(ValueError) as exc:
            compile_rule(mkrule(match_kind=kind))
        assert "match_kind" in str(exc.value)

    def test_the_compiled_rule_exposes_the_ordering_fields(self):
        compiled = mkcompiled(id=7, priority=42)
        assert compiled.id == 7
        assert compiled.priority == 42
        assert compiled.rule.id == 7

    def test_compile_rules_skips_an_unusable_row_and_keeps_the_rest(self, caplog):
        rules = [
            mkrule(id=1, match_value="Groceries"),
            mkrule(id=2, match_kind="regex", match_value="^groc"),
            mkrule(id=3, match_value="Software", target="Expenses:Software"),
        ]
        with caplog.at_level(logging.WARNING, logger="istota.money.core.rules"):
            compiled = compile_rules(rules)
        assert [c.id for c in compiled] == [1, 3]
        assert any("2" in record.getMessage() for record in caplog.records)

    def test_the_warning_names_the_id_and_not_the_match_value(self, caplog):
        """A warning reaches the operator's log channel; the value is user data.

        Both halves are needed: the refusal alone passes if the module stops
        reporting the reason anywhere at all.
        """
        rules = [mkrule(id=2, match_kind="regex", match_value="Rent-Arrears")]
        with caplog.at_level(logging.WARNING, logger="istota.money.core.rules"):
            compile_rules(rules)
        assert caplog.records
        assert not any("Rent-Arrears" in r.getMessage() for r in caplog.records)

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="istota.money.core.rules"):
            compile_rules(rules)
        assert any("match_kind" in r.getMessage() for r in caplog.records)

    def test_a_dropped_skip_rule_lets_the_transaction_through(self, caplog):
        """Not the safe direction, and the reason the drop is logged."""
        rules = [
            mkrule(id=1, field="tag", match_kind="regex", match_value="Personal",
                   action="skip", target=""),
            mkrule(id=2, match_value="Software", target="Expenses:Software"),
        ]
        with caplog.at_level(logging.WARNING, logger="istota.money.core.rules"):
            compiled = compile_rules(rules)
        res = resolve(mktxn(category="Software", tags=["Personal"]), compiled)
        assert res.skip is False
        assert res.posting_account == "Expenses:Software"
        assert any("1" in r.getMessage() for r in caplog.records)

    def test_compile_rules_preserves_the_order_it_was_given(self):
        rules = [mkrule(id=9), mkrule(id=4), mkrule(id=6)]
        assert [c.id for c in compile_rules(rules)] == [9, 4, 6]


class TestScopeAndOrdering:
    def test_sort_key_is_priority_then_id(self):
        assert sort_key(mkrule(id=3, priority=50)) == (50, 3)
        assert sort_key(mkcompiled(id=3, priority=50)) == (50, 3)

    def test_rules_in_scope_sorts_by_priority_then_id(self):
        rules = compile_rules(
            [
                mkrule(id=5, priority=100),
                mkrule(id=2, priority=50),
                mkrule(id=1, priority=100),
                mkrule(id=9, priority=10),
            ]
        )
        assert [c.id for c in rules_in_scope(rules, "acme", "monarch-api")] == [9, 2, 1, 5]

    def test_a_wildcard_scope_is_in_every_scope(self):
        rules = compile_rules([mkrule(id=1, ledger="", source="")])
        assert len(rules_in_scope(rules, "acme", "monarch-api")) == 1
        assert len(rules_in_scope(rules, "personal", "monarch-csv")) == 1

    def test_both_columns_narrow(self):
        rules = compile_rules(
            [
                mkrule(id=1, ledger="acme", source=""),
                mkrule(id=2, ledger="", source="monarch-csv"),
                mkrule(id=3, ledger="acme", source="monarch-csv"),
                mkrule(id=4, ledger="personal", source="monarch-csv"),
                mkrule(id=5, ledger="acme", source="monarch-api"),
            ]
        )
        assert [c.id for c in rules_in_scope(rules, "acme", "monarch-csv")] == [1, 2, 3]
        assert [c.id for c in rules_in_scope(rules, "personal", "monarch-csv")] == [2, 4]
        assert [c.id for c in rules_in_scope(rules, "other", "other")] == []

    def test_scope_comparison_is_exact(self):
        rules = compile_rules([mkrule(id=1, ledger="Acme")])
        assert rules_in_scope(rules, "acme", "monarch-api") == []

    def test_a_disabled_rule_is_out_of_scope(self):
        rules = compile_rules([mkrule(id=1, enabled=False), mkrule(id=2)])
        assert [c.id for c in rules_in_scope(rules, "acme", "monarch-api")] == [2]


class TestResolve:
    def test_an_empty_rule_list(self):
        res = resolve(mktxn(category="Groceries"), [])
        assert res == Resolution(
            skip=False,
            posting_account=None,
            contra_account=None,
            hits=[],
            considered=0,
        )

    def test_no_rule_matches(self):
        rules = compile_rules([mkrule(id=1, match_value="Software")])
        res = resolve(mktxn(category="Groceries"), rules)
        assert res.posting_account is None
        assert res.contra_account is None
        assert res.hits == []
        assert res.considered == 1

    def test_the_two_slots_fill_from_different_rules(self):
        rules = compile_rules(
            [
                mkrule(
                    id=1,
                    field="category",
                    match_value="Software",
                    action="posting_account",
                    target="Expenses:Business:Software",
                ),
                mkrule(
                    id=2,
                    field="account",
                    match_value="Acme Business",
                    action="contra_account",
                    target="Assets:Bank:Acme-Business",
                ),
            ]
        )
        res = resolve(mktxn(category="Software", account_name="Acme Business"), rules)
        assert res.skip is False
        assert res.posting_account == "Expenses:Business:Software"
        assert res.contra_account == "Assets:Bank:Acme-Business"
        assert res.hits == [
            RuleHit(rule_id=1, action="posting_account", target="Expenses:Business:Software"),
            RuleHit(rule_id=2, action="contra_account", target="Assets:Bank:Acme-Business"),
        ]

    def test_first_match_wins_per_slot(self):
        rules = rules_in_scope(
            compile_rules(
                [
                    mkrule(id=3, priority=30, match_value="Software", target="Expenses:Second"),
                    mkrule(id=1, priority=10, match_value="Software", target="Expenses:First"),
                    mkrule(
                        id=2,
                        priority=20,
                        field="account",
                        match_value="Acme",
                        action="contra_account",
                        target="Assets:Acme",
                    ),
                ]
            ),
            "acme",
            "monarch-api",
        )
        res = resolve(mktxn(category="Software", account_name="Acme"), rules)
        assert res.posting_account == "Expenses:First"
        assert [hit.rule_id for hit in res.hits] == [1, 2]

    def test_first_match_wins_per_slot_in_the_order_given(self):
        """`resolve` does not sort; `rules_in_scope` is what establishes order."""
        rules = compile_rules(
            [
                mkrule(id=1, priority=10, match_value="Software", target="Expenses:First"),
                mkrule(
                    id=2,
                    priority=20,
                    field="account",
                    match_value="Acme",
                    action="contra_account",
                    target="Assets:Acme",
                ),
                mkrule(id=3, priority=30, match_value="Software", target="Expenses:Second"),
            ]
        )
        res = resolve(mktxn(category="Software", account_name="Acme"), rules)
        assert res.posting_account == "Expenses:First"
        assert [hit.rule_id for hit in res.hits] == [1, 2]

    def test_a_shadowed_rule_is_not_a_hit(self):
        rules = rules_in_scope(
            compile_rules(
                [
                    mkrule(
                        id=2, priority=200, match_value="software", target="Expenses:Second"
                    ),
                    mkrule(
                        id=1, priority=100, match_value="Software", target="Expenses:First"
                    ),
                ]
            ),
            "acme",
            "monarch-api",
        )
        res = resolve(mktxn(category="Software"), rules)
        assert [hit.rule_id for hit in res.hits] == [1]
        assert res.considered == 2

    def test_priority_decides_and_id_breaks_the_tie(self):
        rules = rules_in_scope(
            compile_rules(
                [
                    mkrule(id=8, priority=100, match_value="Software", target="Expenses:Eight"),
                    mkrule(id=3, priority=100, match_value="software", target="Expenses:Three"),
                    mkrule(id=9, priority=200, match_value="SOFTWARE", target="Expenses:Nine"),
                ]
            ),
            "acme",
            "monarch-api",
        )
        res = resolve(mktxn(category="Software"), rules)
        assert res.posting_account == "Expenses:Three"

    def test_skip_short_circuits_the_pass(self):
        rules = rules_in_scope(
            compile_rules(
                [
                    mkrule(
                        id=1,
                        priority=50,
                        field="tag",
                        match_value="Personal",
                        action="skip",
                        target="",
                    ),
                    mkrule(id=2, priority=100, match_value="Software", target="Expenses:Software"),
                ]
            ),
            "acme",
            "monarch-api",
        )
        res = resolve(mktxn(category="Software", tags=["Personal"]), rules)
        assert res.skip is True
        assert res.posting_account is None
        assert res.contra_account is None
        assert res.hits == [RuleHit(rule_id=1, action="skip", target="")]
        assert res.considered == 1

    def test_a_skip_that_does_not_match_leaves_the_pass_alone(self):
        rules = rules_in_scope(
            compile_rules(
                [
                    mkrule(
                        id=1,
                        priority=50,
                        field="tag",
                        match_value="Personal",
                        action="skip",
                        target="",
                    ),
                    mkrule(id=2, priority=100, match_value="Software", target="Expenses:Software"),
                ]
            ),
            "acme",
            "monarch-api",
        )
        res = resolve(mktxn(category="Software", tags=["Business"]), rules)
        assert res.skip is False
        assert res.posting_account == "Expenses:Software"
        assert res.considered == 2

    def test_a_skip_after_a_filled_slot_clears_the_slot_and_its_hit(self):
        """The slots and the hits go together, so a trace cannot claim a post."""
        rules = compile_rules(
            [
                mkrule(id=1, match_value="Software", target="Expenses:Software"),
                mkrule(id=2, field="tag", match_value="Personal", action="skip", target=""),
            ]
        )
        res = resolve(mktxn(category="Software", tags=["Personal"]), rules)
        assert res.skip is True
        assert res.posting_account is None
        assert res.contra_account is None
        assert res.hits == [RuleHit(rule_id=2, action="skip", target="")]

    def test_an_unknown_action_is_ignored(self):
        rules = compile_rules(
            [
                mkrule(id=1, match_value="Software", action="rewrite_payee", target="X"),
                mkrule(id=2, match_value="Software", target="Expenses:Software"),
            ]
        )
        res = resolve(mktxn(category="Software"), rules)
        assert res.posting_account == "Expenses:Software"
        assert [hit.rule_id for hit in res.hits] == [2]

    def test_resolve_does_not_raise_on_a_pathological_rule_set(self):
        # Built directly rather than through `compile_rule`, which refuses the
        # unknown kind — this is what reaches `resolve` if a caller skips it.
        rules = [
            CompiledRule(rule=mkrule(id=1, field="nope", match_kind="glob", match_value="")),
            CompiledRule(rule=mkrule(id=2, action="nope", target="")),
        ]
        assert resolve(mktxn(), rules).hits == []


class TestValidateRuleFields:
    def _fields(self, **kw):
        base = {
            "field": "category",
            "match_kind": "iexact",
            "match_value": "Software",
            "action": "posting_account",
            "target": "Expenses:Business:Software",
        }
        base.update(kw)
        return base

    def test_it_returns_a_complete_normalized_row(self):
        out = validate_rule_fields(
            {
                "field": "category",
                "match_value": "Software",
                "action": "posting_account",
                "target": "Expenses:Business:Software",
            }
        )
        assert out == {
            "ledger": "",
            "source": "",
            "field": "category",
            "match_kind": "iexact",
            "match_value": "Software",
            "action": "posting_account",
            "target": "Expenses:Business:Software",
            "priority": 100,
            "enabled": True,
            "origin": "",
            "note": "",
        }

    def test_it_does_not_mutate_its_argument(self):
        given = self._fields()
        validate_rule_fields(given)
        assert given == self._fields()

    @pytest.mark.parametrize("key", ["field", "match_value", "action"])
    def test_a_missing_required_key(self, key):
        fields = self._fields()
        del fields[key]
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(fields)
        assert key in str(exc.value)

    def test_an_unknown_key_is_refused(self):
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(amount_over=500))
        assert "amount_over" in str(exc.value)

    def test_the_id_and_the_timestamps_are_not_rule_fields(self):
        for key in ("id", "created_at", "updated_at"):
            with pytest.raises(ValueError):
                validate_rule_fields(self._fields(**{key: "x"}))

    @pytest.mark.parametrize("value", FIELDS)
    def test_every_field_in_the_enum_is_accepted(self, value):
        out = validate_rule_fields(self._fields(field=value))
        assert out["field"] == value

    @pytest.mark.parametrize("value", MATCH_KINDS)
    def test_every_match_kind_in_the_enum_is_accepted(self, value):
        out = validate_rule_fields(self._fields(match_kind=value, match_value="soft"))
        assert out["match_kind"] == value

    @pytest.mark.parametrize("value", ACTIONS)
    def test_every_action_in_the_enum_is_accepted(self, value):
        target = "" if value == "skip" else "Expenses:Business:Software"
        out = validate_rule_fields(self._fields(action=value, target=target))
        assert out["action"] == value

    @pytest.mark.parametrize(
        "key,value",
        [
            ("field", "amount"),
            ("match_kind", "glob"),
            ("action", "rewrite_payee"),
        ],
    )
    def test_a_value_outside_its_enum(self, key, value):
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(**{key: value}))
        assert value in str(exc.value)

    @pytest.mark.parametrize("value", ["", "   ", None, 5])
    def test_an_empty_or_non_string_match_value(self, value):
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(match_value=value))
        assert "match_value" in str(exc.value)

    def test_an_over_long_match_value(self):
        ok = validate_rule_fields(self._fields(match_value="x" * MAX_MATCH_VALUE_CHARS))
        assert len(ok["match_value"]) == MAX_MATCH_VALUE_CHARS
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(match_value="x" * (MAX_MATCH_VALUE_CHARS + 1)))
        assert str(MAX_MATCH_VALUE_CHARS) in str(exc.value)

    def test_a_padded_match_value_is_stored_verbatim(self):
        """Refused only when it is *all* whitespace; otherwise the spaces stay."""
        out = validate_rule_fields(self._fields(match_kind="exact", match_value="  Software  "))
        assert out["match_value"] == "  Software  "
        rule = compile_rule(Rule(id=1, **out))
        assert matches(rule, mktxn(category="  Software  "))
        assert not matches(rule, mktxn(category="Software"))

    def test_a_target_that_is_not_a_beancount_account(self):
        with pytest.raises(InvalidAccountError) as exc:
            validate_rule_fields(self._fields(target="not an account"))
        assert "not an account" in str(exc.value)

    def test_the_account_check_covers_the_contra_slot_too(self):
        with pytest.raises(InvalidAccountError):
            validate_rule_fields(self._fields(action="contra_account", target="nope"))

    def test_an_invalid_account_is_also_a_value_error(self):
        with pytest.raises(ValueError):
            validate_rule_fields(self._fields(target="nope"))

    def test_a_skip_with_a_target(self):
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(action="skip", target="Expenses:Whatever"))
        assert "skip" in str(exc.value)

    def test_a_skip_with_no_target_is_fine(self):
        out = validate_rule_fields(self._fields(action="skip", target=""))
        assert out["target"] == ""

    @pytest.mark.parametrize("value", [0, 9999, 50])
    def test_a_priority_inside_the_bound(self, value):
        assert validate_rule_fields(self._fields(priority=value))["priority"] == value

    @pytest.mark.parametrize("value", [-1, 10000, "100", 1.5, None])
    def test_a_priority_outside_the_bound_or_not_an_int(self, value):
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(priority=value))
        assert "priority" in str(exc.value)

    def test_a_bool_is_not_a_priority(self):
        with pytest.raises(ValueError):
            validate_rule_fields(self._fields(priority=True))

    def test_regex_is_refused_at_the_write_boundary(self):
        """ISSUE-429: the kind is out of the vocabulary, not merely unused."""
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(match_kind="regex", match_value="^soft"))
        assert "regex" in str(exc.value)

    @pytest.mark.parametrize(
        "given,expected",
        [(True, True), (False, False), (1, True), (0, False)],
    )
    def test_enabled_takes_a_bool_or_the_two_ints_the_column_holds(self, given, expected):
        assert validate_rule_fields(self._fields(enabled=given))["enabled"] is expected

    @pytest.mark.parametrize("value", ["false", "true", "0", "", None, 2, -1])
    def test_enabled_is_never_coerced_from_a_string(self, value):
        """`bool("false")` is True, and the CLI hands through argparse strings."""
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(enabled=value))
        assert "enabled" in str(exc.value)

    @pytest.mark.parametrize("value", ORIGINS)
    def test_every_origin_in_the_enum_is_accepted(self, value):
        assert validate_rule_fields(self._fields(origin=value))["origin"] == value

    def test_an_origin_outside_the_enum(self):
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(origin="imported"))
        assert "imported" in str(exc.value)

    @pytest.mark.parametrize("key", ["ledger", "source", "note"])
    def test_the_free_text_columns_must_be_strings(self, key):
        assert validate_rule_fields(self._fields(**{key: "x"}))[key] == "x"
        with pytest.raises(ValueError) as exc:
            validate_rule_fields(self._fields(**{key: 3}))
        assert key in str(exc.value)

    def test_a_validated_row_builds_a_rule_that_compiles(self):
        out = validate_rule_fields(self._fields(match_kind="contains", match_value="soft"))
        compiled = compile_rule(Rule(id=1, **out))
        assert matches(compiled, mktxn(category="Software"))


# =============================================================================
# Equivalence with the mapping this replaces
# =============================================================================


def legacy_account(category: str, mapping: dict[str, str]) -> str:
    """What `map_monarch_category_with_config` answers today."""
    config = MonarchConfig(
        credentials=MonarchCredentials(),
        sync=MonarchSyncSettings(),
        accounts={},
        categories=dict(mapping),
        tags=MonarchTagFilters(),
    )
    return map_monarch_category_with_config(category, config)


def migrated_rules(mapping: dict[str, str], *, priority: int, start_id: int) -> list[Rule]:
    """Emit one flat category map the way the spec's migration emits it.

    Old semantics are exact-over-the-whole-map, then case-insensitive-over-the-
    whole-map, so an ordered list of `iexact` rules reproduces them unless two
    keys collide case-insensitively. A colliding group emits each member as an
    `exact` rule ten points ahead of the `iexact` tier, plus one `iexact` rule
    for the group's representative.

    The representative is the group's **first key in map order**, which is the
    key the old case-insensitive scan (`for key, value in config.categories`)
    would have returned. The spec's migration section says "first key by sort
    order", which differs whenever a group's map order is not its sort order —
    see the last test in this file.
    """
    groups: dict[str, list[str]] = {}
    for key in mapping:
        groups.setdefault(key.lower(), []).append(key)

    def rule(rule_id: int, key: str, kind: str, prio: int) -> Rule:
        return Rule(
            id=rule_id,
            ledger="",
            source="",
            field="category",
            match_kind=kind,
            match_value=key,
            action="posting_account",
            target=mapping[key],
            priority=prio,
            enabled=True,
            origin="migrated",
            note="",
        )

    rules: list[Rule] = []
    next_id = start_id
    for keys in groups.values():
        if len(keys) > 1:
            for key in keys:
                rules.append(rule(next_id, key, "exact", priority - 10))
                next_id += 1
    for keys in groups.values():
        rules.append(rule(next_id, keys[0], "iexact", priority))
        next_id += 1
    return rules


def resolved_account(category: str, mapping: dict[str, str]) -> str:
    """What the engine answers over migrated user rules plus the seeded map."""
    rules = migrated_rules(mapping, priority=100, start_id=1)
    rules += migrated_rules(MONARCH_CATEGORY_MAP, priority=900, start_id=100_000)
    scoped = rules_in_scope(compile_rules(rules), "acme", "monarch-api")
    res = resolve(mktxn(category=category), scoped)
    if res.posting_account is not None:
        return res.posting_account
    return f"Expenses:Uncategorized:{account_component(category)}"


CATEGORY_MAPS = {
    "empty": {},
    "no collisions": {
        "Software": "Expenses:Business:Software",
        "Consulting": "Income:Consulting",
    },
    "shadowing the shipped map": {
        "groceries": "Expenses:Groceries-Override",
        "Rent": "Expenses:Housing:Office-Rent",
    },
    "a case collision": {
        "Software": "Expenses:Business:Software",
        "software": "Expenses:Personal:Software",
        "SOFTWARE": "Expenses:Other:Software",
    },
    "lower and casefold disagree": {
        # 'STRASSE'.casefold() == 'Straße'.casefold(), and their .lower()s
        # differ. Swapping the engine to casefold turns this map's cases red,
        # which is the point: the lookup this replaces uses .lower().
        "Straße": "Expenses:Strasse-Eszett",
        "STRASSE": "Expenses:Strasse-Caps",
    },
    "a collision plus a plain key": {
        "Rent": "Expenses:Housing:Office-Rent",
        "rent": "Expenses:Housing:Home-Rent",
        "Software": "Expenses:Business:Software",
    },
}

CATEGORIES = [
    "Software",
    "software",
    "SOFTWARE",
    "SoFtWaRe",
    "Groceries",
    "groceries",
    "Rent",
    "RENT",
    "rent",
    "Consulting",
    "Food & Drink",
    "food & drink",
    "Internet Services (Reimbursed)",
    "Not In Any Map",
    "",
    "  ",
    "Ünïcode",
    "Straße",
    "STRASSE",
    "strasse",
    "straße",
]


class TestEquivalenceWithTheMappingThisReplaces:
    """The migration is behaviour-preserving. This is the test that says so."""

    @pytest.mark.parametrize("map_name", sorted(CATEGORY_MAPS))
    @pytest.mark.parametrize("category", CATEGORIES)
    def test_same_account_as_the_legacy_lookup(self, map_name, category):
        mapping = CATEGORY_MAPS[map_name]
        assert resolved_account(category, mapping) == legacy_account(category, mapping)

    def test_the_shipped_map_alone_still_answers(self):
        for category in CATEGORIES:
            assert resolved_account(category, {}) == legacy_account(category, {})

    def test_a_generated_sweep(self):
        rng = random.Random(20260905)
        names = ["Software", "Rent", "Groceries", "Travel", "Fees"]
        cases = names + [n.lower() for n in names] + [n.upper() for n in names] + ["Nope", ""]
        for _ in range(200):
            mapping: dict[str, str] = {}
            for name in rng.sample(names, rng.randint(0, len(names))):
                for variant in rng.sample([name, name.lower(), name.upper()], rng.randint(1, 3)):
                    suffix = rng.randint(1, 3)
                    mapping[variant] = f"Expenses:Gen:{account_component(variant)}-{suffix}"
            for category in cases:
                assert resolved_account(category, mapping) == legacy_account(category, mapping), (
                    f"category={category!r} mapping={mapping!r}"
                )

    def test_the_uncategorized_fallback_is_reached_through_no_rule(self):
        rules = rules_in_scope(
            compile_rules(migrated_rules(MONARCH_CATEGORY_MAP, priority=900, start_id=1)),
            "acme",
            "monarch-api",
        )
        assert resolve(mktxn(category="Not In Any Map"), rules).posting_account is None
        assert legacy_account("Not In Any Map", {}) == "Expenses:Uncategorized:NotInAnyMap"

    def test_neither_engine_normalizes_unicode(self):
        """NFC and NFD are different subjects here, exactly as they are today."""
        nfc = unicodedata.normalize("NFC", "Café")
        nfd = unicodedata.normalize("NFD", "Café")
        assert nfc != nfd
        mapping = {nfc: "Expenses:Cafe"}
        assert legacy_account(nfd, mapping).startswith("Expenses:Uncategorized")
        assert resolved_account(nfd, mapping) == legacy_account(nfd, mapping)
        assert resolved_account(nfc, mapping) == "Expenses:Cafe"

    def test_the_collision_representative_follows_map_order_not_sort_order(self):
        """Why `migrated_rules` picks the first key in map order.

        The old scan returns the first case-insensitive match in map order, so a
        group written lowercase-first resolves an unseen casing to the lowercase
        key's account. Emitting the sort-order-first key instead would answer
        with the other account, which is the one shape where the spec's
        migration wording is not behaviour-preserving.
        """
        mapping = {"software": "Expenses:Lowercase", "Software": "Expenses:Uppercase"}
        assert legacy_account("SOFTWARE", mapping) == "Expenses:Lowercase"
        assert resolved_account("SOFTWARE", mapping) == "Expenses:Lowercase"
        assert sorted(mapping)[0] == "Software"
