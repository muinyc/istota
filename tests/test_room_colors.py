"""The room-colour palette, held equal across the three places it is written.

ISSUE-433. The palette is a fixed set of names, and it exists in three
languages at once: Python validates a PATCH against it, TypeScript renders the
picker from it, and CSS carries the actual colour values as token pairs. Those
cannot be one file, so the risk is drift — and drift here is silent in both
directions. A name Python accepts with no CSS token behind it stores a colour
that renders as nothing; a name the picker offers that Python rejects is a
400 on a swatch the user can see and click.

The CSS values themselves are deliberately *not* mirrored into either language.
`location-constants.ts` holds hex because MapLibre paints in WebGL and cannot
resolve `var(--token)`; nothing here has that problem, so `tokens.css` stays
the single source of the colour values and the other two carry names only.
"""

import re
from pathlib import Path

import pytest

from istota.room_colors import ROOM_COLORS

_WEB = Path(__file__).resolve().parent.parent / "web" / "src" / "lib"
_TOKENS = _WEB / "styles" / "tokens.css"
_TS = _WEB / "roomColors.ts"


def _css_block(text: str, selector: str) -> str:
    """The body of one top-level rule. The file has exactly the two theme
    blocks the parity rule is about."""
    start = text.index(selector + " {") + len(selector) + 2
    return text[start:text.index("\n}", start)]


def _room_tokens(block: str) -> set[str]:
    return set(re.findall(r"--room-color-([a-z]+)\s*:", block))


class TestPaletteParity:
    def test_python_and_typescript_name_the_same_colours(self):
        ts = _TS.read_text()
        names = set(re.findall(r"'([a-z]+)'", ts.split("ROOM_COLORS")[1].split("]")[0]))
        assert names == set(ROOM_COLORS)

    @pytest.mark.parametrize("selector", [":root", ":root[data-theme='light']"])
    def test_every_palette_name_has_a_token_in_both_themes(self, selector):
        block = _css_block(_TOKENS.read_text(), selector)
        assert _room_tokens(block) == set(ROOM_COLORS)

    def test_no_orphan_token(self):
        """A token with no palette name behind it is unreachable, and reads to
        the next person as a colour the picker forgot to offer."""
        dark = _room_tokens(_css_block(_TOKENS.read_text(), ":root"))
        assert dark - set(ROOM_COLORS) == set()

    def test_the_palette_is_not_empty_and_the_names_are_plain(self):
        """Guards the guard: every assertion above is vacuously true against an
        empty palette, and the names are interpolated into a CSS custom
        property name, so anything outside `[a-z]` would build a selector
        rather than a token."""
        assert len(ROOM_COLORS) >= 6
        for name in ROOM_COLORS:
            assert re.fullmatch(r"[a-z]+", name), name

    @pytest.mark.parametrize("selector", [":root", ":root[data-theme='light']"])
    def test_the_talk_amber_is_not_in_the_palette(self, selector):
        """`.room-origin.talk` already spends amber in this exact row to mean
        "mirrored to Nextcloud Talk". A room colour that reads as that glyph's
        colour would make a user-chosen tint look like a surface binding.

        Both themes, and the amber is **read out of the same block** rather
        than pinned as a literal: `--accent-amber` differs per theme, so a
        one-theme literal leaves the other unguarded and silently stops
        matching anything the day that token is retuned — passing while its
        whole point is defeated, which is the drift this file exists to catch.
        """
        block = _css_block(_TOKENS.read_text(), selector)
        amber = re.search(r"--accent-amber\s*:\s*([^;]+);", block)
        assert amber, f"no --accent-amber in {selector} to compare against"
        for name in ROOM_COLORS:
            value = re.search(rf"--room-color-{name}\s*:\s*([^;]+);", block).group(1)
            assert value.strip().lower() != amber.group(1).strip().lower()
