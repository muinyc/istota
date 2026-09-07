"""Tests for ``usage_render.render_cost``, the Python half of the cost render rule.

The rule is one sentence — no currency unless it is money — implemented twice,
here and in ``web/src/lib/usageFormat.ts``. ``usageFormat.parity.test.ts`` pins
the two together, but it asserts against a *table* of what this function is
believed to produce; nothing in that test executes Python. Without a test on
this side, a change made only to the dashboard leaves the table and the CLI
disagreeing with no failure anywhere.

The cases below therefore mirror the parity table's, and the two must be edited
together.
"""

from __future__ import annotations

import pytest

from istota.usage_render import COST_PLACEHOLDER, fmt_context, fmt_int, render_cost


@pytest.mark.parametrize(
    "cost_by_basis",
    [
        {},
        {"subscription": 99.0},
        {"estimated": 0.0},
        {"estimated": 0.0, "subscription": 1.0, "unknown": 2.0},
    ],
    ids=["empty", "subscription", "zero-estimate", "several-bases"],
)
def test_no_api_rows_render_a_bare_placeholder(cost_by_basis):
    """No ``api`` rows means no money, and the dash says so on its own.

    A subscription's 99.0 is a list price and a catalog estimate is routinely
    0.0, so neither may reach the column as currency. Naming the basis beside
    the dash is what this asserts is gone: every no-money group renders
    identically, whatever it spans.
    """
    assert render_cost(cost_by_basis) == COST_PLACEHOLDER


def test_none_renders_a_placeholder():
    """The callers pass a group's map straight through; it can be absent."""
    assert render_cost(None) == COST_PLACEHOLDER


def test_api_only_renders_a_dollar_figure():
    assert render_cost({"api": 1.5}) == "$1.50"


def test_mixed_group_shows_the_api_figure_alone_never_summed():
    """1.0 + 2.0 = 3.0 is the misread this refuses; naming the other basis is
    not how it refuses it.

    An operator who switched the CLI's auth mid-window has rows of both kinds.
    The column reports the money — 1.0 — and stays silent about the
    plan-equivalent rather than appending its name.
    """
    out = render_cost({"api": 1.0, "subscription": 2.0})

    assert out == "$1.00"
    assert "3.00" not in out
    assert "subscription" not in out


def test_no_basis_name_ever_reaches_the_column():
    """The ``+estimated+subscription+unknown`` suffix is gone.

    It overflowed a fixed-width column, and the bases it named are not
    something a reader can act on. The dollar figure means one thing now: money
    actually spent, on rows we can account for.
    """
    out = render_cost(
        {"api": 1.5, "unknown": 2.0, "estimated": 0.0, "subscription": 1.0}
    )

    assert out == "$1.50"
    assert "+" not in out


# ---------------------------------------------------------------------------
# The three rules that had no parity case (spec: duplicate-code-consolidation,
# F37). They live here rather than in a new file for the reason the module
# docstring gives: `usageFormat.parity.test.ts` asserts against a table of what
# Python is *believed* to produce, and the table is only worth anything while
# something on this side executes the same cases.
# ---------------------------------------------------------------------------


INT_PARITY = [
    (0, "0"),
    (999, "999"),
    (1000, "1,000"),
    (1500, "1,500"),
    (1234567, "1,234,567"),
    (-1234, "-1,234"),
]

CONTEXT_PARITY = [
    (None, COST_PLACEHOLDER),
    (0, "0"),
    (999, "999"),
    (1000, "1,000"),
    (14433.6, "14,434"),
    (1234567, "1,234,567"),
    (-1234, "-1,234"),
]

DURATION_PARITY = [
    (1, "1s"),
    (45, "45s"),
    (59, "59s"),
    (60, "1m"),
    (90, "1m"),
    (599, "9m"),
    (3599, "59m"),
    (3600, "1h 00m"),
    (3660, "1h 01m"),
    (3900, "1h 05m"),
    (7200, "2h 00m"),
    (86399, "23h 59m"),
    (86400, "1d 0h"),
    (90000, "1d 1h"),
    (200000, "2d 7h"),
    (525600, "6d 2h"),
]


@pytest.mark.parametrize("value,expected", INT_PARITY)
def test_fmt_int_matches_the_dashboard_table(value, expected):
    assert fmt_int(value) == expected


def test_fmt_int_placeholder():
    assert fmt_int(None) == COST_PLACEHOLDER


@pytest.mark.parametrize("value,expected", CONTEXT_PARITY)
def test_fmt_context_matches_the_dashboard_table(value, expected):
    assert fmt_context(value) == expected


def test_the_two_known_cross_language_differences_are_still_the_only_two():
    """Asserted so neither can be corrected on one side alone.

    ``fmt_int`` truncates through ``int()`` where ``toLocaleString`` renders
    the fraction, and ``fmt_context`` uses Python's banker's rounding where
    ``Math.round`` takes a half away from zero. Neither is reachable from a
    caller today — both figures are token counts — and both would be a real
    disagreement if one ever were. The TypeScript side asserts the same two
    values from the other direction.
    """
    assert fmt_int(2.7) == "2"  # formatNumber(2.7) === '2.7'
    assert fmt_context(2.5) == "2"  # formatContext(2.5) === '3'


@pytest.mark.parametrize("seconds,expected", DURATION_PARITY)
def test_the_duration_rule_matches_the_dashboard_table(seconds, expected):
    """Three implementations, one rule.

    ``doctor._duration`` and ``commands._usage_age`` are two Python copies
    (``commands`` declines to import ``doctor``, which is reached from inside
    every ``load_config``, and says so); ``dateFormat.formatDuration`` is the
    third, in TypeScript. Nothing held any of the three together before this.
    """
    from istota.commands import _usage_age
    from istota.doctor import _duration

    assert _duration(seconds) == expected
    assert _usage_age(seconds) == expected
