"""`timestamps`, and the guard that stops a tenth copy of the same expression.

Two functions, because there are two formats in the stored data. The module
changes neither; what it buys is that the choice is visible in one place and
the fact that a cross-store sort is meaningless is written down somewhere a
reader will find it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from istota.timestamps import iso_now, iso_now_seconds

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "istota"


class TestTheTwoFormats:
    def test_iso_now_is_offset_aware_and_parses_back(self):
        value = iso_now()
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0
        assert value.endswith("+00:00")

    def test_iso_now_seconds_carries_a_z_and_no_fraction(self):
        value = iso_now_seconds()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value), value

    def test_they_are_the_two_expressions_that_were_in_the_tree(self):
        """Byte-for-byte what the nine helpers computed, so no store's format moved."""
        now = datetime(2026, 9, 5, 14, 22, 31, 482913, tzinfo=timezone.utc)
        assert now.isoformat() == "2026-09-05T14:22:31.482913+00:00"
        assert now.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-09-05T14:22:31Z"

    def test_a_cross_format_string_sort_is_not_chronological(self):
        """The fact the module docstring exists to record.

        For one and the same instant the offset-aware form sorts first, because
        `.` (0x2E) and `+` (0x2B) both precede `Z` (0x5A) at the character after
        the seconds field. A query joining `feeds` and `events` on a timestamp
        is comparing across that, and it does not mean what it reads as.
        """
        now = datetime(2026, 9, 5, 14, 22, 31, 482913, tzinfo=timezone.utc)
        later = datetime(2026, 9, 5, 23, 59, 59, tzinfo=timezone.utc)
        assert now.isoformat() < now.strftime("%Y-%m-%dT%H:%M:%SZ")
        # And the ordering survives a genuinely later instant in the other
        # format, which is what makes it a defect rather than a rounding issue.
        assert later.isoformat() > now.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert later.isoformat() < later.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestTheMigratedHelpers:
    """Each caller's private name still resolves, so no call site had to move."""

    def test_briefings(self):
        from istota.briefings.db import _now_iso

        assert _now_iso is iso_now

    def test_feeds_migrate(self):
        from istota.feeds._migrate import _iso_now

        assert _iso_now is iso_now

    def test_health_migrate(self):
        from istota.health._migrate import _iso_now

        assert _iso_now is iso_now

    def test_money_migrate(self):
        from istota.money._migrate import _iso_now

        assert _iso_now is iso_now

    def test_money_portfolio(self):
        from istota.money.portfolio import _iso_now

        assert _iso_now is iso_now

    def test_curation_audit(self):
        from istota.memory.curation.audit import _utc_now

        assert _utc_now is iso_now_seconds


#: Files still carrying their own copy, with the reason.
#:
#: Both were held by a live sibling session when Stage 1 of the duplicate-code
#: consolidation landed, so migrating them would have meant editing another
#: session's working tree. They are `_now` in each file and are a one-line
#: change each: replace the `def` with
#: `from istota.timestamps import iso_now as _now` and drop this entry.
_PENDING = {
    "health/db.py",
    "health/routes.py",
}


class TestNoSecondCopy:
    """Grep guard. The expression lives in `timestamps.py` and nowhere else.

    Deliberately keyed on the *expression* rather than on a helper name: the
    nine copies carried three different names (`_now_iso`, `_iso_now`, `_now`)
    and two of them were inline at a call site with no helper at all, so a name
    is not what a reader would grep for and not what would come back.
    """

    def test_the_expression_appears_only_in_timestamps(self):
        needle = "datetime.now(timezone.utc).isoformat()"
        hits = set()
        for path in SRC.rglob("*.py"):
            rel = str(path.relative_to(SRC))
            if rel == "timestamps.py":
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("#"):
                    continue
                # Prose, not code: `timestamps.py` is excluded above and
                # `web_app.py` names both formats in a docstring, marked up
                # with the double backticks this repo uses throughout.
                if needle in line and "``" not in line:
                    hits.add(rel)
        assert hits == _PENDING, (
            "a new copy of the UTC-now expression appeared (or a pending one was "
            "migrated without updating _PENDING); call timestamps.iso_now instead"
        )

    def test_every_pending_entry_still_exists(self):
        """A stale exemption is how a guard quietly stops guarding."""
        for rel in _PENDING:
            assert (SRC / rel).is_file(), f"{rel} is gone; drop it from _PENDING"
