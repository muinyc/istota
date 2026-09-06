"""Where a markdown code fence starts and ends in *model* output (F38).

``toml_fence.py`` answers this for a file the **user** writes. This module is
the same question about a string the **model** wrote, and it takes that
module's two decisions verbatim because they were paid for once already:

**Both markers are anchored to a line.** The three health OCR modules each
carried ``r"```(?:[a-zA-Z]+)?\\s*\\n(.*?)\\n```"`` and ``context.py`` carried a
near-identical one, none of them anchored — so a block ended at the first
backtick run appearing anywhere after the fence opened, including one inside
the JSON the block was carrying. A model asked for JSON about a markdown
document quotes backticks routinely.

**Two searches rather than one non-greedy expression, and that is not a
refactor.** ``open(.*?)close`` is quadratic when no closer matches: every
opener is a fresh start position and each rescans to EOF. Measured on this
repository's own expression, against a document of repeated openers with no
closer: 0.10s at 16 KB, 1.64s at 64 KB, **26.5s at 256 KB**. The health
modules feed it whole OCR responses, so the input size is the model's to
choose. The two-search form below is flat at every one of those sizes.

That measurement is also the reason two members of the family this module was
meant to absorb are *not* here, which is worth stating because the shape looks
identical and is not. ``context._parse_relevant_ids`` and
``session/result._CODE_FENCE_PATTERN`` both spell their closer as a bare
backtick run rather than as ``\\n`` plus a backtick run, so a document of
repeated openers supplies its own closers and neither is quadratic — measured
at 0.000s and 0.002s on the same 256 KB input. ``context`` is converted anyway,
for the anchoring. ``session/result`` is not converted at all: it *removes*
every fenced block rather than unwrapping one, and it does so to decide
whether leaked tool-call XML is inside a code block, so line-anchoring it
would flag an inline `````<invoke````` as a malformed result. That is a
user-visible change this stage has no mandate to make; a comment there names
this module and says so.

**The bounds are loose, for ``toml_fence``'s reason.** The expressions being
replaced had no ``^`` at all, so almost any bound is a narrowing and a
narrowing breaks input that used to work. The indent is unbounded, a marker
is three-or-more backticks, the trailing class is every whitespace but a
newline (a CRLF's ``\\r``, a pasted non-breaking space), and a leading BOM is
named separately because it is not ``\\s``. The one deliberate difference from
``toml_fence`` is that the opener's language tag is *optional* here, since a
model writes `````json``, ``````` and `````JSON`` interchangeably.

stdlib-only leaf; imports nothing from the package. Never raises.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

# Whitespace that is not a line break, which is what carries a CRLF's `\r`
# and a pasted non-breaking space.
_FENCE_INDENT = r"[^\S\n]*"
_FENCE_TICKS = r"`{3,}"

# Nothing may precede either marker on its line but whitespace (and, at the
# very start, a BOM). The opener's info string is unconstrained: a model
# writes ```json, ``` and ```JSON interchangeably, and an unrecognised tag
# must not turn a well-formed block into no block at all.
FENCE_OPEN_RE = re.compile(
    rf"^﻿?{_FENCE_INDENT}{_FENCE_TICKS}[^\n]*\n", re.MULTILINE,
)
FENCE_CLOSE_RE = re.compile(
    rf"^{_FENCE_INDENT}{_FENCE_TICKS}[^\S\n]*$", re.MULTILINE,
)

# A backtick run at the very start, and one at the very end, both used only
# by `strip_fences`'s two loose-tail branches.
_LEADING_RUN_RE = re.compile(rf"\A{_FENCE_TICKS}")
_TRAILING_RUN_RE = re.compile(rf"{_FENCE_TICKS}\Z")

# The two widest-substring fallbacks, greedy on purpose: `.*` with DOTALL is
# a single forward scan, and the point is the *outermost* pair of brackets.
_WIDEST_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_WIDEST_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

_LANG_OPENERS: dict[str, re.Pattern[str]] = {}


def _opener_for(lang: str | None) -> re.Pattern[str]:
    if lang is None:
        return FENCE_OPEN_RE
    key = lang.lower()
    cached = _LANG_OPENERS.get(key)
    if cached is None:
        cached = re.compile(
            rf"^﻿?{_FENCE_INDENT}{_FENCE_TICKS}{re.escape(key)}"
            rf"[^\n]*\n",
            re.MULTILINE | re.IGNORECASE,
        )
        _LANG_OPENERS[key] = cached
    return cached


def iter_fenced_blocks(text: str, *, lang: str | None = None) -> Iterator[str]:
    """Yield each fenced block's body, stripped, in document order.

    Linear in ``len(text)``: every search resumes past the previous closer,
    and a failed closer search ends the walk — nothing after a position with
    no closer can have one either.
    """
    opener_re = _opener_for(lang)
    pos = 0
    while True:
        opener = opener_re.search(text, pos)
        if opener is None:
            return
        closer = FENCE_CLOSE_RE.search(text, opener.end())
        if closer is None:
            return
        yield text[opener.end():closer.start()].strip()
        pos = closer.end()


def find_fenced_block(text: str, *, lang: str | None = None) -> str | None:
    """The first fenced block's body, stripped, or ``None``.

    ``None`` means no opener, or an opener with no closer. A caller needing
    to tell those apart uses the two expressions directly.
    """
    for block in iter_fenced_blocks(text, lang=lang):
        return block
    return None


def strip_fences(text: str) -> str:
    """Unwrap a fence the text *starts* with, or return it stripped.

    Only a leading fence, which is what both copies this replaces did — a
    fence in the middle of a longer answer is left where it is, because the
    prose around it is what the caller was told to parse.

    **The tail is deliberately looser than the head**, and that is the one
    place this module keeps a bound ``toml_fence`` would not. Both replaced
    copies dropped a trailing backtick run wherever it sat, and both handled
    an opener with no closer at all by returning the rest — a truncated model
    response is an ordinary event. Line-anchoring the *opener* is what stops a
    stray run mid-document being read as a fence; an unanchored closer at the
    very end of a string that already began with a fence can truncate
    nothing, so tightening it would only lose parses.
    """
    s = text.strip()

    opener = FENCE_OPEN_RE.match(s)
    if opener is None:
        # A degenerate single-line ```{"a": 1}``` has no newline for the
        # opener to end on. Strip the runs off both ends, which is what
        # `memory.curation.prompt` did for this case.
        if _LEADING_RUN_RE.match(s) and "\n" not in s:
            return s.strip("`").strip()
        return s

    body = s[opener.end():]
    closer = FENCE_CLOSE_RE.search(body)
    if closer is not None:
        return body[:closer.start()].strip()

    trimmed = body.rstrip()
    if _TRAILING_RUN_RE.search(trimmed):
        trimmed = _TRAILING_RUN_RE.sub("", trimmed)
    return trimmed.strip()


def candidate_json_blocks(text: str) -> list[str]:
    """JSON candidates to try in order, de-duplicated, order preserved.

    Every fenced block first, then the whole text, then the widest
    ``{ ... }`` substring, then the widest ``[ ... ]``. The model
    occasionally prepends prose ("Here are the biomarkers I found:") or
    fences its answer after being told not to, and the two widest-substring
    arms are what carry a fence this module's anchoring declines to read —
    an opener sharing its line with prose, or a closer with a word after it.
    """
    candidates: list[str] = list(iter_fenced_blocks(text))
    candidates.append(text.strip())
    obj = _WIDEST_OBJECT_RE.search(text)
    if obj:
        candidates.append(obj.group(0))
    arr = _WIDEST_ARRAY_RE.search(text)
    if arr:
        candidates.append(arr.group(0))

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique
