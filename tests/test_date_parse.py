"""The shared loose-date rule (F3).

Three health modules carried the same parser and two of the three validated
the ISO branch. The tests here are the rule itself; each caller's own file
covers what it does with the answer.
"""

from datetime import date

import pytest

from istota.date_parse import is_future_date, parse_loose_date


class TestTheIsoBranchValidates:
    """The behaviour ``health/parser.py`` did not have.

    A well-formed string naming a day that does not exist is ``None``, not
    the string back. Both other copies already did this; the strict one
    wins, and both consumers of the loose one already refused these strings
    one layer further on.
    """

    @pytest.mark.parametrize("raw", [
        "2026-02-31",   # February has no 31st
        "2026-13-45",   # neither field is in range
        "2026-13-01",   # month out of range
        "2026-00-10",   # month zero
        "2026-01-00",   # day zero
        "2025-02-29",   # 2025 is not a leap year
        "2026-04-31",   # April has 30 days
    ])
    def test_an_impossible_iso_date_is_rejected(self, raw):
        assert parse_loose_date(raw) is None

    @pytest.mark.parametrize("raw", [
        "2026-02-28",
        "2024-02-29",   # 2024 is a leap year
        "1970-01-01",
        "2026-12-31",
    ])
    def test_a_real_iso_date_comes_back_verbatim(self, raw):
        assert parse_loose_date(raw) == raw


class TestTheUsBranch:
    """Identical in all three copies, and it moves across unchanged."""

    @pytest.mark.parametrize("raw,expected", [
        ("11/28/2024", "2024-11-28"),
        ("1/5/2024", "2024-01-05"),
        ("01/05/2024", "2024-01-05"),
    ])
    def test_four_digit_years(self, raw, expected):
        assert parse_loose_date(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("11/28/68", "2068-11-28"),   # 68 < 70 -> 20xx
        ("11/28/69", "2069-11-28"),   # the last year on the 20xx side
        ("11/28/70", "1970-11-28"),   # the pivot itself is 19xx
        ("11/28/85", "1985-11-28"),
        ("11/28/99", "1999-11-28"),
        ("11/28/00", "2000-11-28"),
    ])
    def test_the_two_digit_pivot_is_seventy(self, raw, expected):
        assert parse_loose_date(raw) == expected

    @pytest.mark.parametrize("raw", ["13/45/2026", "2/30/2026", "0/1/2026"])
    def test_an_impossible_us_date_is_rejected(self, raw):
        assert parse_loose_date(raw) is None


class TestTheShapesItRefuses:
    @pytest.mark.parametrize("raw", [
        None, "", "   ", "not a date", "2026", "2026-02", "28-11-2024",
        "2024-11-28T09:00:00Z",   # a timestamp is not this parser's shape
        "11.28.2024",
        "2026-2-8",               # the ISO branch wants two digits
    ])
    def test_refused(self, raw):
        assert parse_loose_date(raw) is None

    def test_surrounding_whitespace_is_stripped(self):
        assert parse_loose_date("  2026-02-28\n") == "2026-02-28"


class TestItTakesAnyValueOffJsonLoads:
    """Two of the three callers pass a value straight off ``json.loads``.

    Both coerced with ``str()`` before matching, which is why the parameter
    is ``object``. Narrowing it would put that coercion back at the sites
    this module collapses.
    """

    @pytest.mark.parametrize("raw", [0, False, [], {}, 20260228, ["2026-02-28"]])
    def test_a_non_string_never_raises(self, raw):
        assert parse_loose_date(raw) is None

    def test_a_number_that_happens_to_read_as_a_date_is_still_refused(self):
        # str(20260228) has no separators, so neither branch matches.
        assert parse_loose_date(20260228) is None


class TestIsFutureDate:
    """Its one job is to drop a row, so it must not drop one by accident."""

    def test_a_later_date_is_future(self):
        assert is_future_date("2026-01-02", today=date(2026, 1, 1)) is True

    def test_today_is_not_future(self):
        assert is_future_date("2026-01-01", today=date(2026, 1, 1)) is False

    def test_an_earlier_date_is_not_future(self):
        assert is_future_date("2025-12-31", today=date(2026, 1, 1)) is False

    @pytest.mark.parametrize("raw", [None, "", "not a date", "2026-02-31"])
    def test_an_unreadable_date_is_not_future(self, raw):
        """Reading "cannot parse" as "in the future" drops a row for the

        wrong reason — the caller's next branch is a silent discard.
        """
        assert is_future_date(raw, today=date(2026, 1, 1)) is False


class TestThereIsOneCopy:
    """The pin: a fourth hand-rolled parser must fail this.

    The three that existed were found only because an audit read every file.
    """

    def test_no_module_outside_date_parse_pivots_a_two_digit_year(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "istota"
        offenders = []
        for path in root.rglob("*.py"):
            if path.name == "date_parse.py":
                continue
            text = path.read_text(encoding="utf-8")
            if 'f"20{year}"' in text or 'f"19{year}"' in text:
                offenders.append(str(path.relative_to(root)))
        assert offenders == [], (
            "these modules re-implement the two-digit-year pivot; call "
            "date_parse.parse_loose_date instead"
        )

    def test_no_health_module_still_declares_normalise_date(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "istota"
        offenders = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if "def _normalise_date(" in p.read_text(encoding="utf-8")
        ]
        assert offenders == []
