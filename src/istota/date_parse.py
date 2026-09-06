"""Loose date parsing for text a model or a person typed, in one place (F3).

Three health modules carried the same parser by copy — ``health/parser.py``
(an EHR paste), ``health/encounter_ocr.py`` and ``health/immunization_ocr.py``
(a model's JSON). Two of the three validated the ISO branch and one did not,
so ``2026-02-31`` and ``2026-13-45`` were returned verbatim by ``parser.py``
and rejected by the other two. The validated copy wins, which is the rule
this spec applies everywhere two copies disagree on a safety property.

**What rejecting an impossible ISO date costs, per call site.** Every caller
already had a path for the same answer arriving from the ``M/D/YYYY`` branch,
which has always validated, so no caller grows a branch. Three of the four
change nothing an operator would see; the fourth does, and the first draft of
this docstring claimed otherwise until a reviewer traced it:

- Both OCR modules already treated ``None`` as "no date on this row" and
  already rejected these strings. No change at all.
- ``parser.parse_paste`` sets ``date_given=None`` and drops the row's
  confidence from ``high`` to ``medium``, which its own docstring describes
  as the case where the UI prompts for a date. On the **web** path that is
  strictly better: ``POST /immunizations/bulk`` already answered exactly
  these strings with a 400 naming the ISO format, so a ``high``-confidence
  ``2026-02-31`` used to travel several screens past the parser that called
  it good, and the review screen keys its "needs a date" row on
  ``!row.date_given`` rather than on the confidence label.
- On the **skill** path it is a real change and not a free one.
  ``import-immunizations --confirm`` refuses a batch containing any dateless
  row, and that check runs *before* the deferred insert — which, unlike the
  web route, never validated the ISO string. So a paste with one impossible
  date used to import, storing ``2026-02-31``, and now refuses the batch.
  That is the right direction (``immunizations._parse_date`` cannot read the
  stored value back, so the dose was silently absent from coverage from the
  moment it was written) but it is a batch that used to succeed, and the
  skill's message now names the offending source lines so ``--dry-run`` is
  not the only way to find them.

Nothing back-fills. An impossible date already stored stays stored.

**The two-digit year pivot is 70**, verbatim from all three copies: ``00``–``69``
is 20xx and ``70``–``99`` is 19xx. It is a convention rather than a
derivation, so it is stated once here rather than re-argued per caller.

``raw`` is typed ``object`` rather than ``str | None`` because two of the
three callers hand it a value straight off ``json.loads`` — an ``int``, a
``list``, whatever the model emitted — and both already coerced with
``str()`` before matching. Narrowing the parameter would push that coercion
back out to the call sites this module exists to collapse.

stdlib-only leaf; imports nothing from the package. Never raises.
"""

from __future__ import annotations

import re
from datetime import date

# A four-digit year, a two-digit month and a two-digit day, and nothing else
# on either side. `fullmatch` is what the three copies used; the shape is
# restated as a constant only so the two branches read alike.
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# US order, one or two digits for month and day, two or four for the year.
_US_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")

# 00-69 -> 2000-2069, 70-99 -> 1970-1999.
TWO_DIGIT_YEAR_PIVOT = 70


def parse_loose_date(raw: object) -> str | None:
    """Return an ISO ``YYYY-MM-DD`` string, or ``None``.

    Accepts an already-ISO string and the ``M/D/YYYY`` / ``M/D/YY`` shape.
    Both branches validate: a well-formed string naming a day that does not
    exist is ``None``, not the string back.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    if _ISO_RE.fullmatch(s):
        try:
            date.fromisoformat(s)
        except ValueError:
            return None
        return s

    m = _US_RE.fullmatch(s)
    if not m:
        return None
    month, day, year = m.groups()
    if len(year) == 2:
        year = (
            f"20{year}" if int(year) < TWO_DIGIT_YEAR_PIVOT else f"19{year}"
        )
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def is_future_date(iso_date: str | None, *, today: date | None = None) -> bool:
    """Whether an ISO date string names a day after ``today``.

    An unparseable or absent string is not in the future — the two OCR
    modules each carried this and each used it to drop a row, where reading
    "cannot parse" as "in the future" would drop a row for the wrong reason.
    """
    if not iso_date:
        return False
    try:
        parsed = date.fromisoformat(iso_date)
    except (ValueError, TypeError):
        return False
    return parsed > (today or date.today())
